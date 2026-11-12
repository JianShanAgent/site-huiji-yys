"""阴阳师 wiki 的图库：选哪些图、每张图的记录怎么写。

收哪些图——都是 wiki 上按固定规律命名的文件，文件名里的编号（或名字）在数据里查得到是谁的才收：

- 立绘大图 ``Skin_<角色编号>_<皮肤编号>.png``、卡面小图 ``S_<角色编号>_<皮肤编号>.png``
  （条目页立绘页签上的小图，是立绘的半身特写）、头像 ``Tx_<角色编号>_s.png``
  （另有少量带皮肤编号的 ``Tx_<角色编号>_<皮肤编号>.png``）。
  角色编号查 ``Data:CharacterIndex.json``（式神的名字、稀有度、条目页）；那里没有、
  ``Data:Skin.json`` 里有的是阴阳师（晴明、神乐……）。皮肤编号查 ``Data:Skin.json`` 里这个角色的
  ``skins``（名字、获取方式、类型、日期）：01 号是初始造型，获取方式是「觉醒后获得」的是觉醒造型，
  这两种算「式神立绘」，其余算「皮肤立绘」。
  wiki 常常先传图、后补数据：图在、皮肤数据里还没有这个编号的照收，说明里写明名字待补，
  数据补上以后重跑一次就有名字了（向量不用重算）。
- 御魂 ``<御魂名>.png``（图鉴大图）和 ``<御魂名>_s.png``（小图标），名字和说明来自
  ``Data:Soul.json``。
- 绘卷配图 ``Hj_<式神编号>_<第几章>.png``，各章标题在 ``Data:Character/<式神编号>.json`` 的
  ``huijuan`` 里。
- 条目背景图 ``Chabg_<式神编号>.png``（式神条目页的背景插画）。

不收的：编号在数据里查不到是谁的文件；内容和已经收了的图完全一样的文件
（wiki 上有些皮肤的立绘、卡面是拿别的造型的图占位的，只收先遇到的那一份——编号最小的，
说明里注明这个角色还有哪个造型用的也是它）。

``subject`` 相同的图画的是同一个造型，以图搜图的结果里只留最像的一张：
一个皮肤的立绘大图、卡面小图和它的头像；
同一个角色的两个同名皮肤；不分造型的头像算在初始造型里（实测 278 张里 265 张最像的就是初始卡面）；
一个御魂的大图和小图标。

可以反复跑：数据文档按修订号缓存；离线时用上次联网存下的文件清单。
列文件、下载原图这些和站点无关的部分在核心的 image_collect.py。
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.platforms.huiji.knowledge import HuijiKnowledge
from jianshanagent.plugins.knowledge.fetcher import FetchError
from jianshanagent.plugins.knowledge.image_collect import (
    Catalog,
    WikiFile,
    list_files,
    lookup_files,
)
from jianshanagent.plugins.knowledge.image_store import KbImage
from jianshanagent.plugins.knowledge.markdown import normalize_text
from jianshanagent.plugins.knowledge.sources import PageRef, WikiSource

from jianshanagent_site_huiji_yys.knowledge.datasets import Roster
from jianshanagent_site_huiji_yys.knowledge.settings import CatalogSettings, YysEnv

logger = get_logger(__name__)

#: 上次联网时取到的文件清单和数据文档定位（离线重跑用），在缓存目录下
SNAPSHOT_FILE = "images/listing.json"

KIND_SKIN = "皮肤立绘"
KIND_SKIN_AVATAR = "皮肤头像"
KIND_SOUL = "御魂图标"
KIND_SCROLL = "绘卷配图"
KIND_BACKGROUND = "条目背景图"
FORM_ART = "立绘"
FORM_CARD = "卡面小图"
FORM_AVATAR = "头像"

_EXTENSIONS = r"(?:png|jpe?g|webp|gif)"
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
#: 说明里引用绘卷正文开头的字数
_EXCERPT_CHARS = 100


@dataclass(slots=True)
class CatalogData:
    """生成记录要用的 wiki 数据。"""

    character_index: dict[str, Any] | None = None
    skin: dict[str, Any] | None = None
    soul: dict[str, Any] | None = None
    #: 式神编号 → ``Data:Character/<编号>.json``（只取了有绘卷配图的）
    characters: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 站里已有的页面标题；None 表示不核对页面在不在
    known_titles: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class _Owner:
    """图上画的是谁。"""

    key: str
    name: str
    role: str  # 式神 / 阴阳师
    rarity: str
    page_title: str

    @property
    def who(self) -> str:
        return f"{self.role}「{self.name}」" + (f"（{self.rarity}）" if self.rarity else "")


def _text(value: Any) -> str:
    return normalize_text(str(value if value is not None else "")).strip()


def _plain(value: Any, joiner: str = "；") -> str:
    """数据里带 ``<br>`` 的说明 → 一行文字。"""
    lines = _TAG_RE.sub("", _BR_RE.sub("\n", str(value or ""))).split("\n")
    return joiner.join(part for line in lines if (part := _text(line)))


class _Builder:
    """把一类类文件变成记录。"""

    def __init__(
        self,
        files: dict[str, WikiFile],
        data: CatalogData,
        settings: YysEnv,
    ):
        self.files = files
        self.data = data
        self.settings = settings
        self.cfg: CatalogSettings = settings.images
        self.roster = Roster.build(data.character_index, data.skin)
        self.rarity = {
            str(c.get("id")): _text(c.get("rarity"))
            for c in (data.character_index or {}).get("characters") or []
            if isinstance(c, dict)
        }
        self.images: list[KbImage] = []
        #: 内容哈希 → 已经收了的那张在 ``images`` 里的位置
        self.by_sha1: dict[str, int] = {}
        self.unknown_owner: list[str] = []
        self.duplicates: list[str] = []
        self.unnamed_skins: list[str] = []

    # ── 通用 ──────────────────────────────────────────────────────────

    def numbered(self, prefix: str, tail: str) -> Iterator[tuple[WikiFile, re.Match[str]]]:
        """文件名是 ``<前缀><角色编号><tail>.<扩展名>`` 的文件，按名字的顺序一个个给。

        内容和已经收了的某张图完全一样的跳过（见 :meth:`same_content`）。
        """
        if not prefix:
            return
        pattern = re.compile(rf"{re.escape(prefix)}(\d+){tail}\.{_EXTENSIONS}", re.IGNORECASE)
        for name, file in sorted(self.files.items()):
            match = pattern.fullmatch(name)
            if match is None:
                continue
            if file.sha1 in self.by_sha1:
                self.same_content(file, match)
            else:
                yield file, match

    def same_content(self, file: WikiFile, match: re.Match[str] | None = None) -> None:
        """``file`` 和已经收了的一张图内容完全一样：不重复收。

        两张图是同一个角色的两个造型时，多半是 wiki 拿前一张占的位，
        在前一张的说明里注明这个造型用的也是它。
        """
        self.duplicates.append(file.name)
        index = self.by_sha1[file.sha1]
        first = self.images[index]
        form = first.meta.get("form")
        if match is None or match.lastindex != 2 or not match.group(2).isdigit() or not form:
            return
        key, skin_id = match.group(1), match.group(2).zfill(2)
        if first.meta.get("character_id") != key:
            return
        _short, phrase, _base = self._look(skin_id, self._skins(key).get(skin_id))
        note = f"wiki 上{first.meta['character']}{phrase}的{form}用的也是这张图。"
        shared = [*first.meta.get("shared_with", []), skin_id]
        self.images[index] = dataclasses.replace(
            first, text=first.text + note, meta=first.meta | {"shared_with": shared}
        )

    def owner(self, key: str, file: WikiFile) -> _Owner | None:
        name = self.roster.names.get(key)
        if not name:
            self.unknown_owner.append(file.name)
            return None
        if key in self.roster.onmyoji:
            page = self.settings.datasets.onmyoji_page
            return _Owner(key, name, "阴阳师", "", page)
        return _Owner(key, name, "式神", self.rarity.get(key, ""), self.roster.pages.get(key, name))

    def add(
        self,
        file: WikiFile,
        *,
        kind: str,
        label: str,
        text: str,
        subject: str,
        page_title: str,
        **meta: Any,
    ) -> None:
        known = self.data.known_titles
        if not page_title or (known is not None and page_title not in known):
            page_title = f"File:{file.name}"  # 条目页还没建：指向图片自己的页面
        self.by_sha1[file.sha1] = len(self.images)
        self.images.append(
            KbImage(
                image_id=file.name,
                kind=kind,
                label=label,
                page_title=page_title,
                url=self.settings.wiki.page_url(page_title),
                image_url=file.url,
                text=text,
                subject=subject,
                meta={k: v for k, v in meta.items() if v not in ("", None, [])}
                | {"width": file.width, "height": file.height},
                sha1=file.sha1,
            )
        )

    # ── 立绘、卡面、头像 ────────────────────────────────────────────────

    def _skins(self, key: str) -> dict[str, dict[str, Any]]:
        entry = (self.data.skin or {}).get(key)
        skins = entry.get("skins") if isinstance(entry, dict) else None
        return {
            str(s.get("skin_id")).zfill(2): s
            for s in skins or []
            if isinstance(s, dict) and s.get("skin_id") is not None
        }

    def _look(self, skin_id: str, skin: dict[str, Any] | None) -> tuple[str, str, bool]:
        """一个造型的两种写法（接在名字后面的、用在句子里的），以及它是不是式神本身的造型。"""
        if int(skin_id) == 1:
            return "初始", "初始造型", True
        if skin is None:
            return (
                f"皮肤（编号 {skin_id}，名字待补）",
                f"的一个皮肤（皮肤编号 {skin_id}，wiki 的皮肤数据里还没有它的名字）",
                False,
            )
        if self.cfg.awakened_way and _text(skin.get("skin_way")) == self.cfg.awakened_way:
            return "觉醒", "觉醒造型", True
        name = _text(skin.get("skin_name")) or f"皮肤 {skin_id}"
        return f"皮肤「{name}」", f"的皮肤「{name}」", False

    def skin_subject(self, key: str, skin_id: str, skin: dict[str, Any] | None) -> str:
        """一个造型的 ``subject``。按名字算：同一个角色有两个同名皮肤时（数据里各占一个编号，
        图几乎一样，答出来也是同一句话），算同一个造型。"""
        return f"skin:{key}:{self._look(skin_id, skin)[0]}"

    def skin_family(self, prefix: str, form: str) -> None:
        """``<前缀><角色编号>_<皮肤编号>``：立绘大图、卡面小图、带皮肤编号的头像。"""
        for file, match in self.numbered(prefix, r"_(\d+)"):
            key, skin_id = match.group(1), match.group(2).zfill(2)
            owner = self.owner(key, file)
            if owner is None:
                continue
            skins = self._skins(key)
            skin = skins.get(skin_id)
            short, phrase, base = self._look(skin_id, skin)
            if skin is None and int(skin_id) != 1:
                self.unnamed_skins.append(file.name)
            if form == FORM_AVATAR:
                kind = f"{owner.role}头像" if base else KIND_SKIN_AVATAR
                label = f"{owner.name}的{short}头像"
            else:
                kind = f"{owner.role}立绘" if base else KIND_SKIN
                label = f"{owner.name}的{short}立绘" if base else f"{owner.name}的{short}"
            what = "卡面小图（立绘的半身特写）" if form == FORM_CARD else form
            parts = [f"这是{owner.who}{phrase}的{what}。"]
            details = skin or {}
            for title, field_name in (("类型", "type"), ("获取方式", "skin_way"), ("日期", "date")):
                if _text(details.get(field_name)):
                    parts.append(f"{title}：{_text(details[field_name])}。")
            names = [_text(s.get("skin_name")) for s in skins.values() if _text(s.get("skin_name"))]
            if names:
                parts.append(f"{owner.name}的全部造型（{len(names)} 个）：{'、'.join(names)}。")
            self.add(
                file,
                kind=kind,
                label=label,
                text="".join(parts),
                subject=self.skin_subject(key, skin_id, skin),
                page_title=owner.page_title,
                form=form,
                character=owner.name,
                character_id=key,
                role=owner.role,
                rarity=owner.rarity,
                skin_id=skin_id,
                skin_name=_text(details.get("skin_name")),
            )

    def default_avatars(self, prefix: str) -> None:
        """``<前缀><角色编号>_s``：不分造型的头像。

        头像是从初始造型的立绘上截的脸，所以和初始造型算同一个 ``subject``：
        不然一张初始卡面的截图会同时很像卡面和头像，两个「造型」分不出高下。
        """
        for file, match in self.numbered(prefix, "_s"):
            owner = self.owner(match.group(1), file)
            if owner is None:
                continue
            self.add(
                file,
                kind=f"{owner.role}头像",
                label=f"{owner.name}的头像",
                text=f"这是{owner.who}的头像。",
                subject=self.skin_subject(owner.key, "01", None),
                page_title=owner.page_title,
                form=FORM_AVATAR,
                character=owner.name,
                character_id=owner.key,
                role=owner.role,
                rarity=owner.rarity,
            )

    # ── 绘卷配图、条目背景图 ────────────────────────────────────────────

    def scrolls(self, prefix: str) -> None:
        for file, match in self.numbered(prefix, r"_(\d+)"):
            key, number = match.group(1), int(match.group(2))
            owner = self.owner(key, file)
            if owner is None:
                continue
            chapters = (self.data.characters.get(key) or {}).get("huijuan") or []
            chapter = next(
                (c for c in chapters if isinstance(c, dict) and c.get("index") == number), {}
            )
            title = _text(chapter.get("title"))
            named = f"第 {number} 章「{title}」" if title else f"第 {number} 章"
            text = f"这是{owner.who}绘卷{named}的配图。"
            story = _plain(chapter.get("text"), "")
            if story:
                more = "……" if len(story) > _EXCERPT_CHARS else ""
                text += f"这一章的开头：{story[:_EXCERPT_CHARS]}{more}"
            self.add(
                file,
                kind=KIND_SCROLL,
                label=f"{owner.name}的绘卷「{title}」配图"
                if title
                else f"{owner.name}的绘卷{named}配图",
                text=text,
                subject=f"scroll:{key}:{number}",
                page_title=owner.page_title,
                character=owner.name,
                character_id=key,
                role=owner.role,
                rarity=owner.rarity,
                chapter=number,
                chapter_title=title,
            )

    def backgrounds(self, prefix: str) -> None:
        for file, match in self.numbered(prefix, ""):
            owner = self.owner(match.group(1), file)
            if owner is None:
                continue
            self.add(
                file,
                kind=KIND_BACKGROUND,
                label=f"{owner.name}的条目背景图",
                text=f"这是 wiki 上{owner.who}条目页用的背景插画。",
                subject=f"background:{owner.key}",
                page_title=owner.page_title,
                character=owner.name,
                character_id=owner.key,
                role=owner.role,
                rarity=owner.rarity,
            )

    # ── 御魂 ──────────────────────────────────────────────────────────

    def souls(self) -> None:
        for soul in (self.data.soul or {}).get("soul") or []:
            name = _text(soul.get("name")) if isinstance(soul, dict) else ""
            if not name:
                continue
            parts = []
            for title, field_name in (
                ("属性加成", "suit"),
                ("套装效果", "desc"),
                ("获取途径", "way"),
            ):
                if _plain(soul.get(field_name)):
                    parts.append(f"{title}：{_plain(soul[field_name]).rstrip('。')}。")
            for file_name, form in soul_files(name):
                file = self.files.get(file_name)
                if file is None:
                    continue
                if file.sha1 in self.by_sha1:
                    self.same_content(file)
                    continue
                self.add(
                    file,
                    kind=KIND_SOUL,
                    label=f"御魂「{name}」",
                    text=f"这是御魂「{name}」的{form}。" + "".join(parts),
                    subject=f"soul:{name}",
                    page_title=self.cfg.soul_page,
                    form=form,
                    soul=name,
                )


def soul_files(name: str) -> list[tuple[str, str]]:
    """一个御魂在 wiki 上的两张图：（文件名，是什么图）。"""
    return [(f"{name}.png", "图鉴大图"), (f"{name}_s.png", "小图标")]


def build_catalog(files: dict[str, WikiFile], data: CatalogData, settings: YysEnv) -> Catalog:
    """文件清单 + wiki 数据 → 记录。纯计算，不发请求。"""
    images = settings.images
    builder = _Builder(files, data, settings)
    builder.skin_family(images.art_prefix, FORM_ART)
    builder.skin_family(images.card_prefix, FORM_CARD)
    builder.default_avatars(images.avatar_prefix)
    builder.skin_family(images.avatar_prefix, FORM_AVATAR)
    if images.souls:
        builder.souls()
    builder.scrolls(images.scroll_prefix)
    builder.backgrounds(images.background_prefix)

    with_art = {i.subject for i in builder.images if i.meta.get("form") == FORM_ART}
    missing_art = [
        f"{entry.get('name')}·{skin.get('skin_name')}"
        for key, entry in sorted((data.skin or {}).items())
        if isinstance(entry, dict)
        for skin in entry.get("skins") or []
        if isinstance(skin, dict)
        and builder.skin_subject(key, str(skin.get("skin_id")).zfill(2), skin) not in with_art
    ]
    stats = {
        "images": len(builder.images),
        "by_kind": dict(Counter(i.kind for i in builder.images).most_common()),
        "subjects": len({i.group for i in builder.images}),
        # 文件在、皮肤数据里还没有名字的
        "unnamed_skins": builder.unnamed_skins,
        # 编号在数据里查不到是谁的文件（没收）
        "unknown_owner": builder.unknown_owner,
        # 内容和已收的某张图完全一样的文件（没收；是同一个角色的造型时，记在先收的那张的说明里）
        "duplicates": len(builder.duplicates),
        # 皮肤数据里有、wiki 上还没有自己的立绘大图的造型
        "skins_without_art": len(missing_art),
        "skins_without_art_sample": missing_art[:20],
    }
    unknown = builder.unknown_owner
    notes = [
        f"没收的：{len(unknown)} 个文件的编号在数据里查不到是谁的（{'、'.join(unknown[:5])}）；"
        f"{len(builder.duplicates)} 个文件和已收的图内容完全一样。"
        f"另有 {len(builder.unnamed_skins)} 张图的皮肤在数据里还没有名字（照收，名字待补）；"
        f"皮肤数据里有 {len(missing_art)} 个造型 wiki 上还没有自己的立绘大图"
    ]
    return Catalog(images=builder.images, stats=stats, notes=notes)


# ── 取文件清单和数据文档 ──────────────────────────────────────────────


def _scroll_owners(files: Iterable[str], prefix: str) -> list[str]:
    if not prefix:
        return []
    pattern = re.compile(rf"{re.escape(prefix)}(\d+)_\d+\.{_EXTENSIONS}", re.IGNORECASE)
    return sorted({m.group(1) for name in files if (m := pattern.fullmatch(name))}, key=int)


async def gather(
    source: WikiSource, settings: YysEnv, huiji: HuijiKnowledge
) -> tuple[dict[str, WikiFile], CatalogData]:
    """取齐文件清单和数据文档。离线时用上次联网存下的快照，数据文档读缓存。"""
    cache = source.cache
    images = settings.images
    titles = data_titles(settings)
    snapshot = cache.get_named(SNAPSHOT_FILE)
    if source.fetcher is None:
        if snapshot is None:
            raise FetchError("离线模式需要先联网跑过一次 images collect（缓存里没有文件清单）")
        files = {f["name"]: WikiFile(**f) for f in snapshot.get("files") or []}
        refs = {title: PageRef(**ref) for title, ref in (snapshot.get("docs") or {}).items()}
    else:
        fetcher = source.fetcher
        files = {}
        for prefix in images.prefixes:
            files |= await list_files(fetcher, prefix)
        wanted = [title for title in titles.values() if title]
        wanted += [
            images.character_doc.format(id=key)
            for key in _scroll_owners(files, images.scroll_prefix)
            if images.character_doc
        ]
        refs = {ref.title: ref for ref in await source.resolve_titles(wanted)}

    async def doc(title: str) -> dict[str, Any] | None:
        ref = refs.get(title) or refs.get(title.replace("_", " "))
        if ref is None:
            if title:
                logger.warning("kb_images_doc_missing", title=title)
            return None
        try:
            return await huiji.data_doc(source, ref)
        except FetchError as exc:
            logger.warning("kb_images_doc_failed", title=title, error=str(exc))
            return None

    listing = cache.get_named("listing.json") or {}  # 知识库预处理存下的页面清单
    known = frozenset(str(p.get("title")) for p in listing.get("pages") or [])
    data = CatalogData(
        character_index=await doc(titles["character_index"]),
        skin=await doc(titles["skin"]),
        soul=await doc(titles["soul"]),
        known_titles=known or None,
    )
    for key in _scroll_owners(files, images.scroll_prefix):
        character = await doc(images.character_doc.format(id=key)) if images.character_doc else None
        if character is not None:
            data.characters[key] = character
    if source.fetcher is not None:
        names = [
            file_name
            for soul in (data.soul or {}).get("soul") or []
            if isinstance(soul, dict) and soul.get("name")
            for file_name, _ in soul_files(_text(soul["name"]))
        ]
        files |= await lookup_files(source.fetcher, names)
        cache.put_named(
            SNAPSHOT_FILE,
            {
                "fetched_at": time.time(),
                "files": [asdict(file) for file in files.values()],
                "docs": {title: asdict(ref) for title, ref in refs.items()},
            },
        )
    return files, data


def data_titles(settings: YysEnv) -> dict[str, str]:
    """图片的名字和说明来自的几份数据：是什么 → Data 文档的标题（关掉的是空的）。"""
    return {
        "character_index": settings.datasets.character_index,
        "skin": settings.datasets.skin,
        "soul": settings.entities.soul if settings.images.souls else "",
    }


async def image_catalog(source: WikiSource, settings: YysEnv, huiji: HuijiKnowledge) -> Catalog:
    """选图、生成记录。名字和说明来自的数据没取到时照样生成，但在 ``problem`` 里说明
    （少了这些，图片会认不出是谁的而被丢掉，或者名字全变成「待补」）。"""
    files, data = await gather(source, settings, huiji)
    catalog = build_catalog(files, data, settings)
    titles = data_titles(settings)
    lacking = [
        title
        for title, doc in (
            (titles["character_index"], data.character_index),
            (titles["skin"], data.skin),
            (titles["soul"], data.soul),
        )
        if title and doc is None
    ]
    if lacking:
        catalog.problem = f"图片的名字和说明来自的数据没取到（{'、'.join(lacking)}）"
    return catalog
