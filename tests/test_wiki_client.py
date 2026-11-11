import pytest
from jianshanagent.plugins.wiki_query.client import KIND_PAGE, NameMatch, SearchHit, WikiError

from jianshanagent_site_huiji_yys.wiki import KIND_CHARACTER, KIND_SOUL
from wiki_fakes import SITE, FakeWiki, make_client, yys_of


async def test_auth_header_is_sent_to_both_endpoints() -> None:
    fake = FakeWiki()
    client = make_client(fake, auth_header="X-authkey", auth_key="k123")
    await client.search("十周年")  # 索引和御魂走 REST，没命中再走 api.php
    await client.aclose()

    assert {"/api/rest_v1/namespace/data", "/api.php"} <= set(fake.paths)
    assert all(request.headers["X-authkey"] == "k123" for request in fake.requests)


async def test_auth_header_is_omitted_without_key() -> None:
    fake = FakeWiki()
    client = make_client(fake, auth_header="X-authkey", auth_key=None)
    await client.search("山风")
    await client.aclose()

    assert "x-authkey" not in fake.requests[0].headers


async def test_cloudflare_challenge_page_raises_wiki_error() -> None:
    client = make_client(FakeWiki(challenge=True))
    with pytest.raises(WikiError, match="非 JSON"):
        await client.search("山风")
    await client.aclose()


async def test_search_puts_exact_name_before_partial_matches() -> None:
    fake = FakeWiki()
    client = make_client(fake)
    hits = await client.search("山风")
    await client.aclose()

    assert hits == [
        SearchHit("山风", KIND_CHARACTER, exact=True),
        SearchHit("初翎山风", KIND_CHARACTER),
    ]
    assert "/api.php" not in fake.paths  # 索引命中时不做全文搜索


async def test_search_resolves_nickname_case_insensitively() -> None:
    client = make_client(FakeWiki())
    assert await client.search("ur狗") == [SearchHit("大天狗·高天铁羽", KIND_CHARACTER, exact=True)]
    assert await client.search("岚") == [SearchHit("山风", KIND_CHARACTER, exact=True)]
    await client.aclose()


async def test_lookup_name_resolves_aliases_redirects_and_offers_candidates() -> None:
    fake = FakeWiki()
    client = make_client(fake)
    highway = NameMatch("大天狗·高天铁羽", KIND_CHARACTER, ("高铁", "UR狗"))
    # 登记的别名（不分大小写）和正式名都对到同一个条目，并带回全部别名
    assert await client.lookup_name(" ur狗 ") == [highway]
    assert await client.lookup_name("大天狗·高天铁羽") == [highway]
    assert await client.lookup_name("针") == [NameMatch("针女", KIND_SOUL, ("针",))]
    # 式神和御魂重名：两个都给
    assert [m.kind for m in await client.lookup_name("荒骷髅")] == [KIND_CHARACTER, KIND_SOUL]
    assert "/api.php" not in fake.paths  # 数据里对得上就不去查页面

    # 页面标题，以及页面的重定向
    assert await client.lookup_name("战斗机制") == [NameMatch("战斗机制", KIND_PAGE)]
    assert await client.lookup_name("十周年") == [NameMatch("十周年游园活动", KIND_PAGE)]
    # 只是名字里包含：当候选，不当定论
    assert await client.lookup_name("高天") == [
        NameMatch("大天狗·高天铁羽", KIND_CHARACTER, exact=False)
    ]
    assert await client.lookup_name("没有这个名字") == []
    assert await client.lookup_name("  ") == [] and await client.lookup_name("风") == []
    await client.aclose()


async def test_search_finds_souls_and_reports_name_collision() -> None:
    client = make_client(FakeWiki())
    assert await client.search("针女") == [SearchHit("针女", KIND_SOUL, exact=True)]
    assert await client.search("荒骷髅") == [
        SearchHit("荒骷髅", KIND_CHARACTER, exact=True),
        SearchHit("荒骷髅", KIND_SOUL, exact=True),
    ]
    await client.aclose()


async def test_search_finds_character_missing_from_index() -> None:
    fake = FakeWiki()
    client = make_client(fake)
    assert await client.search("晴明") == [SearchHit("晴明", KIND_CHARACTER, exact=True)]
    await client.aclose()

    assert "/api.php" not in fake.paths


