# 秋招雷达

一个跑在本机的秋招岗位聚合与筛选工具。它定时抓取公开的校园招聘 / 社会招聘页面，按简历画像匹配技术方向，帮你把岗位先筛一遍，减少手动翻招聘网站的时间。

## 它解决什么

- 汇总多公司、多平台的公开招聘信息到一个页面。
- 区分“校招”和“社招/日常招聘”，默认不混在一起。
- 根据简历画像给岗位打匹配度，并按匹配度排序。
- 每 30 分钟在后台自动刷新，不需要每天手动维护。
- 提供 AI 简历助手，支持文字、PDF、Word、图片，可聊简历修改和岗位方向。

## 运行环境

- Windows 10 / 11
- Python 3.11 或更高版本
- 浏览器（推荐 Chrome / Edge）

## 安装依赖

后端主体使用 Python 标准库，另依赖三个库用于 AI 附件预处理：

- `pypdf`：从普通 PDF 提取文本；
- `pymupdf`：把扫描版 PDF 渲染成图片；
- `rapidocr_onnxruntime`：本地 OCR（中英文），识别图片/扫描页为文字。

如果只用岗位浏览、不打算用 AI 分析简历，后两个可不装，但建议按 requirements 一次装齐。

```powershell
pip install -r requirements.txt
```

可选：接入 job-pro 官方 API 信源需要本机有 Node.js（npm 自带 npx）；不装则只跑核心源 + 校招雷达扩展池。

## 启动方式

方式一：进入项目目录，右键 `启动秋招雷达.ps1`，选择“使用 PowerShell 运行”。

方式二：打开 PowerShell，执行：

```powershell
& '项目所在路径\启动秋招雷达.ps1'
```

启动成功后会自动打开 `http://127.0.0.1:5500/`。如果浏览器没有自动打开，手动访问该地址即可。

停止服务：关闭启动时的 PowerShell 窗口，或在任务管理器中结束对应的 `python.exe`。

## 页面功能

- 岗位列表：显示公司、城市、薪资、方向、标签、匹配度。
- 搜索与筛选：公司 / 岗位 / 关键词、城市、方向、薪资。
- 校招 / 社招：每张卡片有标签；勾选或取消“包含社招/日常招聘”可切换。
- 简历画像：右上角只读展示当前画像。
- AI 悬浮球：左下角，聊天、上传附件、简历建议。
- AI 设置：侧栏按钮，可把 AI 切换到任意 OpenAI 兼容接口（如 DeepSeek），地址/模型/Key 填在网页里，只存本机。
- AI 重建画像：右上角“用 AI 重建画像”，上传简历后自动提取方向/技能/关键词/意向城市，预览确认后更新画像并重算匹配分。
- 投递清单：查看当前匹配度最高的岗位。
- 投递追踪：每张岗位卡片可标记 想投递 / 已投递 / 笔试 / 一面~三面 / HR面 / Offer / 已拒绝 / 已终止，工具栏可按状态筛选，侧栏“投递追踪”总览全部记录；数据保存在本地 SQLite（接口 `GET/POST /api/tracking`）。
- 新岗位标记：最近 24 小时内首次出现的岗位带“新”标签。

## 数据来源与刷新

岗位来自预先配置的公开招聘页，包括公司官方招聘页、校园招聘平台和部分聚合平台。后台线程每 30 分钟自动刷新一次，抓取公开 HTML 中新增的岗位链接。

需要说明：本项目只读取公开网页，不绕过登录、验证码、访问控制或反爬机制。部分招聘页是动态渲染，可能只能解析到公司入口，具体职位数量会受页面开放程度影响。

## 扩展信源池（校招雷达融合）

