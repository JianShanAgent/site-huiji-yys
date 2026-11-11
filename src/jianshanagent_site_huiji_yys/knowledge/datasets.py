"""由 Data 数据集生成文档：页面上没有渲染出来（或只渲染了一部分）的数据。

这些文档的类型是 ``data``，``source`` 写明来自哪个数据集，切片规则和别的文档一样。
写法上的原则：一个切片单独拿出来也能看懂在说谁、说什么——每一行都带着主语
（「山风的皮肤」「酒吞童子的技能『狂啸』」「沉睡（减益·状态）」）。

各数据集的结构（取了真实文档核对过）和生成方式：

- 皮肤 ``Data:Skin.json``：``{"<角色编号>": {id, name, skins: [{skin_id, skin_name, skin_way,
  date, type}]}}``。一个角色一篇「X的皮肤」，链接指向它的条目页（式神索引里的 ``pageTitle``）；
  阴阳师（编号在皮肤数据里、不在式神索引里的：晴明、神乐……）没有自己的条目页，指向「阴阳师」页面。
- 百鬼棋局 ``Data:Zzq/character.json`` / ``fuzhou.json`` / ``soul.json``：
  式神（``zzq_character``：费用、站位、定位、羁绊、三个星级的生命和攻击、两个技能各级说明）、
  符咒（``zzq_fuzhou``：名称、品质、说明）、御魂（``zzq_soul``：名称、类别、说明）各一篇，
  式神按棋子成节，符咒按品质、御魂按类别成节。链接指向「百鬼棋局」。
  属性的写法照着「百鬼棋局/梦山白藏主」页面：暴击伤害 = 1 + ``CP``，``Fire`` 是初始鬼火，
  第一个技能是普攻（``fire`` 是回复的鬼火），第二个是技能（``fire`` 是消耗的鬼火）。
- 花合战对白 ``Data:Monthillustration.json``：``{"<期数>": {hero_name, date: "YYYY-MM",
  chapters: [{index, title, desc, dialogue: [{scene_id, speaker_id, flag, text}]}]}}``。
  页面只渲染了 ``desc``，对白没有渲染。有对白的月份各一篇，按章节成节。
  说话人：``flag`` 为 0 时是 ``scene_id``，为 1 时是 ``speaker_id``（拿对白正文核对过）；
  编号在式神索引和皮肤数据里查名字，查不到的用配置里的对照表，再查不到写「角色<编号>」。
  个别台词末尾带着多余的 ``,1``，去掉。
- 状态说明 ``Data:SkillBuffs.json``：``buff: {"<编号>_<等级>": {name, buffDesc, type, kind}}``。
  只补库里没有的：名字和说明在条目正文里挨着出现过的（式神页、技能检索页的状态列表）不再放；
  说明和名字一样、没有信息量的不放。按「类型·种类」成节，一条一行「名称（类型·种类）：说明」。
- 超鬼王 ``Data:SuperGhost/<年份>.json``：``missions: [{star, battle_time, escape_time,
  score_mul, boss: {name, skills: [{name, desc}]}, minions: [{name, def, spd}]}]``。
  页面已经渲染了首领属性、随从防御和超鬼王专属的技能；这里只补页面没有的：
  随从速度、各星级的时间和积分系数，以及说明不在库里的技能（逐句比对，全部句子都在库里才算已有）。

说明文字里的占位符（做法参考 wiki_query 插件）：``[tips_N]`` 查 ``Data:SkillTips.json``，
``[buff_N_L]`` 查 ``Data:SkillBuffs.json``，``[skill_N_L]`` 查同一个单位的技能表，换成「名字」；
``[extra_…]`` 是附加说明，去掉外壳留下文字；``<br>`` 换成换行。
解析不了的（``[s_伤害加成_per]`` 这类要到技能数值表里取数的）原样保留，并计数。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from jianshanagent.plugins.knowledge.markdown import normalize_text
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARA,
    KIND_DATA,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.sources import PageRef

from jianshanagent_site_huiji_yys.knowledge.settings import DatasetSettings, YysEnv

CATEGORY_SKIN = "skin"
CATEGORY_ZZQ_CHARACTER = "zzq_character"
CATEGORY_ZZQ_FUZHOU = "zzq_fuzhou"
CATEGORY_ZZQ_SOUL = "zzq_soul"
CATEGORY_DIALOGUE = "month_dialogue"
CATEGORY_BUFF = "buff"
CATEGORY_SUPER_GHOST = "super_ghost"
CATEGORY_LABELS = {
    CATEGORY_SKIN: "皮肤获取方式",
    CATEGORY_ZZQ_CHARACTER: "百鬼棋局·式神",
    CATEGORY_ZZQ_FUZHOU: "百鬼棋局·符咒",
    CATEGORY_ZZQ_SOUL: "百鬼棋局·御魂",
    CATEGORY_DIALOGUE: "花合战对白",
    CATEGORY_BUFF: "状态说明",
    CATEGORY_SUPER_GHOST: "超鬼王",
}

_PLACEHOLDER_RE = re.compile(r"\[(tips|buff|skill)_([0-9_]+)\]")
_EXTRA_RE = re.compile(r"\[extra_([^\]]*)\]")
#: 剩下的方括号标记：``[s_伤害加成_per]``、``[b_持续伤害]``、没解析出来的 ``[skill_…]``
_MARKER_RE = re.compile(r"\[[a-z]+_[^\]]*\]")
_NESTED_QUOTE_RE = re.compile(r"『「([^」]*)」』")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_RE = re.compile(r"[。；;！？\n]")
_TRAILING_FLAG_RE = re.compile(r",\d$")
#: 说明里没填数值的裸参数（「每回合造成最大生命d%的伤害」「相当于自身血量d1%」）
_BARE_PARAM_RE = re.compile(r"(?<![A-Za-z0-9])[dp]\d?%")


def match_key(text: str) -> str:
    """比对「在不在库里」用的写法：去掉标签、空白和标点，只留字、数字和百分号。"""
    return re.sub(r"[^\w%]+", "", _TAG_RE.sub("", text or ""))


def _text(value: Any) -> str:
    return normalize_text(str(value if value is not None else "")).strip()


def _number(value: Any) -> str:
    """数值的写法：整数不带小数点。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass(slots=True)
