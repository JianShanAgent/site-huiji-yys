"""knowledge 预处理管线的关键纯函数：清洗、表格转换、切片、去重，以及公告、日期、实体、
章节目录页和数据集文档的规则。

夹具都是小段 HTML 或几行文字，不联网、不读配置文件。
"""

import dataclasses
import json
from pathlib import Path

from jianshanagent.platforms.huiji.knowledge import HuijiKnowledge
from jianshanagent.plugins.knowledge.chunker import ChunkStats, chunk_document
from jianshanagent.plugins.knowledge.cleaner import clean, parse_html
from jianshanagent.plugins.knowledge.collect import RawCorpus
from jianshanagent.plugins.knowledge.dates import (
    AnnouncementRef,
    PageHistory,
    find_dates,
    infer_from_announcements,
    infer_from_created,
    page_date,
)
from jianshanagent.plugins.knowledge.documents import STATUS_NAV, build_page
from jianshanagent.plugins.knowledge.markdown import html_to_blocks
from jianshanagent.plugins.knowledge.models import (
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARA,
    BLOCK_TABLE,
    KIND_DATA,
    KIND_ENTRY,
    Block,
    Document,
)
from jianshanagent.plugins.knowledge.page_dates import (
    CSV_COLUMNS,
    analyse_page,
    csv_rows,
    infer_from_histories,
    infer_pages,
    write_missing_year_csv,
)
from jianshanagent.plugins.knowledge.pipeline import (
    Built,
    apply_dates,
    build_documents,
    deduplicate,
)
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
from jianshanagent.plugins.knowledge.sources import Fetched, Listing, PageRef, single_template_call

from jianshanagent_site_huiji_yys.knowledge import YysPlugin
from jianshanagent_site_huiji_yys.knowledge.collect import PLUGIN_ID, YysCorpus
from jianshanagent_site_huiji_yys.knowledge.datasets import (
    Resolver,
    Roster,
    build_buffs,
    build_month_dialogues,
    build_skins,
    build_super_ghost,
    build_zzq_characters,
    match_key,
    onmyoji_names,
)
from jianshanagent_site_huiji_yys.knowledge.entities import Lexicon, entity_stats, nickname_map
from jianshanagent_site_huiji_yys.knowledge.posts import (
    build_dashen,
    build_wechat,
    strip_wechat_tail,
)
from jianshanagent_site_huiji_yys.knowledge.settings import (
    DatasetSettings,
    EntitySettings,
    PostSource,
    YysConfig,
)
from jianshanagent_site_huiji_yys.knowledge.settings import SourceSettings as YysSources

CLEAN = CleanSettings(
    remove_classes=frozenset({"kb-ignore", "navbox", "toc", "shiki-nav", "tabber-filter"}),
    heading_classes={"skill-name": 1},
    term_classes=frozenset({"attr-name"}),
    prefix_classes={"cost": "鬼火消耗："},
    table_placeholders=frozenset({"/"}),
)


def _blocks(html: str) -> list[Block]:
    root = parse_html(html)
    clean(root, CLEAN)
    blocks, _ = html_to_blocks(root, table_placeholders=CLEAN.table_placeholders)
    return blocks


def _doc(blocks: list[Block], title: str = "页面", key: str = "page:1") -> Document:
    return Document(
        doc_key=key, kind=KIND_ENTRY, title=title, url="https://w/wiki/x", source="wiki 条目",
        page_id=1, rev_id=2, blocks=blocks,
    )  # fmt: skip


def test_clean_removes_noise_and_keeps_content() -> None:
    text = "\n".join(
        b.render()
        for b in _blocks(
            """
            <div class="mw-parser-output">
              <div class="toc">目录 1 技能</div>
              <!-- 注释 -->
              <style>.a{color:red}</style><script>var x = 1;</script>
              <p>正文<b>加粗</b>，<a class="new" href="/i?title=x">未创建的页面</a>，<a
                 href="/wiki/A">站内链接</a>
                 <a class="new" title="文件:Skin 1.png" href="/u">文件:Skin 1.png</a></p>
              <div style="display: none">藏起来的</div>
              <div class="kb-ignore">官方约定忽略</div>
              <p>头像：<span><a href="/wiki/B"><img alt="食灵" src="x.png"></a></span>
                 <img alt="Fire.png" src="y.png"> 名字已写出：<img alt="阎魔" src="z.png">阎魔</p>
              <div class="shiki-nav">式神录导航 <a href="/wiki/C">雪女</a></div>
            </div>
            """
        )
    )
    assert "正文加粗，未创建的页面，站内链接" in text
    assert "食灵" in text  # 只有图片带着名字时，alt 留成文字
    assert text.count("阎魔") == 1  # 旁边已有同样的字，图片不再重复
    for noise in (
        "目录",
        "注释",
        "color",
        "var x",
        "藏起来的",
        "官方约定",
        "式神录",
        "Fire",
        "Skin",
    ):
        assert noise not in text


def test_tabber_labels_and_promoted_headings() -> None:
    blocks = _blocks(
        """
        <h2><span class="mw-headline" id="技能">技能</span></h2>
        <div class="skill"><div class="skill-name">风</div><div class="cost">0</div></div>
        <div><span class="attr-name">攻击</span><span>3404</span></div>
        <h2><span class="mw-headline" id="传记">传记</span></h2>
        <div class="tabber">
          <div class="tabber-content">
            <div class="tabber-item">第一段</div>
            <div class="tabber-item" style="display:none">第二段</div>
          </div>
          <ul class="tabber-filter">
            <li class="tabber-filter-item">传记一</li><li class="tabber-filter-item">传记二</li>
          </ul>
        </div>
        """
    )
    headings = [(b.level, b.text) for b in blocks if b.kind == BLOCK_HEADING]
    assert headings == [(2, "技能"), (3, "风"), (2, "传记"), (3, "传记一"), (3, "传记二")]
    paragraphs = [b.text for b in blocks if b.kind == BLOCK_PARA]
    # 没选中的标签页（display:none）也是正文；按钮文字不再单独出现
    assert paragraphs == ["鬼火消耗：0", "攻击：3404", "第一段", "第二段"]
    assert blocks[0].anchor == "技能"


def test_table_with_merged_cells_and_caption_label() -> None:
    blocks = _blocks(
        """
        <div style="font-weight:bold">SP阶总勋章排名</div>
        <table>
          <tr><th>顺位</th><th>组别</th><th colspan="2">贡献</th></tr>
          <tr><td rowspan="2">1</td><td rowspan="2">鲸</td><td>葛叶</td><td>神无月</td></tr>
          <tr><td>15.2%</td><td>13.1%</td></tr>
          <tr><td>2</td><td>a|b</td><td>/</td><td>/</td></tr>
        </table>
        """
    )
    assert [b.kind for b in blocks] == [BLOCK_TABLE]  # 表格前的短句并成了表题
    assert blocks[0].render().split("\n") == [
        "SP阶总勋章排名",
        "",
        "| 顺位 | 组别 | 贡献 | 贡献 |",
        "| --- | --- | --- | --- |",
        "| 1 | 鲸 | 葛叶 | 神无月 |",
        "| 1 | 鲸 | 15.2% | 13.1% |",  # 跨行的值在每一行重复
        "| 2 | a\\|b | / | / |",  # 第二格有内容，不算占位行；竖线转义
    ]


