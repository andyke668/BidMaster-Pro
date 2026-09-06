INSERT INTO rbac_roles (id, name, display_name, description, is_system) VALUES
    ('00000000-0000-0000-0000-role00001', 'admin', '管理员', '系统管理员，拥有所有系统权限', TRUE),
    ('00000000-0000-0000-0000-role00002', 'project_manager', '项目经理', '管理项目和团队成员，可分配任务', FALSE),
    ('00000000-0000-0000-0000-role00003', 'writer', '撰写员', '编写和编辑投标文件内容', FALSE),
    ('00000000-0000-0000-0000-role00004', 'reviewer', '审核员', '审核投标文件内容和检查报告', FALSE)
ON CONFLICT (name) DO NOTHING;

-- ============================================================
-- 十、初始数据 - 权限 (与 rbac.py DEFAULT_PERMISSIONS 对齐)
-- ============================================================

INSERT INTO rbac_permissions (id, code, name, category, description) VALUES
    -- 项目管理权限
    ('00000000-0000-0000-0000-perm00001', 'project.create', '创建项目', 'project', '创建新的投标项目'),
    ('00000000-0000-0000-0000-perm00002', 'project.read', '查看项目', 'project', '查看项目详情信息'),
    ('00000000-0000-0000-0000-perm00003', 'project.update', '更新项目', 'project', '修改项目基本信息'),
    ('00000000-0000-0000-0000-perm00004', 'project.delete', '删除项目', 'project', '删除投标项目'),
    -- 招标解读权限
    ('00000000-0000-0000-0000-perm00005', 'interpret.upload', '上传招标文件', 'interpret', '上传招标文件进行智能解读'),
    ('00000000-0000-0000-0000-perm00006', 'interpret.parse', '解析招标文件', 'interpret', '执行智能解读分析'),
    ('00000000-0000-0000-0000-perm00007', 'interpret.view', '查看解读结果', 'interpret', '查看解读结果和评分矩阵'),
    -- 投标生成权限
    ('00000000-0000-0000-0000-perm00008', 'generate.outline', '生成大纲', 'generate', '根据解读结果生成投标大纲'),
    ('00000000-0000-0000-0000-perm00009', 'generate.content', '生成正文', 'generate', '根据大纲生成章节内容'),
    ('00000000-0000-0000-0000-perm00010', 'generate.review', '审核内容', 'generate', '审核生成的内容'),
    -- 投标检查权限
    ('00000000-0000-0000-0000-perm00011', 'check.run', '执行检查', 'check', '执行各项投标检查'),
    ('00000000-0000-0000-0000-perm00012', 'check.export', '导出检查结果', 'check', '导出检查报告'),
    ('00000000-0000-0000-0000-perm00013', 'check.report', '生成检查报告', 'check', '生成综合检查报告'),
    -- 文档格式化权限
    ('00000000-0000-0000-0000-perm00014', 'format.run', '执行格式化', 'format', '应用文档格式化'),
    ('00000000-0000-0000-0000-perm00015', 'format.template', '管理模板', 'format', '管理格式化模板'),
    ('00000000-0000-0000-0000-perm00016', 'format.config', '配置格式化', 'format', '配置格式化参数'),
    -- 资讯中心权限
    ('00000000-0000-0000-0000-perm00017', 'news.monitor', '监控资讯', 'news', '创建资讯监控任务'),
    ('00000000-0000-0000-0000-perm00018', 'news.view', '查看资讯', 'news', '查看招投标资讯'),
    ('00000000-0000-0000-0000-perm00019', 'news.manage', '管理资讯', 'news', '管理资讯监控任务'),
    -- 知识库权限
    ('00000000-0000-0000-0000-perm00020', 'knowledge.create', '创建知识库', 'knowledge', '创建企业知识库'),
    ('00000000-0000-0000-0000-perm00021', 'knowledge.upload', '上传知识文档', 'knowledge', '上传知识文档'),
    ('00000000-0000-0000-0000-perm00022', 'knowledge.search', '搜索知识库', 'knowledge', '搜索知识库内容'),
    ('00000000-0000-0000-0000-perm00023', 'knowledge.delete', '删除知识库', 'knowledge', '删除知识库'),
    -- 系统设置权限
    ('00000000-0000-0000-0000-perm00024', 'settings.view', '查看设置', 'settings', '查看系统设置'),
    ('00000000-0000-0000-0000-perm00025', 'settings.llm', '配置LLM', 'settings', '配置大语言模型参数'),
    ('00000000-0000-0000-0000-perm00026', 'settings.agent', '配置Agent', 'settings', '配置Agent工作流'),
    ('00000000-0000-0000-0000-perm00027', 'settings.rbac', '管理权限', 'settings', '管理角色和权限')
