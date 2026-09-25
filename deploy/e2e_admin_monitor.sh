#!/usr/bin/env bash
# ============================================================================
# v0.4.0 管理后台使用监控 —— 端到端联调（在 192.168.50.31 上跑）
# 走 nginx 的 /zdx 子路径，顺带验证反代前缀剥离后行为归属是否正确。
# 会创建一个临时检查员账号，结束时自动清理。
# ============================================================================
set -uo pipefail
WEB="${WEB:-http://127.0.0.1:8081/zdx}"
API="${API:-http://127.0.0.1:8000}"
PG="${PG:-bidmaster-postgres-1}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@bidmaster.pro}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
CK_EMAIL="monitor-smoke@bidmaster.pro"
CK_PASS="Monitor@2026"
CK_NAME="监控联调检查员"
PASS=0; FAIL=0
ok()  { printf '  \033[1;32m[OK]\033[0m   %s\n' "$*"; PASS=$((PASS+1)); }
bad() { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
note(){ printf '  \033[1;36m[..]\033[0m   %s\n' "$*"; }
hd()  { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

jpath() { # jpath <json> <dotted.path>
  printf '%s' "$1" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
cur = d
for k in sys.argv[1].split("."):
    if not k: continue
    if isinstance(cur, list):
        try: cur = cur[int(k)]
        except Exception: cur = None; break
    elif isinstance(cur, dict):
        cur = cur.get(k)
    else:
        cur = None; break
if cur is None: print("")
elif isinstance(cur, (dict, list)): print(json.dumps(cur, ensure_ascii=False))
else: print(cur)
' "$2"
}

login() { # login <email> <pass> -> 打印 token
  local body
  body=$(curl -sS --max-time 20 -X POST "$WEB/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}" 2>/dev/null)
  jpath "$body" token | tr -d '"'
}

CK_ID=""; SYNTH_ID=""; PID=""
cleanup() {
  hd "清理联调数据"
  if [ -n "$SYNTH_ID" ]; then
    docker exec "$PG" psql -U bidmaster -d bidmaster -q -c \
      "delete from user_activity_log where id='$SYNTH_ID'" >/dev/null 2>&1 && ok "删除 synthetic 流水行" || note "synthetic 行已不存在"
  fi
  if [ -n "$CK_ID" ]; then
    docker exec "$PG" psql -U bidmaster -d bidmaster -q -c \
      "delete from user_quotas where user_id='$CK_ID'" >/dev/null 2>&1
    [ -n "$PID" ] && docker exec "$PG" psql -U bidmaster -d bidmaster -q -c \
      "delete from projects where id='$PID'" >/dev/null 2>&1
    if [ -n "$TOKEN_A" ]; then
      curl -sS -o /dev/null --max-time 15 -X DELETE -H "Authorization: Bearer $TOKEN_A" "$API/api/rbac/users/$CK_ID" 2>/dev/null
    fi
    ok "已删除临时账号 $CK_EMAIL 与其联调项目"
  fi
}
trap cleanup EXIT

hd "1. 管理员登录（走 nginx /zdx 反代）"
TOKEN_A=$(login "$ADMIN_EMAIL" "$ADMIN_PASS")
[ -n "$TOKEN_A" ] && ok "登录成功，token 长度 ${#TOKEN_A}" || { bad "管理员登录失败"; exit 1; }
AUTH_A="Authorization: Bearer ${TOKEN_A}"

hd "2. 匿名访问必须 401"
for ep in overview presence users activities tokens alerts quotas tasks; do
  C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$WEB/api/admin/$ep")
  [ "$C" = "401" ] && ok "匿名 /api/admin/$ep -> 401" || bad "匿名 /api/admin/$ep -> $C（预期 401）"
done

hd "3. 管理员逐个端点（含返回结构）"
probe() { # probe <label> <path> <顶层键...>
  local label="$1" path="$2"; shift 2
  local body code
  body=$(curl -sS --max-time 30 -H "$AUTH_A" -w '\n%{http_code}' "$WEB$path")
  code=$(printf '%s' "$body" | tail -1); body=$(printf '%s' "$body" | sed '$d')
  if [ "$code" != "200" ]; then bad "$label -> HTTP $code : $(printf '%s' "$body" | head -c 150)"; return; fi
  local k miss=""
  for k in "$@"; do printf '%s' "$body" | grep -q "\"$k\"" || miss="$miss $k"; done
  if [ -n "$miss" ]; then bad "$label -> 200 但缺字段:$miss"; else ok "$label -> 200  $(printf '%s' "$body" | head -c 120)"; fi
}

probe_raw() { # probe_raw <label> <path> <子串...>；CSV 表头没有引号，单独比对
  local label="$1" path="$2"; shift 2
  local body code k miss=""
  body=$(curl -sS --max-time 30 -H "$AUTH_A" -w '\n%{http_code}' "$WEB$path")
  code=$(printf '%s' "$body" | tail -1); body=$(printf '%s' "$body" | sed '$d')
  if [ "$code" != "200" ]; then bad "$label -> HTTP $code"; return; fi
  for k in "$@"; do printf '%s' "$body" | grep -q "$k" || miss="$miss $k"; done
  if [ -n "$miss" ]; then bad "$label -> 200 但缺表头:$miss"; else ok "$label -> 200  $(printf '%s' "$body" | head -1 | head -c 110)"; fi
}
probe "总览"        "/api/admin/overview?range=7d"                    range timezone users presence today
probe "实时在线"    "/api/admin/presence?include_offline=true"        online busy running_tasks users
probe "在途任务"    "/api/admin/tasks"                                count tasks
probe "用户用量"    "/api/admin/users?range=7d&page=1&page_size=20"   range total page users
probe "行为流水"    "/api/admin/activities?range=24h&limit=20"        range total actions activities
probe "Token/人"    "/api/admin/tokens?range=7d&group_by=user"        group_by totals items
probe "Token/模型"  "/api/admin/tokens?range=7d&group_by=model"       group_by totals items
probe "Token/动作"  "/api/admin/tokens?range=7d&group_by=action"      group_by totals items
probe "风险告警"    "/api/admin/alerts?range=24h"                     range count alerts
probe "配额治理"    "/api/admin/quotas"                               defaults quotas
probe_raw "CSV/用户" "/api/admin/export?what=users&range=7d"           姓名 邮箱 当前状态
probe_raw "CSV/流水" "/api/admin/export?what=activities&range=24h"     时间 用户 项目名

hd "4. 时间范围参数"
for r in today 24h 7d 30d; do
  C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H "$AUTH_A" "$WEB/api/admin/overview?range=$r")
  [ "$C" = "200" ] && ok "range=$r -> 200" || bad "range=$r -> $C"
done
C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H "$AUTH_A" "$WEB/api/admin/overview?range=bogus")
case "$C" in
  200)      ok "非法 range -> 200（容错回落到默认区间，未 5xx）" ;;
  400|422)  ok "非法 range -> $C（参数校验生效）" ;;
  *)        bad "非法 range -> $C（既没容错也没校验，还可能是 5xx）" ;;
