"""知识库的自动更新：读最近更改并归类、渲染缓存什么时候算过时、取不下来时沿用旧内容、
一轮同步（取数 → 算向量 → 导入）、闸门、全部重取、图库。不联网。"""

import dataclasses
import hashlib
import itertools
import json
import os
import time
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from jianshanagent.core.config import load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingProvider
from jianshanagent.platforms.huiji.knowledge import HuijiKnowledge
from jianshanagent.plugins.knowledge.changes import (
    Change,
    ChangeContext,
    Cursor,
    plan_changes,
    read_changes,
)
from jianshanagent.plugins.knowledge.cli import main
from jianshanagent.plugins.knowledge.embed import embed_chunks
from jianshanagent.plugins.knowledge.fetcher import BudgetExceeded, DiskCache, WikiFetcher
from jianshanagent.plugins.knowledge.image_collect import (
    IMAGES_FILE,
    WikiFile,
    original_path,
    read_images,
    write_images,
)
from jianshanagent.plugins.knowledge.image_embed import embed_image_files, vectors_dirname
from jianshanagent.plugins.knowledge.image_store import MemoryImageStore
from jianshanagent.plugins.knowledge.images import ImageSettings
from jianshanagent.plugins.knowledge.pipeline import run_pipeline
from jianshanagent.plugins.knowledge.settings import (
    KB_DIR_ENV,
    ChunkSettings,
    CleanSettings,
    DateSettings,
    FetchSettings,
    ReportSettings,
    Settings,
    SourceSettings,
    SyncSettings,
    WikiSettings,
    load_settings,
    parse_window,
)
from jianshanagent.plugins.knowledge.sink import CHUNKS_FILE, JsonlSink
from jianshanagent.plugins.knowledge.site import SiteSet
from jianshanagent.plugins.knowledge.sources import (
    META_KEY,
    RENDER_DATA,
    RENDER_NEW,
    RENDER_REFRESH,
    RENDER_TOUCHED,
    PageRef,
    WikiSource,
)
from jianshanagent.plugins.knowledge.store import KbChunk, MemoryKnowledgeStore
from jianshanagent.plugins.knowledge.sync import (
    HEARTBEAT_FILE,
    KB_BUILDING,
    KB_NONE,
    KB_READY,
    KB_REQUESTED,
    RESULT_CHECKED,
    RESULT_PROBLEM,
    RESULT_UNCHANGED,
    RESULT_UPDATED,
    Busy,
    MemoryStatusStore,
    PostgresStatusStore,
    SyncEnv,
    SyncState,
    Targets,
    _locked,
    cancel_build,
    full_due,
    healthy,
    kb_state,
    load_state,
    read_build,
    request_build,
    save_state,
    sync_once,
)
from PIL import Image

from jianshanagent_site_huiji_yys.knowledge import YysPlugin
from jianshanagent_site_huiji_yys.knowledge.changes import character_pages
from jianshanagent_site_huiji_yys.knowledge.images import CatalogData, build_catalog
from jianshanagent_site_huiji_yys.knowledge.settings import (
    CatalogSettings,
    DatasetSettings,
    EntitySettings,
    PostSource,
    YysConfig,
)
from jianshanagent_site_huiji_yys.knowledge.settings import SourceSettings as YysSources

PROVIDER = EmbeddingProvider(
    name="fake", base_url="http://embed.test/v1", model="m", dimensions=8, max_retries=0
)
#: 一轮里列清单要发的请求：主命名空间、两个公告来源、剧情各列一次，再按标题查一次式神索引
LISTING = ["list 0", "list 微信/", "list 大神/", "list Story/", "titles"]


#: 图库只收立绘大图
CATALOG = CatalogSettings(
    card_prefix="", avatar_prefix="", scroll_prefix="", background_prefix="", souls=False
)
#: 阴阳师插件在这些测试里的配置：不生成数据集文档，实体字典只取式神索引
YYS = YysConfig(
    entities=EntitySettings(soul="", skin=""),
    datasets=DatasetSettings(enabled=False),
    images=CATALOG,
)


def make_sites(yys: YysConfig = YYS) -> SiteSet:
    huiji = HuijiKnowledge()
    return SiteSet([huiji, YysPlugin(huiji, yys)])


def make_settings(tmp_path: Path, *, sites: SiteSet | None = None, **sync: Any) -> Settings:
    """``sites`` 不给就是灰机 + 阴阳师；给空的 ``SiteSet()`` 就是纯 MediaWiki。"""
    return Settings(
        wiki=WikiSettings(
            api_url="https://w.test/api.php",
            site_base="https://w.test",
            user_agent="test-agent",
            auth_header="X-authkey",
            auth_key="wiki-secret",
        ),
        fetch=FetchSettings(requests_per_second=1000, max_retries=0),
        sources=SourceSettings(),
        clean=CleanSettings(),
        chunk=ChunkSettings(),
        report=ReportSettings(),
        dates=DateSettings(),
        sync=SyncSettings(**sync),
        output_dir=tmp_path / "kb",
        cache_dir=tmp_path / "kb" / "cache",
        sites=make_sites() if sites is None else sites,
    )


def html(text: str) -> str:
    return f'<div class="mw-parser-output"><p>{text}</p></div>'


