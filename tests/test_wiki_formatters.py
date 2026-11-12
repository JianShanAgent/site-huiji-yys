from functools import partial

from jianshanagent.platforms.huiji.wiki import HuijiData
from jianshanagent.plugins.wiki_query import formatters
from jianshanagent.plugins.wiki_query.client import KIND_PAGE, PageContent, SearchHit
from jianshanagent.plugins.wiki_query.formatters import format_disambiguation, format_not_found

from jianshanagent_site_huiji_yys.wiki import (
    KIND_CHARACTER,
    KIND_LABELS,
    KIND_SOUL,
    AskResult,
    YysProvider,
    format_ask,
)

#: 式神、御魂由阴阳师插件排，普通页面由通用的排
format_page = partial(formatters.format_page, providers=[YysProvider(HuijiData())])

URL = "https://wiki.example/wiki/x"


def _character(**overrides: object) -> PageContent:
    data = {
        "name": "山风",
        "rarity": "SSR",
        "gender": "男",
        "cv": {"jp": "增田俊树"},
        "introduction": "居住在森林中的少年妖怪。",
        "tags": ["伤害"],
        "nicknames": ["岚"],
        "skills": [
            {"name": "风", "cost": 0, "passive": False, "description": "攻击敌方目标2次。"},
            {"name": "烈", "cost": 0, "passive": True, "description": "获得「困兽」。"},
            {"name": "斩", "cost": 3, "passive": False, "description": "造成攻击132%伤害。"},
        ],
    }
    return PageContent("山风", KIND_CHARACTER, url=URL, data={**data, **overrides})


def test_format_character() -> None:
    assert format_page(_character()) == (
        "【山风】 · SSR · 男 · CV 增田俊树\n"
        "别名：岚\n"
        "定位：伤害\n"
        "居住在森林中的少年妖怪。\n"
        "1. 风：攻击敌方目标2次。\n"
        "2. 烈（被动）：获得「困兽」。\n"
        "3. 斩（3火）：造成攻击132%伤害。\n"
        f"{URL}"
    )


def test_format_character_with_sparse_data() -> None:
    page = _character(
        rarity="", gender="", cv={"cn": "甲", "jp": "乙"}, tags=[], nicknames=[], introduction=""
    )
    page.url = ""
    assert format_page(page).splitlines()[:2] == [
        "【山风】 · CV 乙（日）/ 甲（中）",
        "1. 风：攻击敌方目标2次。",
    ]


def test_long_text_is_clipped_but_link_is_kept() -> None:
    page = _character(introduction="长" * 500)
    text = format_page(page, max_chars=120)

    assert len(text) <= 120
    assert text.endswith("…\n" + URL)
    assert URL not in format_page(page, max_chars=120, include_link=False)


def test_clipping_does_not_leave_half_a_bold_marker() -> None:
    page = _character(introduction="")
    full = format_page(page, max_chars=2000, markdown=True)
    bold = [index for index in range(len(full)) if full.startswith("**", index)]
    assert len(bold) >= 4  # 标题和技能名是加粗的
    # 让截断处落在各种位置上（加粗记号前、中间、加粗的字里面、后面）：
    # 截出来的正文里加粗都是成对的，不超字数，最后一行还是链接
    for limit in range(len(URL) + 30, len(full)):
        text = format_page(page, max_chars=limit, markdown=True)
        body, _, link = text.rpartition("\n")
        assert len(text) <= limit and link == f"[查看 wiki 页面]({URL})"
        assert body.count("**") % 2 == 0 and "***" not in body, (limit, body)


def test_format_soul() -> None:
    page = PageContent(
        "针女",
        KIND_SOUL,
        url=URL,
        data={
            "name": "针女",
            "attribute": "暴击15%",
            "effect": "暴击时造成无视防御伤害。",
            "special": "",
            "sources": ["御魂副本-周四", "商店-秘魂屋"],
        },
    )
    assert format_page(page) == (
        "【针女】御魂\n"
        "属性：暴击15%\n"
        "效果：暴击时造成无视防御伤害。\n"
        "获取：御魂副本-周四；商店-秘魂屋\n"
        f"{URL}"
    )


def test_format_wiki_page_without_extract() -> None:
    assert format_page(PageContent("首页", KIND_PAGE, url=URL)) == f"【首页】\n{URL}"


def test_format_disambiguation_labels_kinds() -> None:
    text = format_disambiguation(
        "山",
        [
            SearchHit("山风", KIND_CHARACTER),
            SearchHit("山童", KIND_SOUL),
            SearchHit("山中", KIND_PAGE),
        ],
        kind_labels=KIND_LABELS,
    )
    assert text.splitlines() == [
        "「山」有多个相关条目，请发送 /查 <完整名称>：",
        "1. 山风（式神）",
        "2. 山童（御魂）",
        "3. 山中",
    ]


def test_format_not_found() -> None:
    assert "没有找到" in format_not_found("不存在的东西")


def test_format_ask() -> None:
    rows = [{"name": n, "rarity": "SP", "tags": []} for n in ("咲耶", "千姬")]
    assert format_ask(AskResult(rows=rows, total=2), rarity=["SP"], tags=["拉条"]) == (
        "SP + 拉条：共 2 个式神\n咲耶、千姬"
    )
    assert format_ask(AskResult(), rarity=[], tags=["拉条"]) == "没有符合「拉条」的式神。"
    assert format_ask(AskResult(unknown_tags=["拉跳"]), rarity=[], tags=["拉跳"]) == (
        "没有这个标签：拉跳"
    )


def test_format_ask_lists_only_what_fits() -> None:
    rows = [{"name": f"式神{i:03d}", "rarity": "SR", "tags": []} for i in range(200)]
    text = format_ask(AskResult(rows=rows, total=200), rarity=[], tags=["伤害"], max_chars=300)
    header, names = text.split("\n")
    listed = names.split("、")

    assert len(text) <= 300
    assert listed == [row["name"] for row in rows[: len(listed)]]  # 不会截在名字中间
    assert header == f"伤害：共 200 个式神（仅列出前 {len(listed)} 个）"