esac

hd "5. 建一个临时标书检查员账号"
docker exec "$PG" psql -U bidmaster -d bidmaster -q -c "delete from users where email='$CK_EMAIL'" >/dev/null 2>&1
CK_CREATE=$(curl -sS --max-time 20 -X POST "$API/api/rbac/users" -H "$AUTH_A" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$CK_EMAIL\",\"name\":\"$CK_NAME\",\"password\":\"$CK_PASS\",\"role_name\":\"bid_checker\"}")
CK_ID=$(jpath "$CK_CREATE" id)
[ -n "$CK_ID" ] && ok "创建成功 id=$CK_ID（RBAC 角色 bid_checker）" || { bad "创建失败：$CK_CREATE"; exit 1; }

hd "6. 检查员不能看监控（403，而不是 401）"
TOKEN_C=$(login "$CK_EMAIL" "$CK_PASS")
[ -n "$TOKEN_C" ] && ok "检查员登录成功" || { bad "检查员登录失败"; exit 1; }
AUTH_C="Authorization: Bearer ${TOKEN_C}"
for ep in overview presence users activities tokens alerts quotas; do
  C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "$AUTH_C" "$WEB/api/admin/$ep")
  [ "$C" = "403" ] && ok "检查员 /api/admin/$ep -> 403" || bad "检查员 /api/admin/$ep -> $C（预期 403）"
