"""公告帖子（微信公众号、网易大神）→ 文档。

两类 Data 文档的结构（取了真实文档核对过）：

- 微信 ``Data:微信/<公众号>/<专辑>/<YYYY-MM>.json``：``{month, part, items: [文章…]}``；
  也有单篇文章直接作为一个文档的（顶层带 ``_hjschema: 微信公众号文章``）。
  文章字段：``id, title, url, createTime, mp_name, album_name, description, text, html,
  blocks, media``。
  单篇文档里 ``html`` 是公众号正文原样；月合集里 ``html`` 和 ``text`` 一样，
  是去掉标签后连成一串的文字，按原文分行的文字在 ``blocks`` 里
  （``{type: text, text}``，另有 image 块）。约六成文章只有标题，正文全空。
- 大神 ``Data:大神/<账号>/<YYYY-MM>.json``：``{month, part, items: [帖子…]}``。
  帖子字段：``id, nick, type, createTime, content.body.{text, title, longText, media, link}``。
  type 6 是长文（``title`` + ``longText`` 是 HTML）；
  type 4 是转发：自己的话（转发语）在 ``text``，被转发的帖子 ID 在 ``target.id``，
  原帖在 ``origin``。
  564 帖转发里 521 帖的 ``origin.content`` 带着原帖全文——它是一个 JSON **字符串**，
  解开后和普通帖子的 ``content`` 同构（``{type, body: {text, title, longText, media, link}}``），
  原帖作者的昵称在 ``origin.appRole``（也是 JSON 字符串）的 ``nick`` 里（约一半有）；
  其余 43 帖的 ``origin`` 只剩 ``{type: -1}``（原帖已删除或不可见），拿得到的只有 ``target.id``。
  其余类型是短帖（``text`` 是带换行的纯文本）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jianshanagent.plugins.knowledge.documents import (
    STATUS_EMPTY,
    BuildResult,
    has_text,
    html_blocks,
    plain_blocks,
)
from jianshanagent.plugins.knowledge.markdown import normalize_text
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_PARA,
    KIND_ANNOUNCEMENT,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.sources import PageRef

from jianshanagent_site_huiji_yys.knowledge.settings import YysEnv

STATUS_TITLE_ONLY = "title_only"  # 公告只有标题，wiki 没有收录正文

_CST = timezone(timedelta(hours=8))
_HTML_TAG_RE = re.compile(r"<(p|div|section|span|br|h[1-6]|table|ul|ol|img)\b", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#[^#\s]{1,30}#")
_STAR_TITLE_RE = re.compile(r"☆\s*(.+?)\s*☆")
#: 微信文章的落款：「在庭院里赏花的扫地工」「参上」，也有写在一行的
_SIGN_OFF_RE = re.compile(r"^(?:.{0,40}扫地工\s*)?参上[!！~～。]*$")
#: 配图署名「pic.@芍子子」（找不到落款时拿它定位文末，所以只认最规整的写法）
_CREDIT_RE = re.compile(r"^pic\s*[.．:：]?\s*@\s*\S.{0,30}$", re.IGNORECASE)
_CAPTION_PREFIXES = ("△", "▲")  # 图注，是正文
_CREDIT_MIN_FOLLOWING = 3
DASHEN_FEED_URL = "https://ds.163.com/feed/{}/"


@dataclass(slots=True)
class PostResult(BuildResult):
    """一份公告合集的处理结果。"""

    #: 同一帖在别的文档里已经收过、这里没有再收的帖数
    duplicate_ids: int = 0
    #: 微信文章末尾被清掉的行：（文档标识，被清掉的各行）
    tail_cleanups: list[tuple[str, list[str]]] = field(default_factory=list)


def timestamp(value: Any) -> datetime | None:
    """毫秒时间戳（可能包在 ``{"$numberLong": "…"}`` 里）→ 北京时间。"""
    if isinstance(value, dict):
        value = value.get("$numberLong")
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=_CST)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _html_blocks(html: str, settings: YysEnv, result: BuildResult) -> list[Block]:
    """帖子自带的 HTML → 块。只做通用清洗，不套 wiki 页面的组件规则。"""
    return html_blocks(html, settings.clean, settings.chunk, result.table_stats)


# ── 公告：微信 ────────────────────────────────────────────────────────


def _post_items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """月合集取 ``items``；单篇作为一个文档的，文档本身就是一帖。"""
    if isinstance(doc.get("items"), list):
        return [item for item in doc["items"] if isinstance(item, dict)]
    return [doc] if doc.get("id") else []


def wechat_tail_start(
    lines: Sequence[str], *, window: int = 10, max_lines: int = 8, line_chars: int = 80
) -> int | None:
    """微信文章末尾「署名 + 推荐阅读」从第几行开始；不符合文末的固定格式就返回 None（不删）。

    公众号文章的结尾是固定的：落款（「……的扫地工」「参上」）→ 配图署名（「pic.@某某」）→
    三四行别的文章的标题（推荐阅读）。规则只认这个格式：

    - 落款必须在最后 ``window`` 行以内；找不到落款时，退而找最后 ``window`` 行里的「pic.@某某」，
      而且它后面至少还有 3 行（推荐阅读）；
    - 它后面不超过 ``max_lines`` 行，而且每行都是不超过 ``line_chars`` 字的单行（标题那样的短行）；
    - 落款本身保留，从它的下一行开始删；以署名行为准时连署名一起删。

    正文中间的「pic.@某某」（图注）不在文末，不会被碰到。
    """
    total = len(lines)
    floor = max(0, total - window)
    start: int | None = None
    for index in range(total - 1, floor - 1, -1):
        if _SIGN_OFF_RE.match(lines[index]):
            start = index + 1
            break
    if start is None:
        for index in range(total - 1, floor - 1, -1):
            if _CREDIT_RE.match(lines[index]):
                # 没有落款可认时更小心：署名后面至少还有几行（推荐阅读），否则它可能只是文末的图注
                if total - index - 1 >= _CREDIT_MIN_FOLLOWING:
                    start = index
                break
    if start is None or start >= total:
        return None
    tail = lines[start:]
    if len(tail) > max_lines or any("\n" in line or len(line) > line_chars for line in tail):
        return None
    return start


def strip_wechat_tail(
    lines: Sequence[str], *, window: int = 10, max_lines: int = 8, line_chars: int = 80
) -> tuple[list[str], list[str]]:
    """去掉微信文章末尾的署名和推荐阅读。返回（留下的行，删掉的行）。「△」开头的图注留下。"""
    start = wechat_tail_start(lines, window=window, max_lines=max_lines, line_chars=line_chars)
    if start is None:
        return list(lines), []
    tail = lines[start:]
    kept = [line for line in tail if line.startswith(_CAPTION_PREFIXES)]
    return [*lines[:start], *kept], [
        line for line in tail if not line.startswith(_CAPTION_PREFIXES)
    ]


def build_wechat(ref: PageRef, doc: dict[str, Any], settings: YysEnv, seen: set[str]) -> PostResult:
    result = PostResult()
    for item in _post_items(doc):
        article_id = str(item.get("id") or "")
        key = f"wechat:{article_id}"
        if not article_id or key in seen:
            result.duplicate_ids += bool(article_id)
            continue
        seen.add(key)
        title = normalize_text(str(item.get("title") or "")).strip() or "（无标题）"
        published = timestamp(item.get("createTime"))
        album = str(item.get("album_name") or "")
        source = "·".join(p for p in ("微信公众号", str(item.get("mp_name") or ""), album) if p)

        # 正文来源按优先级：真正的 HTML（只有单篇文档里有）→ blocks（月合集里按原文分行的文字）
        # → text（连成一串、没有分行的文字）。月合集里的 html 字段其实和 text 一样是纯文字。
        blocks: list[Block] = []
        html = str(item.get("html") or "")
        if _HTML_TAG_RE.search(html):
            blocks = _html_blocks(html, settings, result)
        if not has_text(blocks):
            blocks = [
                Block(BLOCK_PARA, text=line, tight=True)
                for b in item.get("blocks") or []
                if isinstance(b, dict) and b.get("type") == "text"
                for line in [" ".join(normalize_text(str(b.get("text") or "")).split())]
                if line
            ]
        if not has_text(blocks):
            blocks = plain_blocks(str(item.get("text") or ""))
        src = settings.sources
        tail_at = wechat_tail_start(
            [b.render() for b in blocks],
            window=src.wechat_tail_window,
            max_lines=src.wechat_tail_max_lines,
            line_chars=src.wechat_tail_line_chars,
        )
        if tail_at is not None:
            removed = [
                b.render() for b in blocks[tail_at:] if not b.text.startswith(_CAPTION_PREFIXES)
            ]
            blocks = blocks[:tail_at] + [
                b for b in blocks[tail_at:] if b.text.startswith(_CAPTION_PREFIXES)
            ]
            if removed:
                result.tail_cleanups.append((key, removed))
        description = normalize_text(str(item.get("description") or "")).strip()
        body = "\n".join(b.render() for b in blocks)
        if description and description not in body:
            blocks.insert(0, Block(BLOCK_PARA, text=f"摘要：{description}"))

        meta: dict[str, Any] = {"album": album, "data_page": ref.title}
        if published:
            meta["published_at"] = published.isoformat(timespec="seconds")
        if not has_text(blocks):
            # wiki 只收录了标题：按配置留一片最小的切片（标题 + 日期 + 链接），打上标记便于下游过滤
            if not settings.sources.keep_title_only_posts:
                result.skipped.append((key, title, STATUS_TITLE_ONLY))
                continue
            blocks = [Block(BLOCK_PARA, text=title)]
            meta["title_only"] = True
        result.documents.append(
            Document(
                doc_key=key,
                kind=KIND_ANNOUNCEMENT,
                title=title,
                url=str(item.get("url") or "").replace("http://", "https://", 1),
                source=source,
                page_id=ref.page_id,
                rev_id=ref.rev_id,
                date=published.date().isoformat() if published else None,
                blocks=blocks,
                meta=meta,
            )
        )
    return result


# ── 公告：大神 ────────────────────────────────────────────────────────


def dashen_title(text: str) -> str:
    """短帖没有标题：优先取「☆…☆」里的字，否则取去掉话题标签后的第一行（截到 30 字）。"""
    text = normalize_text(text)
    star = _STAR_TITLE_RE.search(text)
    if star:
        return star.group(1).strip()
    for line in text.split("\n"):
        line = _HASHTAG_RE.sub("", line).strip(" \t-—~·")
        if line:
            if len(line) <= 30:
                return line
            cut = re.search(r"[，。！？!?~；;、]", line[8:30])
            return line[: 8 + cut.start()] if cut else line[:30]
    return "（无标题）"


def _post_extras(body: dict[str, Any], text: str) -> list[str]:
    """帖子正文之外的文字：视频标题、外链标题。"""
    extras: list[str] = []
    for media in body.get("media") or []:
        caption = normalize_text(str((media or {}).get("text") or "")).strip()
        if caption and caption not in text and f"视频：{caption}" not in extras:
            extras.append(f"视频：{caption}")
    link_title = normalize_text(str((body.get("link") or {}).get("linkTitle") or "")).strip()
    if link_title:
        extras.append(f"链接：{link_title}")
    return extras


def _post_blocks(body: dict[str, Any], settings: YysEnv, result: BuildResult) -> list[Block]:
    """一帖的正文块：长文走 HTML，短帖是纯文本；后面跟视频标题和外链标题。"""
    text = str(body.get("text") or "")
    long_html = str(body.get("longText") or "")
    blocks = _html_blocks(long_html, settings, result) if long_html else []
    if not has_text(blocks):
        blocks = plain_blocks(text)
    return blocks + [Block(BLOCK_PARA, text=line) for line in _post_extras(body, text)]


def _json_field(value: Any) -> dict[str, Any]:
    """转发帖里的 ``origin.content``、``origin.appRole`` 是 JSON 字符串，解开；解不开给空字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _repost_blocks(
    body: dict[str, Any], settings: YysEnv, result: BuildResult
) -> tuple[list[Block], dict[str, Any], str]:
    """转发帖 → 「转发语」「原帖」两节。返回（块，元数据，原帖标题）。

    原帖正文在 ``origin.content`` 里就收进来；不在（原帖已删）就只留一句说明和原帖链接。
    """
    origin = body.get("origin") if isinstance(body.get("origin"), dict) else {}
    target = body.get("target") if isinstance(body.get("target"), dict) else {}
    origin_id = str(origin.get("id") or target.get("id") or "")
    origin_body = _json_field(_json_field(origin.get("content")).get("body"))
    author = normalize_text(str(_json_field(origin.get("appRole")).get("nick") or "")).strip()
    posted = timestamp(origin.get("createTime") or target.get("createTime"))
    link = DASHEN_FEED_URL.format(origin_id) if origin_id else ""

    blocks: list[Block] = []
    own = plain_blocks(str(body.get("text") or ""))
    if own:
        blocks += [Block(BLOCK_HEADING, text="转发语", level=1), *own]
    blocks.append(Block(BLOCK_HEADING, text="原帖", level=1))
    info = [
        f"原帖作者：{author}" if author else "",
        f"发布于 {posted.date().isoformat()}" if posted else "",
        f"链接：{link}" if link else "",
    ]
    origin_title = normalize_text(str(origin_body.get("title") or "")).strip()
    origin_blocks = _post_blocks(origin_body, settings, result) if origin_body else []
    available = has_text(origin_blocks)
    if not available:
        info.insert(0, "原帖已删除或不可见，wiki 没有收录它的正文")
    blocks.append(Block(BLOCK_PARA, text="；".join(part for part in info if part)))
    if origin_title:
        blocks.append(Block(BLOCK_PARA, text=f"原帖标题：{origin_title}"))
    for block in origin_blocks:
        if block.kind == BLOCK_HEADING:
            block.level += 1  # 原帖长文自带的小标题挂到「原帖」下面
    blocks += origin_blocks

    meta: dict[str, Any] = {"repost": True, "origin_available": available}
    if origin_id:
        meta["origin_id"] = origin_id
    if author:
        meta["origin_author"] = author
    fallback = origin_title or (
        dashen_title(str(origin_body.get("text") or "")) if available else ""
    )
    return blocks, meta, fallback


