"""用评测题目检验机器人真正走的那条检索路径（数据库 + 向量和关键词融合），以及大模型最后答得对不对。

和 eval_quality.py 的区别：那个脚本直接拿向量文件用 numpy 算，评的是嵌入模型本身；
这个脚本调用的就是机器人运行时的代码（Retriever、chat 插件、查证工作流），
评的是「接到机器人上以后」的效果。不启动 QQ 网关，不会让机器人上线。

准备：数据库里已经导入了知识库（``python -m jianshanagent.plugins.knowledge load``），
题目在 data/kb/eval/questions.json（eval_quality.py 生成的）。数据库地址用环境变量
JIANSHANAGENT_POSTGRES_DSN 指定（本机测试库，别指到线上）。

::

    .venv/bin/python tools/kb_eval/eval_retrieval.py rank      # 目标切片排第几（不调大模型）
    .venv/bin/python tools/kb_eval/eval_retrieval.py answer    # 走完整的对话 + 查证工作流，再判对错
    .venv/bin/python tools/kb_eval/eval_retrieval.py answer --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import dataclasses
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingSettings
from jianshanagent.core.llm import LLMClient, LLMSettings, user_message
from jianshanagent.core.logging import setup_logging
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import PluginHost, ToolSpec
from jianshanagent.plugins.knowledge.plugin import retrieval_settings
from jianshanagent.plugins.knowledge.retrieval import Retriever, query_provider
from jianshanagent.plugins.knowledge.store import PostgresKnowledgeStore
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "kb" / "eval"
CHUNKS = ROOT / "data" / "kb" / "chunks.jsonl"
TOPS = (1, 3, 5, 10)


def _questions(limit: int | None) -> list[dict[str, Any]]:
    """题目：出题脚本照着切片出的（questions.json），后面接上手工加的真实问题（questions.real.json）。

    真实问题是用户在 QQ 里问过机器人的原话，单独放一个文件——重新出题会整个覆盖 questions.json。
    格式和出的题一样；能回答它的切片不止一片时，多写一项 ``chunk_ids``
    （``chunk_id`` 写最主要的那片）。
    """
    questions: list[dict[str, Any]] = json.loads((EVAL / "questions.json").read_text("utf-8"))
    real = EVAL / "questions.real.json"
    if real.exists():
        questions += json.loads(real.read_text("utf-8"))
    return questions[:limit] if limit else questions


def _gold(item: dict[str, Any]) -> list[str]:
    """能回答这道题的切片。"""
    return list(item.get("chunk_ids") or [item["chunk_id"]])


def _database(config: AppConfig) -> Database:
    if not config.postgres_dsn:
        raise SystemExit("先用环境变量 JIANSHANAGENT_POSTGRES_DSN 指定（本机测试用的）数据库")
    return Database(config.postgres_dsn, pool_max=4)


def _rank_table(label: str, ranks: list[int | None]) -> None:
    total = len(ranks)
    cells = "  ".join(
        f"前{k}名 {sum(r is not None and r <= k for r in ranks) / total:4.0%}" for k in TOPS
    )
    print(f"{cells}  没进 {sum(r is None for r in ranks)} 题  {label}")


async def _rank(args: argparse.Namespace) -> None:
    config = load_config()
    questions = _questions(args.limit)
    settings = retrieval_settings(config.plugin("knowledge").get("retrieval") or {})
    embedding = EmbeddingSettings.from_config(config)
    assert embedding is not None, "core.embedding.yaml 没有启用"
    db = _database(config)
    await db.connect()
    store = PostgresKnowledgeStore(db)
    embedder = EmbeddingClient(query_provider(embedding.provider(), settings))
    routes = {
        f"向量 + 关键词补 {settings.keyword_slots} 片（机器人实际用的）": Retriever(
            store, embedder, settings
        ),
        "只用向量": Retriever(store, embedder, dataclasses.replace(settings, keyword_slots=0)),
        "只用关键词（向量用不了时的退路）": Retriever(store, None, settings),
    }
    info = await store.info()
    print(f"库里 {info.chunks} 片，{info.with_vectors} 片有向量，指纹 {info.fingerprint}")
    print(f"{len(questions)} 个问题，每次给 {settings.top_k} 片\n")

    ranks: dict[str, list[int | None]] = {label: [] for label in routes}
    seconds: list[float] = []
    try:
        for item in questions:
            for label, retriever in routes.items():
                result = await retriever.search(item["question"])
                # 题目里记的是切片文件里的 id；库里的 id 前面带着是哪个 wiki 的
                ids = [hit.chunk.local_id for hit in result.hits]
                found = [ids.index(gold) + 1 for gold in _gold(item) if gold in ids]
                ranks[label].append(min(found) if found else None)
                if retriever.settings is settings and retriever.embedder is not None:
                    seconds.append(result.seconds)
                    assert result.mode != "keyword", result.vector_skipped
    finally:
        await embedder.aclose()
        await db.close()

    for label, values in ranks.items():
        _rank_table(label, values)
    seconds.sort()
    print(
        f"\n一次检索（算问题向量 + 查库）用时：中位数 {statistics.median(seconds):.2f} 秒，"
        f"最慢 {seconds[-1]:.2f} 秒"
    )
    for label, values in ranks.items():
        for question, rank in zip(questions, values, strict=True):
            if rank is None and "关键词（" not in label:
                print(f"  {label}，没进前 {settings.top_k} 片：{question['question']}")


class Verdict(BaseModel):
    #: correct = 和参考原文一致；wrong = 说错了或张冠李戴；no_answer = 说没查到 / 没给出答案
    verdict: str
    reason: str = ""


_JUDGE = """你是评测员。下面有一个问题、能回答它的参考原文、以及一个机器人的回答。
判断机器人的回答是否正确，只返回 JSON：
{{"verdict": "correct" | "wrong" | "no_answer", "reason": "一句话"}}