class Resolver:
    """把说明文字里的占位符换成名字。"""

    buffs: dict[str, Any] = field(default_factory=dict)
    tips: dict[str, Any] = field(default_factory=dict)
    #: 解析不了、原样留下的标记 → 次数
    unresolved: Counter[str] = field(default_factory=Counter)

    @classmethod
    def build(cls, buffs_doc: dict[str, Any] | None, tips_doc: dict[str, Any] | None) -> Resolver:
        buffs = (buffs_doc or {}).get("buff")
        tips = (tips_doc or {}).get("tips")
        return cls(
            buffs=buffs if isinstance(buffs, dict) else {},
            tips=tips if isinstance(tips, dict) else {},
        )

    def _name(self, kind: str, key: str, skills: dict[str, str]) -> str:
        if kind == "tips":
            return _text((self.tips.get(key) or {}).get("skill_tips_name"))
        if kind == "buff":
            return _text((self.buffs.get(key) or {}).get("name"))
        # [skill_576_1]：同一个单位的技能 576（等级那一段不管）
        return skills.get(key.split("_")[0], "")

    def resolve(self, text: Any, skills: dict[str, str] | None = None) -> str:
        """解析占位符。``skills`` 是同一个单位的技能：编号 → 名字。"""

        def substitute(match: re.Match[str]) -> str:
            name = self._name(match.group(1), match.group(2), skills or {})
            return f"「{name}」" if name else match.group(0)

        out = _BR_RE.sub("\n", str(text or ""))
        out = _TAG_RE.sub("", out)
        out = _EXTRA_RE.sub(lambda m: m.group(1), out)
        out = _NESTED_QUOTE_RE.sub(r"『\1』", _PLACEHOLDER_RE.sub(substitute, out))
        self.unresolved.update(_MARKER_RE.findall(out))
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalize_text(out).split("\n")]
        return "\n".join(line for line in lines if line)