def test_headerless_tables_degrade_to_list_and_placeholder_rows_dropped() -> None:
    blocks = _blocks(
        """
        <table><tr><td>排名1</td><td>500,000</td></tr><tr><td>排名2</td><td>450,000</td></tr></table>
        <table>
          <tr><th></th><th>文本</th></tr>
          <tr><th>登场</th><td>森林，是我的故乡。</td></tr>
          <tr><th>阵亡</th><td>/</td></tr>
        </table>
        """
    )
    assert blocks[0].kind == BLOCK_LIST
    assert blocks[0].items == ["- 排名1：500,000", "- 排名2：450,000"]
    assert blocks[1].table is not None
    assert blocks[1].table.rows == ["| 登场 | 森林，是我的故乡。 |"]


def test_large_table_split_repeats_caption_and_header() -> None:
    rows = "".join(
        f"<tr><td>{i}</td><td>式神{i}</td><td>{i * 1000:,}</td></tr>" for i in range(1, 81)
    )
    html = (
        '<h2><span class="mw-headline" id="成绩">成绩</span></h2><h3>最终成绩一览</h3>'
        "<div>总勋章排名</div>"
        f"<table><tr><th>顺位</th><th>式神</th><th>总勋章</th></tr>{rows}</table>"
    )
    cfg = ChunkSettings(target_chars=400, max_chars=600, min_chars=60)
    stats = ChunkStats()
    chunks = chunk_document(_doc(_blocks(html), "为崽而战"), cfg, stats)
    assert stats.split_tables == 1 and len(chunks) == stats.table_pieces > 1
    seen_rows: list[str] = []
    for chunk in chunks:
        assert chunk.header == "为崽而战 > 成绩 > 最终成绩一览"
        assert chunk.text.startswith(chunk.header + "\n\n")
        lines = chunk.body.split("\n")
        assert lines[:4] == ["总勋章排名", "", "| 顺位 | 式神 | 总勋章 |", "| --- | --- | --- |"]
        assert all(line.startswith("| ") and line.endswith(" |") for line in lines[2:])  # 没有半行
        assert len(chunk.body) <= cfg.max_chars
        assert chunk.url == "https://w/wiki/x#%E6%88%90%E7%BB%A9"
        seen_rows += lines[4:]
    assert seen_rows == [f"| {i} | 式神{i} | {i * 1000:,} |" for i in range(1, 81)]  # 不丢不重
    row = next(c for c in chunks if "| 15 | 式神15 | 15,000 |" in c.body)
    assert "| 顺位 | 式神 | 总勋章 |" in row.body

    # 同样的内容重跑，ID 不变；内容变了，ID 才变
    again = chunk_document(_doc(_blocks(html), "为崽而战"), cfg, ChunkStats())
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]
    changed = chunk_document(
        _doc(_blocks(html.replace("式神15", "梦山")), "为崽而战"), cfg, ChunkStats()
    )
    assert {c.chunk_id for c in changed} != {c.chunk_id for c in chunks}


def test_long_paragraph_split_on_sentence_boundaries() -> None:
    sentences = [f"这是第{i}句话，讲的是切片不应该把句子切成两半。" for i in range(40)]
    blocks = [Block(BLOCK_PARA, text="".join(sentences))]
    cfg = ChunkSettings(target_chars=200, max_chars=300, min_chars=40)
    chunks = chunk_document(_doc(blocks), cfg, ChunkStats())
    assert len(chunks) > 1
    assert "".join(c.body for c in chunks) == "".join(sentences)
    assert all(c.body.endswith("。") and len(c.body) <= cfg.max_chars for c in chunks)


def test_small_sections_merge_with_headings_and_duplicates_collapse() -> None:
    blocks = [
        Block(BLOCK_PARA, text="导言。"),
        Block(BLOCK_HEADING, text="甲", level=2),
        Block(BLOCK_PARA, text="甲的内容。"),
        Block(BLOCK_HEADING, text="乙", level=2),
        Block(BLOCK_HEADING, text="丙", level=3),
        Block(BLOCK_PARA, text="丙的内容。"),
    ]
    cfg = ChunkSettings(target_chars=200, max_chars=300, min_chars=40)
    chunks = chunk_document(_doc(blocks), cfg, ChunkStats())
    assert len(chunks) == 1 and chunks[0].section_path == []
    assert chunks[0].body == "导言。\n\n## 甲\n\n甲的内容。\n\n## 乙\n\n### 丙\n\n丙的内容。"

    other = chunk_document(_doc(blocks, "另一页", "page:9"), cfg, ChunkStats())
    kept, stats = deduplicate(chunks + other)
    assert [c.doc_key for c in kept] == ["page:1"] and stats["removed"] == 1
    assert kept[0].also_in[0]["title"] == "另一页"


def test_single_template_call() -> None:
    assert single_template_call("{{剧情页面|575|104}}\n") == ("剧情页面", ["575", "104"])
    assert single_template_call("{{扫地工}}") == ("扫地工", [])
    assert single_template_call("{{角色页|296}}\n{{式神录导航}}") is None
    assert single_template_call("正文 {{剧情页面|1|2}}") is None


# ── 公告：来源开关、转发帖、只有标题的文章、微信文末 ──────────────────

DATES = DateSettings(min_year=2016, exclude_prefixes=("首页/",))
HUIJI = HuijiKnowledge()
YYS = YysPlugin(
    HUIJI,
    YysConfig(
        sources=YysSources(
            posts=(PostSource("微信/", "wechat", enabled=False), PostSource("大神/", "dashen"))
        )
    ),
)
#: 核心的设置，装着灰机和阴阳师两个站点插件
SETTINGS = Settings(
    wiki=WikiSettings(api_url="https://w/api.php", site_base="https://w", user_agent="test"),
    fetch=FetchSettings(),
    sources=SourceSettings(),
    clean=CLEAN,
    chunk=ChunkSettings(),
    report=ReportSettings(),
    dates=DATES,
    output_dir=Path("unused"),
    cache_dir=Path("unused"),
    sites=SiteSet([HUIJI, YYS]),
)
#: 阴阳师插件里的函数用的设置
ENV = YYS.env(SETTINGS)
DASHEN_REF = PageRef(page_id=11, title="Data:大神/扫地工/2026-09.json", rev_id=3, namespace=3500)
WECHAT_REF = PageRef(page_id=12, title="Data:微信/2026-09.json", rev_id=4, namespace=3500)
SEPT_14 = 1789344000000  # 2026-09-14 08:00（北京时间）的毫秒时间戳


def _post(post_id: str, text: str, **body: object) -> dict:
    return {
        "id": post_id,
        "type": 4 if "origin" in body else 1,
        "createTime": SEPT_14,
        "content": {"body": {"text": text, **body}},
    }


