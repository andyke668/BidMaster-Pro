-- ============================================================================
-- 002 · 审查档案留存（管理后台可查看/下载用户的招标文件、投标书与审查报告）
-- ============================================================================
-- 背景：标书检查员走的「上传模式」（POST /api/check/tender-bid-review 与
-- /api/check/upload-check）此前**不留任何持久数据**——
--   ① 上传的招标/投标原件在 _parse_review_document 里写 NamedTemporaryFile，
--      解析完 finally 就 os.unlink 删掉（services/routers/check.py）；
--   ② 生成的审查报告 Excel 落在 tempfile.gettempdir()/bidmaster_exports，
--      而 compose 只挂了 projects / uploads / chroma 三个卷，容器一重启即丢，
--      用户过一阵再点下载就撞上「文件不存在或已过期」；
--   ③ 两者都没有任何数据库行，既关联不到用户，也关联不到行为流水。
-- 所以管理后台即便有 settings.monitor 权限也无从查看——数据在源头就不存在。
--
-- 本迁移补上这两张表，配合 uploads/reviews/{review_id}/ 的持久化目录，
-- 让「谁在什么时候审了哪两份文件、结论是什么、原件和报告在哪」可查可下载。
--
-- 项目模式不在此表：它的文件本就有 documents 行（落 ./projects/{pid}/，在
-- projects 卷上）、报告有 check_reports 行，管理端直接读那两张表即可，
-- 不做二次落库以免产生需要同步的冗余副本。
--
-- 幂等：全部 IF NOT EXISTS，可重复执行。
-- 执行：bash deploy_bidmaster.sh migrate
-- ============================================================================

BEGIN;

-- ── 1. 审查记录（上传模式，一次审查一行）──────────────────────────────────
-- user_id 刻意不建外键：与 user_activity_log 同理，档案要在用户被删除后依然
-- 可查，故冗余 user_email / user_name。
CREATE TABLE IF NOT EXISTS review_records (
    id             VARCHAR(36)  PRIMARY KEY,
    user_id        VARCHAR(36),
    user_email     VARCHAR(255),
    user_name      VARCHAR(100),
    source         VARCHAR(32)  NOT NULL,             -- upload_review / upload_check
    task_id        VARCHAR(64),                       -- TaskManager 任务号
    activity_id    VARCHAR(36),                       -- 关联 user_activity_log.id
    company_name   VARCHAR(200),                      -- 投标方公司
    school_name    VARCHAR(200),                      -- 招标方单位
    check_type     VARCHAR(40),                       -- upload_check 的检查类型
    status         VARCHAR(16)  NOT NULL DEFAULT 'running',  -- running/success/failed
    error_message  TEXT,
    report_summary JSON,                              -- total_items/high_count/维度计数
    report_data    JSON,                              -- 维度明细，供后台原生预览
    created_at     TIMESTAMP,
    finished_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_review_records_user_id    ON review_records (user_id);
CREATE INDEX IF NOT EXISTS ix_review_records_source     ON review_records (source);
CREATE INDEX IF NOT EXISTS ix_review_records_status     ON review_records (status);
CREATE INDEX IF NOT EXISTS ix_review_records_task_id    ON review_records (task_id);
CREATE INDEX IF NOT EXISTS ix_review_records_created_at ON review_records (created_at);
CREATE INDEX IF NOT EXISTS ix_rrec_user_created         ON review_records (user_id, created_at);

-- ── 2. 审查涉及的文件（招标件 / 投标件 / 报告，一个文件一行）────────────────
-- 这里的 review_id **要**建外键并级联删除：管理后台的「手动清理」按钮就是靠它
-- 一次删掉记录与全部文件行，不留孤儿。与上面 user_id 不建外键的取舍不同——
-- 用户要能消失而档案留下，档案被清理时则应当连文件行一起干净地走掉。
CREATE TABLE IF NOT EXISTS review_files (
    id             VARCHAR(36)  PRIMARY KEY,
    review_id      VARCHAR(36)  NOT NULL REFERENCES review_records(id) ON DELETE CASCADE,
    kind           VARCHAR(20)  NOT NULL,             -- tender / bid / report
    original_name  VARCHAR(255),                      -- 用户看到的原始文件名
    stored_path    VARCHAR(500) NOT NULL,             -- 相对 /app 的路径
    file_size      INTEGER,
    sha256         VARCHAR(64),
    created_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_review_files_review_id ON review_files (review_id);
CREATE INDEX IF NOT EXISTS ix_review_files_kind      ON review_files (kind);

COMMIT;

SELECT '002_review_artifacts applied' AS message,
       (SELECT count(*) FROM information_schema.tables
         WHERE table_name IN ('review_records','review_files')) AS new_tables;