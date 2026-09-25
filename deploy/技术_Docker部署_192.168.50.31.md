# 技术_BidMaster-Pro Docker 部署（192.168.50.31）

> 部署日期：2026-09-06 ｜ 状态：**已完成，全链路验证通过（含真实 LLM 解读）**
> 代码基线：`andyke668/BidMaster-Pro` @ `cc89d90` + 本地部署补丁分支 `deploy/local`
> 脚本：`deploy_bidmaster.sh`（部署）、`verify_bidmaster.sh`（验证），均已上传至服务器 `~/deploy/`
>
> **v0.4.0（2026-09-24）**：新增管理后台「使用监控」，基线 `ec0bc74`。升级顺序必须是
> `code` → `migrate` → `llm` → `up` → `seed`（先迁移再重启 api，见 §16）。

## 1. 访问入口

| 项 | 值 |
|---|---|
| Web 界面 | http://192.168.50.31:8081/zdx/ （**根路径按设计返回 404**，只发布 /zdx 子路径） |
| 管理后台 | http://192.168.50.31:8081/zdx/admin （v0.4.0 起，仅 `settings.monitor` 权限可见） |
| API 根 | http://192.168.50.31:8000 |
| API 文档 | http://192.168.50.31:8000/docs |
| 健康检查 | http://192.168.50.31:8000/api/health → `db_ready: true` |
| 默认账号 | `admin@bidmaster.pro` / `admin123` ← **请登录后立即改密** |
| LLM 网关 | `http://192.168.40.113:8088/v1`（内网，复用 openbidkit-web 的 `YIBIAO_AI_API_KEY`） |
| 模型 | 文本 `qwen3.8-max`，降级 `qwen3.7-flash`，向量 `qwen3.7-text-embedding`（1024 维） |
| AI 功能 | **已可用**，端到端冒烟测试通过（见 §7） |
| 服务器代码目录 | `~/bidmaster-pro`（用户 `andy`，主机 `ubuntu-test`） |
| 部署配置 | `~/bidmaster-pro/docker/.env`（权限 600，含随机 PG/MinIO 密码） |

SSH：`ssh -i ~/.ssh/id_ed25519_workbuddy andy@192.168.50.31`（密钥登录，`andy` 在 `docker` 组，无需 sudo）
**管理员账号重置（锁定自救）**：登录校验支持 sha256 回退（`auth.py:_verify_password`），`admin123` 的种子哈希为
`240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9`。把 email 与 password_hash 一并写回即可恢复初始账号
（2026-09-06 实际执行过：用户误将 email 改成 `admin` 导致无法登录）。在本机 PowerShell 执行：

```powershell
ssh -i C:\Users\andy\.ssh\id_ed25519_workbuddy andy@192.168.50.31 'docker exec bidmaster-postgres-1 psql -U bidmaster -d bidmaster -c "update users set email=\$\$admin@bidmaster.pro\$\$, password_hash=\$\$240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9\$\$ where id=\$\$00000000-0000-0000-0000-user00001\$\$"'
```

注意：改密请走页面或 `PUT /api/auth/change-password`，**不要手改数据库里的 email 字段**。

## 2. 目标机环境

| 项 | 值 |
|---|---|
| 系统 | Ubuntu 24.04.1 LTS，x86_64，4 核 / 5.8G 内存 |
| 磁盘 | 48G，部署前可用 9.0G（81% 已用）→ 清理构建缓存后 16G → 部署后剩 12G（75%） |
| Docker | 28.5.1（**snap 安装**，数据目录 `/var/snap/docker/common/var-lib-docker`），Compose v2.40.0 |
| 自启 | `snap.docker.dockerd.service` = enabled + active，容器 `restart: unless-stopped` → 重启后自动恢复 |
| 共存业务 | 同机已运行 `openbidkit-yibiao-web`（Project_002，占用 **8080** 端口，已运行 6 天）→ 本项目 Web 改用 **8081** |
| 网络 | Docker Hub 直连 **TLS handshake timeout**；GitHub / PyPI 可达但慢 |

部署后内存占用（`docker stats`）：api 124M、minio 96M、postgres 30M、redis 3M、web 5M，整机 2.5G/5.8G，可用 3.3G。

## 3. 实际运行的服务集

| 服务 | 状态 | 端口 | 说明 |
|---|---|---|---|
| `api` | healthy | `0.0.0.0:8000` | FastAPI，`uvicorn --workers 1`（登录态在进程内，多 worker 会随机 401） |
| `web` | healthy | `0.0.0.0:8081` | nginx 托管 Vite 产物 + 反代 `/api` |
| `postgres` | healthy | `127.0.0.1:5432` | PG 16，库 `bidmaster`，用户 `bidmaster` |
| `redis` | healthy | `127.0.0.1:16379` | 仅 Celery 用；当前代码未投递任何任务 |
| `minio` | healthy | `127.0.0.1:19000/19001` | **代码中完全未使用**（`minio` 仅出现在 `core/settings.py`），因 compose 弱依赖被带起 |
| `celery-worker` / `celery-beat` / `minio-init` | 未启动 | — | 见下方说明 |

- 未启动 Celery 的依据：全仓库搜索无 `.delay(` / `apply_async` 调用，唯一的定时任务 `news-monitor` 在 `services/celery_app.py:31` 里只是个返回 `{"status":"processed"}` 的空壳。
- minio/redis 之所以仍被拉起：`api.depends_on` 对它们是 `required: false` 的弱依赖，但 `--profile infra` 一旦启用这些服务，compose 仍会把它们当依赖一并创建。**因此必须一起预拉镜像**，否则会去连 Docker Hub 超时并中断整个 `up`。
- 基础设施端口全部只绑 `127.0.0.1`（Redis 无密码、MinIO 桶被 `mc anonymous set download` 设为匿名可下载，不应暴露内网），已验证从外部无法访问 5432/16379/19000/19001。

## 4. 部署过程遇到的 5 个阻塞问题

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `docker compose up` 报 `Get "https://registry-1.docker.io/v2/": net/http: TLS handshake timeout` | 国内网络无法直连 Docker Hub | 所有基础镜像预拉自 `docker.m.daocloud.io`（library 镜像走 `/library/<name>`，第三方走 `/<ns>/<name>`），拉完 retag 成规范名并删掉镜像源 tag；pip 换 aliyun、npm 换 npmmirror |
| 2 | api 启动日志 `数据库不可用，应用将以降级模式启动: ... can't render element of type MEDIUMTEXT` → `db_ready:false` → 所有接口 503 | `services/models.py:6` 引入 MySQL 专有类型 `MEDIUMTEXT`（用于 4 处 Column），PostgreSQL 无法渲染，`Base.metadata.create_all` 整体失败 | 新增跨方言别名 `LongText = Text().with_variant(MEDIUMTEXT, "mysql")`，4 处 `Column(MEDIUMTEXT,` → `Column(LongText,` |
| 3 | PG 库里 **0 张表**，initdb 日志 `ERROR: relation "documents" does not exist` | `db/init_pg.sql` 的 CREATE TABLE 顺序错误：`projects`（第 109 行）引用了尚未创建的 `documents`（第 130 行）；官方镜像以 `ON_ERROR_STOP=1` 执行 → 整个初始化失败回滚，容器重启后 PGDATA 已存在便跳过初始化，留下空库。且该脚本比 `models.py` 少 5 张表（`news_source_registry`/`hotspot_items`/`llm_provider_configs`/`api_keys`/`api_key_usage`） | 三段式：① `db/init_pg.sql` 改为只建扩展的安全引导（原文件备份为 `.orig`）；② 表结构交给应用 `create_all`（与 models.py 永远同步，实际建出 22 张表）；③ 种子数据抽成 `db/seed_pg.sql`，在 api 健康后用 `psql -v ON_ERROR_STOP=1` 灌入 |
| 4 | 灌种子报 `ERROR: value too long for type character varying(36)` | `rbac_role_permissions.id` 由 `'00000000-0000-0000-rp-<role>-' \|\| p.code` 拼接（28 + 权限码长度），超过 `models.py` 里 `String(36)` 的上限 | 4 处拼接改为 `md5('rp-<role>-' \|\| p.code)`（定长 32），仍确定且唯一，另有 `ON CONFLICT (role_id, permission_id)` 兜底 |
| 5 | web 首页 200 但 `/api/*` 全部 **502** | `docker/nginx.conf` 用静态主机名 `proxy_pass http://api:8000`，nginx 只在启动时解析一次；api 容器重建后 IP 变化，而 web 容器沿用旧进程 | 部署脚本新增 `fix_web_proxy()`：api 健康后探一次反代，非 200 则 `compose restart web` |
| 6 | 创建项目返回 500：`invalid input for query argument $7 ... can't subtract offset-naive and offset-aware datetimes` → **任何写库操作都失败** | `services/models.py` 时间戳定义自相矛盾：列类型是 `DateTime`（PG 下 = `TIMESTAMP WITHOUT TIME ZONE`），但 `created_at` 默认 `datetime.now(timezone.utc)` 是 tz-aware，`updated_at` 默认 `datetime.now` 是 naive 本地时间；asyncpg 拒绝把 aware 值编码进 naive 列 | 新增 `_naive_utcnow()`（`datetime.now(timezone.utc).replace(tzinfo=None)`），22 处 `created_at`/`updated_at` 默认值统一改为它；`services/routers/api_key.py` 9 处 `datetime.now(timezone.utc)` 同理改为 `_naive_utc()`。列类型不变，无需重建表 |
| 7 | 配好 `BMP_EMBEDDING_MODEL` 后知识库仍会去请求不存在的 `text-embedding-v3` | 键名不匹配：`services/routers/knowledge.py:38` 传 `"model"`，而 `core/rag_engine/embedder.py:10` 读的是 `config.get("model_name")` → 配置被**静默忽略**，无任何报错 | `embedder.py` 改为 `config.get("model_name") or config.get("model") or 默认值` |
| 8 | 生成环节的「全局知识检索」永远返回空 | `services/generate/skills/knowledge_assist_skill.py:86-87` 用裸 `VectorStore()` / `Embedder()`，不读任何配置 → chroma 路径错、api_key 为空；异常被 try/except 吞掉只留 warning | 改为从 `get_settings()` 构造，传入 `chroma_dir` 与 embedding 四项配置 |

