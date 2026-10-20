"""Run a public KB regression corpus without API calls, or measure real embeddings.

python -m tele_bot.knowledge.evaluation --output evaluation/KNOWLEDGE_REPORT.md
python -m tele_bot.knowledge.evaluation --live --output evaluation/KNOWLEDGE_LIVE_REPORT.md
"""

import argparse
import json
from pathlib import Path

from tele_bot.config.llm.settings import AliBailianSettings
from tele_bot.knowledge.config import RetrievalSettings
from tele_bot.knowledge.ingestion import DocumentParser
from tele_bot.knowledge.models import DocumentSource
from tele_bot.knowledge.search import KnowledgeIndexer, KnowledgeSearch
from tele_bot.llm.embeddings import AliBailianEmbeddingClient, EmbeddingBatchResult
from tele_bot.persistence.kb_store import KnowledgeStore


CASES = Path(__file__).resolve().parents[2] / "evaluation" / "knowledge_cases.json"


class FrozenEmbeddings:
    """Hand-authored vectors exercise ranking; never presented as quality evidence."""
    def __init__(self, corpus):
        self.vectors = {doc["text"]: doc["vector"] for doc in corpus["documents"]}
        for query in corpus["queries"]:
            self.vectors[query["query"]] = query["vector"]
            self.vectors.update(zip(query.get("expansions", []), query.get("expansion_vectors", [])))

    def embed(self, texts):
        vectors = []
        for text in texts:
            # Frozen prefix ablation intentionally holds vectors fixed. Only
            # --live measures the semantic effect of contextual input prefixes.
            if text.startswith("Document: "):
                text = text.split("\n\n", 1)[1]
            vectors.append(tuple(self.vectors[text]))
        return [EmbeddingBatchResult(tuple(vectors), len(texts), None, "frozen-fixture")] if texts else []


def evaluate(corpus, *, live=False):
    settings = (AliBailianSettings.from_env() if live else AliBailianSettings(
        api_key="unused", base_url="https://unused.invalid", model="fixture", reasoning_model="fixture",
        timeout_seconds=10, embedding_dimensions=4, embedding_daily_token_budget=100_000,
    ))
    rows = []
    for mode, expansion, prefix in [("vector", False, False), ("hybrid", False, False),
                                    ("hybrid", True, False), ("hybrid", False, True)]:
        client = AliBailianEmbeddingClient(settings) if live else FrozenEmbeddings(corpus)
        store = KnowledgeStore(":memory:")
        try:
            store.create_space("chat", "Public fixture", "admin")
            store.add_member("chat", "admin", "reader")
            store.create_space("private-chat", "Private", "other-user")
            indexer = KnowledgeIndexer(store, client, settings, embedding_context_prefix=prefix)
            document_ids = {}
            for doc in corpus["documents"]:
                parsed = DocumentParser().parse_bytes(doc["title"], doc["text"].encode())
                pending = indexer.enqueue(chat_id="chat", actor_user_id="admin", source=DocumentSource.REPORT,
                                          source_ref=doc["id"], parsed=parsed)
                job = indexer.run("chat", "admin", pending)
                if job.status.value != "succeeded":
                    raise RuntimeError(job.error)
                document_ids[pending.document.id] = doc["id"]
            expansions = {q["query"]: q.get("expansions", []) for q in corpus["queries"]}
            engine = KnowledgeSearch(store, client, settings,
                                     retrieval=RetrievalSettings(mode=mode, expansion_enabled=expansion),
                                     query_expander=lambda query: expansions.get(query, []))
            details = []
            for query in corpus["queries"]:
                denied = False
                try:
                    hits = engine.search(query.get("chat", "chat"), query.get("user", "reader"), query["query"])
                except PermissionError:
                    denied, hits = True, []
                if denied != query.get("denied", False):
                    raise AssertionError(f"unexpected authorization result: {query['id']}")
                found = list(dict.fromkeys(document_ids[hit.document_id] for hit in hits))
                relevant = set(query["relevant"])
                reciprocal = next((1 / rank for rank, doc in enumerate(found[:5], 1) if doc in relevant), 0)
                details.append(dict(id=query["id"], category=query["category"], found=found, denied=denied,
                                    recall1=len(relevant & set(found[:1])) / len(relevant) if relevant else None,
                                    recall5=len(relevant & set(found[:5])) / len(relevant) if relevant else None,
                                    mrr5=reciprocal if relevant else None))
            relevant_rows = [row for row in details if row["recall1"] is not None]
            negatives = [row for row in details if row["category"] == "no-relevant"]
            tokens, cost = store.daily_embedding_usage(store.require_member("chat", "admin").id)
            rows.append(dict(mode=mode, expansion=expansion, prefix=prefix,
                             model=settings.embedding_model if live else "hand-authored-fixture",
                             dimensions=settings.embedding_dimensions, tokens=tokens, cost=cost,
                             recall1=sum(r["recall1"] for r in relevant_rows) / len(relevant_rows),
                             recall5=sum(r["recall5"] for r in relevant_rows) / len(relevant_rows),
                             mrr5=sum(r["mrr5"] for r in relevant_rows) / len(relevant_rows),
                             false_positives=sum(bool(r["found"]) for r in negatives),
                             denials=sum(r["denied"] for r in details), details=details))
        finally:
            store.close()
    return rows


