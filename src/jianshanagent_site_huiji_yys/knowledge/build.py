"""建文档和切片之后的事：公告、剧情、数据集变成文档；给切片标实体；插件自己的统计。

文档的顺序是公告 → 剧情 → 数据集（都排在条目后面）。数据集里的状态说明和超鬼王只补库里没有的部分，
所以放在最后，拿条目正文来比。
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.documents import BuildResult
from jianshanagent.plugins.knowledge.models import BLOCK_PARA, KIND_DATA, KIND_ENTRY, Block, Chunk
from jianshanagent.plugins.knowledge.pipeline import (
    BuildContext,
    ProcessContext,
    announcement_ref,
)
from jianshanagent.plugins.knowledge.sources import PageRef

from jianshanagent_site_huiji_yys.knowledge.collect import PLUGIN_ID, YysCorpus, yys_corpus
from jianshanagent_site_huiji_yys.knowledge.datasets import (
    CATEGORY_LABELS,
    build_datasets,
    match_key,
    onmyoji_names,
)
from jianshanagent_site_huiji_yys.knowledge.entities import (
    Hit,
    Lexicon,
    entity_stats,
    nickname_map,
)
from jianshanagent_site_huiji_yys.knowledge.posts import PostResult, build_dashen, build_wechat
from jianshanagent_site_huiji_yys.knowledge.settings import YysEnv
from jianshanagent_site_huiji_yys.knowledge.story import build_story, story_id_of


@dataclass(slots=True)
class YysBuilt:
    """建文档时留给后面的东西（放在 ``Built.extras["huiji_yys"]``）。"""

    #: 微信文章末尾清掉的行：文档标识 → 各行
    tail_cleanups: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class YysResult:
    """留给报告的东西（放在 ``ProcessResult.extras["huiji_yys"]``）。"""

    lexicon: Lexicon = field(default_factory=Lexicon)
    #: 切片 ID → 这一片的实体命中（报告里给候选配上下文用）
    hits: dict[str, list[Hit]] = field(default_factory=dict)
    #: （切片类型，这一片的实体命中），统计用
    tagged: list[tuple[str, list[Hit]]] = field(default_factory=list)


def _story_order(ref: PageRef) -> tuple[int, str]:
    stem = story_id_of(ref)
    return (int(stem) if stem.isdigit() else 10**9, stem)


def build_documents(ctx: BuildContext, env: YysEnv) -> None:
    own = yys_corpus(ctx.corpus)
    built, only_titles = ctx.built, ctx.only_titles
    documents = built.documents
    state = YysBuilt()
    built.extras[PLUGIN_ID] = state
    built.totals["duplicate_post_ids"] += 0

    def absorb(result: BuildResult, ref: PageRef) -> None:
        ctx.absorb(result, ref)
        if isinstance(result, PostResult):
            built.totals["duplicate_post_ids"] += result.duplicate_ids
            state.tail_cleanups.update(dict(result.tail_cleanups))

    # 公告：同一帖可能同时出现在月合集和单篇文档里，按帖子 ID 只收一次
    seen_posts: set[str] = set()
    builders = {"wechat": build_wechat, "dashen": build_dashen}
    for kind, fetched in sorted(own.posts, key=lambda item: (item[0], item[1].ref.data_id)):
        ref = fetched.ref
        if fetched.error:
            ctx.fetch_failed(ref, fetched.error)
            continue
        absorb(builders[kind](ref, fetched.value, env, seen_posts), ref)
    # 转发帖的原帖如果本身也在库里，记下它的文档标识
    doc_keys = {doc.doc_key for doc in documents}
    for doc in documents:
        origin_key = f"dashen:{doc.meta.get('origin_id')}"
        if doc.meta.get("repost") and origin_key in doc_keys:
            doc.meta["origin_doc_key"] = origin_key

    # 关掉的公告来源：缓存里的帖子不入库，只把标题、日期、链接留给推断日期用
    reference_docs = 0
    seen_reference: set[str] = set()
    for kind, fetched in sorted(
        own.reference_posts, key=lambda item: (item[0], item[1].ref.data_id)
    ):
        result = builders[kind](fetched.ref, fetched.value, env, seen_reference)
        reference_docs += len(result.documents)
        built.reference_announcements += [
            found for doc in result.documents if (found := announcement_ref(doc, in_kb=False))
        ]

    # 剧情
    for fetched in sorted(own.stories, key=lambda f: _story_order(f.ref)):
        ref = fetched.ref
        if fetched.error:
            ctx.fetch_failed(ref, fetched.error)
            continue
        story = build_story(ref, fetched.value, env, own.story_pages)
        if (
            only_titles is not None
            and ref.title not in only_titles
            and ref.data_id not in only_titles
        ):
            # 调试某个剧情页面时，只留它那一章
            story.documents = [d for d in story.documents if d.title in only_titles]
            story.skipped = [s for s in story.skipped if s[1] in only_titles]
        absorb(story, ref)

    # 数据集文档。状态说明和超鬼王只补库里没有的部分，拿条目正文来比
    entry_text = match_key(
        "\n".join(b.render() for d in documents if d.kind == KIND_ENTRY for b in d.blocks)
    )
    named = {
        title: (own.named_refs[title], doc)
        for title, doc in own.named_docs.items()
        if title in own.named_refs and isinstance(doc, dict)
    }
    ghost = []
    for fetched in own.dataset_docs:
        if fetched.error:
            ctx.fetch_failed(fetched.ref, fetched.error)
        elif isinstance(fetched.value, dict):
            ghost.append((fetched.ref, fetched.value))
    datasets = build_datasets(named, ghost, entry_text, env, ctx.known_titles)
    if only_titles is not None:
        # 调试时只留点名的数据集（写数据集标题或生成的文档标题都行）
        datasets.documents = [
            d
            for d in datasets.documents
            if d.title in only_titles
            or d.meta["dataset"] in only_titles
            or str(d.meta["dataset"]).replace("_", " ") in only_titles
        ]
    documents.extend(datasets.documents)
    built.stats |= {
        "reference_posts": reference_docs,
        "datasets": datasets.stats,
        "dataset_unresolved": dict(datasets.unresolved.most_common()),
    }


# ── 实体 ──────────────────────────────────────────────────────────────


def character_names(own: YysCorpus, env: YysEnv) -> dict[str, str]:
    """数据里当角色出现过的名字 → 出处。实体字典用它认出和妖怪同名的御魂。

    出处有三种：剧情里当过说话人、超鬼王的首领或随从、百鬼棋局的棋子。
    """
    names: dict[str, str] = {}
    cfg = env.datasets
    zzq = own.named_docs.get(cfg.zzq_character)
    for piece in (zzq or {}).get("zzq_character") or []:
        if isinstance(piece, dict) and piece.get("hero_name"):
            names.setdefault(str(piece["hero_name"]).strip(), "百鬼棋局的棋子")
    for fetched in own.dataset_docs:
        doc = fetched.value if isinstance(fetched.value, dict) else {}
        for mission in doc.get("missions") or []:
            if not isinstance(mission, dict):
                continue
            units = [mission.get("boss"), *(mission.get("minions") or [])]
            for unit in units:
                if isinstance(unit, dict) and unit.get("name"):
                    names.setdefault(str(unit["name"]).strip(), "超鬼王的单位")
    speakers: Counter[str] = Counter()

    def walk(steps: Any) -> None:
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if step.get("kind") == "line" and step.get("speaker"):
                speakers[str(step["speaker"]).strip()] += 1
            for option in step.get("options") or []:
                if isinstance(option, dict):
                    walk(option.get("steps"))

    for fetched in own.stories:
        doc = fetched.value if isinstance(fetched.value, dict) else {}
        entries = doc.get("story")
        for story in entries if isinstance(entries, list) else [entries]:
            if not isinstance(story, dict):
                continue
            for chapter in story.get("chapters") or []:
                if isinstance(chapter, dict):
                    walk(chapter.get("steps"))
    for name, count in speakers.items():
        names[name] = f"剧情里的说话人（{count} 句）"
    return names


def build_lexicon(own: YysCorpus, env: YysEnv) -> Lexicon:
    """实体字典（正式名和昵称）全部来自数据——Data:CharacterIndex.json、Data:Soul.json、
    Data:Skin.json；没取到字典就是空的，什么都不标。"""
    cfg = env.entities
    if not cfg.enabled:
        return Lexicon()
    skin = own.named_docs.get(cfg.skin)
    return Lexicon.build(
        own.named_docs.get(cfg.character_index),
        own.named_docs.get(cfg.soul),
        cfg,
        onmyoji=onmyoji_names(own.named_docs.get(cfg.character_index), skin),
        monsters=character_names(own, env),
        onmyoji_nicknames=nickname_map((skin or {}).values()),
    )


def annotate(ctx: ProcessContext, env: YysEnv) -> None:
    """给去重后的切片标实体。"""
    lexicon = build_lexicon(yys_corpus(ctx.corpus), env)
    outcome = YysResult(lexicon=lexicon)
    ctx.result.extras[PLUGIN_ID] = outcome
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in ctx.chunks:
        by_doc.setdefault(chunk.doc_key, []).append(chunk)

    def subject(chunk: Chunk) -> str:
        # 数据集文档讲的是谁记在 meta 里（「山风的皮肤」讲的是山风），当作它自己的页面
        return str(chunk.meta.get("character") or chunk.title)

    confirmed = {
        doc_key: lexicon.confirmed((c.text for c in chunks), subject(chunks[0]))
        for doc_key, chunks in by_doc.items()
    }
    for chunk in ctx.chunks:
        hits = lexicon.find(chunk.text, subject(chunk), confirmed[chunk.doc_key])
        chunk.entities = lexicon.entities(hits)
        outcome.hits[chunk.chunk_id] = hits
        outcome.tagged.append((chunk.kind, hits))


# ── 统计 ──────────────────────────────────────────────────────────────


def _tail_stats(ctx: ProcessContext, env: YysEnv) -> dict[str, Any]:
    """微信文末清理的效果：动了多少篇、多少行，改变了多少个切片，外加几个前后对比。"""
    state = ctx.built.extras.get(PLUGIN_ID)
    cleanups = state.tail_cleanups if isinstance(state, YysBuilt) else {}
    by_key = {doc.doc_key: doc for doc in ctx.built.documents}
    changed = 0
    lines = 0
    samples: list[dict[str, Any]] = []
    for key, removed in cleanups.items():
        doc = by_key.get(key)
        if doc is None:
            continue
        lines += len(removed)
        before = dataclasses.replace(
            doc, blocks=[*doc.blocks, *(Block(BLOCK_PARA, text=t, tight=True) for t in removed)]
        )
        old = {c.content_hash for c in chunk_document(before, env.chunk, ChunkStats())}
        changed += sum(1 for c in ctx.per_doc.get(key, []) if c.content_hash not in old)
        if len(samples) < 5:
            kept = [b.render() for b in doc.blocks[-3:]]
            samples.append({"title": doc.title, "kept_tail": kept, "removed": removed})
    return {
        "documents": sum(1 for key in cleanups if key in by_key),
        "lines_removed": lines,
        "chunks_changed": changed,
        "samples": samples,
    }


def _dataset_stats(ctx: ProcessContext) -> dict[str, Any]:
    """每类数据集文档的文档数、切片数，加上建文档时记下的细节。"""
    documents, per_doc, kept_ids = ctx.built.documents, ctx.per_doc, ctx.kept_ids
    build_stats = ctx.built.stats
    out: dict[str, Any] = {}
    for category, label in CATEGORY_LABELS.items():
        docs = [d for d in documents if d.kind == KIND_DATA and d.meta.get("category") == category]
        chunks = [c for d in docs for c in per_doc.get(d.doc_key, [])]
        out[category] = {
            "label": label,
            "datasets": sorted({str(d.meta.get("dataset")) for d in docs}),
            "documents": len(docs),
            "chunks": len(chunks),
            "chunks_kept": sum(1 for c in chunks if c.chunk_id in kept_ids),
            "details": {
                k: v
                for k, v in (build_stats.get("datasets") or {}).get(category, {}).items()
                if k != "documents"
            },
        }
    return out


def stats(ctx: ProcessContext, env: YysEnv) -> dict[str, Any]:
    documents, kept = ctx.built.documents, ctx.chunks
    outcome = ctx.result.extras.get(PLUGIN_ID)
    if not isinstance(outcome, YysResult):
        outcome = YysResult()
    lexicon = outcome.lexicon
    reposts = [d for d in documents if d.meta.get("repost")]
    return {
        "reposts": {
            "documents": len(reposts),
            "with_origin": sum(1 for d in reposts if d.meta.get("origin_available")),
            "origin_missing": sum(1 for d in reposts if not d.meta.get("origin_available")),
            "origin_in_kb": sum(1 for d in reposts if d.meta.get("origin_doc_key")),
            "chunks": sum(1 for c in kept if c.meta.get("repost")),
        },
        "wechat_tail": _tail_stats(ctx, env),
        "datasets": _dataset_stats(ctx),
        "entities": entity_stats(outcome.tagged)
        | {
            "onmyoji": lexicon.onmyoji,
            "onmyoji_source": lexicon.onmyoji_source,
            "ambiguous": {
                name: {"type": kind, "basis": lexicon.ambiguous_basis.get(name, "")}
                for name, kind in lexicon.ambiguous.items()
            },
            "shadow_words": sorted(lexicon.shadow_words),
            "terms": len(lexicon.terms),
            "nicknames": lexicon.alias_summary(),
            "strict_terms": sorted(lexicon.strict),
        },
    }
