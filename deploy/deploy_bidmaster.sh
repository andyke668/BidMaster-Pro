#!/usr/bin/env bash
# ============================================================================
# BidMaster Pro — Docker 部署脚本
# 目标机：默认 192.168.50.31（测试）；生产 192.168.50.16 需带 PROJECT=zdx 等覆盖，见 §17
#
# 用法：
#   bash deploy_bidmaster.sh          # 全流程（取码→补丁→配置→拉镜像→构建→启动）
#   bash deploy_bidmaster.sh code     # 仅取码+打补丁+写配置
#   bash deploy_bidmaster.sh pull     # 仅预拉基础镜像
#   bash deploy_bidmaster.sh up       # 仅构建并启动
#   bash deploy_bidmaster.sh migrate  # 仅执行 db/migrations/*.sql（幂等，可重复跑）
#   bash deploy_bidmaster.sh down     # 停止（保留数据卷）
#
# 设计原则：
#   1. 所有部署期改动都是「幂等补丁」，落在 ~/bidmaster-pro 工作区，
#      可用 `git -C ~/bidmaster-pro diff origin/main` 审阅。
#   2. 精简服务集：只跑 postgres + api + web。
#      minio / redis / celery 在当前代码中未被调用（见 技术_Docker部署_192.168.50.31.md §3）。
#   3. 国内网络：基础镜像走 daocloud，pip 走 aliyun，npm 走 npmmirror。
# ============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/bidmaster-pro}"
REPO_URL="${REPO_URL:-https://github.com/andyke668/BidMaster-Pro.git}"
IMG_MIRROR="${IMG_MIRROR:-docker.m.daocloud.io}"
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_HOST="${PIP_HOST:-mirrors.aliyun.com}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
HOST_ADDR="${HOST_ADDR:-192.168.50.31}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-8081}"
# 生产只发布 /zdx 子路径（见 docker/nginx.conf）：根路径与 /api 一律 404，
# 探活必须带上子路径前缀，否则 set -e 会把后续步骤（如 seed_db）一起带走。
WEB_BASE="${WEB_BASE:-/zdx}"
SERVICES="${SERVICES:-postgres api web}"
PROJECT="${PROJECT:-bidmaster}"
PG_USER="${PG_USER:-bidmaster}"
PG_DB="${PG_DB:-bidmaster}"
# 资源上限：各目标机余量差别很大（50.31 是 5.8G 纯测试机；50.16 是生产机，
# 还并跑宝塔面板/MySQL/MinIO/Redis），必须可按机覆盖，不能写死。
API_MEM_LIMIT="${API_MEM_LIMIT:-1g}"
PG_MEM_LIMIT="${PG_MEM_LIMIT:-512m}"
WEB_MEM_LIMIT="${WEB_MEM_LIMIT:-128m}"

# ── 内网 LLM 网关（复用 openbidkit-web 的 YIBIAO_AI_API_KEY）──
LLM_SRC_ENV="${LLM_SRC_ENV:-$HOME/openbidkit-web/.env}"
GW_BASE="${GW_BASE:-http://192.168.40.113:8088/v1}"
GW_TEXT_MODEL="${GW_TEXT_MODEL:-qwen3.8-max}"
GW_FALLBACK_MODEL="${GW_FALLBACK_MODEL:-qwen3.7-flash}"
GW_EMBED_MODEL="${GW_EMBED_MODEL:-qwen3.7-text-embedding}"
# 注意：compose 里 api 对 redis/minio 是 required:false 的弱依赖，但 --profile infra
# 一旦启用它们就会被当作依赖一并拉起，所以必须一起预拉，否则会去连 Docker Hub 超时。
BASE_IMAGES="${BASE_IMAGES:-python:3.12-slim node:20-alpine nginx:1.27-alpine postgres:16-alpine redis:7-alpine minio/minio:latest minio/mc:latest}"