另有两处非阻塞问题已一并修掉：
- `minio` 容器崩溃重启：`.env` 里 `MINIO_ROOT_PASSWORD=unused` 只有 6 位，MinIO 要求 ≥8 位 → 改为随机 32 位十六进制。
- 镜像体积：`pyproject.toml` 无条件依赖 `sentence-transformers>=3.0` 会拖入 PyTorch（Linux 默认 CUDA 轮子数 GB），而代码只在 `core/rag_engine/embedder.py:22` 惰性导入、且本机用 `EMBEDDING_MODE=api`。移除后 **api 镜像 1.3GB**（含 torch 预计 5GB+，本机磁盘装不下）。

## 5. 部署期补丁清单

全部落在服务器 `~/bidmaster-pro` 工作区，提交在本地分支 `deploy/local`，可用 `git diff origin/main..HEAD` 审阅：

```text
 core/rag_engine/embedder.py                        |   2 +-   兼容 model/model_name 两种键
 db/init_pg.sql                                     | 571 +---   改为安全引导（只建扩展）
 db/init_pg.sql.orig                                | 562 ++++   原文件备份
 db/seed_pg.sql                                     | 131 +++    抽取的种子数据（id 改 md5 定长）
 docker/Dockerfile.api                              |   2 +-    pip 源 -> mirrors.aliyun.com
 docker/Dockerfile.web                              |   3 +-    npm 源 -> registry.npmmirror.com（ARG 可覆盖）
 docker/docker-compose.override.yml                 |  20 +     项目名 bidmaster + 内存上限（api 1g/pg 512m/web 128m）
 pyproject.toml                                     |   4 +-    chromadb 钉 <1.0、补 bcrypt、去 sentence-transformers
 services/generate/skills/knowledge_assist_skill.py |   6 +-    Embedder/VectorStore 改为读 settings
 services/models.py                                 |  76 +--    MEDIUMTEXT -> 跨方言 LongText；时间戳统一 naive UTC
 services/routers/api_key.py                        |  14 +-    时间戳统一 naive UTC
```
合计 11 个文件、+784/−607 行；`deploy_bidmaster.sh` 每处补丁都带 `grep -q` 幂等守卫，关键文件改完还会跑 `ast.parse` 语法校验。

为什么钉 `chromadb>=0.5,<1.0`：`core/rag_engine/vector_store.py:54` 写的是 `[c.name for c in client.list_collections()]`，这是 0.5.x 语义；chromadb 1.x 的 `list_collections()` 返回字符串列表，会直接 `AttributeError`。
为什么补 `bcrypt`：`services/routers/auth.py:7` 直接 `import bcrypt`，但 `pyproject.toml` 未声明，原本靠 chromadb 的传递依赖侥幸可用。

## 6. 运维手册

```bash
ssh -i ~/.ssh/id_ed25519_workbuddy andy@192.168.50.31
cd ~/bidmaster-pro/docker

# 状态 / 日志
docker compose -p bidmaster ps
docker logs -f bidmaster-api-1
docker compose -p bidmaster logs -f --tail 100 api web

# 重启单个服务（改完 .env 后）
docker compose -p bidmaster --profile infra --profile web up -d api

# 停止（保留数据卷） / 彻底删除（含数据卷，慎用）
bash ~/deploy/deploy_bidmaster.sh down
docker compose -p bidmaster --profile infra --profile web down -v

# 升级到上游最新（部署补丁会自动 rebase，冲突则中止并提示）
bash ~/deploy/deploy_bidmaster.sh all

# 重新灌种子（幂等）
bash ~/deploy/deploy_bidmaster.sh seed

# 重新写入 LLM 网关配置（从 ~/openbidkit-web/.env 读 Key，幂等）
bash ~/deploy/deploy_bidmaster.sh llm

# 验证 LLM / Embedding 通路（在容器内走应用自身代码）
bash ~/deploy/deploy_bidmaster.sh verify-llm

# 全量验证
bash ~/deploy/verify_bidmaster.sh

# 端到端冒烟测试（登录→建项目→上传→解析→AI 解读，约 4 分钟，会产生测试数据）
bash ~/deploy/e2e_smoke.sh

# 备份数据库
docker exec bidmaster-postgres-1 pg_dump -U bidmaster bidmaster | gzip > ~/bidmaster-$(date +%F).sql.gz
```

数据卷：`bidmaster_pgdata`（库）、`bidmaster_projects` / `bidmaster_chroma` / `bidmaster_uploads`（业务文件与向量库）。

## 7. LLM 网关配置与端到端验证

### 7.1 网关
复用同机 `~/openbidkit-web/.env` 里的 `YIBIAO_AI_API_KEY`，网关地址取自 `~/openbidkit-web/docker-compose.yml:23`：

| 项 | 值 |
|---|---|
| Base URL | `http://192.168.40.113:8088/v1`（**跨网段**：服务器在 192.168.50.x，网关在 192.168.40.x；已验证宿主机与容器内均可达） |
| 可用模型 | 16 个，含 `qwen3.8-max`、`qwen3.7-flash`、`deepseek-v4-pro/flash`、`glm-5.3`、`mimo-v2.5-pro`、`qwen3.7-text-embedding`、`gpt-image-2`、`qwen-image-3.0` |
| 文本模型 | `qwen3.8-max`（与 openbidkit-web 保持一致） |
| 降级模型 | `qwen3.7-flash`（同网关，替代原来无效的 `ollama/qwen2.5`） |
| 向量模型 | `qwen3.7-text-embedding` → 1024 维 |

配置由 `deploy_bidmaster.sh llm` 写入 `docker/.env`：脚本从 `~/openbidkit-web/.env` **直接读取密钥**，不硬编码、不回显、不写进任何文档。

注意 `core/llm_gateway/gateway.py` 其实**没有用 litellm**（尽管 pyproject 里有），而是直接用 `openai.AsyncOpenAI`，并且：
- `_strip_provider_prefix()` 会剥掉 `deepseek/`、`openai/` 等已知前缀，所以 `LLM_MODEL` 直接写网关的模型 id 即可（`qwen3.8-max`，不要加前缀）；
- `_build_client()` 会自动给 `api_base` 补 `/v1` 后缀；
- `timeout=300s`、`max_retries=0`（重试由网关自己控制，`BMP_LLM_MAX_RETRIES=3`）。

### 7.2 验证结果

**通路验证**（`deploy_bidmaster.sh verify-llm`，走应用自身代码）：
```text
settings: model=qwen3.8-max base=http://192.168.40.113:8088/v1 key_len=51
embedding: mode=api model=qwen3.7-text-embedding base=http://192.168.40.113:8088/v1
chat  -> '正常'
embedder.model_name -> qwen3.7-text-embedding      ← 证明键名补丁生效
embed -> 向量维度: 1024
```