def build_dashen(ref: PageRef, doc: dict[str, Any], settings: YysEnv, seen: set[str]) -> PostResult:
    result = PostResult()
    account = ref.data_id.split("/")[1] if ref.data_id.count("/") >= 2 else ""
    for item in _post_items(doc):
        post_id = str(item.get("id") or "")
        key = f"dashen:{post_id}"
        if not post_id or key in seen:
            result.duplicate_ids += bool(post_id)
            continue
        seen.add(key)
        body = ((item.get("content") or {}).get("body")) or {}
        text = str(body.get("text") or "")
        meta: dict[str, Any] = {"post_type": item.get("type"), "data_page": ref.title}
        title = normalize_text(str(body.get("title") or "")).strip() or dashen_title(text)
        if item.get("type") == 4 or isinstance(body.get("origin"), dict):
            # 转发：转发语和原帖收进同一篇，分成两节；元数据标 repost，下游可以区别对待
            blocks, repost_meta, origin_title = _repost_blocks(body, settings, result)
            meta |= repost_meta
            if title == "（无标题）" and origin_title:
                title = f"转发：{origin_title}"
        else:
            blocks = _post_blocks(body, settings, result)
        if not has_text(blocks):
            result.skipped.append((key, title, STATUS_EMPTY))
            continue
        published = timestamp(item.get("createTime"))
        if published:
            meta["published_at"] = published.isoformat(timespec="seconds")
        topics = [
            str(t.get("topicName"))
            for t in item.get("topicInfoList") or []
            if isinstance(t, dict) and t.get("topicName")
        ]
        if topics:
            meta["topics"] = topics
        result.documents.append(
            Document(
                doc_key=key,
                kind=KIND_ANNOUNCEMENT,
                title=title,
                url=DASHEN_FEED_URL.format(post_id),
                source="·".join(p for p in ("网易大神", account, str(item.get("nick") or "")) if p),
                page_id=ref.page_id,
                rev_id=ref.rev_id,
                date=published.date().isoformat() if published else None,
                blocks=blocks,
                meta=meta,
            )
        )
    return result