done
ME=$(curl -sS --max-time 15 -H "$AUTH_C" "$WEB/api/auth/sessions")
printf '%s' "$ME" | grep -q '"token"' && ok "检查员可查自己的登录会话（user_sessions 落库生效）" || note "/api/auth/sessions 返回：$(printf '%s' "$ME" | head -c 120)"

hd "7. 检查员干活：建项目（走 /zdx 反代）"
PNAME="监控联调项目_$(date +%H%M%S)"
# create_project 的 name 是查询参数（同签名里还有 File(None)，FastAPI 不会把它当表单字段）
PR=$(curl -sS --max-time 25 -H "$AUTH_C" -X POST -G --data-urlencode "name=$PNAME" "$WEB/api/projects/")
PID=$(jpath "$PR" project_id)
[ -n "$PID" ] && ok "创建项目成功：$PNAME (id=$PID)" || bad "创建项目失败：$PR"
sleep 2

hd "8. 管理员能看到这条行为，且归属正确"
ACT=$(curl -sS --max-time 25 -H "$AUTH_A" "$WEB/api/admin/activities?range=24h&limit=50")
HIT=$(printf '%s' "$ACT" | python3 -c '
import sys, json
d = json.load(sys.stdin)
me = [a for a in d.get("activities", []) if a.get("action") == "project.create" and a.get("user_email") == sys.argv[1]]
print(json.dumps(me[0], ensure_ascii=False) if me else "")
' "$CK_EMAIL")
if [ -n "$HIT" ]; then
  ok "行为流水里有 $CK_EMAIL 的 project.create"
  note "  归属：$(jpath "$HIT" user_name) / status=$(jpath "$HIT" status) / 耗时=$(jpath "$HIT" duration_ms)ms / IP=$(jpath "$HIT" client_ip)"
  note "  detail：$(jpath "$HIT" detail)"
else
  bad "行为流水里找不到该检查员的 project.create"
fi
PRES=$(curl -sS --max-time 25 -H "$AUTH_A" "$WEB/api/admin/presence?include_offline=true")
printf '%s' "$PRES" | grep -q "$CK_ID" && ok "实时在线列表包含该检查员" || bad "实时在线列表缺少该检查员"
UL=$(curl -sS --max-time 25 -H "$AUTH_A" "$WEB/api/admin/users?range=24h&q=monitor-smoke")
UTOT=$(jpath "$UL" total)
[ "${UTOT:-0}" -ge 1 ] 2>/dev/null && ok "用户用量搜索命中 total=$UTOT" || bad "用户用量搜索未命中（total=$UTOT）"
SUM=$(curl -sS --max-time 25 -H "$AUTH_A" "$WEB/api/admin/users/$CK_ID/summary?range=24h")
printf '%s' "$SUM" | grep -q '"user"' && ok "单人详情 -> 200  $(printf '%s' "$SUM" | head -c 130)" || bad "单人详情异常：$(printf '%s' "$SUM" | head -c 150)"

hd "8b. 项目名在写入时补齐（中间件只看得到 project_id）"
if [ -n "$PID" ]; then
  # compliance 在「招/投标文件正文为空」时会先 400，压根不碰 LLM，正好零成本走完
  # 鉴权 -> 中间件 -> 落库整条链路。注意不能用不存在的路由：404 不跑鉴权依赖，
  # 中间件拿不到身份（scope.state.bmp_user 为空）就不记，这是设计如此。
  CBODY=$(curl -sS --max-time 25 -H "$AUTH_C" -X POST -w '\n%{http_code}' "$WEB/api/check/$PID/compliance")
  CCODE=$(printf '%s' "$CBODY" | tail -1)
  if [ "$CCODE" = "400" ]; then ok "无正文时 compliance -> 400（未触发 LLM）"; else note "compliance 返回 $CCODE：$(printf '%s' "$CBODY" | head -1 | head -c 120)"; fi
  sleep 2
  PN=$(docker exec "$PG" psql -U bidmaster -d bidmaster -tAc \
    "select project_name from user_activity_log where project_id='$PID' and project_name is not null order by created_at desc limit 1")
  if [ "$PN" = "$PNAME" ]; then ok "流水里的项目名已补齐：$PN"; else bad "项目名未补齐（得到 '$PN'，期望 '$PNAME'）"; fi
  AN=$(docker exec "$PG" psql -U bidmaster -d bidmaster -tAc \
    "select action || '/' || status from user_activity_log where project_id='$PID' order by created_at desc limit 1")
  note "该条流水：$AN（4xx 记为 rejected，与系统故障分开算）"
else
  bad "没有项目 id，跳过项目名补齐验证"
fi

hd "9. 配额治理：设限 -> 触发 429"
Q=$(curl -sS --max-time 20 -X PUT -H "$AUTH_A" -H 'Content-Type: application/json' \
  -d '{"daily_action_limit":1,"daily_token_limit":0,"note":"联调临时限额"}' \
  "$WEB/api/admin/users/$CK_ID/quota")
printf '%s' "$Q" | grep -q '"daily_action_limit"' && ok "设置配额成功：$(printf '%s' "$Q" | head -c 120)" || bad "设置配额失败：$Q"
QL=$(curl -sS --max-time 20 -H "$AUTH_A" "$WEB/api/admin/quotas")
printf '%s' "$QL" | python3 -c '
import sys, json
d = json.load(sys.stdin)
row = [q for q in d.get("quotas", []) if q.get("user_id") == sys.argv[1]]
print("FOUND" if row and row[0].get("daily_action_limit") == 1 else "MISSING")
' "$CK_ID" | grep -q FOUND && ok "配额列表回显 daily_action_limit=1" || bad "配额列表未回显新限额"
# 造一条已计入的 check.full 流水，把当日「投标检查」用量顶到上限
SYNTH_ID=$(docker exec "$PG" psql -U bidmaster -d bidmaster -tAc \
  "insert into user_activity_log (id,user_id,user_email,user_name,action,status,created_at,finished_at,detail) \
   values (md5(random()::text), '$CK_ID', '$CK_EMAIL', '$CK_NAME', 'check.full', 'success', \
           (now() at time zone 'utc'), (now() at time zone 'utc'), '{}'::json) returning id")
[ -n "$SYNTH_ID" ] && ok "已注入 1 条当日 check.full 用量（用完即删）" || bad "注入用量失败"
if [ -n "$PID" ]; then
  C429=$(curl -sS --max-time 25 -o /tmp/q429.json -w '%{http_code}' -H "$AUTH_C" -X POST "$WEB/api/check/$PID/full-check")
  if [ "$C429" = "429" ]; then ok "超额后 POST full-check -> 429：$(head -c 140 /tmp/q429.json)"; else bad "预期 429，实际 $C429：$(head -c 160 /tmp/q429.json)"; fi
  CGET=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -H "$AUTH_C" "$WEB/api/projects/$PID")
  [ "$CGET" = "200" ] && ok "GET 轮询不受配额影响 -> 200" || bad "GET 被限流了 -> $CGET（配额只该管写操作）"
