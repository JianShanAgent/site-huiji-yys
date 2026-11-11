# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["numpy", "pyyaml"]
# ///
"""在我们自己的资料上评测嵌入质量：各家托管接口 对 官方做法（本机模型当参照）。

办法：从切片里按类型抽一批「目标切片」，让对话模型针对每一片写一个玩家会问的问题；
再把目标切片所在页面的全部切片（最难区分的干扰项）加上随机切片凑成一个小库。
各家各自把小库和问题算成向量，看每个问题能不能把它的目标切片排到前面；
另外算一种「混用」：小库用本机的向量、问题用托管接口的向量（本机算全量、线上只算问题时就是这样）。

三步，中间结果都存在 data/kb/eval/ 下，可以分开跑、重复跑：

    uv run --no-project tools/kb_eval/eval_quality.py build   # 出题、选小库（调对话模型）
    uv run --no-project tools/kb_eval/eval_quality.py embed --sides gitee  # 算向量
                                                # （选 local 时本机服务要先起来）
    uv run --no-project tools/kb_eval/eval_quality.py score   # 出结果

全量嵌入跑完以后，再用同一批问题在整个库里找（不只是 800 片的小库）：

    uv run --no-project tools/kb_eval/eval_quality.py full
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data/kb/eval"
# 每一家：显示名、地址、型号名、密钥在 config/secrets.yaml 里的位置、一次几条、额外参数
PROVIDERS: dict[str, dict] = {
    "local": {
        "label": "官方做法（本机）",
        "url": "http://127.0.0.1:8765/v1/embeddings",
        "model": "Qwen/Qwen3-VL-Embedding-8B",
        "secret": None,
        "batch": 32,
        "extra": {},
    },
    "siliconflow": {
        "label": "硅基流动",
        "url": "https://api.siliconflow.cn/v1/embeddings",
        "model": "Qwen/Qwen3-VL-Embedding-8B",
        "secret": "siliconflow",
        "batch": 16,
        "extra": {},
    },
    "gitee": {
        "label": "模力方舟",
        "url": "https://ai.gitee.com/v1/embeddings",
        "model": "Qwen3-VL-Embedding-8B",
        "secret": "gitee_ai",
        "batch": 16,
        # 不带 dimensions 时只给前 1024 维，而且不重新归一化
        "extra": {"dimensions": 4096},
    },
}
PER_TYPE = {"entry": 20, "announcement": 20, "story": 10, "data": 10}
TYPE_NAMES = {"entry": "条目", "announcement": "公告", "story": "剧情", "data": "数据"}

QUESTION_PROMPT = """下面是阴阳师 wiki 知识库里的一段内容。\
请写一个玩家在 QQ 群里可能会问的问题，要求：
1. 答案能从这段内容里找到，而且最好只有这段内容能回答；
2. 口语化，不超过 30 个字，像真人随口问的；
3. 要带上必要的名字（式神、活动、皮肤等），让人知道在问什么，但不要照抄原文的句子；
4. 只输出问题本身，不要解释。