async def test_search_pages_by_title_before_fulltext() -> None:
    client = make_client(FakeWiki())
    # 「战斗机制」同时出现在另一页的正文里：按标题找，只返回同名页面
    assert await client.search("战斗机制") == [SearchHit("战斗机制", KIND_PAGE, exact=True)]
    assert await client.search("十周年") == [SearchHit("十周年游园活动", KIND_PAGE)]
    # 标题里没有的词，才用全文搜索
    assert await client.search("广州塔") == [SearchHit("十周年游园活动", KIND_PAGE)]
    await client.aclose()


async def test_fulltext_search_asks_for_the_configured_ranking_and_survives_without_it() -> None:
    def rankings(fake: FakeWiki) -> list[str | None]:
        """每次全文搜索请求里带的排法。"""
        return [
            request.url.params.get("srqiprofile")
            for request in fake.requests
            if request.url.params.get("list") == "search"
        ]

    # 不配：不带这个参数，用接口默认的排法
    fake = FakeWiki()
    client = make_client(fake)
    assert await client.fulltext_titles("广州塔") == ["十周年游园活动"]
    assert rankings(fake) == [None]
    await client.aclose()

    # 配了：/查 找页面（先按标题、再全文）和站内搜索都带上
    fake = FakeWiki(rankings=("classic_noboostlinks",))
    client = make_client(fake, search_ranking="classic_noboostlinks")
    assert await client.search("广州塔") == [SearchHit("十周年游园活动", KIND_PAGE)]
    assert await client.fulltext_titles("站位") == ["战斗机制"]
    assert rankings(fake) == ["classic_noboostlinks"] * 3
    await client.aclose()

    # 这个站不认识配的排法（接口报错）：退回默认的排法，照样搜得到
    fake = FakeWiki()
    client = make_client(fake, search_ranking="classic_noboostlinks")
    assert await client.fulltext_titles("广州塔") == ["十周年游园活动"]
    assert rankings(fake) == ["classic_noboostlinks", None]
    # 被拒过以后接下来一阵子不再带它：不是每搜一个词都先白发一个注定失败的请求
    assert await client.fulltext_titles("站位") == ["战斗机制"]
    assert rankings(fake) == ["classic_noboostlinks", None, None]
    client._ranking_off_until = 0.0  # 过了那一阵再试一次（站点恢复了就又用上）
    assert await client.fulltext_titles("十周年版本") == ["战斗机制"]
    assert rankings(fake)[3:] == ["classic_noboostlinks", None]
    await client.aclose()


async def test_an_error_reply_is_not_cached() -> None:
    # 接口报错是 HTTP 200 加 error：不能跟着成功的结果一起缓存，
    # 不然搜索后端一次临时的故障会让同一个词接下来几分钟一直搜不了
    fake = FakeWiki()
    client = make_client(fake)
    fake.search_outages = 1
    with pytest.raises(WikiError, match="temporary problem"):
        await client.fulltext_titles("广州塔")
    assert await client.fulltext_titles("广州塔") == ["十周年游园活动"]
    searches = len(fake.requests)
    assert await client.fulltext_titles("广州塔") == ["十周年游园活动"]  # 成功的照常缓存
    assert len(fake.requests) == searches
    await client.aclose()


async def test_search_handles_misses_blank_queries_and_limit() -> None:
    client = make_client(FakeWiki())
    assert await client.search("不存在的东西") == []
    assert await client.search("   ") == []
    assert len(await client.search("山", limit=1)) == 1
    await client.aclose()


async def test_get_character_uses_index_rarity_and_resolves_placeholders() -> None:
    client = make_client(FakeWiki())
    page = await client.get_page("山风", kind=KIND_CHARACTER)
    await client.aclose()

    assert page is not None and page.kind == KIND_CHARACTER
    assert page.url == f"{SITE}/wiki/%E5%B1%B1%E9%A3%8E"
    data = page.data
    assert data["rarity"] == "SSR"  # 文档里的 grade 是占位值 "0"
    assert data["gender"] == "男"
    assert data["cv"] == {"jp": "增田俊树"}
    assert data["nicknames"] == ["岚"]
    assert data["introduction"] == "居住在森林中的少年妖怪，被称作森林之王。"
    assert [(s["name"], s["cost"], s["passive"]) for s in data["skills"]] == [
        ("风", 0, False),
        ("烈", 0, True),
        ("斩", 3, False),
    ]
    # 技能自带表优先于全局表；全局表补缺；skill 指向同式神技能；都找不到时不泄露内部编号
    assert data["skills"][1]["description"] == (
        "自身「控制效果」结束时获得「困兽」。\n施放『斩』后触发「?」。"
    )