# 注意：API_PORT / WEB_PORT 这两个名字同时是 docker/.env 里的 compose 插值变量，
# 而 shell 环境变量优先级高于 .env。若调用方 export 了它们（本脚本自己也用同名
# 变量做探活），compose 就会用命令行的值覆盖 .env 里的 "127.0.0.1:8000"，
# 把 api 端口从仅回环静默改成 0.0.0.0 全网卡暴露。这里显式剔除，保证端口绑定
# 只由 docker/.env 决定。
COMPOSE=(env -u API_PORT -u WEB_PORT docker compose -p "$PROJECT")

say() { printf '\n\033[1;36m=== [%s] %s ===\033[0m\n' "$(date '+%F %T')" "$*"; }
die() { printf '\n\033[1;31m[FAIL] %s\033[0m\n' "$*" >&2; exit 1; }

# ─────────────────────────── 1. 取代码 ───────────────────────────
fetch_code() {
  say "1/5 获取代码 -> $REPO_DIR"
  command -v git >/dev/null || die "服务器未安装 git"
  if [ -d "$REPO_DIR/.git" ]; then
    # rebase 要重放部署补丁提交，仓库没有身份就会当场失败，还会把 rebase
    # 卡在半途（HEAD 游离 + 补丁躺在暂存区），下一次部署接着报错。
    git -C "$REPO_DIR" config user.name  'deploy-bot'  >/dev/null
    git -C "$REPO_DIR" config user.email 'deploy@local' >/dev/null
    if [ -d "$REPO_DIR/.git/rebase-merge" ] || [ -d "$REPO_DIR/.git/rebase-apply" ]; then
      echo "  ! 检测到上次未完成的 rebase，先中止再重来"
      git -C "$REPO_DIR" rebase --abort || true
    fi
    git -C "$REPO_DIR" fetch origin --prune
    git -C "$REPO_DIR" checkout main
    # 保留部署补丁：rebase 到上游最新，冲突时中止并提示
    if git -C "$REPO_DIR" rev-parse --verify -q deploy/local >/dev/null; then
      git -C "$REPO_DIR" rebase origin/main || {
        git -C "$REPO_DIR" rebase --abort || true
        die "部署补丁与上游冲突，请手工处理 $REPO_DIR"
      }
      # 让标记分支始终跟着 main 的尖端，避免下次拿到过期基线
      git -C "$REPO_DIR" branch -f deploy/local HEAD
    else
      git -C "$REPO_DIR" reset --hard origin/main
    fi
  else
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  git -C "$REPO_DIR" log -1 --pretty='当前基线: %h %ad %s' --date=short
}

