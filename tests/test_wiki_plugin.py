import sys
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path

import httpx
import pytest
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.messages import Button, ChatScope, InboundMessage, OutboundMessage
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.knowledge.retrieval import Retriever
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore
from jianshanagent.plugins.wiki_query.client import WikiQueryClient
from jianshanagent.plugins.wiki_query.plugin import WikiQueryPlugin

from wiki_fakes import SITE, WIKI_PAGES, FakeWiki


class Bot:
    """把插件注册的命令包成「发一句、收一句」。"""

    def __init__(self, ctx: PluginContext):
        self._commands = {
            key: spec.handler for spec in ctx.commands for key in (spec.name, *spec.aliases)
        }

    async def send(self, text: str) -> str:
        name, _, args = text.lstrip("/").partition(" ")
        message = InboundMessage(
            message_id="m1", chat_scope=ChatScope.C2C, chat_id="u1", user_id="u1", content=text
        )
        self.message = message
        out = await self._commands[name](message, args)
        assert isinstance(out, OutboundMessage) and out.reply_to == "m1"
        self.last = out
        return out.content


def _register(monkeypatch: pytest.MonkeyPatch, fake: FakeWiki) -> PluginContext:
    """用仓库里真实的插件配置注册，只把站点地址和传输换成假 wiki。"""
    # 包的 __init__ 导出了同名的 plugin 实例，所以要从 sys.modules 取子模块本身
    monkeypatch.setattr(
        sys.modules["jianshanagent.plugins.wiki_query.plugin"],
        "WikiQueryClient",
        partial(WikiQueryClient, transport=httpx.MockTransport(fake)),
    )
    repo = load_config(Path(__file__).resolve().parent / "config")
    names = ("plugin.wiki_query", "core.wiki", "site.huiji", "site.huiji_yys")
    files = {name: repo.get(name) for name in names}
    (wiki,) = files["core.wiki"]["wikis"]
    files["core.wiki"] = files["core.wiki"] | {"wikis": [wiki | {"api_url": f"{SITE}/api.php"}]}
    # 密钥按 wiki 分着写：secrets.yaml 的 wiki.<这个 wiki 的 id>.authkey
    secrets = {"wiki": {wiki["id"]: {"authkey": "k123"}}}
    config = AppConfig(root=Path("."), files=files, secrets=secrets)
    ctx = PluginContext("wiki_query", config, {})
    WikiQueryPlugin().register(ctx)
    return ctx


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bot]:
    ctx = _register(monkeypatch, FakeWiki())
    yield Bot(ctx)
    await ctx.services["wiki_query"].aclose()


async def test_plugin_wires_header_name_from_config_and_key_from_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWiki()
    ctx = _register(monkeypatch, fake)
    await Bot(ctx).send("/查 山风")
    await ctx.services["wiki_query"].aclose()

    assert fake.requests and all(r.headers["X-authkey"] == "k123" for r in fake.requests)
    assert sorted(spec.name for spec in ctx.commands) == ["查", "筛选"]


async def test_lookup_exact_name_shows_character(bot: Bot) -> None:
    text = await bot.send("/查 山风")
    lines = text.splitlines()

    # 仓库配置里 /查 的结果按 Markdown 发
    assert bot.last.markdown is True
    assert lines[0] == "**【山风】** · SSR · 男 · CV 增田俊树"
    assert "> 居住在森林中的少年妖怪，被称作森林之王。" in lines
    assert "2. **烈**（被动）：自身「控制效果」结束时获得「困兽」。" in lines
    assert "3. **斩**（3火）：造成攻击132%伤害。" in lines
    assert lines[-1] == f"[查看 wiki 页面]({SITE}/wiki/%E5%B1%B1%E9%A3%8E)"


async def test_lookup_by_nickname_and_alias_command(bot: Bot) -> None:
    assert (await bot.send("/查询 高铁")).startswith("**【大天狗·高天铁羽】** · UR")


async def test_lookup_ambiguous_offers_buttons(bot: Bot) -> None:
    assert await bot.send("/查 山") == (
        "「山」有多个相关条目，点下面的按钮，或发送 /查 <完整名称>：\n"
        "1. 山风（式神）\n2. 初翎山风（式神）"
    )
    assert bot.last.buttons == [Button("山风", "/查 山风"), Button("初翎山风", "/查 初翎山风")]


async def test_lookup_same_name_character_and_soul_shows_both(bot: Bot) -> None:
    character, soul = (await bot.send("/查 荒骷髅")).split("\n\n")

    assert character.startswith("**【荒骷髅】** · SSR")
    assert soul.startswith("**【荒骷髅】**御魂\n属性：攻击加成15%")