def test_repost_keeps_comment_and_origin_as_two_sections() -> None:
    origin = {
        "id": "o1",
        "createTime": SEPT_14,
        "appRole": json.dumps({"nick": "墨离"}),
        "content": json.dumps({"body": {"text": "风神的养龙日记：如何快速驯服一条龙"}}),
    }
    deleted = {"type": -1}
    doc = {
        "items": [
            _post("p1", "驯龙高手一目连在线教学", origin=origin),
            _post("p2", "前方鬼切出没", origin=deleted, target={"id": "o2"}),
            _post("p3", "☆9月16日维护更新公告☆\n正文"),
        ]
    }
    first, gone, plain = build_dashen(DASHEN_REF, doc, ENV, set()).documents

    text = "\n".join(b.render() for b in first.blocks)
    assert [b.text for b in first.blocks if b.kind == BLOCK_HEADING] == ["转发语", "原帖"]
    assert text.index("驯龙高手") < text.index("原帖作者") < text.index("风神的养龙日记")
    assert "原帖作者：墨离；发布于 2026-09-14；链接：https://ds.163.com/feed/o1/" in text
    assert first.meta["repost"] is True and first.meta["origin_available"] is True
    assert first.meta["origin_id"] == "o1" and first.meta["origin_author"] == "墨离"

    # 原帖已删：转发语照样入库，原帖一节只留说明和链接
    gone_text = "\n".join(b.render() for b in gone.blocks)
    assert gone.meta["repost"] is True and gone.meta["origin_available"] is False
    assert "原帖已删除或不可见" in gone_text and "https://ds.163.com/feed/o2/" in gone_text

    assert "repost" not in plain.meta


def test_title_only_wechat_article_is_kept_but_not_embedded() -> None:
    doc = {
        "items": [
            {"id": "a1", "title": "只有标题的文章", "createTime": SEPT_14, "url": "http://mp/a1"},
            {
                "id": "a2",
                "title": "有正文的文章",
                "createTime": SEPT_14,
                "blocks": [{"type": "text", "text": "正文第一行，写了不少内容。"}],
            },
        ]
    }
    bare, full = build_wechat(WECHAT_REF, doc, ENV, set()).documents
    assert bare.meta["title_only"] is True and "title_only" not in full.meta
    (bare_chunk,) = chunk_document(bare, ChunkSettings(), ChunkStats())
    (full_chunk,) = chunk_document(full, ChunkSettings(), ChunkStats())
    assert bare_chunk.embed is False and bare_chunk.to_json()["embed"] is False
    assert full_chunk.embed is True and full_chunk.to_json()["embed"] is True


def test_wechat_tail_removed_only_at_the_very_end() -> None:
    body = ["活动开启啦", "pic.@正文里的画师", "△图注：活动界面", "更多内容敬请期待"]
    tail = ["pic.@某画师", "☆全新式神情报☆", "△祝大人们节日快乐", "10月21日维护更新公告"]
    kept, removed = strip_wechat_tail([*body, "为大人们奔波的扫地工 参上", *tail])
    # 落款留下，署名和推荐阅读删掉，「△」开头的图注留下；正文中间的 pic.@ 不动
    assert kept == [*body, "为大人们奔波的扫地工 参上", "△祝大人们节日快乐"]
    assert removed == ["pic.@某画师", "☆全新式神情报☆", "10月21日维护更新公告"]

    # 落款后面跟着长段落：不是「署名 + 推荐阅读」的格式，一行也不删
    long_tail = [*body, "参上", "这是一段很长的正文。" * 12]
    assert strip_wechat_tail(long_tail) == (long_tail, [])
    # 没有落款：文末的「pic.@」后面跟着好几行标题才删；只跟着一两行的当作图注，不删
    assert strip_wechat_tail(body) == (body, [])
    unsigned = ["活动开启啦", "敬请期待", "pic.@某画师", "标题一", "标题二", "标题三"]
    assert strip_wechat_tail(unsigned) == (unsigned[:2], unsigned[2:])
    # 落款离文末太远（后面还有十几行正文）：不删
    far = ["参上", *[f"第 {i} 行" for i in range(12)]]
    assert strip_wechat_tail(far) == (far, [])


def test_disabled_source_gives_no_documents_but_still_dates_entries() -> None:
    wechat = {
        "items": [
            {
                "id": "a1",
                "title": "「为崽而战·浮世之航」9月19日开启！",
                "createTime": SEPT_14,
                "blocks": [{"type": "text", "text": "9月19日维护后开启，敬请期待。"}],
            }
        ]
    }
    dashen = {"items": [_post("p1", "☆9月16日维护更新公告☆\n全新活动即将上线，敬请期待。")]}
    corpus = RawCorpus(
        listing=Listing(pages=[], data={}),
        extras={
            PLUGIN_ID: YysCorpus(
                posts=[("dashen", Fetched(DASHEN_REF, value=dashen))],
                reference_posts=[("wechat", Fetched(WECHAT_REF, value=wechat))],
            )
        },
    )
    built = build_documents(corpus, SETTINGS)
    assert [d.doc_key for d in built.documents] == ["dashen:p1"]
    assert all(r["doc_key"] != "wechat:a1" for r in built.records)
    refs = {a.doc_key: a for a in built.announcements}
    assert refs["dashen:p1"].in_kb is True
    assert refs["wechat:a1"].in_kb is False and refs["wechat:a1"].date == "2026-09-14"
    assert (9, 19) in refs["wechat:a1"].month_days


# ── 条目日期：页面自己写明的优先，其次才推断 ──────────────────────────


def test_find_dates_formats_year_inheritance_and_exclusions() -> None:
    text = (
        "2026年9月23日 2026-09-24 2026/9/25 2026.9.26 2026年10月\n"
        "活动时间：2026年12月30日维护后-1月5日23:59\n"
        "9月25日20:00开放应援\n"
        "生日：9月19日\n"
        "本景点于2002年11月1日建成"
    )
    found = find_dates(text, DATES)
    assert [(m.year, m.month, m.day) for m in found[:5]] == [
        (2026, 9, 23),
        (2026, 9, 24),
        (2026, 9, 25),
        (2026, 9, 26),
        (2026, 10, None),
    ]
    start, end, lone, birthday, historical = found[5:]
    # 范围的后半段没写年份：跟着前半段走，跨年加一；它不算「缺年份」
    assert (start.year, end.year, end.inherited_year) == (2026, None, 2027) and not end.bare
    assert lone.bare and (lone.month, lone.day) == (9, 25)
    assert not birthday.bare and birthday.excluded  # 生日本来就不该带年份
    assert not historical.bare and historical.excluded  # 游戏上线以前的年份不算活动日期


def test_page_date_reads_dates_near_time_keywords_only() -> None:
    assert page_date("神猪战纪", "活动时间：2026年8月12日维护后-8月25日23:59", DATES) == (
        "2026-08",
        "content",
    )
    assert page_date("x", "开启时间：2026-09-23 至 2026-09-27", DATES)[0] == "2026-09"
    assert page_date("x", "开放时间：2026/9/23-2026/9/27", DATES)[0] == "2026-09"
    assert page_date("x", "活动日期：2026.9.23~2026.9.27", DATES)[0] == "2026-09"
    assert page_date("花合战/202008", "随便什么", DATES) == ("2020-08", "title")
    # 正文开头的日期范围（没有「活动时间」字样）
    assert page_date("首页/时尚", "「狮歌粤韵」系列皮肤 2026.11.4-2026.11.17 上架", DATES) == (
        "2026-11",
        "content_lead",
    )
    # 回顾里的年份不是活动日期：取紧跟「活动时间」的那个
    retro = "本活动于2019年3月首次上线，2021年8月复刻。\n活动时间：2026年9月23日-9月27日"
    assert page_date("百鬼夜行", retro, DATES)[0] == "2026-09"
    # 只有回顾、没有活动时间：留空
    assert (
        page_date("百鬼夜行", "本玩法于2019年3月首次上线，沿用至今，内容很多。" * 9, DATES)[0]
        is None
    )
    # 几期活动相隔很久、都写在「活动时间」后面：拿不准，留空
    many = "第一期\n活动时间：2026年3月4日-3月17日\n第二期\n活动时间：2026年9月9日-9月22日"
    assert page_date("为崽而战", many, DATES) == (None, "ambiguous")