# ─────────────────────── 2. 部署补丁（幂等） ───────────────────────
patch_repo() {
  say "2/5 应用部署补丁"
  cd "$REPO_DIR"

  # 2a) pyproject.toml：chromadb 钉 0.5.x（代码按 0.5 语义写 list_collections）
  if ! grep -q '"chromadb>=0.5,<1.0"' pyproject.toml; then
    sed -i 's/"chromadb>=0\.5"/"chromadb>=0.5,<1.0"/' pyproject.toml
    echo "  - chromadb 钉版本 >=0.5,<1.0"
  fi

  # 2b) pyproject.toml：显式声明 bcrypt（auth.py 直接 import，原仓库靠 chromadb 传递依赖）
  if ! grep -q '"bcrypt>=4.0"' pyproject.toml; then
    sed -i '/"fastapi>=0\.115",/a\    "bcrypt>=4.0",' pyproject.toml
    echo "  - 补声明 bcrypt>=4.0"
  fi

  # 2c) pyproject.toml：移除 sentence-transformers（会拖入 PyTorch 约 4GB；
  #     仅 BMP_EMBEDDING_MODE=local 才惰性导入，本机用 api 模式）
  if grep -q '"sentence-transformers' pyproject.toml; then
    sed -i '/"sentence-transformers/d' pyproject.toml
    echo "  - 移除 sentence-transformers（避开 PyTorch，省 ~4GB 镜像）"
  fi

  # 2d) Dockerfile.api：pip 走国内镜像
  if ! grep -q -- '--index-url' docker/Dockerfile.api; then
    sed -i "s#^RUN pip install --prefix=/install \.\$#RUN pip install --prefix=/install --index-url ${PIP_INDEX} --trusted-host ${PIP_HOST} .#" docker/Dockerfile.api
    echo "  - Dockerfile.api: pip 源 -> ${PIP_INDEX}"
  fi

  # 2e) Dockerfile.web：npm 走国内镜像
  if ! grep -q 'NPM_REGISTRY' docker/Dockerfile.web; then
    sed -i "s#^RUN if \[ -f package-lock.json \]; then npm ci; else npm install; fi\$#ARG NPM_REGISTRY=${NPM_REGISTRY}\nRUN npm config set registry \${NPM_REGISTRY} \&\& if [ -f package-lock.json ]; then npm ci; else npm install; fi#" docker/Dockerfile.web
    echo "  - Dockerfile.web: npm 源 -> ${NPM_REGISTRY}"
  fi

  grep -q '"bcrypt>=4.0"' pyproject.toml || die "bcrypt 补丁未生效，请检查 pyproject.toml 格式"
  grep -q -- '--index-url' docker/Dockerfile.api || die "pip 源补丁未生效"
  grep -q 'NPM_REGISTRY' docker/Dockerfile.web || die "npm 源补丁未生效"

  # 2f) services/models.py：MEDIUMTEXT 是 MySQL 专有类型，PostgreSQL 无法渲染，
  #     会让 Base.metadata.create_all 整体失败 -> db_ready=false -> 所有接口 503。
  #     改为跨方言类型：PG 用 TEXT，MySQL 仍用 MEDIUMTEXT。
  if ! grep -q '^LongText = Text()' services/models.py; then
    sed -i '/^from sqlalchemy.dialects.mysql import MEDIUMTEXT$/a LongText = Text().with_variant(MEDIUMTEXT, "mysql")  # 部署补丁：PG 渲染为 TEXT' services/models.py
    echo "  - models.py: 新增跨方言 LongText"
  fi
  if grep -q 'Column(MEDIUMTEXT,' services/models.py; then
    sed -i 's/Column(MEDIUMTEXT,/Column(LongText,/g' services/models.py
    echo "  - models.py: MEDIUMTEXT -> LongText（4 处）"
  fi
  grep -q 'Column(MEDIUMTEXT,' services/models.py && die "models.py 仍有 MEDIUMTEXT 未替换"
  grep -q '^LongText = Text()' services/models.py || die "models.py LongText 补丁未生效"

  # 2g) db/init_pg.sql：原脚本 CREATE TABLE 存在外键顺序错误
  #     （projects 引用尚未创建的 documents），PostgreSQL 16 + ON_ERROR_STOP=1 下
  #     整个 initdb 失败回滚 -> 库里 0 张表。改为：
  #       initdb 阶段只建扩展；表结构交给应用 create_all；种子数据启动后单独灌。
  if [ ! -f db/init_pg.sql.orig ]; then
    cp db/init_pg.sql db/init_pg.sql.orig
    awk '/^INSERT INTO/{f=1} f' db/init_pg.sql.orig > db/seed_pg.sql
    cat > db/init_pg.sql <<'SQL'
-- ============================================================================
-- 部署期替换文件（原文件备份为 db/init_pg.sql.orig）
-- 原脚本的 CREATE TABLE 存在外键顺序错误：projects 引用了尚未创建的 documents，
-- 在 PostgreSQL 16 + ON_ERROR_STOP=1 下会让 docker-entrypoint-initdb.d 整体失败，
-- 数据库最终 0 张表。
--
-- 本部署改为三段式：
--   1) initdb 阶段（本文件）：只建扩展，保证 postgres 容器首次初始化成功
--   2) 表结构：由 api 启动时 SQLAlchemy Base.metadata.create_all 生成（与 models.py 同步）
--   3) 种子数据：api 健康后执行 db/seed_pg.sql（RBAC 角色/权限、管理员、Agent 配置）
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
SELECT 'BidMaster Pro bootstrap OK (tables via create_all, seeds via seed_pg.sql)' AS message;
SQL
    echo "  - db/init_pg.sql 改为安全引导；种子抽取到 db/seed_pg.sql（$(wc -l < db/seed_pg.sql) 行）"
  fi
  [ -s db/seed_pg.sql ] || die "db/seed_pg.sql 为空，种子抽取失败"

  # 2h) db/seed_pg.sql：rbac_role_permissions.id 由 '00000000-0000-0000-rp-<role>-' || p.code
  #     拼接而成，长度会超过 models.py 里的 String(36)，报
  #     "value too long for type character varying(36)"。改为 md5() 定长 32 位，
  #     仍然确定性且唯一（另有 ON CONFLICT (role_id, permission_id) 兜底）。
  if grep -q "'00000000-0000-0000-rp-" db/seed_pg.sql; then
    sed -i "s/'00000000-0000-0000-rp-\([A-Za-z_]*\)-' || p\.code/md5('rp-\1-' || p.code)/g" db/seed_pg.sql
    echo "  - seed_pg.sql: role_permissions.id 改 md5() 定长（原拼接超出 varchar(36)）"
  fi
  grep -q "'00000000-0000-0000-rp-" db/seed_pg.sql && die "seed_pg.sql 的超长 id 未修正"

  # 2i) core/rag_engine/embedder.py：键名不匹配 bug。
  #     services/routers/knowledge.py:38 传的是 "model"，而 Embedder 读的是 "model_name"，
  #     导致 BMP_EMBEDDING_MODEL 被静默忽略、永远用默认的 text-embedding-v3。
  if grep -q 'config.get("model_name", "text-embedding-v3")' core/rag_engine/embedder.py; then
    sed -i 's/config\.get("model_name", "text-embedding-v3")/config.get("model_name") or config.get("model") or "text-embedding-v3"/' core/rag_engine/embedder.py
    echo "  - embedder.py: 兼容 model/model_name 两种键（原 BMP_EMBEDDING_MODEL 被忽略）"
  fi

  # 2j) services/generate/skills/knowledge_assist_skill.py：裸 Embedder()/VectorStore()
  #     不带任何配置 -> api_key 为空、chroma 路径错，全局知识检索必然失败（仅告警降级）。
  if grep -q '^            embedder = Embedder()$' services/generate/skills/knowledge_assist_skill.py; then
    sed -i 's/^            vector_store = VectorStore()$/            from core.settings import get_settings as _get_settings\n            _st = _get_settings()\n            vector_store = VectorStore(persist_dir=_st.chroma_dir)/' services/generate/skills/knowledge_assist_skill.py
    sed -i 's/^            embedder = Embedder()$/            embedder = Embedder({"mode": _st.embedding_mode, "model_name": _st.embedding_model, "api_key": _st.embedding_api_key, "api_base": _st.embedding_api_base})/' services/generate/skills/knowledge_assist_skill.py
    echo "  - knowledge_assist_skill.py: Embedder/VectorStore 改为读取 settings"
  fi
  grep -q '^            embedder = Embedder()$' services/generate/skills/knowledge_assist_skill.py && die "knowledge_assist_skill.py 补丁未生效"

  # 2k) services/models.py：时间戳 tz-aware / naive 混用，PostgreSQL 下所有 INSERT 都会失败。
  #     列类型是 DateTime（= TIMESTAMP WITHOUT TIME ZONE），但
  #       created_at default = datetime.now(timezone.utc)   -> tz-aware
  #       updated_at default = datetime.now                 -> naive 本地时间
  #     asyncpg 编码 aware 值到 naive 列时报
  #       "can't subtract offset-naive and offset-aware datetimes"
  #     统一改为 naive UTC；列类型不变，无需重建表。
  if grep -q 'datetime\.now(timezone\.utc)' services/models.py; then
    sed -i 's/default=lambda: datetime\.now(timezone\.utc)/default=_naive_utcnow/g' services/models.py
    sed -i 's/onupdate=lambda: datetime\.now(timezone\.utc)/onupdate=_naive_utcnow/g' services/models.py
    sed -i 's/default=datetime\.now,/default=_naive_utcnow,/g' services/models.py
    echo "  - models.py: 时间戳默认值统一为 naive UTC"
  fi
  if ! grep -q '^def _naive_utcnow' services/models.py; then
    sed -i '/^def _uuid_default():/i def _naive_utcnow():\n    """部署补丁：列为 TIMESTAMP WITHOUT TIME ZONE，asyncpg 拒绝 tz-aware 值，统一 naive UTC"""\n    return datetime.now(timezone.utc).replace(tzinfo=None)\n\n' services/models.py
    echo "  - models.py: 新增 _naive_utcnow() 帮助函数"
  fi
  grep -Eq 'default=(lambda: datetime\.now\(timezone\.utc\)|datetime\.now,)' services/models.py \
    && die "models.py 仍有未统一的时间戳默认值"

  # 2l) services/routers/api_key.py：同样往 naive 列写 aware 值（expires_at/last_used_at/updated_at）。
  #     先做全局替换、再插入帮助函数，避免替换掉函数体自身造成递归。
  if grep -q 'datetime\.now(timezone\.utc)' services/routers/api_key.py; then
    sed -i 's/datetime\.now(timezone\.utc)/_naive_utc()/g' services/routers/api_key.py
    echo "  - api_key.py: datetime.now(timezone.utc) -> _naive_utc()"
  fi
  if ! grep -q '^def _naive_utc' services/routers/api_key.py; then
    sed -i '/^from services\.models import/a def _naive_utc():\n    """部署补丁：与 models.py 一致，写入 naive UTC（列为 TIMESTAMP WITHOUT TIME ZONE）"""\n    return datetime.now(timezone.utc).replace(tzinfo=None)\n' services/routers/api_key.py
    echo "  - api_key.py: 新增 _naive_utc() 帮助函数"
  fi
  python3 -c "import ast,sys; ast.parse(open('services/routers/api_key.py',encoding='utf-8').read())" \
    || die "api_key.py 补丁后语法错误"
  python3 -c "import ast; ast.parse(open('services/models.py',encoding='utf-8').read())" \
    || die "models.py 补丁后语法错误"

  git add -A
  if ! git diff --cached --quiet; then
    git -c user.name='deploy-bot' -c user.email='deploy@local' commit -q \
      -m "deploy(${HOST_ADDR}): 钉 chromadb<1.0、补 bcrypt、去 sentence-transformers、pip/npm 换国内源"
    git branch -f deploy/local HEAD
    echo "  - 已提交为本地补丁（分支 deploy/local）"
  else
    echo "  - 补丁已是最新，无需重复提交"
  fi
  git --no-pager diff --stat origin/main..HEAD || true
}

