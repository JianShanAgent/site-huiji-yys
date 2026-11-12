<p align="center"><img src="assets/icon.svg" width="88" alt="JianShanAgent"></p>

<h1 align="center">site-huiji-yys</h1>

<p align="center"><a href="https://github.com/JianShanAgent/agent-core">JianShanAgent（见山）</a>的<a href="https://yys.huijiwiki.com">灰机阴阳师 Wiki</a> 站点插件。</p>

<p align="center">
  <img src="https://img.shields.io/badge/插件-站点-2f6b5a?style=flat-square&labelColor=1f3b34" alt="站点插件">
  <img src="https://img.shields.io/badge/id-huiji__yys-e9a66b?style=flat-square&labelColor=1f3b34" alt="id: huiji_yys">
  <img src="https://img.shields.io/badge/平台-huiji-e9a66b?style=flat-square&labelColor=1f3b34" alt="平台: huiji">
  <img src="https://img.shields.io/github/license/JianShanAgent/site-huiji-yys?label=License&color=2f6b5a&style=flat-square&labelColor=1f3b34" alt="License">
</p>

<sub>JianShanAgent site plugin for the Onmyoji Wiki (`yys.huijiwiki.com`), built on the Huiji platform support bundled with the core. Implements structured shikigami/mitama lookups, nickname resolution, `/筛选` filtering, announcements/story ingestion, entity disambiguation, and reverse image search curation.</sub>

## 插件简介

如果不装这个插件，见山虽然也能直接连上阴阳师 Wiki，但它只能把站点当成普通的词条集合——玩家问 `/查 高铁`，它只会去全文搜索带“高铁”两个字的页面，不知道这是「大天狗·高天铁羽」的玩家黑话；也没法按稀有度和功能标签筛选式神。

灰机平台的东西（`Data:` 命名空间和它的查询接口）核心自带；本插件在它之上，把阴阳师 Wiki 的结构化数据、黑话体系与多源资料完整接入见山：

### 1. 结构化直查与筛选指令

```text
用户  /查 高铁
见山  【大天狗·高天铁羽】 · UR · 男 · CV 前野智昭（日）/ 郑武文杰（中）
      别名：高铁、UR狗、UR大天狗
      定位：伤害
      ……（完整技能描述、御魂搭配与词条直达链接）

用户  /筛选 SP 拉条
见山  SP + 拉条：共 7 个式神
      灼华桃花妖、纺愿缘结神、禅心云外镜、梦寻山兔、空相面灵气、聆海金鱼姬、稻荷神御馔津
```

- **式神与御魂精准卡片**：支持正式名称与玩家常用外号/简称直查，直接读取灰机 `Data:` JSON 数据返回稀有度、声优、技能数值、定位等结构化内容。
- **`/筛选` 指令与大模型工具**：支持按稀有度（UR / SP / SSR / SR / R）与定位标签组合筛选式神，同时注册为工具供对话模型在自然语言问答中调用。

### 2. 多源知识库扩展与以图搜图

- **扩展词条之外的数据源**：除普通 Wiki 条目外，自动抓取并切片网易大神更新公告、主线与活动剧情、以及 `Data:` 数据集中的结构化资料（皮肤获取途径、战斗状态说明、百鬼棋局、超鬼王等）。
- **同名实体标注与消歧**：为每个知识切片打上涉及的式神、御魂实体标签，区分同名概念（例如御魂「地震鲶」与首领妖怪「地震鲶」）。
- **立绘与图标图库构建**：收集皮肤立绘、式神立绘、式神头像、御魂图标、绘卷配图等（现在约 3,000 张），供 QQ 发图提问时做以图搜图识别。
- **精准增量更新映射**：在 `kbsync` 每小时自动同步时，精确识别某份 `Data:` JSON 改动对应哪一位式神的词条页，只重抓受影响的那一页。

### 3. 单站清洗规则、查证提示词与人设（纯配置实现）

在 [`config.yaml`](src/jianshanagent_site_huiji_yys/config.yaml) 的 `extend` 段中，不写代码直接补充了阴阳师 Wiki 专属的 HTML 模板清洗规则、查证问题分类、同名周期性活动（如“为崽而战”“超鬼王”每年多期）的核对提示以及机器人说话人设——**如果你想为自己的游戏 Wiki 写插件，这一段是最直观的参考模板。**