**端到端冒烟测试**（`e2e_smoke.sh`：登录 → 建项目 → 上传 → 解析 → AI 解读）：
```text
1/6 登录            [OK] token 长度 64
3/6 创建项目        [OK] 含 multipart 上传招标文件（证明时间戳补丁生效）
4/6 解析招标文件    [OK] text_length=558，识别出 10 个章节
5/6 AI 解读         HTTP 200，耗时 236.7s，响应 4814 字节
6/6 解读结果        success=True，15 个维度全部返回
      project_info   → 项目名「某市城区道路改造工程」、预算「人民币 4800 万元」
      qualification  → 「市政公用工程施工总承包壹级及以上资质」+ 业绩/建造师要求
      scoring        → 综合评估法，技术标 60%、商务标 40%
      disqualification → 签字盖章、密封骑缝章等实质性要求
```
测试数据已清理（`projects`/`documents`/`analyses` 均为 0），种子数据完好（users=1、rbac_role_permissions=72）。

### 7.3 性能提醒
`qwen3.8-max` 是**推理模型**（响应里带 `reasoning_content`/`reasoning_tokens`），单次解读要串行发多次 LLM 请求，实测各次耗时 5s / 69s / 134s，整条解读链路 **约 4 分钟**。
若追求响应速度，把 `docker/.env` 的 `LLM_MODEL` 换成 `qwen3.7-flash` 或 `deepseek-v4-flash`，然后：
```bash
cd ~/bidmaster-pro/docker && docker compose -p bidmaster --profile infra --profile web up -d api
```

## 8. 待办

1. **改默认密码**：`admin123` 是仓库公开种子密码，登录后调 `PUT /api/auth/change-password` 或在页面里改。（2026-09-06：曾有一次误改把 email 改成 `admin` 导致锁定，已重置回种子账号，重置命令见 §1；改密时请勿动 email 字段。）
2. 需要扫描件 OCR 时填 `MINERU_API_KEY`（默认 cloud 模式，指向 mineru.net）。
3. **AI 配图不可用**：`services/generate/skills/ai_image_skill.py` 只支持火山引擎（`volcengine_api_key`）与 Google Imagen（`google_api_key`），参数来自 `ctx.parameters` 而非环境变量；内网网关虽有 `gpt-image-2`/`qwen-image-3.0`，但需要改代码才能接。
4. 若要启用本地 Embedding（`EMBEDDING_MODE=local`），需先把 `sentence-transformers` 加回 `pyproject.toml` 并预留 ~5GB 磁盘。
5. 磁盘只剩 12G，建议定期 `docker builder prune -f`（本次清理释放了 6.2G）。
6. 密钥来源耦合：LLM Key 读自 `~/openbidkit-web/.env`，若那套部署被清理，重跑 `deploy_bidmaster.sh llm` 会失败（但已写入 `docker/.env` 的值不受影响）。
7. 生产化建议：登录态改存 Redis（当前在 `auth.py` 进程内字典，无法多副本）、补 `db/migrations`（alembic 目前不可用）、MinIO 桶取消匿名下载、`news` 模块混用 naive 本地时间与 naive UTC（`fetchers.py`/`dedup.py` 用 `datetime.now()`，而 `created_at` 现为 UTC），启用资讯功能前需统一。
8. 未发现「删除项目」接口（`projects.py` 只有 gate 的 DELETE），且 `projects.tender_doc_id → documents.id` 与 `documents.project_id → projects.id` 构成**循环外键**、无 `ondelete=cascade`；将来加删除功能时必须先置空 `tender_doc_id` 再按 analyses → documents → projects 顺序删。

## 9. LLM 自定义供应商 + 智能体模型配置生效（2026-09-06）

**提交**：`2162904`（本地分支 `feature/custom-llm-provider`，push 到服务器远端 `srv` 后 `git merge --ff-only`；服务器 `main` 与 `deploy/local` 均已同步）

**使用方式**（Web 平台设置）：
1. 「LLM供应商」→ 新增配置 → 供应商选「自定义供应商（OpenAI 兼容）」；
2. 填 API Base URL（如 `http://192.168.40.113:8088/v1`）与 API Key（Bearer 认证）；
3. 点「获取模型」→ 后端代理调 `GET {api_base}/models` 拉取列表 → 下拉选择默认模型 → 保存（模型列表随配置持久化，下次打开无需重拉）；
4. 「智能体模型配置」中每个 Agent 的下拉会展开该配置**全部已获取模型**（存储值格式 `provider_id/model`），保存即生效。

**实现要点**：
- 新增 `POST /api/llm/fetch-models`：支持 `config_id` 复用已存 Key（编辑时 Key 不经前端回传）；base 非 `/v1` 结尾时自动尝试 `/v1/models` 兜底；兼容 `{"data":[{"id":...}]}` / `{"models":[...]}` / 纯数组三种响应。
- `provider_id=custom` 时后端自动生成唯一 `custom_<8hex>`，多个自定义网关实例的同名模型不会歧义。
- `llm_provider_configs` 新增 `models TEXT` 列（JSON 数组）。新库由启动 `create_all` 自动建列；**存量库需手动**：`ALTER TABLE llm_provider_configs ADD COLUMN IF NOT EXISTS models TEXT`（2026-09-06 已执行）。
- 运行时打通：`services/llm_factory.py:get_agent_gateway(db, agent_name)` 按 `AgentConfig.config["model"]` 解析 → 命中已启用供应商配置则用其 api_base/api_key + 所选模型构建专用网关（缓存键含 `updated_at`，改配置自动失效）；Agent 未选模型 → 回落 env 默认网关。**skill 层零改动**（网关的 default_model 即所选模型）。
- 接线范围：interpret(5) / check(26) / generate(9：outline×4、content×3、export×2) / format_doc(7)，共 47 处；`agent_runtime.py` 多 Agent 框架未接线（Web UI 不经过它，全框架共享单一网关，将来要按 Agent 拆分需改框架）。
- 已知边界：Agent 的 temperature/max_tokens 仍不下发（与改造前一致，skill 调用自带参数）；卡片「测试」按钮改用 default_model 或已获取模型的第一个（原逻辑拼 `provider_id/default`，自定义供应商下会带无效前缀）。

**当前数据**：配置 `custom_cb3690ea`「内网统一网关」（默认、启用，16 个模型，default_model=`qwen3.8-max`）；用户此前手工建的两条残留配置（ollama/武汉内部、openai/111，模型名含 U+2011 隐形连字符，网关必然找不到模型）已删除。

**验证**（`~/deploy/verify_custom_provider.sh [provider_id]`，缺省自动发现 custom 配置）：providers 含 custom → fetch-models 拉到 16 模型 → 创建配置 → models 持久化 → interpret 设为 `qwen3.7-flash` → 容器内 `get_agent_gateway` 解析正确且真实 chat 返回 OK → 重置后回落 `qwen3.8-max`。全部通过（2026-09-06）。

## 10. 老式二进制 .doc 解析支持（2026-09-06）

**问题**：上传 WPS 保存的 `.doc` 招标文件，解析报 `文件解析失败: Package not found at '...'`——误导性报错，文件其实存在。

**根因**：`core/doc_engine/parsers/base.py` 的 `EXT_MAP` 把 `.doc`/`.wps` 直接分发给 `DocxParser`（python-docx），而 python-docx 只能读 zip 格式 `.docx`；对 OLE2 二进制一律抛 `PackageNotFoundError`（文件不存在时也抛同样错误，无法区分）。

**修复**（提交 `05f79ba` + `07c2a55`）：
- `DocxParser.parse` 先嗅探 magic bytes：`PK\x03\x04`(zip)→python-docx；`D0CF11E0`(OLE2)→老式 .doc 通道；`%PDF`→转交 PdfParser；未识别→友好报错「另存为 .docx」。
- 老式 .doc 通道：`antiword -m UTF-8.txt` 优先，失败回退 `catdoc -d utf-8`。**实测用户的 WPS 保存件 antiword 拒读**（"is not a Word Document"，WPS 写的 FIB 头非标准，antiword 2005 年后未更新），catdoc 成功提取 54,905 字符中文。
- 过滤 catdoc 的 `[This was fast-saved...]` 警告行；两个工具都只出纯文本，`tables` 为空（表格以制表文本混在正文里，LLM 解读不受影响）。
- `Dockerfile.api` 运行时依赖新增 `antiword` + `catdoc`（合计 <1MB）。
- 加密/损坏/纯扫描件 .doc 仍会失败，但报错改为可操作提示（转存 .docx）。

**附带确认**：`/app/projects` 上传目录挂的是命名卷 `bidmaster_projects`（compose 锚点 `x-api-volumes`），重建容器不丢文件。