从开源项目 [xiaozhao-radar](https://github.com/jiabaobei/xiaozhao-radar) 融合了它的数据信源抓取模式，但**岗位筛选、匹配打分、简历画像等全部仍使用本项目自己的定制逻辑**，扩展只发生在“抓哪些源、怎么抓”。

- 扩展源清单：`data/xiaozhao_sites.json`（约 785 个官方招聘站点，按 URL 去重）+ `data/xiaozhao_batches.json`（约 359 个聚合校招批次入口，来自每周同步的 1400+ 条公开表格）。扩展源以 `xz-` / `xb-` 开头，优先级低于你手写配置的核心源，抓到同一岗位时核心源优先保留。
- 刷新节奏：每次后台刷新先完整跑你原来的核心源，再轮询抓扩展池的一小批（默认 25 个，可用环境变量 `EXTRA_BATCH_SIZE` 调整），不会每 30 分钟全量打 800+ 个站点。
- 抓取分层：扩展源优先用 Firecrawl 渲染抓取（需在 `.env` 配 `FIRECRAWL_API_KEY`），失败/未配置时自动用 AnySearch 兜底，都不可用才回退到原来的 urllib 静态抓取。核心源行为保持不变。
- 更新扩展池：偶尔运行 `python sync_xiaozhao_data.py`（本地仓库模式）或 `python sync_xiaozhao_data.py --remote`（直接拉 GitHub 最新），会重新生成 `data/` 下两个 JSON。
- 关闭：把 `.env` 里的 `EXTRA_POOL_ENABLED` 设为 `0`，或删除 `data/` 下两个 JSON 后重启。

## job-pro 官方招聘 API 信源

额外接入了 [job-pro](https://github.com/HA7CH/job-pro)：通过它直连 50 家大厂（腾讯/字节/阿里/美团/宇树/智元/地平线/寒武纪/银河通用等）的官方招聘 API，拿到的是**具体 JD 列表**（标题/城市/投递链接），不再依赖 HTML 抓取。机器上需有 Node.js（npx）。返回的岗位仍先过本项目的 `is_relevant_job` 方向过滤，再按简历画像打分入库，匹配逻辑不变。

> 说明：本项目只使用 job-pro 的**岗位查询**能力；自动投递功能已移除，投递请通过岗位卡片上的官方链接手动完成。

- 公司清单：`data/jobpro_companies.json`（50 家，key 可用于 `JOBPRO_COMPANIES` 过滤）。
- 刷新节奏：每轮后台刷新处理 `JOBPRO_BATCH_SIZE` 家（默认 5，全部跑完约 5 轮）；每家公司最多入库 40 个高分岗位，避免单家刷屏。
- 校招/社招：投递链接含 `social` 的标为社招，其余按校招处理（默认列表只显示校招，可勾选“包含社招/日常招聘”）。
- 关闭：`.env` 里设 `JOBPRO_ENABLED=0`。

## AI 能力说明

AI 有两种驱动方式，按优先级使用：

1. **OpenAI 兼容接口（推荐）**：在网页侧栏“AI 设置”填写 Base URL、模型、API Key（例如 DeepSeek：`https://api.deepseek.com` + `deepseek-chat`），立即生效，Key 只存本机数据库。
2. **Codex / CCSwitch 兜底**：未配置上面的 Key 时，走本机安装的 Codex CLI，模型和 Key 由 CCSwitch 管理。

DeepSeek 等文本模型不能直接读 PDF/图片，本项目在上传时先用 `pypdf`/`pymupdf`/`RapidOCR` 在本机把附件转成文字，再发给模型；附件本身不会上传到 AI 服务。

## 主要文件

- `server.py`：后端主程序，SQLite 岗位库、定时刷新、页面和 API 服务。
- `local_ai_proxy.py`：8765 端口兼容转发层。
- `sync_xiaozhao_data.py`：从校招雷达仓库生成扩展信源池（`data/` 下两个 JSON）。
- `data/xiaozhao_sites.json`、`data/xiaozhao_batches.json`：扩展信源清单。
- `data/jobpro_companies.json`：job-pro 官方 API 公司清单。
- `app.js`：前端逻辑。
- `index.html`：页面结构。
- `styles.css`：样式。
- `启动秋招雷达.ps1`：一键启动脚本。
- `.env.example`：环境变量示例。
- `profile.json`（可选，个人文件）：简历画像；缺失时使用内置的嵌入式/机器人示例画像。
- `qiuzhao.db`（运行后自动生成，个人文件）：岗位、追踪、AI 配置等本地数据，不随项目分发。

## 如何换成自己的简历

项目首次启动会内置嵌入式 / 机器人方向的示例画像。换人或换方向时有两种方式：

- 网页端：右上角“用 AI 重建画像”，上传简历后自动提取并保存（需要先配置 AI Key）；
- 手动：编辑 `profile.json` 里的 `targetRoles`、`skills`、`keywords`、`preferredCities`，然后删除 `qiuzhao.db` 再重启。

## 首次启动

首次启动会自动创建 `qiuzhao.db` 数据库，并写入一组默认的种子岗位。第一次后台刷新可能需要一点时间，页面数据会在刷新完成后自动更新。