内容：
{text}"""


def secrets() -> dict:
    return yaml.safe_load((ROOT / "config/secrets.yaml").read_text(encoding="utf-8"))


def post(url: str, payload: dict, key: str | None = None, timeout: int = 600) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(6):
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 5:
                raise RuntimeError(f"{exc.code} {exc.read().decode()[:300]}") from exc
            time.sleep(3 * (attempt + 1))
    raise AssertionError


def in_input_order(data: list[dict]) -> list[dict]:
    """把一次请求返回的向量排回输入顺序。

    硅基流动一次超过 8 条时，``index`` 每 8 条从 0 重新数（实测 16 条返回 0–7、0–7），
    但返回顺序本身和输入一致。所以只有 ``index`` 正好是 0..n-1 各一个时才按它排，否则信返回顺序。
    """
    if sorted(item["index"] for item in data) == list(range(len(data))):
        return sorted(data, key=lambda item: item["index"])
    return data


def embed(name: str, texts: list[str]) -> np.ndarray:
    provider = PROVIDERS[name]
    label, batch = provider["label"], provider["batch"]
    key = secrets()[provider["secret"]]["api_key"] if provider["secret"] else None
    out: list[np.ndarray] = []
    started = time.perf_counter()
    for start in range(0, len(texts), batch):
        payload = {
            "model": provider["model"],
            "input": texts[start : start + batch],
            "encoding_format": "base64",
            **provider["extra"],
        }
        data = in_input_order(post(provider["url"], payload, key)["data"])
        for item in data:
            raw = item["embedding"]
            if isinstance(raw, str):
                out.append(np.frombuffer(base64.b64decode(raw), dtype="<f4"))
            else:
                out.append(np.asarray(raw, dtype=np.float32))
        done = min(start + batch, len(texts))
        if done % (batch * 5) == 0 or done == len(texts):
            print(
                f"  {label} {done}/{len(texts)}，{time.perf_counter() - started:.0f}s", flush=True
            )
    vectors = np.vstack(out)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def build(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    with (EVAL / "chunks.snapshot.jsonl").open(encoding="utf-8") as lines:
        chunks = [json.loads(line) for line in lines]
    by_type: dict[str, list[dict]] = {}
    for chunk in chunks:
        if 150 <= chunk["chars"] <= 1000 and not chunk["meta"].get("nav"):
            by_type.setdefault(chunk["type"], []).append(chunk)

    targets: list[dict] = []
    for kind, count in PER_TYPE.items():
        seen_docs: set[str] = set()
        pool = by_type[kind][:]
        rng.shuffle(pool)
        for chunk in pool:
            if chunk["doc_key"] not in seen_docs:
                seen_docs.add(chunk["doc_key"])
                targets.append(chunk)
            if len(seen_docs) == count:
                break

    llm = yaml.safe_load((ROOT / "config/core.llm.yaml").read_text(encoding="utf-8"))
    key = secrets()["llm"]["api_key"]
    questions = []
    for i, chunk in enumerate(targets):
        body = {
            "model": llm["profiles"]["chat"]["model"],
            "messages": [{"role": "user", "content": QUESTION_PROMPT.format(text=chunk["text"])}],
            "thinking": {"type": "disabled"},
            "temperature": 0.7,
            "max_completion_tokens": 200,
        }
        reply = post(llm["base_url"].rstrip("/") + "/chat/completions", body, key, timeout=90)
        question = reply["choices"][0]["message"]["content"].strip().splitlines()[0].strip()
        questions.append(
            {
                "question": question,
                "chunk_id": chunk["chunk_id"],
                "type": chunk["type"],
                "header": chunk["header"],
            }
        )
        print(
            f"{i + 1:>2} [{TYPE_NAMES[chunk['type']]}] {question}  ←  {chunk['header'][:40]}",
            flush=True,
        )

    # 小库：目标切片所在页面（同名页面）的全部切片 + 随机切片
    titles = {chunk["title"] for chunk in targets}
    corpus = {c["chunk_id"]: c for c in chunks if c["title"] in titles}
    rest = [c for c in chunks if c["chunk_id"] not in corpus]
    rng.shuffle(rest)
    for chunk in rest[: max(args.corpus - len(corpus), 0)]:
        corpus[chunk["chunk_id"]] = chunk
    rows = [
        {"chunk_id": c["chunk_id"], "type": c["type"], "header": c["header"], "text": c["text"]}
        for c in corpus.values()
    ]
    (EVAL / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=1), "utf-8"
    )
    (EVAL / "corpus.json").write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    same_page = sum(1 for c in corpus.values() if c["title"] in titles)
    print(f"问题 {len(questions)} 个；小库 {len(rows)} 片，其中 {same_page} 片和目标切片同页面")


def run_embed(args: argparse.Namespace) -> None:
    questions = json.loads((EVAL / "questions.json").read_text("utf-8"))
    corpus = json.loads((EVAL / "corpus.json").read_text("utf-8"))
    texts = [row["text"] for row in corpus] + [q["question"] for q in questions]
    for name in args.sides:
        np.save(EVAL / f"vectors.{name}.npy", embed(name, texts))


def score(_args: argparse.Namespace) -> None:
    questions = json.loads((EVAL / "questions.json").read_text("utf-8"))
    corpus = json.loads((EVAL / "corpus.json").read_text("utf-8"))
    n = len(corpus)
    index = {row["chunk_id"]: i for i, row in enumerate(corpus)}
    gold = np.array([index[q["chunk_id"]] for q in questions])
    kinds = [q["type"] for q in questions]
    vectors = {
        name: np.load(EVAL / f"vectors.{name}.npy")
        for name in PROVIDERS
        if (EVAL / f"vectors.{name}.npy").exists()
    }

    def report(label: str, docs: np.ndarray, queries: np.ndarray) -> np.ndarray:
        scores = queries @ docs.T
        gold_score = scores[np.arange(len(gold)), gold]
        rank = (scores > gold_score[:, None]).sum(1) + 1
        print(f"\n{label}（{len(questions)} 个问题，小库 {n} 片）")
        print(
            f"  第 1 名命中 {(rank == 1).mean():.0%}，前 5 {(rank <= 5).mean():.0%}，"
            f"前 10 {(rank <= 10).mean():.0%}，前 20 {(rank <= 20).mean():.0%}，"
            f"MRR {(1 / rank).mean():.3f}，名次中位 {int(np.median(rank))}，最差 {rank.max()}"
        )
        for kind, kind_name in TYPE_NAMES.items():
            mask = np.array([k == kind for k in kinds])
            print(
                f"    {kind_name}（{mask.sum()}）：第 1 名 {(rank[mask] == 1).mean():.0%}，"
                f"前 5 {(rank[mask] <= 5).mean():.0%}，前 10 {(rank[mask] <= 10).mean():.0%}"
            )
        print(
            f"  目标切片的相似度平均 {gold_score.mean():.3f}，全库平均 {scores.mean():.3f}，"
            f"差 {gold_score.mean() - scores.mean():.3f}"
        )
        return rank

    ranks = {name: report(PROVIDERS[name]["label"], v[:n], v[n:]) for name, v in vectors.items()}
    if "local" not in vectors:
        return
    local = vectors["local"]
    for name, v in vectors.items():
        if name == "local":
            continue
        label = PROVIDERS[name]["label"]
        same = (v * local).sum(1)
        print(
            f"\n{label} 对 官方：同一段文字两边向量的相似度平均 {same.mean():.4f}，"
            f"中位 {np.median(same):.4f}，最低 {same.min():.4f}（一致应接近 1）"
        )
        mixed = report(f"混用：小库用本机向量，问题用{label}的向量", local[:n], v[n:])
        for title, rank in ((f"全用{label}", ranks[name]), ("混用", mixed)):
            base = ranks["local"]
            print(
                f"  逐题对比官方｜{title}：更好 {(rank < base).sum()} 题，"
                f"相同 {(rank == base).sum()} 题，更差 {(rank > base).sum()} 题"
            )
        worst = [i for i in np.argsort(ranks["local"] - ranks[name])[:6] if ranks[name][i] > 5]
        if worst:
            print(f"  全用{label}时排到 5 名以外、且比官方差得最多的题（官方名次 → {label}名次）：")
            for i in worst:
                item = questions[i]
                print(
                    f"    {ranks['local'][i]} → {ranks[name][i]}  {item['question']}"
                    f"  ←  {item['header'][:36]}"
                )


def full(_args: argparse.Namespace) -> None:
    """同一批问题，在全量向量（data/kb/vectors/<提供方>/）里找目标切片。"""
    questions = json.loads((EVAL / "questions.json").read_text("utf-8"))
    n = len(json.loads((EVAL / "corpus.json").read_text("utf-8")))
    kinds = np.array([q["type"] for q in questions])
    queries = {
        name: np.load(EVAL / f"vectors.{name}.npy")[n:]
        for name in PROVIDERS
        if (EVAL / f"vectors.{name}.npy").exists()
    }
    with (ROOT / "data/kb/chunks.jsonl").open(encoding="utf-8") as lines:
        current = {json.loads(line)["chunk_id"] for line in lines}
    for store_name in PROVIDERS:
        directory = ROOT / "data/kb/vectors" / store_name
        if not (directory / "index.jsonl").exists():
            continue
        with (directory / "index.jsonl").open(encoding="utf-8") as lines:
            ids = [json.loads(line)["chunk_id"] for line in lines]
        docs = np.fromfile(directory / "vectors.f32", dtype="<f4").reshape(len(ids), -1)
        keep = np.array([chunk_id in current for chunk_id in ids])
        row = {chunk_id: i for i, chunk_id in enumerate(ids)}  # 同一片有多条时取最新的
        have = [q["chunk_id"] in row for q in questions]
        norms = np.linalg.norm(docs, axis=1)
        label = PROVIDERS[store_name]["label"]
        print(
            f"\n库：{label}，{len(ids)} 条向量（属于当前切片的 {keep.sum()} 条，"
            f"当前切片共 {len(current)} 片），向量长度 {norms.min():.4f}–{norms.max():.4f}；"
            f"{sum(have)}/{len(questions)} 个问题的目标切片在库里"
        )
        if not all(have):
            print("  还没算完，先不评")
            continue
        gold = np.array([row[q["chunk_id"]] for q in questions])
        for query_name, vectors in queries.items():
            scores = vectors @ docs.T
            scores[:, ~keep] = -1.0
            gold_score = scores[np.arange(len(gold)), gold]
            rank = (scores > gold_score[:, None]).sum(1) + 1
            by_kind = "，".join(
                f"{TYPE_NAMES[k]} {(rank[kinds == k] == 1).mean():.0%}"
                f"/{(rank[kinds == k] <= 5).mean():.0%}"
                for k in TYPE_NAMES
            )
            asker = PROVIDERS[query_name]["label"]
            print(
                f"  问题用{asker}的向量：第 1 名 {(rank == 1).mean():.0%}，"
                f"前 5 {(rank <= 5).mean():.0%}，前 10 {(rank <= 10).mean():.0%}，"
                f"前 20 {(rank <= 20).mean():.0%}，MRR {(1 / rank).mean():.3f}，最差 {rank.max()}"
                f"｜分类型（第 1 名/前 5）：{by_kind}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--corpus", type=int, default=800)
    p_build.add_argument("--seed", type=int, default=20261111)
    p_build.set_defaults(func=build)
    p_embed = sub.add_parser("embed")
    p_embed.add_argument("--sides", nargs="+", choices=list(PROVIDERS), required=True)
    p_embed.set_defaults(func=run_embed)
    sub.add_parser("score").set_defaults(func=score)
    sub.add_parser("full").set_defaults(func=full)
    args = parser.parse_args()
    EVAL.mkdir(parents=True, exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