**验证**：用户实际文件（湘潭大学采购项目，695KB）解析成功 → `parsed_content` 54,905 字符落库、章节检测出 104 节、项目状态 `interpreting`，可直接继续 AI 解读。
---

## 11. AI 解读「解读失败」根因与异步化改造（2026-09-06）

**现象**：招标文件解析成功后，点「AI 解读」等约 2 分钟即报「解读失败」；nginx 访问日志对应 3 次 `POST /api/interpret/interpret/<id>` 返回 **499**（客户端主动断开）。

**排查过程与实测数据**：
- 前端 axios 全局 `timeout: 120000`（`api.ts:5`），`interpretApi.interpret` 未覆盖 → 2 分钟必断。
- 浏览器断开后**后端仍在继续跑并最终落库**：`analyses` 表里 15 个维度齐全（41,326 字符）。用户重试导致 3 条解读流水线并行，把内网网关拖慢，日志出现 "Request timed out"。
- 把 nginx `proxy_read_timeout` 与前端超时都提到 **600s** 后**实测仍然失败**：curl 在整 600s 处拿到 **504 Gateway Time-out**，而后端 **664s** 才把结果写库。
- 量级来源：解读链跑 **15 个维度**，`asyncio.gather` + `Semaphore(max_concurrent)` 并发度**默认 3**（`tender_interpret_skill.py:363`，路由未传该参数）→ 5 波；interpret 智能体绑定 `qwen3.8-max`（推理型），单次调用实测 **75-141s** → 整链 **约 11 分钟**。

**结论**：同步 HTTP 请求承载不了 10 分钟级的 LLM 链路，**单纯放宽超时不是解法**（永远追不上，且长时间占用连接）。

**修复**（提交 `48e91a3` + `87e623e`，沿用仓库既有 `/generate` 异步范式）：

后端 `services/routers/interpret.py`：
- `POST /interpret/interpret/{id}` 改为**提交即返回 `task_id`**，整链交给 `core.task_manager.TaskManager` 后台执行；后台函数 `_do_interpret_tender` **自建 DB 会话**（请求级会话在响应返回后即关闭）并**显式 `await db.commit()`**（原来靠 `get_db` 自动提交）。
- 新增 `GET /interpret/task/{task_id}`：查不到时返回 `status: "unknown"` 而非 404，便于前端回退。
- `GET /interpret/analysis/{id}` 增加 `analysis.updated_at` 与 `interpret_running`。
- 在途登记 `_interpret_inflight` **前移到提交前**，闭合快速双击竞态；重复提交返回 **409**；`_INTERPRET_TTL` 900s → **1800s**（覆盖 11 分钟级正常耗时）；`try/finally` 保证摘除（`safe_execute` 只吞 `Exception`，`CancelledError` 仍会外抛）。
- 并发度改为可配 `BMP_INTERPRET_MAX_CONCURRENT`（默认 3，行为不变；已加入 compose `x-api-env` anchor）。

前端：
- `api.ts`：`interpret` 降为 60s（只是提交），新增 `getInterpretTask`；`parse` 300s、`scoringMatrix` 600s。
- `InterpretPage.tsx`：提交后每 **6s** 轮询，预算 **40 分钟**，全程显示「已用 X 分 Y 秒」蓝色进度条并提示勿重复提交；`status=unknown` 时回退 DB 判定（带 **60s 冷启动窗口**，避免把上一轮旧结果当本次结果瞬间返回）；刷新页面时若 `interpret_running` 为真也会提示「后台进行中」。
- `docker/nginx.conf`：`proxy_read/send_timeout` 300s → **600s**（解读已不依赖它，但 SSE/生成链路受益）。

**关于 worker 数**：`TaskManager` 与在途登记都是**进程内**的。本项目 `docker/.env` 里 `UVICORN_WORKERS=1`（部署脚本生成时即写死），因此二者都是精确的；前端仍保留 DB 回退分支作为多 worker 场景的兜底。API 全是 LLM I/O 等待，单 worker + asyncio 足够。

**提速建议（无需改代码）**：在「平台设置 → 智能体模型配置」把 interpret 从 `qwen3.8-max` 换成 `custom_cb3690ea/qwen3.7-flash`（单次 7-23s），整链可从约 11 分钟降到 1-2 分钟；或在 `.env` 调高 `BMP_INTERPRET_MAX_CONCURRENT`（网关有余量时近似线性提速）。

**验证结果**（192.168.50.31，项目 `da89d9a7-8747-4465-9aac-84fbe38b1bda`，湘潭大学采购项目）：

| 验证项 | 结果 |
|---|---|
| `POST /interpret/interpret/{id}` 提交耗时 | **19ms** 返回 `task_id`（改造前 600s 处 504） |
| 重复提交 | **409** + 「该项目正在解读中（通常需 5-15 分钟），请勿重复提交」 |
| `GET /interpret/task/{id}` | `pending` → `running` → `completed`，`result.success=true` |
| 未知 task_id | 返回 `{"status":"unknown"}`（非 404），前端可回退 |
| `interpret_running` | 运行期 `true`，结束后 `false` |
| 整链实际耗时 | 14:29:20 提交 → **14:40:22 落库 ≈ 11 分钟**，全程无一处 HTTP 超时 |
| 落库结果 | `analyses.dimensions` 41,056 字符，15 维度齐全 |
| 在途护栏 | 11 分钟内持续拒绝重复提交，结束后正常释放 |
| 部署态 | 5 容器全 healthy；`/api/health` 200；线上 bundle `index-Pkw2xGd8.js` 含 `interpret_running` / `AI 解读进行中` / `/interpret/task/` |

单 worker 下的容器内直调验证（不触发 LLM）：无在途登记时假 PID 返回 404（护栏不误伤）、预置在途登记后 **5ms** 返回 409、过期登记正确放行。

---

## 12. 资讯中心「智能推荐」聚合 500 修复（2026-09-06）

**现象**：资讯中心 → 智能推荐，点「开始首次采集」或「立即聚合抓取」都报 `Request failed with status code 500`。

**定位**：两个按钮走的是同一个 `handleAggregate`（`NewsPage.tsx:387` / `:1204`）→ `POST /news/aggregate`。API 日志里 5 次同一条堆栈：

```
File "/app/services/routers/news.py", line 623, in aggregate_hotspots
    from services.news.fetchers import get_fetcher
File "/app/services/news/fetchers.py", line 16, in <module>
    import feedparser
ModuleNotFoundError: No module named 'feedparser'
```

**根因**：`services/news/fetchers.py` 顶层 `import feedparser`，但 `pyproject.toml` 的 `dependencies` **从未声明该包**，镜像里没装。该导入写在路由函数体内（懒导入），所以应用能正常启动、健康检查也通过，只有真正点到聚合按钮才炸 —— 属于上游打包遗漏。

**容器内全量导入扫描**（139 个模块逐个 `importlib.import_module`）确认这是**全应用唯一**的缺包项，修复后 `failed=0`。

**连带发现的两个隐患**：
1. **同步 `requests` 阻塞事件循环**：`fetchers.py` 有 3 处 `requests.get`（RSS feed / 详情页 / GitHub API）直接跑在协程里。本项目 `UVICORN_WORKERS=1`，聚合期间整条事件循环被冻住，**全站接口无响应**（连 §11 的解读轮询都会一起卡死）；上层 `asyncio.Semaphore(4)` 的并发也形同虚设。且每个 RSS 条目还要再抓一次详情页（每源最多 30 次），很容易突破前端 120s 默认超时。
2. **数据源大面积失效**：36 个启用源实测只有 14 个返回 HTTP 200；**政府采购/公共资源交易类几乎全死**（`ccgp_*` 404、`ggzy_*` 404/SSL/超时、`cebpubservice` 404）。另 7 个 `type=crawl` 源走 `HTMLFetcher`，而该类是空壳（`fetchers.py:253` 直接 `return []`），静默产出 0 条。

**修复**（提交 `c2c2bc0`）：
- `pyproject.toml`：dependencies 补 `feedparser>=6.0`（实装 6.0.14）。
- `services/news/fetchers.py`：3 处 `requests.get` 全部改为 `await asyncio.to_thread(requests.get, ...)`，并发真正生效且不再阻塞事件循环。
- `api.ts`：`/news/aggregate` 超时 120s → **600s**。

**验证结果**：