def _dated_page(body: str):
    title = "为崽而战·浮世之航"
    page = analyse_page(7161, title, "https://w/wiki/x", "ok", body, DATES)
    announcements = [
        AnnouncementRef(
            "dashen:p1", "「为崽而战·浮世之航」9月19日开启！", "2026-09-14", "https://ds/p1",
            month_days=((9, 19),),
        ),
    ]  # fmt: skip
    pending = infer_pages([page], announcements, [title, "为崽而战"], DATES)
    doc = _doc([Block(BLOCK_PARA, text=body)], title=title, key="page:7161")
    doc.date = page.date
    apply_dates(Built(documents=[doc], page_dates=[page]))
    return page, pending, doc


def test_same_body_infers_without_year_and_uses_own_date_with_year() -> None:
    body = "活动时间：9月23日维护后-9月27日23:59\n应援集结（9月25日20:00-9月27日23:59）期间可更换"

    page, pending, doc = _dated_page(body)
    assert page.date is None and page.needs_inference and page.listed and pending == []
    assert (doc.date, doc.date_inferred) == (None, "2026-09")
    assert doc.date_source["kind"] == "announcement"
    assert doc.date_source["doc_key"] == "dashen:p1" and doc.date_source["date"] == "2026-09-14"
    (chunk,) = chunk_document(doc, ChunkSettings(), ChunkStats())
    assert chunk.header.endswith("（时间：2026-09，据同名公告推断）")
    assert chunk.to_json()["date_inferred"] == "2026-09" and chunk.to_json()["date"] is None

    # 编辑补上年份以后：用页面自己的日期，不再推断，也不再列进「缺年份」表格
    page, pending, doc = _dated_page(body.replace("活动时间：9月", "活动时间：2026年9月"))
    assert (page.date, page.basis) == ("2026-09", "content")
    assert not page.needs_inference and not page.listed and page.inference is None
    assert (doc.date, doc.date_inferred, doc.date_source) == ("2026-09", None, None)
    (chunk,) = chunk_document(doc, ChunkSettings(), ChunkStats())
    assert "推断" not in chunk.header


def test_inference_refuses_conflicting_years_and_stale_creation_time() -> None:
    title = "锦绣南行"
    bare = [m for m in find_dates("活动时间：11月04日维护后-11月17日23:59", DATES) if m.bare]

    def ref(key: str, day: str) -> AnnouncementRef:
        return AnnouncementRef(key, "「锦绣南行」活动开启", day, month_days=((11, 4),))

    one = infer_from_announcements(title, bare, [ref("a", "2026-11-03")], [title], DATES)
    assert (one.date, one.source) == ("2026-11", "announcement")
    # 同名公告分属两个年份（每年都办）：不知道页面写的是哪一年，不采用
    both = [ref("a", "2026-11-03"), ref("b", "2025-11-02")]
    assert infer_from_announcements(title, bare, both, [title], DATES).date is None
    # 公告日期和页面写的月日对不上：不采用
    assert (
        infer_from_announcements(title, bare, [ref("c", "2026-05-03")], [title], DATES).date is None
    )

    created = infer_from_created(
        bare, PageHistory("2026-11-04T03:12:00Z", "2026-11-10T00:00:00Z"), DATES
    )
    assert (created.date, created.source) == ("2026-11", "page_created")
    assert created.date_source() == {"kind": "page_created", "created_at": "2026-11-04"}
    # 页面建了很久还在改：创建时间不能代表正文里的日期
    stale = PageHistory("2024-11-04T03:12:00Z", "2026-11-10T00:00:00Z")
    assert infer_from_created(bare, stale, DATES).date is None
    # 创建时间和正文里的月日对不上
    assert infer_from_created(bare, PageHistory("2026-03-01T00:00:00Z"), DATES).date is None


def test_missing_year_csv_lists_pages_sorted_with_bom(tmp_path: Path) -> None:
    def page(page_id: int, title: str, body: str, status: str = "ok"):
        return analyse_page(page_id, title, f"https://w/wiki/{page_id}", status, body, DATES)

    pages = [
        page(1, "锦绣南行", "活动时间：11月04日维护后-11月17日23:59"),
        page(2, "百鬼棋局", "更新于2026年6月17日和2026年10月21日，相隔很久\n10月21日更新内容"),
        page(3, "洪狐天运", "活动时间：2026年7月22日-8月4日\n7月25日开放第二章"),  # 活动时间带年份
        page(4, "花合战/202008", "8月1日开启"),  # 标题带年份
        page(5, "首页/超鬼王特攻", "11月11日（周三）150% 攻防加成"),  # 首页组件
        page(6, "山风", "生日：2月7日"),  # 生日不该带年份
        page(7, "阴阳师", "一款手游"),
    ]
    infer_from_histories(
        infer_pages(pages, [], [p.title for p in pages], DATES),
        {1: PageHistory("2026-11-04T00:00:00Z")},
        DATES,
    )
    rows = csv_rows(pages)
    assert [r["页面标题"] for r in rows] == ["百鬼棋局", "锦绣南行"]
    assert tuple(rows[0]) == CSV_COLUMNS
    partial, missing = rows
    assert partial["推断的年月"] == "" and "带年份" in partial["备注"]
    assert (
        missing["缺年份的日期共几处"] == "2" and "11月04日" in missing["缺年份的日期原文（前几处）"]
    )
    assert missing["推断的年月"] == "2026-11" and missing["推断依据"] == "页面创建时间：2026-11-04"

    target = tmp_path / "export" / "活动页缺年份.csv"
    assert write_missing_year_csv(target, pages) == 2
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "页面标题,页面链接".encode() in raw


# ── 实体 ──────────────────────────────────────────────────────────────


