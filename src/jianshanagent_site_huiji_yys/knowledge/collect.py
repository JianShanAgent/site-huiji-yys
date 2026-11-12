"""取数：阴阳师 wiki 在条目页以外还要取什么，哪些条目页不用渲染。

- 源码整页只有 ``{{剧情页面|剧情ID|章节ID}}`` 或 ``{{扫地工}}`` 的页面，内容完全来自 Data，
  不渲染，改由对应的 Data 文档入库（页面的标题、ID、链接挂到那一章剧情上）；
  对应的 Data 内容不存在时照常渲染，不丢内容；
- ``Data:微信/…``、``Data:大神/…`` 的月合集（文件名以 ``_`` 开头的索引和草稿跳过）；
- ``Data:Story/NN.json``；
- 配置里点名的数据集（皮肤、百鬼棋局、花合战对白、状态说明、超鬼王，见 datasets.py）
  和实体字典用的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.collect import CollectContext, RawCorpus
from jianshanagent.plugins.knowledge.models import KIND_ANNOUNCEMENT, KIND_STORY
from jianshanagent.plugins.knowledge.sources import (
    Fetched,
    ListingSpec,
    PageRef,
    single_template_call,
)
from jianshanagent_site_huiji.knowledge import HuijiPlugin

from jianshanagent_site_huiji_yys.knowledge.settings import YysConfig
from jianshanagent_site_huiji_yys.knowledge.story import story_chapters, story_id_of

logger = get_logger(__name__)

PLUGIN_ID = "huiji_yys"


@dataclass(slots=True)
class YysCorpus:
    """这个插件取齐的原始内容（放在 ``RawCorpus.extras["huiji_yys"]``）。"""

    #: (剧情ID, 章节ID) → 渲染这一章的页面（可能不止一个）
    story_pages: dict[tuple[str, str], list[PageRef]] = field(default_factory=dict)
    #: 整页只有帖子模板调用的页面（内容由 Data 帖子代替）
    post_pages: list[PageRef] = field(default_factory=list)
    #: 公告合集：(来源类别, 取数结果)
    posts: list[tuple[str, Fetched]] = field(default_factory=list)
    #: 关掉的公告来源里缓存过的合集：不入库，只给条目推断日期当依据
    reference_posts: list[tuple[str, Fetched]] = field(default_factory=list)
    #: 关掉的来源：来源类别 → 清单里的文档数
    disabled_sources: dict[str, int] = field(default_factory=dict)
    #: 按标题单独取的 Data 文档（实体字典、数据集）：标题 → 文档
    named_docs: dict[str, Any] = field(default_factory=dict)
    #: 上面这些文档的定位信息：标题 → PageRef
    named_refs: dict[str, PageRef] = field(default_factory=dict)
    #: 按前缀取的数据集文档（超鬼王各年份）
    dataset_docs: list[Fetched] = field(default_factory=list)
    stories: list[Fetched] = field(default_factory=list)
    #: 被跳过的 Data 索引 / 草稿文件
    skipped_data: list[PageRef] = field(default_factory=list)


def yys_corpus(corpus: RawCorpus) -> YysCorpus:
    """这个插件取的原始内容；还没取过就给一份空的。"""
    own = corpus.extras.get(PLUGIN_ID)
    if not isinstance(own, YysCorpus):
        own = corpus.extras[PLUGIN_ID] = YysCorpus()
    return own


def is_auxiliary_data(ref: PageRef) -> bool:
    """文件名以 ``_`` 开头的是索引（``_months.json``）或草稿（``_scratch_*.json``），不是正文。"""
    return ref.basename.startswith("_")


def listing_spec(cfg: YysConfig, huiji: HuijiPlugin) -> ListingSpec:
    """列页面清单时另外要列的 Data 文档：公告合集、剧情、按前缀取的数据集，和点名的那几份。"""
    prefixes = [
        *(p.prefix for p in cfg.sources.posts),
        cfg.sources.story_prefix,
        *cfg.datasets.data_prefixes,
    ]
    return ListingSpec(
        prefixes=dict.fromkeys(prefixes, huiji.data_namespace), titles=cfg.named_titles
    )


async def select_pages(ctx: CollectContext, cfg: YysConfig, huiji: HuijiPlugin) -> None:
    """认出「内容全在 Data 里」的页面，顺带把剧情和公告合集取下来（认的时候要核对它们）。"""
    src = cfg.sources
    source, listing = ctx.source, ctx.listing
    own = yys_corpus(ctx.corpus)
    data_doc = huiji.data_getter(source)

    # 1. 页面源码：只用来认出「内容全在 Data 里」的页面。全量时所有页面都要认（剧情要靠它挂标题）
    wikitext = await source.wikitext(listing.pages)
    story_candidates: dict[tuple[str, str], list[PageRef]] = {}
    post_candidates: list[PageRef] = []
    for ref in ctx.pages:
        call = single_template_call(wikitext.get(ref.page_id, ""))
        if call and call[0] == src.story_template and len(call[1]) >= 2:
            story_candidates.setdefault((call[1][0], call[1][1]), []).append(ref)
        elif call and call[0] in src.post_templates:
            post_candidates.append(ref)

    # 2. 剧情。指定了剧情页面标题时，把渲染它的那份 Data 文档也算进来
    story_refs = listing.data.get(src.story_prefix) or []
    own.skipped_data += [r for r in story_refs if is_auxiliary_data(r)]
    wanted_story_ids = {
        story_id
        for (story_id, _), refs in story_candidates.items()
        if ctx.only_titles is not None and any(r.title in ctx.only_titles for r in refs)
    }
    picked = [
        r
        for r in story_refs
        if not is_auxiliary_data(r) and (ctx.wanted(r) or story_id_of(r) in wanted_story_ids)
    ]
    own.stories = await source.fetch_many(picked, data_doc, label=src.story_prefix)

    # 剧情页面对应的章节在 Data 里确实有文字，才由 Data 代替；否则照常渲染，不丢内容
    available = story_chapters(own.stories)
    fetched_story_ids = {story_id_of(r.ref) for r in own.stories}
    known_story_ids = {story_id_of(r) for r in story_refs}
    for key, refs in story_candidates.items():
        # 只跑指定页面时，没取的剧情文档无从核对，按「由 Data 代替」处理
        unchecked = key[0] not in fetched_story_ids and key[0] in known_story_ids
        if key in available or unchecked:
            own.story_pages[key] = refs
            for ref in refs:
                ctx.replace(ref, f"story:{key[0]}:{key[1]}", kind=KIND_STORY)

    # 3. 公告合集
    post_data_ids: set[str] = set()
    for post_source in src.posts:
        refs = listing.data.get(post_source.prefix) or []
        post_data_ids |= {r.data_id for r in refs}
        if not post_source.enabled:
            # 关掉的来源：不新取数，缓存里有的留作推断日期的依据
            usable = [r for r in refs if not is_auxiliary_data(r)]
            own.disabled_sources[post_source.kind] = len(usable)
            for ref in usable:
                doc = huiji.cached_data_doc(source, ref)
                if doc is not None:
                    own.reference_posts.append((post_source.kind, Fetched(ref, value=doc)))
            continue
        own.skipped_data += [r for r in refs if is_auxiliary_data(r)]
        picked = [r for r in refs if not is_auxiliary_data(r) and ctx.wanted(r)]
        fetched = await source.fetch_many(picked, data_doc, label=post_source.prefix)
        own.posts += [(post_source.kind, item) for item in fetched]
    for ref in post_candidates:
        # 「扫地工/2026-09」对应 Data:大神/扫地工/2026-09.json；那份数据不存在就照常渲染
        backed = any(
            f"Data:{p.prefix}{ref.title.replace(' ', '_')}.json" in post_data_ids for p in src.posts
        )
        if backed:
            own.post_pages.append(ref)
            ctx.replace(ref, "posts", kind=KIND_ANNOUNCEMENT)


async def collect_data(ctx: CollectContext, cfg: YysConfig, huiji: HuijiPlugin) -> None:
    """实体字典和数据集（Data:CharacterIndex.json、Data:Soul.json、Data:Skin.json……）。"""
    source, listing = ctx.source, ctx.listing
    own = yys_corpus(ctx.corpus)
    data_doc = huiji.data_getter(source)
    named = [listing.named[t] for t in cfg.named_titles if t in listing.named]
    for item in await source.fetch_many(named, data_doc, label="实体字典和数据集"):
        if item.error:
            logger.warning("kb_named_doc_failed", title=item.ref.title, error=item.error)
        else:
            own.named_docs[item.ref.title] = item.value
            own.named_refs[item.ref.title] = item.ref
    for prefix in cfg.datasets.data_prefixes:
        refs = listing.data.get(prefix) or []
        own.skipped_data += [r for r in refs if is_auxiliary_data(r)]
        picked = [r for r in refs if not is_auxiliary_data(r)]
        own.dataset_docs += await source.fetch_many(picked, data_doc, label=prefix)


def summary(corpus: RawCorpus) -> dict[str, Any]:
    own = yys_corpus(corpus)
    return {
        "story_pages": sum(len(v) for v in own.story_pages.values()),
        "post_pages": len(own.post_pages),
        "post_docs": len(own.posts),
        "reference_post_docs": len(own.reference_posts),
        "disabled_sources": dict(own.disabled_sources),
        "named_docs": sorted(own.named_docs),
        "dataset_docs": sorted(f.ref.data_id for f in own.dataset_docs if not f.error),
        "story_docs": len(own.stories),
        "skipped_data": len(own.skipped_data),
    }
