#!/usr/bin/env bash
# ============================================================================
# BidMaster Pro — 部署验证脚本（在 192.168.50.31 上执行）
# 用法： bash verify_bidmaster.sh
# ============================================================================
set -uo pipefail

HOST_ADDR="${HOST_ADDR:-192.168.50.31}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-8081}"
# 生产只发布 /zdx 子路径（见 docker/nginx.conf），根路径与 /api 一律 404。
WEB_BASE="${WEB_BASE:-/zdx}"
PROJECT="${PROJECT:-bidmaster}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@bidmaster.pro}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"

ok()   { printf '  \033[1;32m[OK]\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*"; }
info() { printf '  \033[1;36m[..]\033[0m   %s\n' "$*"; }
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

FAIL=0

say "1. 容器状态"
docker compose -p "$PROJECT" ps 2>/dev/null || docker ps --filter "label=com.docker.compose.project=$PROJECT"
for c in api postgres web; do
  name="${PROJECT}-${c}-1"
  st=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)
  if [ "$st" = "running" ]; then
    h=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)
    ok "$name running (health=$h)"
  else
    bad "$name 状态=$st"; FAIL=1
  fi
done

say "2. API 健康检查（等待最多 180s）"
HEALTH=""
for i in $(seq 1 60); do
  HEALTH=$(curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null || true)
  [ -n "$HEALTH" ] && break
  sleep 3
done
if [ -n "$HEALTH" ]; then
  ok "/api/health -> $HEALTH"
  case "$HEALTH" in
    *'"db_ready":true'*) ok "数据库已就绪 (db_ready=true)" ;;
    *) bad "db_ready 非 true，数据库未就绪"; FAIL=1 ;;
  esac
else
  bad "/api/health 无响应"; FAIL=1
  info "最近日志："; docker logs --tail 40 "${PROJECT}-api-1" 2>&1 | sed 's/^/      /'
fi

say "3. Web 前端（nginx）"
CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then ok "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/ -> 200"; else bad "首页返回 $CODE"; FAIL=1; fi

CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/api/health" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then ok "nginx 反代 ${WEB_BASE}/api -> 200"; else bad "nginx 反代 ${WEB_BASE}/api 返回 $CODE"; FAIL=1; fi

CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${WEB_PORT}/" 2>/dev/null || echo 000)
if [ "$CODE" = "404" ]; then ok "根路径按设计返回 404（仅发布 ${WEB_BASE}/）"; else bad "根路径返回 $CODE，预期 404"; FAIL=1; fi

CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/admin" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then ok "SPA 回退 ${WEB_BASE}/admin -> 200"; else bad "SPA 回退 ${WEB_BASE}/admin 返回 $CODE"; FAIL=1; fi

say "4. 管理员登录（种子数据是否生效）"
LOGIN=$(curl -sS --max-time 15 -X POST "http://127.0.0.1:${WEB_PORT}${WEB_BASE}/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASS}\"}" 2>/dev/null || true)
TOKEN=$(printf '%s' "$LOGIN" | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
case "$LOGIN" in
  *'"token"'*)
    ok "登录成功：${ADMIN_EMAIL} / ${ADMIN_PASS}"
    ok "请登录后立即修改默认密码"
    ;;
  *)
    bad "登录失败：${LOGIN:-无响应}"
    info "多为种子数据未灌入（外部 PG 时需手动执行 db/init_pg.sql）"
    FAIL=1
    ;;
esac

say "4b. 管理后台使用监控（v0.4.0）"
CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${API_PORT}/api/admin/overview" 2>/dev/null || echo 000)
if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then ok "匿名访问 /api/admin/overview -> $CODE（权限守卫生效）"; else bad "匿名访问返回 $CODE，预期 401/403"; FAIL=1; fi
PERM=$(docker exec "${PROJECT}-postgres-1" psql -U bidmaster -d bidmaster -tAc "select count(*) from rbac_permissions where code='settings.monitor'" 2>/dev/null || echo 0)
if [ "${PERM:-0}" -ge 1 ] 2>/dev/null; then ok "权限码 settings.monitor 已入库"; else bad "缺少 settings.monitor 权限码，请重跑 deploy_bidmaster.sh seed"; FAIL=1; fi
if [ -n "$TOKEN" ]; then
  OV=$(curl -sS --max-time 20 -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${API_PORT}/api/admin/overview?range=7d" 2>/dev/null || true)
  case "$OV" in
    *'"presence"'*) ok "管理员可读取总览：$(printf '%s' "$OV" | head -c 200)" ;;
    *) bad "总览接口异常：${OV:-无响应}"; FAIL=1 ;;
  esac
else
  bad "未取得 token，跳过监控接口校验"; FAIL=1
fi

say "5. 数据库表与种子数据"
PG="${PROJECT}-postgres-1"
if docker exec "$PG" psql -U bidmaster -d bidmaster -tAc "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null | sed 's/^/  表数量: /'; then
  docker exec "$PG" psql -U bidmaster -d bidmaster -tAc "select '用户数: '||count(*) from users union all select 'RBAC角色数: '||count(*) from rbac_roles union all select 'Agent配置数: '||count(*) from agent_configs" 2>/dev/null | sed 's/^/  /'
else
  bad "无法连接数据库容器"; FAIL=1
fi

say "6. 资源占用"
docker stats --no-stream --format '  {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null | grep "$PROJECT" || true
df -h / | tail -1 | sed 's/^/  磁盘: /'
free -h | sed -n '2p' | sed 's/^/  内存: /'

say "7. 访问入口"
info "Web 界面 : http://${HOST_ADDR}:${WEB_PORT}${WEB_BASE}/"
info "API 文档 : http://${HOST_ADDR}:${API_PORT}/docs"
info "健康检查 : http://${HOST_ADDR}:${API_PORT}/api/health"
info "默认账号 : ${ADMIN_EMAIL} / ${ADMIN_PASS}（请立即改密）"
info "日志     : docker logs -f ${PROJECT}-api-1"

if [ -n "${LLM_KEY_HINT:-}" ]; then :; fi
if grep -q '^LLM_API_KEY=$' "$HOME/bidmaster-pro/docker/.env" 2>/dev/null; then
  say "⚠ 待办：LLM_API_KEY 为空"
  info "AI 功能（解读/生成/检查）暂不可用。填 Key 后执行："
  info "  vi ~/bidmaster-pro/docker/.env   # 填 LLM_API_KEY / EMBEDDING_API_KEY"
  info "  docker compose -p bidmaster --profile infra --profile web up -d api"
fi

say "验证结果"
if [ "$FAIL" = "0" ]; then ok "全部通过"; else bad "存在失败项，见上"; fi
exit "$FAIL"
