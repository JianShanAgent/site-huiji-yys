"""阴阳师 wiki 站点插件里管查询的那一摊：式神、御魂，和按稀有度、标签筛式神。

通用的查询只认页面。这个站的式神页面源码只有一行模板调用、SMW 和导言摘要都是空的，
真正的内容在灰机的 Data 命名空间里（经 ``huiji`` 插件读）：

- ``Data:CharacterIndex.json`` 是式神索引：稀有度、功能标签、昵称以它为准
  （式神文档自身的 grade 有一百多条是占位值 "0"）。``/查 高铁`` 认昵称靠的就是它。
- ``Data:Character/<编号>.json`` 是一个式神的详细数据（CV、简介、技能）。阴阳师、英杰等不在索引里，
  按名字到 Data 里找。技能描述里的 ``[tips_N]``、``[buff_N_L]``、``[skill_N_L]`` 是占位符，
  换成对应的名字（``Data:SkillTips.json``、``Data:SkillBuffs.json``、同一个式神的技能表）。
- ``Data:Soul.json`` 是御魂表。御魂没有自己的页面，链接指向配置里的 ``soul_page``。

接进通用查询的地方（接口见 ``plugins/wiki_query/provider.py``）：``/查`` 和 wiki_search 先在这里找
式神、御魂（正式名、昵称）；查证前查别名（:meth:`YysProvider.lookup_name`）；
另加 ``/筛选`` 命令和 wiki_filter 工具（:meth:`YysProvider.ask`）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jianshanagent.core.logging import get_logger
from jianshanagent.core.messages import InboundMessage, OutboundMessage
from jianshanagent.plugins.wiki_query.client import (
    NameMatch,
    PageContent,
    SearchHit,
    WikiError,
    WikiQueryClient,
)
from jianshanagent.plugins.wiki_query.formatters import bold, clip, finish
from jianshanagent.plugins.wiki_query.provider import (
    ProviderContext,
    SiteSetup,
    WikiProvider,
)
from jianshanagent_site_huiji.wiki import HuijiData

logger = get_logger(__name__)

KIND_CHARACTER = "character"
KIND_SOUL = "soul"
KIND_LABELS = {KIND_CHARACTER: "式神", KIND_SOUL: "御魂"}

RARITIES = ("UR", "SP", "SSR", "SR", "R", "N")

_INDEX_ID = "Data:CharacterIndex.json"
_SOUL_ID = "Data:Soul.json"
_TIPS_ID = "Data:SkillTips.json"
_BUFFS_ID = "Data:SkillBuffs.json"

_PLACEHOLDER_RE = re.compile(r"\[(tips|buff|skill)_([0-9_]+)\]")
_NESTED_QUOTE_RE = re.compile(r"『「([^」]*)」』")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_INTRO_CHARS = 120


@dataclass(slots=True)
class AskResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: 符合条件的总数（rows 可能被 limit 截断）
    total: int = 0
    #: 索引里不存在的标签；非空时 rows 一定为空
    unknown_tags: list[str] = field(default_factory=list)
    #: 仅在有未知标签时给出全部可用标签，方便调用方改用正确的词
    known_tags: list[str] = field(default_factory=list)


def _as_list(value: Any) -> list[str]:
    """索引里的昵称有时是字符串、有时是数组、有时缺失。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if v]


def _split_tags(value: Any) -> list[str]:
    return [t.strip() for t in str(value or "").split(",") if t.strip()]


class _CharacterIndex:
    def __init__(self, entries: Sequence[dict[str, Any]]):
        self.entries = [e for e in entries if e.get("name")]
        self._by_name = {str(e["name"]): e for e in self.entries}
        self._by_alias: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            for nick in _as_list(entry.get("nickname")):
                self._by_alias.setdefault(nick.casefold(), entry)
        self.all_tags = {t for e in self.entries for t in self.tags_of(e)}

    @staticmethod
    def tags_of(entry: dict[str, Any]) -> set[str]:
        tags = {str(t) for t in entry.get("tags") or []}
        for value in (entry.get("extraTags") or {}).values():
            tags.update(_split_tags(value))
        return tags

    def get(self, name: str) -> dict[str, Any] | None:
        return self._by_name.get(name)

    def alias(self, folded: str) -> dict[str, Any] | None:
        """按登记的别名找（``folded`` 是已经 casefold 过的）。"""
        return self._by_alias.get(folded)

    def match(self, query: str) -> list[SearchHit]:
        """正式名完全一致 > 昵称完全一致 > 名字或昵称包含查询词。"""
        folded = query.casefold()
        exact = self._by_name.get(query) or self._by_alias.get(folded)
        hits = [SearchHit(str(exact["name"]), KIND_CHARACTER, exact=True)] if exact else []
        partial = [
            entry
            for entry in self.entries
            if entry is not exact
            and (
                folded in str(entry["name"]).casefold()
                or any(folded in n.casefold() for n in _as_list(entry.get("nickname")))
            )
        ]
        partial.sort(key=lambda e: len(str(e["name"])))
        hits.extend(SearchHit(str(e["name"]), KIND_CHARACTER) for e in partial)
        return hits