class FakeSite:
    """假 wiki：页面、Data 文档、文件和最近更改都能在测试里改，接口照真实的形状应答。"""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.data: dict[str, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.changes: list[dict[str, Any]] = []
        #: 收到的请求，一条一个简短的说法
        self.asked: list[str] = []
        #: 这些页面的渲染请求返回 500
        self.broken: set[str] = set()
        #: 最近更改一页给几条
        self.changes_per_page = 500
        self.now = "2030-01-01T00:00:00Z"
        self._ids = itertools.count(1)
        self._revs = itertools.count(100)
        self._rcids = itertools.count(1)

    # ── 在测试里改站点 ──

    def changed(self, ns: int, title: str, kind: str = "edit", log: str = "") -> None:
        entry = {"type": kind, "ns": ns, "title": title, "timestamp": self.now}
        self.changes.append(entry | {"rcid": next(self._rcids)} | ({"logtype": log} if log else {}))

    def edit(self, title: str, text: str, *, wikitext: str = "正文") -> None:
        """新建或编辑一个条目：有新修订，最近更改里记一条。"""
        page = self.pages.setdefault(title, {"id": next(self._ids)})
        page |= {
            "rev": next(self._revs),
            "touched": self.now,
            "wikitext": wikitext,
            # html：按修订号取到的（wiki 缓存着的）；fresh：把源码交过去现渲染出来的
            "html": html(text),
            "fresh": html(text),
        }
        self.changed(0, title)

    def retemplate(self, title: str, text: str) -> None:
        """模板改了：页面没有新修订，touched 变了，渲染结果跟着变。"""
        self.pages[title] |= {"touched": self.now, "html": html(text), "fresh": html(text)}
        self.changed(10, "模板:活动")

    def put_data(self, title: str, doc: dict[str, Any]) -> None:
        entry = self.data.setdefault(title, {"id": next(self._ids)})
        entry |= {"rev": next(self._revs), "doc": doc}
        self.changed(3500, title)

    def remove(self, title: str) -> None:
        del self.pages[title]
        self.changed(0, title, kind="log", log="delete")

    # ── 应答 ──

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.test":
            name = request.url.path.rsplit("/", 1)[-1]
            self.asked.append(f"download {name}")
            return httpx.Response(200, content=self.files[name])
        assert request.headers["X-authkey"] == "wiki-secret"
        params = dict(request.url.params)
        if request.url.path.endswith("/namespace/data"):
            title = json.loads(params["filter"])["_id"]
            self.asked.append(f"data {title}")
            found = self.data.get(title)
            return httpx.Response(200, json={"_embedded": [found["doc"]] if found else []})
        if params.get("action") == "parse":
            return self._parse(params)
        if params.get("list") == "recentchanges":
            assert params["rcdir"] == "newer"
            self.asked.append("changes")
            found_changes = sorted(
                (c for c in self.changes if c["timestamp"] >= params["rcstart"]),
                key=lambda c: (c["timestamp"], c["rcid"]),
            )
            start = int(params.get("rccontinue", 0))
            end = start + self.changes_per_page
            body: dict[str, Any] = {"query": {"recentchanges": found_changes[start:end]}}
            if end < len(found_changes):
                body["continue"] = {"rccontinue": str(end)}
            return httpx.Response(200, json=body)
        if params.get("generator") == "allpages":
            prefix = params.get("gapprefix", "")
            self.asked.append(f"list {prefix or params['gapnamespace']}")
            if params["gapnamespace"] == "0":
                listed = [self._page_info(title) for title in self.pages]
            else:
                listed = [
                    self._data_info(title)
                    for title in self.data
                    if title.partition(":")[2].startswith(prefix)
                ]
            return httpx.Response(200, json={"query": {"pages": listed}})
        if params.get("list") == "allimages":
            self.asked.append(f"files {params['aiprefix']}")
            listed = [
                {"name": name, "url": f"https://cdn.test/{name}", "sha1": sha1(content)}
                for name, content in self.files.items()
                if name.startswith(params["aiprefix"])
            ]
            return httpx.Response(200, json={"query": {"allimages": listed}})
        if params.get("prop") == "info":
            self.asked.append("titles")
            listed = [
                self._data_info(title) if title in self.data else {"title": title, "missing": True}
                for title in params["titles"].split("|")
            ]
            return httpx.Response(200, json={"query": {"pages": listed}})
        if params.get("prop") == "revisions" and "rvslots" in params:
            self.asked.append("wikitext")
            by_id = {str(page["id"]): page for page in self.pages.values()}
            listed = [
                {
                    "pageid": int(page_id),
                    "revisions": [{"slots": {"main": {"content": by_id[page_id]["wikitext"]}}}],
                }
                for page_id in params["pageids"].split("|")
            ]
            return httpx.Response(200, json={"query": {"pages": listed}})
        raise AssertionError(f"没有料到的请求：{params}")

    def _page_info(self, title: str) -> dict[str, Any]:
        page = self.pages[title]
        return {
            "pageid": page["id"],
            "title": title,
            "ns": 0,
            "lastrevid": page["rev"],
            "touched": page["touched"],
            "length": 10,
        }

    def _data_info(self, title: str) -> dict[str, Any]:
        entry = self.data[title]
        return {"pageid": entry["id"], "title": title, "ns": 3500, "lastrevid": entry["rev"]}

    def _parse(self, params: dict[str, str]) -> httpx.Response:
        if "oldid" in params:
            title = next(t for t, p in self.pages.items() if str(p["rev"]) == params["oldid"])
            self.asked.append(f"parse {title}")
            text = self.pages[title]["html"]
        else:
            title = params["title"]
            assert params["text"] == self.pages[title]["wikitext"]
            self.asked.append(f"fresh {title}")
            text = self.pages[title]["fresh"]
        if title in self.broken:
            return httpx.Response(500, json={})
        parsed = {"title": title, "pageid": self.pages[title]["id"], "text": text, "sections": []}
        return httpx.Response(200, json={"parse": parsed})


def sha1(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest()


def fake_embedder(seen: list[str]) -> httpx.MockTransport:
    """假的嵌入接口：记下要算的每一项（文字原样，图片记成 ``<image>``），向量由内容决定。"""

    def handler(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["input"]
        data = []
        for index, item in enumerate(items):
            text = item if isinstance(item, str) else item["image"]
            seen.append(item if isinstance(item, str) else "<image>")
            vector = [0.0] * PROVIDER.dimensions
            vector[int(hashlib.sha1(text.encode()).hexdigest(), 16) % PROVIDER.dimensions] = 1.0
            data.append({"index": index, "embedding": vector})
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": len(items)}})

    return httpx.MockTransport(handler)


INDEX = {"characters": [{"id": 1, "name": "山风", "pageTitle": "山风", "rarity": "SSR"}]}


def make_site() -> FakeSite:
    site = FakeSite()
    site.edit(
        "山风",
        "山风是居住在森林中的少年妖怪，被称作森林之王，擅长单体输出。",
        wikitext="{{角色页|1}}",
    )
    site.edit("活动甲", "活动甲的说明：收集道具可以兑换奖励，活动一共持续两周。")
    site.edit("活动乙", "活动乙的说明：每天登录领取奖励，连续七天可以拿到头像框。")
    site.put_data("Data:CharacterIndex.json", INDEX)
    return site


async def prepare(site: FakeSite, settings: Settings) -> None:
    """像人手动跑过一遍 run 和 embed 那样，把这台机器上的文件准备好；之后站点上的改动才算新的。"""
    fetcher = WikiFetcher(settings.wiki, settings.fetch, transport=httpx.MockTransport(site))
    try:
        await run_pipeline(settings, JsonlSink(settings.output_dir), fetcher=fetcher)
    finally:
        await fetcher.aclose()
    await embed_chunks(
        PROVIDER,
        settings.output_dir / CHUNKS_FILE,
        settings.vectors_dir / PROVIDER.name,
        transport=fake_embedder([]),
    )
    site.changes.clear()
    site.asked.clear()
    site.now = "2030-01-02T00:00:00Z"


@dataclasses.dataclass
class Setup:
    site: FakeSite
    settings: Settings
    env: SyncEnv
    store: MemoryKnowledgeStore
    #: 嵌入接口收到的每一项
    embedded: list[str]

    async def round(self, **options: Any) -> dict[str, Any]:
        """做一轮；``self.site.asked`` 里是这一轮发的请求。"""
        self.site.asked.clear()
        self.embedded.clear()
        return await sync_once(self.env, **options)

    async def text_of(self, title: str) -> str:
        return "\n".join(chunk.text for chunk in await self.store.page(title))

    @property
    def state(self) -> SyncState:
        return load_state(self.settings.cache_dir)


async def make_setup(tmp_path: Path, **sync: Any) -> Setup:
    site, settings = make_site(), make_settings(tmp_path, **sync)
    await prepare(site, settings)
    store, embedded = MemoryKnowledgeStore(), []
    transport = httpx.MockTransport(site)
    env = SyncEnv(
        settings=settings,
        images=None,
        provider=PROVIDER,
        targets=Targets(store, MemoryImageStore(), MemoryStatusStore()),
        wiki_transport=transport,
        embed_transport=fake_embedder(embedded),
        cdn_transport=transport,
    )
    return Setup(site, settings, env, store, embedded)


# ---------- 读最近更改、归类 ----------


async def test_changes_are_read_oldest_first_and_never_twice() -> None:
    site = FakeSite()
    site.changes_per_page = 2
    for second, title in enumerate(["甲", "乙", "丙", "丁", "戊"]):
        site.now = f"2030-01-01T00:00:0{second // 2}Z"  # 甲乙同一秒，丙丁同一秒
        site.changed(0, title)
    settings = make_settings(Path("."))
    fetcher = WikiFetcher(settings.wiki, settings.fetch, transport=httpx.MockTransport(site))
    try:
        # 一轮只许翻两页：读到 4 条，说明还有没读完的
        first = await read_changes(fetcher, Cursor(), since="2029-12-31T00:00:00Z", max_requests=2)
        assert [c.title for c in first.changes] == ["甲", "乙", "丙", "丁"] and first.more
        assert first.cursor == Cursor("2030-01-01T00:00:01Z", 4)
        # 接着读：起点那一秒的丙、丁接口会再给一遍，不能再算一次
        rest = await read_changes(fetcher, first.cursor, since="", max_requests=2)
        assert [c.title for c in rest.changes] == ["戊"] and not rest.more
        # 没有新的：游标不动
        again = await read_changes(fetcher, rest.cursor, since="")
        assert again.changes == [] and again.cursor == rest.cursor == Cursor(
            "2030-01-01T00:00:02Z", 5
        )
    finally:
        await fetcher.aclose()


def test_plan_tells_what_each_kind_of_change_calls_for(tmp_path: Path) -> None:
    yys = YysConfig(
        sources=YysSources(
            posts=(PostSource("微信/", "wechat", enabled=False), PostSource("大神/", "dashen"))
        )
    )
    settings = make_settings(tmp_path, sites=make_sites(yys))
    # 式神编号对应哪个条目页，是从缓存里上次取到的式神索引看的（不发请求）
    index = {"characters": [{"id": 331, "pageTitle": "御怨般若"}, {"id": 365, "name": "入内雀"}]}
    assert character_pages(index) == {"331": "御怨般若", "365": "入内雀"}
    cache = DiskCache(settings.cache_dir)
    ref = PageRef(page_id=77, title="Data:CharacterIndex.json", rev_id=5, namespace=3500)
    cache.put_named("listing.json", {"named": {ref.title: dataclasses.asdict(ref)}})
    cache.put("data", ref.page_id, ref.rev_id, index)
    with_images = ChangeContext(settings, cache, images_on=True)

    def plan(*changes: tuple[int, str]) -> Any:
        found = [
            Change(
                rcid=n,
                timestamp=f"2030-01-01T00:00:{n:02d}Z",
                namespace=ns,
                title=title,
                kind="edit",
            )
            for n, (ns, title) in enumerate(changes, start=1)
        ]
        return plan_changes(found, settings, with_images)

    # 标题是接口给的原样：命名空间是中文的，下划线是空格
    nothing = plan(
        (4, "Project:Sandbox"),
        (2, "用户:某人"),
        (2300, "零件:特攻.js"),
        (8, "MediaWiki:Common.css"),
    )
    assert (nothing.kb, nothing.images, nothing.fresh_titles, nothing.shared_data_at) == (
        False,
        False,
        set(),
        "",
    )
    assert (
        plan((0, "锦绣南行")).kb
        and plan((828, "模块:SuperGhost")).kb
        and plan((10, "模板:特攻")).kb
    )

    posts = plan((3500, "Data:大神/扫地工/2026-11.json"), (3500, "Data:大神/扫地工/ months.json"))
    assert posts.kb and not posts.shared_data_at  # 公告直接入库，没有页面是拿它渲染的
    story = plan((3500, "Data:Story/20261104.json"))
    assert story.kb and not story.shared_data_at
    off = plan((3500, "Data:微信/网易阴阳师手游/式神情报/2026-11.json"))  # 关掉的来源
    assert not off.kb and off.counts == {"关掉的公告来源": 1}

    # 某个式神自己的数据：它的条目页现渲染；索引里没有的编号（新式神）不管
    own = plan(
        (3500, "Data:Skilldiff/331.json"),
        (3500, "Data:Character/365.json"),
        (3500, "Data:Character/999.json"),
    )
    assert own.fresh_titles == {"御怨般若", "入内雀"} and not own.kb and not own.shared_data_at
    assert own.images  # 绘卷配图的说明来自 Data:Character/<编号>.json

    # 公共数据集：记下最近一次改动的时间。入库的那几份同时要重跑管线
    shared = plan(
        (3500, "Data:Gift.json"), (3500, "Data:Skin.json"), (3500, "Data:SuperGhost/2026.json")
    )
    assert shared.shared_data_at == "2030-01-01T00:00:03Z" and shared.kb and shared.images
    assert not plan((3500, "Data:Gift.json")).kb

    files = plan((6, "文件:Hj 612 2.png"), (6, "文件:1 kol 20261106 57.png"))
    assert files.images and not files.kb
    assert files.counts == {"图库的文件": 1, "别的文件": 1} and files.titles == [
        "文件:Hj 612 2.png"
    ]
    assert not plan((6, "文件:1 kol 20261106 57.png")).images
    upload = [Change(1, "2030-01-01T00:00:00Z", 6, "文件:Hj 612 2.png", "log", "upload")]
    # 不管图库时，文件的改动不算数
    assert not plan_changes(upload, settings, ChangeContext(settings, cache)).images

    # 只装了灰机插件：不知道哪份数据对应哪些页面，Data 的改动一律当公共数据（等着全部重取）
    huiji_only = make_settings(tmp_path, sites=SiteSet([HuijiKnowledge()]))
    data = [Change(1, "2030-01-01T00:00:00Z", 3500, "Data:Character/365.json", "edit")]
    plain = plan_changes(data, huiji_only)
    assert plain.shared_data_at == "2030-01-01T00:00:00Z" and not plain.kb
    assert not plain.fresh_titles and plain.counts == {"Data": 1}
    # 纯 MediaWiki：条目和模板的改动照认，别的命名空间不管
    bare = make_settings(tmp_path, sites=SiteSet())
    mixed = [*data, Change(2, "2030-01-01T00:00:01Z", 0, "某个条目", "edit"), *upload]
    nobody = plan_changes(mixed, bare)
    assert nobody.kb and not nobody.shared_data_at and not nobody.images
    assert nobody.counts == {"其它": 1, "条目": 1, "别的文件": 1}


# ---------- 渲染缓存什么时候算过时 ----------


async def test_old_cache_without_touched_is_judged_by_when_it_was_stored(tmp_path: Path) -> None:
    site, settings = make_site(), make_settings(tmp_path)
    cache = DiskCache(tmp_path)
    page = site.pages["活动甲"]
    # 老版本存的渲染结果：没有记取的时候 touched 是多少
    cache.put(
        "parse", page["id"], page["rev"], {"text": html("缓存里的旧渲染结果，够二十个字了吧。")}
    )
    stored_at = time.mktime((2026, 11, 11, 3, 30, 0, 0, 0, -1))
    path = tmp_path / "parse" / f"{page['id']}-{page['rev']}.json.gz"
    os.utime(path, (stored_at, stored_at))

    async def render(touched_at: float) -> tuple[str, list[str]]:
        site.asked.clear()
        fetcher = WikiFetcher(settings.wiki, settings.fetch, transport=httpx.MockTransport(site))
        touched = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(touched_at))
        ref = PageRef(page["id"], "活动甲", page["rev"], touched=touched)
        source = WikiSource(fetcher, cache, settings)
        try:
            parsed = await source.parsed(ref)
        finally:
            await fetcher.aclose()
        return str(parsed["text"]), [*source.renders]

    # 页面是在缓存存盘之前失效的：缓存还能用
    assert (await render(stored_at - 3600))[1] == [] and site.asked == []
    # 清单是老快照、没有 touched：只能按修订号认
    source = WikiSource(None, cache, settings)
    assert "旧渲染" in (await source.parsed(PageRef(page["id"], "活动甲", page["rev"])))["text"]
    # 存盘以后页面又失效过：重取，这回记下了 touched
    text, reasons = await render(stored_at + 3600)
    assert "活动甲的说明" in text and reasons == [RENDER_TOUCHED] and site.asked == ["parse 活动甲"]
    assert cache.get("parse", page["id"], page["rev"])[META_KEY]["mode"] == "oldid"
    # 之后就按记下的 touched 比：没变不取，变了才取
    assert (await render(stored_at + 3600))[1] == []
    assert (await render(stored_at + 7200))[1] == [RENDER_TOUCHED]


async def test_new_revision_replaces_older_ones_in_the_cache_and_old_one_covers_a_failure(
    tmp_path: Path,
) -> None:
    site, settings = make_site(), make_settings(tmp_path)
    cache = DiskCache(tmp_path)

    async def render(**options: Any) -> tuple[str, WikiSource]:
        fetcher = WikiFetcher(
            settings.wiki, settings.fetch, transport=httpx.MockTransport(site), **options
        )
        page = site.pages["活动甲"]
        source = WikiSource(fetcher, cache, settings)
        try:
            parsed = await source.parsed(PageRef(page["id"], "活动甲", page["rev"], touched="t"))
        finally:
            await fetcher.aclose()
        return str(parsed["text"]), source

    text, source = await render()
    assert "持续两周" in text and source.renders == {RENDER_NEW: 1}
    first_rev = site.pages["活动甲"]["rev"]

    site.edit("活动甲", "活动甲延期了：改成持续三周，奖励不变，记得每天上线领取。")
    site.broken.add("活动甲")
    text, source = await render()
    # 新修订取不下来：用缓存里上一个修订的内容顶着，记一笔
    assert "持续两周" in text and not source.renders
    assert [(item.title, item.kind) for item in source.stale] == [("活动甲", "parse")]
    # 请求数到了上限也一样（这时候请求根本没有发出去）
    site.broken.clear()
    site.asked.clear()
    text, source = await render(budget=0)
    assert "持续两周" in text and len(source.stale) == 1 and site.asked == []
    with pytest.raises(BudgetExceeded):
        fetcher = WikiFetcher(settings.wiki, settings.fetch, budget=0)
        await fetcher.api("list", {})

    text, source = await render()
    assert "持续三周" in text and not source.stale
    # 新的一版到手，旧修订的缓存就删掉了
    page_id = site.pages["活动甲"]["id"]
    assert cache.get("parse", page_id, first_rev) is None
    assert [p.name for p in (tmp_path / "parse").iterdir()] == [
        f"{page_id}-{site.pages['活动甲']['rev']}.json.gz"
    ]


# ---------- 一轮同步 ----------


async def test_first_round_loads_the_database_and_a_quiet_round_costs_one_request(
    tmp_path: Path,
) -> None:
    # 不按天数全部重取：不然夜里 03:00–06:00 跑这条测试，第一轮就会去全部重取
    setup = await make_setup(tmp_path, full_every_days=0)
    # 库是空的、文件是现成的（刚把文件拷到服务器上就是这样）：不用取数，直接导入
    first = await setup.round()
    assert setup.site.asked == ["changes"]
    assert first["result"] == RESULT_UPDATED and first["kb"]["upserted"] == 3
    assert (await setup.store.info()).with_vectors == 3 and setup.embedded == []
    assert "森林之王" in await setup.text_of("山风")

    # wiki 没有改动：一个请求，什么都不做，也不留记录
    quiet = await setup.round()
    assert setup.site.asked == ["changes"]
    assert quiet["result"] == RESULT_UNCHANGED and "kb" not in quiet
    state = setup.state
    assert len(state.rounds) == 1 and state.checked_at > 0 and not state.kb_pending
    # 状态也写进了库里（管理后台从那里读）
    status = await setup.env.targets.status.read()
    assert status["checked_at"] and status["problems"] == {"check": "", "kb": "", "images": ""}
    assert status["rounds"][0]["result"] == RESULT_UPDATED and status["loaded"]["kb"]

    # 无关的改动（沙盒、用户页）也一样
    setup.site.changed(4, "Project:Sandbox")
    setup.site.changed(2, "用户:某人", kind="log", log="newusers")
    idle = await setup.round()
    assert setup.site.asked == ["changes"] and idle["changes"] == {"其它": 2}
    assert idle["result"] == RESULT_UNCHANGED
    assert (await setup.round())["changes"] == {}  # 读过的变更不会再读一遍


async def test_edited_page_is_the_only_thing_fetched_embedded_and_loaded(tmp_path: Path) -> None:
    setup = await make_setup(tmp_path)
    await setup.round()
    before = {chunk.chunk_id for chunk in await setup.store.page("活动乙")}

    setup.site.edit("活动甲", "活动甲延期了：改成持续三周，奖励不变，记得每天上线领取。")
    record = await setup.round()
    assert setup.site.asked == ["changes", *LISTING, "wikitext", "parse 活动甲"]
    assert record["result"] == RESULT_UPDATED and record["changes"] == {"条目": 1}
    kb = record["kb"]
    assert kb["renders"] == {RENDER_NEW: 1} and (kb["documents"], kb["chunks"]) == (3, 3)
    # 只给变了的那一片算向量；库里换掉的也只有它
    assert len(setup.embedded) == 1 and "持续三周" in setup.embedded[0]
    assert (kb["embedded"], kb["upserted"], kb["deleted"]) == (1, 1, 1)
    text = await setup.text_of("活动甲")
    assert "持续三周" in text and "持续两周" not in text
    assert {chunk.chunk_id for chunk in await setup.store.page("活动乙")} == before
    assert (await setup.store.info()).with_vectors == 3
    assert setup.state.updated_at > 0

    # 新建的页面进库，删掉的页面出库
    setup.site.edit("活动丙", "活动丙的说明：组队挑战首领，按伤害排名发放奖励，为期十天。")
    assert (await setup.round(force=True))["kb"]["chunks"] == 4
    setup.site.remove("活动丙")
    removed = await setup.round(force=True)
    assert (removed["kb"]["chunks"], removed["kb"]["deleted"]) == (3, 1)
    assert await setup.store.page("活动丙") == []


async def test_template_change_is_seen_through_touched_and_data_change_renders_from_source(
    tmp_path: Path,
) -> None:
    setup = await make_setup(tmp_path)
    await setup.round()

    # 模板改了：活动乙没有新修订，但 touched 变了
    setup.site.retemplate("活动乙", "活动乙的说明（新模板）：每天登录领取奖励，连续七天拿头像框。")
    record = await setup.round()
    assert setup.site.asked == ["changes", *LISTING, "parse 活动乙"]
    assert record["kb"]["renders"] == {RENDER_TOUCHED: 1}
    assert "新模板" in await setup.text_of("活动乙")

    # 山风的数据改了：页面没有新修订，touched 也没动，按修订号取到的还是 wiki 缓存着的旧内容
    setup.site.pages["山风"]["fresh"] = html("山风的技能改了：风的伤害系数从一百提高到一百二十。")
    setup.site.changed(3500, "Data:Character/1.json")
    record = await setup.round()
    assert setup.site.asked == ["changes", *LISTING, "fresh 山风"]  # 把源码交过去现渲染
    assert record["kb"]["renders"] == {RENDER_DATA: 1} and record["changes"] == {"Data": 1}
    assert "一百二十" in await setup.text_of("山风") and setup.state.fresh_titles == []

    # 这时候全部重取：别的页面都重取，刚现渲染过的山风不取（取到的会是 wiki 缓存里的旧内容）
    record = await setup.round(full=True)
    assert setup.site.asked == ["changes", *LISTING, "parse 活动甲", "parse 活动乙"]
    assert record["kb"]["renders"] == {RENDER_REFRESH: 2} and record["kb"]["full"]
    assert "一百二十" in await setup.text_of("山风")
    assert record["result"] == RESULT_CHECKED  # 重取回来的内容没有变


async def test_page_that_cannot_be_fetched_keeps_its_old_content_until_it_can(
    tmp_path: Path,
) -> None:
    setup = await make_setup(tmp_path)
    await setup.round()
    setup.site.edit("活动甲", "活动甲延期了：改成持续三周，奖励不变，记得每天上线领取。")
    setup.site.pages["山风"]["fresh"] = html("山风的技能改了：风的伤害系数从一百提高到一百二十。")
    setup.site.changed(3500, "Data:Character/1.json")
    setup.site.broken |= {"活动甲", "山风"}

    record = await setup.round()
    assert record["kb"]["stale"] == 2 and record["result"] == RESULT_CHECKED
    # 两页都还在库里，内容是旧的
    assert "持续两周" in await setup.text_of("活动甲") and "森林之王" in await setup.text_of("山风")
    state = setup.state
    assert state.kb_pending and state.fresh_titles == ["山风"]

    # 下一轮 wiki 上没有新的改动，但上一轮留下的事接着做
    setup.site.broken.clear()
    record = await setup.round()
    assert setup.site.asked == ["changes", *LISTING, "fresh 山风", "parse 活动甲"]
    assert record["kb"]["stale"] == 0 and record["result"] == RESULT_UPDATED
    assert "持续三周" in await setup.text_of("活动甲") and "一百二十" in await setup.text_of("山风")
    state = setup.state
    assert not state.kb_pending and state.fresh_titles == []


async def test_request_budget_ends_the_round_and_the_next_one_picks_up(tmp_path: Path) -> None:
    # 读最近更改 1 + 列清单 5 + 取源码 1 + 渲染 1：三个改过的页面这一轮只取得了一个
    setup = await make_setup(tmp_path, max_requests=8)
    await setup.round()
    for title in ("山风", "活动甲", "活动乙"):
        setup.site.edit(title, f"{title}的新内容：这一段是改过以后的说明，和原来的不一样。")

    record = await setup.round()
    assert len(setup.site.asked) == 8 and record["requests"] == 8
    assert record["kb"]["stale"] == 2 and record["kb"]["renders"] == {RENDER_NEW: 1}
    record = await setup.round()
    assert len(setup.site.asked) == 8 and record["kb"]["stale"] == 0
    for title in ("山风", "活动甲", "活动乙"):
        assert "改过以后" in await setup.text_of(title)


async def test_round_stops_asking_when_the_wiki_keeps_failing(tmp_path: Path) -> None:
    setup = await make_setup(tmp_path, max_drop_ratio=1.0)
    await setup.round()
    titles = [f"新活动{n}" for n in range(12)]
    for title in titles:
        setup.site.edit(title, f"{title}的说明：这是一个新上线的活动，奖励和规则都写在这里。")
    setup.site.broken |= set(titles)

    # wiki 出故障了：试了五六个都不成，剩下的不再发（同时最多有两个请求在路上，所以可能多发一个）
    record = await setup.round()
    attempts = [asked for asked in setup.site.asked if asked.startswith("parse")]
    assert 5 <= len(attempts) <= 6
    # 这些都是新页面，没有旧内容可用：这一轮先不进库，记着下一轮再取
    assert (record["kb"]["chunks"], record["kb"]["failed"]) == (3, 12)
    assert setup.state.kb_pending
    setup.site.broken.clear()
    record = await setup.round()  # wiki 上没有新的改动，但上一轮没取到的接着取
    assert (record["kb"]["chunks"], record["kb"]["failed"]) == (15, 0)
    assert not setup.state.kb_pending


# ---------- 闸门 ----------


async def test_nothing_is_done_on_a_machine_without_the_files(tmp_path: Path) -> None:
    site, settings = make_site(), make_settings(tmp_path)
    env = SyncEnv(settings, None, PROVIDER, wiki_transport=httpx.MockTransport(site))
    record = await sync_once(env)
    assert record["result"] == RESULT_PROBLEM and "kb-seed" in record["problems"][0]
    assert site.asked == []  # 不会自己从头全量取数
    assert not (settings.output_dir / CHUNKS_FILE).exists()

    env.provider = None
    assert "嵌入接口" in (await sync_once(env))["problems"][0]


async def test_a_requested_build_fetches_everything_then_it_is_incremental(tmp_path: Path) -> None:
    # 请求数的上限只管平常的增量：从头建不受它限制
    site, settings = make_site(), make_settings(tmp_path, max_requests=3, full_every_days=0)
    site.put_data("Data:Skin.json", SKINS)
    site.files = {"Skin_1_01.png": png((200, 30, 30)), "Skin_1_02.png": png((30, 30, 200))}
    store, images, embedded = MemoryKnowledgeStore(), MemoryImageStore(), []
    transport = httpx.MockTransport(site)
    env = SyncEnv(
        settings=settings,
        images=IMAGES,
        provider=PROVIDER,
        targets=Targets(store, images, MemoryStatusStore()),
        wiki_transport=transport,
        embed_transport=fake_embedder(embedded),
        cdn_transport=transport,
    )
    out = settings.output_dir
    assert kb_state(out)["state"] == KB_NONE
    # 管理后台的「Wiki」页里点了「建知识库」
    request_build(out)
    assert kb_state(out)["state"] == KB_REQUESTED

    # wiki 正好整个连不上：没做完，请求留着，下一轮接着做
    env.wiki_transport = httpx.MockTransport(lambda _request: httpx.Response(502))
    record = await sync_once(env)
    assert record["result"] == RESULT_PROBLEM and "下一轮接着做" in record["problems"][0]
    assert kb_state(out)["state"] == KB_BUILDING and "下一轮接着做" in kb_state(out)["problem"]
    assert (await store.info()).chunks == 0
    assert not cancel_build(out)  # 已经开始建的不能撤回

    env.wiki_transport = transport
    record = await sync_once(env)
    assert record["result"] == RESULT_UPDATED
    assert record["build"] == {"documents": 3, "chunks": 3, "failed": 0, "images": 2}
    assert len(site.asked) > 3  # 不受一轮三个请求的上限限制
    # 切片和图片都算了向量、进了库，不过闸门（全是新的）
    assert (await store.info()).with_vectors == 3 and (await images.info()).with_vectors == 2
    assert kb_state(out) == {
        "state": KB_READY,
        "requested_at": None,
        "started_at": None,
        "problem": "",
    }
    assert read_build(out) is None

    # 以后就是普通的增量：wiki 没有新的改动，一个请求
    site.changes.clear()
    site.asked.clear()
    quiet = await sync_once(env)
    assert site.asked == ["changes"] and quiet["result"] == RESULT_UNCHANGED

    # 文件已经有了再点：不会重来一遍
    request_build(out)
    assert kb_state(out)["state"] == KB_READY
    site.asked.clear()
    assert "build" not in await sync_once(env) and site.asked == ["changes"]
    assert read_build(out) is None


async def test_a_big_drop_is_not_written_until_someone_forces_it(tmp_path: Path) -> None:
    setup = await make_setup(tmp_path)
    await setup.round()
    chunks = setup.settings.output_dir / CHUNKS_FILE
    before = chunks.read_bytes()

    setup.site.remove("活动甲")
    setup.site.remove("活动乙")
    record = await setup.round()
    assert record["result"] == RESULT_PROBLEM and "少了 67%" in record["problems"][0]
    assert chunks.read_bytes() == before and (await setup.store.info()).chunks == 3
    assert setup.state.kb_pending and "少了 67%" in setup.state.kb_problem
    assert (await setup.env.targets.status.read())["problems"]["kb"]

    # 还是拦着（每一轮都会再试）；看过确实是 wiki 上删了，强制做一次
    assert (await setup.round())["result"] == RESULT_PROBLEM
    record = await setup.round(force=True)
    assert record["result"] == RESULT_UPDATED and record["kb"]["deleted"] == 2
    assert (await setup.store.info()).chunks == 1 and setup.state.kb_problem == ""


async def test_too_many_vectors_to_compute_or_a_foreign_fingerprint_stops_before_loading(
    tmp_path: Path,
) -> None:
    # 向量文件没拷过来的样子：切片都在，一个向量都没有
    setup = await make_setup(tmp_path, max_embed_chunks=2)
    store_dir = setup.settings.vectors_dir / PROVIDER.name
    for name in ("index.jsonl", "vectors.f32"):
        (store_dir / name).unlink()
    record = await setup.round()
    assert record["result"] == RESULT_PROBLEM and "3 片要新算向量" in record["problems"][0]
    assert setup.embedded == [] and (await setup.store.info()).chunks == 0  # 没算，也没导入

    record = await setup.round(force=True)
    assert len(setup.embedded) == 3 and record["kb"]["upserted"] == 3

    # 库里的向量是另一种嵌入接口算的：导进去会把它们清掉，所以不导
    other = MemoryKnowledgeStore()
    chunk = KbChunk(chunk_id="c", doc_key="d", kind="entry", title="t", text="t\n\n正文")
    await other.sync([(chunk, [1.0] + [0.0] * 7)], fingerprint="other:m:8")
    setup.env.targets = Targets(other, MemoryImageStore(), MemoryStatusStore())
    record = await setup.round()
    assert record["result"] == RESULT_PROBLEM and "other:m:8" in record["problems"][0]
    assert [c.chunk_id for c in await other.get(["c"])] == ["c"]


async def test_only_one_round_runs_at_a_time(tmp_path: Path) -> None:
    setup = await make_setup(tmp_path)
    with _locked(setup.settings.cache_dir), pytest.raises(Busy):
        await setup.round()
    assert (await setup.round())["result"] == RESULT_UPDATED


# ---------- 全部重取 ----------


def at(hour: int, *, day: int = 12) -> float:
    # 年份要比跑测试的日子晚：第一轮从缓存文件的时间（真实的）往后读最近更改，早于它的改动读不到
    return time.mktime((2029, 11, day, hour, 0, 0, 0, 0, -1))


def test_full_refresh_waits_for_the_window_the_interval_and_the_wiki_cache() -> None:
    cfg = (
        SyncSettings()
    )  # 03:00–06:00，七天一次，公共数据集改了满 24 小时做一次，两次至少隔 20 小时
    stamp = lambda moment: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))  # noqa: E731
    never = SyncState()
    assert full_due(never, cfg, at(4)) and not full_due(never, cfg, at(12))
    assert not full_due(never, cfg, at(6)) and full_due(never, cfg, at(3))

    done = SyncState(full_at=at(4, day=11))
    assert not full_due(done, cfg, at(4))  # 昨天刚做过，wiki 也没动静
    assert full_due(SyncState(full_at=at(4, day=5)), cfg, at(4))  # 七天了
    assert not full_due(
        SyncState(full_at=at(4, day=5)), dataclasses.replace(cfg, full_every_days=0), at(4)
    )

    changed = dataclasses.replace(done, shared_data_at=stamp(at(1, day=11)))
    assert full_due(changed, cfg, at(4))  # 公共数据集 27 小时前改过
    assert not full_due(changed, dataclasses.replace(cfg, full_on_shared_data=False), at(4))
    recent = dataclasses.replace(done, shared_data_at=stamp(at(1)))
    assert not full_due(recent, cfg, at(4))  # 3 小时前才改：wiki 自己的缓存还没换
    hurried = SyncState(full_at=at(22, day=11), shared_data_at=stamp(at(1, day=11)))
    assert not full_due(hurried, cfg, at(4))  # 6 小时前才做过一次
    assert full_due(
        dataclasses.replace(hurried, full_started_at=at(22, day=11)), cfg, at(4)
    )  # 没做完的接着做

    anytime = dataclasses.replace(cfg, full_window=None)
    assert full_due(never, anytime, at(12))
    night = dataclasses.replace(cfg, full_window=parse_window("23:00-05:00"))
    assert full_due(never, night, at(23)) and full_due(never, night, at(4))
    assert not full_due(never, night, at(12))