- correct：回答里包含了问题问的那个事实，并且和参考原文一致（说得更多、措辞不同都没关系）。
- wrong：回答给出了答案，但和参考原文矛盾，或者把别的东西（别的年份、别的活动、别的角色）
  当成了答案。
- no_answer：回答说没查到、不确定，或者没有给出问题问的那个事实。

问题：{question}

参考原文：
{reference}

机器人的回答：
{answer}
"""


async def _answer(args: argparse.Namespace) -> None:
    config = load_config()
    # 只开回答问题用得到的插件；不开 memory（不往测试库里写记忆）、不接 QQ
    config.files["core.plugins"] = {"enabled": ["wiki_query", "knowledge", "research", "chat"]}
    questions = _questions(args.limit)
    reference = {}
    with CHUNKS.open(encoding="utf-8") as lines:
        wanted = {chunk_id for q in questions for chunk_id in _gold(q)}
        for line in lines:
            row = json.loads(line)
            if row["chunk_id"] in wanted:
                reference[row["chunk_id"]] = row["text"]
    missing = wanted - set(reference)
    assert not missing, f"题目指向的切片在 {CHUNKS} 里找不到：{sorted(missing)}"

    llm_settings = LLMSettings.from_config(config)
    assert llm_settings is not None, "core.llm.yaml 没有启用"
    llm = LLMClient(llm_settings)
    db = _database(config)
    await db.connect()
    host = PluginHost(config)
    host.services["llm"] = llm
    host.services["database"] = db  # 不注册 conversation_store：对话历史放内存，不写库
    host.load()
    research = host.services.get("research")
    assert host.chat_handler is not None and research is not None, "查证工作流没有启用"

    # 记下每道题的查证经过：chat 插件每次都从服务表里现取 research，所以包一层就能录到
    current: contextvars.ContextVar[str] = contextvars.ContextVar("eval_chat_id")
    outcomes: dict[str, Any] = {}
    asked: dict[str, str] = {}
    run = research.run

    async def recording(question: str, **kwargs: Any) -> Any:
        outcome = await run(question, **kwargs)
        outcomes[current.get()] = outcome
        asked[current.get()] = question
        return outcome

    research.run = recording
    other_tools: dict[str, list[str]] = {}

    def counting(spec: ToolSpec) -> ToolSpec:
        async def handler(arguments: dict[str, Any], message: InboundMessage) -> Any:
            other_tools.setdefault(message.chat_id, []).append(spec.name)
            return await spec.handler(arguments, message)

        return ToolSpec(spec.name, spec.description, spec.parameters, handler, spec.plugin_id)

    for name in list(host.tools):
        host.tools[name] = counting(host.tools[name])

    gate = asyncio.Semaphore(args.concurrency)
    rows: list[dict[str, Any]] = []

    async def one(index: int, item: dict[str, Any]) -> None:
        async with gate:
            chat_id = f"eval-{index}"
            current.set(chat_id)
            message = InboundMessage(
                message_id=f"eval-{index}",
                chat_scope=ChatScope.C2C,
                chat_id=chat_id,
                user_id=chat_id,
                content=item["question"],
            )
            started = time.perf_counter()
            assert host.chat_handler is not None
            out = await host.chat_handler(message)
            seconds = time.perf_counter() - started
            answer = out.content if out else ""
            gold = _gold(item)
            outcome = outcomes.get(chat_id)
            evidence = outcome.evidence if outcome else []
            verdict = await llm.chat_json(
                [
                    user_message(
                        _JUDGE.format(
                            question=item["question"],
                            reference="\n\n".join(reference[chunk_id] for chunk_id in gold),
                            answer=answer,
                        )
                    )
                ],
                Verdict,
                profile="chat",
            )
            rows.append(
                {
                    "index": index,
                    "question": item["question"],
                    "type": item["type"],
                    "header": item["header"],
                    "answer": answer,
                    "verdict": verdict.verdict,
                    "reason": verdict.reason,
                    "researched": outcome is not None,
                    "asked": asked.get(chat_id),
                    "status": outcome.status if outcome else None,
                    "query": outcome.query if outcome else None,
                    "steps": outcome.steps if outcome else [],
                    "levels": outcome.levels if outcome else 0,
                    "rejections": outcome.rejections if outcome else 0,
                    "fixed": outcome.fixed if outcome else False,
                    "problems": outcome.problems if outcome else [],
                    "research_seconds": round(outcome.seconds, 1) if outcome else None,
                    "gold_in_evidence": any(e.key in gold for e in evidence),
                    "evidence_chars": sum(len(e.text) for e in evidence),
                    "other_tools": other_tools.get(chat_id, []),
                    "seconds": round(seconds, 1),
                }
            )
            print(
                f"[{len(rows)}/{len(questions)}] {verdict.verdict:<9} {item['question']}",
                flush=True,
            )

    try:
        await asyncio.gather(*(one(i, item) for i, item in enumerate(questions)))
    finally:
        for service in host.services.values():
            if hasattr(service, "aclose"):
                await service.aclose()
        await db.close()

    rows.sort(key=lambda row: row["index"])
    out_path = EVAL / (f"answers.{args.name}.json" if args.name else "answers.json")
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    total = len(rows)
    count = {v: sum(r["verdict"] == v for r in rows) for v in ("correct", "wrong", "no_answer")}
    times = sorted(r["seconds"] for r in rows)
    print(
        f"\n{total} 题：答对 {count['correct']}（{count['correct'] / total:.0%}），"
        f"答错 {count['wrong']}，没答出来 {count['no_answer']}"
    )
    print(
        f"交给查证工作流的 {sum(r['researched'] for r in rows)} 题，"
        f"目标切片在取到的资料里的 {sum(r['gold_in_evidence'] for r in rows)} 题"
    )
    levels = [r["levels"] for r in rows]
    rewritten = sum(bool(r["query"]) and r["query"] != r["asked"] for r in rows)
    print(
        f"只查了第一级的 {sum(n == 1 for n in levels)} 题，"
        f"升到第二级的 {sum(n == 2 for n in levels)} 题，"
        f"升到第三级的 {sum(n >= 3 for n in levels)} 题；"
        f"被复核打回过的 {sum(r['rejections'] > 0 for r in rows)} 题"
        f"（其中按复核意见快速修正后发出的 {sum(r['fixed'] for r in rows)} 题），"
        f"最后说没查到的 {sum(r['status'] == 'not_found' for r in rows)} 题；"
        f"检索问句被补过正式名或时间的 {rewritten} 题"
    )
    print(f"一题从提问到答完：中位数 {statistics.median(times):.1f} 秒，最慢 {times[-1]:.1f} 秒")
    print(f"逐题结果在 {out_path}")
    for row in rows:
        if row["verdict"] != "correct":
            steps = " → ".join(row["steps"]) or "没有交给查证工作流"
            print(f"\n[{row['verdict']}] {row['question']}（目标：{row['header']}）")
            print(
                f"  经过：{steps}；打回 {row['rejections']} 次；"
                f"目标切片在资料里：{row['gold_in_evidence']}"
            )
            print(f"  回答：{row['answer'][:200]}")
            print(f"  判定理由：{row['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rank = sub.add_parser("rank", help="目标切片在检索结果里排第几")
    rank.add_argument("--limit", type=int, help="只跑前 N 题")
    answer = sub.add_parser("answer", help="走完整的对话 + 查证工作流，再判对错")
    answer.add_argument("--limit", type=int, help="只跑前 N 题")
    answer.add_argument(
        "--name", help="结果写到 answers.<name>.json（对比几种配法时用），不给就是 answers.json"
    )
    # 同时跑几题。卡住它的是每分钟请求数：测试和线上机器人共用同一个密钥的每分钟 100 次，
    # 一题约 6 次模型调用（含判分）。不开思考的配法一题十几秒，3 题同时跑就是每分钟约 70 次，
    # 不能再高；开思考的配法也有一半的题二十来秒就答完，平均四五十秒一题，最多开到 5
    answer.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    setup_logging(level="ERROR", json_logs=False)  # 逐次检索的日志不打，只看汇总
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_rank(args) if args.command == "rank" else _answer(args))


if __name__ == "__main__":
    sys.exit(main())