| 验证项 | 结果 |
|---|---|
| `POST /news/aggregate` | **HTTP 200，35 秒**（修复前 500） |
| 采集/入库 | **72 条，全部入库**；`GET /news/hotspots` 返回 `total=72` |
| 事件循环是否被阻塞 | 聚合期间每 2s 探测 `/api/health`，**18 次全 200，最大延迟 10ms** |
| 容器内导入扫描 | 139 模块，**0 失败** |
| 部署态 | 5 容器全 healthy |

实际产出数据的源：`tds` 20 条、`techcrunch_ai` 19、`openai_blog` 13、`qbitai` 10、`mit_techreview` 10。19 个源报错（404/SSL/超时），7 个 crawl 源静默返回空。

**遗留（当时未改，属数据源治理而非代码缺陷；前两条已在 §13 解决）**：
- `is_hot` 命中 0 条 —— 现存可用源都是 AI 行业博客，不是招标公告，评分（阈值 60）普遍只有 56 左右。要让「智能推荐」真正推出招标商机，得替换/修复失效的政务采购源。**→ §13 已替换数据源并补齐评分字段。**
- `HTMLFetcher` 是空壳，7 个 `crawl` 源永远无产出（**→ §13 已实现为配置驱动**）；`news.py` 的 4 处仍用 `get_llm_gateway()`（env 默认），未接入 §9 的 `get_agent_gateway` 智能体模型配置（**仍未改**）。
- 同页的 `/news/refresh-hot`（循环跑所有监控任务）与 `/news/tasks/{id}/semantic-filter`（抓取 + LLM 过滤）同样是长耗时同步接口，仍用前端 120s 默认超时，属同类潜在超时风险，本次未改。

---

## 13. 资讯中心数据源治理：清掉死源 + 落地 HTMLFetcher（2026-09-07）

§12 修好了聚合 500，但留下两条尾巴：**`is_hot` 命中 0 条**、**`HTMLFetcher` 是空壳**。这两条的根因都不在代码逻辑，而在数据源本身，本次一并治理（提交 `1df3244`）。

### 13.1 失效源实测清单（全部在 api 容器内用真实代码跑）

| 源 | 实测结果 | 结论 |
|---|---|---|
| `ccgp_*` 的 RSS 地址 | 404 | 中国政府采购网**从未提供 RSS**，配置是凭空写的 |
| `ggzy_national`（ggzy.gov.cn） | 200，但内容是 **2024-01-19 的静态快照**，`/information/deal/` 404 | 死数据 |
| `ggzy_bj/sh/gd/js/zj/sd`（省级公共资源交易） | 全部 DNS 失败 / 超时 / SSL 错误 | 网络不可达 |
| `ebidding`（ctbpsp.com） | 200，页面是混淆 JS | 无静态条目，需浏览器渲染 |
| `cebpubservice` | 200，首页是**政策新闻**而非公告列表 | 选错页面 |
| `qianlima` | GBK + JS 渲染 | 无静态条目 |
| `zhaobiao`（zhaobiao.cn） | 200，但日期只有 `09.07` **没有年份** | 算不出时效分，弃用 |
| `chinamobile/chinaunicom/chinatelecom/sgcc/csg/chd` | 不可达或 JS 渲染 | 央企电子采购平台全部挡爬虫 |
| 新智元 / AI科技评论 / 智谱 / 百川 / 阶跃星辰 / The Batch / HF Blog | 404 或域名失效 | 从清单移除 |
| `NewsCrawlerSkill._get_default_sites()` 的 5 个兜底站点 | chinabidding、bidlink 解析出 **0 条**（纯前端渲染）；bidcenter 只抓到「添加到收藏夹」「立即返回首页」；cebpubservice 是政策新闻；ccgp 汇总页 `/cggg/zygg/` 的 `<a>` 文本只有频道名 4 个字，被长度下限过滤 | 合计 **0 条有效数据**，已换成 3 个 ccgp 分类频道页 |

**汇总**：36 个启用源只有 14 个返回 200，其中真正能解析出条目的**只有 5 个 AI 行业博客**——所以「智能推荐」被 72 条 AI 博客刷屏、招标采购商机 0 条。

### 13.2 换上的数据源（6 个启用，2026-09-07 实测 200 + 当日真实条目）

| code | URL | 选择器 | 编码 | 实测条数 |
|---|---|---|---|---|
| `ccgp_central_zbgg` | `http://www.ccgp.gov.cn/cggg/zygg/zbgg/` | `ul.c_list_bid > li` | 自动探测（UTF-8） | 20 |
| `ccgp_central_xjgg` | `http://www.ccgp.gov.cn/cggg/zygg/xjgg/` | 共享 `&ccgp_list` anchor | 同上 | 20 |
| `ccgp_central_cjgg` | `http://www.ccgp.gov.cn/cggg/zygg/cjgg/` | 同上 | 同上 | 20 |
| `ccgp_local_zbgg` | `http://www.ccgp.gov.cn/cggg/dfgg/zbgg/` | 同上 | 同上 | 20 |
| `ccgp_local_cjgg` | `http://www.ccgp.gov.cn/cggg/dfgg/cjgg/` | 同上 | 同上 | 20 |
| `okcis` | `https://www.okcis.cn/` | `li:has(div.list-left-2024313)` | **GB18030** | 23 |

选它们的理由：**权威**（财政部主管的中国政府采购网 + 中国建设工程招标网）、**当日更新**、**列表页就是静态 HTML**（不需要浏览器渲染）、**条目自带地域和采购人**（可直接喂评分与去重指纹）。

另外保留但**默认关闭**：2 个 ccgp 汇总页（与分类频道内容重叠）、8 个实测可用的 AI 博客源（正是之前刷屏的元凶）、`github_tender`。需要时在 **资讯中心 → 数据源管理** 里单独开启。

### 13.3 新增一个数据源：只改 YAML，不用改代码

`HTMLFetcher` 已从空壳实现为**配置驱动**，抓取规则全部写在 `services/news/sources.yaml` 的 `config` 里：

| config 字段 | 必填 | 说明 |
|---|---|---|
| `item_selector` | 是 | 列表项 CSS 选择器。soupsieve 支持 `:has()`，可用 `li:has(div.xxx)` 从子元素反查父项 |
| `link_selector` | 否 | 条目内链接选择器，默认 `a` |
| `encoding` | 否 | 强制编码。不填时，若服务端未声明 charset（requests 会退到 `iso-8859-1`，中文必乱码）自动改用 `apparent_encoding` 探测 |
| `date_pattern` | 否 | 含 1 个捕获组的正则，从条目文本提取发布时间，结果经 `_normalize_pub_date` 归一成 ISO |
| `field_patterns` | 否 | `{字段名: 正则}`，可提 `region` / `owner_org` / `project_code` / `bid_deadline`，直接进评分与落库 |
| `max_items` | 否 | 单源最多条目数，默认 30 |
| `min_title_len` | 否 | 标题最小长度，滤掉「更多」「首页」等导航短链接，默认 10 |

新增站点的完整步骤：
1. 在 `sources.yaml` 对应分组下加一条（`name`/`code`/`type: crawl`/`url`/`industry`/`weight`/`enabled`/`description`/`config`）；
2. 用浏览器 F12 找到列表项的 CSS 选择器填进 `item_selector`，需要时补 `date_pattern` 与 `field_patterns`；
3. 部署后调 `POST /api/news/sources/sync?force_enabled=true` 把新源写进 DB；
4. 点「立即聚合抓取」验证条数与字段填充率。

**实测踩到的 3 个坑**：
- ccgp 的 `<a>` 可见文本被 CSS 截断，**`title` 属性才是完整标题**，`HTMLFetcher` 已优先取 `title`；
- ccgp 的**频道页没有** `<em rel="bxlx">公告类型</em>`（只有汇总页 `/cggg/` 才有）。一开始照汇总页结构配了 `announce_type: '(\S+)\s+发布时间：'`，结果把整条标题误抓成「公告类型」。公告类型本来就在标题里（…中标公告 / …询价公告），不需要单独抓；
- 地域为空的条目（如雄安新区）后面紧跟的就是 `采购人：` 标签，`region` 正则必须用负向先行断言排除标签词：`地域：\s*(?!(?:采购人|发布时间|代理机构|地域)：)(\S+)`，否则会把「采购人：」当成地域值抓进来。

**只抓列表页、不追详情页**：招标列表的「标题 + 地域 + 采购人」已足够评分与去重，逐条追详情会让单源多出几十次请求，把聚合从 35 秒拖到分钟级以上。请求与 BeautifulSoup 解析都走 `asyncio.to_thread`，延续 §12 的不阻塞事件循环原则。