# ─────────────────────── 3. 写部署配置 ───────────────────────
write_config() {
  say "3/5 写入部署配置"
  cd "$REPO_DIR/docker"

  if [ -f .env ]; then
    echo "  - docker/.env 已存在，保留（不覆盖已生成的密码/密钥）"
  else
    local pg_pass minio_pass
    pg_pass="$(openssl rand -hex 16)"
    cat > .env <<EOF
# ===== BidMaster Pro 生产部署配置（192.168.50.31）=====
# 由 deploy_bidmaster.sh 生成于 $(date '+%F %T')，含密码，勿提交到仓库

# ── 应用 ──
BMP_DEBUG=false
UVICORN_WORKERS=1
BMP_DB_TYPE=postgresql
DB_WAIT_TIMEOUT=8

# ── PostgreSQL（compose 内置，仅绑定宿主机 127.0.0.1）──
POSTGRES_DB=bidmaster
POSTGRES_USER=bidmaster
POSTGRES_PASSWORD=${pg_pass}
POSTGRES_PORT=127.0.0.1:5432

# ── 端口 ──
API_PORT=127.0.0.1:${API_PORT}
WEB_PORT=${WEB_PORT}

# ── CORS ──
ALLOWED_ORIGINS=http://${HOST_ADDR}:${WEB_PORT}

# ── LLM（必填后 AI 功能才可用）──
LLM_MODEL=deepseek/deepseek-chat
LLM_API_KEY=
LLM_API_BASE=https://api.deepseek.com
LLM_FALLBACK_MODES=

# ── Embedding（知识库；api 模式）──
EMBEDDING_MODE=api
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_API_KEY=
EMBEDDING_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1

# ── MinerU OCR（扫描件解析，可选）──
MINERU_MODE=cloud
MINERU_API_KEY=
MINERU_ENDPOINT=https://mineru.net/api/v4

# ── 未启用的服务（保留变量以便日后开启 redis/minio/celery）──
REDIS_HOST=
REDIS_INTERNAL_PORT=6379
MINIO_ENDPOINT=
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=unused
MINIO_BUCKET=bidmaster
CELERY_CONCURRENCY=1
EOF
    chmod 600 .env
    echo "  - 已生成 docker/.env（权限 600，PG 密码随机生成）"
  fi

  # 修正已存在 .env 中的问题项（幂等）
  if grep -q '^MINIO_ROOT_PASSWORD=unused$' .env; then
    sed -i "s/^MINIO_ROOT_PASSWORD=unused$/MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)/" .env
    echo "  - 修正 MINIO_ROOT_PASSWORD（MinIO 要求 >=8 位，原值会导致容器崩溃重启）"
  fi
  # 基础设施端口只绑宿主机回环：Redis 无密码、MinIO 桶匿名可下载，不应暴露到内网
  for kv in "REDIS_EXTERNAL_PORT=127.0.0.1:16379" "MINIO_API_PORT=127.0.0.1:19000" "MINIO_CONSOLE_PORT=127.0.0.1:19001"; do
    key="${kv%%=*}"
    grep -q "^${key}=" .env || { echo "$kv" >> .env; echo "  - 追加 ${key}（仅绑定 127.0.0.1）"; }
  done

  # 这里必须用不带引号的 EOF，让 ${PROJECT} 与内存上限展开。
  # 曾经把项目名写死成 bidmaster —— 在 compose 项目名为 zdx 的生产机上会另起
  # 一套平行栈（全新数据卷 = 空库，且 8081/8000/5432 端口冲突），务必参数化。
  cat > docker-compose.override.yml <<EOF
# 部署期覆盖文件（由 deploy_bidmaster.sh 生成）
# 作用：1) 固定 compose 项目名；2) 按目标机内存余量收紧资源上限
name: ${PROJECT}

services:
  api:
    deploy:
      resources:
        limits:
          memory: ${API_MEM_LIMIT}
  postgres:
    deploy:
      resources:
        limits:
          memory: ${PG_MEM_LIMIT}
  web:
    deploy:
      resources:
        limits:
          memory: ${WEB_MEM_LIMIT}
EOF
  echo "  - 已生成 docker/docker-compose.override.yml（项目名 ${PROJECT}，内存 api=${API_MEM_LIMIT} pg=${PG_MEM_LIMIT} web=${WEB_MEM_LIMIT}）"
}