async def test_full_refresh_runs_at_night_and_covers_shared_data_that_has_settled(
    tmp_path: Path,
) -> None:
    setup = await make_setup(tmp_path)
    now = at(12)
    setup.env.clock = lambda: now
    await setup.round()
    assert setup.state.full_at == 0  # 白天不做

    setup.site.now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at(13)))
    setup.site.changed(3500, "Data:Gift.json")  # 公共数据集：说不清哪些页面用了它
    now = at(14)
    record = await setup.round()
    assert setup.site.asked == ["changes"] and record["result"] == RESULT_UNCHANGED
    assert setup.state.shared_data_at == setup.site.now

    # 当天夜里：从来没全部重取过，做一次。
    # 但数据集才改了十几个小时，wiki 的缓存还没换，这一次不算盖住了它
    now = at(4, day=13)
    record = await setup.round()
    assert record["kb"]["renders"] == {RENDER_REFRESH: 3} and record["kb"]["full"]
    assert setup.state.full_at == now and setup.state.shared_data_at == setup.site.now
    assert not (await setup.round()).get("kb")  # 同一个时段里的下一轮不再做

    # 再过一夜：改动满 24 小时了，再做一次，这回盖住了
    now = at(4, day=14)
    setup.site.pages["活动甲"]["html"] = html(
        "活动甲的说明：奖励里多了一个新道具（数据集里加的）。"
    )
    record = await setup.round()
    assert record["kb"]["renders"] == {RENDER_REFRESH: 3} and record["result"] == RESULT_UPDATED
    assert "新道具" in await setup.text_of("活动甲")
    assert setup.state.full_at == now and setup.state.shared_data_at == ""


