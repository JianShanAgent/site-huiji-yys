"""这个插件合不合 JianShanAgent 的插件规范（核心仓库的 docs/插件规范.md）。"""

from jianshanagent.testing.conformance import check_plugin


def test_conforms_to_the_plugin_spec() -> None:
    manifest = check_plugin("jianshanagent_site_huiji_yys")
    assert (manifest.id, manifest.platform) == ("huiji_yys", "huiji")
    assert set(manifest.capabilities) == {"wiki", "knowledge", "extend"}
