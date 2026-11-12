"""阴阳师 wiki 站点插件里管知识库的那一摊。排在 ``huiji`` 后面（要用它取 Data 文档）。

核心只会把条目页渲染、清洗、切片。这个站另有这些，都在这个包里：

- 公告（微信公众号、网易大神的帖子，wiki 存在 ``Data:微信/``、``Data:大神/``）→ posts.py
- 剧情（``Data:Story/``；渲染它的页面不再渲染，标题和链接挂到剧情上）→ story.py、collect.py
- 由数据集生成的文档（皮肤、百鬼棋局、花合战对白、状态说明、超鬼王）→ datasets.py
- 实体标注（式神、御魂、阴阳师；字典来自 wiki 数据）→ entities.py
- 图库（立绘、卡面、头像、御魂图标、绘卷配图，按文件名规律收）→ images.py
- 自动更新时 Data 文档和文件的改动意味着什么 → changes.py
- 质量报告里自己的几节 → report.py

这一摊的配置在 ``config/site.huiji_yys`` 的 ``knowledge`` 段。
这个站的清洗规则（要去掉哪些 class、哪些 class 是小标题……）不是代码，是配置：
写在 ``config/site.huiji_yys`` 的 ``extend`` 段里，读配置时就并进 ``plugin.knowledge`` 了。
"""

from __future__ import annotations

from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.plugins.knowledge.changes import ChangeContext
from jianshanagent.plugins.knowledge.collect import CollectContext, RawCorpus
from jianshanagent.plugins.knowledge.image_collect import Catalog
from jianshanagent.plugins.knowledge.pipeline import BuildContext, ProcessContext, ProcessResult
from jianshanagent.plugins.knowledge.report import ReportWriter
from jianshanagent.plugins.knowledge.settings import Settings
from jianshanagent.plugins.knowledge.site import Classifier, SitePlugin, SiteSetup
from jianshanagent.plugins.knowledge.sources import ListingSpec, WikiSource
from jianshanagent_site_huiji.knowledge import HuijiPlugin

from jianshanagent_site_huiji_yys.knowledge import build, changes, collect, images, report
from jianshanagent_site_huiji_yys.knowledge.collect import PLUGIN_ID
from jianshanagent_site_huiji_yys.knowledge.settings import YysConfig, YysEnv, load_yys_config

logger = get_logger(__name__)

#: 这些段原来写在 plugin.knowledge 里，现在归这个插件（site.huiji_yys 的 knowledge 段）。
#: 还留在老地方的不起作用，加载时提醒一声：（老地方的路径，现在的路径）
_MOVED = (
    (("entities",), "entities"),
    (("datasets",), "datasets"),
    (("sources", "posts"), "posts"),
    (("sources", "story"), "story"),
    (("sources", "wechat_tail"), "wechat_tail"),
    (("sources", "keep_title_only_posts"), "keep_title_only_posts"),
    (("sources", "pages", "story_template"), "pages.story_template"),
    (("sources", "pages", "post_templates"), "pages.post_templates"),
    (("sync", "page_data"), "page_data"),
    (("images", "art_prefix"), "images.art_prefix"),
    (("images", "card_prefix"), "images.card_prefix"),
    (("images", "avatar_prefix"), "images.avatar_prefix"),
    (("images", "scroll_prefix"), "images.scroll_prefix"),
    (("images", "background_prefix"), "images.background_prefix"),
    (("images", "souls"), "images.souls"),
    (("images", "awakened_way"), "images.awakened_way"),
    (("images", "soul_page"), "images.soul_page"),
    (("images", "character_doc"), "images.character_doc"),
)


def moved_keys(knowledge: dict[str, Any]) -> list[tuple[str, str]]:
    """``plugin.knowledge`` 里还留着哪些已经归这个插件的项：（老路径，新路径）。"""
    found = []
    for path, target in _MOVED:
        node: Any = knowledge
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if node is not None:
            found.append((".".join(path), target))
    return found


class YysPlugin(SitePlugin):
    id = PLUGIN_ID

    def __init__(self, huiji: HuijiPlugin, cfg: YysConfig | None = None):
        self.huiji = huiji
        self.cfg = cfg or YysConfig()

    def env(self, settings: Settings) -> YysEnv:
        """插件里的函数要用到的设置：核心的几项加上自己的。"""
        return YysEnv(wiki=settings.wiki, clean=settings.clean, chunk=settings.chunk, cfg=self.cfg)

    # ── 取数 ──────────────────────────────────────────────────────────

    def listing(self) -> ListingSpec:
        return collect.listing_spec(self.cfg, self.huiji)

    async def select_pages(self, ctx: CollectContext) -> None:
        await collect.select_pages(ctx, self.cfg, self.huiji)

    async def collect(self, ctx: CollectContext) -> None:
        await collect.collect_data(ctx, self.cfg, self.huiji)

    def summary(self, corpus: RawCorpus) -> dict[str, Any]:
        return collect.summary(corpus)

    # ── 建文档、切片 ──────────────────────────────────────────────────

    def build_documents(self, ctx: BuildContext) -> None:
        build.build_documents(ctx, self.env(ctx.settings))

    def annotate(self, ctx: ProcessContext) -> None:
        build.annotate(ctx, self.env(ctx.settings))

    def stats(self, ctx: ProcessContext) -> dict[str, Any]:
        return build.stats(ctx, self.env(ctx.settings))

    # ── 报告 ──────────────────────────────────────────────────────────

    def status_labels(self) -> dict[str, str]:
        return dict(report.STATUS_LABELS)

    def report(
        self, slot: str, out: ReportWriter, result: ProcessResult, settings: Settings
    ) -> None:
        outcome = result.extras.get(PLUGIN_ID)
        if not isinstance(outcome, build.YysResult):
            outcome = build.YysResult()
        report.write(slot, out, result, settings, lexicon=outcome.lexicon, hits=outcome.hits)

    # ── 自动更新 ──────────────────────────────────────────────────────

    def change_classifier(self, ctx: ChangeContext) -> Classifier:
        return changes.classifier(self.cfg, self.huiji, ctx)

    # ── 图库 ──────────────────────────────────────────────────────────

    def has_images(self) -> bool:
        return self.cfg.images.enabled

    async def image_catalog(self, source: WikiSource, settings: Settings) -> Catalog | None:
        if not self.cfg.images.enabled:
            return None
        return await images.image_catalog(source, self.env(settings), self.huiji)


def create(setup: SiteSetup) -> YysPlugin:
    huiji = setup.parent(HuijiPlugin)  # 要用灰机的 Data 接口
    for old, new in moved_keys(setup.config.plugin("knowledge")):
        logger.warning(
            "kb_config_moved",
            old=f"plugin.knowledge 的 {old}",
            new=f"site.huiji_yys 的 knowledge.{new}",
            hint="老地方写的不起作用了，挪到新地方（管理后台改过的在配置覆写目录里）",
        )
    return YysPlugin(huiji, load_yys_config(setup.raw.get("knowledge") or {}))
