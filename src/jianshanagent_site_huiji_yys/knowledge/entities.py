"""给切片标实体：字典匹配，不用模型。

字典匹配只负责「找出可能的提法」。正式名命中是**确定**的（``certain: true``）；昵称命中一律保留，
但只算**候选**（``certain: false``，``via`` 写命中的昵称）——昵称是不是真的在指那个实体，
留给后面的检索去判断（重排序模型会读着问题和原文来排）。所以这里没有按字数的门槛，
也没有按具体词写的排除名单。

字典全部来自 wiki 数据：

- 式神：``Data:CharacterIndex.json`` 的 ``characters``（``name`` 正式名、``rarity`` 稀有度、
  ``nickname`` 昵称，昵称可能是字符串、数组或没有）；
- 御魂：``Data:Soul.json`` 的 ``soul``（``name``；条目里有 ``nickname`` 的话同样当昵称用）；
- 阴阳师：``Data:Skin.json`` 里有、式神索引里没有的角色（晴明、神乐、八百比丘尼、源博雅、
  源赖光、藤原道长），数据里找不到时才用配置里的短名单；``Data:Skin.json`` 里那个角色的条目
  有 ``nickname`` 的话同样当昵称用。

正式名（确定）的规则：

- 长名优先：从左到右扫，同一位置取最长的名字，已经被长名字占用的字不再匹配短名字
  （「大天狗·高天铁羽」不会再标出「大天狗」）。带「·」的名字去掉「·」的写法也认。
- 一个字的正式名（泷、季、荒、觉……）和配置里列出的撞词名字（言和、山风、珍珠……）走严格规则，
  满足其一才标：这一片就在它自己的页面里；前后都不是汉字 / 假名 / 字母 / 数字（表格单元格、
  顿号隔开的名单、引号里）；紧邻「御魂」这类上下文词。
- 御魂名同时是别的东西的，不硬判，标中性类型；只有上下文明确在说御魂时才标「御魂」：
  - 和妖怪同名（荒骷髅以外的：飞缘魔、胧车、镇墓兽、土蜘蛛……）→「御魂/妖怪」。
    名单从数据里来：这个名字在剧情里当过说话人，或者是超鬼王的单位、百鬼棋局的棋子；
    数据认不出来的可以在配置里补（``monster_souls``）。
  - 和道具同名（天羽羽斩、月之石、八咫镜，配置里的 ``item_souls``）→「御魂/道具」。
  - 同时在式神索引里（荒骷髅，2025 年起也是 SSR 式神）→「式神/御魂」；在它自己的式神页里标「式神」。
  「明确在说御魂」指：名字前后 ``ambiguous_window`` 个字以内有 ``context_words``（御魂、件套、套装，
  比如「胧车两件套」「御魂「飞缘魔」」；隔得远的不算，「御魂自选次数对胧车同样生效」说的是玩法）；
  或者所在的小节标题 / 页面名带「御魂」（御魂搭配、御魂检索器）；
  或者旁边紧挨着另一个不会认错的御魂名（「破势/荒骷髅」）。
  同一片里既有明确的又有不明确的提法时，只留明确的类型。
- ``shadow_words``：包含实体名、但说的不是它的更长的词（「黑晴明」「神乐铃」），
  整个词占住位置，不标。

昵称（候选）的规则，只有结构上的两条，不看具体是哪个词：

- 一个字的昵称（象、灿、骨、杯……）：前后都不是汉字 / 假名 / 字母 / 数字，
  或者这一片就在它指向的实体自己的页面里，才出候选。
- 字母 / 数字开头或结尾的昵称（018、0618、sp凤、dst）：那一头不能紧挨着别的字母或数字
  （「2018」里不匹配出「018」）。带字母的昵称不分大小写。

候选不参与别的判断，也不占位：

- 正式名先匹配。候选只在没被正式名（和占位词）占用的字上匹配，候选之间长的优先。
  没通过严格规则的正式名（「神荒」里的「荒」）不算占用。
- 唯一的例外还是「长的写法优先」：昵称把一个正式名整个包在里面时（「UR大天狗」里的「大天狗」），
  文中写的是更长、更具体的那个，里面的正式名不标，按昵称出候选（大天狗·高天铁羽）。
  要是文中的正式名比昵称还长（「UR大天狗·高天铁羽」），照常标正式名。
- 候选不能当作「旁边紧挨着另一个不会认错的御魂名」的证据。
- 同一片里某个实体已经靠正式名确定了，就不再出它的候选。
- 一个昵称指向不止一个实体：每个实体各出一项候选。
- 昵称和某个正式名相同（「初音未来」既是联动式神的正式名，又是丑时之女的昵称）：
  正式名那个实体照常标确定，昵称指向的实体另出一项候选。
- 候选的类型和正式名一样定（中性名字按上下文判）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from jianshanagent_site_huiji_yys.knowledge.settings import EntitySettings

TYPE_SHIKIGAMI = "式神"
TYPE_SOUL = "御魂"
TYPE_ONMYOJI = "阴阳师"
TYPE_SOUL_OR_MONSTER = "御魂/妖怪"
TYPE_SOUL_OR_ITEM = "御魂/道具"
TYPE_SHIKIGAMI_OR_SOUL = "式神/御魂"
NEUTRAL_TYPES = (TYPE_SOUL_OR_MONSTER, TYPE_SOUL_OR_ITEM, TYPE_SHIKIGAMI_OR_SOUL)
ONMYOJI_FROM_DATA = "data"
ONMYOJI_FROM_CONFIG = "config"

_DOTS = "·・•‧"
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]")
#: 「字」：汉字、假名、字母、数字。严格规则要求名字前后都不是它
_WORD_CHAR_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff々A-Za-z0-9]")
_HEADING_LINE_RE = re.compile(r"^#{1,6} .*$", re.MULTILINE)
#: 找昵称时，被正式名占用的字换成它（任何昵称里都不会有）
_MASK = "\x00"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def nickname_map(entries: Iterable[Any]) -> dict[str, list[str]]:
    """数据条目（``{"name": …, "nickname": 字符串 / 数组 / 没有}``）→ 名字 → 昵称列表。"""
    result: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        nicknames = [n for raw in _as_list(entry.get("nickname")) if (n := raw.strip())]
        if name and nicknames:
            result.setdefault(name, []).extend(nicknames)
    return result


@dataclass(frozen=True, slots=True)
class Hit:
    name: str
    type: str
    #: 确定的命中：正文里实际出现的写法；候选：命中的昵称（数据里的写法）
    surface: str
    start: int
    #: 正式名命中是确定的；昵称命中只是候选
    certain: bool


@dataclass(slots=True)
class Lexicon:
    #: 正式写法（小写）→ [（正式名，类型）]；占位词的列表是空的
    terms: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    strict: set[str] = field(default_factory=set)
    #: 昵称（小写）→ [（它指向的正式名，类型）]，可以不止一个
    aliases: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: 昵称（小写）→ 数据里的写法
    alias_spelling: dict[str, str] = field(default_factory=dict)
    rarity: dict[str, str] = field(default_factory=dict)
    context_words: tuple[str, ...] = ()
    context_window: int = 12
    ambiguous_window: int = 4
    #: 御魂名同时是别的东西的：正式名 → 没有御魂语境时的中性类型
    ambiguous: dict[str, str] = field(default_factory=dict)
    #: 中性类型的依据：正式名 → 说明（报告用）
    ambiguous_basis: dict[str, str] = field(default_factory=dict)
    soul_section_words: tuple[str, ...] = ()
    onmyoji: list[str] = field(default_factory=list)
    #: 阴阳师名单的来源：data（皮肤数据）/ config（配置里的短名单）/ 空（没有）
    onmyoji_source: str = ""
    shadow_words: frozenset[str] = frozenset()
    _pattern: re.Pattern[str] | None = None
    _alias_pattern: re.Pattern[str] | None = None
    #: 包着正式名的昵称的匹配式；False = 还没算，None = 没有这样的昵称
    _enclosing_pattern: re.Pattern[str] | None | bool = False

    @classmethod
    def build(
        cls,
        character_index: dict[str, Any] | None,
        soul: dict[str, Any] | None,
        cfg: EntitySettings,
        *,
        onmyoji: Iterable[str] = (),
        monsters: dict[str, str] | None = None,
        onmyoji_nicknames: dict[str, list[str]] | None = None,
    ) -> Lexicon:
        """``onmyoji``：数据里找到的阴阳师名单；``monsters``：数据里出现过的角色名 → 出处
        （剧情说话人、超鬼王单位、百鬼棋局棋子），用来认出和妖怪同名的御魂；
        ``onmyoji_nicknames``：数据里阴阳师的昵称（名字 → 昵称列表，见 :func:`nickname_map`）。"""
        lexicon = cls(
            context_words=cfg.context_words,
            context_window=cfg.context_window,
            ambiguous_window=cfg.ambiguous_window,
            soul_section_words=cfg.soul_section_words,
        )
        characters = [
            c for c in (character_index or {}).get("characters") or [] if isinstance(c, dict)
        ]
        souls = [s for s in (soul or {}).get("soul") or [] if isinstance(s, dict)]
        official: list[tuple[str, str]] = []
        for character in characters:
            name = str(character.get("name") or "").strip()
            if name:
                official.append((name, TYPE_SHIKIGAMI))
                lexicon.rarity[name] = str(character.get("rarity") or "")
        shikigami = {name for name, _ in official}
        soul_names = [n for s in souls if (n := str(s.get("name") or "").strip())]
        for name in soul_names:
            if name in shikigami:
                lexicon.ambiguous[name] = TYPE_SHIKIGAMI_OR_SOUL
                lexicon.ambiguous_basis[name] = "同时在式神索引里"
            elif name in cfg.item_souls:
                lexicon.ambiguous[name] = TYPE_SOUL_OR_ITEM
                lexicon.ambiguous_basis[name] = "配置 item_souls"
            elif name in (monsters or {}):
                lexicon.ambiguous[name] = TYPE_SOUL_OR_MONSTER
                lexicon.ambiguous_basis[name] = (monsters or {})[name]
            elif name in cfg.monster_souls:
                lexicon.ambiguous[name] = TYPE_SOUL_OR_MONSTER
                lexicon.ambiguous_basis[name] = "配置 monster_souls"
        # 同时是式神的御魂名只登记一次（类型在匹配时按上下文定）
        official += [(n, TYPE_SOUL) for n in soul_names if n not in shikigami]
        taken = {name for name, _ in official}
        found = [n for n in dict.fromkeys(str(n).strip() for n in onmyoji) if n]
        fallback = [n for n in cfg.onmyoji_fallback if n]
        lexicon.onmyoji_source = (
            ONMYOJI_FROM_DATA if found else ONMYOJI_FROM_CONFIG if fallback else ""
        )
        lexicon.onmyoji = [n for n in (found or fallback) if n not in taken]
        official += [(n, TYPE_ONMYOJI) for n in lexicon.onmyoji]

        for name, kind in official:
            lexicon._add(name, name, kind)
            plain = name.translate({ord(dot): None for dot in _DOTS})
            if plain != name:
                lexicon._add(plain, name, kind)
            if len(name) == 1 or name in cfg.strict_names:
                lexicon.strict.add(name.lower())

        # 昵称只来自数据，一律收进来当候选：式神索引、御魂数据、皮肤数据里阴阳师的条目
        kinds = dict(official)
        sources = [nickname_map(characters), nickname_map(souls)]
        sources.append({n: v for n, v in (onmyoji_nicknames or {}).items() if n in lexicon.onmyoji})
        for source in sources:
            for name, nicknames in source.items():
                for nickname in nicknames:
                    lexicon._add_alias(nickname, name, kinds.get(name, ""))
        # 占位的词：只占住位置，不产生实体。和某个实体写法相同的不算
        lexicon.shadow_words = frozenset(
            w.lower() for w in cfg.shadow_words if w and w.lower() not in lexicon.terms
        )
        for word in lexicon.shadow_words:
            lexicon.terms[word] = []
        lexicon._pattern = None
        lexicon._alias_pattern = None
        lexicon._enclosing_pattern = False
        return lexicon

    def _add_alias(self, nickname: str, name: str, kind: str) -> None:
        key = nickname.lower()
        if not key or not kind or any(n == name for n, _ in self.terms.get(key, ())):
            return  # 就是它自己的正式写法
        entry = self.aliases.setdefault(key, [])
        if (name, kind) not in entry:
            entry.append((name, kind))
        self.alias_spelling.setdefault(key, nickname)
        self._alias_pattern = None
        self._enclosing_pattern = False

    def alias_summary(self) -> dict[str, Any]:
        """字典里昵称的概况（报告用）。只按结构数，不看具体是哪个词。"""
        by_type = Counter(kind for targets in self.aliases.values() for _, kind in targets)
        return {
            "total": len(self.aliases),
            "pairs": sum(len(targets) for targets in self.aliases.values()),
            "by_type": dict(by_type),
            "single_char": sum(1 for a in self.aliases if len(a) == 1),
            "ascii_edge": sum(
                1 for a in self.aliases if _ASCII_WORD_RE.match(a[0]) or _ASCII_WORD_RE.match(a[-1])
            ),
            "multi_target": sum(1 for targets in self.aliases.values() if len(targets) > 1),
            "same_as_official": sorted(
                self.alias_spelling[a] for a in self.aliases if self.terms.get(a)
            ),
            # 里面包着一个正式名的昵称（「UR大天狗」）：文中出现时按昵称出候选，里面的正式名不标
            "enclosing_official": sorted(self.alias_spelling[a] for a in self._enclosing_aliases()),
            # 自查：昵称单独出现时也出不了候选的。按现在的规则应该是空的
            "blocked_by_official": sorted(
                spelling
                for spelling in self.alias_spelling.values()
                if not any(not h.certain and h.surface == spelling for h in self.find(spelling))
            ),
        }

    def _add(self, surface: str, name: str, kind: str) -> None:
        entry = self.terms.setdefault(surface.lower(), [])
        if (name, kind) not in entry:
            entry.append((name, kind))
        self._pattern = None
        self._enclosing_pattern = False

    @property
    def pattern(self) -> re.Pattern[str] | None:
        if self._pattern is None and self.terms:
            ordered = sorted(self.terms, key=lambda t: (-len(t), t))
            self._pattern = re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)
        return self._pattern

    @property
    def alias_pattern(self) -> re.Pattern[str] | None:
        if self._alias_pattern is None and self.aliases:
            ordered = sorted(self.aliases, key=lambda t: (-len(t), t))
            self._alias_pattern = re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)
        return self._alias_pattern

    def _enclosing_aliases(self) -> list[str]:
        """里面整个包着某个正式写法的昵称（「ur大天狗」包着「大天狗」），长的在前。"""
        official = [term for term, targets in self.terms.items() if targets]
        return sorted(
            (a for a in self.aliases if any(term != a and term in a for term in official)),
            key=lambda a: (-len(a), a),
        )

    def _enclosing_spans(self, text: str, title: str) -> list[tuple[int, int]]:
        """文中这类昵称出现的位置：落在里面的正式名不标，留给昵称出候选。"""
        if self._enclosing_pattern is False:
            enclosing = self._enclosing_aliases()
            self._enclosing_pattern = (
                re.compile("|".join(re.escape(a) for a in enclosing), re.IGNORECASE)
                if enclosing
                else None
            )
        pattern = self._enclosing_pattern
        if pattern is None:
            return []
        return [
            match.span()
            for match in pattern.finditer(text)
            if self._alias_targets(text, *match.span(), match.group(0).lower(), title)
        ]

    @staticmethod
    def _ascii_glued(text: str, start: int, end: int, surface: str) -> bool:
        """字母 / 数字那一头紧挨着别的字母、数字（英文词里的「dst」，「2018」里的「018」）。"""
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if _ASCII_WORD_RE.match(surface[0]) and before and _ASCII_WORD_RE.match(before):
            return True
        return bool(_ASCII_WORD_RE.match(surface[-1]) and after and _ASCII_WORD_RE.match(after))

    @staticmethod
    def _isolated(text: str, start: int, end: int) -> bool:
        """前后都不是字（汉字 / 假名 / 字母 / 数字）。"""
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        return not (before and _WORD_CHAR_RE.match(before)) and not (
            after and _WORD_CHAR_RE.match(after)
        )

    def _accept(
        self,
        text: str,
        start: int,
        end: int,
        surface: str,
        title: str,
        confirmed: frozenset[str] = frozenset(),
    ) -> bool:
        if self._ascii_glued(text, start, end, surface):
            return False
        if surface not in self.strict:
            return True
        names = {name for name, _ in self.terms[surface]}
        if any(title == name or title.startswith(f"{name}/") for name in names):
            return True
        if len(surface) > 1 and names & confirmed:
            return True  # 同一篇文档别处已经确认说的是它（一个字的名字不走这条，太容易撞词）
        if self._isolated(text, start, end):
            return True
        if not any(kind == TYPE_SOUL for _, kind in self.terms[surface]):
            return False  # 「紧邻御魂」只对御魂名有意义（「觉醒、御魂副本」里的「觉」不是式神）
        return self._near_context_word(text, start, end)

    def _alias_targets(
        self, text: str, start: int, end: int, surface: str, title: str
    ) -> list[tuple[str, str]]:
        """这个位置上的昵称可以给哪些实体出候选。只有结构上的两条规则。"""
        if self._ascii_glued(text, start, end, surface):
            return []
        targets = self.aliases[surface]
        if len(surface) > 1 or self._isolated(text, start, end):
            return targets
        # 一个字的昵称：不是孤立出现的，只在它指向的实体自己的页面里算
        return [(n, k) for n, k in targets if title == n or title.startswith(f"{n}/")]

    def _near_context_word(self, text: str, start: int, end: int, width: int | None = None) -> bool:
        width = self.context_window if width is None else width
        window = text[max(0, start - width) : end + width]
        return any(word in window for word in self.context_words)

    def _in_soul_section(self, text: str, start: int) -> bool:
        """名字所在的小节在讲御魂：切片首行（页面名 > 章节路径）或前面最近的小节标题带「御魂」。"""
        if not self.soul_section_words:
            return False
        lines = [text.split("\n", 1)[0]]
        headings = _HEADING_LINE_RE.findall(text[:start])
        if headings:
            lines.append(headings[-1])
        return any(word in line for line in lines for word in self.soul_section_words)

    def _resolve_type(
        self,
        name: str,
        text: str,
        start: int,
        end: int,
        title: str,
        soul_spans: list[tuple[int, int]],
    ) -> str:
        """中性名字在这个位置的类型。``soul_spans``：这一片里不会认错的御魂名的位置。"""
        neutral = self.ambiguous[name]
        # 中性名字要求更近：「胧车探索时，御魂及觉醒材料……」「御魂自选次数对胧车同样生效」
        # 说的是胧车这个玩法，十来个字以内有「御魂」不算明确
        near = self._near_context_word(text, start, end, self.ambiguous_window)
        if near or self._in_soul_section(text, start):
            return TYPE_SOUL
        window = self.context_window
        if any(s < end + window and e > start - window for s, e in soul_spans):
            return TYPE_SOUL
        own_page = title == name or title.startswith(f"{name}/")
        if neutral == TYPE_SHIKIGAMI_OR_SOUL and own_page:
            return TYPE_SHIKIGAMI
        return neutral

    def confirmed(self, texts: Iterable[str], title: str = "") -> frozenset[str]:
        """同一篇文档的各个切片里，靠严格规则确认过的实体名。

        撞词的名字（「山风」）在对白行「山风：……」里能确认，在叙述句「山风盯着篝火」里确认不了；
        同一篇文档里只要确认过一次，其余切片里的同名写法也算它。
        """
        pattern = self.pattern
        if pattern is None or not self.strict:
            return frozenset()
        names: set[str] = set()
        for text in texts:
            for match in pattern.finditer(text):
                surface = match.group(0).lower()
                if (
                    surface in self.strict
                    and len(surface) > 1
                    and self._accept(text, match.start(), match.end(), surface, title)
                ):
                    names.update(name for name, _ in self.terms[surface])
        return frozenset(names)

    def find(
        self, text: str, title: str = "", confirmed: frozenset[str] = frozenset()
    ) -> list[Hit]:
        """正文里命中的实体：先是确定的（正式名），再是候选（昵称），各按出现位置排列
        （同一个名字出现多次会有多条）。

        ``confirmed``：同一篇文档别处确认过的实体名（见 :meth:`confirmed`）。
        """
        accepted: list[re.Match[str]] = []
        occupied: list[tuple[int, int]] = []
        pattern = self.pattern
        enclosing = self._enclosing_spans(text, title)
        for match in pattern.finditer(text) if pattern is not None else ():
            surface = match.group(0).lower()
            start, end = match.span()
            if any(s <= start and end <= e and (s, e) != (start, end) for s, e in enclosing):
                continue  # 被更长的昵称整个包住（「UR大天狗」里的「大天狗」）：按昵称出候选
            if not self.terms[surface]:
                occupied.append(match.span())  # 占位的词（黑晴明）没有实体，只占住位置
            elif self._accept(text, match.start(), match.end(), surface, title, confirmed):
                accepted.append(match)
                occupied.append(match.span())
        # 只有确定的命中才能当「旁边有个不会认错的御魂名」的证据
        soul_spans = [
            (match.start(), match.end())
            for match in accepted
            if any(
                kind == TYPE_SOUL and name not in self.ambiguous
                for name, kind in self.terms[match.group(0).lower()]
            )
        ]

        def typed(name: str, kind: str, start: int, end: int) -> str:
            if name not in self.ambiguous:
                return kind
            return self._resolve_type(name, text, start, end, title, soul_spans)

        certain: list[Hit] = []
        candidates: list[Hit] = []
        for match in accepted:
            surface = match.group(0).lower()
            start, end = match.span()
            names = dict.fromkeys(name for name, _ in self.terms[surface])
            for name, kind in dict(self.terms[surface]).items():
                certain.append(
                    Hit(name, typed(name, kind, start, end), match.group(0), start, True)
                )
            # 同一个写法还是别的实体的昵称：那个实体另出一项候选
            if surface in self.aliases:
                for name, kind in self._alias_targets(text, start, end, surface, title):
                    if name not in names:
                        nickname = self.alias_spelling[surface]
                        candidates.append(
                            Hit(name, typed(name, kind, start, end), nickname, start, False)
                        )
        candidates += self._find_aliases(text, title, occupied, typed)
        candidates.sort(key=lambda hit: hit.start)

        # 同一片里对同一个名字既有明确的判断又有中性的：只留明确的。候选不影响确定的那一级
        def settle(hits: list[Hit], evidence: list[Hit]) -> list[Hit]:
            decided = {
                h.name for h in evidence if h.name in self.ambiguous and h.type not in NEUTRAL_TYPES
            }
            return [h for h in hits if not (h.name in decided and h.type in NEUTRAL_TYPES)]

        certain = settle(certain, certain)
        # 已经靠正式名确定的实体不再出候选
        known = {h.name for h in certain}
        candidates = [h for h in candidates if h.name not in known]
        return certain + settle(candidates, candidates)

    def _find_aliases(
        self,
        text: str,
        title: str,
        occupied: list[tuple[int, int]],
        typed: Callable[[str, str, int, int], str],
    ) -> list[Hit]:
        """昵称命中：只在没被正式名占用的字上匹配，长的优先。"""
        pattern = self.alias_pattern
        if pattern is None:
            return []
        masked = text
        if occupied:
            chars = list(text)
            for start, end in occupied:
                chars[start:end] = _MASK * (end - start)
            masked = "".join(chars)
        hits: list[Hit] = []
        position = 0
        while (match := pattern.search(masked, position)) is not None:
            surface = match.group(0).lower()
            start, end = match.span()
            targets = self._alias_targets(text, start, end, surface, title)
            if not targets:
                position = start + 1  # 这个位置不算，后面重叠的位置上可能还有别的昵称
                continue
            position = end
            for name, kind in targets:
                nickname = self.alias_spelling[surface]
                hits.append(Hit(name, typed(name, kind, start, end), nickname, start, False))
        return hits

    @staticmethod
    def entities(hits: Iterable[Hit]) -> list[dict[str, Any]]:
        """切片的 ``entities`` 字段：先是确定的，再是候选，各按首次出现的顺序，不重复。

        确定的：``{"name": 正式名, "type": 类型, "certain": true}``；
        候选：``{"name": 正式名, "type": 类型, "certain": false, "via": 命中的昵称}``。
        同一片里同一个实体靠不止一个昵称命中时，``via`` 只写最先出现的那个。
        """
        seen: dict[tuple[str, str, bool], dict[str, Any]] = {}
        for hit in hits:
            key = (hit.name, hit.type, hit.certain)
            if key not in seen:
                seen[key] = {"name": hit.name, "type": hit.type, "certain": hit.certain}
                if not hit.certain:
                    seen[key]["via"] = hit.surface
        return sorted(seen.values(), key=lambda entity: not entity["certain"])

    def tag(
        self, text: str, title: str = "", confirmed: frozenset[str] = frozenset()
    ) -> list[dict[str, Any]]:
        return self.entities(self.find(text, title, confirmed))


def entity_stats(tagged: Iterable[tuple[str, list[Hit]]]) -> dict[str, Any]:
    """汇总标注结果：``tagged`` 是（切片类型，这一片的命中）。

    切片分三种：有确定标签的、只有候选标签的、两者都没有的。实体的统计只数确定的；
    候选按昵称另外统计。
    """
    total = 0
    levels: Counter[str] = Counter()
    by_kind: dict[str, Counter[str]] = {}
    chunk_counts: Counter[tuple[str, str]] = Counter()
    candidate_chunks = 0
    candidate_pairs = 0
    candidate_entities: set[str] = set()
    by_nickname: Counter[str] = Counter()
    nickname_targets: dict[str, dict[str, None]] = {}
    for kind, hits in tagged:
        total += 1
        certain = {(h.name, h.type) for h in hits if h.certain}
        candidates = [h for h in hits if not h.certain]
        level = "certain" if certain else "candidate_only" if candidates else "none"
        levels[level] += 1
        by_kind.setdefault(kind, Counter())[level] += 1
        chunk_counts.update(certain)
        if candidates:
            candidate_chunks += 1
            candidate_pairs += len({(h.name, h.type) for h in candidates})
            candidate_entities.update(h.name for h in candidates)
            by_nickname.update({h.surface for h in candidates})
            for hit in candidates:
                nickname_targets.setdefault(hit.surface, {}).setdefault(hit.name)
    return {
        "chunks": total,
        "chunks_certain": levels["certain"],
        "chunks_candidate_only": levels["candidate_only"],
        "chunks_none": levels["none"],
        #: 切片类型 → [有确定标签的，只有候选的，都没有的]
        "by_type": {k: [c["certain"], c["candidate_only"], c["none"]] for k, c in by_kind.items()},
        "distinct_entities": len({name for name, _ in chunk_counts}),
        #: 类型 → 带这个类型的（实体，切片）对数
        "by_entity_type": dict(
            Counter(kind for (_, kind), n in chunk_counts.items() for _ in range(n))
        ),
        #: 每个（实体，类型）出现在多少个切片里
        "chunk_counts": [[name, kind, count] for (name, kind), count in chunk_counts.most_common()],
        "top": [[name, kind, count] for (name, kind), count in chunk_counts.most_common(20)],
        "candidates": {
            #: 带候选的切片（不管有没有确定标签）
            "chunks": candidate_chunks,
            #: （候选实体，切片）对数
            "pairs": candidate_pairs,
            "distinct_entities": len(candidate_entities),
            #: 每个昵称出了候选的切片数：[昵称, [指向的实体], 切片数]
            "by_nickname": [
                [nickname, list(nickname_targets[nickname]), count]
                for nickname, count in by_nickname.most_common()
            ],
        },
    }