async def test_interrupted_full_refresh_resumes_without_refetching_what_it_got(
    tmp_path: Path,
) -> None:
    setup = await make_setup(tmp_path, full_window=None)  # 不限时段：第一轮就全部重取
    setup.site.broken.add("活动乙")
    record = await setup.round()
    assert record["kb"]["stale"] == 1 and record["kb"]["renders"] == {RENDER_REFRESH: 2}
    state = setup.state
    assert state.full_at == 0 and state.full_started_at > 0

    setup.site.broken.clear()
    record = await setup.round()
    assert setup.site.asked == ["changes", *LISTING, "parse 活动乙"]  # 上一轮取到的两页不再取
    assert record["kb"]["renders"] == {RENDER_REFRESH: 1}
    assert setup.state.full_at > 0 and setup.state.full_started_at == 0
    assert "kb" not in await setup.round()  # 做完了，下一轮不再做


# ---------- 图库 ----------


def png(color: tuple[int, int, int]) -> bytes:
    out = BytesIO()
    Image.new("RGB", (40, 40), color).save(out, format="PNG")
    return out.getvalue()


SKINS = {
    "1": {
        "id": 1,
        "name": "山风",
        "skins": [
            {"skin_id": "01", "skin_name": "默认"},
            {"skin_id": "02", "skin_name": "觉醒", "skin_way": "觉醒后获得"},
            {"skin_id": "03", "skin_name": "青岚"},
        ],
    }
}
IMAGES = ImageSettings(max_side=32, jpeg_quality=80, embed_batch_size=2)


