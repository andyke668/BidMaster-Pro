# 更新日志（Changelog）

本文件记录本仓库的所有重要变更。

- 格式遵循 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)
- 版本号遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)
- 提交信息遵循 [Conventional Commits 1.0.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/)

> 本仓库 fork 自 [guangshu100/BidMaster-Pro](https://github.com/guangshu100/BidMaster-Pro)。
> **`0.1.0` = fork 时的上游基线**（`cc89d90`），本文件不追溯上游历史。
> `0.1.0` 之后的全部条目均为本 fork 的自研改动，且都已在内网 `192.168.50.31`
> （Docker Compose：`postgres` + `api` + `web`，精简服务集）实际部署并逐项验证。

---

## [Unreleased]

### ✨ 新增（Added）

- **投标审查固定 Excel 模板**：输出固定为 12 个彩色标签页，顺序为项目信息、审查概要与风险、评分项分析与预测、废标项核查、分项报价、交付时间对比、时间节点、废标项核对(BidMaster)、▲参数核对、证明材料清单、时间节点核对、合同条款要点。
- 新增项目基础信息抽取维度，用于自动填充项目信息表；评分项提示词强制返回单一 `predicted_score` 和预测理由。
- 高/中/低风险行使用统一底色；缺失页保留标准“待补充”占位，保证每次下载的报告结构一致。

### 🔧 调整（Changed）

- Excel 组装逻辑从审查技能拆分到 `tender_review_template.py`，后续模板调整不再影响模型审查流程。
- 前端审查阶段和文案同步为固定 12 页模板口径。

---

## [0.3.0] - 2026-09-09

**11 个提交 · 7 个文件 · +1190 / −20**

一句话总结：新增「投标文件审查」双文件交叉审查工作流，分别上传招标文件和投标书后进行六维度比对、
输出可溯源 Excel 报告；同时修复该链路进度丢失、任务 ID 注入错误和审查超时问题。

### ✨ 新增（Added）

- **投标文件审查工作流** (`1e1c6e6`)
  - 新增检查页 `tenderReview` 模式：独立上传**招标文件**与**投标书**，填写投标方公司名称和招标方学校名称。
  - 后台按六个维度交叉审查：废标项核对、评分项响应、▲参数核对、证明材料清单、时间节点核对、合同条款要点。
  - 完成后生成并支持下载 `投标文件审查_公司名称_学校名称.xlsx`；每个工作表包含要求、原文出处、投标书响应、偏离/风险与建议。
  - 报告坚持“列清单与事实，不下结论”原则，关键条目保留原文行号/章节，便于逐条溯源。
  - 新增 `POST /check/tender-bid-review` 提交任务和 `GET /check/tender-bid-review/download/{file_name}` 下载 Excel；任务状态继续复用 `GET /check/task/{task_id}`。

### 🚀 性能（Performance）

- **按维度定位关键条款，避免长文全量反复进入模型** (`6580096`)
  - 对招标/投标文本建立轻量索引，并按审查维度提取关键条款和上下文，压缩进入 LLM 的上下文。
- **关闭投标审查思考模式** (`12f9e5a`)
  - `qwen3.8-max` 默认思考输出会额外消耗推理时间，是此前审查长时间无结果的根因之一。
  - 通过 `chat_template_kwargs.enable_thinking=false` 为 `check` Agent 关闭该工作流的思考模式。

### 🐞 修复（Fixed）

- **投标审查任务进度绑定错误** (`787f64e` `4cd8e6e` `1c41652` `fe07526`)
  - 预生成业务任务 ID，并确保 `TaskManager.submit` 注入的业务函数收到同一个 `task_id`。
  - 修复后台审查与前端轮询访问不同任务的问题，进度百分比、阶段文案和各维度状态现在真实更新。
- **任务管理器 ID 泄漏** (`e160749`)
  - 避免 TaskManager 自身任务 ID 覆盖业务函数接收到的任务 ID。
- **LLM 扩展参数透传** (`e31d694`)
  - `LLMGateway.chat` / `collect_json` 支持 `extra_body` 透传，`get_agent_gateway` 的默认选项可真正到达供应商请求体。
- **上传文件可删除** (`fc105ad`)
  - 招标文件、投标书上传后提供删除入口，可在开始审查前纠正错误上传。

### 🧪 本轮验证（192.168.50.31 实测）

| 用例 | 结果 |
|---|---|
| 小文件审查 | **19.9s** 完成，6 个维度全部成功 |
| 真实 Word 文档审查 | **84.5s** 完成，提取 **127** 条发现 |
| Excel 下载 | HTTP 200，文件为合法 XLSX，文件名格式符合 `投标文件审查_公司_学校.xlsx` |
| 进度 | 提交后可查询任务，前端展示总进度和维度进度，完成后可下载 |

---

## [0.2.0] - 2026-09-08

**24 个提交 · 34 个文件 · +2433 / −1211**

一句话总结：把「点了没反应」「全是异常」「导出全废」这类**静默失败**逐个挖到根因，
把两条耗时链路（AI 解读、投标检查）从同步 HTTP 改成**异步任务 + 轮询**，
并补齐自定义 LLM 供应商、老式 `.doc` 读写、资讯中心数据源治理与 PostgreSQL 部署链路。

### ⚠️ 破坏性变更（BREAKING CHANGES）

| 变更 | 影响与迁移方式 | 提交 |
|---|---|---|
| `POST /api/check/upload-check` 由**同步返回结果**改为返回 `{task_id, status:"pending", ...}` | 结果需轮询 `GET /api/check/task/{task_id}`，`status=completed` 时载荷在 **`result`** 字段里；仍按旧契约直接读 `data` 的客户端会拿到空。文件解析仍在提交请求内同步做，格式问题立刻以 400 反馈 | `05dbf5a` |
| `POST /api/interpret/interpret/{project_id}` 由同步改为异步任务 | 同上，轮询 `GET /api/interpret/task/{task_id}`；实测提交耗时 **19ms**（原同步需约 11 分钟） | `48e91a3` `87e623e` |
| `UVICORN_WORKERS` 生产环境**必须为 1** | `core/task_manager.py` 的 `TaskManager` 是**进程内单例**，解读/生成/检查的「提交 + 轮询」要求两次请求落在同一个 worker；>1 会随机出现轮询 404「任务不存在」。compose 默认值仍是 2，`.env` 必须显式写 1 | `87e623e` `05dbf5a` |
| `llm_provider_configs` 新增 `models TEXT` 列 | 新库由启动 `create_all` 自动建列；**存量库需手动**执行 `ALTER TABLE llm_provider_configs ADD COLUMN IF NOT EXISTS models TEXT` | `2162904` |
| api 镜像新增系统依赖 | `antiword`、`catdoc`（读老式 `.doc`）、`libreoffice-writer-nogui`、`fonts-noto-cjk`（写 `.doc` / 转 PDF，缺字体整篇豆腐块）；镜像体积 1.31GB → **1.73GB** | `05f79ba` `80110fe` |
| 移除依赖 `sentence-transformers` | 它会拖入约 4GB 的 PyTorch，而仅 `BMP_EMBEDDING_MODE=local` 才惰性导入。需要本地 Embedding 时自行加回并预留 ~5GB 磁盘 | `561fabf` |
| `db/init_pg.sql` 语义改变 | 由「建表 + 灌种子」改为**只建扩展的安全引导**，表结构交给应用启动时的 `create_all`（与 `models.py` 永远同步），种子数据抽到新增的 `db/seed_pg.sql`；原文件备份为 `db/init_pg.sql.orig` | `789fa1b` `ca0f8c9` |

### ✨ 新增（Added）

- **自定义 LLM 供应商（OpenAI 兼容网关）+ 智能体模型配置真正生效** (`2162904`)
  - 新增 `POST /api/llm/fetch-models`：支持 `config_id` 复用已存 Key（编辑时 Key 不经前端回传）；`api_base` 非 `/v1` 结尾时自动兜底尝试 `/v1/models`；兼容 `{"data":[{"id":...}]}` / `{"models":[...]}` / 纯数组三种响应。
  - 新增 `services/llm_factory.py:get_agent_gateway(db, agent_name)`：按 `AgentConfig.config["model"]`（存储格式 `provider_id/model`）解析出该 Agent 专用网关，缓存键含 `updated_at`，改配置自动失效；Agent 未选模型则回落 env 默认网关。
  - 接线 4 个路由共 **47 处**调用点：interpret(5) / check(26) / generate(9) / format_doc(7)，**skill 层零改动**（网关的 `default_model` 即所选模型）。
  - `provider_id=custom` 时后端自动生成唯一 `custom_<8hex>`，多个自定义网关实例的同名模型不再歧义。
- **老式二进制 `.doc` 解析通道** (`05f79ba` `07c2a55`)：`DocxParser.parse` 先嗅探 magic bytes —— `PK\x03\x04`(zip)→python-docx、`D0CF11E0`(OLE2)→老式 `.doc` 通道、`%PDF`→转交 `PdfParser`、未识别→友好报错「另存为 .docx」；OLE2 走 `antiword -m UTF-8.txt`，失败回退 `catdoc -d utf-8`。
- **`core/http_headers.py:content_disposition()`** (`4aa592c`)：统一做 RFC 5987 百分号编码的下载响应头，替换全仓 7 处裸拼。
- **并发度可配环境变量** (`87e623e` `05dbf5a`)：`BMP_INTERPRET_MAX_CONCURRENT`（默认 3）与 `BMP_CHECK_MAX_CONCURRENT`（默认 8，钳制 1-15），均已在 `docker/docker-compose.yml` 登记。
- **`db/seed_pg.sql`** (`789fa1b` `ca0f8c9`)：种子数据独立成文件，在 api 健康后用 `psql -v ON_ERROR_STOP=1` 灌入。
- **`NewsItem` 新增字段** `region` / `owner_org` / `project_code` / `bid_deadline` / `amount` (`1df3244`)：这正是「72 条清一色 56 分」的根因（评分里 region 15% + amount 20% 恒为 0）。
- **配置驱动的 `HTMLFetcher`** (`1df3244`)：抓取规则全部写在 `sources.yaml` 的 `config` 段，新增站点不用改代码（只抓列表页、不追详情）；`sync_sources_to_db` 增加 **prune**（YAML 删掉的源在 DB 自动停用）与 `force_enabled` 开关。
- **前端错误可见性** (`05dbf5a`)：检查详情卡新增「执行异常原因」块 + 悬浮提示；失败行的风险等级/是否严重问题显示 `-`；单项检查失败改为原因面板（此前失败会渲染成「风险等级：低风险」+ 空 `{}`，看起来像通过）。
- **在途登记防重复提交** (`48e91a3`)：同一项目重复提交解读返回 **409**，不再把 LLM 网关压垮。

### 🐛 修复（Fixed）

#### 投标检查（check）
- **上传模式「全面检查」15 项全部判「异常」** (`05dbf5a`)：`upload_and_check` 的嵌套闭包 `_run_upload_skill` 用 `importlib.import_module` 加载 skill，而**同一函数靠后的单项分支又写了一句 `import importlib`**（模块级第 4 行本已有）。函数内 `import` 等价于赋值、作用域是**整个函数**，于是 `importlib` 成为该函数的局部名并遮蔽模块级导入，闭包只能把它当外层自由变量读；`fullCheck` 分支在 `asyncio.gather` 处就跑完 15 项、**永远执行不到那句 import**，cell 为空 → 15 项齐抛 `cannot access free variable 'importlib'`，再被兜底 `except` 吞成 `success=False`，前端只剩「异常」两个字。项目模式 `_do_full_check` 仅因 `import importlib` 写在嵌套 def **之前**（差两行）而幸免。
- **`upload-check` 单项分支 500** (`dd3adbf`)：提交响应的 `message` 是三元表达式，`fullCheck` 分支是普通字符串（`{task_id}` 只是字面占位、不求值），单项分支是 f-string，而作用域里只有 `task`、没有裸 `task_id` → `NameError` → 500「服务器内部错误」，上传模式单项检查整个不可用。
- 三处各写一遍的 skill 加载逻辑收敛为 `_exec_check_skill()`，返回结构统一为 `{success, data, error, warnings}`（原失败分支缺 `data`/`warnings`，而 8 个 skill 是会产出 warnings 的）；15 项清单与参数装配抽为 `_FULL_CHECK_TYPES` / `_full_check_params()`，项目模式与上传模式共用一份，避免各自维护漂移 (`05dbf5a`)。
- 两条链都加 `asyncio.Semaphore` 限流：裸 `gather` 会把 15 路请求一次压到网关上，触发限流即 15 项集体失败 (`05dbf5a`)。
- 删掉函数内 `import importlib` 与 3 处同样写法的函数内 `import re`（AST 扫全仓同款）(`05dbf5a`)。
- 前端轮询预算 120×3s（6 分钟）→ 400×3s（**20 分钟**），按实测耗时（单项 70-80s）而非感觉放宽 (`05dbf5a`)。

#### 文档输出 / 导出（format / export / generate）
- **「下载Word文档」返回 `{"detail":"Not Found"}`** (`9e2ee87`)：前端 `window.open('/api/projects/{id}/export/word')` 调用的路由**根本不存在**（裸英文 `Not Found` 是 FastAPI 路由未匹配的默认 404，业务自抛的都是中文），真实接口是 `GET /api/generate/{id}/export-docx`；改为复用既有 `generateApi.exportDocx()` + blob 下载，并把该请求超时从全局 120s 提到 600s。
- **导出 Word 500（一）** (`8db49fc`)：`from docx import Document` 被同文件 `from services.models import ... Document ...`（ORM 模型）**同名覆盖**，`Document()` 造出的其实是 ORM 实例，紧接着 `doc.sections[0]` 抛 AttributeError；因为文件头有 `from __future__ import annotations`，helper 的 `doc: Document` 注解被延迟成字符串、运行时不求值，所以启动期毫无征兆。改用 `Document as DocxDocument`。
- **导出 Word 500（二）** (`072ea37`)：`Content-Disposition` 把**原始中文项目名**塞进 HTTP 头，而头部按 latin-1 编码 → `UnicodeEncodeError`。加 `urllib.parse.quote`（`filename*=UTF-8'` 是 RFC 5987 语法，值本身必须百分号编码，声明字符集不会替你编码）。
- **「一键排版」三个导出全废** (`ed806f3`)：产物写在 `/tmp/bidmaster_format`（下载白名单外）、前端强依赖上传件（项目模式没有上传件 → 按钮不渲染）、`reportlab` 未在 `pyproject.toml` 声明。
- **`download` 端点 404 + 前端面板空白** (`5107059`)：端点重复拼接相对路径；前端把后端 `{success, data}` 的**外壳当载荷**读（只剥了 axios 那一层 `res.data`），所有字段恒为 `undefined`，导出按钮因读到 `undefined` 干脆不渲染。
- **「导出 doc」产出的是改名的 docx** (`516c3ed`)：`soffice --convert-to doc:MS Word 2007 XML` 里 `MS Word 2007 XML` 是 **.docx** 的过滤器名，`.doc` 的是 `MS Word 97`；soffice 完全按过滤器决定内容、只按扩展名命名，于是退出码 0、magic 却是 `504b0304`(ZIP)。
- **「导出 doc」恒 500「未找到 soffice」** (`80110fe`)：api 镜像只有 `antiword`/`catdoc`（能**读**不能**写** `.doc`），装上 `libreoffice-writer-nogui`；同时给 PDF 转换补独立 profile，避免与 doc 转换争用同一 user profile 锁。
- **导出端点的 `template` 被当 query 解析** (`d03f036`)：前端选的模板恒不生效；顺带关掉 `srcdocx_*` 临时文件泄漏。
- **`fmt=docx` 分支漏了落盘清理** (`4aa592c`)：同一函数里 `fmt=pdf` 与 `fmt=doc` 两支都有 `unlink`，唯独 docx 漏了。

#### AI 解读（interpret）
- **「解读失败」误报** (`48e91a3` `87e623e`)：解读链实测约 11 分钟（15 维度 ÷ 并发 3，`qwen3.8-max` 单次 75-141s），前端 120s 先超时（nginx 记 499）；把超时提到 600s 后仍在 600s 处 504、后端 664s 才落库 —— 证明**同步 HTTP 承载不了，放宽超时不是解法**。改为复用仓库既有 `TaskManager` 异步范式，`analysis` 补 `updated_at` / `interpret_running`，前端每 6s 轮询并显示已用时长。
- **轮询两处正确性护栏** (`d06191b`)。

#### 资讯中心（news）
- **「智能推荐」聚合 500** (`c2c2bc0`)：`fetchers.py` 顶层 `import feedparser` 但 `pyproject.toml` 从未声明该依赖，且导入写在路由函数体内（懒导入），故启动正常、只在点「开始首次采集」/「立即聚合抓取」时炸（两个按钮同一个 handler）。容器内 139 模块全量导入扫描确认为唯一缺包项。
- **3 处同步 `requests.get` 阻塞事件循环** (`c2c2bc0`)：单 worker 下聚合期间全站无响应 → 改 `asyncio.to_thread`，并把 `/news/aggregate` 前端超时提到 600s。
- **36 个数据源实际只有 5 个 AI 博客能产出** (`1df3244`)：ccgp 的 RSS 地址**从未存在**(404)、ggzy.gov.cn 是 2024-01-19 的静态快照、省级 ggzy 全部网络不可达、ctbpsp 是混淆 JS、cebpubservice 首页是政策新闻、千里马 GBK+JS 渲染、zhaobiao.cn 日期只有 `09.07` 没年份；`NewsCrawlerSkill` 的 5 个兜底站点同样合计 0 条有效数据。重写 `sources.yaml` 为 **17 源 / 6 启用**（ccgp 5 个分类频道列表页 + okcis 工程建设 GB18030，全部实测当日真实条目）。
- **`_resolve_url` 改 `urljoin`** (`1df3244`)：修掉 `/./` 脏 URL。
- 聚合性能 (`1df3244`)：**35s / 72 条 → 1.01s / 123 条**，6 源 0 报错，101 条带地域、100 条带采购人，行业分类真正分散，评分出现 3 档差异；聚合期间 60 次健康探测最大延迟 **10ms**。

#### 上传解析（parse）
- **上传 WPS 保存的 `.doc` 报 `文件解析失败: Package not found at '...'`** (`05f79ba`)：误导性报错，文件其实存在 —— `EXT_MAP` 把 `.doc`/`.wps` 直接分发给 `DocxParser`，而 python-docx 只能读 zip，对 OLE2 二进制一律抛 `PackageNotFoundError`（文件不存在时也抛同样错误，无法区分）。
- **WPS 变体 `antiword` 拒读** (`07c2a55`)：WPS 写出的 FIB 头非标准，`antiword`（2005 年后未更新）报 "is not a Word Document"，补 `catdoc` 兜底；实测用户 695KB 文件提取 **54,905** 字符中文、104 章节。

#### 数据库 / 部署（db / deploy）
- **`MEDIUMTEXT` 致 PostgreSQL 建表整体失败** (`789fa1b`)：`MEDIUMTEXT` 是 MySQL 专有类型，PG 无法渲染 → `Base.metadata.create_all` 整体失败 → `db_ready=false` → **所有接口 503**。改为跨方言 `LongText = Text().with_variant(MEDIUMTEXT, "mysql")`（4 处）。
- **PG 库里 0 张表** (`789fa1b`)：`db/init_pg.sql` 的 CREATE TABLE 顺序错误（`projects` 引用尚未创建的 `documents`），官方镜像以 `ON_ERROR_STOP=1` 执行 → 初始化整体失败回滚，容器重启后 PGDATA 已存在便跳过初始化，留下空库；且该脚本比 `models.py` 少 5 张表。改为三段式（见「破坏性变更」）。
- **种子 id 超 `varchar(36)`** (`ca0f8c9`)。
- **时间戳 tz-aware/naive 混用导致所有写库操作 500** (`dc2d31d`)：`services/models.py` 22 处 + `services/routers/api_key.py` 9 处。
- **`BMP_EMBEDDING_MODEL` 被静默忽略** (`e8ddf5c`)：`core/rag_engine/embedder.py` 键名不匹配；另修 `knowledge_assist_skill.py` 裸构造 `Embedder()`（不读配置）。
- **构建源国内化** (`561fabf`)：`chromadb` 钉 `>=0.5,<1.0`（代码按 0.5 语义写 `list_collections`）、显式声明 `bcrypt>=4.0`（`auth.py` 直接 import，原仓库靠 chromadb 传递依赖）、移除 `sentence-transformers`；`Dockerfile.api` pip 走 aliyun、`Dockerfile.web` npm 走 npmmirror、基础镜像走 daocloud。
- **nginx 反代超时太短** (`48e91a3`)：`proxy_read_timeout` / `proxy_send_timeout` 由 300s 提到 **600s**（解读链改异步之前的过渡措施；改异步后该值不再是瓶颈）。另有一条**纯运维项、未改代码**：`nginx.conf` 用的是 `proxy_pass http://api:8000` 静态主机名，只在 nginx 启动时解析一次，api 容器重建后 IP 变化会让 web 持续 502 —— 部署脚本在 api 健康后探一次反代、失败即重启 web。

### ♻️ 重构（Changed / Refactored）

- 抽出 `core/http_headers.py:content_disposition()`，把 7 个导出端点的响应头编码收敛到一处；改完全仓 `rg -n "Content-Disposition"` 只剩 helper 内部那一处字面量。重构前后导出字节数完全一致（1,242,354 B），证明只换了响应头生成方式、没碰产物 (`4aa592c`)。
- 检查链的 skill 加载 / 15 项清单 / 参数装配三处重复实现收敛为共用函数（见上）(`05dbf5a`)。
- `sources.yaml` 从「36 源大多失效」重写为「17 源 / 6 启用 + 配置驱动抓取规则」(`1df3244`)。

### 🔒 依赖与声明（Dependencies）

- 新增声明：`bcrypt>=4.0`、`reportlab>=4.0`、`feedparser>=6.0`
- 收紧：`chromadb>=0.5` → `chromadb>=0.5,<1.0`
- 移除：`sentence-transformers>=3.0`

### 🧪 本轮发布验证（192.168.50.31 实测）

| 用例 | 结果 |
|---|---|
| 上传模式 `fullCheck` | 提交 **0.32s** 返回 `task_id`；任务 **278s** 完成；**15/15 `success=true`**（修复前 0.005s、15/15 全失败） |
| 上传模式单项 `compliance` | **81.3s** 完成，`success=true`，1 条 finding |
| 上传模式 `selfcheck` | 正常返回 `categories/all_passed/can_submit/total_items/passed_items` |
| 项目模式 `full-check`（191 章真实项目） | **729s** 完成，**14/15 `success=true`** |
| 错误路径 | 未知 `check_type` → 400、空投标文件 → 400、不存在 task → 404 |
| 服务健康 | api healthy（3s）、`db_ready:true`、nginx 反代 `/api/health` 200 |
| 导出（191 章 / 110.8 万字项目） | `apply_format=false` → 200 / 5.2s / 1,184,672 B；`apply_format=true` → 200 / 8.0s / 1,242,354 B；均为合法 docx |
| 资讯聚合 | 200 / **1.01s** / 123 条入库，6 源 0 报错 |

### 🐞 已知问题（0.2.0 未修，待方案确认）

1. **`collect_json` 的类型标注是假的**：`core/llm_gateway/gateway.py` 标注 `-> dict`，实际返回 `json_repair.repair_and_validate()` 的 `dict | list`。0.2.0 验证中 16 次调用有 **1 次**模型返回长度 2 字符的 `[]`（日志 `结果类型=list`），`services/check/skills/ai_text_check_skill.py:61` 裸 `result.get(...)` 抛 `'list' object has no attribute 'get'` → 该项显示「异常」。全仓 **41 处 / 27 个文件**同款写法，间歇性复现。建议给调用点传 `validator=lambda d: isinstance(d, dict)`（走既有 `max_repair_attempts=2` 修复回路），并在 `collect_json` 内对非 dict 结果兜底 + 改掉说谎的标注。注意 `response_format={"type":"json_object"}` **不构成**「一定返回对象」的保证。
2. **上传模式的 `_parse_uploaded_file` 是独立弱化解析器**：不支持 `.doc`/`.wps`（而 `projects.py` 的白名单支持），异常与未知后缀一律 `decode(errors="replace")` **静默返回二进制乱码**，扫描件 PDF 抽空、不校验招标文件为空、也不走 `_truncate_text` 截断。乱码非空能绕过所有「内容为空」守卫，会让检查结果不可信。建议改用仓内已有的 `core/doc_engine.get_parser`。
3. **检查链很慢**：质量检查 Agent 默认走 `qwen3.8-max`，单项 78s、全面检查 4-12 分钟；网关单次请求超时 300s、`max_retries=2`，重文档下会出现 300s 超时重试。在「设置 → 智能体模型配置 → 质量检查Agent」改为 `qwen3.7-flash` 是纯配置提速，无需改代码。
4. **`is_hot` 恒为 0 条**：`POST /news/aggregate` 的请求体支持 `company_profile`，但前端「立即聚合抓取」发的是空 body → `company_profile={}`，而评分里 region 那 15% 只有传画像才生效。需把公司画像接进聚合链路（功能增强）。
5. 其余上游遗留（登录态存进程内字典无法多副本、alembic 不可用、MinIO 桶匿名下载、`projects ↔ documents` 循环外键无 `ondelete`、AI 配图只支持火山/Google）详见部署文档 §8。

### 📝 提交清单（24）

<details>
<summary>展开查看全部提交</summary>

| 提交 | 日期 | 说明 |
|---|---|---|
| `561fabf` | 2026-09-06 | deploy: 钉 chromadb<1.0、补 bcrypt、去 sentence-transformers、pip/npm 换国内源 |
| `789fa1b` | 2026-09-06 | deploy: PG 建表失败与空库（LongText 跨方言 + init_pg.sql 三段式 + override.yml） |
| `ca0f8c9` | 2026-09-06 | deploy: 种子 id 超 varchar(36) |
| `e8ddf5c` | 2026-09-06 | deploy: embedder 键名不匹配致 `BMP_EMBEDDING_MODEL` 静默失效 |
| `dc2d31d` | 2026-09-06 | deploy: 时间戳 tz-aware/naive 混用致所有写库 500（models.py 22 处 + api_key.py 9 处） |
| `2162904` | 2026-09-06 | feat(llm): 自定义供应商接入 OpenAI 兼容网关 + 智能体模型配置真正生效 |
| `05f79ba` | 2026-09-06 | fix(parse): 老式二进制 .doc 上传后解析报 Package not found |
| `07c2a55` | 2026-09-06 | fix(parse): 老式 .doc 解析增加 catdoc 兜底（WPS 变体 antiword 拒读） |
| `48e91a3` | 2026-09-06 | fix(interpret): AI 解读超时误报失败 + 重复提交压垮 LLM 网关 |
| `87e623e` | 2026-09-06 | fix(interpret): 解读改为异步任务 + 前端轮询，根治「解读失败」 |
| `d06191b` | 2026-09-06 | fix(interpret): 轮询补两处正确性护栏 |
| `c2c2bc0` | 2026-09-06 | fix(news): 资讯聚合 500 —— feedparser 未声明依赖 + 同步抓取阻塞事件循环 |
| `1df3244` | 2026-09-07 | fix(news): 清理并替换失效数据源，落地 HTMLFetcher 与地域/采购人字段 |
| `9e2ee87` | 2026-09-08 | fix(generate): 下载Word文档 404 —— 前端调用了不存在的导出路径 |
| `8db49fc` | 2026-09-08 | fix(generate): 导出Word 500 —— python-docx 的 Document 被同名 ORM 模型覆盖 |
| `072ea37` | 2026-09-08 | fix(generate): 导出Word 500 —— Content-Disposition 塞原始中文，HTTP 头只能 latin-1 |
| `4aa592c` | 2026-09-08 | refactor(export): 抽出 content_disposition() 统一响应头编码，并修掉 fmt=docx 落盘泄漏 |
| `ed806f3` | 2026-09-08 | fix(format): 文档输出「一键排版项目」三个导出全废 |
| `5107059` | 2026-09-08 | fix(format): download 端点重复拼接相对路径致 404；前端四个结果面板读错一层 |
| `80110fe` | 2026-09-08 | fix(export): 装上 LibreOffice 让「导出 doc」真正可用，并给 PDF 转换补独立 profile |
| `516c3ed` | 2026-09-08 | fix(export): soffice 过滤器名写错，「导出 doc」产出的是改名的 docx |
| `d03f036` | 2026-09-08 | fix(format): 导出端点的 template 被当 query 解析，前端选的模板恒不生效 |
| `05dbf5a` | 2026-09-08 | fix(check): 上传模式「全面检查」15 项全判异常 —— importlib 被函数内局部 import 遮蔽 |
| `dd3adbf` | 2026-09-08 | fix(check): upload-check 单项分支 500 —— message 的 f-string 引用了不存在的 task_id |

> 注：前 5 个 `deploy(...)` 提交共用了同一条 subject（迭代部署补丁时未细分），实际内容以上表与各自的
> `git show <hash>` 为准。后续提交已按 Conventional Commits 一事一提交。

</details>

---

## [0.1.0] - 2026-09-06

fork 自上游 `guangshu100/BidMaster-Pro` 的基线版本（`cc89d90`），未做改动。

- 全流程智能招投标平台：招标解读 / 投标生成 / 投标检查（15 项）/ 文档排版 / 资讯中心
- 技术栈：FastAPI + SQLAlchemy(async) + ChromaDB + React/Vite + Docker Compose
- skill 引擎：interpret / check / generate / format / news 五类（当前仓库共 43 个 `*_skill.py`）

---

[0.2.0]: https://github.com/andyke668/BidMaster-Pro/compare/cc89d90...v0.2.0
[0.3.0]: https://github.com/andyke668/BidMaster-Pro/compare/v0.2.0...v0.3.0
[0.1.0]: https://github.com/andyke668/BidMaster-Pro/releases/tag/cc89d90
