"""评测以图搜图，给 plugin.knowledge.yaml 的 images 段定 min_score / sure_score / sure_margin。

办法：从入库的图里按种类抽一批，每张做几种变形（缩小、裁边、只留上半身、JPEG 重压、
透明底换成深色或游戏场景、套一圈截图式的界面、手机截图式的上下留黑，以及这些叠在一起），
变形后的图和用户发来的图走同一道预处理，再算成向量，到整个图片索引里找：

- 能不能把自己（同一个造型）排到第 1 名，相似度有多高；
- 把候选里「自己这个造型」或者「同一个角色的全部图」去掉以后，最高还能撞出多高的相似度
  ——这就是「库里没有这张图」时会看到的分数；
- 另外可以拿一批 wiki 上不在索引里的图（最近上传的公告配图）当「完全无关的图」。

三步，中间结果都存在 data/kb/eval/images/ 下，可以分开跑、重复跑（都用 .venv/bin/python）：

    # 可选：下载一批不在索引里的 wiki 图片当无关图（1 个 wiki 请求，其余走图片 CDN）
    tools/kb_eval/eval_images.py negatives
    # 抽样、做变形、算查询向量（调嵌入接口，可断点续跑）
    tools/kb_eval/eval_images.py embed
    # 出结果（只读向量文件，不联网；纯 Python 算，要一两分钟）
    tools/kb_eval/eval_images.py score
    # 另一个角度：入库的图两两之间有多像（不同造型会不会像到分不开；卡面小图、头像值不值得收）
    tools/kb_eval/eval_images.py pairs

接到机器人上以后的效果另外看：拿几张变形过的图走一遍运行时的检索代码（ImageSearch + 数据库），
数据库地址用环境变量 JIANSHANAGENT_POSTGRES_DSN 指定（本机测试库，别指到线上）：

    tools/kb_eval/eval_images.py search
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import heapq
import logging
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from jianshanagent.core.config import AppConfig, load_config
from jianshanagent.core.db import Database
from jianshanagent.core.embedding import (
    EmbeddingClient,
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingSettings,
)
from jianshanagent.core.images import normalize_image
from jianshanagent.core.logging import setup_logging
from jianshanagent.plugins.knowledge.embed import VectorStore, embed_items
from jianshanagent.plugins.knowledge.fetcher import WikiFetcher
from jianshanagent.plugins.knowledge.image_collect import (
    IMAGES_FILE,
    download_originals,
    original_path,
    read_images,
)
from jianshanagent.plugins.knowledge.image_embed import embed_key, vectors_dirname
from jianshanagent.plugins.knowledge.image_store import KbImage, PostgresImageStore
from jianshanagent.plugins.knowledge.images import ImageSearch, ImageSettings, is_sure
from jianshanagent.plugins.knowledge.plugin import retrieval_settings
from jianshanagent.plugins.knowledge.retrieval import query_provider
from jianshanagent.plugins.knowledge.settings import Settings, load_settings
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from jianshanagent_site_huiji_yys.knowledge.images import (
    FORM_ART,
    FORM_AVATAR,
    FORM_CARD,
    KIND_BACKGROUND,
    KIND_SCROLL,
    KIND_SOUL,
)

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "kb" / "eval" / "images"
NEGATIVES = EVAL / "negatives"
SEED = 20261111
#: 变形的做法改了就换一个值，已经算过的查询向量会重算
VERSION = "v1"
NEGATIVE = "无关图"
#: 每一组抽多少张：（组名，张数）
SAMPLE = (("立绘大图", 120), ("卡面小图", 40), ("头像", 30), ("御魂", 20), ("绘卷和背景", 30))
_DARK = (28, 30, 38)
_FONTS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


# ── 准备 ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Context:
    config: AppConfig
    settings: Settings
    images: ImageSettings
    provider: EmbeddingProvider
    records: list[KbImage]

    @property
    def index_dir(self) -> Path:
        return self.settings.vectors_dir / vectors_dirname(self.provider)


def context() -> Context:
    config = load_config()
    settings = load_settings(config)
    embedding = EmbeddingSettings.from_config(config)
    assert embedding is not None, "core.embedding.yaml 没有启用"
    return Context(
        config=config,
        settings=settings,
        images=ImageSettings.from_config(config.plugin("knowledge").get("images") or {}),
        provider=embedding.provider(),
        records=read_images(settings.output_dir / IMAGES_FILE),
    )


def group_of(image: KbImage) -> str:
    form = image.meta.get("form")
    if form == FORM_ART:
        return "立绘大图"
    if form == FORM_CARD:
        return "卡面小图"
    if form == FORM_AVATAR:
        return "头像"
    if image.kind == KIND_SOUL:
        return "御魂"
    if image.kind in (KIND_SCROLL, KIND_BACKGROUND):
        return "绘卷和背景"
    return image.kind


def owner_of(image: KbImage) -> str:
    """图上画的是谁：同一个角色（或同一个御魂）的图算一家。"""
    return str(image.meta.get("character_id") or image.meta.get("soul") or image.image_id)


def pick_sample(ctx: Context) -> list[KbImage]:
    """每组随机抽几张（原图在缓存里的），种子固定，重跑抽到的是同一批。"""
    rng = random.Random(SEED)
    groups: dict[str, list[KbImage]] = defaultdict(list)
    for image in ctx.records:
        if original_path(ctx.settings.cache_dir, image).exists():
            groups[group_of(image)].append(image)
    picked: list[KbImage] = []
    for name, count in SAMPLE:
        picked += rng.sample(groups[name], min(count, len(groups[name])))
    return picked


# ── 变形 ──────────────────────────────────────────────────────────────


def _flat(image: Image.Image, color: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    rgba = image.convert("RGBA")
    out = Image.new("RGB", rgba.size, color)
    out.paste(rgba, mask=rgba.getchannel("A"))
    return out


def _transparent(image: Image.Image) -> bool:
    return image.mode == "RGBA" and image.getextrema()[3][0] < 250


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _cover(background: Image.Image, size: tuple[int, int]) -> Image.Image:
    """把背景图裁成和 ``size`` 一样的比例再缩放，铺满。"""
    scale = max(size[0] / background.width, size[1] / background.height)
    resized = background.resize(
        (
            max(size[0], round(background.width * scale)),
            max(size[1], round(background.height * scale)),
        )
    )
    left, top = (resized.width - size[0]) // 2, (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1])).convert("RGB")


def _on_scene(image: Image.Image, scene: Image.Image) -> Image.Image:
    """透明底的图贴到一张（略微虚化的）游戏场景图上，像游戏里看立绘那样。"""
    out = _cover(scene, image.size).filter(ImageFilter.GaussianBlur(2))
    out.paste(image, mask=image.getchannel("A"))
    return out


def _with_ui(picture: Image.Image) -> Image.Image:
    """套一圈截图式的界面：上面一条标题栏，下面一排按钮，左右留边，角上有图标。"""
    width, height = picture.size
    pad, top, bottom = round(width * 0.06), round(height * 0.12), round(height * 0.2)
    canvas = Image.new("RGB", (width + pad * 2, height + top + bottom), (236, 226, 208))
    canvas.paste(picture.convert("RGB"), (pad, top))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, canvas.width, top), fill=(60, 44, 40))
    draw.text(
        (pad, top // 4), "< 返回    式神录 · 皮肤", fill=(240, 226, 190), font=_font(top // 2)
    )
    draw.ellipse(
        (canvas.width - top, top // 6, canvas.width - top // 3, top * 5 // 6), fill=(196, 60, 52)
    )
    y = height + top + bottom // 4
    button = (canvas.width - pad * 4) // 3
    for n, (label, color) in enumerate(
        (("更换", (188, 150, 84)), ("详情", (92, 120, 150)), ("分享", (120, 96, 150)))
    ):
        x = pad + n * (button + pad)
        draw.rounded_rectangle((x, y, x + button, y + bottom // 2), radius=bottom // 8, fill=color)
        draw.text((x + button // 4, y + bottom // 8), label, fill="white", font=_font(bottom // 4))
    return canvas


def _landscape(image: Image.Image, scene: Image.Image) -> Image.Image:
    """横屏的游戏截图（阴阳师是横屏游戏）：场景铺满，立绘站在左边，右边一块面板，上面一条标题栏。"""
    width, height = 2340, 1080
    canvas = _cover(scene, (width, height)).filter(ImageFilter.GaussianBlur(3)).convert("RGBA")
    art = image.resize((round(image.width * 1000 / image.height), 1000), Image.Resampling.LANCZOS)
    canvas.alpha_composite(art, (round(width * 0.08), 70))
    panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, 0, width, 90), fill=(40, 30, 28, 225))
    draw.text((40, 20), "< 返回    式神录 · 皮肤", fill=(240, 226, 190, 255), font=_font(44))
    draw.rounded_rectangle((1480, 130, 2290, 1010), radius=24, fill=(30, 24, 22, 205))
    for n, label in enumerate(("默认", "觉醒", "典藏皮肤", "活动皮肤", "皮肤券兑换")):
        top = 170 + n * 165
        draw.rounded_rectangle((1520, top, 2250, top + 130), radius=18, fill=(236, 226, 208, 235))
        draw.text((1560, top + 36), label, fill=(60, 44, 40, 255), font=_font(48))
    canvas.alpha_composite(panel)
    return canvas.convert("RGB")


def _phone(picture: Image.Image) -> Image.Image:
    """手机竖屏截图：图片在中间，上下留黑。"""
    width = picture.width
    canvas = Image.new("RGB", (width, round(width * 19.5 / 9)), (0, 0, 0))
    canvas.paste(picture.convert("RGB"), (0, (canvas.height - picture.height) // 2))
    return canvas


def _jpeg(picture: Image.Image, quality: int) -> bytes:
    out = BytesIO()
    picture.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _png(picture: Image.Image) -> bytes:
    out = BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


def _shrunk(image: Image.Image, factor: float) -> Image.Image:
    size = (max(8, round(image.width * factor)), max(8, round(image.height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _cropped(image: Image.Image, keep: float) -> Image.Image:
    """裁掉四周，只留中间 ``keep``（按边长算）。"""
    dx, dy = round(image.width * (1 - keep) / 2), round(image.height * (1 - keep) / 2)
    return image.crop((dx, dy, image.width - dx, image.height - dy))


#: 变形：名字 → （做法，适用条件）。做法拿到（原图，场景图），返回要发给检索的图片字节
Transform = Callable[[Image.Image, Image.Image], bytes]
TRANSFORMS: dict[str, tuple[Transform, Callable[[KbImage, Image.Image], bool]]] = {
    "缩小一半": (lambda im, _: _png(_shrunk(im, 0.5)), lambda *_: True),
    "裁掉四周30%": (lambda im, _: _png(_cropped(im, 0.7)), lambda *_: True),
    "只留上面55%": (
        lambda im, _: _png(im.crop((0, 0, im.width, round(im.height * 0.55)))),
        lambda record, _: group_of(record) == "立绘大图",
    ),
    "JPEG质量30": (lambda im, _: _jpeg(_flat(im), 30), lambda *_: True),
    "深色底": (lambda im, _: _png(_flat(im, _DARK)), lambda _, im: _transparent(im)),
    "场景底": (lambda im, scene: _jpeg(_on_scene(im, scene), 90), lambda _, im: _transparent(im)),
    "截图界面": (lambda im, _: _jpeg(_with_ui(_flat(im)), 85), lambda *_: True),
    "手机留黑": (lambda im, _: _jpeg(_phone(_flat(im)), 85), lambda *_: True),
    "综合截图": (
        lambda im, scene: _jpeg(
            _shrunk(_phone(_with_ui(_on_scene(im, scene) if _transparent(im) else _flat(im))), 0.5),
            60,
        ),
        lambda *_: True,
    ),
    "横屏游戏截图": (
        lambda im, scene: _jpeg(_landscape(im, scene), 85),
        lambda record, im: group_of(record) == "立绘大图" and _transparent(im),
    ),
}


# ── 算查询向量 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Query:
    #: 「变形|图片 ID」；无关图是「无关图|文件名」
    chunk_id: str
    digest: str
    path: Path
    transform: str


def _queries(ctx: Context) -> list[Query]:
    queries: list[Query] = []
    for record in pick_sample(ctx):
        path = original_path(ctx.settings.cache_dir, record)
        with Image.open(path) as opened:
            image = opened.convert("RGBA") if opened.mode in ("RGBA", "LA", "P") else opened.copy()
        for name, (_, applies) in TRANSFORMS.items():
            if applies(record, image):
                digest = f"{embed_key(record, ctx.images.preprocess)}:{VERSION}"
                queries.append(Query(f"{name}|{record.image_id}", digest, path, name))
    for path in sorted(NEGATIVES.glob("*")) if NEGATIVES.exists() else []:
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            digest = f"{hashlib.sha1(path.read_bytes()).hexdigest()}:{ctx.images.preprocess}"
            queries.append(Query(f"{NEGATIVE}|{path.name}", digest, path, NEGATIVE))
    return queries


def _scenes(ctx: Context) -> list[Path]:
    paths = [
        original_path(ctx.settings.cache_dir, record)
        for record in ctx.records
        if record.kind == KIND_BACKGROUND
    ]
    return [path for path in paths if path.exists()]


def query_bytes(query: Query, scenes: Sequence[Path]) -> bytes:
    """一条查询要发出去的图片（还没过 normalize_image）。场景图按查询名固定地挑。"""
    if query.transform == NEGATIVE:
        return query.path.read_bytes()
    with Image.open(query.path) as opened:
        image = opened.convert("RGBA") if opened.mode in ("RGBA", "LA", "P") else opened.copy()
    pick = random.Random(f"{SEED}{query.chunk_id}")
    with Image.open(pick.choice(scenes)) as scene:
        return TRANSFORMS[query.transform][0](image, scene.convert("RGB"))


async def _embed(_args: argparse.Namespace) -> None:
    ctx = context()
    queries = _queries(ctx)
    scenes = _scenes(ctx)
    assert scenes, "缓存里没有条目背景图（场景底要用），先跑 images collect"

    def prepare(batch: list[Query]) -> list[bytes]:
        return [
            normalize_image(
                query_bytes(query, scenes),
                max_side=ctx.images.max_side,
                quality=ctx.images.jpeg_quality,
            )
            for query in batch
        ]

    async def embed_batch(client: EmbeddingClient, batch: list[Query]) -> EmbeddingResult:
        return await client.embed_images(await asyncio.to_thread(prepare, batch))

    report = await embed_items(
        ctx.provider,
        queries,
        EVAL / "queries",
        embed_batch,
        batch_size=ctx.images.embed_batch_size,
        embedded="image (eval query)",
    )
    print(
        f"查询 {report.total} 条，已有 {report.already + report.embedded} 条"
        f"（这次算了 {report.embedded} 条，{report.prompt_tokens} 个 token，"
        f"{report.seconds / 60:.1f} 分钟），没算成 {len(report.failed)} 条",
        file=sys.stderr,
    )


# ── 无关图 ────────────────────────────────────────────────────────────


async def _negatives(args: argparse.Namespace) -> None:
    """wiki 上最近上传、不在索引里的照片类图片（基本是公告配图），下载到 negatives/。"""
    ctx = context()
    indexed = {record.image_id for record in ctx.records}
    fetcher = WikiFetcher(ctx.settings.wiki, ctx.settings.fetch)
    try:
        data = await fetcher.api(
            "list",
            {
                "action": "query",
                "list": "allimages",
                "aisort": "timestamp",
                "aidir": "descending",
                "ailimit": "500",
                "aiprop": "url|size|sha1|mime",
            },
        )
    finally:
        await fetcher.aclose()
    candidates = [
        KbImage(
            image_id=str(item["name"]),
            kind=NEGATIVE,
            label="",
            image_url=str(item["url"]),
            sha1=str(item["sha1"]),
        )
        for item in data["query"]["allimages"]
        if item["name"] not in indexed
        and item.get("mime") == "image/jpeg"
        and item.get("size", 0) < 1_500_000
        and 0.4 < item.get("width", 1) / max(item.get("height", 1), 1) < 2.5
    ]
    picked = random.Random(SEED).sample(candidates, min(args.count, len(candidates)))
    NEGATIVES.mkdir(parents=True, exist_ok=True)
    missing = [record for record in picked if not (NEGATIVES / record.image_id).exists()]
    report = await download_originals(missing, EVAL, ctx.settings.wiki, ctx.settings.fetch)
    for record in missing:  # 下载是按内容哈希存的，挪成原来的文件名
        source = original_path(EVAL, record)
        if source.exists():
            source.replace(NEGATIVES / record.image_id)
    print(
        f"候选 {len(candidates)} 张，抽了 {len(picked)} 张，已有 {len(picked) - len(missing)} 张，"
        f"下载 {report.downloaded} 张（失败 {len(report.failed)} 张），在 {NEGATIVES}",
        file=sys.stderr,
    )


# ── 出结果 ────────────────────────────────────────────────────────────


def _opened(directory: Path, provider: EmbeddingProvider) -> VectorStore:
    store = VectorStore(directory, provider)
    store.open()
    store.close()
    return store


def _quantiles(values: Sequence[float]) -> str:
    if not values:
        return "（没有）"
    ordered = sorted(values)

    def at(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    return (
        f"最低 {ordered[0]:.3f}  5% {at(0.05):.3f}  中位 {statistics.median(ordered):.3f}  "
        f"95% {at(0.95):.3f}  最高 {ordered[-1]:.3f}"
    )


def _share(values: Sequence[float], threshold: float) -> str:
    return f"{sum(v >= threshold for v in values) / max(len(values), 1):5.1%}"


@dataclass(slots=True)
class Outcome:
    """一条查询在索引里找的结果（候选按造型去重，每个造型只算最像的那张）。"""

    transform: str
    group: str
    #: 自己这个造型的相似度、名次（从 1 数起）
    own: float
    rank: int
    #: 最像的两个造型的相似度（第 1 名、第 2 名）
    top: tuple[float, float]
    #: 去掉自己这个造型以后最像的两个（同一个角色别的造型还在）：库里没有这个造型时看到的
    without_subject: tuple[float, float]
    #: 去掉同一个角色的全部图以后最像的两个：库里没有这个角色时看到的
    without_owner: tuple[float, float]


def _top_two(scores: Sequence[float]) -> tuple[float, float]:
    first, second = heapq.nlargest(2, scores)
    return first, second


def _lead(pair: tuple[float, float]) -> float:
    return pair[0] - pair[1]


def _sure_share(pairs: Sequence[tuple[float, float]], settings: ImageSettings) -> float:
    """按运行时的规则（images.is_sure），这些「第 1 名、第 2 名」里有多大比例会判成就是这张图。"""
    return sum(is_sure(first, second, settings) for first, second in pairs) / max(len(pairs), 1)


def _score(_args: argparse.Namespace) -> None:
    ctx = context()
    by_id = {record.image_id: record for record in ctx.records}
    stored = _opened(ctx.index_dir, ctx.provider)
    # 向量转成浮点数列表，math.sumprod 才快（纯 Python，没有 numpy）
    index = [
        (record, list(stored.vector(row)))
        for record in ctx.records
        if (row := stored.row(record.image_id, embed_key(record, ctx.images.preprocess)))
        is not None
    ]
    print(f"索引 {len(index)} 张（记录 {len(ctx.records)} 张）", file=sys.stderr)
    queries = _opened(EVAL / "queries", ctx.provider)
    outcomes: list[Outcome] = []
    #: 无关图：（最像的两个造型的相似度，文件名，最像的那个造型是什么）
    negatives: list[tuple[tuple[float, float], str, str]] = []
    started = time.perf_counter()
    for query in _queries(ctx):  # 只看现在这批查询，向量文件里更早的版本不算
        row = queries.row(query.chunk_id, query.digest)
        if row is None:
            continue
        vector = list(queries.vector(row))
        transform, _, image_id = query.chunk_id.partition("|")
        best: dict[str, float] = {}  # 造型 → 这个造型里最像的那张的相似度
        owners: dict[str, str] = {}
        for record, candidate in index:
            score = math.sumprod(vector, candidate)
            if score > best.get(record.group, -2.0):
                best[record.group] = score
                owners[record.group] = owner_of(record)
        if transform == NEGATIVE:
            winner = max(best, key=lambda group: best[group])  # noqa: B023
            top = next(record for record, _ in index if record.group == winner)
            negatives.append((_top_two(list(best.values())), image_id, top.label))
            continue
        me = by_id.get(image_id)
        if me is None or transform not in TRANSFORMS or me.group not in best:
            continue
        own = best[me.group]
        others = [score for group, score in best.items() if group != me.group]
        strangers = [score for group, score in best.items() if owners[group] != owner_of(me)]
        outcomes.append(
            Outcome(
                transform=transform,
                group=group_of(me),
                own=own,
                rank=1 + sum(score > own for score in best.values()),
                top=_top_two(list(best.values())),
                without_subject=_top_two(others),
                without_owner=_top_two(strangers),
            )
        )
    seconds = time.perf_counter() - started
    print(f"算了 {len(outcomes) + len(negatives)} 条查询，用时 {seconds:.0f} 秒", file=sys.stderr)

    def table(title: str, rows: dict[str, list[Outcome]]) -> None:
        print(f"\n== {title} ==")
        print(
            f"{'':<14}{'条数':>5} {'第1名':>6} {'前3名':>6} {'判成就是它':>8} {'其中认错':>6}"
            "   自己这个造型的相似度"
        )
        for name, items in rows.items():
            if not items:
                continue
            sure = [o for o in items if is_sure(*o.top, ctx.images)]
            print(
                f"{name:<14}{len(items):>5} "
                f"{sum(o.rank == 1 for o in items) / len(items):>6.1%} "
                f"{sum(o.rank <= 3 for o in items) / len(items):>6.1%} "
                f"{len(sure) / len(items):>10.1%} {sum(o.rank != 1 for o in sure):>8}   "
                f"{_quantiles([o.own for o in items])}"
            )

    print(
        f"现在的配置：min_score {ctx.images.min_score}，sure_score {ctx.images.sure_score}，"
        f"sure_margin {ctx.images.sure_margin}（「判成就是它」「其中认错」两栏按它算）"
    )

    table(
        "入库的图变形以后找自己（各种变形）",
        {name: [o for o in outcomes if o.transform == name] for name in TRANSFORMS},
    )
    table(
        "同上，按图片种类分（所有变形合在一起）",
        {name: [o for o in outcomes if o.group == name] for name, _ in SAMPLE},
    )
    worst = sorted(outcomes, key=lambda o: o.own)[:10]
    print("\n相似度最低的 10 条：")
    for o in worst:
        print(f"  {o.own:.3f}  第 {o.rank} 名  {o.transform}  {o.group}")

    in_library = [o.top for o in outcomes if o.rank == 1]
    without_subject = [o.without_subject for o in outcomes]
    without_owner = [o.without_owner for o in outcomes]
    unrelated = [pair for pair, _, _ in negatives]
    scenarios = (
        ("库里有（第 1 名就是它）", in_library),
        ("库里没有这个造型，同角色别的造型在", without_subject),
        ("库里没有这个角色", without_owner),
        (f"无关的图（{len(negatives)} 张公告配图）", unrelated),
    )
    print("\n== 第 1 名的相似度：库里有这张图和没有时差多少（同一批查询，去掉一部分候选）==")
    for name, pairs in scenarios:
        print(f"{name}：{_quantiles([first for first, _ in pairs])}")
    print("\n== 第 1 名比第 2 名（别的造型）领先多少 ==")
    for name, pairs in scenarios:
        print(f"{name}：{_quantiles([_lead(pair) for pair in pairs])}")
    print("领先最多的几张无关图（看了原图：领先 0.06 以上的都是含着库里那张立绘或绘卷图的海报）：")
    for pair, name, label in sorted(negatives, key=lambda item: -_lead(item[0]))[:8]:
        print(f"    相似度 {pair[0]:.3f}  领先 {_lead(pair):.3f}  {name}  →  {label}")

    print("\n== sure_score / sure_margin 取不同的值（规则见 images.is_sure）==")
    print(
        f"{'sure_score':>10} {'sure_margin':>11} {'变形后判成就是它':>14} {'其中认错(条)':>10} "
        f"{'没有这个造型时误判':>16} {'没有这个角色时误判':>16} {'无关图误判(张)':>12}   "
        "截图界面 / 综合截图 / 横屏游戏截图"
    )
    grid = [(score, margin) for score in (0.60, 0.65, 0.70) for margin in (0.06, 0.08, 0.10, 0.12)]
    # 对照：只看相似度、不看领先幅度
    grid += [(score, 0.0) for score in (0.80, 0.85, 0.88, 0.90, 0.92)]
    for score, margin in grid:
        rule = dataclasses.replace(ctx.images, sure_score=score, sure_margin=margin)
        sure = [o for o in outcomes if is_sure(*o.top, rule)]

        def of(items: list[Outcome], name: str) -> int:
            return sum(o.transform == name for o in items)

        shots = " / ".join(
            f"{of(sure, name) / max(1, of(outcomes, name)):.0%}"
            for name in ("截图界面", "综合截图", "横屏游戏截图")
        )
        print(
            f"{score:>10.2f} {margin:>11.2f} {len(sure) / max(len(outcomes), 1):>16.1%} "
            f"{sum(o.rank != 1 for o in sure):>12} {_sure_share(without_subject, rule):>18.1%} "
            f"{_sure_share(without_owner, rule):>18.1%} "
            f"{sum(is_sure(*pair, rule) for pair in unrelated):>14}   {shots}"
        )

    print("\n== min_score 取不同的值 ==")
    print(
        f"{'min_score':>10} {'自己被挡掉':>10} {'没有这个造型时仍返回':>18} "
        f"{'没有这个角色时仍返回':>18} {'无关图仍返回':>12}"
    )
    for threshold in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
        blocked = sum(o.own < threshold for o in outcomes) / max(len(outcomes), 1)
        print(
            f"{threshold:>10.2f} {blocked:>10.1%} "
            f"{_share([first for first, _ in without_subject], threshold):>18} "
            f"{_share([first for first, _ in without_owner], threshold):>18} "
            f"{_share([first for first, _ in unrelated], threshold):>12}"
        )


def _pairs(_args: argparse.Namespace) -> None:
    """入库的图两两之间有多像（不做变形，不联网）。

    看两件事：不同造型的图会不会像到分不开（换色皮肤、wiki 上重复上传的图）；
    只收立绘大图的话，卡面小图和头像还认不认得出是谁——也就是这两类值不值得收。
    """
    ctx = context()
    stored = _opened(ctx.index_dir, ctx.provider)
    index = [
        (record, list(stored.vector(row)))
        for record in ctx.records
        if (row := stored.row(record.image_id, embed_key(record, ctx.images.preprocess)))
        is not None
    ]
    count = len(index)
    started = time.perf_counter()
    #: 每张图：别的造型里最像的（同一个角色的 / 别的角色的），以及立绘大图里最像的
    sibling: list[tuple[float, int]] = [(-2.0, -1)] * count
    stranger: list[tuple[float, int]] = [(-2.0, -1)] * count
    art: list[tuple[float, int]] = [(-2.0, -1)] * count
    is_art = [group_of(record) == "立绘大图" for record, _ in index]
    for i in range(count):
        mine, vector = index[i]
        for j in range(i + 1, count):
            other, candidate = index[j]
            score = math.sumprod(vector, candidate)
            for a, b in ((i, j), (j, i)):
                if is_art[b] and score > art[a][0]:
                    art[a] = (score, b)
            if mine.group == other.group:
                continue
            table = sibling if owner_of(mine) == owner_of(other) else stranger
            if score > table[i][0]:
                table[i] = (score, j)
            if score > table[j][0]:
                table[j] = (score, i)
    print(f"{count} 张图两两比完，用时 {time.perf_counter() - started:.0f} 秒", file=sys.stderr)

    print("== 入库的图和别的造型里最像的那张（不做变形）==")
    same = [score for score, j in sibling if j >= 0]
    different = [score for score, j in stranger if j >= 0]
    print(f"同一个角色的别的造型：{_quantiles(same)}")
    print(f"别的角色：            {_quantiles(different)}")
    for threshold in (0.80, 0.85, 0.90, 0.95, 0.98):
        print(
            f"  最像的别的造型 ≥ {threshold:.2f} 的图："
            f"同角色 {sum(s >= threshold for s in same)} 张，"
            f"别的角色 {sum(s >= threshold for s in different)} 张"
        )
    closest = sorted(
        {
            (round(score, 4), *sorted((index[i][0].image_id, index[j][0].image_id)))
            for table in (sibling, stranger)
            for i, (score, j) in enumerate(table)
            if j >= 0
        },
        reverse=True,
    )[:15]
    by_id = {record.image_id: record for record, _ in index}
    print("最像的 15 对（不同造型）：")
    for score, first, second in closest:
        print(f"  {score:.3f}  {first}（{by_id[first].label}） ↔ {second}（{by_id[second].label}）")

    print("\n== 假如只收立绘大图：卡面小图、头像各自最像的立绘是不是同一个造型 / 同一个角色 ==")
    for name in ("卡面小图", "头像"):
        rows = [
            (index[i][0], index[j][0], score)
            for i, (score, j) in enumerate(art)
            if j >= 0 and group_of(index[i][0]) == name
        ]
        same_subject = sum(a.group == b.group for a, b, _ in rows) / max(len(rows), 1)
        same_owner = sum(owner_of(a) == owner_of(b) for a, b, _ in rows) / max(len(rows), 1)
        print(
            f"{name} {len(rows)} 张：同一个造型 {same_subject:.1%}，同一个角色 {same_owner:.1%}；"
            f"相似度 {_quantiles([score for _, _, score in rows])}"
        )


# ── 走一遍运行时的检索代码 ────────────────────────────────────────────


async def _search(args: argparse.Namespace) -> None:
    ctx = context()
    if not ctx.config.postgres_dsn:
        raise SystemExit("先用环境变量 JIANSHANAGENT_POSTGRES_DSN 指定（本机测试用的）数据库")
    retrieval = retrieval_settings(ctx.config.plugin("knowledge").get("retrieval") or {})
    db = Database(ctx.config.postgres_dsn, pool_max=2)
    await db.connect()
    search = ImageSearch(
        PostgresImageStore(db),
        EmbeddingClient(query_provider(ctx.provider, retrieval)),
        ctx.images,
    )
    scenes = _scenes(ctx)
    sample = random.Random(args.seed).sample(_queries(ctx), args.count)
    by_id = {record.image_id: record for record in ctx.records}
    try:
        info = await search.store.info()
        print(
            f"库里 {info.images} 张图，{info.with_vectors} 张有向量；"
            f"min_score {ctx.images.min_score}，sure_score {ctx.images.sure_score}，"
            f"sure_margin {ctx.images.sure_margin}"
        )
        for query in sample:
            transform, _, image_id = query.chunk_id.partition("|")
            data = query_bytes(query, scenes)
            started = time.perf_counter()
            hits = await search.search(data)
            seconds = time.perf_counter() - started
            wanted = by_id[image_id].label if image_id in by_id else "（库里没有）"
            print(
                f"\n{transform}  {image_id}（{wanted}）  {len(data) // 1024} KB  {seconds:.2f} 秒"
            )
            for hit in hits:
                mark = "就是这张" if hit.sure else "有些像"
                print(f"    {hit.score:.3f}  {mark}  {hit.kind}  {hit.label}  [{hit.image_id}]")
            if not hits:
                print("    （没有相似度够高的图）")
    finally:
        await search.aclose()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="评测以图搜图，定相似度门槛")
    sub = parser.add_subparsers(dest="command", required=True)
    negatives = sub.add_parser("negatives", help="下载一批不在索引里的 wiki 图片当无关图")
    negatives.add_argument("--count", type=int, default=80)
    sub.add_parser("embed", help="抽样、做变形、算查询向量")
    sub.add_parser("score", help="出结果（不联网）")
    sub.add_parser("pairs", help="入库的图两两之间有多像（不联网）")
    search = sub.add_parser("search", help="拿几张变形过的图走一遍运行时的检索代码")
    search.add_argument("--count", type=int, default=12)
    search.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    setup_logging(json_logs=False)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command in ("score", "pairs"):
        {"score": _score, "pairs": _pairs}[args.command](args)
        return
    asyncio.run({"negatives": _negatives, "embed": _embed, "search": _search}[args.command](args))


if __name__ == "__main__":
    main()
