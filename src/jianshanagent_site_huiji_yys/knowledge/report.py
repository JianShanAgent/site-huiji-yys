"""质量报告里阴阳师 wiki 自己的几节：转发帖、微信文末清理、实体标注、由数据集生成的文档，
以及前两节里和公告、剧情有关的几句。写在报告的哪里见核心 report.py 的 ``SLOT_*``。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from jianshanagent.plugins.knowledge.models import KIND_DATA, KIND_LABELS, Chunk
from jianshanagent.plugins.knowledge.pipeline import ProcessResult
from jianshanagent.plugins.knowledge.report import (
    SLOT_COUNTS,
    SLOT_DATES,
    SLOT_END,
    SLOT_PAGES,
    SLOT_PROBLEMS,
    SLOT_REPLACED,
    ReportWriter,
    chunk_block,
    entity_line,
    fenced,
)
from jianshanagent.plugins.knowledge.settings import Settings

from jianshanagent_site_huiji_yys.knowledge.entities import (
    NEUTRAL_TYPES,
    ONMYOJI_FROM_DATA,
    TYPE_ONMYOJI,
    TYPE_SOUL,
    Hit,
    Lexicon,
)
from jianshanagent_site_huiji_yys.knowledge.posts import STATUS_TITLE_ONLY
from jianshanagent_site_huiji_yys.knowledge.story import STATUS_EMPTY_CHAPTER

STATUS_LABELS = {
    STATUS_EMPTY_CHAPTER: "Data 里的剧情章节没有文字",
    STATUS_TITLE_ONLY: "公告只有标题（按配置不入库）",
}


def write(
    slot: str,
    out: ReportWriter,
    result: ProcessResult,
    settings: Settings,
    *,
    lexicon: Lexicon | None,
    hits: dict[str, list[Hit]],
) -> None:
    if slot == SLOT_COUNTS:
        _disabled_sources(out, result)
    elif slot == SLOT_REPLACED:
        _replaced_line(out, result)
    elif slot == SLOT_PAGES:
        _data_docs(out, result)
    elif slot == SLOT_PROBLEMS:
        _repost_section(out, result)
        _tail_section(out, result)
    elif slot == SLOT_DATES:
        _entity_section(out, result, settings, lexicon, hits)
    elif slot == SLOT_END:
        _dataset_section(out, result)


def _disabled_sources(out: ReportWriter, result: ProcessResult) -> None:
    stats = result.stats
    disabled = (stats.get("corpus") or {}).get("disabled_sources") or {}
    if not disabled:
        return
    dating = stats.get("announcements_for_dating") or {}
    out.add(
        "**已关闭的公告来源**（配置里 `enabled: false`，不入库、不新取数）："
        + "，".join(f"{k}（清单里 {v} 份文档）" for k, v in disabled.items())
        + f"。缓存里读到 {(stats.get('corpus') or {}).get('reference_post_docs', 0)} 份文档、"
        f"{stats['build'].get('reference_posts', 0)} 篇帖子，"
        f"其中带日期的 {dating.get('reference_only', 0)} 篇只把标题、发布日期、链接"
        "留给条目推断日期当依据，本身不产生切片。"
    )
    out.add("")


def _replaced_line(out: ReportWriter, result: ProcessResult) -> None:
    corpus = result.stats["corpus"]
    out.add(
        "- 内容全在 Data 里、由剧情或帖子代替（不重复入库）："
        f"{corpus['story_pages'] + corpus['post_pages']} 个"
        f"（剧情页面 {corpus['story_pages']}，帖子页面 {corpus['post_pages']}）"
    )


def _data_docs(out: ReportWriter, result: ProcessResult) -> None:
    stats = result.stats
    corpus = stats["corpus"]
    out.add(
        f"Data 文档：公告合集 {corpus['post_docs']} 份，剧情 {corpus['story_docs']} 份；"
        f"跳过索引和草稿文件 {corpus['skipped_data']} 份（文件名以 `_` 开头）。"
        "同一帖在多份文档里重复出现、只收一次的有 "
        f"{stats['build'].get('duplicate_post_ids', 0)} 帖。"
    )
    out.add("")


def _repost_section(out: ReportWriter, result: ProcessResult) -> None:
    add = out.add
    reposts = result.stats.get("reposts") or {}
    out.section("大神转发帖")
    if not reposts.get("documents"):
        add("本次没有转发帖。")
        add("")
        return
    add(
        f"大神上的转发帖（`type: 4`）全部入库：{reposts['documents']} 帖、{reposts['chunks']} 片，"
        "元数据带 `repost: true`。每帖分成「转发语」和「原帖」两节"
        "（都在切片的标题路径或正文小标题里）。"
    )
    add("")
    add(
        f"- 数据里带着原帖全文的 {reposts['with_origin']} 帖：原帖正文收进同一篇，"
        "另记原帖作者（有的话）、发布日期、链接；`origin_available: true`"
    )
    add(
        f"- 原帖已删除或不可见的 {reposts['origin_missing']} 帖：只有转发语，"
        "外加一句说明和原帖链接；`origin_available: false`"
    )
    add(
        f"- 原帖本身也是库里的一篇（官方转发自己的帖子）的 {reposts['origin_in_kb']} 帖："
        "元数据里另记 `origin_doc_key`，原帖正文会在库里出现两次（一次是它自己，一次在转发帖里）"
    )
    add("")
    samples = [c for c in result.chunks if c.meta.get("repost")]
    picked = [
        next((c for c in samples if c.meta.get("origin_available")), None),
        next((c for c in samples if not c.meta.get("origin_available")), None),
    ]
    for chunk in picked:
        if chunk is not None:
            add(chunk_block(chunk))
            add("")


def _tail_section(out: ReportWriter, result: ProcessResult) -> None:
    add = out.add
    tail = result.stats.get("wechat_tail") or {}
    out.section("微信文末的署名和推荐阅读")
    if not tail.get("documents"):
        add(
            "规则已经写好（见这个站点插件 `knowledge/posts.py` 的 `wechat_tail_start`），"
            "但本次没有微信文章入库（来源关闭，或没有命中），所以没有生效的记录。"
        )
        add("")
        return
    add(
        f"清理了 {tail['documents']} 篇文章末尾的 {tail['lines_removed']} 行"
        f"（配图署名和推荐阅读的标题），因此内容发生变化的切片有 {tail['chunks_changed']} 片。"
        "落款（「……的扫地工 参上」）保留；正文中间的图注不动。"
    )
    add("")
    for index, sample in enumerate(tail.get("samples") or [], 1):
        add(f"**样例 {index}**：{sample['title']}")
        add("")
        add("清理后的结尾（最后三行）：")
        add("")
        add(fenced("\n".join(sample["kept_tail"])))
        add("")
        add("清理掉的行：")
        add("")
        add(fenced("\n".join(sample["removed"])))
        add("")


def _entity_section(
    out: ReportWriter,
    result: ProcessResult,
    settings: Settings,
    lexicon: Lexicon | None,
    hits_by_chunk: dict[str, list[Hit]],
) -> None:
    add = out.add
    stats = result.stats.get("entities") or {}
    out.section("实体标注")
    if not stats.get("terms"):
        add(
            "本次没有实体字典（没有取到 `Data:CharacterIndex.json` / `Data:Soul.json`），没有标注。"
        )
        add("")
        return
    nicknames = stats.get("nicknames") or {}
    by_source = "、".join(f"{k} {v}" for k, v in (nicknames.get("by_type") or {}).items())
    same = nicknames.get("same_as_official") or []
    add(
        f"字典：正式写法 {stats['terms']} 个（式神正式名、御魂名、阴阳师名，含占位词）；"
        f"昵称 {nicknames.get('total', 0)} 个，全部来自 wiki 数据"
        f"（按指向的实体类型：{by_source or '无'}），没有字数门槛，也没有排除名单。"
        f"其中一个字的 {nicknames.get('single_char', 0)} 个、"
        f"字母或数字开头 / 结尾的 {nicknames.get('ascii_edge', 0)} 个、"
        f"指向不止一个实体的 {nicknames.get('multi_target', 0)} 个、"
        f"和某个正式名相同的 {len(same)} 个{'（' + '、'.join(same) + '）' if same else ''}。"
    )
    add("")
    enclosing = nicknames.get("enclosing_official") or []
    if enclosing:
        add(
            f"有 {len(enclosing)} 个昵称里面包着一个正式名（{'、'.join(enclosing)}）："
            "长的写法优先，文中出现时按昵称出候选，里面的那个正式名不标。"
        )
        add("")
    blocked = nicknames.get("blocked_by_official") or []
    if blocked:
        names = "、".join(blocked)
        add(f"自查：有 {len(blocked)} 个昵称单独出现时也出不了候选（{names}），需要看一下。")
        add("")
    add(
        "标签分两级：正式名命中是**确定**的（`certain: true`）；昵称命中一律保留，但只算**候选**"
        "（`certain: false`，`via` 写命中的昵称），是不是真的在指那个实体留给检索去判断。"
        "同一片里某个实体已经靠正式名确定了，就不再出它的候选。"
    )
    add("")
    add("| 切片 | 有确定标签 | 只有候选标签 | 两者都没有 | 合计 |")
    add("| --- | --- | --- | --- | --- |")
    rows = [
        (
            "全部",
            [stats["chunks_certain"], stats["chunks_candidate_only"], stats["chunks_none"]],
        )
    ]
    rows += [(KIND_LABELS.get(k, k), levels) for k, levels in (stats.get("by_type") or {}).items()]
    for label, levels in rows:
        count = sum(levels) or 1
        cells = " | ".join(f"{n}（{n * 100 / count:.1f}%）" for n in levels)
        add(f"| {label} | {cells} | {sum(levels)} |")
    add("")
    add(f"下面几张表只数确定的标签。出现过的实体 {stats['distinct_entities']} 个。")
    add("")
    counts: dict[str, dict[str, int]] = {}
    for name, kind, count in stats.get("chunk_counts") or []:
        counts.setdefault(name, {})[kind] = count
    add(
        "按实体类型（一个切片里提到一个实体算一次）："
        + "，".join(
            f"{kind} {count}" for kind, count in (stats.get("by_entity_type") or {}).items()
        )
    )
    add("")
    onmyoji = stats.get("onmyoji") or []
    source = (
        "数据（`Data:Skin.json` 里有、`Data:CharacterIndex.json` 里没有的角色）"
        if stats.get("onmyoji_source") == ONMYOJI_FROM_DATA
        else "配置里的短名单（`onmyoji_fallback`；数据里没找到）"
    )
    add(f"**阴阳师**（类型「{TYPE_ONMYOJI}」）共 {len(onmyoji)} 个，名单来自{source}：")
    add("")
    add("| 阴阳师 | 提到它的切片数 |")
    add("| --- | --- |")
    for name in onmyoji:
        add(f"| {name} | {counts.get(name, {}).get(TYPE_ONMYOJI, 0)} |")
    add("")
    shadows = stats.get("shadow_words") or []
    if shadows:
        add(
            "这些更长的词里的名字不标（整个词占住位置）："
            + "、".join(shadows)
            + "。阴阳师的简称（「博雅」「比丘尼」）现在数据里没有，所以没有候选；"
            "`Data:Skin.json` 里那个角色的条目加上 `nickname` 就会自动用上。"
        )
        add("")
    ambiguous = stats.get("ambiguous") or {}
    add(
        f"**类型改成中性的御魂名** {len(ambiguous)} 个。没有御魂语境时标中性类型；"
        "名字紧挨着（几个字以内）「御魂 / 件套 / 套装」、所在小节或页面名带「御魂」、"
        "或紧挨着另一个不会认错的御魂名时标「御魂」；同一片里两种情况都有时只留「御魂」："
    )
    add("")
    add("| 名字 | 中性类型 | 依据 | 标成中性类型的切片 | 标成「御魂」的切片 | 标成「式神」的切片 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for name, info in ambiguous.items():
        by_kind = counts.get(name, {})
        add(
            f"| {name} | {info['type']} | {info['basis']} | {by_kind.get(info['type'], 0)} "
            f"| {by_kind.get(TYPE_SOUL, 0)} | {by_kind.get('式神', 0)} |"
        )
    add("")
    add("出现最多的 20 个实体（按提到它的切片数）：")
    add("")
    add("| 实体 | 类型 | 切片数 |")
    add("| --- | --- | --- |")
    for name, kind, count in stats.get("top") or []:
        add(f"| {name} | {kind} | {count} |")
    add("")
    candidates = stats.get("candidates") or {}
    by_nickname = candidates.get("by_nickname") or []
    add(
        f"**候选（昵称命中）**：带候选的切片 {candidates.get('chunks', 0)} 个"
        f"（其中 {stats['chunks_candidate_only']} 个没有确定标签），"
        f"候选标签（实体 × 切片）{candidates.get('pairs', 0)} 个，"
        f"涉及 {candidates.get('distinct_entities', 0)} 个实体。"
        f"字典里的 {nicknames.get('total', 0)} 个昵称里有 {len(by_nickname)} 个出过候选。"
        "已经靠正式名确定的实体不出候选，下表不含那些。"
    )
    add("")
    limit = 40
    add(f"出候选最多的 {min(limit, len(by_nickname))} 个昵称：")
    add("")
    add("| 昵称 | 指向 | 出候选的切片数 |")
    add("| --- | --- | --- |")
    for nickname, names, count in by_nickname[:limit]:
        add(f"| {nickname} | {'、'.join(names)} | {count} |")
    add("")
    if len(by_nickname) > limit:
        add(
            "其余出过候选的昵称："
            + "、".join(f"{n}→{'/'.join(names)}（{c}）" for n, names, c in by_nickname[limit:])
        )
        add("")
    if lexicon is not None:
        hit = {str(nickname).lower() for nickname, _, _ in by_nickname}
        silent = [
            f"{lexicon.alias_spelling[a]}→{'/'.join(n for n, _ in lexicon.aliases[a])}"
            for a in lexicon.aliases
            if a not in hit
        ]
        add(f"一次候选都没出过的昵称（{len(silent)} 个）：{'、'.join(silent) or '（无）'}")
        add("")
        add(
            "昵称只有两条结构上的规则：一个字的昵称，前后都不是字、或者这一片就在它指向的实体"
            "自己的页面里才出候选；字母 / 数字开头或结尾的昵称，那一头不能紧挨着别的字母或数字。"
        )
        add("")
        add(
            "走严格规则的正式名（只在自己的页面里、前后都不是字、御魂名紧邻「御魂」二字、"
            "或同一篇文档别处已经确认过时才标；一个字的名字不走最后一条）："
            + "、".join(sorted(lexicon.strict))
        )
        add("")
    sample = _entity_sample(result, settings, set(ambiguous))
    add(
        f"抽 {len(sample)} 个有确定标签的切片核对"
        "（分层抽：带「阴阳师」的、带中性类型的、把中性名字判成「御魂」的，其余随机）："
    )
    add("")
    for index, chunk in enumerate(sample, 1):
        add(f"**实体抽样 {index}**　{chunk.header}")
        add("")
        add(f"标出的{entity_line(chunk)}")
        add("")
        body = chunk.body if len(chunk.body) <= 500 else chunk.body[:500] + "……（后略）"
        add(fenced(body))
        add("")

    picked = _candidate_sample(result, settings, hits_by_chunk)
    add(
        f"抽 {len(picked)} 个带候选的切片核对（随机抽，每个昵称最多一片，"
        "所以少见的昵称在这里的比重比实际大）。每行是：昵称 → 指向的实体，和昵称前后的原文："
    )
    add("")
    for index, (chunk, hits) in enumerate(picked, 1):
        add(f"**候选抽样 {index}**　{chunk.header}")
        add("")
        for hit in hits:
            add(f"- 「{hit.surface}」→ {hit.name}（{hit.type}）：`{_context(chunk.text, hit)}`")
        add("")


def _context(text: str, hit: Hit, width: int = 30) -> str:
    """昵称前后的原文（昵称用【】标出）。"""
    end = hit.start + len(hit.surface)
    before = text[max(0, hit.start - width) : hit.start]
    after = text[end : end + width]
    snippet = f"{before}【{text[hit.start : end]}】{after}"
    return snippet.replace("\n", "⏎").replace("`", "'")


def _candidate_sample(
    result: ProcessResult, settings: Settings, hits_by_chunk: dict[str, list[Hit]]
) -> list[tuple[Chunk, list[Hit]]]:
    """带候选的切片里随机抽几个，每个昵称最多一片。返回（切片，这一片里的候选命中，不重复）。"""
    rng = random.Random(settings.report.sample_seed + 2)
    pool = [c for c in result.chunks if any(not e["certain"] for e in c.entities)]
    rng.shuffle(pool)
    picked: list[tuple[Chunk, list[Hit]]] = []
    used: set[str] = set()
    for chunk in pool:
        if len(picked) >= settings.report.entity_sample_size:
            break
        hits: dict[tuple[str, str], Hit] = {}
        for hit in hits_by_chunk.get(chunk.chunk_id, []):
            if not hit.certain:
                hits.setdefault((hit.surface, hit.name), hit)
        if not hits or any(surface in used for surface, _ in hits):
            continue
        used.update(surface for surface, _ in hits)
        picked.append((chunk, list(hits.values())))
    return picked


def _entity_sample(result: ProcessResult, settings: Settings, ambiguous: set[str]) -> list[Chunk]:
    """实体抽样：新加的类型各占几个名额，免得随机抽到的全是只有式神名的切片。"""
    rng = random.Random(settings.report.sample_seed + 1)
    tagged = [c for c in result.chunks if any(e["certain"] for e in c.entities)]
    size = min(settings.report.entity_sample_size, len(tagged))

    def has(kinds: tuple[str, ...], names: set[str] | None = None) -> Callable[[Chunk], bool]:
        return lambda c: any(
            e["certain"] and e["type"] in kinds and (names is None or e["name"] in names)
            for e in c.entities
        )

    quotas = [
        (has((TYPE_ONMYOJI,)), 3),
        (has(NEUTRAL_TYPES), 3),
        (has((TYPE_SOUL,), ambiguous), 2),
    ]
    picked: dict[str, Chunk] = {}
    for wanted, quota in quotas:
        pool = [c for c in tagged if wanted(c) and c.chunk_id not in picked]
        for chunk in rng.sample(pool, min(quota, len(pool), max(0, size - len(picked)))):
            picked[chunk.chunk_id] = chunk
    rest = [c for c in tagged if c.chunk_id not in picked]
    for chunk in rng.sample(rest, min(len(rest), max(0, size - len(picked)))):
        picked[chunk.chunk_id] = chunk
    return list(picked.values())


def _dataset_section(out: ReportWriter, result: ProcessResult) -> None:
    add = out.add
    stats: dict[str, Any] = result.stats.get("datasets") or {}
    out.section("由 Data 数据集生成的文档（类型 `data`）")
    add("| 类别 | 数据集 | 文档数 | 切片数（去重后） | 细节 |")
    add("| --- | --- | --- | --- | --- |")
    for info in stats.values():
        details = "；".join(f"{k} {v}" for k, v in (info.get("details") or {}).items())
        add(
            f"| {info['label']} | {'、'.join(f'`{d}`' for d in info['datasets']) or '-'} "
            f"| {info['documents']} | {info['chunks_kept']} | {details} |"
        )
    add("")
    add(
        "细节里的字段：`in_kb` 已在库、没有再放；`no_information` 说明和名字相同、没有放；"
        "`duplicate` 名字和说明都相同的重复条目；`added` 补进来的；"
        "`bare_params` 补进来的条目里带着没填数值的裸参数（`d%`、`p1%`）；"
        "`skills_in_kb` / `skills_added` 超鬼王技能说明已在库 / 补进来的；"
        "`unknown_speakers` 查不到名字的说话人编号和句数。"
    )
    add("")
    unresolved = (result.stats.get("build") or {}).get("dataset_unresolved") or {}
    add(
        f"解析不了、原样保留的占位符共 {sum(unresolved.values())} 处："
        + ("，".join(f"`{k}` {v}" for k, v in unresolved.items()) or "（无）")
    )
    add("")
    for category, info in stats.items():
        sample = next(
            (
                c
                for c in result.chunks
                if c.kind == KIND_DATA and c.meta.get("category") == category
            ),
            None,
        )
        if sample is None:
            continue
        add(f"**{info['label']}** 的一个切片：")
        add("")
        add(chunk_block(sample))
        add("")