def report(rows, *, live=False):
    lines = ["# 知识库检索评测", "",
             "模式：" + ("实时 Embedding（真实模型调用）" if live else "离线固定向量回归（人工指定向量，无模型调用）"), "",
             f"模型：{rows[0]['model']}；维度：{rows[0]['dimensions']}。",
             f"本次总输入 tokens：{sum(row['tokens'] for row in rows)}；估算费用：￥{sum(row['cost'] for row in rows):.6f}（离线仅为记账回归值）。", "",
             "公开小语料覆盖精确工具名、型号、缩写、语义改写、无资料及两种越权案例。",
             "Recall/MRR 仅统计 8 个有标注相关文档的查询；两条无相关资料查询单列误召回；两条权限案例单列拒绝。",
             "指标在去重文档级计算，候选每支 30、返回 5、RRF k=60；默认不设额外余弦拒绝阈值（>=-1），保留原向量基线排序。", "",
             "| 模式 | 扩写 | 上下文前缀 | Recall@1 | Recall@5 | MRR@5 | 无资料误召回 / 2 | 权限拒绝 / 2 |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['mode']} | {row['expansion']} | {row['prefix']} | {row['recall1']:.3f} | "
                     f"{row['recall5']:.3f} | {row['mrr5']:.3f} | {row['false_positives']} | {row['denials']} |")
    lines += [""]
    if live:
        lines += ["本次使用真实 Embedding API，对比发生在同一公开项目领域小语料上；样本量有限，不能外推到私有项目资料。",
                  f"混合相对基线 MRR@5 变化：{rows[1]['mrr5'] - rows[0]['mrr5']:+.3f}；"
                  f"前缀相对混合 MRR@5 变化：{rows[3]['mrr5'] - rows[1]['mrr5']:+.3f}。"]
    else:
        lines += ["离线向量是为回归排名逻辑人工指定的，不能证明真实 Embedding 的质量提升。",
                  "离线前缀组刻意固定向量，只检查分离保存及输入通路；需要 --live 才能衡量前缀的语义效果。"]
    lines += [
              "扩写使用案例中人工给定的别名，仅作为独立消融，不代表自动生成扩写的可靠性。",
              "默认向量召回总会尝试返回最近候选，无资料查询存在误召回；当前没有用 RRF 分数推断答案可信度或自动声称有依据。",
              "默认仍为 vector，扩写及上下文前缀均关闭。用真实项目查询重新标注并运行 --live 后再决定默认值。",
              "本评测只评价检索；生成答案忠实度尚需单独以答案和引用标注评测。", "",
              "融合依据：[Elastic RRF 官方文档](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)，每支贡献 1/(k+rank)，不混加原始分数。", "",
              "## 每条查询（返回文档 ID，顺序即排名）", ""]
    for row in rows:
        lines.append(f"### {row['mode']} expansion={row['expansion']} prefix={row['prefix']}")
        lines.append("")
        for item in row["details"]:
            lines.append(f"- {item['id']} ({item['category']}): " + ("DENIED" if item["denied"] else ", ".join(item["found"]) or "无命中"))
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--live", action="store_true", help="call configured embedding API; consumes token budget")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = json.loads(args.cases.read_text(encoding="utf-8"))
    result = report(evaluate(corpus, live=args.live), live=args.live)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