else
  bad "没有项目 id，跳过配额限流验证"
fi

hd "10. 强制下线"
FL=$(curl -sS --max-time 20 -X POST -H "$AUTH_A" -H 'Content-Type: application/json' -d '{}' "$WEB/api/admin/users/$CK_ID/force-logout")
printf '%s' "$FL" | grep -qi 'revoked\|ok\|count' && ok "强制下线接口 -> $(printf '%s' "$FL" | head -c 120)" || note "强制下线返回：$(printf '%s' "$FL" | head -c 150)"
sleep 1
C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "$AUTH_C" "$WEB/api/auth/me")
[ "$C" = "401" ] && ok "被踢后旧 token 立刻失效 -> 401" || bad "被踢后旧 token 仍可用 -> $C"

hd "11. 禁用 / 启用账号"
TOKEN_C=$(login "$CK_EMAIL" "$CK_PASS"); AUTH_C="Authorization: Bearer ${TOKEN_C}"
[ -n "$TOKEN_C" ] && ok "重新登录成功" || bad "重新登录失败"
ST=$(curl -sS --max-time 20 -X PUT -H "$AUTH_A" -H 'Content-Type: application/json' -d '{"is_active":false}' "$WEB/api/admin/users/$CK_ID/status")
ok "禁用接口 -> $(printf '%s' "$ST" | head -c 120)"
sleep 1
# 回归：禁用成功必须记为 success。曾被错记成 rejected（语义是 4xx 被拒），
# 会把一次正常封号显示成「被拒」，还会稀释失败率分母。
SACT=$(docker exec "$PG" psql -U bidmaster -d bidmaster -tAc \
  "select status from user_activity_log where action='admin.set_active' and resource_id='$CK_ID' order by created_at desc limit 1")
