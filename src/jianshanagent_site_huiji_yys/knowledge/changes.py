"""自动更新：阴阳师 wiki 上 Data 文档和文件的改动意味着要做什么。

2026-11-11 对着这个站一周的最近更改定的（核心怎么用这些判断见 changes.py）：

- Data 文档：

  - 管线自己要取的（公告合集、剧情、数据集）：重新列清单，有新修订的会被取下来。
  - 某个式神自己的数据（配置的 ``page_data``，如 ``Data:Character/365.json``）：
    那个式神的条目页没有新修订，``touched`` 也多半不变，点名让它按源码现渲染。
  - 其余的算公共数据集（式神索引、皮肤、状态说明……）：说不清哪些页面用了它，记下改动的时间，
    由自动更新过后把渲染的页面全部重取一遍。上面那些管线自己要取的数据集也算在里面
    （``Data:SuperGhost/2026.json`` 既单独入库，「超鬼王」页也是拿它渲染的）。
  - 关掉的公告来源（微信）不管。

- 文件：名字符合图库收图规律的（``Skin_``、``Hj_`` ……）传了、删了：对一次图库。
  图库的名字和说明来自的那几份数据（式神索引、皮肤、御魂、式神的详细数据）改了也对一次。
  御魂的图是按御魂名起名的，没有固定的前缀，从文件名看不出来：
  靠自动更新每天至少对一次图库（``sync.images_every_hours``）发现。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jianshanagent.plugins.knowledge.changes import Change, ChangeContext, Plan
from jianshanagent.plugins.knowledge.fetcher import DiskCache
from jianshanagent.plugins.knowledge.site import Classifier, Verdict
from jianshanagent_site_huiji.knowledge import DATA_KIND, LABEL_DATA, HuijiPlugin

from jianshanagent_site_huiji_yys.knowledge.settings import YysConfig

LABEL_DISABLED_POSTS = "关掉的公告来源"
LABEL_IMAGE_FILES = "图库的文件"


def character_pages(index_doc: Mapping[str, Any] | None) -> dict[str, str]:
    """式神索引（``Data:CharacterIndex.json``）→ 式神编号对应的条目页标题。"""
    pages: dict[str, str] = {}
    for entry in (index_doc or {}).get("characters") or []:
        if isinstance(entry, Mapping) and entry.get("id") is not None:
            title = str(entry.get("pageTitle") or entry.get("name") or "")
            if title:
                pages[str(entry["id"])] = title
    return pages


def cached_index(cache: DiskCache | None, cfg: YysConfig) -> dict[str, Any] | None:
    """缓存里的式神索引（上次取到的那一版）。不发请求。"""
    if cache is None:
        return None
    listing = cache.get_named("listing.json") or {}
    ref = (listing.get("named") or {}).get(cfg.datasets.character_index) or {}
    if not ref.get("page_id"):
        return None
    doc = cache.latest(DATA_KIND, int(ref["page_id"]))
    return doc if isinstance(doc, dict) else None


def _data_name(title: str) -> str:
    """配置里写的 Data 文档标题（``Data:Skin.json``）→ 不带命名空间、空格换成下划线的名字。"""
    return title.partition(":")[2].replace(" ", "_") if ":" in title else title.replace(" ", "_")


def _id_patterns(templates: Iterable[str]) -> list[re.Pattern[str]]:
    """``Data:Character/{id}.json`` 这样的写法 → 能从文档名里取出编号的正则。"""
    patterns = []
    for template in templates:
        name = _data_name(template)
        if "{id}" in name:
            head, _, tail = name.partition("{id}")
            patterns.append(re.compile(re.escape(head) + r"(\d+)" + re.escape(tail)))
    return patterns


def _matched_id(patterns: Sequence[re.Pattern[str]], name: str) -> str | None:
    for pattern in patterns:
        match = pattern.fullmatch(name)
        if match is not None:
            return match.group(1)
    return None


def classifier(
    cfg: YysConfig,
    huiji: HuijiPlugin,
    ctx: ChangeContext,
    *,
    pages_by_character: Mapping[str, str] | None = None,
) -> Classifier:
    """``pages_by_character``：式神编号 → 条目页标题；不给就用缓存里的式神索引算（不发请求）。"""
    src = cfg.sources
    if pages_by_character is None:
        pages_by_character = character_pages(cached_index(ctx.cache, cfg))
    data_namespace = huiji.data_namespace
    file_namespace = ctx.settings.sync.file_namespace

    enabled_posts = tuple(p.prefix for p in src.posts if p.enabled)
    disabled_posts = tuple(p.prefix for p in src.posts if not p.enabled)
    # 管线自己会按清单取的 Data 文档：有新修订要重跑管线
    fetched_prefixes = (*enabled_posts, src.story_prefix, *cfg.datasets.data_prefixes)
    fetched_names = {_data_name(title) for title in cfg.named_titles}
    # 这些不算公共数据集：页面不是拿它们渲染的（公告、剧情的页面由 Data 直接入库，不渲染）
    not_shared = tuple(prefix for prefix in (*enabled_posts, src.story_prefix) if prefix)
    page_patterns = _id_patterns(cfg.page_data)

    image_prefixes: tuple[str, ...] = ()
    image_docs: set[str] = set()
    image_patterns: list[re.Pattern[str]] = []
    if ctx.images_on and cfg.images.enabled:
        images = cfg.images
        image_prefixes = images.prefixes
        image_docs = {
            _data_name(title)
            for title in (
                cfg.datasets.character_index,
                cfg.datasets.skin,
                cfg.entities.soul if images.souls else "",
            )
            if title
        }
        image_patterns = _id_patterns([images.character_doc])

    def classify(change: Change, plan: Plan) -> Verdict | None:
        name = change.name
        if change.namespace == data_namespace:
            if any(name.startswith(prefix) for prefix in disabled_posts if prefix):
                return Verdict(LABEL_DISABLED_POSTS, relevant=False)
            if name in fetched_names or any(name.startswith(p) for p in fetched_prefixes if p):
                plan.kb = True
            if name in image_docs or _matched_id(image_patterns, name) is not None:
                plan.images = True
            character = _matched_id(page_patterns, name)
            if character is not None:
                page = pages_by_character.get(character)
                if page:  # 索引里还没有的编号：新式神，它的条目页建出来时自然会被取到
                    plan.fresh_titles.add(page)
            elif not any(name.startswith(prefix) for prefix in not_shared):
                plan.shared_data_at = max(plan.shared_data_at, change.timestamp)
            return Verdict(LABEL_DATA)
        if change.namespace == file_namespace:
            if not any(name.startswith(prefix) for prefix in image_prefixes):
                return None
            plan.images = True
            return Verdict(LABEL_IMAGE_FILES)
        return None

    return classify