顺带修了 `NewsCrawlerSkill._resolve_url`：原来手拼 `base + href`，ccgp 的 `./202609/xxx.htm` 会得到 `.../zbgg/./202609/xxx.htm` 这种带 `/./` 的脏 URL，与 `HTMLFetcher` 用 `urljoin` 生成的同一篇公告 URL 不相等，去重会失效。现已统一改用 `urljoin`。

### 13.4 为什么 72 条评分全一样：NewsItem 缺字段

`scoring.BusinessValueScorer` 的权重里 **region 占 15%、amount 占 20%**，`hotspot_items` 落库也读 `region/owner_org/project_code/bid_deadline/amount`。但 `NewsItem` 这个 dataclass **根本没有这些字段**——抓取器即使解析到了也传不到下游，地域分和金额分恒为 0，所有条目只剩时效性分，于是 72 条清一色 56 分、`is_hot`（阈值 60）命中 0 条。

本次给 `NewsItem` 补齐了这 5 个字段，并新增 `_normalize_pub_date`：各站点日期写法五花八门（`2026年09月07日` / `2026/9/7` / `2026.09.07`），而 `scoring._freshness` 用 `datetime.fromisoformat` 解析，不归一就直接抛错、时效分算不出来。

> `is_hot` 能否命中还取决于公司画像（`company_profile` 的 regions / keywords）——地域分要画像里有对应地域才拿得到。

### 13.5 部署后必做：强制下发启用状态

`sync_sources_to_db` 对**已存在**的源默认**不覆盖 `enabled`**（以 DB 为准，保留管理员在 UI 里的手动开关）。所以换完源清单后必须显式跑一次：

```bash
TOKEN=$(curl -s -X POST http://192.168.50.31/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@bidmaster.pro","password":"admin123"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -X POST "http://192.168.50.31/api/news/sources/sync?force_enabled=true" \
  -H "Authorization: Bearer $TOKEN"
```

这一步做两件事：① 把 YAML 里仍存在的 9 个源的 `enabled` 对齐（8 个 AI 源 + `github_tender` 关掉，6 个新源打开）；② **prune** —— YAML 中已删除、但 DB 里仍 `enabled=true` 的死源自动停用（保留行以留存抓取历史），否则它们会永远启用、每次聚合都报一次错。本次启动时 prune 掉了 13 个。

prune 是**无条件**执行的（应用启动时的自动同步也会跑）；`force_enabled` 只影响第 ① 件事。

### 13.6 验证结果（2026-09-07 已部署到 192.168.50.31）

**部署前**（容器内直接跑真实 `HTMLFetcher` 代码）：

| 验证项 | 结果 |
|---|---|
| YAML 解析 | 17 源 / 6 启用，anchor 共享生效，正则全部可编译，中文无损 |
| 6 个启用源真实抓取 | **123 条、0 错误** |
| 字段填充率 | 123/123 带 ISO 日期，**97 条带地域**、**100 条带采购人** |
| 日期范围 | `2026-09-02` ~ `2026-09-07T10:15`（当日数据） |

**部署后**（提交 `1df3244`；未改依赖，镜像构建全部走缓存，api 容器 **19 秒**恢复 healthy）：

| 验证项 | 结果 |
|---|---|
| 启动自动同步 | prune 停用 **13 个**「YAML 已移除但 DB 里仍 enabled」的死源，新插入 **8 个**源 |
| `POST /sources/sync?force_enabled=true` | `{"success":true,"synced":0,"force_enabled":true}`，8 个 AI 源 + `github_tender` 全部关闭 |
| `GET /news/sources` | DB **46 行 / 6 启用**，与 YAML 完全一致 |
| `POST /news/aggregate` | **HTTP 200，1.01 秒**（§12 是 35 秒 —— 不再逐条追详情页），`total=123 saved=123` |
| 聚合期间事件循环 | 每 2s 探测 `/api/health`，**60 次全 200，最大延迟 10ms** |
| 6 个源产出 | ccgp 5 个频道各 **20 条** + okcis **23 条**，**0 报错** |
| 字段落库 | `region` 非空 **101 条**、`owner_org` 非空 **100 条**（`hotspot_items` 现已存 region / owner_org / project_code / amount / bid_deadline） |
| 行业分类 | 真正分散：其他/综合 48、设备采购 22、高校采购 11、信息化采购 10、市政工程 8、医院采购 4… |
| 评分分布 | **3 档**（57.5 × 100 / 56.0 × 7 / 47.0 × 16），不再是清一色 56 分 |
| `GET /news/hotspots` | 200，返回体已带 `region` / `owner_org` / `project_code` / `amount` / `bid_deadline` |
| `GET /news/today-hot` | 200，`total=127`（当日），全部来自聚合 |
| `GET /news/industries` | 200，12 个行业大类 |

**`is_hot` 仍为 0 —— 这次不是数据问题，是没人传公司画像。**

`POST /news/aggregate` 的请求体本来就支持 `company_profile`（`news.py:617`）和 `is_hot_threshold`（默认 60），但前端「立即聚合抓取」发的是**空 body**，于是 `company_profile={}`。57.5 分是这么来的：

| 维度 | 权重 | 空画像时的取值 | 得分 |
|---|---|---|---|
| urgency | 20% | 列表页没有截止时间 → `_urgency` 兜底 0.5 | 10.0 |
| match | 30% | `if not profile: return 0.5` | 15.0 |
| amount | 20% | 列表页没有金额（123 条里仅 1 条带金额）→ 0.5 | 10.0 |
| region | 15% | `if not profile: return 0.5`（**根本不看 item 的 region**） | 7.5 |
| freshness | 15% | 6 小时内 → 1.0 | 15.0 |
| | | **合计** | **57.5** |

即 `region` 这 15% **只有在画像带 `regions` 时才生效**（命中 1.0 = 15 分，不命中 0.4 = 6 分）。本次补齐字段是**必要条件**（没有 region 值就永远不可能命中），但还需要有人把画像传进来。

用一份样例画像 `{"regions":["湖南","北京","广东"],"industries":["0701","0203","1002"],"keywords":["智慧课程","通信工程","信息化","云"]}` 对库里 199 条做**只读**重算（不写库）：

- 评分从 42.5 分散到 **80.0**，共 12 个不同档位；
- `is_hot`（≥60）命中 **77 / 199**；
- `region` 分项出现两种取值：**15.0（17 条命中偏好地域）** 与 6.0（其余）—— 地域维度确实生效了。

要让线上「智能推荐」真正推出热门商机，还差**把公司画像接进聚合链路**这一步（前端传 `company_profile`，或后端从用户/公司配置读取），属功能增强，本次未做。

> **残留数据已清除（2026-09-07 执行）**：`hotspot_items` 里原有 **76 条**旧 AI 博客条目（TechCrunch AI 21、Towards Data Science 20、OpenAI Blog 15、量子位 10、MIT Technology Review 10），来自本次已停用的源，日期 2026-08-31 ~ 09-06，混在列表里干扰阅读。删除前已核对：76 行全部 `is_converted=0`、`is_hot=0`，且 `information_schema` 查无任何外键引用 `hotspot_items`，故直接事务内删除；删前用 `\copy` 备份到宿主机 `~/backup/hotspot_items_ai_backup_20260907.csv`（162KB，1 表头 + 76 行）。删除后 `hotspot_items` = **143 行 / 143 个唯一 URL / 121 条带地域**，全部来自 6 个招标源；`GET /news/hotspots` 与 `/news/today-hot` 均返回 `total=143`。当时执行的语句：
>
> ```bash
> echo "delete from hotspot_items where source in ('TechCrunch AI','Towards Data Science','OpenAI Blog','量子位','MIT Technology Review')" \
>   | ssh -i ~/.ssh/id_ed25519_workbuddy andy@192.168.50.31 \
>     'docker exec -i bidmaster-postgres-1 psql -U bidmaster -d bidmaster'
> ```

## 14. 投标检查「全面检查全是异常」修复 + 上传模式异步化（2026-09-08）

**提交**：`05dbf5a`（主修复）+ `dd3adbf`（发布验证阶段抓到的 500）。本地分支 `feature/custom-llm-provider` → push 到服务器远端 `srv` 的 `deploy/local` → 服务器 `git merge --ff-only`，重建 **api + web** 两个镜像。

### 14.1 现象与取证

用户报「投标检查 → 上传标书检查 → 全面检查」，上传招标+投标文件后 15 项结果**全部显示「异常」**且不给原因；同页**单项检查**与**项目模式**全面检查都正常。

直接打接口取证（不靠读代码猜）：

