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