async def test_images_follow_the_wiki_and_only_new_ones_are_downloaded(tmp_path: Path) -> None:
    setup = await make_setup(tmp_path)
    site, settings = setup.site, setup.settings
    site.put_data("Data:Skin.json", SKINS)
    site.files = {"Skin_1_01.png": png((200, 30, 30)), "Skin_1_02.png": png((30, 30, 200))}
    # 图库以前建好了：记录和向量都在，原图没有带过来（服务器上就是这样）
    files = {
        name: WikiFile(name=name, url=f"https://cdn.test/{name}", sha1=sha1(content))
        for name, content in site.files.items()
    }
    env = settings.sites.require("huiji_yys").env(settings)  # type: ignore[attr-defined]
    catalog = build_catalog(files, CatalogData(character_index=INDEX, skin=SKINS), env)
    images_path = settings.output_dir / IMAGES_FILE
    store_dir = settings.output_dir / "vectors" / vectors_dirname(PROVIDER)
    write_images(images_path, catalog.images)
    for image in catalog.images:
        path = original_path(settings.cache_dir, image)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(site.files[image.image_id])
    await embed_image_files(
        PROVIDER, images_path, store_dir, settings.cache_dir, IMAGES, transport=fake_embedder([])
    )
    for image in catalog.images:
        original_path(settings.cache_dir, image).unlink()
    site.changes.clear()
    setup.env.images = IMAGES
    image_store = setup.env.targets.images

    # 第一轮：对一次清单（没有变化），把现成的记录和向量导入库里；一张原图都不用下
    record = await setup.round()
    assert [a for a in site.asked if a.startswith("download")] == [] and setup.embedded == []
    assert record["images"]["images"] == 2 and record["images"]["upserted"] == 2
    assert (await image_store.info()).with_vectors == 2
    assert "files Skin_" in site.asked

    # wiki 上传了一张新皮肤的立绘：只下载、只算这一张
    site.files["Skin_1_03.png"] = png((30, 200, 30))
    site.changed(6, "文件:Skin 1 03.png", kind="log", log="upload")
    record = await setup.round()
    assert [a for a in site.asked if a.startswith("download")] == ["download Skin_1_03.png"]
    assert setup.embedded == ["<image>"]
    assert record["images"] == {
        "images": 3,
        "stale": 0,
        "downloaded": 1,
        "embedded": 1,
        "upserted": 1,
        "deleted": 0,
        "without_vectors": 0,
    }
    assert record["result"] == RESULT_UPDATED and record["changes"] == {"图库的文件": 1}
    assert [image.label for image in read_images(images_path)][-1] == "山风的皮肤「青岚」"
    assert (await image_store.info()).with_vectors == 3

    # 无关的文件传上来不用对图库；没到一天也不用
    site.changed(6, "文件:1 kol 20261106.png", kind="log", log="upload")
    assert "images" not in await setup.round() and site.asked == ["changes"]

    # 清单一下子少了一大截（接口出了岔子）：不写记录、不动库
    del site.files["Skin_1_02.png"], site.files["Skin_1_03.png"]
    site.changed(6, "文件:Skin 1 03.png", kind="log", log="delete")
    record = await setup.round()
    assert record["result"] == RESULT_PROBLEM and "图片从 3 变成了 1" in record["problems"][0]
    assert len(read_images(images_path)) == 3 and (await image_store.info()).images == 3
    assert setup.state.images_pending