| 请求 | 耗时 | 结果 |
|---|---|---|
| `check_type=fullCheck` | **0.005s** | 15 项 error 全是 `cannot access free variable 'importlib' where it is not associated with a value in enclosing scope` |
| `check_type=compliance` | **78s** | HTTP 200，结果正常 |

0.005s 全失败 ⇒ 根本没进入真正的检查工作，在「加载 skill」阶段就炸了。

### 14.2 根因

`services/routers/check.py` 的 `upload_and_check` 里，嵌套闭包 `_run_upload_skill` 用 `importlib.import_module` 加载 skill，而**同一函数靠后的单项分支又写了一句 `import importlib`**（模块级第 4 行本来就有）。函数内 `import` 等价于赋值、作用域是**整个函数**，于是 `importlib` 成为该函数的局部名并遮蔽模块级导入；闭包只能把它当外层自由变量读。`fullCheck` 分支在 `asyncio.gather` 处就跑完了 15 项、**永远执行不到那句 import**，cell 为空 → 15 项齐抛 NameError，再被兜底 `except` 吞成 `success=False`，前端只剩「异常」两个字。

项目模式 `_do_full_check` 的 `import importlib` 恰好写在嵌套函数定义**之前**（差两行），cell 已绑定所以正常 —— 同一份代码、同一个 bug 模式，只因导入语句位置差两行，一个能用一个全废。

### 14.3 修复（方案 C）

1. 删掉函数内 `import importlib`；用 AST 扫全仓同款，连带清掉 3 处同样写法的函数内 `import re`。
2. 三处各写一遍的 skill 加载逻辑收敛为 `_exec_check_skill()`，返回结构统一 `{success, data, error, warnings}`（原失败分支缺 `data`/`warnings`，而 8 个 skill 是会产出 warnings 的）；15 项清单与参数装配抽为 `_FULL_CHECK_TYPES` / `_full_check_params()`，项目模式与上传模式共用一份，避免各自维护漂移。
3. 上传模式整条改为**异步任务 + 轮询**（与项目模式 `full-check` 一致）：单项实测 78s，15 项同步等待必撞 axios 300s / nginx 600s 超时。
4. 两条链都加 `asyncio.Semaphore` 限流（裸 `gather` 会把 15 路请求一次压到网关上，触发限流即 15 项集体失败）。
5. 前端轮询预算 120×3s（6 分钟）→ 400×3s（20 分钟）。
6. 前端把一直丢弃的 `error` 显示出来：全面检查详情卡新增「执行异常原因」块 + 悬浮提示；失败行的风险等级/是否严重问题显示 `-`；单项失败不再渲染成「低风险 + 空 `{}`」的假通过态。

### 14.4 发布验证阶段抓到的第二个坑（`dd3adbf`）

主修复上线后跑主诉路径（fullCheck）15/15 通过，但**补跑单项检查回归**时发现 `check_type=compliance` 直接 500：`{"detail":"服务器内部错误","error":"name 'task_id' is not defined"}`，0.0097s 返回 —— 上传模式单项检查被改坏了（改造前它是 200/78s 可用的）。

原因：提交响应的 `message` 写成三元表达式，两个分支长得几乎一样但**一个是普通字符串、一个是 f-string**：`fullCheck` 分支的 `{task_id}` 只是字面占位（仓库既有写法，不求值），单项分支是真 f-string，而作用域里只有 `task`、没有裸 `task_id`。全面检查走不求值的那一支，所以主诉路径完全正常。修法：两个分支统一 f-string + `task.task_id`。

**教训**：发布验证必须覆盖被改动的**每条**分支，不能只验用户主诉的那条。

### 14.5 接口契约变化（重要）

`POST /api/check/upload-check` **不再同步返回检查结果**，改为立即返回：

```json
{"task_id":"...","status":"pending","source":"upload","check_type":"fullCheck",
 "bid_filename":"...","tender_filename":"...","message":"...请通过 GET /check/task/{task_id} 查询进度"}
```

结果统一从 `GET /api/check/task/{task_id}` 取（`status=completed` 时载荷在 `result` 字段里）。文件解析仍在提交请求内同步做，格式问题立刻以 400 反馈，不必等轮询。

前端保留了「响应里没有 `task_id` 就回落同步渲染」的分支，所以**只发前端不会崩**；反之只发后端会渲染出垃圾 —— **api 与 web 两个镜像必须一起重建**。

### 14.6 运维约束

- `UVICORN_WORKERS` **必须为 1**：`TaskManager` 是进程内单例，解读/生成/检查的「提交 + 轮询」要求两次请求落在同一个 worker，多 worker 会随机出现轮询 404「任务不存在」（服务器 `.env` 已是 1）。
- `BMP_CHECK_MAX_CONCURRENT` 默认 8、钳制 1-15（compose 已登记，`.env` 未显式写则取默认）。调低更稳（网关不易限流）、调高更快。

### 14.7 验证结果（2026-09-08，192.168.50.31 实测）

| 用例 | 结果 |
|---|---|
| 上传模式 `fullCheck` | 提交 **0.32s** 返回 `task_id`；任务 **278s** 完成；**15/15 `success=true`**（修复前 0.005s、15/15 全失败） |
| 上传模式 `compliance`（单项） | 提交即返回 `task_id`（含真实 id 的 message）；**81.3s** 完成，`success=true`，1 条 finding |
| 项目模式 `full-check`（湘潭大学项目，191 章） | **729s** 完成，**14/15 `success=true`**；唯一失败项 `aiTextCheck` 见 §14.8 |
| web 镜像 | `/assets/index-BC0n88Hq.js` 含「执行异常原因」「该项检查执行异常，未产出结果」「已等待 20 分钟」等新串 |
| 服务健康 | api healthy（3s）、`db_ready:true`、nginx 反代 `/api/health` 200（本次无需重启 web） |

> 验证在真实项目上产生了一条 `full_check` 报告（`eb3ccf56-a7ac-4df4-a736-e1bda74bf882`，2026-09-08 06:26 UTC），与既有的 `d868889d`（05:52 UTC）并存，可在「检查报告」列表里看到。

### 14.8 本轮新发现（未修，待定方案）

1. **`collect_json` 标注 `-> dict` 但实际会返回 list**（本轮项目模式验证时真实触发）。`core/llm_gateway/gateway.py:197` 标注 `-> dict`，实际返回 `json_repair.repair_and_validate()` 的结果，而后者签名是 `-> dict | list`。`ai_text_check_skill.py:59` 调 `collect_json` 时未传 `schema`/`validator`，校验层形同虚设；本次模型耗时 342s 后返回**长度 2 字符的 `[]`**（日志 `原始文本长度=2字符, 前200字符=[]`、`结果类型=list`），下一行 `result.get("issues", [])` 抛 `'list' object has no attribute 'get'` → skill `success=false` → UI「执行异常」。同一次运行里其余 **15 次调用都返回 dict**，所以表现为"偶尔有一项异常"。全仓 `collect_json(` 共 **41 处 / 27 个文件**，大多紧跟一句裸 `.get`，任何一次模型返回 JSON 数组都会复现。
   建议：给需要 dict 的调用点传 `validator=lambda d: isinstance(d, dict)`（走 `repair_and_validate` 既有的 `max_repair_attempts=2` 修复回路重新问模型），并在 `collect_json` 内对非 dict 结果兜底归一 + 改掉说谎的类型标注。单点修好，41 个调用点一起受益。
2. **上传模式的 `_parse_uploaded_file` 是独立弱化解析器**（方案 D，本轮未做）：不支持 `.doc`/`.wps`（而 `projects.py` 的白名单支持），异常与未知后缀一律 `decode(errors="replace")` **静默返回二进制乱码**，扫描件 PDF 抽空、不校验招标文件为空、也不走 `_truncate_text` 截断。乱码非空能绕过所有「内容为空」守卫，会让检查结果不可信。建议改用仓内已有的 `core/doc_engine.get_parser`（§10 那条链已处理 OLE2/PDF/CJK）。
3. **检查链很慢**：质量检查 Agent 当前用 `qwen3.8-max`，单项 78s、全面检查 4-12 分钟；网关单次请求超时 300s、`max_retries=2`，重文档下会出现 300s 超时重试（本轮项目模式日志里 3 次）。改「设置 → 智能体模型配置 → 质量检查Agent」为 `qwen3.7-flash` 是纯配置提速，无需改代码，但需登录后台操作。

## 15. v0.2.0 版本收口与 GitHub 同步（2026-09-08）

之前 22 个提交只 push 到内网 `srv`，GitHub fork（`andyke668/BidMaster-Pro`）一直停在 fork 基线 `cc89d90`。本次把版本收口成 **v0.2.0** 并三处同步。