[ "$SACT" = "success" ] && ok "禁用账号在流水里记为 success（不是 rejected）" || bad "禁用账号被记成 '$SACT'，预期 success"
C=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "$AUTH_C" "$WEB/api/auth/me")
[ "$C" = "403" ] || [ "$C" = "401" ] && ok "禁用后持旧 token 访问 -> $C（被挡住）" || bad "禁用后仍可访问 -> $C"
L=$(curl -sS --max-time 20 -X POST "$WEB/api/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$CK_EMAIL\",\"password\":\"$CK_PASS\"}")
printf '%s' "$L" | grep -q '"token"' && bad "禁用后仍能登录！" || ok "禁用后登录被拒：$(printf '%s' "$L" | head -c 100)"
curl -sS -o /dev/null --max-time 20 -X PUT -H "$AUTH_A" -H 'Content-Type: application/json' -d '{"is_active":true}' "$WEB/api/admin/users/$CK_ID/status"
TOKEN_C=$(login "$CK_EMAIL" "$CK_PASS")
[ -n "$TOKEN_C" ] && ok "启用后可重新登录（恢复成功）" || bad "启用后仍无法登录"

hd "12. 数据落库核对"
docker exec "$PG" psql -U bidmaster -d bidmaster -c \
  "select action, status, count(*) from user_activity_log group by action, status order by count(*) desc limit 12"
docker exec "$PG" psql -U bidmaster -d bidmaster -c \
  "select (select count(*) from user_sessions where revoked_at is null) as 活跃会话, \
          (select count(*) from user_sessions where revoked_at is not null) as 已吊销, \
          (select count(*) from llm_usage_log) as token流水, \
          (select count(*) from user_quotas) as 自定义配额"
docker exec "$PG" psql -U bidmaster -d bidmaster -c \
  "select action, project_id, project_name, resource_name from user_activity_log where project_id is not null order by created_at desc limit 5"

hd "13. api 日志异常扫描"
ERR=$(docker logs --tail 600 bidmaster-api-1 2>&1 | grep -Ei 'traceback|error|exception' | grep -vi 'error_message\|errors=' | tail -8)
[ -z "$ERR" ] && ok "最近 600 行日志无 error/traceback" || { bad "日志有异常："; printf '%s\n' "$ERR" | sed 's/^/      /'; }

printf '\n\033[1m结果：通过 %d 项，失败 %d 项\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