# ---------- 配置、状态、命令行 ----------


def test_settings_come_from_the_config_file_and_the_data_directory_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    settings = load_settings(config)
    sync = settings.sync
    assert sync.enabled and sync.interval_seconds == 3600 and sync.full_window == (180, 360)
    assert settings.sites.ids == ("huiji", "huiji_yys")
    yys = settings.sites.require("huiji_yys")
    assert isinstance(yys, YysPlugin)
    assert yys.cfg.page_data == ("Data:Character/{id}.json", "Data:Skilldiff/{id}.json")
    assert (sync.full_every_days, sync.full_settle_hours, sync.max_requests) == (7, 24, 600)
    # 知识库目录下一个 wiki 一层（名字是 core.wiki.yaml 里这个 wiki 的 id），缓存在它里面
    assert settings.output_dir.name == "huiji" and settings.output_dir.parent.name == "kb"
    assert settings.cache_dir == settings.output_dir / "cache"

    monkeypatch.setenv(KB_DIR_ENV, str(tmp_path))
    moved = load_settings(config)
    assert moved.output_dir == tmp_path.resolve() / "huiji"
    assert moved.cache_dir == moved.output_dir / "cache"

    assert parse_window("") is None and parse_window("23:30-05:00") == (1410, 300)
    for bad in ("3点到6点", "03:00", "03:00-03:00", "25:00-26:00"):
        with pytest.raises(ValueError):
            parse_window(bad)
    # 写得再离谱也有上下限：这是别人的站
    wild = load_config()
    wild.files["plugin.knowledge"] = config.plugin("knowledge") | {
        "sync": {"interval_minutes": 0.1, "limits": {"requests": 10**6}}
    }
    assert load_settings(wild).sync.interval_seconds == 300
    assert load_settings(wild).sync.max_requests == 2000


