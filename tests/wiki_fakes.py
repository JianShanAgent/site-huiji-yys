"""测试用的假 wiki：数据形状照抄 yys.huijiwiki.com 的真实返回（内容做了删减）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
from jianshanagent.core.wiki import WikiSite
from jianshanagent.platforms.huiji.wiki import HuijiData
from jianshanagent.plugins.wiki_query.client import WikiQueryClient

from jianshanagent_site_huiji_yys.wiki import YysProvider

SITE = "https://wiki.example"

DOCS: dict[str, dict[str, Any]] = {
    "Data:CharacterIndex.json": {
        "_id": "Data:CharacterIndex.json",
        "characters": [
            {
                "id": 611,
                "name": "大天狗·高天铁羽",
                "pageTitle": "大天狗·高天铁羽",
                "rarity": "UR",
                "year": "2026",
                "gender": ["男"],
                "tags": ["伤害", "拉条"],
                "extraTags": {"dmgtype": "群体"},
                "nickname": ["高铁", "UR狗"],
            },
            {
                "id": 400,
                "name": "初翎山风",
                "pageTitle": "初翎山风",
                "rarity": "SP",
                "year": "2023",
                "gender": ["男"],
                "tags": ["伤害"],
                "extraTags": {"dmgtype": "单体"},
            },
            {
                "id": 296,
                "name": "山风",
                "pageTitle": "山风",
                "rarity": "SSR",
                "year": "2018",
                "gender": ["男"],
                "tags": ["伤害"],
                "extraTags": {"dmgtype": "单体,间接伤害", "positive": "行动条提升,增伤"},
                "nickname": "岚",
            },
            {
                "id": 300,
                "name": "荒骷髅",
                "pageTitle": "荒骷髅",
                "rarity": "SSR",
                "year": "2017",
                "gender": [],
                "tags": ["伤害"],
                "extraTags": {"dmgtype": "群体"},
            },
            {
                "id": 500,
                "name": "咲耶",
                "pageTitle": "咲耶",
                "rarity": "SP",
                "year": "2025",
                "gender": ["女"],
                "tags": ["拉条", "治疗"],
            },
        ],
    },
    "Data:Character/296.json": {
        "_id": "Data:Character/296.json",
        "_hjschema": "Character",
        "id": 296,
        "name": {"cn": "山风", "jp": ""},
        "cv": {"cn": "", "jp": "增田俊树"},
        "year": 2018,
        "gender": "男",
        "grade": "0",
        "introduction": "居住在森林中的少年妖怪，<br>被称作森林之王。",
        "skills": [
            {
                "id": "296_01",
                "name": "风",
                "cost": 0,
                "description": "攻击敌方目标2次。",
            },
            {
                "id": "296_02",
                "name": "烈",
                "cost": -1,
                "passive": True,
                "description": (
                    "自身[tips_3]结束时获得[buff_2962_1]。<br>"
                    "施放『[skill_296_03]』后触发[tips_999]。"
                ),
                "tips": [{"id": 3, "name": "控制效果", "text": "…"}],
            },
            {
                "id": "296_03",
                "name": "斩",
                "cost": 3,
                "description": "造成攻击132%伤害。",
            },
        ],
    },
    "Data:Character/300.json": {
        "_id": "Data:Character/300.json",
        "_hjschema": "Character",
        "id": 300,
        "name": {"cn": "荒骷髅", "jp": ""},
        "cv": {"cn": "", "jp": ""},
        "gender": "",
        "grade": "SSR",
        "introduction": "",
        "skills": [{"id": "300_01", "name": "骨碎", "cost": 0, "description": "攻击敌方。"}],
    },
    "Data:Character/611.json": {
        "_id": "Data:Character/611.json",
        "_hjschema": "Character",
        "id": 611,
        "name": {"cn": "大天狗·高天铁羽", "jp": ""},
        "cv": {"cn": "", "jp": "前野智昭"},
        "gender": "男",
        "grade": "UR",
        "introduction": "",
        "skills": [],
    },
    # 不在式神索引里的角色；同时有一份游离的重复文档
    "Data:10.json": {
        "_id": "Data:10.json",
        "_hjschema": "Character",
        "id": 10,
        "name": {"cn": "晴明", "jp": ""},
        "grade": "阴阳师",
        "skills": [],
    },
    "Data:Character/10.json": {
        "_id": "Data:Character/10.json",
        "_hjschema": "Character",
        "id": 10,
        "name": {"cn": "晴明", "jp": ""},
        "cv": {"cn": "杨天翔", "jp": "杉山纪彰"},
        "gender": "男",
        "grade": "阴阳师",
        "introduction": "",
        "skills": [{"id": "10_01", "name": "基础术式", "cost": 0, "description": "造成伤害。"}],
    },
    "Data:Soul.json": {
        "_id": "Data:Soul.json",
        "soul": [
            {
                "id": 27,
                "name": "针女",
                "nickname": ["针"],
                "desc": "暴击时，有40%概率造成无视防御伤害。",
                "suit": "暴击15%",
                "type": 1,
                "way": "御魂副本-周四（主）/六/日<br>商店-秘魂屋",
                "Tag": "暴击",
                "effect": None,
            },
            {
                "id": 60,
                "name": "荒骷髅",
                "desc": "造成伤害时，提升伤害。",
                "suit": "攻击加成15%",
                "type": 2,
                "way": "首领御魂副本",
                "Tag": "单件属性",
                "effect": "独立乘区",
            },
        ],
    },
    "Data:SkillTips.json": {
        "_id": "Data:SkillTips.json",
        "tips": {"3": {"id": 3, "skill_tips_name": "全局控制效果"}},
    },
    "Data:SkillBuffs.json": {
        "_id": "Data:SkillBuffs.json",
        "buff": {"2962_1": {"id": "2962_1", "name": "困兽"}},
    },
}

WIKI_PAGES: dict[str, str] = {
    "十周年游园活动": "\n== 活动时间 ==\n9月25日-9月27日\n\n\n== 活动地点 ==\n\t\t\n广州塔广场",
    "战斗机制": "站位机制：十周年版本后有调整。",
    "式神传记": "这一页的正文提到了战斗机制。",
}


#: 重定向：页面的别名 → 真正的标题
REDIRECTS: dict[str, str] = {"十周年": "十周年游园活动"}


class FakeWiki:
    """记录请求，并按真实接口的规则应答。"""

    def __init__(self, *, challenge: bool = False, rankings: tuple[str, ...] = ()):
        self.requests: list[httpx.Request] = []
        self.challenge = challenge
        #: 这个站的搜索接口认识的排法（``srqiprofile``）；别的值接口报错
        self.rankings = rankings
        #: 接下来这么多次搜索，搜索后端出故障（接口照样回 HTTP 200，内容是错误）
        self.search_outages = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.challenge:
            return httpx.Response(
                403,
                headers={"content-type": "text/html; charset=UTF-8"},
                text="<!DOCTYPE html><title>请稍候…</title>",
            )
        params = dict(request.url.params)
        if request.url.path == "/api/rest_v1/namespace/data":
            return httpx.Response(200, json=self._find(json.loads(params["filter"])))
        if request.url.path == "/api.php":
            if params.get("list") == "search" and self.search_outages > 0:
                self.search_outages -= 1
                info = "We could not complete your search due to a temporary problem."
                return httpx.Response(200, json={"error": {"code": "internal", "info": info}})
            ranking = params.get("srqiprofile")
            if ranking is not None and ranking not in self.rankings:
                # 真实接口对不认识的排法：HTTP 200，内容是错误
                info = f'Unrecognized value for parameter "srqiprofile": {ranking}.'
                return httpx.Response(200, json={"error": {"code": "badvalue", "info": info}})
            return httpx.Response(200, json=self._api(params))
        return httpx.Response(404, json={"title": "Not found."})

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @staticmethod
    def _find(filter_: dict[str, Any]) -> dict[str, Any]:
        wanted = filter_.get("_id")
        if isinstance(wanted, dict):
            docs = [DOCS[i] for i in wanted["$in"] if i in DOCS]
        elif wanted is not None:
            docs = [DOCS[wanted]] if wanted in DOCS else []
        else:
            docs = [
                d
                for d in DOCS.values()
                if d.get("_hjschema") == filter_.get("_hjschema")
                and (d.get("name") or {}).get("cn") == filter_.get("name.cn")
            ]
        return {"_embedded": docs, "_id": "yys", "_returned": len(docs)}

    @staticmethod
    def _api(params: dict[str, str]) -> dict[str, Any]:
        if params.get("list") == "search":
            term = params["srsearch"]
            if term.startswith('intitle:"'):  # 只匹配标题
                word = term[len('intitle:"') : -1]
                titles = [t for t in WIKI_PAGES if word in t]
            else:  # 全文：标题或正文里出现
                titles = [t for t, text in WIKI_PAGES.items() if term in t or term in text]
            return {"query": {"search": [{"title": t, "pageid": 1} for t in titles]}}
        title = params["titles"]
        redirects = []
        if params.get("redirects") and title in REDIRECTS:
            redirects = [{"from": title, "to": REDIRECTS[title]}]
            title = REDIRECTS[title]
        if title not in WIKI_PAGES:
            return {"query": {"pages": [{"title": title, "missing": True}]}}
        if "prop" not in params:  # 只问有没有这个页面
            return {"query": {"redirects": redirects, "pages": [{"pageid": 7, "title": title}]}}
        page = {
            "pageid": 7,
            "title": title,
            "extract": WIKI_PAGES[title],
            "fullurl": f"{SITE}/wiki/{title}",
        }
        return {"query": {"pages": [page]}}


def make_client(
    fake: FakeWiki,
    *,
    auth_header: str | None = None,
    auth_key: str | None = None,
    sites: bool = True,
    **kwargs: Any,
) -> WikiQueryClient:
    """接着假 wiki 的查询客户端。``sites``：装上灰机和阴阳师两个站点插件（式神、御魂）；
    False 就是只认页面的通用客户端。"""
    wiki = WikiSite(
        api_url=f"{SITE}/api.php",
        site_base=SITE,
        user_agent="test-agent",
        auth_header=auth_header,
        auth_key=auth_key,
    )
    client = WikiQueryClient(wiki, transport=httpx.MockTransport(fake), **kwargs)
    if sites:
        data = HuijiData()
        for provider in (data, YysProvider(data, soul_page="御魂检索器")):
            client.add_provider(provider)
            provider.bind(client)
    return client


def yys_of(client: WikiQueryClient) -> YysProvider:
    """客户端上装着的阴阳师插件。"""
    provider = client.provider("huiji_yys")
    assert isinstance(provider, YysProvider)
    return provider
