"""换一个 wiki：查询、提示词这些通用的部分不带任何站点的东西，站点特有的由站点插件接进来。

- 一个站点插件都不装：``/查`` 只认页面（标题、重定向、全文搜索），不去碰灰机的 Data 接口；
  给模型的工具说明、查证和对话的提示词里没有阴阳师的字眼；
- 装上灰机和阴阳师两个插件（仓库里的配置）：式神、御魂、``/筛选`` 都在，提示词是阴阳师的说法；
- 自己写的站点插件（一个目录：清单、默认配置、``wiki`` 模块）能加自己的条目种类和命令。

知识库那一摊的站点插件见 test_kb_sites.py，插件的格式和清单见 test_plugin_spec.py。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.messages import ChatScope, InboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.wiki import WikiSite, load_wikis, wiki_subject
from jianshanagent.plugins.research import analysis, workflow
from jianshanagent.plugins.research.evidence import SiteSearchSource, site_keywords_hint
from jianshanagent.plugins.research.plugin import wording_from
from jianshanagent.plugins.wiki_query.client import KIND_PAGE, NameMatch, SearchHit, WikiQueryClient
from jianshanagent.plugins.wiki_query.hub import WikiQueryHub
from jianshanagent.plugins.wiki_query.plugin import WikiQueryPlugin
from jianshanagent.testing.sites import one_wiki, site_packages, write_site

from wiki_fakes import SITE, FakeWiki, make_client

REPO_CONFIG = Path(__file__).resolve().parent / "config"
#: 只该出现在阴阳师插件和它的配置里的字眼
YYS_WORDS = re.compile("阴阳师|式神|御魂|huijiwiki|yys")


def plain_config(tmp_path: Path, *, site: str = "", **subject: Any) -> AppConfig:
    """仓库里的配置换成「一个普通的 MediaWiki」：不装站点插件，地址和名字换掉。

    ``subject``：这个 wiki 叫什么（``name``、``about``）；``site``：要装的站点插件。
    """
    root = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, root, ignore=shutil.ignore_patterns("local", "secrets.yaml"))
    core = subject | one_wiki(site, api_url=f"{SITE}/api.php", site_base=SITE)
    (root / "core.wiki.yaml").write_text(yaml.safe_dump(core, allow_unicode=True), "utf-8")
    return load_config(root)


def register(monkeypatch: pytest.MonkeyPatch, config: AppConfig, fake: FakeWiki) -> PluginContext:
    """用给定的配置注册 wiki_query 插件，请求都发给假 wiki。"""
    monkeypatch.setattr(
        sys.modules["jianshanagent.plugins.wiki_query.plugin"],
        "WikiQueryClient",
        partial(WikiQueryClient, transport=httpx.MockTransport(fake)),
    )
    ctx = PluginContext("wiki_query", config, {})
    WikiQueryPlugin().register(ctx)
    return ctx


async def send(ctx: PluginContext, text: str) -> str:
    name, _, args = text.lstrip("/").partition(" ")
    handlers = {key: spec.handler for spec in ctx.commands for key in (spec.name, *spec.aliases)}
    message = InboundMessage(
        message_id="m1", chat_scope=ChatScope.C2C, chat_id="u1", user_id="u1", content=text
    )
    out = await handlers[name](message, args)
    assert out is not None
    return out.content


# ---------- 一个站点插件都不装 ----------


async def test_a_plain_mediawiki_is_queried_through_api_php_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWiki()
    ctx = register(monkeypatch, plain_config(tmp_path, name="示例"), fake)
    hub: WikiQueryHub = ctx.services["wiki_query"]
    (client,) = hub.clients
    assert client.providers == [] and client.kind_labels == {} and not hub.multiple

    # 命令只有 /查；说明和用法里不提式神、御魂
    assert [(spec.name, spec.description) for spec in ctx.commands] == [("查", "查询 wiki 条目")]
    assert await send(ctx, "/查") == "用法：/查 <条目名>"
    # 标题完全一致：直接给摘要和链接
    text = await send(ctx, "/查 战斗机制")
    assert text.startswith("**【战斗机制】**\n站位机制") and f"{SITE}/wiki/" in text
    # 标题里带这个词的页面只有一个：也直接给
    assert (await send(ctx, "/查 十周年")).startswith("**【十周年游园活动】**\n【活动时间】")
    assert await send(ctx, "/查 不存在的东西") == "没有找到与「不存在的东西」相关的 wiki 条目。"
    # 在阴阳师插件里是式神的名字，这里只是一个搜不到的词
    assert await send(ctx, "/查 山风") == "没有找到与「山风」相关的 wiki 条目。"

    # 别名只认重定向
    # 查到的东西上标着出自哪个 wiki（core.wiki.yaml 里它的 id）
    assert await hub.lookup_name("十周年") == [NameMatch("十周年游园活动", KIND_PAGE, wiki="main")]
    assert await hub.lookup_name("高铁") == []
    assert await hub.search("战斗机制") == [
        SearchHit("战斗机制", KIND_PAGE, exact=True, wiki="main")
    ]

    # 全程只用 api.php：没有去碰灰机的 Data 接口
    assert set(fake.paths) == {"/api.php"}

    # 给模型的工具：只有通用的两个，说明里是这个 wiki 的名字，没有 kind 可选
    assert set(ctx.tools) == {"wiki_search", "wiki_get"}
    assert ctx.tools["wiki_search"].description == (
        "在示例 wiki 里按名称找条目。支持页面标题和关键词。"
        "返回候选列表，exact=true 表示名称完全一致。"
    )
    assert ctx.tools["wiki_get"].description == (
        "取一个条目的内容。普通页面返回正文摘要。title 用 wiki_search 返回的 title。"
    )
    assert set(ctx.tools["wiki_get"].parameters["properties"]) == {"title"}
    found = await ctx.tools["wiki_get"].handler({"title": "战斗机制", "kind": "character"}, None)  # type: ignore[arg-type]
    assert found["found"] and found["kind"] == KIND_PAGE
    await client.aclose()


def test_without_site_plugins_nothing_in_the_effective_config_is_about_onmyoji(
    tmp_path: Path,
) -> None:
    config = plain_config(tmp_path)
    assert config.sites == []
    # 自带的站点插件的配置还读得到（没用到就不生效）；其余生效的配置里一个阴阳师的字眼都没有
    effective = {name: data for name, data in config.files.items() if not name.startswith("site.")}
    text = json.dumps(effective, ensure_ascii=False)
    assert YYS_WORDS.findall(text.replace("JianShanAgent/agent-core", "")) == []

    # 查证的提示词：站名没写就只说 wiki，因站而异的说法用通用的
    wording = wording_from(config.plugin("research").get("wording") or {}, wiki_subject(config))
    prompts = [
        analysis._prompt("某某是什么时候加入的", analysis.DEFAULT_TOPICS, None, wording),
        workflow._ANALYST.format_map(_Blank(bot=wording.bot, facts=wording.facts,
                                            mixups=wording.mixups, analyst_note="")),
        workflow._REVIEWER.format_map(_Blank(facts=wording.facts, mixups=wording.mixups,
                                             reviewer_note="")),
        workflow._FIXER.format_map(_Blank(bot=wording.bot)),
        site_keywords_hint(wording.keyword_nouns, wording.keyword_example),
        *config.plugin("chat")["prompts"].values(),
        str(config.plugin("memory")["preference_examples"]),
        str(config.get("persona/persona.default")["system_prompt"]),
    ]  # fmt: skip
    assert all(YYS_WORDS.search(prompt) is None for prompt in prompts)
    assert prompts[0].startswith("你在帮 wiki 的问答机器人做查资料前的准备。")
    assert (
        "你是 wiki 机器人的查证员。" in prompts[1]
        and "每一个数值、日期、名字、引文，" in prompts[1]
    )
    assert (
        "不要把别的年份 / 别的对象的内容安到问的对象上。\n3. " in prompts[1]
    )  # 没有这个站特有的提醒
    assert "这种泛指的词不算名字" not in prompts[0]
    assert prompts[4] == SiteSearchSource.keywords_hint and "例：" not in prompts[4]
    # 写了站名：提示词里带上它
    named = wiki_subject(plain_config(tmp_path / "named", name="塞尔达", about="任天堂的游戏"))
    assert (named.label, named.bot) == ("塞尔达 wiki", "塞尔达 wiki 机器人")
    assert named.intro == "塞尔达（任天堂的游戏）wiki"


class _Blank(dict[str, Any]):
    """填提示词模板时，运行时才有的那些项先空着。"""

    def __missing__(self, key: str) -> str:
        return ""


def test_which_wiki_comes_from_one_place_and_is_never_guessed(tmp_path: Path) -> None:
    config = load_config(REPO_CONFIG)
    (repo,) = load_wikis(config)
    subject = wiki_subject(config)
    assert (subject.name, subject.about) == ("阴阳师", "网易手游")
    assert (repo.id, repo.label) == ("huiji", "灰机 wiki")
    assert repo.api_url.endswith("/api.php") and repo.auth_header == "X-authkey"
    assert repo.site_base == "https://yys.huijiwiki.com"  # 没另写：从接口地址得出
    # 页面地址的写法跟着这个站走
    other = WikiSite(api_url="https://x.test/w/api.php", site_base="https://x.test",
                     article_path="/index.php/$1")  # fmt: skip
    assert other.page_url("Main Page") == "https://x.test/index.php/Main_Page"
    assert other.page_url_prefix == "https://x.test/index.php/"
    # 没写是哪个 wiki：不猜，直接报错（以前会悄悄去查阴阳师 wiki）
    (tmp_path / "core.wiki.yaml").write_text("wikis: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不知道是哪个 wiki"):
        load_wikis(load_config(tmp_path))


# ---------- 装上灰机和阴阳师（仓库里的配置） ----------


async def test_the_shipped_sites_add_characters_souls_and_the_filter_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = load_config(REPO_CONFIG)
    names = ("plugin.wiki_query", "core.wiki", "platform.huiji", "site.huiji_yys")
    files = {name: repo.get(name) for name in names}
    (wiki,) = files["core.wiki"]["wikis"]
    files["core.wiki"] = files["core.wiki"] | {"wikis": [wiki | {"api_url": f"{SITE}/api.php"}]}
    fake = FakeWiki()
    ctx = register(monkeypatch, AppConfig(root=Path("."), files=files, secrets={}), fake)
    (client,) = ctx.services["wiki_query"].clients
    assert [provider.id for provider in client.providers] == ["huiji", "huiji_yys"]
    assert client.kind_labels == {"character": "式神", "soul": "御魂"}
    assert [spec.name for spec in ctx.commands] == ["查", "筛选"]
    assert ctx.commands[0].description == "查询式神、御魂或 wiki 条目"  # 阴阳师插件补的说法
    assert await send(ctx, "/查") == "用法：/查 <式神、御魂或条目名>"
    assert (await send(ctx, "/查 高铁")).startswith("**【大天狗·高天铁羽】** · UR")
    assert (await send(ctx, "/筛选 SP 拉条")).startswith("SP + 拉条：共 1 个式神")
    assert list(ctx.tools) == ["wiki_search", "wiki_get", "wiki_filter"]
    assert "式神正式名、玩家昵称" in ctx.tools["wiki_search"].description
    assert ctx.tools["wiki_get"].parameters["properties"]["kind"] == {
        "type": "string",
        "enum": ["character", "soul", "page"],
        "description": "wiki_search 返回的 kind；式神与御魂重名时用它区分",
    }
    assert "/api/rest_v1/namespace/data" in fake.paths
    await client.aclose()

    # 配置里只写了 huiji_yys：灰机是顺着清单带出来的
    assert wiki["site"] == "huiji_yys"


async def test_a_generic_client_and_one_with_the_sites_differ_only_in_what_the_sites_add() -> None:
    plain, full = make_client(FakeWiki(), sites=False), make_client(FakeWiki())
    # 页面两边一样
    assert await plain.search("战斗机制") == await full.search("战斗机制")
    assert await plain.get_page("十周年") == await full.get_page("十周年")
    assert await plain.fulltext_titles("十周年") == await full.fulltext_titles("十周年")
    # 式神只有装了插件的那边认得
    assert await plain.search("山风") == [] and (await full.search("山风"))[0].kind == "character"
    assert await plain.get_page("山风", kind="character") is None
    await plain.aclose()
    await full.aclose()


# ---------- 自己写的站点插件 ----------

CUSTOM_PROVIDER = '''
from jianshanagent.core.messages import OutboundMessage
from jianshanagent.plugins.wiki_query.client import NameMatch, PageContent, SearchHit
from jianshanagent.plugins.wiki_query.provider import WikiProvider


class Recipes(WikiProvider):
    """这个站另有一份菜谱表：/查 能查到菜谱（认别名），另加一个 /食材 命令。"""

    id = "recipes"
    kinds = {"recipe": "菜谱"}
    search_hint = "支持菜谱名和别名、页面标题。"
    get_hint = "菜谱返回食材和做法；"

    def __init__(self, setup):
        self.recipes = dict((setup.raw.get("wiki") or {}).get("recipes") or {})

    def bind(self, client):
        self.client = client

    def _find(self, name):
        for title, recipe in self.recipes.items():
            if name in (title, *recipe.get("aliases", [])):
                return title, recipe
        return None

    async def search(self, query):
        found = self._find(query)
        return [SearchHit(found[0], "recipe", exact=True)] if found else []

    async def lookup_name(self, name):
        found = self._find(name)
        return [NameMatch(found[0], "recipe", tuple(found[1].get("aliases", [])))] if found else []

    async def get_page(self, title, kind):
        found = self._find(title)
        if not found:
            return None
        return PageContent(found[0], "recipe", url=self.client.page_url(found[0]), data=found[1])

    def format_page(self, page, *, max_chars, include_link, markdown):
        if page.kind != "recipe":
            return None
        return f"【{page.title}】食材：{'、'.join(page.data['ingredients'])}"

    def register(self, ctx):
        async def ingredients(message, args):
            found = self._find(args.strip())
            text = "、".join(found[1]["ingredients"]) if found else "没有这道菜"
            return OutboundMessage(content=text, reply_to=message.message_id)

        ctx.plugin.add_command("食材", ingredients, description="查一道菜要哪些食材")


def create(setup):
    return Recipes(setup)
'''


async def test_a_site_of_ones_own_adds_its_entries_and_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 站点插件是一个目录：清单、默认配置、接扩展点的模块。这个站只接查询（wiki），不管知识库
    site_file = {
        "wiki": {"recipes": {"番茄炒蛋": {"aliases": ["西红柿炒鸡蛋"], "ingredients": ["番茄", "鸡蛋"]}}},  # noqa: E501
        # 这个站对通用配置的补充：/查 的说明、查证时问题分哪几类
        "extend": {
            "plugin.wiki_query": {"commands": {"lookup": {"description": "查菜谱或 wiki 条目"}}},
            "plugin.research": {"analysis": {"topics": ["菜谱", "食材", "其它"]}},
        },
    }  # fmt: skip
    sites = site_packages(tmp_path, monkeypatch, "cook_sites")
    write_site(
        sites / "recipes", capabilities=("wiki", "extend"), config=site_file, wiki=CUSTOM_PROVIDER
    )
    config = plain_config(tmp_path, name="下厨", site="cook_sites.recipes")
    assert config.sites == ["recipes"] and config.site("recipes")["wiki"] == site_file["wiki"]
    assert config.plugin("research")["analysis"]["topics"] == ["菜谱", "食材", "其它"]

    fake = FakeWiki()
    ctx = register(monkeypatch, config, fake)
    (client,) = ctx.services["wiki_query"].clients
    assert [(spec.name, spec.description) for spec in ctx.commands] == [
        ("查", "查菜谱或 wiki 条目"),
        ("食材", "查一道菜要哪些食材"),
    ]
    assert await send(ctx, "/查") == "用法：/查 <菜谱或条目名>"
    assert await send(ctx, "/查 西红柿炒鸡蛋") == "【番茄炒蛋】食材：番茄、鸡蛋"
    assert await send(ctx, "/食材 番茄炒蛋") == "番茄、鸡蛋"
    assert (await send(ctx, "/查 战斗机制")).startswith("**【战斗机制】**")  # 页面照常查
    assert await client.lookup_name("西红柿炒鸡蛋") == [
        NameMatch("番茄炒蛋", "recipe", ("西红柿炒鸡蛋",), wiki="main")
    ]
    assert client.kind_label("recipe") == "菜谱" and client.kind_label("page") == "页面"
    assert "支持菜谱名和别名、页面标题。" in ctx.tools["wiki_search"].description
    assert ctx.tools["wiki_get"].parameters["properties"]["kind"]["enum"] == ["recipe", "page"]
    assert "/api/rest_v1/namespace/data" not in fake.paths
    await client.aclose()
