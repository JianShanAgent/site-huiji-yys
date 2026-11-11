<p align="center"><img src="assets/icon.svg" width="96" alt="JianShanAgent"></p>

# site-huiji-yys

> JianShanAgent · 站点插件 · 站 · 阴阳师 wiki（yys.huijiwiki.com，建在灰机平台上）
>
> JianShanAgent site plugin for the Onmyoji wiki (yys.huijiwiki.com), built on the Huiji Wiki platform plugin.

| | |
|---|---|
| 层 | 站点插件 · 站 |
| id | `yys`（上级 `huiji`） |
| 包 | 发行包 `jianshanagent-site-huiji-yys` / Python 包 `jianshanagent_site_huiji_yys` |
| 依赖 | 核心 `jianshanagent`、上级插件 `jianshanagent-site-huiji` |
| 登记 | entry points `jianshanagent.sites`：`yys = "jianshanagent_site_huiji_yys"` |

## 这是什么

阴阳师 wiki 特有的东西：

- **查询**（接核心的 `wiki` 扩展点）：式神、御魂按名字和昵称查（数据在灰机的 Data 命名空间里）；
  `/筛选`（按稀有度、标签列式神）和给对话模型用的 `wiki_filter` 工具。
- **知识库**（接 `knowledge` 扩展点）：公告（大神）、剧情、数据集文档、实体标注、图库选图；
  自动更新时认得哪份 Data 对应哪个式神的页面；报告里这个站自己的几节。
- **对通用配置的补充**（`extend`）：这个站的清洗规则、查证和对话提示词里的说法、问题分类、人设。

## 在整套里的位置

JianShanAgent 由几个仓库拼成（并排放在开发机的同一个目录下）：

| 仓库 | 层 | 管什么 |
|---|---|---|
| [`agent-core`](https://github.com/JianShanAgent/agent-core) | 核心 | 机器人本体：QQ 网关、七个功能插件（查询、知识库、查证、对话、记忆、群聊记录、安全兜底）、管理后台、部署工具 |
| [`site-huiji`](https://github.com/JianShanAgent/site-huiji) | 站点插件 · 平台 | 灰机 wiki 平台在 MediaWiki 之上多出来的东西 |
| **[`site-huiji-yys`](https://github.com/JianShanAgent/site-huiji-yys)（这里）** | 站点插件 · 站 | 阴阳师 wiki 特有的东西（建在灰机平台上） |
| [`instance-yys`](https://github.com/JianShanAgent/instance-yys) | 实例 | 阴阳师 wiki 机器人这个部署：配置、用哪个版本、运维备忘 |
| [`template-site-platform`](https://github.com/JianShanAgent/template-site-platform) | 模板 · 站点插件 · 平台 | 接一个新的 wiki 平台时，从它开始 |
| [`template-site-wiki`](https://github.com/JianShanAgent/template-site-wiki) | 模板 · 站点插件 · 站 | 接一个新的 wiki 时，从它开始 |
| [`template-instance`](https://github.com/JianShanAgent/template-instance) | 模板 · 实例 | 新建一个部署时，从它开始 |

## 目录

```
src/jianshanagent_site_huiji_yys/
  plugin.yaml      清单（kind: site，parent: huiji）
  config.yaml      默认配置，三段：wiki（查询）、knowledge（知识库）、extend（对通用配置的补充）
  wiki.py          查询扩展点：YysProvider
  knowledge/       知识库扩展点：YysPlugin（取数、公告、剧情、数据集、实体、图库、改动归类、报告）
tests/             这个站的测试；也是查询和知识库管线的主要测试（拿这个站的数据写的）
  config/          「一个阴阳师的部署」的示例配置，测试里用
tools/kb_eval/     知识库检索、以图搜图的评测脚本
```

## 安装和启用

装进和核心同一个 Python 环境（实例仓库的 `components.lock` 里列上它和 `site-huiji`）。
实例的 `config/core.wiki.yaml` 写成 [`tests/config/core.wiki.yaml`](tests/config/core.wiki.yaml) 那样：
wiki 的地址、放行请求头，和 `site: yys`（只写这一级，灰机平台会自动带出来）。
放行密钥在实例的 `config/secrets.yaml` 的 `wiki.authkey`。

## 配置

配置名 `site.yys`，默认值在 `src/jianshanagent_site_huiji_yys/config.yaml`；某个部署要改，
在实例的 `config/site.yys.yaml` 里只写要改的项。`extend` 段不能在管理后台里改
（它在读配置时就并进了 `plugin.knowledge`、`plugin.research`、`plugin.chat` 等通用配置，要改去改那几份）。

## 开发和测试

```bash
uv sync --extra dev      # 核心和上级插件用旁边目录里的源码（../agent-core、../site-huiji）
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

对真实 wiki 取数要守规矩：并发不超过 2、每秒不超过 2 个请求（那是别人的生产站点）。

## 相关文档

- [插件规范](https://github.com/JianShanAgent/agent-core/blob/main/docs/插件规范.md)：分层和命名、清单、登记、文档格式
- 核心的 [部署与运维](https://github.com/JianShanAgent/agent-core/blob/main/docs/部署与运维.md)「知识库自动更新」一节
- 许可：[MIT](LICENSE)