def test_entities_official_names_are_certain_longest_name_and_strict_names() -> None:
    characters = {
        "characters": [
            {"name": "大天狗", "rarity": "SSR"},
            {"name": "少羽大天狗", "rarity": "SP"},
            {"name": "大天狗·高天铁羽"},
            {"name": "山风"},
            {"name": "荒"},
            {"name": "荒骷髅"},
        ]
    }
    # 御魂数据里现在没有 nickname 字段，照常能跑
    souls = {"soul": [{"name": "心眼"}, {"name": "针女"}, {"name": "荒骷髅"}]}
    cfg = EntitySettings(strict_names=frozenset({"山风", "心眼"}))
    lexicon = Lexicon.build(characters, souls, cfg)
    assert lexicon.aliases == {}

    def names(text: str, title: str = "", confirmed: frozenset[str] = frozenset()) -> list[str]:
        tagged = lexicon.tag(text, title, confirmed)
        # 正式名命中都是确定的，没有 via
        assert all(e["certain"] is True and "via" not in e for e in tagged)
        return [f"{e['name']}/{e['type']}" for e in tagged]

    assert lexicon.tag("大天狗登场") == [{"name": "大天狗", "type": "式神", "certain": True}]
    # 长名优先：「少羽大天狗」不会再标出一个「大天狗」；去掉「·」的写法也认
    assert names("少羽大天狗登场") == ["少羽大天狗/式神"]
    assert names("少羽大天狗和大天狗") == ["少羽大天狗/式神", "大天狗/式神"]
    assert names("大天狗高天铁羽") == ["大天狗·高天铁羽/式神"]
    # 同名的式神和御魂：不硬判，标一个中性类型（见下面中性类型的测试）
    assert names("荒骷髅") == ["荒骷髅/式神/御魂"]
    # 一个字的名字：前后都不是字才算
    assert names("荒川边一片荒凉") == [] and names("一目连、荒、辉夜姬") == ["荒/式神"]
    # 撞词的名字：对白行、自己的页面、紧邻「御魂」才算
    assert names("山风呼啸而过") == [] and names("山风：走吧。") == ["山风/式神"]
    assert names("山风盯着篝火", title="山风") == ["山风/式神"]
    assert names("真是坏心眼") == [] and names("御魂推荐心眼或针女") == ["心眼/御魂", "针女/御魂"]
    # 同一篇文档别处确认过，叙述句里的也算；一个字的名字不走这条
    confirmed = lexicon.confirmed(["山风：走吧。", "山风盯着篝火"])
    assert confirmed == frozenset({"山风"})
    assert names("山风盯着篝火", confirmed=confirmed) == ["山风/式神"]
    assert names("一片荒凉", confirmed=frozenset({"荒"})) == []


def test_nicknames_become_candidates_with_only_structural_rules() -> None:
    characters = {
        "characters": [
            {"name": "咲耶", "nickname": ["刹那", "宵夜"]},  # 两个字、和普通词撞车的昵称也收
            {"name": "毗沙门天", "nickname": "象"},
            {"name": "铃彦姬", "nickname": ["017"]},
            {"name": "大岳丸", "nickname": "DYW"},
            {"name": "白藏主", "nickname": "小白"},
            {"name": "梦山白藏主", "nickname": ["小白", "大白"]},
            {"name": "初音未来"},
            {"name": "丑时之女", "nickname": ["初音未来"]},
            {"name": "大天狗"},
            {"name": "大天狗·高天铁羽", "nickname": ["UR大天狗", "高铁"]},
            {"name": "荒"},
            {"name": "神启荒", "nickname": "神荒"},
            {"name": "荒骷髅", "nickname": "骨"},
        ]
    }
    # 御魂数据里有 nickname（字符串或数组）时同样当候选用
    souls = {
        "soul": [
            {"name": "招财猫", "nickname": "招财"},
            {"name": "地藏像", "nickname": ["地藏"]},
            {"name": "破势"},
            {"name": "荒骷髅"},
        ]
    }
    skin = {"13": {"id": 13, "name": "源博雅", "nickname": ["博雅"], "skins": []}, "_id": "x"}
    assert nickname_map(skin.values()) == {"源博雅": ["博雅"]}
    lexicon = Lexicon.build(
        characters,
        souls,
        EntitySettings(context_words=("御魂",)),
        onmyoji=["源博雅"],
        onmyoji_nicknames=nickname_map(skin.values()),
    )
    summary = lexicon.alias_summary()
    assert summary["total"] == 15 and summary["pairs"] == 16
    assert summary["by_type"] == {"式神": 13, "御魂": 2, "阴阳师": 1}
    assert summary["single_char"] == 2 and summary["multi_target"] == 1
    assert summary["same_as_official"] == ["初音未来"]
    # 「神荒」里包着一个字的正式名「荒」，也算
    assert summary["enclosing_official"] == ["UR大天狗", "神荒"]
    assert summary["blocked_by_official"] == []

    def tag(text: str, title: str = "") -> list[str]:
        return [
            f"{e['name']}/{e['type']}" + ("" if e["certain"] else f"?{e['via']}")
            for e in lexicon.tag(text, title)
        ]

    # 两级标签：昵称命中是候选，via 写命中的昵称
    assert lexicon.tag("刹那之间") == [
        {"name": "咲耶", "type": "式神", "certain": False, "via": "刹那"}
    ]
    # 同一个实体靠两个昵称命中：只出一项，via 写先出现的那个
    assert tag("吃完宵夜的刹那") == ["咲耶/式神?宵夜"]
    # 这一片里已经靠正式名确定了：只留确定的那一项
    assert tag("咲耶在吃宵夜") == ["咲耶/式神"]
    # 确定的排在前面，候选在后
    assert tag("坐高铁去看大天狗") == ["大天狗/式神", "大天狗·高天铁羽/式神?高铁"]
    # 一个字的昵称：前后都不是字，或者在它指向的实体自己的页面里，才出候选
    assert tag("一头大象") == [] and tag("阵容：象、破势") == ["破势/御魂", "毗沙门天/式神?象"]
    assert tag("大象无形", title="毗沙门天") == ["毗沙门天/式神?象"]
    # 数字、字母昵称：紧挨着别的字母或数字的不算；不分大小写，via 用数据里的写法
    assert tag("2017年") == [] and tag("0171") == [] and tag("抽到017了") == ["铃彦姬/式神?017"]
    assert tag("带dyw上场") == ["大岳丸/式神?DYW"] and tag("dywx") == []
    # 一个昵称指向不止一个实体：各出一项；其中一个已经确定的不再出候选
    assert tag("小白：好的") == ["白藏主/式神?小白", "梦山白藏主/式神?小白"]
    assert tag("白藏主就是小白") == ["白藏主/式神", "梦山白藏主/式神?小白"]
    # 昵称和某个正式名相同：正式名照常确定，昵称指向的实体另出候选
    assert tag("初音未来联动") == ["初音未来/式神", "丑时之女/式神?初音未来"]
    # 候选不占位：正式名先匹配，候选只在没被正式名占用的字上匹配
    assert tag("大天狗和高铁") == ["大天狗/式神", "大天狗·高天铁羽/式神?高铁"]
    # 例外是长的写法优先：昵称把一个正式名整个包在里面时，里面的正式名不标，按昵称出候选
    assert tag("UR大天狗登场") == ["大天狗·高天铁羽/式神?UR大天狗"]
    assert tag("ur大天狗和大天狗") == ["大天狗/式神", "大天狗·高天铁羽/式神?UR大天狗"]
    # 文中的正式名比昵称还长：照常标正式名
    assert tag("UR大天狗·高天铁羽登场") == ["大天狗·高天铁羽/式神"]
    assert tag("xUR大天狗") == ["大天狗/式神"]  # 昵称的字母一头紧挨着别的字母，不算昵称
    # 没通过严格规则的正式名（「神荒」里的「荒」）不算占用
    assert tag("神荒很强") == ["神启荒/式神?神荒"]
    # 御魂和阴阳师的昵称
    assert tag("博雅带招财和地藏") == ["源博雅/阴阳师?博雅", "招财猫/御魂?招财", "地藏像/御魂?地藏"]
    # 候选不能当「旁边紧挨着另一个不会认错的御魂名」的证据
    assert tag("招财/荒骷髅都行") == ["荒骷髅/式神/御魂", "招财猫/御魂?招财"]
    assert tag("破势/荒骷髅都行") == ["破势/御魂", "荒骷髅/御魂"]
    # 候选的类型和正式名一样按上下文定
    assert tag("御魂「骨」") == ["荒骷髅/御魂?骨"] and tag("「骨」") == ["荒骷髅/式神/御魂?骨"]

    stats = entity_stats(
        ("story", lexicon.find(text)) for text in ("大天狗", "刹那", "小白和大白", "今天天气不错")
    )
    assert (stats["chunks_certain"], stats["chunks_candidate_only"], stats["chunks_none"]) == (
        1,
        2,
        1,
    )
    assert stats["by_type"] == {"story": [1, 2, 1]} and stats["top"] == [["大天狗", "式神", 1]]
    assert stats["candidates"]["chunks"] == 2 and stats["candidates"]["pairs"] == 3
    assert sorted(stats["candidates"]["by_nickname"]) == [
        ["刹那", ["咲耶"], 1],
        ["大白", ["梦山白藏主"], 1],
        ["小白", ["白藏主", "梦山白藏主"], 1],
    ]


