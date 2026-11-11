"""wiki 图片索引：选图和记录、取清单、下载原图、算向量、存储、导入、以图搜图。不联网。"""

import base64
import dataclasses
import hashlib
import json
import math
import os
from array import array
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingError, EmbeddingProvider
from jianshanagent.core.images import ImageError, normalize_image
from jianshanagent.core.plugin import PluginContext
from jianshanagent.plugins.knowledge.fetcher import DiskCache, WikiFetcher
from jianshanagent.plugins.knowledge.image_collect import (
    WikiFile,
    download_originals,
    original_path,
    write_images,
)
from jianshanagent.plugins.knowledge.image_embed import (
    embed_image_files,
    load_images_into,
    vectors_dirname,
)
from jianshanagent.plugins.knowledge.image_store import (
    ImageStore,
    KbImage,
    MemoryImageStore,
    PostgresImageStore,
)
from jianshanagent.plugins.knowledge.images import ImageHit, ImageSearch, ImageSettings
from jianshanagent.plugins.knowledge.plugin import KnowledgePlugin
from jianshanagent.plugins.knowledge.settings import (
    ChunkSettings,
    CleanSettings,
    DateSettings,
    FetchSettings,
    ReportSettings,
    Settings,
    SourceSettings,
    WikiSettings,
)
from jianshanagent.plugins.knowledge.site import SiteSet
from jianshanagent.plugins.knowledge.sources import WikiSource
from jianshanagent_site_huiji.knowledge import HuijiPlugin
from PIL import Image

from jianshanagent_site_huiji_yys.knowledge import YysPlugin
from jianshanagent_site_huiji_yys.knowledge.images import CatalogData, build_catalog, gather
from jianshanagent_site_huiji_yys.knowledge.settings import CatalogSettings, YysConfig

DIM = 4096  # 表里的向量列是 4096 维，存储的测试也用同样的维度
PROVIDER = EmbeddingProvider(
    name="t", base_url="http://embed.test/v1", model="m", dimensions=DIM, max_retries=0
)
#: 算向量、导入的测试用小维度的提供方
SMALL = EmbeddingProvider(
    name="fake", base_url="http://embed.test/v1", model="m", dimensions=8, max_retries=0
)
IMAGES = ImageSettings(
    min_score=0.5,
    sure_score=0.8,
    sure_margin=0.05,
    max_side=32,
    jpeg_quality=80,
    embed_batch_size=2,
)
HUIJI = HuijiPlugin()
YYS = YysPlugin(HUIJI, YysConfig())
SETTINGS = Settings(
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
    output_dir=Path("."),
    cache_dir=Path("."),
    sites=SiteSet([HUIJI, YYS]),
)
#: 阴阳师插件里的函数用的设置（收哪些图在里面）
ENV = YYS.env(SETTINGS)


def unit(*weights: tuple[int, float], dim: int = DIM) -> array:
    """只有指定的几维非零的单位向量。"""
    vector = array("f", bytes(4 * dim))
    norm = math.sqrt(sum(w * w for _, w in weights))
    for axis, weight in weights:
        vector[axis] = weight / norm
    return vector


