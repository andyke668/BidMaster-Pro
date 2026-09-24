-- ============================================================================
-- 001 · 管理后台使用监控（v0.4.0）
-- ============================================================================
-- 为什么需要手工迁移：本项目的 alembic 不可用（README「已知问题」已记录），
-- 建表全靠启动时 SQLAlchemy Base.metadata.create_all —— 它只创建「不存在的表」，
-- **不会给已存在的表补列**。所以 users 表的两个新列必须在这里手工 ALTER，
-- 否则老库升级后 is_active / last_login_at 恒为 NULL。
--
-- 四张新表 create_all 也会建；这里同时写出来是为了：
--   ① 脚本能脱离应用独立执行（部署时先迁移再重启，避免半启动状态）；
--   ② 显式声明复合索引，与 models.py 的 __table_args__ 保持一致。
--
-- 幂等：全部 IF NOT EXISTS，可重复执行。
-- 执行：bash deploy_bidmaster.sh migrate   （见 开发/deploy/deploy_bidmaster.sh）
-- ============================================================================

BEGIN;

-- ── 1. users 表补列 ────────────────────────────────────────────────────────
-- is_active 必须带 DEFAULT TRUE：已有用户行要落定为「启用」，
-- 否则升级完所有人都是 NULL，虽然代码里判活写的是 `is False`（NULL 视为启用）
-- 不会锁死，但管理后台的「已禁用」筛选会算不准。
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP;

-- ── 2. 登录会话（登录态从 auth.py 进程内字典迁到这里）────────────────────────
CREATE TABLE IF NOT EXISTS user_sessions (
    id           VARCHAR(36)  PRIMARY KEY,
    token_hash   VARCHAR(64)  NOT NULL UNIQUE,   -- sha256(token)，明文只在登录响应出现一次
    user_id      VARCHAR(36)  NOT NULL REFERENCES users(id),
    client_ip    VARCHAR(64),
    user_agent   VARCHAR(256),
    created_at   TIMESTAMP,
    last_seen_at TIMESTAMP,                      -- 在线判定的数据来源，心跳节流刷新
    expires_at   TIMESTAMP    NOT NULL,
    revoked_at   TIMESTAMP                       -- 退出登录 / 改密 / 管理员强制下线
);
CREATE INDEX IF NOT EXISTS ix_user_sessions_token_hash   ON user_sessions (token_hash);
CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id      ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_user_sessions_last_seen_at ON user_sessions (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_user_sessions_revoked_at   ON user_sessions (revoked_at);
CREATE INDEX IF NOT EXISTS ix_usession_user_seen         ON user_sessions (user_id, last_seen_at);

-- ── 3. 用户行为流水（管理后台「使用情况」的唯一数据源）───────────────────────
-- user_id 刻意不建外键：审计日志要在用户被删除后依然可查，故冗余 email / name。
CREATE TABLE IF NOT EXISTS user_activity_log (
    id            VARCHAR(36)  PRIMARY KEY,
    user_id       VARCHAR(36),
    user_email    VARCHAR(255),
    user_name     VARCHAR(100),
    action        VARCHAR(64)  NOT NULL,
    resource_type VARCHAR(32),
    resource_id   VARCHAR(64),
    resource_name VARCHAR(500),
    project_id    VARCHAR(36),
    project_name  VARCHAR(200),
    detail        JSON,
    status        VARCHAR(16)  NOT NULL DEFAULT 'success',
    error_message TEXT,
    duration_ms   INTEGER,
    client_ip     VARCHAR(64),
    user_agent    VARCHAR(256),
    created_at    TIMESTAMP,
    finished_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_user_activity_log_user_id    ON user_activity_log (user_id);
CREATE INDEX IF NOT EXISTS ix_user_activity_log_action     ON user_activity_log (action);
CREATE INDEX IF NOT EXISTS ix_user_activity_log_project_id ON user_activity_log (project_id);
CREATE INDEX IF NOT EXISTS ix_user_activity_log_status     ON user_activity_log (status);
CREATE INDEX IF NOT EXISTS ix_user_activity_log_created_at ON user_activity_log (created_at);
CREATE INDEX IF NOT EXISTS ix_ual_user_created             ON user_activity_log (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ual_action_created           ON user_activity_log (action, created_at);

-- ── 4. LLM token 消耗流水（按人 / 按动作 / 按模型）──────────────────────────
CREATE TABLE IF NOT EXISTS llm_usage_log (
    id                VARCHAR(36) PRIMARY KEY,
    user_id           VARCHAR(36),
    action            VARCHAR(64),
    model             VARCHAR(100),
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    total_tokens      INTEGER     NOT NULL DEFAULT 0,
    created_at        TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_llm_usage_log_user_id    ON llm_usage_log (user_id);
CREATE INDEX IF NOT EXISTS ix_llm_usage_log_created_at ON llm_usage_log (created_at);
CREATE INDEX IF NOT EXISTS ix_lul_user_created         ON llm_usage_log (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_lul_action_created       ON llm_usage_log (action, created_at);

-- ── 5. 每人每日配额 ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_quotas (
    id                 VARCHAR(36) PRIMARY KEY,
    user_id            VARCHAR(36) NOT NULL UNIQUE REFERENCES users(id),
    daily_action_limit INTEGER     NOT NULL DEFAULT 0,   -- 0 = 不限
    daily_token_limit  INTEGER     NOT NULL DEFAULT 0,   -- 0 = 不限
    note               VARCHAR(256),
    created_at         TIMESTAMP,
    updated_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_user_quotas_user_id ON user_quotas (user_id);

-- ── 6. 新权限码 settings.monitor ───────────────────────────────────────────
-- 应用启动时 initialize_rbac() 也会补，这里先插一份，保证「先迁移、后重启」
-- 的窗口期内权限就已经齐全。
-- 注意：**不要**把它授给 project_manager。该角色在 rbac.py 里用的是「排除法」，
-- 已显式排除 settings.monitor，否则项目经理会在下次重启后自动获得全公司监控权。
INSERT INTO rbac_permissions (id, code, name, category, description)
VALUES (
    '00000000-0000-0000-0000-perm00028',
    'settings.monitor',
    '查看使用监控',
    'settings',
    '查看全体成员的使用情况、在线状态、行为明细与 Token 消耗'
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-admin-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p
WHERE r.name = 'admin' AND p.code = 'settings.monitor'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- ── 7. 补种 bid_checker 角色（rbac.py 有、seed_pg.sql 一直漏了）─────────────
INSERT INTO rbac_roles (id, name, display_name, description, is_system)
VALUES (
    '00000000-0000-0000-0000-role00005',
    'bid_checker',
    '标书检查员',
    '上传招标/投标文件执行合规检查并导出报告',
    FALSE
)
ON CONFLICT (name) DO NOTHING;

INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-bid_checker-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p
WHERE r.name = 'bid_checker'
  AND p.code IN (
      'project.create', 'project.read',
      'interpret.upload', 'interpret.parse', 'interpret.view',
      'check.run', 'check.export', 'check.report'
  )
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;

SELECT '001_admin_monitor applied' AS message,
       (SELECT count(*) FROM information_schema.tables
         WHERE table_name IN ('user_sessions','user_activity_log','llm_usage_log','user_quotas')) AS new_tables,
       (SELECT count(*) FROM information_schema.columns
         WHERE table_name = 'users' AND column_name IN ('is_active','last_login_at')) AS new_user_columns;