### 15.1 做了什么

| 项 | 内容 |
|---|---|
| 版本号 | `0.1.0` → `0.2.0`，**5 处落点**全部改齐：`pyproject.toml:3`、`README.md` 徽章、`packages/desktop/package.json:3`、`services/main.py:73`（`FastAPI(version=)`）、`services/main.py:165`（`/api/health` 返回值）。后两处原先是**硬编码字面量**，与 pyproject 无关联，所以容器里报的版本号永远停在 0.1.0 —— 现在 `curl /api/health` 就能核对线上跑的是哪版 |
| CHANGELOG | 新增 `CHANGELOG.md`：Keep a Changelog 1.1.0 + SemVer 2.0.0 + Conventional Commits 1.0.0；0.2.0 一节按「破坏性变更 / 新增 / 修复（分模块）/ 重构 / 依赖 / 发布验证 / 已知问题 / 提交清单」组织，每条挂提交号 |
| 提交 | `76d078e` `chore(release): v0.2.0 —— 提升版本号并补 CHANGELOG` |
| 标签 | 附注标签 `v0.2.0`（tag 对象 `afea61b` → commit `76d078e`），标签消息里写清基线与破坏性变更 |
| GitHub | `origin/main` `cc89d90` → `76d078e`（快进）；新推分支 `feature/custom-llm-provider`；推标签；创建 Release <https://github.com/andyke668/BidMaster-Pro/releases/tag/v0.2.0> |
| 内网 srv | `deploy/local` → `76d078e`、标签 `v0.2.0` 已推；服务器 `main` 用 `git merge --ff-only deploy/local` 跟上 |

### 15.2 为什么是 0.2.0 而不是 0.1.1 / 1.0.0

- 有 `feat(llm)` 自定义供应商这种**功能新增** → MINOR；
- 有两处**接口契约破坏性变更**（`upload-check` 与 AI 解读改为「提交任务 + 轮询」）→ 0.x 阶段按 SemVer 惯例，破坏性变更计入 MINOR（`0.y+1.0`）而不是 MAJOR；
- 不打 1.0.0：仍有 §14.8 的已知问题与 §8 的上游遗留（登录态在进程内字典、alembic 不可用、循环外键无 cascade），不足以声明稳定 API。

### 15.3 两个流程坑

1. **`git push srv main:main` 会被拒**：`~/bidmaster-pro` 是**非裸仓库**且 `main` 正是当前 checkout 的分支，默认 `receive.denyCurrentBranch=refuse` →
   `remote: error: refusing to update checked out branch: refs/heads/main`。
   正确做法（也是本项目一直用的流程）：**push 到 `deploy/local`，再在服务器上 `git merge --ff-only deploy/local`**。不要为了省事去改 `denyCurrentBranch` 或 `receive.denyCurrentBranch=updateInstead`——那会让工作区与索引在你不知情的情况下被改写。
2. **只想改版本号，却触发了 api 镜像全量重装依赖**：`Dockerfile.api` 是 `COPY pyproject.toml README.md ./` 之后紧接 `RUN pip install --prefix=/install ...`。`pyproject.toml` 内容变了（哪怕只是 `version` 一行）就会让这一层及其**之后所有层**的缓存失效，于是 chromadb / weasyprint / pymupdf / onnxruntime / langgraph 等全部重下载重编译，实测 **10 分钟以上**（而只改 `services/**.py` 时构建约 30s，因为那些文件在更靠后的 COPY 层）。
   ⇒ **发布节奏建议**：把版本号提升与代码修复**分开提交、分开部署**，或者攒到一次发布里一起做；不要为了"让 /api/health 显示新版本号"单独发一次 pyproject 改动。若要根治，可把版本号的单一来源改为构建期注入（`ARG BMP_VERSION` → `ENV`，代码里读环境变量），让 `pyproject.toml` 不再因发版而变动。

### 15.4 升级 / 回滚

```bash
# 升级（服务器）
cd ~/bidmaster-pro && git fetch --tags && git merge --ff-only deploy/local
cd docker && docker compose -p bidmaster --profile infra --profile web up -d --build api web
curl -fsS http://127.0.0.1:8000/api/health      # 期望 "version":"0.2.0"

# 回滚到上一个已验证版本
cd ~/bidmaster-pro && git checkout d03f036      # 或 git reset --hard <tag>
cd docker && docker compose -p bidmaster --profile infra --profile web up -d --build api web
```

> 存量库升级别忘了 §9 的 `ALTER TABLE llm_provider_configs ADD COLUMN IF NOT EXISTS models TEXT`（本机 2026-09-06 已执行，无需重复）。

## 16. v0.4.0 管理后台「使用监控」部署（2026-09-24）

### 16.1 这次部署多出来的步骤

| 步骤 | 命令 | 为什么 |
|---|---|---|
| 1 | `bash ~/deploy/deploy_bidmaster.sh code` | 拉代码 + 重放部署补丁（rebase） |
| 2 | `bash ~/deploy/deploy_bidmaster.sh migrate` | **新增**。执行 `db/migrations/001_admin_monitor.sql`（4 张新表 + users 补 2 列）。必须在重启 api 之前跑：`create_all` 只建缺失的表、不给已有表补列，顺序反了 ORM 一查 users 就 `column does not exist`。脚本幂等，可重复跑。 |
| 3 | `bash ~/deploy/deploy_bidmaster.sh llm` | 写 LLM 网关配置（幂等） |
| 4 | `bash ~/deploy/deploy_bidmaster.sh up` | 构建 + 启动 |
| 5 | `bash ~/deploy/deploy_bidmaster.sh seed` | 灌 `settings.monitor` 权限码与 `bid_checker` 角色（幂等） |

迁移对旧版 api 向后兼容（只加表加列，旧代码不读），所以**先 migrate 再 up 没有停机窗口**。

### 16.2 部署脚本这次修的两个坑

1. **`git rebase` 需要 git 身份**：`fetch_code` 用 rebase 重放部署补丁提交，仓库没配 `user.name`/`user.email` 时 rebase 当场失败，并把仓库卡在半途（HEAD 游离、补丁躺在暂存区），下一次部署接着报错。现在 `fetch_code` 开头先写仓库级身份，rebase 失败时自动 `--abort`，并让 `deploy/local` 标记分支始终跟着 main 尖端。
2. **探活路径没跟发布前缀走**：nginx 只发布 `/zdx/` 之后，`fix_web_proxy` 还在探 `:8081/api/health`，恒 404；叠加 `set -euo pipefail` 会把同一条命令链后面的 `seed_db` 一起带走（表现为「seed 没跑，但脚本看起来执行完了」）。现在探活统一带 `WEB_BASE`（默认 `/zdx`）；`verify_bidmaster.sh` 同步修正，并新增「根路径必须 404」「`/zdx/admin` SPA 回退必须 200」两条断言。

### 16.3 验证结果

- 本地 `测试/verify_admin_monitor.py`：**194 项断言全通过**（含新增 [9.5] 项目名补齐一节）。
- 服务器 `verify_bidmaster.sh`：全部通过（匿名 401、`settings.monitor` 已入库、管理员可读总览）。
- 服务器 `e2e_admin_monitor.sh`（新增，位于 `~/deploy/`）：**56 项全通过**，覆盖匿名 401 / 非管理员 403 / 12 个端点返回结构 / 6 个时间区间 / 行为归属 / 项目名补齐 / 配额 429 / GET 不限流 / 强制下线 / 禁用启用 / CSV 导出；结束自动清理临时账号与联调项目。
- 浏览器实测：`/zdx/admin` 七个页签全部渲染真实数据，控制台零报错；行为流水可见具体项目名与标书文件名。

### 16.4 回滚锚点（v0.4.0 之前的镜像）

```
api: sha256:4c462a9b0c55f963b44e1d69960a383d7cabdd761e7c157b2a91c9cde842c9d9
web: sha256:1289249606220f1d87b626f3855d87b57665381627e04e12330a7d47607046b2
```

回滚 = `git -C ~/bidmaster-pro checkout <旧提交>` + `up`；迁移是**只增不改**，回滚代码不需要回滚库。

### 16.5 升级后须知

- 登录态已迁到 `user_sessions` 表：**升级后所有人要重新登录一次**（进程内 token 全部作废）。
- 管理员侧边栏新增「使用监控」；非管理员访问 `/zdx/admin` 会被前端挡回，直接调接口返回 403。
- 磁盘余量紧张（87% 已用）：本次靠 `docker image prune -f` 与构建缓存撑过，后续发版前先看 `docker system df`。