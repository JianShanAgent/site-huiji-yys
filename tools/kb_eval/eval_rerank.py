# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["numpy", "pyyaml"]
# ///
"""在我们自己的资料上评测重排序模型（模力方舟上的几个型号）。

办法：还是 eval_quality.py 出的那批问题。每个问题先用向量在**全量库**里取前 K 名（默认 20），
再把这 K 片交给重排序模型打分、重新排，看目标切片的名次变好了多少、一次要多久。

另外看「分数能不能用来判断没查到」：重排序是一片一片独立打分的，所以把目标切片去掉以后，
剩下候选里的最高分就是「库里其实没有答案」时会看到的最高分。两种最高分分得越开，
越能定一个分数线，低于它就说没查到，不硬凑答案。
（同一页的其它切片有时也能答上一部分，所以这个估计偏保守。）

    uv run --no-project tools/kb_eval/eval_rerank.py run      # 调接口打分
    uv run --no-project tools/kb_eval/eval_rerank.py score    # 出结果（可重复跑，不调接口）

打分结果存在 data/kb/eval/rerank/，中断后重跑会接着打。

前提：全量向量已经算完（data/kb/vectors/gitee/），问题的向量在 data/kb/eval/vectors.gitee.npy。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data/kb/eval"
OUT = EVAL / "rerank"
BASE = "https://ai.gitee.com/v1"
TYPE_NAMES = {"entry": "条目", "announcement": "公告", "story": "剧情", "data": "数据"}

# 模力方舟上两种重排序接口：
#   multimodal  POST /rerank/multimodal，query 和 documents 都是对象（{"text":…} / {"image":…}），
#               返回一个列表，每项 {index, score}，index 从 1 数起、按输入顺序
#   text        POST /rerank，query 是字符串、documents 是字符串列表，
#               返回 {results: [{index, relevance_score}]}，index 从 0 数起、按分数排
MODELS: dict[str, str] = {
    "Qwen3-VL-Reranker-8B": "multimodal",
    "Qwen3-VL-Reranker-2B": "multimodal",
    "Qwen3-Reranker-8B": "text",
    "bge-reranker-v2-m3": "text",
}


def api_key() -> str:
    secrets = yaml.safe_load((ROOT / "config/secrets.yaml").read_text(encoding="utf-8"))
    return secrets["gitee_ai"]["api_key"]


def post(path: str, payload: dict, key: str) -> tuple[object, float]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    for attempt in range(5):
        request = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(), headers=headers
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response), time.perf_counter() - started
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code not in (400, 429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"{exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(2 * (attempt + 1))
    raise AssertionError


def rerank(model: str, question: str, docs: list[str], key: str) -> tuple[list[float], float]:
    """返回和 docs 一一对应的分数，以及这次请求用了几秒。"""
    if MODELS[model] == "multimodal":
        payload = {
            "model": model,
            "query": {"text": question},
            "documents": [{"text": doc} for doc in docs],
        }
        data, seconds = post("/rerank/multimodal", payload, key)
        items = data if isinstance(data, list) else data["results"]
        base = min(item["index"] for item in items)  # 实测从 1 数起；万一改成从 0 数也不会错
        pairs = [
            (item["index"] - base, item.get("score", item.get("relevance_score"))) for item in items
        ]
    else:
        payload = {"model": model, "query": question, "documents": docs, "return_documents": False}
        data, seconds = post("/rerank", payload, key)
        pairs = [(item["index"], item["relevance_score"]) for item in data["results"]]
    if sorted(i for i, _ in pairs) != list(range(len(docs))):
        raise RuntimeError(f"{model} 返回的条目和输入对不上：{sorted(i for i, _ in pairs)}")
    scores = [0.0] * len(docs)
    for i, score in pairs:
        scores[i] = float(score)
    return scores, seconds


def candidates(top_k: int) -> list[dict]:
    """每个问题在全量库里按向量取前 top_k 名。"""
    questions = json.loads((EVAL / "questions.json").read_text("utf-8"))
    n = len(json.loads((EVAL / "corpus.json").read_text("utf-8")))
    queries = np.load(EVAL / "vectors.gitee.npy")[n:]
    texts: dict[str, str] = {}
    with (ROOT / "data/kb/chunks.jsonl").open(encoding="utf-8") as lines:
        for line in lines:
            chunk = json.loads(line)
            texts[chunk["chunk_id"]] = chunk["text"]
    store = ROOT / "data/kb/vectors/gitee"
    with (store / "index.jsonl").open(encoding="utf-8") as lines:
        ids = [json.loads(line)["chunk_id"] for line in lines]
    docs = np.fromfile(store / "vectors.f32", dtype="<f4").reshape(len(ids), -1)
    missing = set(texts) - set(ids)
    if missing:
        raise SystemExit(f"全量向量还差 {len(missing)} 片没算完，先跑完 embed")
    keep = np.array([chunk_id in texts for chunk_id in ids])
    scores = queries @ docs.T
    scores[:, ~keep] = -1.0
    out = []
    for q, row in zip(questions, scores, strict=True):
        order = np.argsort(-row)[:top_k]
        gold_rank = int((row > row[ids.index(q["chunk_id"])]).sum()) + 1
        out.append(
            {
                "question": q["question"],
                "type": q["type"],
                "gold": q["chunk_id"],
                "embed_rank": gold_rank,
                "candidates": [ids[i] for i in order],
                "embed_scores": [round(float(row[i]), 4) for i in order],
                "texts": [texts[ids[i]] for i in order],
            }
        )
    return out


def run(args: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    key = api_key()
    items = candidates(args.top_k)
    slim = [{k: v for k, v in item.items() if k != "texts"} for item in items]
    (OUT / "candidates.json").write_text(json.dumps(slim, ensure_ascii=False), "utf-8")
    inside = sum(item["gold"] in item["candidates"] for item in items)
    print(f"{len(items)} 个问题，每个取向量前 {args.top_k} 名；目标切片在候选里的有 {inside} 个")
    for model in args.models:
        path = OUT / f"{model}.json"
        done = json.loads(path.read_text("utf-8")) if path.exists() else {}
        started = time.perf_counter()
        for i, item in enumerate(items):
            if item["question"] in done:
                continue
            scores, seconds = rerank(model, item["question"], item["texts"], key)
            done[item["question"]] = {"scores": scores, "seconds": round(seconds, 2)}
            path.write_text(json.dumps(done, ensure_ascii=False), "utf-8")
            if (i + 1) % 20 == 0:
                print(
                    f"  {model} {i + 1}/{len(items)}，{time.perf_counter() - started:.0f}s",
                    flush=True,
                )
        print(f"{model} 完成", flush=True)


def score(_args: argparse.Namespace) -> None:
    items = json.loads((OUT / "candidates.json").read_text("utf-8"))
    kinds = np.array([item["type"] for item in items])
    top_k = len(items[0]["candidates"])
    inside = np.array([item["gold"] in item["candidates"] for item in items])

    def line(label: str, rank: np.ndarray) -> None:
        by_kind = "，".join(
            f"{TYPE_NAMES[k]} {(rank[kinds == k] == 1).mean():.0%}" for k in TYPE_NAMES
        )
        print(
            f"  {label}：第 1 名 {(rank == 1).mean():.0%}，前 3 {(rank <= 3).mean():.0%}，"
            f"前 5 {(rank <= 5).mean():.0%}，MRR {(1 / rank).mean():.3f}｜第 1 名分类型：{by_kind}"
        )

    embed_rank = np.array([item["embed_rank"] for item in items], dtype=float)
    print(f"{len(items)} 个问题，全量库 → 向量前 {top_k} 名 → 重排序")
    print(
        f"目标切片进了前 {top_k} 名的：{inside.sum()}/{len(items)}"
        "（没进的重排序救不回来，名次按原样算）"
    )
    line("只用向量（不重排）", embed_rank)
    for model in MODELS:
        path = OUT / f"{model}.json"
        if not path.exists():
            continue
        done = json.loads(path.read_text("utf-8"))
        if any(item["question"] not in done for item in items):
            print(f"\n{model}：还没跑完")
            continue
        rank = embed_rank.copy()
        seconds, with_answer, without_answer, gold_scores = [], [], [], []
        for i, item in enumerate(items):
            result = done[item["question"]]
            scores = np.array(result["scores"])
            seconds.append(result["seconds"])
            if not inside[i]:
                without_answer.append(scores.max())
                continue
            gold = item["candidates"].index(item["gold"])
            rank[i] = (scores > scores[gold]).sum() + 1
            gold_scores.append(scores[gold])
            with_answer.append(scores.max())
            without_answer.append(np.delete(scores, gold).max())
        print(f"\n{model}")
        line("重排后", rank)
        better, worse = (rank < embed_rank).sum(), (rank > embed_rank).sum()
        same = len(items) - better - worse
        print(
            f"  逐题对比只用向量：更好 {better} 题，相同 {same} 题，更差 {worse} 题"
            f"｜一次请求（{top_k} 片）中位 {np.median(seconds):.1f} 秒，最慢 {max(seconds):.1f} 秒"
        )
        has, none = np.array(with_answer), np.array(without_answer)
        q = lambda a: "/".join(f"{x:.2f}" for x in np.quantile(a, [0.1, 0.5, 0.9]))  # noqa: E731
        # 找一条分数线：最高分不低于它算「查到了」。看两类各判对多少
        best = max(
            ((has >= t).mean() + (none < t).mean(), t)
            for t in np.unique(np.concatenate([has, none]))
        )
        t = best[1]
        print(
            f"  最高分（10%/中位/90% 分位）：有答案时 {q(has)}，去掉答案后 {q(none)}；"
            f"目标切片自己的分数 {q(np.array(gold_scores))}"
        )
        print(
            f"  分数线取 {t:.2f} 时：有答案的 {(has >= t).mean():.0%} 判成查到了，"
            f"没答案的 {(none < t).mean():.0%} 判成没查到"
        )
        lost = [i for i in np.argsort(embed_rank - rank)[:3] if rank[i] > embed_rank[i]]
        for i in lost:
            print(f"    变差：{int(embed_rank[i])} → {int(rank[i])}  {items[i]['question']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--top-k", type=int, default=20)
    p_run.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    p_run.set_defaults(func=run)
    sub.add_parser("score").set_defaults(func=score)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