# ─────────────────── 4. 预拉基础镜像（走国内镜像源） ───────────────────
pull_images() {
  say "4/5 预拉基础镜像（源：${IMG_MIRROR}）"
  local img ref
  for img in $BASE_IMAGES; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      echo "  - 已存在 $img"
      continue
    fi
    # 官方镜像走 <mirror>/library/<name>，第三方镜像走 <mirror>/<namespace>/<name>
    case "$img" in
      */*) ref="${IMG_MIRROR}/${img}" ;;
      *)   ref="${IMG_MIRROR}/library/${img}" ;;
    esac
    echo "  - 拉取 $img  <- $ref"
    docker pull "$ref"
    docker tag "$ref" "$img"
    docker rmi "$ref" >/dev/null 2>&1 || true
  done
}

# ─────────────────────── 5. 构建并启动 ───────────────────────
build_up() {
  say "5/5 构建并启动服务：${SERVICES}"
  cd "$REPO_DIR/docker"
  df -h / | tail -1 | sed 's/^/  磁盘: /'
  "${COMPOSE[@]}" --profile infra --profile web up -d --build $SERVICES
  echo
  "${COMPOSE[@]}" ps
}

wait_healthy() {
  say "等待 api 健康并确认数据库就绪（最多 240s）"
  local i st health
  for i in $(seq 1 80); do
    st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${PROJECT}-api-1" 2>/dev/null || echo missing)
    if [ "$st" = "healthy" ]; then
      echo "  - api healthy（约 $((i * 3))s）"
      health=$(curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null || true)
      echo "  - /api/health -> ${health:-无响应}"
      case "$health" in
        *'"db_ready":true'*)
          echo "  - 数据库已就绪"
          fix_web_proxy
          return 0 ;;
        *)
          echo "  ! db_ready 非 true，api 最近日志："
          docker logs --tail 25 "${PROJECT}-api-1" 2>&1 | sed 's/^/    /'
          return 1 ;;
      esac
    fi
    sleep 3
  done
  echo "  ! api 未达 healthy，最近日志："
  docker logs --tail 30 "${PROJECT}-api-1" 2>&1 | sed 's/^/    /'
  return 1
}

seed_db() {
  say "灌入种子数据（RBAC / 管理员 / Agent 配置）"
  cd "$REPO_DIR"
  local n
  n=$(docker exec "${PROJECT}-postgres-1" psql -U "$PG_USER" -d "$PG_DB" -tAc \
      "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo 0)
  echo "  - 当前 public 表数量：$n"
  if [ "${n:-0}" -lt 20 ]; then
    die "表未建好（期望 >=20 张）；请检查 api 日志里的 init_db 结果"
  fi
  docker exec -i "${PROJECT}-postgres-1" psql -v ON_ERROR_STOP=1 -q -U "$PG_USER" -d "$PG_DB" < db/seed_pg.sql
  echo "  - 种子数据已灌入（脚本幂等，可重复执行）"
  docker exec "${PROJECT}-postgres-1" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select '用户数: '||count(*) from users union all select 'RBAC角色: '||count(*) from rbac_roles union all select 'RBAC权限: '||count(*) from rbac_permissions union all select 'Agent配置: '||count(*) from agent_configs" \
    | sed 's/^/  /'
}

# ─────────── 5b. 数据库迁移（db/migrations/*.sql，按文件名顺序） ───────────
# 为什么不能只靠启动时的 Base.metadata.create_all：它只建「不存在的表」，
# **不会给已存在的表补列**。v0.4.0 给 users 加了 is_active / last_login_at，
# 老库不 ALTER 的话，ORM 一 select(User) 就会报 column does not exist -> 全站 500。
# 所以 migrate 必须排在 build_up 之后、wait_healthy 之前。
wait_pg() {
  local i
  for i in $(seq 1 30); do
    if docker exec "${PROJECT}-postgres-1" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
      echo "  - postgres 已就绪"
      return 0
    fi
    sleep 2
  done
  die "postgres 探活 30 次仍未就绪"
}

migrate_db() {
  say "执行数据库迁移（db/migrations/*.sql）"
  cd "$REPO_DIR"
  if ! ls db/migrations/*.sql >/dev/null 2>&1; then
    echo "  - 没有迁移脚本，跳过"
    return 0
  fi
  wait_pg
  local f
  for f in $(ls db/migrations/*.sql | sort); do
    echo "  - 应用 $(basename "$f")"
    docker exec -i "${PROJECT}-postgres-1" \
      psql -v ON_ERROR_STOP=1 -q -U "$PG_USER" -d "$PG_DB" < "$f" | sed 's/^/    /'
  done
  echo "  - 迁移完成（脚本全部幂等，可重复执行）"
}

# ─────────── 6. 配置 LLM 网关（复用内网 Key，不落盘到脚本/文档） ───────────
set_env() { # set_env <file> <KEY> <VALUE>
  local f="$1" k="$2" v="$3"
  if grep -q "^${k}=" "$f"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "$f"
  else
    echo "${k}=${v}" >> "$f"
  fi
}

config_llm() {
  say "配置 LLM / Embedding 网关"
  [ -f "$LLM_SRC_ENV" ] || die "找不到密钥来源文件：$LLM_SRC_ENV"
  local key
  key=$(grep -E '^YIBIAO_AI_API_KEY=' "$LLM_SRC_ENV" | head -1 | cut -d= -f2- | tr -d "\r\"' ")
  [ -n "$key" ] || die "YIBIAO_AI_API_KEY 为空"
  echo "  - 已从 $(basename "$LLM_SRC_ENV") 读取 API Key（长度 ${#key}，不回显）"
  cd "$REPO_DIR/docker"
  set_env .env LLM_API_KEY        "$key"
  set_env .env LLM_API_BASE       "$GW_BASE"
  set_env .env LLM_MODEL          "$GW_TEXT_MODEL"
  set_env .env LLM_FALLBACK_MODES "$GW_FALLBACK_MODEL"
  set_env .env EMBEDDING_MODE     "api"
  set_env .env EMBEDDING_MODEL    "$GW_EMBED_MODEL"
  set_env .env EMBEDDING_API_KEY  "$key"
  set_env .env EMBEDDING_API_BASE "$GW_BASE"
  chmod 600 .env
  echo "  - 文本模型   : $GW_TEXT_MODEL"
  echo "  - 降级模型   : $GW_FALLBACK_MODEL"
  echo "  - 向量模型   : $GW_EMBED_MODEL"
  echo "  - 网关地址   : $GW_BASE"
}

verify_llm() {
  say "验证 LLM / Embedding 通路（走应用自身代码）"
  # 用 if 包裹以绕过 set -e，失败时给出明确提示
  if docker exec -i "${PROJECT}-api-1" python - <<'PY'
import asyncio
from core.settings import get_settings
from services.llm_factory import get_llm_gateway
from core.rag_engine.embedder import Embedder

s = get_settings()
print(f"  settings: model={s.llm_default_model} base={s.llm_api_base} key_len={len(s.llm_api_key)}")
print(f"  embedding: mode={s.embedding_mode} model={s.embedding_model} base={s.embedding_api_base}")

g = get_llm_gateway()
out = asyncio.run(g.chat([{"role": "user", "content": "只回复两个字：正常"}], max_tokens=64))
print("  chat  ->", repr(out)[:100])

# 故意用 knowledge.py 的 "model" 键，验证 embedder 键名补丁生效
e = Embedder({"mode": s.embedding_mode, "model": s.embedding_model,
              "api_key": s.embedding_api_key, "api_base": s.embedding_api_base})
print("  embedder.model_name ->", e.model_name)
v = asyncio.run(e.embed(["招标文件测试"]))
print("  embed -> 向量维度:", len(v[0]))
PY
  then
    echo "  - LLM 与 Embedding 通路均正常"
  else
    die "LLM 通路验证失败，请检查 docker logs ${PROJECT}-api-1 与网关连通性"
  fi
}

down() {
  say "停止服务（保留数据卷）"
  cd "$REPO_DIR/docker"
  "${COMPOSE[@]}" --profile infra --profile web down
}

# nginx.conf 里是 `proxy_pass http://api:8000`（静态主机名），只在 nginx 启动时解析一次。
# api 容器被重建后 IP 会变，而 web 容器若沿用旧进程就会一直 502。
# 这里在 api 健康后探一次反代，失败则重启 web 让 nginx 重新解析。
fix_web_proxy() {
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/api/health" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "  - nginx 反代 ${WEB_BASE}/api 正常"
    return 0
  fi
  echo "  ! nginx 反代 ${WEB_BASE}/api 返回 ${code}，重启 web 以重新解析 api 地址 ..."
  ( cd "$REPO_DIR/docker" && "${COMPOSE[@]}" restart web >/dev/null 2>&1 )
  sleep 6
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/api/health" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then echo "  - nginx 反代 ${WEB_BASE}/api 已恢复"; else echo "  ! 反代仍返回 ${code}"; return 1; fi
}

case "${1:-all}" in
  code) fetch_code; patch_repo; write_config ;;
  pull) pull_images ;;
  up)   build_up ;;
  migrate) migrate_db ;;
  seed) wait_healthy; seed_db ;;
  llm)  config_llm ;;
  verify-llm) verify_llm ;;
  down) down ;;
  all)  fetch_code; patch_repo; write_config; config_llm; pull_images; build_up; migrate_db; wait_healthy; seed_db; verify_llm ;;
  *)    die "未知参数：$1（可用：all|code|pull|up|migrate|seed|llm|verify-llm|down）" ;;
esac

say "脚本执行完毕"
echo "  API  : http://${HOST_ADDR}:${API_PORT}  (文档 /docs)"
echo "  Web  : http://${HOST_ADDR}:${WEB_PORT}${WEB_BASE}/"
echo "  验证 : bash verify_bidmaster.sh"