async def test_lookup_soul_wiki_page_and_not_found(bot: Bot) -> None:
    assert (await bot.send("/查 针女")).startswith("**【针女】**御魂\n属性：暴击15%")
    assert (await bot.send("/查 十周年")).startswith("**【十周年游园活动】**\n【活动时间】")
    assert (await bot.send("/查 晴明")).startswith("**【晴明】** · 阴阳师 · 男 · CV 杉山纪彰（日）")
    assert await bot.send("/查 不存在的东西") == "没有找到与「不存在的东西」相关的 wiki 条目。"
    assert await bot.send("/查") == "用法：/查 <式神、御魂或条目名>"


async def test_a_page_the_wiki_gives_no_text_for_is_shown_from_the_knowledge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 「超鬼王」是模板拼出来的页面：wiki 接口有这一页，但摘要是空的
    monkeypatch.setitem(WIKI_PAGES, "超鬼王", "")
    link = f"[查看 wiki 页面]({SITE}/wiki/超鬼王)"
    ctx = _register(monkeypatch, FakeWiki())
    bot = Bot(ctx)
    assert await bot.send("/查 超鬼王") == f"**【超鬼王】**\n{link}"  # 没有知识库：只有标题和链接

    def piece(chunk_id: str, text: str, seq: int, *path: str) -> tuple[KbChunk, None]:
        page = KbChunk(
            chunk_id=chunk_id,
            doc_key="page:1",
            kind="entry",
            title="超鬼王",
            text=text,
            url=f"{SITE}/wiki/超鬼王",
            section_path=path,
            seq=seq,
        )
        return page, None

    store = MemoryKnowledgeStore()
    await store.sync(
        [
            piece("b0", "超鬼王\n\n2026 年超鬼王数据", 0),
            piece("b1", "超鬼王 > 6星\n\n### 蟹姬\n\n生命：1\n\n防御：563", 1, "6星"),
            # 同名的公告不是这一页
            (
                KbChunk(
                    "n1", "dashen:1", "announcement", "超鬼王", "超鬼王\n\n公告", "https://d/1"
                ),
                None,
            ),
        ],
        fingerprint="fp",
    )
    # knowledge 插件在 wiki_query 之后注册：/查 用的时候才去取它
    ctx.services["knowledge"] = Retriever(store, None)
    assert await bot.send("/查 超鬼王") == (
        f"**【超鬼王】**\n2026 年超鬼王数据\n【6星】\n【蟹姬】\n生命：1\n防御：563\n{link}"
    )
    # 给对话模型用的工具拿到的也是这份正文
    got = await ctx.tools["wiki_get"].handler({"title": "超鬼王"}, bot.message)
    assert got["extract"] == "2026 年超鬼王数据\n【6星】\n【蟹姬】\n生命：1\n防御：563"

    # wiki 接口给了摘要的页面不动；知识库里没有的页面、知识库出错：照旧只有标题和链接
    assert (await bot.send("/查 十周年")).startswith("**【十周年游园活动】**\n【活动时间】")
    monkeypatch.setitem(WIKI_PAGES, "祸至无日", "")
    assert (
        await bot.send("/查 祸至无日")
        == f"**【祸至无日】**\n[查看 wiki 页面]({SITE}/wiki/祸至无日)"
    )

    class Broken:
        async def page_text(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("数据库连不上")

    ctx.services["knowledge"] = Broken()
    assert await bot.send("/查 超鬼王") == f"**【超鬼王】**\n{link}"
    await ctx.services["wiki_query"].aclose()


async def test_filter_command(bot: Bot) -> None:
    assert (
        await bot.send("/筛选 sp ur 拉条") == "SP + UR + 拉条：共 2 个式神\n大天狗·高天铁羽、咲耶"
    )
    assert await bot.send("/筛 拉跳") == "没有这个标签：拉跳"
    assert await bot.send("/筛选 N") == "没有符合「N」的式神。"
    assert await bot.send("/筛选") == "用法：/筛选 <稀有度或标签…>，例如：/筛选 SSR 拉条"


async def test_wiki_outage_gives_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _register(monkeypatch, FakeWiki(challenge=True))
    bot = Bot(ctx)

    assert await bot.send("/查 山风") == "wiki 暂时访问不了，请稍后再试。"
    assert await bot.send("/筛选 SSR") == "wiki 暂时访问不了，请稍后再试。"
    await ctx.services["wiki_query"].aclose()