class YysProvider(WikiProvider):
    id = "yys"
    kinds = KIND_LABELS
    search_hint = "支持式神正式名、玩家昵称（如「高铁」）、御魂名、页面标题。"
    get_hint = "式神返回稀有度、CV、简介、技能（含数值）；御魂返回属性与效果；"

    def __init__(
        self,
        data: HuijiData,
        *,
        soul_page: str = "御魂检索器",
        filter_command: dict[str, Any] | None = None,
    ):
        self.data = data
        #: 御魂没有自己的页面，链接指向这一页；空 = 不给链接
        self.soul_page = soul_page
        #: ``/筛选`` 命令叫什么（``name`` / ``aliases`` / ``description``）
        self.filter_command = dict(filter_command or {})
        self._client: WikiQueryClient | None = None

    def bind(self, client: WikiQueryClient) -> None:
        self._client = client

    @property
    def client(self) -> WikiQueryClient:
        if self._client is None:
            raise RuntimeError("阴阳师插件还没有接到查询客户端上")
        return self._client

    # ── 站点数据集（解析后的对象也缓存，避免每次重建）─────────────────

    async def _index(self) -> _CharacterIndex:
        cached = self.client.recall("index")
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        doc = await self.data.doc(_INDEX_ID)
        if doc is None:
            raise WikiError(f"wiki 上找不到式神索引 {_INDEX_ID}")
        index = _CharacterIndex(doc.get("characters") or [])
        self.client.remember("index", index)
        return index

    async def _souls(self) -> list[dict[str, Any]]:
        doc = await self.data.doc(_SOUL_ID)
        return list((doc or {}).get("soul") or [])

    async def _glossary(self) -> tuple[dict[str, str], dict[str, str]]:
        """全局的「技能说明」「状态」名称表，供解析技能描述里的占位符。"""
        cached = self.client.recall("glossary")
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        found = await self.data.find({"_id": {"$in": [_TIPS_ID, _BUFFS_ID]}})
        docs = {d["_id"]: d for d in found}
        tips = {
            str(key): str(value.get("skill_tips_name") or "")
            for key, value in ((docs.get(_TIPS_ID) or {}).get("tips") or {}).items()
        }
        buffs = {
            str(key): str(value.get("name") or "")
            for key, value in ((docs.get(_BUFFS_ID) or {}).get("buff") or {}).items()
        }
        self.client.remember("glossary", (tips, buffs))
        return tips, buffs

    # ── 接进通用查询的方法 ────────────────────────────────────────────

    async def search(self, query: str) -> list[SearchHit]:
        """式神（正式名、昵称）在前，御魂在后；都没有时再看是不是索引以外的角色。"""
        hits = (await self._index()).match(query)

        folded = query.casefold()
        souls = [str(s.get("name") or "") for s in await self._souls()]
        exact_souls = [n for n in souls if n.casefold() == folded]
        partial_souls = [n for n in souls if n and folded in n.casefold() and n not in exact_souls]
        hits += [SearchHit(n, KIND_SOUL, exact=True) for n in exact_souls]
        hits += [SearchHit(n, KIND_SOUL) for n in partial_souls]

        if not hits and await self._unindexed_character(query) is not None:
            # 阴阳师、英杰等不在式神索引里，但有数据文档
            hits.append(SearchHit(query, KIND_CHARACTER, exact=True))
        return hits

    async def lookup_name(self, name: str) -> list[NameMatch]:
        """式神索引和御魂表里的正式名、登记的别名（完全一致）。"""
        folded = name.casefold()
        index = await self._index()
        found: list[NameMatch] = []
        entry = index.get(name) or index.alias(folded)
        if entry is not None:
            aliases = tuple(_as_list(entry.get("nickname")))
            found.append(NameMatch(str(entry["name"]), KIND_CHARACTER, aliases))
        for soul in await self._souls():
            soul_name = str(soul.get("name") or "")
            aliases = tuple(_as_list(soul.get("nickname")))
            if soul_name and folded in {soul_name.casefold(), *(a.casefold() for a in aliases)}:
                found.append(NameMatch(soul_name, KIND_SOUL, aliases))
        return found

    async def name_candidates(self, name: str, limit: int) -> list[NameMatch]:
        """名字或昵称里包含它的式神。"""
        partial = [hit for hit in (await self._index()).match(name) if not hit.exact]
        return [NameMatch(hit.title, hit.kind, exact=False) for hit in partial[: max(limit, 0)]]

    async def _unindexed_character(self, name: str) -> dict[str, Any] | None:
        docs = await self.data.find({"_hjschema": "Character", "name.cn": name})
        # 库里有游离的重复文档（如 Data:603.json），优先取规范路径下的那份
        docs.sort(key=lambda d: not str(d.get("_id", "")).startswith("Data:Character/"))
        return docs[0] if docs else None

    async def get_page(self, title: str, kind: str | None) -> PageContent | None:
        """``kind`` 不给时先当式神找、再当御魂找。"""
        if kind in (None, KIND_CHARACTER):
            page = await self._character_page(title)
            if page is not None or kind == KIND_CHARACTER:
                return page
        if kind in (None, KIND_SOUL):
            return await self._soul_page(title)
        return None

    async def ask(
        self,
        *,
        rarity: Iterable[str] = (),
        tags: Iterable[str] = (),
        limit: int = 50,
    ) -> AskResult:
        """筛选式神：稀有度之间是「或」，标签之间是「且」。"""
        index = await self._index()
        wanted_rarity = {r.upper() for r in rarity}
        wanted_tags = [t.strip() for t in tags if t.strip()]
        unknown = [t for t in wanted_tags if t not in index.all_tags]
        if unknown:
            return AskResult(unknown_tags=unknown, known_tags=sorted(index.all_tags))

        rows = [
            {
                "name": str(entry["name"]),
                "rarity": str(entry.get("rarity") or ""),
                "tags": [str(t) for t in entry.get("tags") or []],
            }
            for entry in index.entries
            if (not wanted_rarity or str(entry.get("rarity") or "").upper() in wanted_rarity)
            and all(tag in index.tags_of(entry) for tag in wanted_tags)
        ]
        return AskResult(rows=rows[:limit], total=len(rows))

    # ── 各类条目 ──────────────────────────────────────────────────────

    async def _character_page(self, name: str) -> PageContent | None:
        entry = (await self._index()).get(name)
        if entry is not None:
            # 按 ID 取：库里有游离的同名文档（如 Data:603.json），按名字取会拿错
            doc = await self.data.doc(f"Data:Character/{entry['id']}.json")
        else:
            # 阴阳师、英杰等不在式神索引里，也没有自己的页面
            doc = await self._unindexed_character(name)
        if doc is None:
            return None

        rarity = str((entry or {}).get("rarity") or doc.get("grade") or "")
        data = {
            "id": doc.get("id"),
            "name": name,
            "rarity": "" if rarity == "0" else rarity,
            "gender": "/".join(_as_list((entry or {}).get("gender") or doc.get("gender"))),
            "cv": {k: v for k, v in (doc.get("cv") or {}).items() if v},
            "year": str((entry or {}).get("year") or doc.get("year") or ""),
            "introduction": _BR_RE.sub("", str(doc.get("introduction") or "")).strip(),
            "tags": [str(t) for t in (entry or {}).get("tags") or []],
            "nicknames": _as_list((entry or {}).get("nickname")),
            "skills": await self._skills(doc.get("skills") or []),
        }
        url = self.client.page_url(str(entry.get("pageTitle") or name)) if entry else ""
        return PageContent(title=name, kind=KIND_CHARACTER, url=url, data=data)

    async def _skills(self, raw_skills: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        own_names = {str(s.get("id")): str(s.get("name") or "") for s in raw_skills}
        skills: list[dict[str, Any]] = []
        for raw in raw_skills:
            local_tips = {str(t.get("id")): str(t.get("name") or "") for t in raw.get("tips") or []}
            local_buffs = {
                f"{b.get('id')}_{b.get('level')}": str(b.get("name") or "")
                for b in raw.get("buffs") or []
            }
            description = str(raw.get("description") or "")
            names: dict[str, str] = {}
            for kind, key in set(_PLACEHOLDER_RE.findall(description)):
                if kind == "tips":
                    name = local_tips.get(key) or (await self._glossary())[0].get(key, "")
                elif kind == "buff":
                    name = local_buffs.get(key) or (await self._glossary())[1].get(key, "")
                else:  # [skill_576_1] 指向同一式神的技能 576_01
                    owner, _, number = key.partition("_")
                    padded = f"{owner}_{int(number):02d}" if number.isdigit() else key
                    name = own_names.get(padded) or own_names.get(key, "")
                names[f"{kind}_{key}"] = name

            def substitute(match: re.Match[str], names: dict[str, str] = names) -> str:
                resolved = names.get(f"{match.group(1)}_{match.group(2)}")
                # 解析不到时留一个可读的占位，不把内部编号发给用户
                return f"「{resolved}」" if resolved else "「?」"

            # 原文里技能引用已经带『』，去掉重复的一层括号
            resolved_text = _NESTED_QUOTE_RE.sub(
                r"『\1』", _PLACEHOLDER_RE.sub(substitute, description)
            )
            cost = raw.get("cost")
            skills.append(
                {
                    "name": str(raw.get("name") or ""),
                    "cost": cost if isinstance(cost, int) and cost > 0 else 0,
                    "passive": bool(raw.get("passive")),
                    "description": _BR_RE.sub("\n", resolved_text),
                }
            )
        return skills

    async def _soul_page(self, name: str) -> PageContent | None:
        folded = name.casefold()
        soul = next(
            (s for s in await self._souls() if str(s.get("name") or "").casefold() == folded),
            None,
        )
        if soul is None:
            return None
        data = {
            "name": str(soul["name"]),
            "attribute": str(soul.get("suit") or ""),
            "effect": str(soul.get("desc") or ""),
            "special": str(soul.get("effect") or ""),
            "sources": [p.strip() for p in _BR_RE.split(str(soul.get("way") or "")) if p.strip()],
        }
        return PageContent(
            title=data["name"],
            kind=KIND_SOUL,
            url=self.client.page_url(self.soul_page) if self.soul_page else "",
            data=data,
        )

    # ── 排版、命令、工具 ──────────────────────────────────────────────

    def format_page(
        self, page: PageContent, *, max_chars: int, include_link: bool, markdown: bool
    ) -> str | None:
        formatter = {KIND_CHARACTER: format_character, KIND_SOUL: format_soul}.get(page.kind)
        if formatter is None:
            return None
        return formatter(page, max_chars=max_chars, include_link=include_link, markdown=markdown)

    def register(self, ctx: ProviderContext) -> None:
        """``/筛选`` 命令和给对话模型用的 wiki_filter 工具。"""
        cfg = self.filter_command
        name = str(cfg.get("name") or "筛选")
        max_chars, unavailable = ctx.max_chars, ctx.unavailable

        async def handle_filter(message: InboundMessage, args: str) -> OutboundMessage | None:
            def reply(text: str) -> OutboundMessage:
                return OutboundMessage(content=text, reply_to=message.message_id)

            tokens = (args or "").split()
            if not tokens:
                return reply(f"用法：/{name} <稀有度或标签…>，例如：/{name} SSR 拉条")
            rarity = [t.upper() for t in tokens if t.upper() in RARITIES]
            tags = [t for t in tokens if t.upper() not in RARITIES]
            try:
                result = await self.ask(rarity=rarity, tags=tags, limit=500)
            except WikiError:
                logger.exception("wiki_filter_failed", args_chars=len(args))
                return reply(unavailable)
            return reply(format_ask(result, rarity=rarity, tags=tags, max_chars=max_chars))

        async def tool_filter(args: dict[str, Any], _message: InboundMessage) -> Any:
            rarity = [str(r) for r in args.get("rarity") or [] if isinstance(r, str)]
            tags = [str(t) for t in args.get("tags") or [] if isinstance(t, str)]
            result = await self.ask(rarity=rarity, tags=tags, limit=80)
            if result.unknown_tags:
                return {
                    "error": "没有这些标签",
                    "unknown": result.unknown_tags,
                    "可用标签": result.known_tags,
                }
            return {"total": result.total, "names": [r["name"] for r in result.rows]}

        ctx.plugin.add_tool(
            "wiki_filter",
            "按条件筛选式神，返回名单。rarity 之间是「或」，tags 之间是「且」。"
            "常用标签：伤害、控制、治疗、拉条、推条、护盾、鬼火、驱散、复活、反击、无视防御。",
            {
                "type": "object",
                "properties": {
                    "rarity": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(RARITIES)},
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
            tool_filter,
        )
        ctx.plugin.add_command(
            name,
            handle_filter,
            aliases=[str(alias) for alias in cfg.get("aliases") or []],
            description=str(cfg.get("description") or "按稀有度、标签筛选式神"),
        )


# ── 排成发给用户的文字 ────────────────────────────────────────────────


def _skill_label(skill: dict) -> str:
    if skill.get("passive"):
        return "（被动）"
    return f"（{skill['cost']}火）" if skill.get("cost") else ""


def format_character(
    page: PageContent, *, max_chars: int = 800, include_link: bool = True, markdown: bool = False
) -> str:
    data = page.data
    cv = data.get("cv") or {}
    if cv.get("jp") and cv.get("cn"):
        cv_text = f"CV {cv['jp']}（日）/ {cv['cn']}（中）"
    else:
        cv_text = f"CV {cv.get('jp') or cv.get('cn')}" if cv else ""
    facts = [data.get("rarity"), data.get("gender"), cv_text]
    lines = [" · ".join([bold(f"【{page.title}】", markdown), *(f for f in facts if f)])]
    if data.get("nicknames"):
        lines.append("别名：" + "、".join(data["nicknames"]))
    if data.get("tags"):
        lines.append("定位：" + "、".join(data["tags"]))
    if data.get("introduction"):
        intro = clip(data["introduction"], _INTRO_CHARS)
        lines.append(f"> {intro}" if markdown else intro)
    for number, skill in enumerate(data.get("skills") or [], start=1):
        name = bold(skill["name"], markdown)
        lines.append(f"{number}. {name}{_skill_label(skill)}：{skill['description']}")
    return finish(
        lines, page.url, max_chars=max_chars, include_link=include_link, markdown=markdown
    )


def format_soul(
    page: PageContent, *, max_chars: int = 800, include_link: bool = True, markdown: bool = False
) -> str:
    data = page.data
    lines = [
        bold(f"【{page.title}】", markdown) + "御魂",
        f"属性：{data['attribute']}" if data.get("attribute") else "",
        f"效果：{data['effect']}" if data.get("effect") else "",
        f"备注：{data['special']}" if data.get("special") else "",
        "获取：" + "；".join(data["sources"]) if data.get("sources") else "",
    ]
    return finish(
        lines, page.url, max_chars=max_chars, include_link=include_link, markdown=markdown
    )


def format_ask(
    result: AskResult,
    *,
    rarity: Sequence[str],
    tags: Sequence[str],
    max_chars: int = 800,
) -> str:
    if result.unknown_tags:
        return "没有这个标签：" + "、".join(result.unknown_tags)
    condition = " + ".join([*rarity, *tags])
    if not result.rows:
        return f"没有符合「{condition}」的式神。"
    # 名单按长度预算装，装不下的不列，并在标题里说明只列了多少个
    header = f"{condition}：共 {result.total} 个式神"
    budget = max_chars - len(header) - 12
    names: list[str] = []
    for row in result.rows:
        if sum(len(n) + 1 for n in names) + len(row["name"]) > budget:
            break
        names.append(row["name"])
    more = f"（仅列出前 {len(names)} 个）" if len(names) < result.total else ""
    return f"{header}{more}\n" + "、".join(names)


def create(setup: SiteSetup) -> YysProvider:
    raw = setup.raw.get("wiki") or {}
    soul_page = raw["soul_page"] if "soul_page" in raw else "御魂检索器"
    return YysProvider(
        setup.parent(HuijiData),  # 数据在灰机的 Data 命名空间里
        soul_page=str(soul_page or ""),
        filter_command=(raw.get("commands") or {}).get("filter") or {},
    )