ON CONFLICT (code) DO NOTHING;

-- ============================================================
-- 十一、初始数据 - 角色权限分配 (与 rbac.py DEFAULT_ROLES 对齐)
-- ============================================================

-- 管理员: 拥有所有权限
INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-admin-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p WHERE r.name = 'admin'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- 项目经理: 除 settings.rbac 和 settings.agent 外所有权限
INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-mgr-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p
WHERE r.name = 'project_manager' AND p.code NOT IN ('settings.rbac', 'settings.agent')
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- 撰写员: 项目读写 + 解读 + 生成 + 检查执行/导出 + 格式化执行 + 知识搜索
INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-writer-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p
WHERE r.name = 'writer' AND p.code IN (
    'project.create', 'project.read', 'project.update',
    'interpret.upload', 'interpret.parse', 'interpret.view',
    'generate.outline', 'generate.content', 'generate.review',
    'check.run', 'check.export',
    'format.run',
    'knowledge.search'
)
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- 审核员: 项目查看 + 解读查看 + 内容审核 + 检查 + 格式化
INSERT INTO rbac_role_permissions (id, role_id, permission_id)
SELECT md5('rp-reviewer-' || p.code), r.id, p.id
FROM rbac_roles r, rbac_permissions p
WHERE r.name = 'reviewer' AND p.code IN (
    'project.read',
    'interpret.view',
    'generate.review',
    'check.run', 'check.export', 'check.report',
    'format.run'
)
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- ============================================================
-- 十二、初始数据 - 默认管理员
-- ============================================================

INSERT INTO users (id, email, name, role, password_hash)
VALUES ('00000000-0000-0000-0000-user00001', 'admin@bidmaster.pro', '系统管理员', 'admin', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9')
ON CONFLICT (email) DO NOTHING;

-- 绑定管理员角色
INSERT INTO rbac_user_roles (id, user_id, role_id)
SELECT '00000000-0000-0000-ur-admin-role', u.id, r.id
FROM users u, rbac_roles r
WHERE u.email = 'admin@bidmaster.pro' AND r.name = 'admin'
ON CONFLICT (user_id, role_id) DO NOTHING;

-- ============================================================
-- 十三、初始数据 - 默认 Agent 配置 (与 llm_config.py DEFAULT_AGENTS 对齐)
-- ============================================================

INSERT INTO agent_configs (id, name, workflow_dsl, skills, config, enabled) VALUES
    ('00000000-0000-0000-agent-0001', 'interpret', '{}', '[]',
     '{"display_name": "招标解读Agent", "description": "解读招标文件，提取关键信息、评分标准、资质要求", "temperature": 0.3, "max_tokens": 8192}', TRUE),
    ('00000000-0000-0000-agent-0002', 'outline', '{}', '[]',
     '{"display_name": "大纲生成Agent", "description": "根据解读结果生成投标大纲，对齐评分项", "temperature": 0.5, "max_tokens": 4096}', TRUE),
    ('00000000-0000-0000-agent-0003', 'content', '{}', '[]',
     '{"display_name": "内容生成Agent", "description": "根据大纲逐章节生成标书内容", "temperature": 0.7, "max_tokens": 8192}', TRUE),
    ('00000000-0000-0000-agent-0004', 'check', '{}', '[]',
     '{"display_name": "质量检查Agent", "description": "对生成内容进行合规性、一致性、完整性检查", "temperature": 0.2, "max_tokens": 4096}', TRUE),
    ('00000000-0000-0000-agent-0005', 'format', '{}', '[]',
     '{"display_name": "格式排版Agent", "description": "对文档进行格式排版和美化", "temperature": 0.1, "max_tokens": 2048}', TRUE),
    ('00000000-0000-0000-agent-0006', 'final_check', '{}', '[]',
     '{"display_name": "终审Agent", "description": "最终全面检查，确保无遗漏", "temperature": 0.1, "max_tokens": 4096}', TRUE),
    ('00000000-0000-0000-agent-0007', 'export', '{}', '[]',
     '{"display_name": "导出Agent", "description": "导出最终投标文件", "temperature": 0.0, "max_tokens": 2048}', TRUE)
ON CONFLICT (name) DO NOTHING;

-- 完成
SELECT 'BidMaster Pro database initialized successfully!' AS message;
