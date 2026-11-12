"""知识库的站点插件：通用的代码对任何 MediaWiki 都能用，平台的东西核心带着，站点特有的由插件带进来。

- 一个插件都不装：一个普通的 MediaWiki（接口在 ``/w/api.php``，没有灰机的 Data 接口）
  能跑完预处理、出报告、做自动更新；
- 插件怎么加载：``core.wiki.yaml`` 的 ``site`` 写站点插件的 id，它的平台跟着加载；
- 插件对通用配置的补充（它 ``config.yaml`` 里的 ``extend``）怎么并进去；
- 自己写的插件（一个包的路径）能插手取数、建文档、标注、报告、自动更新。

查询那一摊的站点插件见 test_wiki_sites.py，插件的格式和清单见 test_plugin_spec.py。
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jianshanagent.core.config import load_config, locked_changes, overlay
from jianshanagent.core.embedding import EmbeddingProvider
from jianshanagent.platforms.huiji.knowledge import HuijiKnowledge
from jianshanagent.plugins.knowledge.changes import Change, plan_changes
from jianshanagent.plugins.knowledge.cli import main
from jianshanagent.plugins.knowledge.embed import embed_chunks
from jianshanagent.plugins.knowledge.fetcher import WikiFetcher
from jianshanagent.plugins.knowledge.image_store import MemoryImageStore
from jianshanagent.plugins.knowledge.pipeline import run_pipeline
from jianshanagent.plugins.knowledge.report import build_report
from jianshanagent.plugins.knowledge.settings import (
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
)
from jianshanagent.plugins.knowledge.sink import CHUNKS_FILE, JsonlSink
from jianshanagent.plugins.knowledge.site import SiteError, SiteSet
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore
from jianshanagent.plugins.knowledge.sync import (
    RESULT_UPDATED,
    MemoryStatusStore,
    SyncEnv,
    Targets,
    sync_once,
)
from jianshanagent.testing.sites import one_wiki, site_packages, write_site

from jianshanagent_site_huiji_yys.knowledge import YysPlugin, moved_keys

PROVIDER = EmbeddingProvider(
    name="fake", base_url="http://embed.test/v1", model="m", dimensions=8, max_retries=0
)


# ---------- 一个普通的 MediaWiki ----------


class PlainWiki:
    """假的普通 MediaWiki：只有 ``/w/api.php``。别的地址（比如灰机的 Data 接口）一律算错。"""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.changes: list[dict[str, Any]] = []
        self.asked: list[str] = []
        self._ids = itertools.count(1)
        self._revs = itertools.count(100)

    def edit(self, title: str, html: str) -> None:
        page = self.pages.setdefault(title, {"id": next(self._ids)})
        page |= {"rev": next(self._revs), "html": f'<div class="mw-parser-output">{html}</div>'}
        self.changes.append(
            {
                "type": "edit",
                "ns": 0,
                "title": title,
                "timestamp": "2030-01-02T00:00:00Z",
                "rcid": len(self.changes) + 1,
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/w/api.php", f"普通的 MediaWiki 没有这个地址：{request.url}"
        assert "X-authkey" not in request.headers
        params = dict(request.url.params)
        if params.get("action") == "parse":
            title = next(t for t, p in self.pages.items() if str(p["rev"]) == params["oldid"])
            self.asked.append(f"parse {title}")
            return httpx.Response(200, json={"parse": {"text": self.pages[title]["html"]}})
        if params.get("list") == "recentchanges":
            self.asked.append("changes")
            found = [c for c in self.changes if c["timestamp"] >= params["rcstart"]]
            return httpx.Response(200, json={"query": {"recentchanges": found}})
        if params.get("generator") == "allpages":
            self.asked.append(f"list {params['gapnamespace']}")
            listed = [
                {
                    "pageid": page["id"],
                    "title": title,
                    "ns": 0,
                    "lastrevid": page["rev"],
                    "touched": "2030-01-01T00:00:00Z",
                }
                for title, page in self.pages.items()
            ]
            return httpx.Response(200, json={"query": {"pages": listed}})
        if params.get("prop") == "revisions":  # 页面的创建时间（推断日期用）
            self.asked.append("history")
            revisions = [{"revid": 1, "timestamp": "2029-06-01T00:00:00Z"}]
            return httpx.Response(200, json={"query": {"pages": [{"revisions": revisions}]}})
        raise AssertionError(f"没有料到的请求：{params}")


def plain_settings(tmp_path: Path, sites: SiteSet | None = None) -> Settings:
    return Settings(
        wiki=WikiSettings(
            api_url="https://plain.test/w/api.php",
            site_base="https://plain.test",
            user_agent="test-agent",
            article_path="/index.php/$1",
        ),
        fetch=FetchSettings(requests_per_second=1000, max_retries=0),
        sources=SourceSettings(),
        clean=CleanSettings(),
        chunk=ChunkSettings(),
        report=ReportSettings(sample_size=2),
        dates=DateSettings(),
        sync=SyncSettings(full_window=None, full_every_days=0),
        output_dir=tmp_path / "kb",
        cache_dir=tmp_path / "kb" / "cache",
        sites=sites or SiteSet(),
    )


def plain_wiki() -> PlainWiki:
    wiki = PlainWiki()
    wiki.edit(
        "Python",
        '<div class="navbox">编程语言导航 <a href="/index.php/Rust">Rust</a></div>'
        "<p>Python 是一种解释型的通用编程语言，强调代码的可读性，用缩进来划分代码块。</p>"
        '<h2><span class="mw-headline" id="历史">历史</span></h2>'
        "<p>Python 由吉多·范罗苏姆在 1989 年底开始设计，第一个公开发行版发行于 1991 年。</p>",
    )
    wiki.edit(
        "编程语言列表",
        '<p><a href="/index.php/Python">Python 编程语言</a></p>'
        '<p><a href="/index.php/Rust">Rust 编程语言</a></p>'
        '<p><a href="/index.php/Go">Go 编程语言</a></p>',
    )
    wiki.edit("Rust", "<p>Rust 是一种注重内存安全和并发的系统编程语言，由 Mozilla 主导开发。</p>")
    wiki.edit("Go", "<p>Go 是 Google 开发的静态类型、编译型语言，自带垃圾回收和并发支持。</p>")
    return wiki


def fake_embedder() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [1.0] + [0.0] * 7} for i in range(len(items))]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": len(items)}})

    return httpx.MockTransport(handler)


async def test_a_plain_mediawiki_works_without_any_site_plugin(tmp_path: Path) -> None:
    wiki, settings = plain_wiki(), plain_settings(tmp_path)
    transport = httpx.MockTransport(wiki)
    fetcher = WikiFetcher(settings.wiki, settings.fetch, transport=transport)
    try:
        result = await run_pipeline(settings, JsonlSink(settings.output_dir), fetcher=fetcher)
    finally:
        await fetcher.aclose()

    # 只发了 MediaWiki 自己的接口：列页面、渲染；没有去取页面源码（那是站点插件认模板用的）
    assert sorted(set(wiki.asked)) == ["list 0", "parse Go", "parse Python", "parse Rust",
                                       "parse 编程语言列表"]  # fmt: skip
    by_title = {chunk.title: chunk for chunk in result.chunks}
    assert set(by_title) == {"Python", "Rust", "Go", "编程语言列表"}
    assert {chunk.kind for chunk in result.chunks} == {"entry"}
    python = by_title["Python"]
    assert "导航" not in python.text and "## 历史" in python.text  # 通用清洗照做
    assert python.url == "https://plain.test/index.php/Python"  # 页面地址按这个站的写法
    assert all(chunk.entities == [] for chunk in result.chunks)  # 没有插件标实体
    # 章节目录页：站内链接按这个站的地址前缀认
    directory = by_title["编程语言列表"]
    assert directory.meta["nav"] and "「编程语言列表」的目录（共 3 项）" in directory.text
    assert result.stats["sites"] == [] and result.stats["corpus"]["replaced_pages"] == 0

    # 报告只有核心的几节，节号连着排，互相引用的节号对得上
    report = build_report(result, settings)
    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert [int(h.split(".")[0][3:]) for h in headings] == list(range(1, len(headings) + 1))
    assert not any(word in report for word in ("实体标注", "转发帖", "Data 数据集", "\x00"))
    directories = next(h for h in headings if "章节目录页" in h).split(".")[0][3:]
    assert f"章节目录页已入库，见第 {directories} 节" in report

    # 自动更新：页面改了就重取、重算向量、导入；没有插件管图库，图库那一半不做
    await embed_chunks(
        PROVIDER,
        settings.output_dir / CHUNKS_FILE,
        settings.vectors_dir / PROVIDER.name,
        transport=fake_embedder(),
    )
    store = MemoryKnowledgeStore()
    env = SyncEnv(
        settings=settings,
        images=None,
        provider=PROVIDER,
        targets=Targets(store, MemoryImageStore(), MemoryStatusStore()),
        wiki_transport=transport,
        embed_transport=fake_embedder(),
    )
    wiki.changes.clear()
    first = await sync_once(env)  # 第一轮：wiki 没有改动，把现成的切片和向量导入库里
    assert first["kb"]["upserted"] == len(result.chunks) and first["problems"] == []
    wiki.asked.clear()
    wiki.edit("Rust", "<p>Rust 的第一个稳定版 1.0 发布于 2015 年，之后每六周发布一个新版本。</p>")
    record = await sync_once(env)
    assert record["result"] == RESULT_UPDATED and record["changes"] == {"条目": 1}
    assert record["kb"]["renders"] == {"revision": 1} and "images" not in record
    assert "每六周发布" in "\n".join(chunk.text for chunk in await store.page("Rust"))
    assert set(wiki.asked) == {"changes", "list 0", "parse Rust"}


def test_images_collect_says_so_when_no_plugin_curates_images(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    # 部署目录里放一份和插件配置同名的文件：这次部署对知识库默认配置的改动
    knowledge = {"paths": {"root": str(tmp_path / "kb")}}
    (root / "plugin.knowledge.yaml").write_text(yaml.safe_dump(knowledge), encoding="utf-8")
    (root / "core.wiki.yaml").write_text(
        yaml.safe_dump(one_wiki(api_url="https://plain.test/w/api.php")), encoding="utf-8"
    )
    settings = load_settings(load_config(root))
    # 知识库目录下一个 wiki 一层
    assert settings.output_dir == tmp_path / "kb" / "main"
    # 只写了接口地址：站点地址从它来，页面地址按 MediaWiki 的默认写法
    assert settings.wiki.site_base == "https://plain.test"
    assert settings.wiki.page_url("Main Page") == "https://plain.test/wiki/Main_Page"
    assert settings.wiki.link_prefix == "/wiki/" and len(settings.sites) == 0
    # 这个 wiki 没有图库可收：说一声就过去了（挂着几个 wiki 时，别的 wiki 照常收）
    assert main(["--config-dir", str(root), "images", "collect"]) == 0
    assert "没有站点插件管图库" in capsys.readouterr().err


# ---------- 加载 ----------


def write_config(
    root: Path, site: str = "", *, platform: str = "", **deployed: dict[str, Any]
) -> None:
    """一份最小的部署配置：``core.wiki.yaml``（地址 + 用哪个站点插件，或者只写在哪个平台上）。
    ``deployed``：部署目录里另放的、改平台默认配置的文件（``huiji`` → ``platform.huiji.yaml``）。"""
    root.mkdir(parents=True, exist_ok=True)
    wiki = one_wiki(site, api_url="https://w.test/api.php", platform=platform)
    (root / "core.wiki.yaml").write_text(yaml.safe_dump(wiki), encoding="utf-8")
    for name, raw in deployed.items():
        (root / f"platform.{name}.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8"
        )


def test_the_shipped_config_loads_huiji_then_yys_and_merges_their_rules() -> None:
    config = load_config()
    settings = load_settings(config)
    # 配置里只写了 huiji_yys；它是灰机上的站，灰机平台的那一份先加载
    assert [wiki["site"] for wiki in config.get("core.wiki")["wikis"]] == ["huiji_yys"]
    assert settings.sites.ids == ("huiji", "huiji_yys") and settings.sites.has_images
    huiji, yys = settings.sites.require("huiji"), settings.sites.require("huiji_yys")
    assert isinstance(huiji, HuijiKnowledge) and isinstance(yys, YysPlugin)
    assert huiji.data_url(settings.wiki).endswith("/api/rest_v1/namespace/data")
    assert yys.huiji is huiji and yys.cfg.images.art_prefix == "Skin_"
    assert [post.kind for post in yys.cfg.sources.posts if post.enabled] == ["dashen"]

    # 清洗规则是三份合起来的：MediaWiki 通用项、灰机的约定、这个站的组件。
    # 平台和站点插件的补充读配置时就并进 plugin.knowledge 了
    clean = settings.clean
    assert {"navbox", "mw-editsection", "kb-ignore", "huiji-tt-preload", "shiki-nav"} <= (
        clean.remove_classes
    )
    assert clean.heading_classes["skill-name"] == 1 and clean.table_placeholders == {"/"}
    assert "首页" in settings.sources.exclude_titles
    assert settings.dates.min_year == 2016 and settings.dates.exclude_prefixes == ("首页/",)
    assert settings.report.acceptance_page == "为崽而战·浮世之航"
    merged = config.plugin("knowledge")
    assert "shiki-nav" in merged["clean"]["remove_classes"]
    assert not {"entities", "datasets"} & set(merged) and "page_data" not in merged["sync"]
    assert moved_keys(merged) == []
    # 仓库里核心自己的那份文件里没有站点的东西
    source = config.source_path("plugin.knowledge")
    assert source.name == "config.yaml" and source.parent.name == "knowledge"  # 在插件自己的目录里
    own = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert "shiki-nav" not in own["clean"]["remove_classes"]
    assert own["clean"]["remove_classes"][0] == "navbox" and "min_year" not in own["dates"]


def test_the_site_named_in_the_config_brings_its_platform_and_wrong_names_are_explained(
    tmp_path: Path,
) -> None:
    write_config(tmp_path / "none")
    bare = load_settings(load_config(tmp_path / "none"))
    assert len(bare.sites) == 0 and not bare.sites.has_images
    # 配置里什么清洗规则都没写：MediaWiki 通用的那几样照样去掉
    assert bare.clean.remove_classes >= CleanSettings().remove_classes >= {"navbox", "toc"}
    assert "kb-ignore" not in bare.clean.remove_classes  # 灰机的约定：不在灰机上的 wiki 不生效

    # 没有站点插件、只写了在灰机上；部署目录里同名的文件改平台的默认配置
    write_config(tmp_path / "huiji", platform="huiji", huiji={"data_namespace": 3600})
    only_huiji = load_settings(load_config(tmp_path / "huiji"))
    assert only_huiji.sites.ids == ("huiji",) and "kb-ignore" in only_huiji.clean.remove_classes
    assert only_huiji.sites.require("huiji").data_namespace == 3600  # type: ignore[attr-defined]
    assert "shiki-nav" not in only_huiji.clean.remove_classes  # 阴阳师的规则不生效

    # 写站点插件就够：它的平台自动带出来，排在前面
    write_config(tmp_path / "yys", "huiji_yys")
    config = load_config(tmp_path / "yys")
    assert (config.platforms, config.sites) == (["huiji"], ["huiji_yys"])
    assert load_settings(config).sites.ids == ("huiji", "huiji_yys")

    write_config(tmp_path / "typo", "huiji_yyss")
    with pytest.raises(SiteError, match="没有叫「huiji_yyss」的站点插件（装着的有：huiji_yys"):
        load_config(tmp_path / "typo")
    # 老写法（只能挂一个 wiki 时的）：说清楚该怎么改
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "core.wiki.yaml").write_text(
        "api_url: https://w.test/api.php\nsite: huiji_yys\n", encoding="utf-8"
    )
    with pytest.raises(
        ValueError, match="最外层的 api_url、site 不用了.*每个 wiki 是 wikis 里的一项"
    ):
        load_config(tmp_path / "old")
    # 不知道是哪个 wiki 时不猜
    (tmp_path / "nowhere").mkdir()
    (tmp_path / "nowhere" / "core.wiki.yaml").write_text("wikis: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不知道是哪个 wiki"):
        load_settings(load_config(tmp_path / "nowhere"))


def test_site_rules_are_added_to_the_generic_ones_and_single_values_come_from_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generic = {
        "clean": {"remove_classes": ["a", "b"], "heading_classes": {"x": 1}},
        "dates": {"min_year": 2000, "lead_chars": 300},
    }
    site = {
        "clean": {"remove_classes": ["b", "c"], "heading_classes": {"y": 2}, "tabber_class": "t"},
        "dates": {"min_year": 2016},
    }
    assert overlay(generic, site) == {
        "clean": {
            "remove_classes": ["a", "b", "c"],  # 名单合在一起，不重复
            "heading_classes": {"x": 1, "y": 2},
            "tabber_class": "t",
        },
        "dates": {"min_year": 2016, "lead_chars": 300},  # 单个值以站点插件给的为准
    }
    assert generic["clean"]["remove_classes"] == ["a", "b"]  # 不动传进来的

    # 读配置时就并好了：只并用到的那个插件的；
    # 部署目录里同名的文件、local、覆写目录还能再盖
    sites = site_packages(tmp_path, monkeypatch, "demo_sites")
    two = write_site(
        sites / "two",
        capabilities=("extend",),
        config={
            "extend": {"plugin.demo": {"words": ["甲", "乙"], "label": "乙", "size": 2}},
            "own": 1,
        },
    )
    write_site(
        sites / "three",
        capabilities=("extend",),
        config={"extend": {"plugin.demo": {"words": ["没用到"]}}},
    )
    root, overrides = tmp_path / "config", tmp_path / "overrides"
    (root / "local").mkdir(parents=True)
    overrides.mkdir()

    def write(directory: Path, name: str, data: dict[str, Any]) -> None:
        text = yaml.safe_dump(data, allow_unicode=True)
        (directory / f"{name}.yaml").write_text(text, encoding="utf-8")

    write(root, "core.wiki", one_wiki("demo_sites.two"))
    write(root, "plugin.demo", {"words": ["通用"], "label": "通用", "size": 1, "keep": True})
    config = load_config(root, overrides)
    assert config.sites == ["two"]
    assert config.site("two") == config.get("site.two")
    assert config.site("two")["own"] == 1 and "site.three" not in config.files
    assert config.source_path("site.two") == two / "config.yaml"
    assert config.plugin("demo") == {
        "words": ["通用", "甲", "乙"],
        "label": "乙",
        "size": 2,
        "keep": True,
    }
    write(root, "site.two", {"own": 5})  # 部署目录里同名的文件：改插件的默认配置
    write(root / "local", "plugin.demo", {"size": 3})
    write(overrides, "plugin.demo", {"label": "后台改的"})
    config = load_config(root, overrides)
    assert config.plugin("demo") | {"words": 0} == {
        "words": 0,
        "label": "后台改的",
        "size": 3,
        "keep": True,
    }
    assert config.site("two")["own"] == 5 and "extend" in config.site("two")
    assert config.overrides == {"plugin.demo": {"label": "后台改的"}}  # 覆写是和并好的默认值比的
    # 补充里写了不存在的配置：读配置时就报错，不悄悄不生效
    write_site(
        sites / "bad",
        capabilities=("extend",),
        config={"extend": {"plugin.nope": {"x": 1}}},
    )
    write(root, "core.wiki", one_wiki("demo_sites.bad"))
    with pytest.raises(ValueError, match="plugin.nope"):
        load_config(root)
    # 配置里有补充、清单里却没写这个能力：也报错（清单要说实话）
    write_site(sites / "sly", config={"extend": {"plugin.demo": {"size": 9}}})
    write(root, "core.wiki", one_wiki("demo_sites.sly"))
    with pytest.raises(ValueError, match="capabilities 里没有写 extend"):
        load_config(root)


def test_settings_left_in_the_old_place_are_pointed_out() -> None:
    old = {
        "entities": {"enabled": True},
        "sources": {"posts": [{"prefix": "微信/", "kind": "wechat"}], "pages": {"namespace": 0}},
        "sync": {"page_data": ["Data:Character/{id}.json"], "interval_minutes": 60},
        "images": {"top_k": 3, "art_prefix": "Skin_"},
    }
    assert moved_keys(old) == [
        ("entities", "entities"),
        ("sources.posts", "posts"),
        ("sync.page_data", "page_data"),
        ("images.art_prefix", "images.art_prefix"),
    ]


def test_which_plugins_load_cannot_be_changed_from_the_admin_page() -> None:
    # 挂哪几个 wiki（里面有地址和用哪个站点插件）整段锁着；这个 wiki 叫什么能改
    evil = one_wiki("evil.module", api_url="https://evil.test/api.php")
    assert locked_changes("core.wiki", {**evil, "name": "x"}) == ["wikis"]
    assert locked_changes("core.wiki", {"name": "x"}) == []
    assert locked_changes("plugin.knowledge", {"sync": {"interval_minutes": 30}}) == []
    # 站点插件对通用配置的补充读配置时就并进去了，在网页上改它不会生效：锁着。它自己的设置能改
    change = {"extend": {"plugin.chat": {}}, "knowledge": {"posts": []}}
    assert locked_changes("site.huiji_yys", change) == ["extend"]


# ---------- 自己写的插件 ----------

CUSTOM_PLUGIN = '''
from jianshanagent.plugins.knowledge.models import BLOCK_PARA, KIND_DATA, Block, Document
from jianshanagent.plugins.knowledge.site import SitePlugin, Verdict


class Glossary(SitePlugin):
    """把配置里写的术语表做成一篇文档；给提到术语的切片打标记；认「术语」命名空间的改动。"""

    id = "glossary"

    def __init__(self, setup):
        raw = setup.raw.get("knowledge") or {}
        self.terms = dict(raw.get("terms") or {})
        self.namespace = int(raw.get("namespace") or 0)

    def build_documents(self, ctx):
        blocks = [Block(BLOCK_PARA, text=f"{name}：{text}") for name, text in self.terms.items()]
        doc = Document(
            doc_key="data:glossary",
            kind=KIND_DATA,
            title="术语表",
            url=ctx.settings.wiki.page_url("术语表"),
            source="数据·术语表",
            blocks=blocks,
            meta={"category": "glossary"},
        )
        ctx.built.documents.append(doc)

    def annotate(self, ctx):
        for chunk in ctx.chunks:
            chunk.entities = [
                {"name": name, "type": "术语", "certain": True}
                for name in self.terms
                if name in chunk.text
            ]

    def stats(self, ctx):
        return {"glossary": {"terms": len(self.terms)}}

    def report(self, slot, out, result, settings):
        if slot == "end":
            out.section("术语表")
            out.add(f"共 {result.stats['glossary']['terms']} 个术语。")

    def change_classifier(self, ctx):
        def classify(change, plan):
            if change.namespace != self.namespace:
                return None
            plan.kb = True
            return Verdict("术语")

        return classify


def create(setup):
    return Glossary(setup)
'''


async def test_a_plugin_of_ones_own_is_loaded_by_module_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 站点插件是一个目录：清单、默认配置、接扩展点的模块（knowledge）都在里面
    sites = site_packages(tmp_path, monkeypatch, "my_sites")
    write_site(
        sites / "glossary",
        capabilities=("knowledge", "extend"),
        config={
            "knowledge": {
                "terms": {"所有权": "Rust 里每个值都有唯一的所有者。"},
                "namespace": 3000,
            },
            # 这个站自己的清洗规则，并进通用的
            "extend": {"plugin.knowledge": {"clean": {"remove_classes": ["site-banner"]}}},
        },
        knowledge=CUSTOM_PLUGIN,
    )
    write_config(tmp_path / "config", "my_sites.glossary")
    loaded = load_settings(load_config(tmp_path / "config"))
    assert loaded.sites.ids == ("glossary",)
    assert {"site-banner", "navbox"} <= loaded.clean.remove_classes

    wiki = plain_wiki()
    wiki.edit(
        "Rust",
        '<div class="site-banner">站点公告</div>'
        "<p>Rust 靠所有权规则在编译的时候就保证内存安全，不需要垃圾回收。</p>",
    )
    settings = dataclasses.replace(plain_settings(tmp_path, loaded.sites), clean=loaded.clean)
    fetcher = WikiFetcher(settings.wiki, settings.fetch, transport=httpx.MockTransport(wiki))
    try:
        result = await run_pipeline(settings, JsonlSink(settings.output_dir), fetcher=fetcher)
    finally:
        await fetcher.aclose()
    by_title = {chunk.title: chunk for chunk in result.chunks}
    assert by_title["术语表"].kind == "data" and "唯一的所有者" in by_title["术语表"].text
    assert "站点公告" not in by_title["Rust"].text
    assert by_title["Rust"].entities == [{"name": "所有权", "type": "术语", "certain": True}]
    assert by_title["Go"].entities == []
    assert result.stats["glossary"] == {"terms": 1} and result.stats["sites"] == ["glossary"]
    report = build_report(result, settings)
    assert report.rstrip().endswith("共 1 个术语。") and "术语表\n" in report

    changes = [
        Change(1, "2030-01-01T00:00:00Z", 3000, "术语:所有权", "edit"),
        Change(2, "2030-01-01T00:00:01Z", 2, "用户:某人", "edit"),
    ]
    plan = plan_changes(changes, settings)
    assert plan.kb and plan.counts == {"术语": 1, "其它": 1} and plan.titles == ["术语:所有权"]
