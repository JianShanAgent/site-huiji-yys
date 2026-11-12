"""阴阳师插件知识库这一摊的设置：读 ``config/site.huiji_yys`` 的 ``knowledge`` 段。

那份配置里的 ``extend`` 一段（这个站的清洗规则、排除的页面、日期的门槛……）是给通用配置的补充，
读配置时就并进去了（见 core/config.py），这里不读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jianshanagent.plugins.knowledge.settings import ChunkSettings, CleanSettings, WikiSettings


def _strs(value: Any) -> list[str]:
    return [str(v) for v in value or [] if str(v)]


@dataclass(frozen=True, slots=True)
class PostSource:
    prefix: str
    kind: str  # wechat / dashen
    #: 关掉的来源不入库、不新取数；缓存里已有的帖子标题和日期仍可用来推断条目的日期
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SourceSettings:
    """公告和剧情：哪些页面的内容全在 Data 里、公告合集在哪、剧情在哪。"""

    story_template: str = "剧情页面"
    post_templates: tuple[str, ...] = ("扫地工",)
    posts: tuple[PostSource, ...] = (PostSource("微信/", "wechat"), PostSource("大神/", "dashen"))
    keep_title_only_posts: bool = True
    #: 微信文章末尾的落款行之后，最多有这么多行才按「署名 + 推荐阅读」清理
    wechat_tail_max_lines: int = 8
    wechat_tail_window: int = 10
    wechat_tail_line_chars: int = 80
    story_prefix: str = "Story/"


@dataclass(frozen=True, slots=True)
class EntitySettings:
    """切片的实体标注（字典匹配，规则见 entities.py 顶部）。"""

    enabled: bool = True
    character_index: str = "Data:CharacterIndex.json"
    soul: str = "Data:Soul.json"
    strict_names: frozenset[str] = frozenset()
    context_words: tuple[str, ...] = ("御魂",)
    context_window: int = 12
    ambiguous_window: int = 4
    #: 皮肤数据：里面有、式神索引里没有的角色就是阴阳师
    skin: str = "Data:Skin.json"
    #: 数据里找不到阴阳师名单时才用的短名单
    onmyoji_fallback: tuple[str, ...] = ()
    #: 包含实体名、但说的不是它的更长的词（「黑晴明」里的「晴明」）：整个词占住位置，不标
    shadow_words: frozenset[str] = frozenset()
    #: 御魂名同时是道具名的：没有御魂语境时标这个中性类型
    item_souls: frozenset[str] = frozenset()
    #: 数据认不出来、但确实和妖怪同名的御魂（补充名单）
    monster_souls: frozenset[str] = frozenset()
    #: 小节标题或页面名里带这些字样的，整片都按御魂语境算
    soul_section_words: tuple[str, ...] = ("御魂",)

    @property
    def data_titles(self) -> tuple[str, ...]:
        titles = (self.character_index, self.soul, self.skin)
        return tuple(t for t in titles if t) if self.enabled else ()


@dataclass(frozen=True, slots=True)
class DatasetSettings:
    """由 Data 数据集生成的文档（规则见 datasets.py 顶部）。标题留空就不生成那一类。"""

    enabled: bool = True
    character_index: str = "Data:CharacterIndex.json"
    skin: str = "Data:Skin.json"
    #: 阴阳师（晴明、神乐……）没有自己的条目页时，皮肤文档的链接指向这个页面
    onmyoji_page: str = "阴阳师"
    zzq_character: str = "Data:Zzq/character.json"
    zzq_fuzhou: str = "Data:Zzq/fuzhou.json"
    zzq_soul: str = "Data:Zzq/soul.json"
    zzq_page: str = "百鬼棋局"
    month_illustration: str = "Data:Monthillustration.json"
    #: 花合战月份页的标题，``{yyyymm}`` 换成年月
    month_page: str = "花合战/{yyyymm}"
    #: 对白里查不到名字的说话人编号 → 名字（从对白正文能确认的才写）
    speakers: dict[str, str] = field(default_factory=dict)
    skill_buffs: str = "Data:SkillBuffs.json"
    skill_tips: str = "Data:SkillTips.json"
    buffs_page: str = "技能检索"
    super_ghost_prefix: str = "SuperGhost/"
    super_ghost_page: str = "超鬼王"
    #: 比对「在不在库里」时，一句话至少这么多个字才拿去比
    match_min_chars: int = 8

    @property
    def data_titles(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        titles = (
            self.character_index,
            self.skin,
            self.zzq_character,
            self.zzq_fuzhou,
            self.zzq_soul,
            self.month_illustration,
            self.skill_buffs,
            self.skill_tips,
        )
        return tuple(t for t in titles if t)

    @property
    def data_prefixes(self) -> tuple[str, ...]:
        return (self.super_ghost_prefix,) if self.enabled and self.super_ghost_prefix else ()


@dataclass(frozen=True, slots=True)
class CatalogSettings:
    """图库收哪些图（规则见 images.py 顶部）。"""

    #: 管不管图库
    enabled: bool = True
    #: wiki 上各类图的文件名前缀；写成空字符串就不收那一类
    art_prefix: str = "Skin_"
    card_prefix: str = "S_"
    avatar_prefix: str = "Tx_"
    scroll_prefix: str = "Hj_"
    background_prefix: str = "Chabg_"
    #: 收不收御魂的图（文件名就是御魂名）
    souls: bool = True
    #: 皮肤数据里「获取方式」是这个值的，是觉醒后的造型
    awakened_way: str = "觉醒后获得"
    #: 御魂没有自己的条目页，链接指向这个页面
    soul_page: str = "御魂检索器"
    #: 式神的详细数据（绘卷各章的标题在里面），``{id}`` 换成式神编号
    character_doc: str = "Data:Character/{id}.json"

    @property
    def prefixes(self) -> tuple[str, ...]:
        """按前缀收的那几类图的前缀（不重复，关掉的不算）。"""
        prefixes = (
            self.art_prefix,
            self.card_prefix,
            self.avatar_prefix,
            self.scroll_prefix,
            self.background_prefix,
        )
        return tuple(dict.fromkeys(p for p in prefixes if p))

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> CatalogSettings:
        defaults = cls()

        def text(key: str, default: str) -> str:
            # 写成空字符串表示关掉这一类；没写才用默认值
            return str(raw[key] or "") if key in raw else default

        return cls(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            art_prefix=text("art_prefix", defaults.art_prefix),
            card_prefix=text("card_prefix", defaults.card_prefix),
            avatar_prefix=text("avatar_prefix", defaults.avatar_prefix),
            scroll_prefix=text("scroll_prefix", defaults.scroll_prefix),
            background_prefix=text("background_prefix", defaults.background_prefix),
            souls=bool(raw.get("souls", defaults.souls)),
            awakened_way=text("awakened_way", defaults.awakened_way),
            soul_page=text("soul_page", defaults.soul_page),
            character_doc=text("character_doc", defaults.character_doc),
        )


@dataclass(frozen=True, slots=True)
class YysConfig:
    """插件自己的全部设置。"""

    sources: SourceSettings = field(default_factory=SourceSettings)
    entities: EntitySettings = field(default_factory=EntitySettings)
    datasets: DatasetSettings = field(default_factory=DatasetSettings)
    images: CatalogSettings = field(default_factory=CatalogSettings)
    #: 条目页的内容来自哪些 Data 文档：标题里的 ``{id}`` 是式神编号。这样的文档改了，
    #: 那个式神的条目页自己没有新修订，wiki 上的渲染结果也要过一天才变，所以直接按源码重新渲染
    page_data: tuple[str, ...] = ("Data:Character/{id}.json", "Data:Skilldiff/{id}.json")

    @property
    def named_titles(self) -> tuple[str, ...]:
        """要按标题单独取的 Data 文档（实体字典 + 数据集），不重复。"""
        return tuple(dict.fromkeys([*self.entities.data_titles, *self.datasets.data_titles]))


@dataclass(frozen=True, slots=True)
class YysEnv:
    """插件里的函数要用到的设置：核心的几项，加上插件自己的。"""

    wiki: WikiSettings
    clean: CleanSettings = field(default_factory=CleanSettings)
    chunk: ChunkSettings = field(default_factory=ChunkSettings)
    cfg: YysConfig = field(default_factory=YysConfig)

    @property
    def sources(self) -> SourceSettings:
        return self.cfg.sources

    @property
    def entities(self) -> EntitySettings:
        return self.cfg.entities

    @property
    def datasets(self) -> DatasetSettings:
        return self.cfg.datasets

    @property
    def images(self) -> CatalogSettings:
        return self.cfg.images


def load_yys_config(raw: dict[str, Any]) -> YysConfig:
    pages = raw.get("pages") or {}
    story = raw.get("story") or {}
    tail = raw.get("wechat_tail") or {}
    defaults = SourceSettings()
    sources = SourceSettings(
        story_template=str(pages.get("story_template") or defaults.story_template),
        post_templates=tuple(_strs(pages.get("post_templates"))) or defaults.post_templates,
        posts=tuple(
            PostSource(str(p["prefix"]), str(p["kind"]), bool(p.get("enabled", True)))
            for p in raw.get("posts") or []
            if p
        )
        or defaults.posts,
        keep_title_only_posts=bool(raw.get("keep_title_only_posts", True)),
        wechat_tail_max_lines=int(tail.get("max_lines") or defaults.wechat_tail_max_lines),
        wechat_tail_window=int(tail.get("window") or defaults.wechat_tail_window),
        wechat_tail_line_chars=int(tail.get("line_chars") or defaults.wechat_tail_line_chars),
        story_prefix=str(story.get("prefix") or defaults.story_prefix),
    )

    e = raw.get("entities") or {}
    entity_defaults = EntitySettings()
    entities = EntitySettings(
        enabled=bool(e.get("enabled", True)),
        character_index=str(e.get("character_index") or entity_defaults.character_index),
        soul=str(e.get("soul") or entity_defaults.soul),
        strict_names=frozenset(_strs(e.get("strict_names"))),
        context_words=tuple(_strs(e.get("context_words"))) or entity_defaults.context_words,
        context_window=int(e.get("context_window") or entity_defaults.context_window),
        ambiguous_window=int(e.get("ambiguous_window") or entity_defaults.ambiguous_window),
        skin=str(e.get("skin") or entity_defaults.skin),
        onmyoji_fallback=tuple(_strs(e.get("onmyoji_fallback"))),
        shadow_words=frozenset(_strs(e.get("shadow_words"))),
        item_souls=frozenset(_strs(e.get("item_souls"))),
        monster_souls=frozenset(_strs(e.get("monster_souls"))),
        soul_section_words=tuple(_strs(e.get("soul_section_words")))
        or entity_defaults.soul_section_words,
    )

    ds = raw.get("datasets") or {}
    zzq = ds.get("zzq") or {}
    month = ds.get("month_illustration") or {}
    buffs = ds.get("buffs") or {}
    ghost = ds.get("super_ghost") or {}
    dd = DatasetSettings()

    def title(section: dict[str, Any], key: str, default: str) -> str:
        # 写成空字符串表示关掉这一类；没写才用默认值
        return str(section[key] or "") if key in section else default

    datasets = DatasetSettings(
        enabled=bool(ds.get("enabled", True)),
        character_index=title(ds, "character_index", dd.character_index),
        skin=title(ds, "skin", dd.skin),
        onmyoji_page=title(ds, "onmyoji_page", dd.onmyoji_page),
        zzq_character=title(zzq, "character", dd.zzq_character),
        zzq_fuzhou=title(zzq, "fuzhou", dd.zzq_fuzhou),
        zzq_soul=title(zzq, "soul", dd.zzq_soul),
        zzq_page=title(zzq, "page", dd.zzq_page),
        month_illustration=title(month, "data", dd.month_illustration),
        month_page=title(month, "page", dd.month_page),
        speakers={str(k): str(v) for k, v in (month.get("speakers") or {}).items()},
        skill_buffs=title(buffs, "data", dd.skill_buffs),
        skill_tips=title(buffs, "tips", dd.skill_tips),
        buffs_page=title(buffs, "page", dd.buffs_page),
        super_ghost_prefix=title(ghost, "prefix", dd.super_ghost_prefix),
        super_ghost_page=title(ghost, "page", dd.super_ghost_page),
        match_min_chars=int(ds.get("match_min_chars") or dd.match_min_chars),
    )
    defaults_cfg = YysConfig()
    return YysConfig(
        sources=sources,
        entities=entities,
        datasets=datasets,
        images=CatalogSettings.from_config(raw.get("images") or {}),
        page_data=tuple(_strs(raw["page_data"])) if "page_data" in raw else defaults_cfg.page_data,
    )