async def test_get_character_outside_index_prefers_canonical_document() -> None:
    client = make_client(FakeWiki())
    page = await client.get_page("晴明")
    await client.aclose()

    assert page is not None and page.kind == KIND_CHARACTER
    assert page.url == ""  # 没有对应页面
    assert page.data["rarity"] == "阴阳师"
    assert page.data["cv"] == {"cn": "杨天翔", "jp": "杉山纪彰"}
    assert [s["name"] for s in page.data["skills"]] == ["基础术式"]


async def test_get_soul_and_kind_selects_between_same_names() -> None:
    client = make_client(FakeWiki())
    soul = await client.get_page("荒骷髅", kind=KIND_SOUL)
    default = await client.get_page("荒骷髅")
    needle = await client.get_page("针女")
    await client.aclose()

    assert soul is not None and soul.kind == KIND_SOUL
    assert soul.data == {
        "name": "荒骷髅",
        "attribute": "攻击加成15%",
        "effect": "造成伤害时，提升伤害。",
        "special": "独立乘区",
        "sources": ["首领御魂副本"],
    }
    assert soul.url.startswith(f"{SITE}/wiki/")
    assert default is not None and default.kind == KIND_CHARACTER
    assert needle is not None and needle.data["sources"] == [
        "御魂副本-周四（主）/六/日",
        "商店-秘魂屋",
    ]


async def test_get_wiki_page_cleans_extract_and_handles_missing() -> None:
    client = make_client(FakeWiki())
    page = await client.get_page("十周年游园活动", kind=KIND_PAGE)
    missing = await client.get_page("不存在的页面")
    not_a_soul = await client.get_page("山风", kind=KIND_SOUL)
    await client.aclose()

    assert page is not None and page.kind == KIND_PAGE
    assert page.extract == "【活动时间】\n9月25日-9月27日\n【活动地点】\n广州塔广场"
    assert page.url == f"{SITE}/wiki/十周年游园活动"
    assert missing is None
    assert not_a_soul is None


async def test_ask_filters_by_rarity_or_and_tags_and() -> None:
    client = make_client(FakeWiki())
    pull = await yys_of(client).ask(tags=["拉条"])
    sp_or_ur_pull = await yys_of(client).ask(rarity=["sp", "UR"], tags=["拉条"])
    extra = await yys_of(client).ask(rarity=["SSR"], tags=["伤害", "间接伤害"])
    limited = await yys_of(client).ask(tags=["伤害"], limit=2)
    nothing = await yys_of(client).ask(rarity=["N"])
    unknown = await yys_of(client).ask(tags=["拉条", "不存在的标签"])
    await client.aclose()

    assert [r["name"] for r in pull.rows] == ["大天狗·高天铁羽", "咲耶"]
    assert [r["name"] for r in sp_or_ur_pull.rows] == ["大天狗·高天铁羽", "咲耶"]
    assert [r["name"] for r in extra.rows] == ["山风"]  # 细分标签也能筛
    assert (len(limited.rows), limited.total) == (2, 4)
    assert (nothing.rows, nothing.total, nothing.unknown_tags) == ([], 0, [])
    assert (unknown.rows, unknown.unknown_tags) == ([], ["不存在的标签"])


async def test_repeated_queries_are_served_from_cache() -> None:
    fake = FakeWiki()
    client = make_client(fake)
    await client.search("山风")
    await client.get_page("山风", kind=KIND_CHARACTER)
    first = len(fake.requests)
    await client.search("山风")
    await client.get_page("山风", kind=KIND_CHARACTER)
    await yys_of(client).ask(tags=["拉条"])
    await client.aclose()

    assert len(fake.requests) == first


async def test_expired_cache_entries_do_not_pile_up() -> None:
    fake = FakeWiki()
    client = make_client(fake, cache_ttl_seconds=0.001)
    # 搜索词每次都不一样：过期的条目只在再查同一个键时才删的话，缓存只增不减
    for number in range(450):
        await client.fulltext_titles(f"词{number}")
    await client.aclose()
    assert len(client._cache._store) < 250


async def test_cache_can_be_disabled() -> None:
    fake = FakeWiki()
    client = make_client(fake, cache_ttl_seconds=0)
    await yys_of(client).data.doc("Data:Soul.json")
    await yys_of(client).data.doc("Data:Soul.json")
    await client.aclose()

    assert len(fake.requests) == 2