@dataclass(slots=True)
class Roster:
    """角色编号 → 名字、条目页。式神来自式神索引，阴阳师来自皮肤数据。"""

    names: dict[str, str] = field(default_factory=dict)
    pages: dict[str, str] = field(default_factory=dict)
    onmyoji: dict[str, str] = field(default_factory=dict)  # 编号 → 名字

    @classmethod
    def build(cls, character_index: dict[str, Any] | None, skin: dict[str, Any] | None) -> Roster:
        roster = cls()
        for character in (character_index or {}).get("characters") or []:
            if isinstance(character, dict) and character.get("name"):
                key = str(character.get("id"))
                roster.names[key] = _text(character["name"])
                roster.pages[key] = _text(character.get("pageTitle") or character["name"])
        for key, entry in (skin or {}).items():
            if isinstance(entry, dict) and entry.get("name") and str(key) not in roster.names:
                roster.onmyoji[str(key)] = _text(entry["name"])
        roster.names |= roster.onmyoji
        return roster


def onmyoji_names(character_index: dict[str, Any] | None, skin: dict[str, Any] | None) -> list[str]:
    """皮肤数据里有、式神索引里没有的角色：阴阳师（晴明、神乐、源博雅……）。"""
    if not character_index or not skin:
        return []
    return list(Roster.build(character_index, skin).onmyoji.values())


@dataclass(slots=True)
class DatasetResult:
    documents: list[Document] = field(default_factory=list)
    #: 类别 → 统计（文档数之外的细节：条目数、已在库的数量……）
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    unresolved: Counter[str] = field(default_factory=Counter)


def _document(
    key: str,
    title: str,
    category: str,
    ref: PageRef,
    settings: YysEnv,
    blocks: list[Block],
    *,
    link: str = "",
    known_titles: frozenset[str] | set[str] | None = None,
    **meta: Any,
) -> Document:
    """链接指向 ``link`` 页面；那个页面不存在就指向数据文档本身。"""
    exists = bool(link) and (known_titles is None or link in known_titles)
    return Document(
        doc_key=f"data:{key}",
        kind=KIND_DATA,
        title=title,
        url=settings.wiki.page_url(link if exists else ref.title),
        source=f"数据·{ref.data_id}（{CATEGORY_LABELS[category]}）",
        page_id=ref.page_id,
        rev_id=ref.rev_id,
        blocks=blocks,
        meta={"dataset": ref.data_id, "category": category, **meta},
    )


# ── 皮肤 ──────────────────────────────────────────────────────────────


