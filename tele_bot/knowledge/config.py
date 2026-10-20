"""Explicit retrieval experiments; production defaults preserve the vector baseline."""

from dataclasses import dataclass
import os

from tele_bot.config.llm.settings import _load_local_env


def kb_option(name: str, default: str = "") -> str:
    return os.environ.get(name, _load_local_env().get(name, default)).strip()


@dataclass(frozen=True)
class IndexingSettings:
    max_chars: int = 1200
    overlap_chars: int = 120
    context_prefix: bool = False

    def __post_init__(self):
        if self.max_chars <= 0 or not 0 <= self.overlap_chars < self.max_chars:
            raise ValueError("invalid KB chunk length or overlap")

    @classmethod
    def from_env(cls):
        return cls(max_chars=int(kb_option("KB_CHUNK_MAX_CHARS", "1200")),
                   overlap_chars=int(kb_option("KB_CHUNK_OVERLAP_CHARS", "120")),
                   context_prefix=kb_option("KB_EMBEDDING_CONTEXT_PREFIX", "false").lower() == "true")


@dataclass(frozen=True)
class RetrievalSettings:
    mode: str = "vector"
    candidate_limit: int = 30
    result_limit: int = 5
    rrf_constant: int = 60
    min_vector_score: float = -1.0
    expansion_enabled: bool = False
    max_expansions: int = 2

    def __post_init__(self):
        if self.mode not in {"vector", "hybrid", "keyword"}:
            raise ValueError("KB search mode must be vector, hybrid or keyword")
        if not 1 <= self.result_limit <= 20 or self.candidate_limit < self.result_limit:
            raise ValueError("KB candidate limit must cover result limit (1..20)")
        if self.rrf_constant < 1 or not -1 <= self.min_vector_score <= 1:
            raise ValueError("invalid RRF constant or cosine threshold")
        if not 0 <= self.max_expansions <= 5:
            raise ValueError("max_expansions must be between 0 and 5")

    @classmethod
    def from_env(cls):
        values = _load_local_env()
        def option(name, default):
            return os.environ.get(name, values.get(name, default)).strip()
        return cls(
            mode=option("KB_SEARCH_MODE", "vector"),
            candidate_limit=int(option("KB_SEARCH_CANDIDATES", "30")),
            result_limit=int(option("KB_SEARCH_RESULTS", "5")),
            rrf_constant=int(option("KB_RRF_CONSTANT", "60")),
            min_vector_score=float(option("KB_MIN_VECTOR_SCORE", "-1")),
            expansion_enabled=option("KB_QUERY_EXPANSION", "false").lower() == "true",
            max_expansions=int(option("KB_QUERY_MAX_EXPANSIONS", "2")),
        )