def test_onmyoji_from_data_neutral_soul_types_and_shadow_words() -> None:
    characters = {"characters": [{"id": 585, "name": "荒骷髅"}, {"id": 300, "name": "茨木童子"}]}
    skin = {
        "10": {"id": 10, "name": "晴明", "skins": []},
        "11": {"id": 11, "name": "神乐", "skins": []},
        "300": {"id": 300, "name": "茨木童子", "skins": []},
        "_id": "Data:Skin.json",
    }
    souls = {"soul": [{"name": n} for n in ("荒骷髅", "胧车", "地震鲶", "天羽羽斩", "破势")]}
    cfg = EntitySettings(
        context_words=("御魂", "件套"),
        shadow_words=frozenset({"黑晴明", "神乐铃"}),
        item_souls=frozenset({"天羽羽斩"}),
        monster_souls=frozenset({"地震鲶"}),
        onmyoji_fallback=("源博雅",),
    )
    # 阴阳师名单从数据里来：皮肤数据里有、式神索引里没有的角色；数据里有就不用配置的短名单
    assert onmyoji_names(characters, skin) == ["晴明", "神乐"]
    lexicon = Lexicon.build(
        characters,
        souls,
        cfg,
        onmyoji=onmyoji_names(characters, skin),
        monsters={"胧车": "剧情里的说话人（18 句）", "茨木童子": "剧情里的说话人（9 句）"},
    )
    assert lexicon.onmyoji == ["晴明", "神乐"] and lexicon.onmyoji_source == "data"
    fallback = Lexicon.build(characters, souls, cfg)
    assert fallback.onmyoji == ["源博雅"] and fallback.onmyoji_source == "config"

    def names(text: str, title: str = "") -> list[str]:
        tagged = lexicon.tag(text, title)
        assert all(e["certain"] is True for e in tagged)
        return [f"{e['name']}/{e['type']}" for e in tagged]

    assert names("晴明：神乐，退后。") == ["晴明/阴阳师", "神乐/阴阳师"]
    # 更长的词占住位置：「黑晴明」不是晴明，「神乐铃」不是神乐
    assert names("黑晴明摇了摇神乐铃") == [] and names("源博雅") == []
    # 中性类型：数据认出的妖怪、配置补的妖怪、道具、同时是式神的；和妖怪不同名的御魂不受影响
    assert lexicon.ambiguous == {
        "荒骷髅": "式神/御魂",
        "胧车": "御魂/妖怪",
        "地震鲶": "御魂/妖怪",
        "天羽羽斩": "御魂/道具",
    }
    assert names("乘胧车路过，地震鲶在暴走") == ["胧车/御魂/妖怪", "地震鲶/御魂/妖怪"]
    assert names("须佐之男拔出天羽羽斩") == ["天羽羽斩/御魂/道具"]
    assert names("荒骷髅在咆哮") == ["荒骷髅/式神/御魂"] and names("破势") == ["破势/御魂"]
    # 御魂语境才标「御魂」：紧邻「御魂 / 件套」、小节标题带「御魂」、紧挨着另一个御魂名
    assert names("胧车四件套效果") == ["胧车/御魂"]
    # 隔得远的「御魂」不算：这句说的是胧车这个玩法
    assert names("维护公告\n\n御魂自选次数对胧车同样生效") == ["胧车/御魂/妖怪"]
    assert names("茨木童子 > 御魂搭配\n\n推荐天羽羽斩，输出更高，暴击也不低") == [
        "茨木童子/式神",
        "天羽羽斩/御魂",
    ]
    text = "茨木童子\n\n## 御魂搭配\n\n" + "带上荒骷髅。" + "\n\n## 传记\n\n" + "他曾乘胧车远行。"
    assert names(text) == ["茨木童子/式神", "荒骷髅/御魂", "胧车/御魂/妖怪"]
    assert names("破势/荒骷髅都行") == ["破势/御魂", "荒骷髅/御魂"]
    # 同时是式神的：在它自己的式神页里是式神
    assert names("荒骷髅的技能", title="荒骷髅") == ["荒骷髅/式神"]
    # 同一片里既有明确的又有不明确的提法：只留明确的
    assert names("胧车御魂掉落于此。" + "很久很久以前，" * 4 + "胧车驶过") == ["胧车/御魂"]


# ── 章节目录页 ────────────────────────────────────────────────────────

PAGE_REF = PageRef(page_id=21, title="金砂福威", rev_id=5)


def _chapter(target: str, index: str, name: str) -> str:
    return (
        f'<div><a href="/wiki/{target}" title="{target}"><span><span class="chapter-index">'
        f'{index}</span><span class="chapter-title-text">{name}</span></span></a></div>'
    )


def test_chapter_directory_page_is_kept_with_hierarchy_but_pager_is_not() -> None:
    html = (
        '<div class="mw-parser-output"><h2><span class="mw-headline">活动剧情</span></h2>'
        '<h3><span class="mw-headline">主线</span></h3>'
        + _chapter("金砂福威/初临砂城", "第一章", "初临砂城")
        + _chapter("金砂福威/勇者奇谭", "第二章", "勇者奇谭")
        + '<h3><span class="mw-headline">支线</span></h3>'
        + _chapter("金砂福威/守望", "第一章", "守望")
        + _chapter("别处的页面", "外传", "砂城旧事")
        + "</div>"
    )
    known = {"金砂福威", "金砂福威/初临砂城", "金砂福威/勇者奇谭", "金砂福威/守望", "别处的页面"}
    result = build_page(
        PAGE_REF, {"text": html}, SETTINGS, known_titles=known, story_titles={"金砂福威/守望"}
    )
    (doc,) = result.documents
    assert doc.meta["nav"] is True
    assert doc.meta["nav_links"] == {
        "total": 4, "named": 4, "subpages": 3, "story_pages": 1, "other_pages": 1
    }  # fmt: skip
    (chunk,) = chunk_document(doc, SETTINGS.chunk, ChunkStats())
    assert chunk.embed is True and chunk.meta["nav"] is True
    # 层级留着：剧情名是小节标题，每组链接前有一句带页面名和小节路径的引导语，章节各占一行
    assert chunk.header == "金砂福威"
    assert "## 活动剧情\n\n### 主线" in chunk.body
    assert "「金砂福威」活动剧情 > 主线 的目录（共 2 项）：" in chunk.body
    assert "- 第一章 初临砂城\n- 第二章 勇者奇谭" in chunk.body
    # 链接文字里看不出页面名的，把页面名补在后面
    assert "- 外传 砂城旧事（页面：别处的页面）" in chunk.body

    # 翻页导航：链接很多，但文字只是编号（花合战漫画月份页）——仍然不入库
    months = "".join(f'<a href="/wiki/花合战/2020{m:02d}">{m:02d}</a> ' for m in range(1, 13))
    pager = (
        '<div class="mw-parser-output"><p>本月为漫画花合战</p>'
        f'<p><a href="/wiki/花合战">花合战</a> 2020 {months}</p></div>'
    )
    ref = PageRef(page_id=22, title="花合战/202006", rev_id=1)
    result = build_page(ref, {"text": pager}, SETTINGS, known_titles=None)
    assert result.documents == [] and result.page_status == STATUS_NAV


