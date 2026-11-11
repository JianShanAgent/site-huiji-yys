"""剧情（``Data:Story/<剧情ID>.json``）→ 文档。

结构（取了真实文档核对过）：``{story: [{story_id, story_name, chapters, items}]}``。
章节：``chapter_id, chapter_title, chapter_desc, series, steps``；步骤 ``kind`` 为 line 的才是文字
（``speaker`` + ``text``，``class: scene-title`` 的是场景小标题），choice 是选项，
其余是图像 / 音效 / 视频。
``items`` 是随剧情解锁的收集品文本（``item_name, item_type, item_tex``）。
"""

from __future__ import annotations

import re
from typing import Any

from jianshanagent.plugins.knowledge.documents import BuildResult, has_text, plain_blocks
from jianshanagent.plugins.knowledge.markdown import normalize_text
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_PARA,
    KIND_STORY,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.sources import Fetched, PageRef

from jianshanagent_site_huiji_yys.knowledge.settings import YysEnv

STATUS_EMPTY_CHAPTER = "empty_chapter"  # Data 里的剧情章节没有文字

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def story_id_of(ref: PageRef) -> str:
    """剧情 ID 以文件名为准（页面模板引用的就是它）。"""
    return ref.basename.removesuffix(".json")


def story_chapters(stories: list[Fetched]) -> set[tuple[str, str]]:
    """Data 里有文字的剧情章节：（剧情ID，章节ID）。"""
    available: set[tuple[str, str]] = set()
    for fetched in stories:
        doc = fetched.value if isinstance(fetched.value, dict) else {}
        entries = doc.get("story")
        for story in entries if isinstance(entries, list) else [entries]:
            if not isinstance(story, dict):
                continue
            # 模板参数里的剧情 ID 是文件名（Data:Story/<ID>.json），
            # 文档里的 story_id 字段不一定和它一致
            story_id = story_id_of(fetched.ref)
            for chapter in story.get("chapters") or []:
                if isinstance(chapter, dict) and any(
                    isinstance(s, dict) and s.get("kind") == "line" and s.get("text")
                    for s in chapter.get("steps") or []
                ):
                    available.add((story_id, str(chapter.get("chapter_id"))))
    return available


def _story_steps(steps: list[Any], blocks: list[Block]) -> None:
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "choice":
            for option in step.get("options") or []:
                if not isinstance(option, dict):
                    continue
                label = normalize_text(str(option.get("text") or "")).strip()
                if label:
                    blocks.append(Block(BLOCK_PARA, text=f"【选项】{label}", tight=True))
                _story_steps(option.get("steps") or [], blocks)
            continue
        if kind != "line":
            continue  # 图像、音效、视频、停顿：没有文字
        text = normalize_text(_TAG_RE.sub("", _BR_RE.sub("\n", str(step.get("text") or ""))))
        text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
        if not text:
            continue
        if "scene-title" in str(step.get("class") or ""):
            blocks.append(Block(BLOCK_HEADING, text=text.replace("\n", " "), level=1))
            continue
        speaker = normalize_text(str(step.get("speaker") or "")).strip()
        line = text if speaker in ("", "旁白") else f"{speaker}：{text}"
        blocks.append(Block(BLOCK_PARA, text=line, tight=True))


def _flatten_items(items: Any) -> list[dict[str, Any]]:
    """个别文档的 items 多套了一层列表，摊平。"""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list):
            out += _flatten_items(item)
    return out


def build_story(
    ref: PageRef,
    doc: dict[str, Any],
    settings: YysEnv,
    story_pages: dict[tuple[str, str], list[PageRef]],
) -> BuildResult:
    result = BuildResult()
    stories = doc.get("story")
    for story in stories if isinstance(stories, list) else [stories]:
        if not isinstance(story, dict):
            continue
        # 剧情 ID 以文件名为准（页面模板引用的就是它）；文档里的 story_id 有的是名字、有的是别的编号
        story_id = story_id_of(ref)
        inner_id = normalize_text(str(story.get("story_id") or "")).strip()
        story_name = normalize_text(str(story.get("story_name") or "")).strip()
        chapters = [c for c in story.get("chapters") or [] if isinstance(c, dict)]
        # 剧情总称：story_name 常为空，这时用渲染它的页面标题的上级
        # （如「踏雪寻踪/龙灯疑云」→「踏雪寻踪」）
        parents = [
            page.title.rsplit("/", 1)[0]
            for chapter in chapters
            for page in story_pages.get((story_id, str(chapter.get("chapter_id"))), [])
            if "/" in page.title
        ]
        story_title = story_name or (
            max(sorted(set(parents)), key=parents.count) if parents else ""
        )
        if not story_title and inner_id and not inner_id.isdigit():
            story_title = inner_id
        story_title = story_title or f"剧情 {story_id}"

        for chapter in chapters:
            chapter_id = str(chapter.get("chapter_id"))
            chapter_title = normalize_text(str(chapter.get("chapter_title") or "")).strip()
            key = f"story:{story_id}:{chapter_id}"
            pages = story_pages.get((story_id, chapter_id), [])
            title = pages[0].title if pages else f"{story_title}/{chapter_title or chapter_id}"
            blocks: list[Block] = []
            desc = normalize_text(str(chapter.get("chapter_desc") or "")).strip()
            if desc:
                blocks.append(Block(BLOCK_PARA, text=f"本章简介：{desc}"))
            _story_steps(chapter.get("steps") or [], blocks)
            if not has_text(blocks):
                result.skipped.append((key, title, STATUS_EMPTY_CHAPTER))
                continue
            meta: dict[str, Any] = {
                "story_id": story_id,
                "chapter_id": chapter_id,
                "story_title": story_title,
                "chapter_title": chapter_title,
                "data_page": ref.title,
            }
            if chapter.get("series"):
                meta["series"] = str(chapter["series"])
            if len(pages) > 1:
                meta["also_pages"] = [p.title for p in pages[1:]]
            result.documents.append(
                Document(
                    doc_key=key,
                    kind=KIND_STORY,
                    title=title,
                    url=settings.wiki.page_url(pages[0].title if pages else ref.title),
                    source=f"剧情·{story_title}",
                    page_id=pages[0].page_id if pages else ref.page_id,
                    rev_id=pages[0].rev_id if pages else ref.rev_id,
                    blocks=blocks,
                    meta=meta,
                )
            )

        item_blocks: list[Block] = []
        for item in _flatten_items(story.get("items")):
            name = normalize_text(str(item.get("item_name") or "")).strip()
            body = plain_blocks(str(item.get("item_tex") or ""))
            if not body:
                continue
            kind = normalize_text(str(item.get("item_type") or "")).strip()
            heading = f"{name}（{kind}）" if name and kind else name or kind or "未命名"
            item_blocks.append(Block(BLOCK_HEADING, text=heading, level=1))
            item_blocks += body
        if item_blocks:
            result.documents.append(
                Document(
                    doc_key=f"story:{story_id}:items",
                    kind=KIND_STORY,
                    title=f"{story_title}/收集品文本",
                    url=settings.wiki.page_url(ref.title),
                    source=f"剧情·{story_title}",
                    page_id=ref.page_id,
                    rev_id=ref.rev_id,
                    blocks=item_blocks,
                    meta={"story_id": story_id, "story_title": story_title, "data_page": ref.title},
                )
            )
    return result