def build_skins(
    ref: PageRef,
    doc: dict[str, Any],
    roster: Roster,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    cfg = settings.datasets
    documents: list[Document] = []
    skins_total = 0
    with_way = 0
    entries = [(str(k), v) for k, v in doc.items() if isinstance(v, dict) and v.get("name")]
    entries.sort(key=lambda item: (int(item[0]) if item[0].isdigit() else 10**9, item[0]))
    for key, entry in entries:
        name = _text(entry["name"])
        skins = [s for s in entry.get("skins") or [] if isinstance(s, dict)]
        items: list[str] = []
        for skin in skins:
            skin_name = _text(skin.get("skin_name")) or f"皮肤 {_text(skin.get('skin_id'))}"
            parts = []
            if _text(skin.get("type")):
                parts.append(f"类型 {_text(skin['type'])}")
            if _text(skin.get("skin_way")):
                parts.append(f"获取方式 {_text(skin['skin_way'])}")
                with_way += 1
            if _text(skin.get("date")):
                parts.append(f"日期 {_text(skin['date'])}")
            detail = "；".join(parts) or "数据里没有写获取方式"
            items.append(f"- {name}的皮肤「{skin_name}」：{detail}")
        if not items:
            continue
        skins_total += len(items)
        is_onmyoji = key in roster.onmyoji
        blocks = [
            Block(BLOCK_PARA, text=f"{name}共有 {len(items)} 个皮肤（含初始皮肤）："),
            Block(BLOCK_LIST, items=items),
        ]
        documents.append(
            _document(
                f"skin:{key}",
                f"{name}的皮肤",
                CATEGORY_SKIN,
                ref,
                settings,
                blocks,
                link=cfg.onmyoji_page if is_onmyoji else roster.pages.get(key, name),
                known_titles=known_titles,
                character=name,
                character_id=key,
                role="阴阳师" if is_onmyoji else "式神",
            )
        )
    stats = {
        "characters": len(documents),
        "onmyoji": sum(1 for d in documents if d.meta["role"] == "阴阳师"),
        "skins": skins_total,
        "skins_with_way": with_way,
    }
    return documents, stats


# ── 百鬼棋局 ──────────────────────────────────────────────────────────


def _stars(values: Any) -> str:
    """``{s1, s2, s3}`` → 「a / b / c」。"""
    if not isinstance(values, dict):
        return _number(values)
    return " / ".join(_number(values[k]) for k in sorted(values))


def _zzq_skill(name: str, label: str, fire_label: str, skill: Any, resolver: Resolver) -> str:
    if not isinstance(skill, dict) or not skill.get("name"):
        return ""
    desc = skill.get("desc") if isinstance(skill.get("desc"), dict) else {}
    levels = [(k, resolver.resolve(v)) for k, v in sorted(desc.items()) if _text(v)]
    head = f"{name}的{label}「{_text(skill['name'])}」"
    if skill.get("fire"):
        head += f"（{fire_label}鬼火 {_number(skill['fire'])}）"
    if not levels:
        return f"{head}：数据里没有说明"
    if len({text for _, text in levels}) == 1:
        return f"{head}：{levels[0][1]}"
    lines = [f"{key.replace('lv', 'Lv')}：{text}" for key, text in levels]
    return "\n".join([f"{head}（各等级）：", *lines])


def build_zzq_characters(
    ref: PageRef,
    doc: dict[str, Any],
    resolver: Resolver,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    blocks: list[Block] = []
    count = 0
    for piece in doc.get("zzq_character") or []:
        if not isinstance(piece, dict) or not piece.get("hero_name"):
            continue
        name = _text(piece["hero_name"])
        count += 1
        bonds = "、".join(
            _text(b.get("name")) for b in piece.get("bonds") or [] if isinstance(b, dict)
        )
        basics = [
            f"{_number(piece.get('cost'))} 费",
            f"站位 {_text(piece.get('location'))}",
            f"定位 {_text(piece.get('type'))}",
            f"羁绊 {bonds}" if bonds else "没有羁绊",
        ]
        attributes = [
            f"生命 {_stars(piece.get('HP'))}",
            f"攻击 {_stars(piece.get('ATK'))}",
            f"防御 {_number(piece.get('DEF'))}",
        ]
        if isinstance(piece.get("CR"), (int, float)):
            attributes.append(f"暴击 {round(piece['CR'] * 100)}%")
        if isinstance(piece.get("CP"), (int, float)):
            attributes.append(f"暴击伤害 {round((1 + piece['CP']) * 100)}%")
        if piece.get("Fire") is not None:
            attributes.append(f"初始鬼火 {_number(piece['Fire'])} 点")
        blocks.append(Block(BLOCK_HEADING, text=name, level=1))
        blocks.append(Block(BLOCK_PARA, text=f"百鬼棋局棋子「{name}」：{'，'.join(basics)}。"))
        blocks.append(
            Block(
                BLOCK_PARA,
                text=f"{name}的棋子属性（生命、攻击依次为 1/2/3 星）：{'，'.join(attributes)}。",
            )
        )
        skills = piece.get("Skill") if isinstance(piece.get("Skill"), dict) else {}
        for key, label, fire_label in (("skill1", "普攻", "回复"), ("skill2", "技能", "消耗")):
            text = _zzq_skill(name, label, fire_label, skills.get(key), resolver)
            if text:
                blocks.append(Block(BLOCK_PARA, text=text))
    if not count:
        return [], {"entries": 0}
    page = settings.datasets.zzq_page
    document = _document(
        "zzq:character",
        f"{page}·式神图鉴",
        CATEGORY_ZZQ_CHARACTER,
        ref,
        settings,
        blocks,
        link=page,
        known_titles=known_titles,
    )
    return [document], {"entries": count}


def _grouped_list(entries: list[tuple[str, str]], heading: str, lead: str) -> list[Block]:
    """一节：标题 + 引导句 + 列表。``entries`` 是（名称，说明）。"""
    return [
        Block(BLOCK_HEADING, text=heading, level=1),
        Block(BLOCK_PARA, text=lead),
        Block(BLOCK_LIST, items=[f"- {name}：{desc}" for name, desc in entries]),
    ]


def build_zzq_fuzhou(
    ref: PageRef,
    doc: dict[str, Any],
    resolver: Resolver,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for item in doc.get("zzq_fuzhou") or []:
        if not isinstance(item, dict) or not item.get("fuzhou_name"):
            continue
        quality = _text(item.get("quality")) or "未分级"
        desc = resolver.resolve(item.get("desc")).replace("\n", "；") or "数据里没有说明"
        groups.setdefault((_number(item.get("rarity") or ""), quality), []).append(
            (f"{quality}符咒「{_text(item['fuzhou_name'])}」", desc)
        )
    blocks: list[Block] = []
    page = settings.datasets.zzq_page
    for (rarity, quality), entries in sorted(groups.items()):
        rank = f"稀有度 {rarity}，" if rarity else ""
        lead = f"{page}的{quality}符咒（{rank}共 {len(entries)} 个）："
        blocks += _grouped_list(entries, f"{quality}符咒", lead)
    count = sum(len(v) for v in groups.values())
    if not count:
        return [], {"entries": 0}
    document = _document(
        "zzq:fuzhou",
        f"{page}·符咒图鉴",
        CATEGORY_ZZQ_FUZHOU,
        ref,
        settings,
        blocks,
        link=page,
        known_titles=known_titles,
    )
    return [document], {"entries": count, "groups": len(groups)}


def build_zzq_souls(
    ref: PageRef,
    doc: dict[str, Any],
    resolver: Resolver,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for item in doc.get("zzq_soul") or []:
        if not isinstance(item, dict) or not item.get("soul_name"):
            continue
        kind = _text(item.get("type")) or "御魂"
        desc = resolver.resolve(item.get("desc")).replace("\n", "；") or "数据里没有说明"
        extras = []
        if _text(item.get("sub_desc")):
            extras.append(_text(item["sub_desc"]))
        bond = item.get("bond")
        if isinstance(bond, dict) and bond.get("name"):
            extras.append(f"对应羁绊：{_text(bond['name'])}")
        if extras:
            desc = f"{desc}（{'；'.join(extras)}）"
        groups.setdefault(kind, []).append((f"{kind}「{_text(item['soul_name'])}」", desc))
    blocks: list[Block] = []
    page = settings.datasets.zzq_page
    for kind, entries in groups.items():
        blocks += _grouped_list(entries, kind, f"{page}的{kind}（共 {len(entries)} 个）：")
    count = sum(len(v) for v in groups.values())
    if not count:
        return [], {"entries": 0}
    document = _document(
        "zzq:soul",
        f"{page}·御魂图鉴",
        CATEGORY_ZZQ_SOUL,
        ref,
        settings,
        blocks,
        link=page,
        known_titles=known_titles,
    )
    return [document], {"entries": count, "groups": len(groups)}


# ── 花合战对白 ────────────────────────────────────────────────────────


def dialogue_speaker(
    line: dict[str, Any], roster: Roster, cfg: DatasetSettings
) -> tuple[str, bool]:
    """一句对白的说话人。返回（名字，是否查到了名字）。

    ``flag`` 为 0 时说话的是 ``scene_id``，为 1 时是 ``speaker_id``。
    """
    key = str(line.get("speaker_id") if line.get("flag") else line.get("scene_id"))
    name = roster.names.get(key) or cfg.speakers.get(key)
    return (name, True) if name else (f"角色{key}", False)


def build_month_dialogues(
    ref: PageRef,
    doc: dict[str, Any],
    roster: Roster,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    cfg = settings.datasets
    documents: list[Document] = []
    lines_total = 0
    chars = 0
    trimmed = 0
    unknown: Counter[str] = Counter()
    months = [(str(k), v) for k, v in doc.items() if isinstance(v, dict)]
    months.sort(key=lambda item: (int(item[0]) if item[0].isdigit() else 10**9, item[0]))
    for key, month in months:
        date = _text(month.get("date"))
        hero = _text(month.get("hero_name"))
        blocks: list[Block] = []
        for chapter in month.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            lines: list[Block] = []
            for line in chapter.get("dialogue") or []:
                if not isinstance(line, dict):
                    continue
                text = _text(line.get("text"))
                if _TRAILING_FLAG_RE.search(text):
                    text = _TRAILING_FLAG_RE.sub("", text)
                    trimmed += 1
                if not text:
                    continue
                speaker, found = dialogue_speaker(line, roster, cfg)
                if not found:
                    unknown[speaker] += 1
                lines.append(Block(BLOCK_PARA, text=f"{speaker}：{text}", tight=True))
                chars += len(text)
            if not lines:
                continue
            lines_total += len(lines)
            title = _text(chapter.get("title")) or f"第 {_text(chapter.get('index'))} 章"
            blocks.append(Block(BLOCK_HEADING, text=title, level=1))
            blocks += lines
        if not blocks:
            continue
        match = re.fullmatch(r"(\d{4})-(\d{1,2})", date)
        yyyymm = f"{match.group(1)}{int(match.group(2)):02d}" if match else date
        when = f"{match.group(1)}年{int(match.group(2))}月" if match else date
        lead = f"{when}花合战（本期式神：{hero}）的剧情对白，按章节排列。"
        documents.append(
            _document(
                f"month_dialogue:{key}",
                f"花合战 {date} {hero}·剧情对白",
                CATEGORY_DIALOGUE,
                ref,
                settings,
                [Block(BLOCK_PARA, text=lead), *blocks],
                link=cfg.month_page.format(yyyymm=yyyymm),
                known_titles=known_titles,
                month=date,
                character=hero,
            )
        )
    stats = {
        "months": len(documents),
        "lines": lines_total,
        "chars": chars,
        "trailing_flags_removed": trimmed,
        "unknown_speakers": dict(unknown),
    }
    return documents, stats


# ── 状态说明 ──────────────────────────────────────────────────────────


def build_buffs(
    ref: PageRef,
    doc: dict[str, Any],
    resolver: Resolver,
    kb_text: str,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    """只补库里没有的状态说明。``kb_text`` 是条目正文的比对写法（见 :func:`match_key`）。"""
    buffs = doc.get("buff")
    entries = [
        b for b in (buffs.values() if isinstance(buffs, dict) else []) if isinstance(b, dict)
    ]
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    # 已在库的条目不该把它的占位符算进「解析不了」里：先用临时的解析器
    probe = Resolver(resolver.buffs, resolver.tips)
    for buff in entries:
        counts["total"] += 1
        name = _text(buff.get("name"))
        desc = probe.resolve(buff.get("buffDesc"))
        key = (name, match_key(desc))
        if not name or key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(key)
        if not key[1] or key[1] == match_key(name):
            counts["no_information"] += 1  # 说明就是名字本身（「变吸血鬼」：变吸血鬼）
            continue
        kind, sort = _text(buff.get("type")), _text(buff.get("kind"))
        # 页面上的写法：「名称：【增益、印记】说明」
        if any(
            f"{match_key(name)}{middle}{key[1]}" in kb_text
            for middle in ("", match_key(kind + sort))
        ):
            counts["in_kb"] += 1
            continue
        counts["added"] += 1
        desc = resolver.resolve(buff.get("buffDesc")).replace("\n", "；")
        counts["bare_params"] += bool(_BARE_PARAM_RE.search(desc))
        label = "·".join(part for part in (kind, sort) if part)
        groups.setdefault((kind, sort), []).append((f"{name}（{label}）" if label else name, desc))
    blocks: list[Block] = []
    for (kind, sort), items in groups.items():
        label = "·".join(part for part in (kind, sort) if part) or "未分类"
        lead = f"游戏数据里的{label}说明（「名称：说明」，共 {len(items)} 条）："
        blocks += _grouped_list(items, label, lead)
    stats = dict(counts) | {"groups": len(groups)}
    if not blocks:
        return [], stats
    document = _document(
        "buff",
        "状态与印记说明（数据补充）",
        CATEGORY_BUFF,
        ref,
        settings,
        blocks,
        link=settings.datasets.buffs_page,
        known_titles=known_titles,
    )
    return [document], stats


# ── 超鬼王 ────────────────────────────────────────────────────────────


def _sentences(text: str, min_chars: int) -> list[str]:
    """拿去比对的句子：按句号、换行和没解析出来的占位符断开，太短的不要。"""
    parts = _SENTENCE_RE.split(_MARKER_RE.sub("\n", text))
    return [key for part in parts if len(key := match_key(part)) >= min_chars]


def _seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return _number(value)
    minutes, seconds = divmod(int(value), 60)
    spelled = (
        f"{minutes} 分" + (f" {seconds} 秒" if seconds else "") if minutes else f"{seconds} 秒"
    )
    return f"{int(value)} 秒（{spelled}）"


def build_super_ghost(
    ref: PageRef,
    doc: dict[str, Any],
    resolver: Resolver,
    kb_text: str,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> tuple[list[Document], dict[str, Any]]:
    cfg = settings.datasets
    year = ref.basename.removesuffix(".json")
    missions = [m for m in doc.get("missions") or [] if isinstance(m, dict)]
    counts: Counter[str] = Counter()
    blocks: list[Block] = []

    # 各星级的时间和积分系数：页面没有渲染。字段的中文名是照字面写的，所以把字段名一并留着
    levels: dict[Any, list[dict[str, Any]]] = {}
    for mission in missions:
        levels.setdefault(mission.get("star"), []).append(mission)
    items: list[str] = []
    for star, group in sorted(levels.items(), key=lambda item: -(item[0] or 0)):
        combos = {
            (m.get("battle_time"), m.get("escape_time"), m.get("score_mul")): None for m in group
        }
        for battle, escape, score in combos:
            parts = []
            if battle is not None:
                parts.append(f"战斗限时（battle_time）{_seconds(battle)}")
            if escape is not None:
                parts.append(f"逃跑时间（escape_time）{_seconds(escape)}")
            if score is not None:
                parts.append(f"积分系数（score_mul）{_number(score)}")
            if parts:
                items.append(f"- {year} 年 {star} 星超鬼王：{'，'.join(parts)}")
    if items:
        blocks.append(Block(BLOCK_HEADING, text="各星级的时间和积分系数", level=1))
        blocks.append(Block(BLOCK_PARA, text=f"{year} 年超鬼王各星级的关卡参数（来自游戏数据）："))
        blocks.append(Block(BLOCK_LIST, items=items))

    for mission in missions:
        boss = mission.get("boss") if isinstance(mission.get("boss"), dict) else {}
        name = _text(boss.get("name"))
        if not name:
            continue
        counts["bosses"] += 1
        who = f"{mission.get('star')} 星超鬼王「{name}」"
        section: list[Block] = []
        minions = [m for m in mission.get("minions") or [] if isinstance(m, dict)]
        speeds = [f"{_text(m.get('name'))} {_number(m['spd'])}" for m in minions if m.get("spd")]
        if speeds:
            counts["minions"] += len(speeds)
            section.append(Block(BLOCK_PARA, text=f"{who}的随从速度：{'、'.join(speeds)}。"))
        skills = [s for s in boss.get("skills") or [] if isinstance(s, dict) and s.get("name")]
        own = {str(s.get("id")): _text(s["name"]) for s in skills}
        for skill in skills:
            counts["skills"] += 1
            probe = Resolver(resolver.buffs, resolver.tips)
            sentences = _sentences(probe.resolve(skill.get("desc"), own), cfg.match_min_chars)
            if sentences and all(s in kb_text for s in sentences):
                counts["skills_in_kb"] += 1
                continue
            if not sentences and match_key(_text(skill["name"])) in kb_text:
                counts["skills_in_kb"] += 1  # 说明短到没法比对，技能名在库里
                continue
            counts["skills_added"] += 1
            desc = resolver.resolve(skill.get("desc"), own)
            section.append(Block(BLOCK_PARA, text=f"{who}的技能「{_text(skill['name'])}」：{desc}"))
        if section:
            blocks.append(Block(BLOCK_HEADING, text=f"{name}（{mission.get('star')}星）", level=1))
            blocks += section
    stats = dict(counts) | {"year": year}
    if not blocks:
        return [], stats
    page = f"{cfg.super_ghost_page}/{year}"
    if known_titles is not None and page not in known_titles:
        page = cfg.super_ghost_page
    document = _document(
        f"super_ghost:{year}",
        f"{cfg.super_ghost_page} {year}·数据补充",
        CATEGORY_SUPER_GHOST,
        ref,
        settings,
        blocks,
        link=page,
        known_titles=known_titles,
        year=year,
    )
    return [document], stats


# ── 汇总 ──────────────────────────────────────────────────────────────


def build_datasets(
    named: dict[str, tuple[PageRef, dict[str, Any]]],
    super_ghost: list[tuple[PageRef, dict[str, Any]]],
    kb_text: str,
    settings: YysEnv,
    known_titles: frozenset[str] | set[str] | None = None,
) -> DatasetResult:
    """``named``：数据集标题 → （定位信息，文档）；``kb_text``：条目正文的比对写法。"""
    cfg = settings.datasets
    result = DatasetResult()
    if not cfg.enabled:
        return result

    def doc_of(title: str) -> dict[str, Any] | None:
        return named[title][1] if title in named else None

    roster = Roster.build(doc_of(cfg.character_index), doc_of(cfg.skin))
    resolver = Resolver.build(doc_of(cfg.skill_buffs), doc_of(cfg.skill_tips))

    def add(category: str, built: tuple[list[Document], dict[str, Any]]) -> None:
        documents, stats = built
        result.documents += documents
        merged = result.stats.setdefault(category, {"documents": 0})
        merged["documents"] += len(documents)
        for key, value in stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[key] = merged.get(key, 0) + value
            else:
                merged[key] = value

    if cfg.skin in named:
        ref, doc = named[cfg.skin]
        add(CATEGORY_SKIN, build_skins(ref, doc, roster, settings, known_titles))
    if cfg.zzq_character in named:
        ref, doc = named[cfg.zzq_character]
        add(
            CATEGORY_ZZQ_CHARACTER,
            build_zzq_characters(ref, doc, resolver, settings, known_titles),
        )
    if cfg.zzq_fuzhou in named:
        ref, doc = named[cfg.zzq_fuzhou]
        add(CATEGORY_ZZQ_FUZHOU, build_zzq_fuzhou(ref, doc, resolver, settings, known_titles))
    if cfg.zzq_soul in named:
        ref, doc = named[cfg.zzq_soul]
        add(CATEGORY_ZZQ_SOUL, build_zzq_souls(ref, doc, resolver, settings, known_titles))
    if cfg.month_illustration in named:
        ref, doc = named[cfg.month_illustration]
        add(CATEGORY_DIALOGUE, build_month_dialogues(ref, doc, roster, settings, known_titles))
    if cfg.skill_buffs in named:
        ref, doc = named[cfg.skill_buffs]
        add(CATEGORY_BUFF, build_buffs(ref, doc, resolver, kb_text, settings, known_titles))
    for ref, doc in sorted(super_ghost, key=lambda item: item[0].title):
        add(
            CATEGORY_SUPER_GHOST,
            build_super_ghost(ref, doc, resolver, kb_text, settings, known_titles),
        )
    result.unresolved = resolver.unresolved
    return result