def test_state_survives_a_restart_and_the_loop_reports_whether_it_is_alive(tmp_path: Path) -> None:
    state = SyncState(cursor_timestamp="2030-01-01T00:00:00Z", cursor_rcid=7, fresh_titles=["山风"])
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    (tmp_path / "sync_state.json").write_text('{"cursor_rcid": 3, "以后加的字段": 1}')
    assert load_state(tmp_path).cursor_rcid == 3  # 不认识的字段不碍事
    (tmp_path / "sync_state.json").write_text("写到一半")
    assert load_state(tmp_path) == SyncState()

    # 更新进程只有一个，轮流更新各个 wiki：心跳在知识库目录下，不在哪个 wiki 的目录里
    root = tmp_path / "kb"
    assert not healthy(root)
    root.mkdir()
    (root / HEARTBEAT_FILE).touch()
    assert healthy(root) and not healthy(root, now=time.time() + 3600)


def test_command_line_shows_status_without_touching_the_wiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KB_DIR_ENV, str(tmp_path))
    assert main(["sync", "--status"]) == 0
    assert "还没跑过" in capsys.readouterr().err
    assert main(["sync", "--health"]) == 1  # 没有在跑


@pytest.fixture
async def postgres() -> AsyncIterator[Database]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    try:
        yield db
    finally:
        await db.pool.execute("DELETE FROM kb_meta WHERE key = 'sync'")
        await db.close()


async def test_status_is_kept_in_the_database(postgres: Database) -> None:
    store = PostgresStatusStore(postgres)
    assert await store.read() == {}
    await store.write({"checked_at": "2030-01-01T00:00:00+0800", "loaded": {"kb": "版本一"}})
    await store.write({"checked_at": "2030-01-01T01:00:00+0800", "loaded": {"kb": "版本二"}})
    assert (await store.read())["loaded"] == {"kb": "版本二"}
