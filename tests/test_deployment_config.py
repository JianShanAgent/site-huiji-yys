"""一个用了阴阳师插件的部署，读出来的配置是什么样（核心的测试里说的是不带站点插件的样子）。"""

from __future__ import annotations

from jianshanagent.admin.schema import build_schema
from jianshanagent.core.config import load_config
from jianshanagent.core.manifest import installed_sites
from jianshanagent.core.wiki import wiki_subject

from conftest import CONFIG_DIR


def test_naming_the_site_brings_the_platform_and_their_additions() -> None:
    assert {"huiji", "yys"} <= set(installed_sites())  # 两个包都装着、都登记了
    config = load_config(CONFIG_DIR, with_overrides=False)
    # 配置里只写了 yys，上级 huiji 顺着清单带出来
    assert config.get("core.wiki")["site"] == "yys" and config.sites == ["huiji", "yys"]
    assert config.site("yys")["wiki"]["soul_page"] == "御魂检索器"
    # 两个插件的默认配置各在自己的包里
    assert config.source_path("site.yys").parent.name == "jianshanagent_site_huiji_yys"
    assert config.source_path("site.huiji").parent.name == "jianshanagent_site_huiji"

    # 这个站对通用配置的补充，读配置时就并进去了
    research = config.plugin("research")
    assert {"式神", "活动"} <= set(research["analysis"]["topics"])
    assert research["wording"]["generic_words"] == ["式神", "活动", "皮肤"]
    assert research["wording"]["analyst_note"].startswith("同名活动每年都有")
    assert "wiki_filter" in config.plugin("chat")["tools_with_research"]
    assert "kb-ignore" in config.plugin("knowledge")["clean"]["remove_classes"]  # 灰机的约定
    subject = wiki_subject(config)
    assert (subject.bot, subject.intro) == ("阴阳师 wiki 机器人", "阴阳师（网易手游）wiki")


def test_the_additions_cannot_be_edited_from_the_admin_pages() -> None:
    config = load_config(CONFIG_DIR, with_overrides=False)
    for name in ("site.huiji", "site.yys"):
        data = config.get(name)
        fields = build_schema(data, data, {}, name=name)
        locked = {".".join(field["path"]) for field in fields if field["locked"]}
        # 站点插件对通用配置的补充：读配置时就并进去了，在网页上改它不会生效
        assert locked == {"extend"}, name