def test_tab_labels_pair_by_data_attribute_and_icon_badge_becomes_text() -> None:
    settings = CleanSettings(
        remove_classes=frozenset({"tabber-filter"}),
        badge_texts={"battle-win-badge": "（胜）"},
    )
    root = parse_html(
        """
        <div class="tabber">
          <div class="tabber-filter">
            <div class="tabber-filter-item" data-type="event" data-event="1">版本活动</div>
            <div class="tabber-filter-item" data-type="event" data-event="2">联动活动</div>
            <div class="tabber-filter-item" data-type="event" data-event="3">节日活动</div>
          </div>
          <div class="tabber-item" data-event="1"><p>洪狐天运</p></div>
          <div class="tabber-item" data-event="2"><p>神猪战纪</p></div>
        </div>
        <div><div>象方</div><div class="battle-win-badge"><img alt="Win.png" src="w.png"></div>
             <div>狐方</div></div>
        """
    )
    clean(root, settings)
    blocks, _ = html_to_blocks(root)
    rendered = [(b.kind, b.text) for b in blocks]
    # 三个按钮、两块内容：按 data-event 配对，没有内容的「节日活动」不出现
    assert rendered[:4] == [
        (BLOCK_HEADING, "版本活动"),
        (BLOCK_PARA, "洪狐天运"),
        (BLOCK_HEADING, "联动活动"),
        (BLOCK_PARA, "神猪战纪"),
    ]
    # 只有图标的「胜」接到它标注的那一方后面
    assert [text for _, text in rendered[4:]] == ["象方", "狐方（胜）"]


# ── 数据集文档 ────────────────────────────────────────────────────────

DATA_REF = PageRef(page_id=31, title="Data:Skin.json", rev_id=7, namespace=3500)
CHARACTERS = {
    "characters": [
        {"id": 249, "name": "觉", "pageTitle": "觉"},
        {"id": 327, "name": "苍风一目连", "pageTitle": "苍风一目连"},
    ]
}
SKINS = {
    "249": {
        "id": 249,
        "name": "觉",
        "skins": [
            {"skin_id": "01", "skin_name": "默认", "skin_way": "初始皮肤", "date": "", "type": ""},
            {
                "skin_id": "02",
                "skin_name": "金扇",
                "skin_way": "活动获得",
                "date": "2026.10.10",
                "type": "典藏",
            },
        ],
    },
    "11": {"id": 11, "name": "神乐", "skins": [{"skin_id": "01", "skin_name": "默认"}]},
    "_id": "Data:Skin.json",
}
ROSTER = Roster.build(CHARACTERS, SKINS)


def _buff(name: str, desc: str, kind: str, sort: str) -> dict:
    return {"name": name, "buffDesc": desc, "type": kind, "kind": sort}


BUFFS = {
    "buff": {
        "1_1": _buff("沉睡", "使目标睡眠1回合", "减益", "状态"),
        "2_1": _buff("蟹壳", "受到非暴击伤害降低30%。", "增益", "印记"),
        "3_1": _buff("变吸血鬼", "变吸血鬼", "通用", "印记"),
        "4_1": _buff(
            "酒火",
            "每层降低[s_属性1数值_per]防御，<br>"
            "在回合后造成[tips_5]并附加[buff_1_1][extra_（至多3层）]。",
            "减益",
            "印记",
        ),
        "4_2": _buff("沉睡", "使目标睡眠1回合。", "减益", "状态"),
    }
}
TIPS = {"tips": {"5": {"id": 5, "skill_tips_name": "间接伤害"}}}


def test_placeholders_resolve_to_names_and_unresolved_ones_are_kept_and_counted() -> None:
    resolver = Resolver.build(BUFFS, TIPS)
    text = resolver.resolve(
        "造成[tips_5]并附加『[buff_1_1]』，<br>释放『[skill_3353_1]』和[skill_9_1]，"
        "伤害[s_伤害加成_per][extra_（不超过攻击的250%）]",
        {"3353": "螺螺锤"},
    )
    assert text == (
        "造成「间接伤害」并附加『沉睡』，\n释放『螺螺锤』和[skill_9_1]，"
        "伤害[s_伤害加成_per]（不超过攻击的250%）"
    )
    assert dict(resolver.unresolved) == {"[skill_9_1]": 1, "[s_伤害加成_per]": 1}


def test_skin_documents_one_per_character_linking_to_entry_page() -> None:
    documents, stats = build_skins(DATA_REF, SKINS, ROSTER, ENV, known_titles={"觉", "阴阳师"})
    assert [d.title for d in documents] == ["神乐的皮肤", "觉的皮肤"]  # 按编号排
    onmyoji, shikigami = documents
    assert shikigami.kind == KIND_DATA and shikigami.doc_key == "data:skin:249"
    assert shikigami.source == "数据·Data:Skin.json（皮肤获取方式）"
    assert shikigami.url.endswith("/wiki/%E8%A7%89")  # 指向式神的条目页
    assert shikigami.meta["character"] == "觉" and shikigami.meta["role"] == "式神"
    # 阴阳师没有自己的条目页，指向「阴阳师」页面
    assert onmyoji.meta["role"] == "阴阳师"
    assert onmyoji.url.endswith("/wiki/%E9%98%B4%E9%98%B3%E5%B8%88")
    body = "\n".join(b.render() for b in shikigami.blocks)
    # 每一行都带着是谁的皮肤；数据里有的字段才写
    assert "- 觉的皮肤「默认」：获取方式 初始皮肤" in body
    assert "- 觉的皮肤「金扇」：类型 典藏；获取方式 活动获得；日期 2026.10.10" in body
    assert "神乐的皮肤「默认」：数据里没有写获取方式" in onmyoji.blocks[1].render()
    assert stats == {"characters": 2, "onmyoji": 1, "skins": 3, "skins_with_way": 2}