## 如何安装使用

**在管理后台里一键添加（最省事）**：打开机器人管理后台的「Wiki」页，在站点目录里选平台「灰机 wiki」，点「阴阳师 Wiki」的「添加」。机器人会下载本插件最新的发布、核对校验和后装上，`/查` 马上能用，不用重启；知识库在同一页里点「建知识库」。

**随部署装进环境**：在你的实例仓库（参考线上实例 [`instance-yys`](https://github.com/JianShanAgent/instance-yys) 或模板 [`template-instance`](https://github.com/JianShanAgent/template-instance)）的 `pyproject.toml` 中加入本插件：

```toml
dependencies = [
  "jianshanagent @ git+https://github.com/JianShanAgent/agent-core.git",
  "jianshanagent-site-huiji-yys @ git+https://github.com/JianShanAgent/site-huiji-yys.git",
]
```

再在实例仓库的 `config/core.wiki.yaml` 中写入（参考 [`tests/config/core.wiki.yaml`](tests/config/core.wiki.yaml)）：

```yaml
name: "阴阳师"
about: "网易手游"
wikis:
  - id: huiji                # 这个 wiki 在部署里的名字：知识库的目录名、密钥的键
    label: "灰机 wiki"       # 挂了不止一个 wiki 时，回答里标出处用
    api_url: "https://yys.huijiwiki.com/api.php"
    auth_header: "X-authkey"
    site: huiji_yys          # 它在灰机上写在它自己的清单里，核心带着的灰机平台会先加载
```

`wikis` 是一份名单：同一个部署还可以再挂别的 wiki（比如 BWiki 上的阴阳师），各用各的站点插件，资料合在一起检索、回答里标明出处。

两种装法都要把灰机 Cloudflare 放行密钥填入实例 `config/secrets.yaml` 的 `wiki.huiji.authkey`（中间那一段是这个 wiki 的 `id`），然后重启机器人（随部署装的：`uv sync && uv run jianshanagent` 或 `docker compose up -d --build`）。

## 配置说明

插件默认配置位于 [`src/jianshanagent_site_huiji_yys/config.yaml`](src/jianshanagent_site_huiji_yys/config.yaml)，逻辑名为 `site.huiji_yys`，分为三部分：
- `wiki`：控制 `/筛选` 指令名及御魂链接跳转目标页；
- `knowledge`：控制公告、剧情、数据集、实体词典与图库的采集规则；
- `extend`：注入核心通用配置的清洗规则、查证分类提示与默认人设。

如需在部署实例中调整参数，新建 `config/site.huiji_yys.yaml` 仅填写需要覆盖的字段即可。

## 目录结构与本地开发

```text
site-huiji-yys/
├── src/jianshanagent_site_huiji_yys/
│   ├── plugin.yaml      # 插件清单（id: huiji_yys, platform: huiji）
│   ├── config.yaml      # 默认配置与 extend 扩展项
│   ├── wiki.py          # 查询侧实现：式神、御魂卡片与 /筛选
│   └── knowledge/       # 知识库侧实现：取数、公告、剧情、数据集、实体、图库、增量同步归类
├── tests/               # 单元测试与管线集成测试（含查询与知识库核心回归用例）
└── tools/kb_eval/       # 知识库检索命中率与以图搜图准确率离线评测脚本
```

本地开发跑测试时，将 [`agent-core`](https://github.com/JianShanAgent/agent-core) 并排放在同级目录（`../agent-core`）后执行：

```bash
uv sync --extra dev
uv run pytest
```

**发版**：改 `pyproject.toml` 里的 `version` 并提交，打一个带附注的标签 `v<版本>` 推上来，[发版流程](.github/workflows/release.yml)会跑测试、打安装包并发 Release；管理后台的一键添加和「更新插件」取的就是最新的 Release。

> **礼貌抓取提醒**：测试默认使用离线桩数据；若对真实线上阴阳师 Wiki 执行取数或评测，请严格保持默认限速（并发不超过 2、每秒不超过 2 个请求）。

## 开源许可

本仓库代码基于 [MIT License](LICENSE) 开源；测试夹具中引用的词条片段版权归灰机阴阳师 Wiki 及其贡献者所有。
