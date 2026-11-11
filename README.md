<p align="center"><img src="assets/icon.svg" width="88" alt="JianShanAgent"></p>

<h1 align="center">site-huiji-yys</h1>

<p align="center">让<a href="https://github.com/JianShanAgent/agent-core">见山</a>读懂<a href="https://yys.huijiwiki.com">阴阳师 wiki</a>。</p>

<p align="center">
  <img src="https://img.shields.io/badge/插件-站点%20·%20站-2f6b5a?style=flat-square&labelColor=1f3b34" alt="站点插件 · 站">
  <img src="https://img.shields.io/badge/id-yys-e9a66b?style=flat-square&labelColor=1f3b34" alt="id: yys">
  <img src="https://img.shields.io/badge/上级-huiji-e9a66b?style=flat-square&labelColor=1f3b34" alt="上级: huiji">
  <img src="https://img.shields.io/github/license/JianShanAgent/site-huiji-yys?label=License&color=2f6b5a&style=flat-square&labelColor=1f3b34" alt="License">
</p>

<sub>JianShanAgent site plugin for the Onmyoji wiki (yys.huijiwiki.com), built on top of the Huiji Wiki platform plugin. Documentation is in Chinese.</sub>

## 它管什么

不装这个插件，见山也能服务阴阳师 wiki——但它只认得「页面」。问「高铁」，它会去搜标题里带这两个字的页面，而不知道这是大天狗·高天铁羽的外号。

这个插件把阴阳师 wiki 自己的东西教给它：

**查得更准**

```text
你    /查 高铁
见山  【大天狗·高天铁羽】 · UR · 男 · CV 前野智昭（日）/ 郑武文杰（中）
      别名：高铁、UR狗、UR大天狗
      定位：伤害
      ……

你    /筛选 SP 拉条
见山  SP + 拉条：共 7 个式神
      灼华桃花妖、纺愿缘结神、禅心云外镜、梦寻山兔、空相面灵气、聆海金鱼姬、稻荷神御馔津
```

- 式神、御魂按正式名和昵称都能查，回复的是稀有度、技能、定位这些结构化的内容，不是页面摘要；
- 多了 `/筛选` 命令和给对话模型用的筛选工具：按稀有度、标签列式神。

**知识库更全**

- 条目页以外的内容也收：大神公告、剧情、数据集里的资料（皮肤获取方式、状态说明、百鬼棋局、超鬼王……）；
- 切片上标好提到了哪些式神、御魂，同名的（御魂「地震鲶」和妖怪同名）不混；
- 图库：立绘、头像、御魂图，供以图搜图用；
- 自动更新时知道哪份数据对应哪个式神的页面，只重取那一页。

**说这个站的话**

- 这个站的页面模板怎么清洗；
- 查证时问题分成哪几类、提示词里怎么称呼这些东西（「同名活动每年都有，看清是哪一期」）；
- 人设。

最后这一类全是配置，没有一行代码，都在 [`config.yaml`](src/jianshanagent_site_huiji_yys/config.yaml) 的 `extend` 段里。
想给自己的 wiki 写插件，这是最值得先看的部分。

## 怎么用

它建在灰机平台上，离不开 [`site-huiji`](https://github.com/JianShanAgent/site-huiji)。把两个都装进和核心同一个环境：
在实例仓库的 `pyproject.toml` 和 `components.lock` 里各加上它们。

实例的 `config/core.wiki.yaml` 照 [`tests/config/core.wiki.yaml`](tests/config/core.wiki.yaml) 写——wiki 的地址、放行请求头，和一行：

```yaml
site: yys          # 只写这一级，灰机平台会被自动带出来
```

灰机的站在 Cloudflare 后面，请求要带放行密钥，填在实例的 `config/secrets.yaml` 的 `wiki.authkey`。
一个完整的例子是 [`instance-yys`](https://github.com/JianShanAgent/instance-yys)。

## 配置

配置名是 `site.yys`，默认值在 [`config.yaml`](src/jianshanagent_site_huiji_yys/config.yaml)，分三段：

| 段 | 管什么 |
|---|---|
| `wiki` | 查询：`/筛选` 命令叫什么、御魂的链接指向哪个页面 |
| `knowledge` | 知识库：公告和剧情的来源、数据集、实体字典、图库怎么选图 |
| `extend` | 对通用配置的补充：清洗规则、提示词里的说法、人设 |

某个部署要改，在实例的 `config/site.yys.yaml` 里只写要改的项。`extend` 段读配置时就并进了通用配置，不能在管理后台里改。

## 开发

```bash
uv sync --extra dev      # 核心和灰机插件用旁边目录里的源码（../agent-core、../site-huiji）
uv run pytest
```

```text
src/jianshanagent_site_huiji_yys/
  plugin.yaml      清单（parent: huiji）
  config.yaml      默认配置
  wiki.py          查询这一侧：式神、御魂、/筛选
  knowledge/       知识库这一侧：取数、公告、剧情、数据集、实体、图库、改动归类、报告
tests/             这个站的测试——也是见山的查询和知识库管线最主要的测试
tools/kb_eval/     知识库检索、以图搜图的评测脚本
```

对真实的 wiki 取数要守规矩：那是别人的生产站点，并发不超过 2、每秒不超过 2 个请求。

插件怎么取名、清单怎么写，见[插件规范](https://github.com/JianShanAgent/agent-core/blob/main/docs/插件规范.md)。全部仓库的目录在[组织主页](https://github.com/JianShanAgent)。

## 许可

代码用 [MIT](LICENSE) 许可。测试数据里引用的 wiki 内容版权归阴阳师 wiki 及其贡献者。