def test_zzq_character_sections_carry_subject_in_every_line() -> None:
    doc = {
        "zzq_character": [
            {
                "hero_name": "觉", "type": "后排妖术类", "location": "后排", "cost": 1,
                "bonds": [{"id": 1, "name": "大江山"}, {"id": 2, "name": "易形"}],
                "HP": {"s1": 5000, "s2": 9000, "s3": 16200},
                "ATK": {"s1": 3250, "s2": 4225, "s3": 5492},
                "DEF": 200, "CR": 0, "CP": 0.5, "Fire": 0,
                "Skill": {
                    "skill1": {"name": "棒打", "fire": 0, "desc": {"lv1": "造成攻击100%伤害。"}},
                    "skill2": {
                        "name": "棒球炸弹", "fire": 2,
                        "desc": {"lv1": "造成攻击100%伤害", "lv2": "造成攻击130%伤害", "lv3": "x"},
                    },
                },
            }
        ]
    }  # fmt: skip
    (document,), stats = build_zzq_characters(
        DATA_REF, doc, Resolver(), ENV, known_titles={"百鬼棋局"}
    )
    assert stats == {"entries": 1} and document.url.endswith("%E7%99%BE%E9%AC%BC%E6%A3%8B%E5%B1%80")
    texts = [b.render() for b in document.blocks]
    assert document.blocks[0].kind == BLOCK_HEADING and texts[0] == "觉"
    assert texts[1] == "百鬼棋局棋子「觉」：1 费，站位 后排，定位 后排妖术类，羁绊 大江山、易形。"
    assert "生命 5000 / 9000 / 16200" in texts[2] and "暴击伤害 150%，初始鬼火 0 点" in texts[2]
    assert texts[3] == "觉的普攻「棒打」：造成攻击100%伤害。"
    assert texts[4].startswith(
        "觉的技能「棒球炸弹」（消耗鬼火 2）（各等级）：\nLv1：造成攻击100%伤害\nLv2："
    )


def test_month_dialogue_speaker_follows_flag_and_trailing_flag_is_removed() -> None:
    doc = {
        "1": {
            "hero_name": "一目连",
            "date": "2020-01",
            "chapters": [
                {
                    "index": 1,
                    "title": "故事一",
                    "desc": "页面上已经有的正文",
                    "dialogue": [
                        {"scene_id": 11, "speaker_id": 0, "flag": 0, "text": "出去走走吧。"},
                        {"scene_id": 0, "speaker_id": 204, "flag": 1, "text": "小白陪您去！,1"},
                        {"scene_id": 327, "speaker_id": 0, "flag": 0, "text": "是你们呀。"},
                        {"scene_id": 0, "speaker_id": 107, "flag": 1, "text": "……"},
                    ],
                }
            ],
        },
        "2": {"hero_name": "面灵气", "date": "2020-06", "chapters": [{"dialogue": []}]},
        "_id": "Data:Monthillustration.json",
    }  # fmt: skip
    settings = dataclasses.replace(
        ENV, cfg=dataclasses.replace(ENV.cfg, datasets=DatasetSettings(speakers={"204": "小白"}))
    )
    documents, stats = build_month_dialogues(
        DATA_REF, doc, ROSTER, settings, known_titles={"花合战/202001"}
    )
    (document,) = documents  # 没有对白的月份（漫画月份）不出文档
    assert document.title == "花合战 2020-01 一目连·剧情对白"
    assert document.url.endswith("/wiki/%E8%8A%B1%E5%90%88%E6%88%98/202001")
    lines = [b.render() for b in document.blocks]
    # flag 为 0 时说话的是 scene_id，为 1 时是 speaker_id；查不到名字的留着编号
    assert lines[2:] == [
        "神乐：出去走走吧。",
        "小白：小白陪您去！",
        "苍风一目连：是你们呀。",
        "角色107：……",
    ]
    assert "页面上已经有的正文" not in "".join(lines)
    assert stats["trailing_flags_removed"] == 1 and stats["unknown_speakers"] == {"角色107": 1}


def test_buffs_only_add_what_the_kb_lacks() -> None:
    # 库里（条目正文）已经有「蟹壳：【增益、印记】受到非暴击伤害降低30%。」
    kb_text = match_key(
        "### 状态与术语说明\n蟹壳：【增益、印记】受到非暴击伤害降低30%。\n使目标睡眠1回合"
    )
    resolver = Resolver.build(BUFFS, TIPS)
    (document,), stats = build_buffs(
        DATA_REF, BUFFS, resolver, kb_text, ENV, known_titles={"技能检索"}
    )
    body = "\n".join(b.render() for b in document.blocks)
    assert "蟹壳" not in body  # 名字和说明挨着出现过：已在库
    assert "变吸血鬼" not in body  # 说明就是名字本身：没有信息量
    # 说明在库里别处出现过、但没和名字挨着：仍然补（「沉睡」）；同名同说明只放一次
    assert body.count("- 沉睡（减益·状态）：使目标睡眠1回合") == 1
    # 占位符换成名字，附加说明去壳，<br> 换成分号，解析不了的原样保留
    assert (
        "- 酒火（减益·印记）：每层降低[s_属性1数值_per]防御，；"
        "在回合后造成「间接伤害」并附加「沉睡」（至多3层）。" in body
    )
    assert stats["added"] == 2 and stats["in_kb"] == 1 and stats["no_information"] == 1
    assert stats["duplicate"] == 1 and dict(resolver.unresolved) == {"[s_属性1数值_per]": 1}
    assert [b.text for b in document.blocks if b.kind == BLOCK_HEADING] == [
        "减益·状态",
        "减益·印记",
    ]


def test_super_ghost_adds_only_missing_skills_and_unrendered_fields() -> None:
    doc = {
        "missions": [
            {
                "star": 6, "battle_time": 180, "escape_time": 5400, "score_mul": 25.575,
                "boss": {
                    "name": "蟹姬",
                    "skills": [
                        {
                            "id": 3351,
                            "name": "左钳接右拳",
                            "desc": "使用蟹钳和拳头攻击敌方目标2次。"
                            "并使下一次释放『[skill_3353_1]』时不会锤晕自己。",
                        },
                        {"id": 3353, "name": "螺螺锤", "desc": "攻击敌方全体2次，造成真实伤害。"},
                    ],
                },
                "minions": [
                    {"name": "蟹姬", "def": 563, "spd": 135},
                    {"name": "飞缘魔", "def": 563, "spd": 146},
                ],
            }
        ]
    }  # fmt: skip
    kb_text = match_key(
        "### 螺螺锤\n攻击敌方全体2次，造成真实伤害。\n使用蟹钳和拳头攻击敌方目标2次。"
    )
    ref = PageRef(page_id=41, title="Data:SuperGhost/2026.json", rev_id=1, namespace=3500)
    (document,), stats = build_super_ghost(
        ref, doc, Resolver(), kb_text, ENV, known_titles={"超鬼王", "超鬼王/2026"}
    )
    assert document.title == "超鬼王 2026·数据补充" and document.url.endswith("/2026")
    body = "\n".join(b.render() for b in document.blocks)
    assert "逃跑时间（escape_time）5400 秒（90 分），积分系数（score_mul）25.575" in body
    assert "6 星超鬼王「蟹姬」的随从速度：蟹姬 135、飞缘魔 146。" in body
    # 说明里有一句不在库里的技能才补；占位符指向同一个首领的技能时换成名字
    assert "6 星超鬼王「蟹姬」的技能「左钳接右拳」" in body and "『螺螺锤』时不会锤晕自己" in body
    assert "的技能「螺螺锤」" not in body
    assert stats["skills"] == 2 and stats["skills_in_kb"] == 1 and stats["skills_added"] == 1