def png(color: tuple[int, int, int], *, size: tuple[int, int] = (48, 40)) -> bytes:
    """一张左半边透明、右半边是 ``color`` 的 PNG（wiki 的立绘大多是透明底）。"""
    picture = Image.new("RGBA", size, (0, 0, 0, 0))
    picture.paste((*color, 255), (size[0] // 2, 0, size[0], size[1]))
    out = BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


def wiki_file(name: str, content: bytes = b"") -> WikiFile:
    return WikiFile(
        name=name,
        url=f"https://cdn.test/{name}",
        sha1=hashlib.sha1(content or name.encode()).hexdigest(),
        width=1160,
        height=1160,
    )


def image(image_id: str, content: bytes = b"", **fields: Any) -> KbImage:
    values: dict[str, Any] = {
        "kind": "皮肤立绘",
        "label": f"图 {image_id}",
        "image_url": f"https://cdn.test/{image_id}",
        "sha1": hashlib.sha1(content or image_id.encode()).hexdigest(),
    }
    return KbImage(image_id=image_id, **(values | fields))


# ---------- 选图、生成记录 ----------

SKIN_DOC = {
    "330": {
        "id": 330,
        "name": "不知火",
        "skins": [
            {"skin_id": "01", "skin_name": "默认", "skin_way": "初始皮肤"},
            {"skin_id": "02", "skin_name": "觉醒", "skin_way": "觉醒后获得"},
            {
                "skin_id": "03",
                "skin_name": "蝶步韶华",
                "skin_way": "魂玉兑换",
                "type": "典藏",
                "date": "2026.09.30",
            },
        ],
    },
    "10": {"id": 10, "name": "晴明", "skins": [{"skin_id": "01", "skin_name": "默认"}]},
}
INDEX_DOC = {"characters": [{"id": 330, "name": "不知火", "pageTitle": "不知火", "rarity": "SSR"}]}
SOUL_DOC = {
    "soul": [
        {"name": "狂骨", "suit": "攻击加成15%", "desc": "鬼火越多伤害越高。", "way": "甲<br>乙"}
    ]
}
CHARACTER_DOC = {"huijuan": [{"index": 2, "title": "初舞", "text": "自记事起，<br>我便在离岛。"}]}


def test_catalog_says_whose_picture_each_file_is() -> None:
    placeholder = b"same card"
    files = [
        wiki_file("Skin_330_01.png"),
        wiki_file("Skin_330_02.png"),
        wiki_file("Skin_330_03.png"),
        wiki_file("Skin_330_09.png"),  # 图先传了，皮肤数据里还没有 09 号
        wiki_file("S_330_01.png", placeholder),
        wiki_file("S_330_03.png", placeholder),  # 和 01 号的卡面内容一样：占位的
        wiki_file("Tx_330_s.png"),
        wiki_file("Tx_10_17.png"),  # 阴阳师；17 号皮肤数据里没有
        wiki_file("Skin_999_01.png"),  # 查不到是谁
        wiki_file("Skin_330_01_(1).png"),  # 不合规律的名字
        wiki_file("S_SSR_bg.png"),
        wiki_file("狂骨.png"),
        wiki_file("狂骨_s.png"),
        wiki_file("Hj_330_2.png"),
        wiki_file("Chabg_330.jpg"),
    ]
    data = CatalogData(
        character_index=INDEX_DOC,
        skin=SKIN_DOC,
        soul=SOUL_DOC,
        characters={"330": CHARACTER_DOC},
        known_titles=frozenset({"不知火", "阴阳师"}),
    )
    catalog = build_catalog({f.name: f for f in files}, data, ENV)
    by_id = {i.image_id: i for i in catalog.images}
    assert set(by_id) == {
        "Skin_330_01.png",
        "Skin_330_02.png",
        "Skin_330_03.png",
        "Skin_330_09.png",
        "S_330_01.png",
        "Tx_330_s.png",
        "Tx_10_17.png",
        "狂骨.png",
        "狂骨_s.png",
        "Hj_330_2.png",
        "Chabg_330.jpg",
    }
    assert {i: (by_id[i].kind, by_id[i].label) for i in by_id} == {
        "Skin_330_01.png": ("式神立绘", "不知火的初始立绘"),
        "Skin_330_02.png": ("式神立绘", "不知火的觉醒立绘"),
        "Skin_330_03.png": ("皮肤立绘", "不知火的皮肤「蝶步韶华」"),
        "Skin_330_09.png": ("皮肤立绘", "不知火的皮肤（编号 09，名字待补）"),
        "S_330_01.png": ("式神立绘", "不知火的初始立绘"),
        "Tx_330_s.png": ("式神头像", "不知火的头像"),
        "Tx_10_17.png": ("皮肤头像", "晴明的皮肤（编号 17，名字待补）头像"),
        "狂骨.png": ("御魂图标", "御魂「狂骨」"),
        "狂骨_s.png": ("御魂图标", "御魂「狂骨」"),
        "Hj_330_2.png": ("绘卷配图", "不知火的绘卷「初舞」配图"),
        "Chabg_330.jpg": ("条目背景图", "不知火的条目背景图"),
    }

    skin = by_id["Skin_330_03.png"]
    assert skin.text == (
        "这是式神「不知火」（SSR）的皮肤「蝶步韶华」的立绘。类型：典藏。获取方式：魂玉兑换。"
        "日期：2026.09.30。不知火的全部造型（3 个）：默认、觉醒、蝶步韶华。"
    )
    assert (skin.page_title, skin.url) == (
        "不知火",
        "https://w.test/wiki/%E4%B8%8D%E7%9F%A5%E7%81%AB",
    )
    assert (skin.image_url, skin.sha1) == ("https://cdn.test/Skin_330_03.png", files[2].sha1)
    assert skin.meta["character"] == "不知火" and skin.meta["skin_name"] == "蝶步韶华"
    # 立绘大图、卡面小图画的是同一个造型，头像算在初始造型里；御魂的两张图是同一个东西
    card = by_id["S_330_01.png"]
    assert card.subject == by_id["Skin_330_01.png"].subject != skin.subject
    assert by_id["Tx_330_s.png"].subject == card.subject
    assert by_id["狂骨.png"].subject == by_id["狂骨_s.png"].subject != card.subject
    # 占位的那张没有收，记在被它顶替的那张的说明里
    assert "卡面小图（立绘的半身特写）" in card.text
    assert card.text.endswith("wiki 上不知火的皮肤「蝶步韶华」的卡面小图用的也是这张图。")
    assert card.meta["shared_with"] == ["03"]
    assert "皮肤编号 09，wiki 的皮肤数据里还没有它的名字" in by_id["Skin_330_09.png"].text
    # 阴阳师没有自己的条目页；御魂检索器这一页不在已有页面里，链接退到图片自己的页面
    assert by_id["Tx_10_17.png"].page_title == "阴阳师"
    assert by_id["Tx_10_17.png"].text.startswith("这是阴阳师「晴明」的一个皮肤")
    assert by_id["狂骨.png"].url == "https://w.test/wiki/File:%E7%8B%82%E9%AA%A8.png"
    assert by_id["狂骨_s.png"].text == (
        "这是御魂「狂骨」的小图标。属性加成：攻击加成15%。套装效果：鬼火越多伤害越高。"
        "获取途径：甲；乙。"
    )
    assert by_id["Hj_330_2.png"].text == (
        "这是式神「不知火」（SSR）绘卷第 2 章「初舞」的配图。这一章的开头：自记事起，我便在离岛。"
    )

    stats = catalog.stats
    assert stats["by_kind"]["式神立绘"] == 3 and stats["images"] == 11
    assert catalog.summary() == {k: stats[k] for k in ("images", "by_kind", "subjects")}
    assert "1 个文件的编号在数据里查不到是谁的（Skin_999_01.png）" in catalog.notes[0]
    assert stats["unknown_owner"] == ["Skin_999_01.png"] and stats["duplicates"] == 1
    assert stats["unnamed_skins"] == ["Skin_330_09.png", "Tx_10_17.png"]
    assert stats["skins_without_art"] == 1  # 晴明的 01 号

    # 关掉的种类不收
    only_art = CatalogSettings(
        card_prefix="", avatar_prefix="", scroll_prefix="", background_prefix="", souls=False
    )
    env = dataclasses.replace(ENV, cfg=dataclasses.replace(ENV.cfg, images=only_art))
    assert {i.image_id[:5] for i in build_catalog(
        {f.name: f for f in files}, data, env
    ).images} == {"Skin_"}  # fmt: skip


# ---------- 取文件清单和数据文档 ----------

DATA_DOCS = {
    "Data:CharacterIndex.json": INDEX_DOC,
    "Data:Skin.json": SKIN_DOC,
    "Data:Soul.json": SOUL_DOC,
    "Data:Character/330.json": CHARACTER_DOC,
}


def file_info(name: str) -> dict[str, Any]:
    file = wiki_file(name)
    return {"name": name, "url": file.url, "sha1": file.sha1, "width": 1160, "height": 1160}


def fake_wiki(asked: list[str]) -> httpx.MockTransport:
    listed = {
        "Skin_": [["Skin_330_01.png"], ["Skin_330_03.png"]],  # 分两页给
        "S_": [["S_330_01.png", "S_SSR_bg.png"]],
        "Hj_": [["Hj_330_2.png"]],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-authkey"] == "wiki-secret"
        params = dict(request.url.params)
        if request.url.path.endswith("/namespace/data"):
            title = json.loads(params["filter"])["_id"]
            asked.append(f"data {title}")
            return httpx.Response(200, json={"_embedded": [DATA_DOCS[title]]})
        if params.get("list") == "allimages":
            pages = listed.get(params["aiprefix"], [[]])
            page = int(params.get("aicontinue", 0))
            asked.append(f"list {params['aiprefix']} {page}")
            body: dict[str, Any] = {"query": {"allimages": [file_info(n) for n in pages[page]]}}
            if page + 1 < len(pages):
                body["continue"] = {"aicontinue": str(page + 1)}
            return httpx.Response(200, json=body)
        titles = params["titles"].split("|")
        if params.get("prop") == "imageinfo":
            asked.append(f"files {params['titles']}")
            assert titles == ["File:狂骨.png", "File:狂骨_s.png"]
            pages_out = [
                {"title": "文件:狂骨.png", "missing": True},
                {"title": "文件:狂骨 s.png", "imageinfo": [file_info("狂骨_s.png")]},
            ]
            return httpx.Response(200, json={"query": {"pages": pages_out}})
        asked.append(f"titles {params['titles']}")
        found = [
            {"pageid": 100 + n, "title": title, "lastrevid": 7, "ns": 3500}
            for n, title in enumerate(titles)
        ]
        return httpx.Response(200, json={"query": {"pages": found}})

    return httpx.MockTransport(handler)


async def test_gather_lists_files_and_docs_and_reuses_them_offline(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    asked: list[str] = []
    fetcher = WikiFetcher(SETTINGS.wiki, SETTINGS.fetch, transport=fake_wiki(asked))
    files, data = await gather(WikiSource(fetcher, cache, SETTINGS), ENV, HUIJI)
    await fetcher.aclose()
    # 按前缀列的（翻了页）加上按名字查到的；wiki 上没有的（狂骨.png）不在里面
    assert sorted(files) == [
        "Hj_330_2.png",
        "S_330_01.png",
        "S_SSR_bg.png",
        "Skin_330_01.png",
        "Skin_330_03.png",
        "狂骨_s.png",
    ]
    assert files["狂骨_s.png"] == wiki_file("狂骨_s.png")
    assert (data.skin, data.soul, data.character_index) == (SKIN_DOC, SOUL_DOC, INDEX_DOC)
    assert data.characters == {"330": CHARACTER_DOC}  # 只取有绘卷配图的式神
    assert "list Skin_ 1" in asked and "data Data:Character/330.json" in asked

    # 离线：不发请求，用上次存下的清单和缓存里的数据文档，结果一样
    again_files, again = await gather(WikiSource(None, cache, SETTINGS), ENV, HUIJI)
    assert again_files == files
    assert (again.skin, again.characters) == (data.skin, data.characters)
    catalog = build_catalog(again_files, again, ENV)
    assert [i.image_id for i in catalog.images] == [
        "Skin_330_01.png",
        "Skin_330_03.png",
        "S_330_01.png",
        "狂骨_s.png",
        "Hj_330_2.png",
    ]


# ---------- 下载原图 ----------


async def test_download_fetches_only_missing_originals_and_checks_content(tmp_path: Path) -> None:
    red, blue = png((200, 30, 30)), png((30, 30, 200))
    images = [
        image("a.png", red),
        image("b.png", blue),
        image("b_again.png", blue, image_url="https://cdn.test/b.png"),  # 内容一样：只下一次
        image("bad.png", b"what wiki says it is"),
    ]
    have = original_path(tmp_path, images[0])
    have.parent.mkdir(parents=True)
    have.write_bytes(red)  # 已经在缓存里
    bodies = {"https://cdn.test/b.png": blue, "https://cdn.test/bad.png": b"something else"}
    asked: list[str] = []

    def cdn(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        # 图片在另一个域名上：wiki 的放行密钥不能发过去
        assert "x-authkey" not in request.headers and request.headers["user-agent"] == "test-agent"
        return httpx.Response(200, content=bodies[str(request.url)])

    report = await download_originals(
        images, tmp_path, SETTINGS.wiki, SETTINGS.fetch, transport=httpx.MockTransport(cdn)
    )
    assert (report.total, report.cached, report.downloaded) == (3, 1, 1)
    assert sorted(asked) == ["https://cdn.test/b.png", "https://cdn.test/bad.png"]
    assert original_path(tmp_path, images[1]).read_bytes() == blue
    assert original_path(tmp_path, images[2]) == original_path(tmp_path, images[1])
    # 下回来的内容和 wiki 给的哈希对不上：不存
    assert report.failed == ["bad.png"] and "哈希" in report.errors[0]
    assert not original_path(tmp_path, images[3]).exists()


# ---------- 算向量、导入、以图搜图 ----------


def fake_image_api(axes: dict[bytes, int], seen: list[list[bytes]]) -> httpx.MockTransport:
    """假的嵌入接口：认识的图片（预处理后的 JPEG）各占一维，不认识的都落在最后一维。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["dimensions"] == SMALL.dimensions
        jpegs = []
        for item in body["input"]:
            head, _, encoded = item["image"].partition(",")
            assert head == "data:image/jpeg;base64" and set(item) == {"image"}
            jpegs.append(base64.b64decode(encoded))
        seen.append(jpegs)
        data = [
            {"index": i, "embedding": list(unit((axes.get(j, 7), 1.0), dim=SMALL.dimensions))}
            for i, j in enumerate(jpegs)
        ]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": len(jpegs)}})

    return httpx.MockTransport(handler)


def prepared(data: bytes, settings: ImageSettings = IMAGES) -> bytes:
    return normalize_image(data, max_side=settings.max_side, quality=settings.jpeg_quality)


async def test_pipeline_embeds_originals_loads_them_and_search_finds_the_same_picture(
    tmp_path: Path,
) -> None:
    originals = {
        "a.png": png((200, 30, 30)),
        "b.png": png((30, 30, 200)),
        "c.png": png((30, 200, 30)),
    }
    images = [image(name, data) for name, data in originals.items()]
    images_path, store_dir = (
        tmp_path / "images.jsonl",
        tmp_path / "vectors" / vectors_dirname(SMALL),
    )
    write_images(images_path, images)
    for item in images[:2]:  # c.png 的原图还没下载
        path = original_path(tmp_path, item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(originals[item.image_id])
    axes = {prepared(data): axis for axis, data in enumerate(originals.values())}
    seen: list[list[bytes]] = []

    async def embed(settings: ImageSettings = IMAGES) -> Any:
        return await embed_image_files(
            SMALL,
            images_path,
            store_dir,
            tmp_path,
            settings,
            transport=fake_image_api(axes, seen),
            retry_base_seconds=0,
        )

    first = await embed()
    assert (first.report.total, first.report.embedded, first.report.missing) == (3, 2, 1)
    assert first.no_original == ["c.png"]
    # 发给接口的是预处理过的图：透明的地方铺了白底、缩到了 max_side、转成了 JPEG
    assert seen == [[prepared(originals["a.png"]), prepared(originals["b.png"])]]
    sent = Image.open(BytesIO(seen[0][0]))
    assert (sent.format, sent.size) == ("JPEG", (32, 27)) and min(sent.getpixel((2, 2))) > 240

    seen.clear()
    again = await embed()  # 再跑一次：已经有的不重算
    assert (again.report.already, again.report.embedded, seen) == (2, 0, [])

    store = MemoryImageStore()
    loaded = await load_images_into(store, images_path, store_dir, SMALL, IMAGES.preprocess)
    assert (loaded.total, loaded.with_vectors) == (3, 2)
    info = await store.info()
    assert (info.fingerprint, info.preprocess) == (SMALL.fingerprint, IMAGES.preprocess)

    # 用户发来同一张图：查询走的是同一套预处理，向量对得上
    search = ImageSearch(
        store, EmbeddingClient(SMALL, transport=fake_image_api(axes, seen)), IMAGES
    )
    hits = await search.search(originals["b.png"])
    assert [(hit.image_id, hit.sure) for hit in hits] == [("b.png", True)]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert await search.search(originals["c.png"]) == []  # 库里没有它的向量
    await search.aclose()

    # wiki 上换了图（内容哈希变了）：只重算这一张，旧向量不会再被用上
    originals["a.png"] = png((250, 250, 30))
    images[0] = image("a.png", originals["a.png"])
    write_images(images_path, images)
    path = original_path(tmp_path, images[0])
    path.write_bytes(originals["a.png"])
    seen.clear()
    changed = await embed()
    assert seen == [[prepared(originals["a.png"])]]
    assert (changed.report.already, changed.report.embedded, changed.report.stale) == (1, 1, 1)
    # 改了预处理参数：全部要重算；没重算之前，按新参数导入时一张向量都对不上
    bigger = dataclasses.replace(IMAGES, max_side=40)
    none = await load_images_into(
        MemoryImageStore(), images_path, store_dir, SMALL, bigger.preprocess
    )
    assert none.with_vectors == 0
    assert (await embed(bigger)).report.embedded == 2


# ---------- 存储：内存和 Postgres 行为一致 ----------

FP, PRE = PROVIDER.fingerprint, IMAGES.preprocess
RECORDS = [
    (
        image("art", subject="skin:1:03", label="甲的皮肤", text="说明", meta={"n": 1}),
        unit((0, 1.0)),
    ),
    (image("card", subject="skin:1:03"), unit((0, 0.9), (1, 0.4))),  # 同一个造型的另一张图
    (image("sibling", subject="skin:1:04"), unit((0, 0.8), (2, 0.6))),
    (image("other"), unit((3, 1.0))),  # 没写 subject：自己算一个
    (image("pending"), None),
]


@pytest.fixture
async def postgres_store() -> AsyncIterator[PostgresImageStore]:
    dsn = os.environ.get("JIANSHANAGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("未设置 JIANSHANAGENT_TEST_POSTGRES_DSN，跳过 Postgres 实测")
    db = Database(dsn)
    await db.connect()
    await db.migrate()
    store = PostgresImageStore(db)
    try:
        yield store
    finally:
        await store.sync([], fingerprint=FP, preprocess=PRE)  # 测试库用完清空
        await db.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> ImageStore:
    if request.param == "memory":
        return MemoryImageStore()
    return request.getfixturevalue("postgres_store")


async def test_sync_is_idempotent_and_follows_the_given_records(store: ImageStore) -> None:
    assert (await store.info()).images == 0
    first = await store.sync(RECORDS, fingerprint=FP, preprocess=PRE)
    assert (first.total, first.upserted, first.unchanged, first.deleted) == (5, 5, 0, 0)
    info = await store.info()
    assert (info.images, info.with_vectors) == (5, 4)
    assert (info.fingerprint, info.preprocess) == (FP, PRE) and info.loaded_at

    again = await store.sync(RECORDS, fingerprint=FP, preprocess=PRE)
    assert (again.upserted, again.unchanged, again.deleted) == (0, 5, 0)

    # 改一张的说明、去掉一张、给原来没有向量的补上
    changed = [
        (dataclasses.replace(RECORDS[0][0], label="甲的皮肤「乙」"), RECORDS[0][1]),
        *RECORDS[1:3],
        (RECORDS[4][0], unit((5, 1.0))),
    ]
    third = await store.sync(changed, fingerprint=FP, preprocess=PRE)
    assert (third.total, third.upserted, third.unchanged, third.deleted) == (4, 2, 2, 1)
    hits = await store.search(unit((0, 1.0)), limit=1)
    assert hits[0].image.label == "甲的皮肤「乙」"
    left = await store.search(unit((3, 1.0)), limit=5)
    assert sorted(hit.image.image_id for hit in left) == ["art", "pending", "sibling"]  # other 删了

    # 预处理换了一种：带向量的行都要重写
    other = await store.sync(changed, fingerprint=FP, preprocess="another")
    assert (other.upserted, other.unchanged) == (4, 0)
    assert (await store.info()).preprocess == "another"


async def test_search_orders_by_similarity_and_keeps_one_image_per_subject(
    store: ImageStore,
) -> None:
    await store.sync(RECORDS, fingerprint=FP, preprocess=PRE)
    hits = await store.search(unit((0, 1.0), (1, 0.6)), limit=10)
    # card 比 art 更像，同一个造型只留它；pending 没有向量
    assert [hit.image.image_id for hit in hits] == ["card", "sibling", "other"]
    best = (0.9 + 0.6 * 0.4) / math.sqrt(0.9**2 + 0.4**2) / math.sqrt(1 + 0.6**2)
    assert hits[0].score == pytest.approx(best, abs=1e-5)
    close = await store.search(unit((0, 1.0)), limit=2)
    assert [hit.image.image_id for hit in close] == ["art", "sibling"]
    # 取回来的和存进去的一模一样
    assert close[0].image == RECORDS[0][0]


# ---------- 以图搜图 ----------


def answering(vector: array, calls: list[int] | None = None) -> EmbeddingClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": list(vector)}]})

    return EmbeddingClient(PROVIDER, transport=httpx.MockTransport(handle), retry_base_seconds=0)


async def searcher(vector: array, settings: ImageSettings = IMAGES) -> ImageSearch:
    store = MemoryImageStore()
    await store.sync(RECORDS, fingerprint=FP, preprocess=settings.preprocess)
    return ImageSearch(store, answering(vector), settings)


async def test_search_returns_close_images_and_only_a_clear_winner_is_sure() -> None:
    picture = png((200, 30, 30))
    search = await searcher(unit((0, 1.0)))
    hits = await search.search(picture)
    # 最像的那张（1.0）明显领先第二名（0.8）：认准了，只给这一张
    assert [(hit.image_id, hit.sure) for hit in hits] == [("art", True)]
    assert hits[0] == ImageHit(
        image_id="art",
        label="甲的皮肤",
        kind="皮肤立绘",
        page_title="",
        url="",
        image_url="https://cdn.test/art",
        score=hits[0].score,
        sure=True,
        text="说明",
        meta={"n": 1},
    )
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    # 没那么像（0.85），但比第二名（0.68）高出一大截：也算
    clear = await searcher(unit((0, 0.85), (6, 0.5268)))
    assert [(h.image_id, h.sure) for h in await clear.search(picture)] == [("art", True)]

    # 两个造型差不多像（0.950 对 0.948）：说不准是哪个，都只算「有些像」，一起返回
    torn_vector = unit((0, 1.0), (2, 0.33))
    hits = await (await searcher(torn_vector)).search(picture)
    assert [(hit.image_id, hit.sure) for hit in hits] == [("art", False), ("sibling", False)]
    assert hits[0].score > hits[1].score > IMAGES.sure_score
    # 第二名不够格返回（低于 min_score）、或者只要 1 张时，照样拿它来比
    strict = dataclasses.replace(IMAGES, min_score=0.949)
    hits = await (await searcher(torn_vector, strict)).search(picture)
    assert [(hit.image_id, hit.sure) for hit in hits] == [("art", False)]
    hits = await (await searcher(torn_vector)).search(picture, top_k=1)
    assert [(hit.image_id, hit.sure) for hit in hits] == [("art", False)]
    # 最像的也不够像（0.71，没到 sure_score）。同一个造型的两张图只算一张，0.5 以下的不返回
    vague = await searcher(unit((0, 0.7), (6, 0.7)))
    hits = await vague.search(picture)
    assert [(hit.image_id, hit.sure) for hit in hits] == [("art", False), ("sibling", False)]
    assert hits[1].score == pytest.approx(0.8 * 0.7 / math.sqrt(0.98))
    assert await (await searcher(unit((6, 1.0)))).search(picture) == []


async def test_search_raises_for_bad_input_and_skips_an_unusable_index() -> None:
    picture = png((200, 30, 30))
    search = await searcher(unit((0, 1.0)))
    with pytest.raises(ImageError):
        await search.search(b"not an image")

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    failing = ImageSearch(
        search.store, EmbeddingClient(PROVIDER, transport=httpx.MockTransport(broken)), IMAGES
    )
    with pytest.raises(EmbeddingError):
        await failing.search(picture)

    # 索引是空的、指纹或预处理和现在的配置不一样：不调嵌入接口，直接返回空
    calls: list[int] = []
    empty = ImageSearch(MemoryImageStore(), answering(unit((0, 1.0)), calls), IMAGES)
    assert await empty.search(picture) == []
    other = MemoryImageStore()
    await other.sync(RECORDS, fingerprint="old:model:4096", preprocess=PRE)
    assert await ImageSearch(other, answering(unit((0, 1.0)), calls), IMAGES).search(picture) == []
    resized = dataclasses.replace(IMAGES, max_side=64)
    assert await ImageSearch(search.store, answering(unit((0, 1.0)), calls), resized).search(
        picture
    ) == []  # fmt: skip
    assert calls == []


# ---------- 插件 ----------


def test_plugin_registers_image_search_only_when_embedding_is_available() -> None:
    repo = load_config(Path(__file__).resolve().parent / "config")
    files = {"plugin.knowledge": repo.plugin("knowledge")}
    ctx = PluginContext("knowledge", AppConfig(Path("."), files, {}), {"database": object()})
    KnowledgePlugin().register(ctx)
    assert "knowledge" in ctx.services and "image_search" not in ctx.services  # 没有嵌入配置

    files["core.embedding"] = repo.get("core.embedding")
    config = AppConfig(Path("."), files, {"gitee_ai": {"api_key": "k"}})
    ctx = PluginContext("knowledge", config, {"database": object()})
    KnowledgePlugin().register(ctx)
    search: ImageSearch = ctx.services["image_search"]
    provider = search.embedder.provider
    assert provider.fingerprint == "gitee:Qwen3-VL-Embedding-8B:4096"  # 和文本知识库同一个指纹
    assert (provider.timeout_seconds, provider.max_retries) == (8.0, 1)  # 和文本检索一样收紧
    settings = search.settings
    assert 0 < settings.min_score <= settings.sure_score < 1 and settings.top_k == 3
    assert 0 < settings.sure_margin < 0.5
    assert settings.preprocess == "flatten=white;max_side=1024;jpeg=90"

    ctx = PluginContext("knowledge", config, {})  # 没有数据库
    KnowledgePlugin().register(ctx)
    assert ctx.services == {}
