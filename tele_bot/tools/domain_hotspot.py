"""
DomainHotspotTool — fetch curated AI / robotics hotspot from multiple sources.

User-triggered (LLM calls when user asks about hotspots), not scheduler-based.

Phase 3 (single-source per domain) extended in Phase 5 to multi-source for
the `robotics` domain. `ai` domain response shape is unchanged.

Safety rules:
- outbound URL allowlist enforced per source (incl. after HTTP redirects)
- single-source HTTP/parse failures → error envelope appended to errors[],
  never raised. All-sources failure → items=[], errors=[...], still no raise
- LRU cache with 30-minute TTL keyed by (domain, limit)
- HTML stripped, entities unescaped, descriptions truncated to 800 chars
- single-source timeout 5s; overall ThreadPoolExecutor wait 10s

Sources:
- ai:       aihot  (https://aihot.virxact.com/rss)
- robotics: 8 parallel — arxiv_cs_ro, robohub, ra_news, techxplore_robotics,
            robotics_tomorrow, qbitai, zhidx, github_robotics

robotics returns shape:
  {domain, fetched_at, sources_succeeded, sources_failed, items:[...], errors:[...]}
ai returns shape (Phase 3 unchanged):
  {domain, source, fetched_at, items:[...]}
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

_LOG = logging.getLogger(__name__)

_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) tele-bot/0.1 DomainHotspotTool"

# Per-source config. parser ∈ {"rss", "atom", "github_json"}.
@dataclass(frozen=True)
class SourceSpec:
    key: str
    url: str
    parser: str
    host: str  # for allowlist enforcement


_AI_SOURCE = SourceSpec(
    key="aihot",
    url="https://aihot.virxact.com/rss",
    parser="rss",
    host="aihot.virxact.com",
)

_ROBOTICS_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="arxiv_cs_ro",
        url=(
            "http://export.arxiv.org/api/query?"
            "search_query=cat:cs.RO&sortBy=submittedDate&sortOrder=descending"
        ),
        parser="atom",
        host="export.arxiv.org",
    ),
    SourceSpec("robohub", "https://robohub.org/feed/", "rss", "robohub.org"),
    SourceSpec(
        "ra_news",
        "https://roboticsandautomationnews.com/feed/",
        "rss",
        "roboticsandautomationnews.com",
    ),
    SourceSpec(
        "techxplore_robotics",
        "https://techxplore.com/rss-feed/robotics-news/",
        "rss",
        "techxplore.com",
    ),
    SourceSpec(
        "robotics_tomorrow",
        "https://www.roboticstomorrow.com/rss/news.aspx",
        "rss",
        "www.roboticstomorrow.com",
    ),
    SourceSpec("qbitai", "https://www.qbitai.com/feed", "rss", "www.qbitai.com"),
    SourceSpec("zhidx", "https://zhidx.com/rss", "rss", "zhidx.com"),
    SourceSpec(
        "github_robotics",
        "https://api.github.com/search/repositories"
        "?q=topic:robotics&sort=stars&per_page={per_source_limit}",
        "github_json",
        "api.github.com",
    ),
)

_ALLOWED_HOSTS: frozenset[str] = frozenset(
    [_AI_SOURCE.host] + [s.host for s in _ROBOTICS_SOURCES]
)


def _validate_source_spec(spec: SourceSpec) -> None:
    """Fail-fast at import time if a SourceSpec's url hostname doesn't
    match its declared host, or if the host isn't in the allowlist.

    Prevents the SourceSpec.host == "api.github.com" but url ==
    "https://evil.example/..." bypass: an attacker editing the source
    list cannot smuggle a non-allowlisted destination by lying about
    `host`. (Committee Q3, Q4 from the second-pass review.)
    """
    # Drop URL template placeholders (e.g. {per_source_limit}) so urlparse
    # sees a syntactically valid URL.
    sample_url = spec.url.format(per_source_limit=1)
    actual_host = urlparse(sample_url).hostname or ""
    if actual_host != spec.host:
        raise RuntimeError(
            f"SourceSpec({spec.key!r}) host mismatch: declared {spec.host!r}, "
            f"url resolves to {actual_host!r}"
        )
    if spec.host not in _ALLOWED_HOSTS:
        raise RuntimeError(
            f"SourceSpec({spec.key!r}) host {spec.host!r} not in allowlist "
            f"{sorted(_ALLOWED_HOSTS)}"
        )


# Validate every spec at import time.
for _spec in (_AI_SOURCE,) + _ROBOTICS_SOURCES:
    _validate_source_spec(_spec)

_LIMIT_MIN = 1
_LIMIT_MAX = 30
_DESC_MAX_CHARS = 800
_CACHE_TTL_SECONDS = 1800  # 30 min (Phase 5: extended from 10 min)
_CACHE_MAXSIZE = 8
_PER_SOURCE_TIMEOUT_SECONDS = 5.0
_OVERALL_TIMEOUT_SECONDS = 10.0
_MAX_WORKERS = 8

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass
class _CacheEntry:
    payload: dict[str, Any]
    expires_at: float


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject 3xx redirects whose Location points to a host outside the allowlist.

    Phase 5 security: prevents a curated source from being silently
    bounced to an arbitrary third-party host.
    """

    def __init__(self, allowed_hosts: frozenset[str]):
        self._allowed = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname or ""
        if host not in self._allowed:
            raise urllib.error.HTTPError(
                newurl, code, f"redirect to disallowed host {host!r}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class DomainHotspotTool:
    timeout: float = _PER_SOURCE_TIMEOUT_SECONDS
    overall_timeout: float = _OVERALL_TIMEOUT_SECONDS
    cache_ttl: float = _CACHE_TTL_SECONDS
    _cache: dict[tuple[str, int], _CacheEntry] = field(default_factory=dict)
    # Optional injection point for tests: a callable(url) -> bytes
    _fetcher: Callable[[str], bytes] | None = None

    def fetch(self, domain: str, limit: int = 10) -> dict[str, Any]:
        if domain not in ("ai", "robotics"):
            raise ValueError(
                f"unknown domain {domain!r}; allowed: ['ai', 'robotics']"
            )

        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 10
        if limit_int < _LIMIT_MIN:
            limit_int = _LIMIT_MIN
        if limit_int > _LIMIT_MAX:
            limit_int = _LIMIT_MAX

        cached = self._cache_get(domain, limit_int)
        if cached is not None:
            _LOG.info("DomainHotspotTool cache hit domain=%s limit=%s", domain, limit_int)
            return cached
        _LOG.info("DomainHotspotTool cache miss domain=%s limit=%s", domain, limit_int)

        if domain == "ai":
            payload = self._fetch_single(_AI_SOURCE, limit_int)
        else:
            payload = self._fetch_multi(_ROBOTICS_SOURCES, limit_int)

        self._cache_put(domain, limit_int, payload)
        return payload

    # ------------------------------------------------------------------
    # single-source fetch (ai domain — Phase 3 shape preserved)
    # ------------------------------------------------------------------

    def _fetch_single(self, source: SourceSpec, limit: int) -> dict[str, Any]:
        items, err = self._fetch_one_source(source, limit)
        if err is not None:
            return {"domain": "ai", "source": source.url, "error": err}
        return {
            "domain": "ai",
            "source": source.url,
            "fetched_at": _utc_iso(),
            "items": items,
        }

    # ------------------------------------------------------------------
    # multi-source fetch (robotics domain — Phase 5)
    # ------------------------------------------------------------------

    def _fetch_multi(
        self, sources: tuple[SourceSpec, ...], limit: int
    ) -> dict[str, Any]:
        per_source = max(1, -(-limit // len(sources)))  # ceil division
        results: list[tuple[SourceSpec, list[dict[str, Any]] | None, str | None]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            future_map = {
                ex.submit(self._fetch_one_source, src, per_source): src
                for src in sources
            }
            try:
                done, not_done = concurrent.futures.wait(
                    future_map, timeout=self.overall_timeout
                )
            except Exception as exc:  # noqa: BLE001
                done, not_done = set(), set(future_map)
                _LOG.warning("ThreadPoolExecutor wait failed: %s", exc)

            for fut in done:
                src = future_map[fut]
                try:
                    items, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    items, err = None, f"task error: {type(exc).__name__}: {exc}"
                results.append((src, items, err))

            for fut in not_done:
                src = future_map[fut]
                fut.cancel()
                results.append(
                    (src, None, f"overall timeout exceeded ({self.overall_timeout}s)")
                )

        merged: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        # Per committee Q1: sources_succeeded must count sources that
        # returned non-empty items. An empty list — e.g. from HTTP 304
        # or a feed that genuinely has zero current items — does NOT
        # advance the aggregate "≥5/8" gate, because the agent has no
        # new content to summarize from that source.
        sources_succeeded = 0
        for src, items, err in results:
            if err is not None or items is None:
                errors.append({"source": src.key, "error": err or "unknown"})
                continue
            if not items:
                # Successful fetch but empty body (304 / empty feed).
                # Not an error — not surfaced in errors[] — but also
                # not counted toward the success threshold.
                continue
            sources_succeeded += 1
            for it in items:
                it["source"] = src.key
                merged.append(it)

        merged.sort(key=_sort_key)
        merged = merged[:limit]

        return {
            "domain": "robotics",
            "fetched_at": _utc_iso(),
            "sources_succeeded": sources_succeeded,
            "sources_failed": len(errors),
            "items": merged,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # per-source fetch + parse
    # ------------------------------------------------------------------

    def _fetch_one_source(
        self, source: SourceSpec, per_source_limit: int
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if source.host not in _ALLOWED_HOSTS:
            return None, f"host {source.host!r} not in allowlist"

        url = source.url.format(per_source_limit=per_source_limit)
        # Re-verify hostname of the actual URL (not just the declared host).
        # Defense in depth against SourceSpec edits that lie about `host`.
        url_host = urlparse(url).hostname or ""
        if url_host not in _ALLOWED_HOSTS:
            return None, f"resolved url host {url_host!r} not in allowlist"
        if url_host != source.host:
            return None, (
                f"SourceSpec host {source.host!r} does not match url host "
                f"{url_host!r}"
            )

        try:
            raw = self._http_get(url)
        except urllib.error.HTTPError as exc:
            # HTTP 304 Not Modified is not an error from our point of view:
            # the source says "nothing new since your conditional request".
            # We treat it as a successful fetch with no items. urllib raises
            # HTTPError for 304 by default; we catch and return empty items.
            if exc.code == 304:
                return [], None
            return None, f"http error: HTTPError {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return None, f"http error: URLError: {reason}"
        except Exception as exc:  # noqa: BLE001
            return None, f"unexpected fetch error: {type(exc).__name__}: {exc}"

        try:
            if source.parser == "rss":
                items = _parse_rss(raw, per_source_limit)
            elif source.parser == "atom":
                items = _parse_atom(raw, per_source_limit)
            elif source.parser == "github_json":
                items = _parse_github_json(raw, per_source_limit)
            else:
                return None, f"unknown parser {source.parser!r}"
        except ET.ParseError as exc:
            return None, f"xml parse error: {exc}"
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return None, f"parse error: {type(exc).__name__}: {exc}"

        return items, None

    def _http_get(self, url: str) -> bytes:
        if self._fetcher is not None:
            return self._fetcher(url)
        opener = urllib.request.build_opener(
            _AllowlistRedirectHandler(_ALLOWED_HOSTS)
        )
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with opener.open(req, timeout=self.timeout) as resp:  # noqa: S310
            return resp.read()

    # ------------------------------------------------------------------
    # cache
    # ------------------------------------------------------------------

    def _cache_get(self, domain: str, limit: int) -> dict[str, Any] | None:
        entry = self._cache.get((domain, limit))
        if entry is None:
            return None
        if entry.expires_at < time.time():
            self._cache.pop((domain, limit), None)
            return None
        return entry.payload

    def _cache_put(self, domain: str, limit: int, payload: dict[str, Any]) -> None:
        if len(self._cache) >= _CACHE_MAXSIZE:
            oldest = min(self._cache.items(), key=lambda kv: kv[1].expires_at)[0]
            self._cache.pop(oldest, None)
        self._cache[(domain, limit)] = _CacheEntry(
            payload=payload, expires_at=time.time() + self.cache_ttl
        )


# ----------------------------------------------------------------------
# parsers
# ----------------------------------------------------------------------

def _parse_rss(xml_bytes: bytes, limit: int) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items = root.findall(".//item")
    out: list[dict[str, Any]] = []
    for el in items[:limit]:
        out.append({
            "title": _clean_text(_text(el, "title")),
            "link": (_text(el, "link") or "").strip(),
            "summary": _clean_html(_text(el, "description") or ""),
            "published": _text(el, "pubDate") or "",
            "categories": [
                c.text.strip() for c in el.findall("category") if c.text
            ],
        })
    return out


def _parse_atom(xml_bytes: bytes, limit: int) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    entries = root.findall(f"{_ATOM_NS}entry")
    out: list[dict[str, Any]] = []
    for el in entries[:limit]:
        title_el = el.find(f"{_ATOM_NS}title")
        # Atom <link href="..."/> — pick first rel=alternate or first link
        link_url = ""
        for link in el.findall(f"{_ATOM_NS}link"):
            rel = link.get("rel", "alternate")
            if rel == "alternate":
                link_url = link.get("href", "")
                break
        if not link_url:
            link_el = el.find(f"{_ATOM_NS}link")
            link_url = link_el.get("href", "") if link_el is not None else ""

        summary_el = el.find(f"{_ATOM_NS}summary")
        if summary_el is None:
            summary_el = el.find(f"{_ATOM_NS}content")
        published_el = el.find(f"{_ATOM_NS}published")
        if published_el is None:
            published_el = el.find(f"{_ATOM_NS}updated")

        authors = [
            (a.find(f"{_ATOM_NS}name").text or "")
            for a in el.findall(f"{_ATOM_NS}author")
            if a.find(f"{_ATOM_NS}name") is not None
        ]

        out.append({
            "title": _clean_text(title_el.text if title_el is not None else ""),
            "link": link_url.strip(),
            "summary": _clean_html(
                (summary_el.text or "") if summary_el is not None else ""
            ),
            "published": (published_el.text or "") if published_el is not None else "",
            "categories": [
                c.get("term", "")
                for c in el.findall(f"{_ATOM_NS}category")
                if c.get("term")
            ],
            "authors": [a.strip() for a in authors if a.strip()],
        })
    return out


def _parse_github_json(raw: bytes, limit: int) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, dict) and "message" in data and "items" not in data:
        # GitHub rate-limit / error envelope
        raise ValueError(data.get("message", "unknown github error"))
    items = data.get("items", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for it in items[:limit]:
        stars = int(it.get("stargazers_count", 0))
        forks = int(it.get("forks_count", 0))
        desc = it.get("description") or ""
        summary = _clean_html(f"stars={stars} forks={forks} desc={desc}")
        out.append({
            "title": it.get("full_name", ""),
            "link": it.get("html_url", ""),
            "summary": summary,
            "published": it.get("pushed_at", "") or it.get("updated_at", ""),
            "categories": list(it.get("topics", []) or []),
            "stars": stars,
        })
    return out


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _text(el: ET.Element, tag: str) -> str | None:
    child = el.find(tag)
    if child is None:
        return None
    return child.text or ""


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return _WS_RE.sub(" ", html.unescape(s)).strip()


def _clean_html(raw: str) -> str:
    no_tags = _TAG_RE.sub(" ", raw)
    unescaped = html.unescape(no_tags)
    collapsed = _WS_RE.sub(" ", unescaped).strip()
    if len(collapsed) > _DESC_MAX_CHARS:
        return collapsed[:_DESC_MAX_CHARS].rstrip() + "…"
    return collapsed


def _sort_key(item: dict[str, Any]) -> tuple:
    """Sort by published desc, then (source, title) ascending for stability.

    Items lacking `published` are sorted last (tuple ordering: empty/None < strings
    when negated). We use a flag bit so missing dates always end up last regardless
    of string comparison quirks.
    """
    pub = item.get("published") or ""
    missing = 1 if not pub else 0
    # negate published by sorting descending via tuple inversion not possible
    # for strings — we use Python's `sort(reverse=True)`? But other keys are
    # ascending. So we apply a transform: return (missing, neg_pub, source, title)
    # where neg_pub uses str comparison reversed. Trick: sort key returns
    # (missing, pub_for_desc, source, title) — for desc on pub string, we
    # rely on caller using `merged.sort(key=...)` with no reverse and
    # transform pub to a value that sorts in reverse string order. Easiest:
    # return tuple where pub is negated via a sentinel.
    return (missing, _ReverseStr(pub), item.get("source", ""), item.get("title", ""))


class _ReverseStr:
    """Wrap a string so it sorts in reverse order under default tuple sort."""

    __slots__ = ("s",)

    def __init__(self, s: str) -> None:
        self.s = s

    def __lt__(self, other: "_ReverseStr") -> bool:
        return self.s > other.s

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ReverseStr) and self.s == other.s


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def to_json(payload: dict[str, Any]) -> str:
    """Serialize payload as compact JSON for tool return."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
