import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  Database, Download, FileText, FolderOpen, HardDrive, RefreshCw,
  Search, Trash2, X,
} from 'lucide-react';
import {
  adminApi,
  type AdminProjectFiles,
  type AdminProjectItem,
  type AdminRange,
  type AdminReportJson,
  type AdminReportText,
  type AdminReviewDetail,
  type AdminReviewFile,
  type AdminReviewItem,
  type AdminStorageStats,
} from '../../services/api';
import MarkdownRenderer from '../common/MarkdownRenderer';
import {
  ACTIVITY_STATUS_META, BTN, BTN_DANGER, BTN_PRIMARY, Card, Empty, ErrorBar,
  INPUT, Kpi, Pager, Pill, Spinner, TD, TH, errText, fmtDateTime, fmtDuration,
  fmtInt, usePoll,
} from './adminShared';

// ─────────────────────────── 常量 ───────────────────────────

const PAGE_SIZE = 20;

type SubTab = 'reviews' | 'projects';

const SUB_TABS: Array<{ key: SubTab; label: string; icon: typeof FileText }> = [
  { key: 'reviews', label: '上传模式审查档案', icon: FileText },
  { key: 'projects', label: '项目模式文件与报告', icon: FolderOpen },
];

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'upload_review', label: '招投标文件审查' },
  { value: 'upload_check', label: '上传模式检查' },
];

const REVIEW_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '进行中' },
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
];

/**
 * 维度明细行里英文字段名的中文表头。
 * pricing / delivery 两个维度模型直接返回中文键，落到这里查不到就原样显示，
 * 所以以后加维度不用改前端。
 */
const FIELD_LABELS: Record<string, string> = {
  label: '字段',
  value: '内容',
  seq: '序号',
  clause_type: '条款类型',
  clause_content: '条款内容',
  source_location: '原文出处',
  matched_quote: '命中关键句',
  response_found: '投标书是否响应',
  response_content: '响应内容',
  response_status: '响应状态',
  risk_level: '风险等级',
  suggestion: '修改建议',
  scoring_item: '评分项',
  score: '分值',
  predicted_score: '预计得分',
  predicted_reason: '评分理由',
  marker: '标记',
  requirement: '招标要求',
  category: '类别',
  deviation: '偏离情况',
  material_name: '材料名称',
  included: '是否包含',
  location: '所在位置',
  note: '备注',
  node_type: '节点类型',
  time_value: '时间',
  responded: '是否响应',
  risk_note: '风险提示',
  topic: '条款主题',
  original_quote: '原文摘录',
  contract_risk: '合同风险',
};

const RISK_META: Record<string, { label: string; color: string; bg: string }> = {
  high: { label: '高', color: '#b91c1c', bg: '#fef2f2' },
  medium: { label: '中', color: '#b45309', bg: '#fffbeb' },
  low: { label: '低', color: '#059669', bg: '#ecfdf5' },
};

type Row = Record<string, unknown>;
type Notice = { text: string; bad: boolean } | null;

// ─────────────────────────── 小工具 ───────────────────────────

function cellText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function columnsOf(rows: Row[]): string[] {
  const seen: string[] = [];
  rows.forEach((row) => {
    Object.keys(row || {}).forEach((key) => {
      if (!seen.includes(key)) seen.push(key);
    });
  });
  return seen;
}

/**
 * 判断 report_data 是不是「维度 → 行数组」结构（招投标文件审查）。
 * 上传模式检查存的是各检查项的嵌套 JSON，形状不固定，只能整块 pretty-print，
 * 所以这里用运行时判断而不是看 source 字段。
 */
function asDimensionData(value: unknown): Record<string, Row[]> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return null;
  if (!entries.every(([, item]) => Array.isArray(item))) return null;
  return value as Record<string, Row[]>;
}

function summaryNumber(summary: Record<string, unknown>, key: string): number | null {
  const value = summary?.[key];
  return typeof value === 'number' ? value : null;
}

function useFlash() {
  const [notice, setNotice] = useState<Notice>(null);
  const flash = useCallback((text: string, bad = false) => {
    setNotice({ text, bad });
    window.setTimeout(() => setNotice(null), 5000);
  }, []);
  return { notice, flash };
}

function NoticeText({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return (
    <span style={{ fontSize: '12px', color: notice.bad ? 'var(--color-danger)' : 'var(--color-text-secondary)' }}>
      {notice.text}
    </span>
  );
}

function MetaGrid({ items }: { items: Array<{ label: string; value: string }> }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px', fontSize: '12px' }}>
      {items.map((item) => (
        <div key={item.label} style={{ display: 'flex', gap: '6px', minWidth: 0 }}>
          <span style={{ color: 'var(--color-text-secondary)', flexShrink: 0 }}>{item.label}</span>
          <span
            title={item.value}
            style={{ color: 'var(--color-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          >
            {item.value || '—'}
          </span>
        </div>
      ))}
    </div>
  );
}

function Drawer({
  title, subtitle, onClose, children, footer, width,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.35)', zIndex: 890 }} />
      <div style={{ position: 'fixed', top: 0, right: 0, bottom: 0, width: width || '760px', maxWidth: '94vw', background: 'var(--color-bg)', zIndex: 891, boxShadow: '-8px 0 28px rgba(15,23,42,0.18)', display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '14px 18px', background: 'var(--color-surface)', borderBottom: '1px solid var(--color-border)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: '15px', fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</div>
            {subtitle && <div style={{ fontSize: '12px', color: '#94a3b8', marginTop: '2px' }}>{subtitle}</div>}
          </div>
          <button type="button" style={{ ...BTN, padding: '5px 8px' }} onClick={onClose}><X size={14} /></button>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '14px 18px' }}>{children}</div>
        {footer && (
          <div style={{ padding: '12px 18px', borderTop: '1px solid var(--color-border)', background: 'var(--color-surface)', display: 'flex', gap: '8px', justifyContent: 'flex-end', alignItems: 'center', flexWrap: 'wrap' }}>
            {footer}
          </div>
        )}
      </div>
    </>
  );
}

function StatusPill({ status }: { status: string }) {
  const meta = ACTIVITY_STATUS_META[status] || ACTIVITY_STATUS_META.success;
  return <Pill text={status} color={meta.color} bg={meta.bg} />;
}

function UserFilter({ value, onChange, range }: { value: string; onChange: (id: string) => void; range: AdminRange }) {
  const [options, setOptions] = useState<Array<{ id: string; name: string; email: string }>>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await adminApi.users({ range, page: 1, page_size: 200, sort: 'name', order: 'asc' });
        if (!cancelled) setOptions(res.data.users.map((u) => ({ id: u.id, name: u.name, email: u.email })));
      } catch {
        /* 拉不到用户列表就只留「全部用户」 */
      }
    })();
    return () => { cancelled = true; };
  }, [range]);

  return (
    <select style={{ ...INPUT, maxWidth: '220px' }} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">全部用户</option>
      {options.map((user) => <option key={user.id} value={user.id}>{user.name}（{user.email}）</option>)}
    </select>
  );
}

function SearchBox({ value, onChange, onSearch, placeholder }: {
  value: string;
  onChange: (text: string) => void;
  onSearch: () => void;
  placeholder: string;
}) {
  return (
    <span style={{ display: 'inline-flex', gap: '6px' }}>
      <input
        style={{ ...INPUT, width: '220px' }}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }}
      />
      <button type="button" style={BTN} onClick={onSearch}><Search size={13} /> 搜索</button>
    </span>
  );
}

// ─────────────────────────── 存储概览与手动清理 ───────────────────────────

function StorageStrip({ reloadToken, onCleaned }: { reloadToken: number; onCleaned: () => void }) {
  const [days, setDays] = useState('90');
  const [busy, setBusy] = useState(false);
  const { notice, flash } = useFlash();
  const query = usePoll<AdminStorageStats>(
    async () => (await adminApi.storage()).data,
    [reloadToken],
    null,
  );
  const stats = query.data;

  const doCleanup = async () => {
    const count = Number(days);
    if (!Number.isFinite(count) || count <= 0) {
      flash('请输入大于 0 的天数', true);
      return;
    }
    const ok = window.confirm(
      '确定清理 ' + count + ' 天前的全部上传模式审查档案？'
      + '数据库记录与磁盘上的招标件、投标件、报告原件会一并删除，且不可恢复。'
      + '项目模式的文件不受影响。',
    );
    if (!ok) return;
    setBusy(true);
    try {
      const res = await adminApi.cleanupReviews({ before_days: count });
      flash('已清理 ' + res.data.deleted + ' 条，释放 ' + res.data.freed_label);
      query.reload();
      onCleaned();
    } catch (err) {
      flash('清理失败：' + errText(err), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card style={{ marginBottom: '12px' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: '10px' }}>
        <Kpi label="审查档案" value={fmtInt(stats?.reviews.records)} sub="上传模式累计留档次数" icon={FileText} color="#1a56db" />
        <Kpi label="档案文件" value={fmtInt(stats?.reviews.files)} sub={'数据库登记 ' + (stats?.reviews.db_label || '—')} icon={Database} color="#7c3aed" />
        <Kpi label="档案磁盘占用" value={stats?.reviews.disk_label || '—'} sub="uploads/reviews 实际占用" icon={HardDrive} color="#059669" />
        <Kpi label="项目模式文件" value={fmtInt(stats?.project_documents.files)} sub={'合计 ' + (stats?.project_documents.db_label || '—')} icon={FolderOpen} color="#d97706" />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--color-border)', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>清理</span>
        <input
          type="number"
          min={1}
          style={{ ...INPUT, width: '86px' }}
          value={days}
          onChange={(e) => setDays(e.target.value)}
        />
        <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>天前的审查档案</span>
        <button type="button" style={BTN_DANGER} onClick={doCleanup} disabled={busy}>
          <Trash2 size={13} /> {busy ? '清理中…' : '立即清理'}
        </button>
        <button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
        <NoticeText notice={notice} />
      </div>
      <div style={{ marginTop: '8px', fontSize: '11px', color: '#94a3b8' }}>
        投标文件属敏感商业资料，系统不做自动过期，什么时候删由管理员手动决定。
      </div>
      <ErrorBar message={query.error} />
    </Card>
  );
}
function RiskPill({ value }: { value: unknown }) {
  const raw = String(value ?? '').trim().toLowerCase();
  const meta = RISK_META[raw];
  if (!meta) return <span>{cellText(value) || '—'}</span>;
  return <Pill text={meta.label + '风险'} color={meta.color} bg={meta.bg} />;
}

function summaryChips(summary: Record<string, unknown>): ReactNode {
  const chips: Array<{ label: string; value: string; color: string; bg: string }> = [];
  const totalItems = summaryNumber(summary, 'total_items');
  const highCount = summaryNumber(summary, 'high_count');
  const guardMissing = summaryNumber(summary, 'guardrail_missing');
  const guardTotal = summaryNumber(summary, 'guardrail_total');
  if (totalItems !== null) chips.push({ label: '审出条目', value: fmtInt(totalItems), color: '#1d4ed8', bg: '#eff6ff' });
  if (highCount !== null) chips.push({ label: '高风险', value: fmtInt(highCount), color: highCount > 0 ? '#b91c1c' : '#059669', bg: highCount > 0 ? '#fef2f2' : '#ecfdf5' });
  if (guardMissing !== null && guardTotal !== null) {
    chips.push({ label: '护栏未覆盖', value: guardMissing + ' / ' + guardTotal, color: guardMissing > 0 ? '#b45309' : '#059669', bg: guardMissing > 0 ? '#fffbeb' : '#ecfdf5' });
  }
  const checkTypes = summary?.check_types;
  if (Array.isArray(checkTypes) && checkTypes.length) {
    chips.push({ label: '检查项', value: checkTypes.map((item) => String(item)).join('、'), color: '#7c3aed', bg: '#f5f3ff' });
  }
  if (summary?.has_critical === true) chips.push({ label: '存在致命问题', value: '是', color: '#b91c1c', bg: '#fef2f2' });
  if (!chips.length) return null;
  return (
    <div style={{ display: 'flex', gap: '8px', marginTop: '10px', flexWrap: 'wrap' }}>
      {chips.map((chip) => (
        <span key={chip.label} style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '3px 9px', borderRadius: '999px', fontSize: '11px', fontWeight: 600, background: chip.bg, color: chip.color }}>
          {chip.label} {chip.value}
        </span>
      ))}
    </div>
  );
}

/**
 * 报告明细预览：按维度折叠展示，列名取「行里出现过的键」的并集。
 * 不硬编码每个维度的列，模型改了 prompt 返回新字段时这里自动跟上。
 */
function ReportPreview({ reportData, labels }: { reportData: unknown; labels: Record<string, string> }) {
  const raw = (reportData && typeof reportData === 'object' && !Array.isArray(reportData))
    ? reportData as Record<string, unknown>
    : {};
  const dimensions = asDimensionData(raw);

  if (!dimensions) {
    if (!Object.keys(raw).length) return <Empty text="这条记录没有可预览的审查明细" />;
    return (
      <pre style={{ margin: 0, padding: '12px', background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', fontSize: '11px', lineHeight: 1.6, overflow: 'auto', maxHeight: '440px' }}>
        {JSON.stringify(raw, null, 2)}
      </pre>
    );
  }

  const entries = Object.entries(dimensions).filter(([, rows]) => rows.length > 0);
  if (!entries.length) return <Empty text="审查已完成但没有产出条目" />;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {entries.map(([key, rows], index) => (
        <DimensionBlock
          key={key}
          title={labels?.[key] || key}
          rows={rows}
          defaultOpen={index === 0}
        />
      ))}
    </div>
  );
}

function DimensionBlock({ title, rows, defaultOpen }: { title: string; rows: Row[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const columns = useMemo(() => columnsOf(rows), [rows]);
  const highCount = rows.filter((row) => String(row?.risk_level ?? '').trim().toLowerCase() === 'high').length;

  return (
    <div style={{ border: '1px solid var(--color-border)', borderRadius: '8px', overflow: 'hidden' }}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        style={{ width: '100%', display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 12px', background: '#f8fafc', border: 'none', cursor: 'pointer', fontSize: '12px', fontWeight: 600, color: 'var(--color-text)', textAlign: 'left' }}
      >
        <span style={{ flex: 1 }}>{title}</span>
        {highCount > 0 && <Pill text={'高风险 ' + highCount} color="#b91c1c" bg="#fef2f2" />}
        <Pill text={rows.length + ' 条'} color="#475569" bg="#f1f5f9" />
        <span style={{ color: '#94a3b8', fontSize: '11px', fontWeight: 400 }}>{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div style={{ overflow: 'auto', maxHeight: '380px' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col} style={{ ...TH, position: 'sticky', top: 0, zIndex: 1 }}>{FIELD_LABELS[col] || col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  {columns.map((col) => (
                    <td key={col} style={{ ...TD, maxWidth: '300px', verticalAlign: 'top', fontSize: '12px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {col === 'risk_level' ? <RiskPill value={row?.[col]} /> : (cellText(row?.[col]) || '—')}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────── 上传模式：审查档案 ───────────────────────────

function ReviewRow({ row, onOpen, onOpenUser }: {
  row: AdminReviewItem;
  onOpen: () => void;
  onOpenUser: (userId: string) => void;
}) {
  const totalItems = summaryNumber(row.summary, 'total_items');
  const highCount = summaryNumber(row.summary, 'high_count');
  const primary = row.school_name || row.company_name || '—';
  const secondary = row.school_name && row.company_name ? row.company_name : '';

  return (
    <tr style={{ cursor: 'pointer' }} onClick={onOpen}>
      <td style={{ ...TD, whiteSpace: 'nowrap', fontSize: '12px', color: 'var(--color-text-secondary)' }}>{fmtDateTime(row.created_at)}</td>
      <td style={TD}>
        {row.user_id ? (
          <button
            type="button"
            onClick={(event) => { event.stopPropagation(); onOpenUser(row.user_id as string); }}
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 500, color: 'var(--color-primary)' }}
          >
            {row.user_name || '未知用户'}
          </button>
        ) : (
          <span style={{ color: '#94a3b8', fontSize: '13px' }}>{row.user_name || '（用户已删除）'}</span>
        )}
        <div style={{ fontSize: '11px', color: '#94a3b8' }}>{row.user_email || ''}</div>
      </td>
      <td style={{ ...TD, fontSize: '12px', whiteSpace: 'nowrap' }}>{row.source_label}</td>
      <td style={{ ...TD, maxWidth: '230px' }}>
        <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={primary}>{primary}</div>
        {secondary && <div style={{ fontSize: '11px', color: '#94a3b8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={secondary}>{secondary}</div>}
      </td>
      <td style={{ ...TD, textAlign: 'right' }}>{totalItems === null ? '—' : fmtInt(totalItems)}</td>
      <td style={{ ...TD, textAlign: 'right' }}>
        {highCount === null ? '—' : highCount > 0
          ? <Pill text={String(highCount)} color="#b91c1c" bg="#fef2f2" />
          : <span style={{ color: '#94a3b8' }}>0</span>}
      </td>
      <td style={TD}><StatusPill status={row.status} /></td>
      <td style={{ ...TD, textAlign: 'right', fontSize: '12px' }}>{row.files.length}</td>
      <td style={{ ...TD, textAlign: 'right', fontSize: '12px', color: 'var(--color-text-secondary)' }}>{fmtDuration(row.duration_ms)}</td>
    </tr>
  );
}

function ReviewDrawer({ reviewId, onClose, onChanged }: {
  reviewId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const query = usePoll<AdminReviewDetail>(
    async () => (await adminApi.reviewDetail(reviewId)).data,
    [reviewId],
    null,
  );
  const [busy, setBusy] = useState(false);
  const { notice, flash } = useFlash();
  const detail = query.data;

  const download = async (file: AdminReviewFile) => {
    setBusy(true);
    try {
      await adminApi.downloadReviewFile(reviewId, file.file_id, file.original_name || file.kind_label);
      flash('已开始下载：' + (file.original_name || file.kind_label));
    } catch (err) {
      flash('下载失败：' + errText(err), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const ok = window.confirm('确定删除这条审查档案？招标件、投标件与报告原件会一并从磁盘清除，且不可恢复。');
    if (!ok) return;
    setBusy(true);
    try {
      await adminApi.deleteReview(reviewId);
      onChanged();
      onClose();
    } catch (err) {
      flash('删除失败：' + errText(err), true);
      setBusy(false);
    }
  };

  return (
    <Drawer
      title={detail ? (detail.school_name || detail.company_name || '审查档案') : '审查档案'}
      subtitle={detail
        ? [detail.user_name || '未知用户', detail.user_email, detail.source_label].filter(Boolean).join(' · ')
        : reviewId}
      onClose={onClose}
      footer={
        <>
          <NoticeText notice={notice} />
          <span style={{ flex: 1 }} />
          <button type="button" style={BTN} onClick={query.reload} disabled={busy}><RefreshCw size={13} /> 刷新</button>
          <button type="button" style={BTN_DANGER} onClick={remove} disabled={busy || !detail}>
            <Trash2 size={13} /> 删除档案
          </button>
        </>
      }
    >
      <ErrorBar message={query.error} />
      {query.loading && !detail ? <Spinner text="正在加载审查档案…" /> : !detail ? (
        <Empty text="档案不存在或已被清理" />
      ) : (
        <>
          <Card title="基本信息" style={{ marginBottom: '12px' }}>
            <MetaGrid items={[
              { label: '档案号', value: detail.review_id },
              { label: '状态', value: detail.status },
              { label: '开始', value: fmtDateTime(detail.created_at) },
              { label: '结束', value: fmtDateTime(detail.finished_at) },
              { label: '耗时', value: fmtDuration(detail.duration_ms) },
              { label: '检查类型', value: detail.check_type || '' },
              { label: '招标单位', value: detail.school_name || '' },
              { label: '投标公司', value: detail.company_name || '' },
              { label: '任务号', value: detail.task_id || '' },
              { label: '行为流水号', value: detail.activity_id || '' },
            ]} />
            {detail.error_message && (
              <div style={{ marginTop: '10px', padding: '8px 10px', borderRadius: '6px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', fontSize: '12px' }}>
                {detail.error_message}
              </div>
            )}
            {summaryChips(detail.summary)}
          </Card>

          <Card title={'文件（' + detail.files.length + '）'} style={{ marginBottom: '12px' }}>
            {detail.files.length === 0 ? (
              <Empty text="这条记录没有留下文件（落盘失败，或早于本功能上线）" />
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={TH}>类型</th>
                      <th style={TH}>文件名</th>
                      <th style={{ ...TH, textAlign: 'right' }}>大小</th>
                      <th style={TH}>落盘时间</th>
                      <th style={{ ...TH, textAlign: 'right' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.files.map((file) => (
                      <tr key={file.file_id}>
                        <td style={TD}><Pill text={file.kind_label} color="#1d4ed8" bg="#eff6ff" /></td>
                        <td style={{ ...TD, maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={file.original_name || ''}>
                          {file.original_name || '—'}
                        </td>
                        <td style={{ ...TD, textAlign: 'right', fontSize: '12px', whiteSpace: 'nowrap' }}>{file.size_label}</td>
                        <td style={{ ...TD, fontSize: '12px', color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>{fmtDateTime(file.created_at)}</td>
                        <td style={{ ...TD, textAlign: 'right' }}>
                          <button type="button" style={BTN} disabled={busy} onClick={() => download(file)}>
                            <Download size={13} /> 下载
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title="审查报告预览">
            <ReportPreview reportData={detail.report_data} labels={detail.dimension_labels} />
          </Card>
        </>
      )}
    </Drawer>
  );
}

function ReviewsPanel({ range, reloadToken, onOpenUser, onChanged }: {
  range: AdminRange;
  reloadToken: number;
  onOpenUser: (userId: string) => void;
  onChanged: () => void;
}) {
  const [userId, setUserId] = useState('');
  const [source, setSource] = useState('');
  const [status, setStatus] = useState('');
  const [keyword, setKeyword] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [activeId, setActiveId] = useState<string | null>(null);

  useEffect(() => { setPage(1); }, [range, userId, source, status, q, reloadToken]);

  const query = usePoll<{ total: number; items: AdminReviewItem[] }>(
    async () => {
      const res = await adminApi.reviews({
        range,
        user_id: userId || undefined,
        source: source || undefined,
        status: status || undefined,
        q: q || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      return { total: res.data.total, items: res.data.items };
    },
    [range, userId, source, status, q, page, reloadToken],
    null,
  );

  const total = query.data?.total || 0;
  const items = query.data?.items || [];

  return (
    <div>
      <ErrorBar message={query.error} />
      <Card style={{ marginBottom: '12px', padding: '12px 16px' }}>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <UserFilter range={range} value={userId} onChange={setUserId} />
          <select style={{ ...INPUT, maxWidth: '180px' }} value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCE_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <select style={INPUT} value={status} onChange={(e) => setStatus(e.target.value)}>
            {REVIEW_STATUS_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <SearchBox
            value={keyword}
            onChange={setKeyword}
            onSearch={() => setQ(keyword.trim())}
            placeholder="文件名 / 公司 / 招标单位 / 用户"
          />
          <div style={{ marginLeft: 'auto' }}>
            <button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
          </div>
        </div>
        <div style={{ marginTop: '8px', fontSize: '11px', color: '#94a3b8' }}>
          点击任意一行，可查看并下载该次审查的招标文件、投标书与报告原件，报告明细支持在后台直接预览。
        </div>
      </Card>

      <Card title={'审查档案（共 ' + fmtInt(total) + ' 条 · 区间 ' + range + '）'}>
        {query.loading && !query.data ? <Spinner text="正在加载审查档案…" /> : items.length === 0 ? (
          <Empty text="该条件下没有审查档案。本功能上线前跑的审查没有留档，无法追溯。" />
        ) : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={TH}>时间</th>
                    <th style={TH}>用户</th>
                    <th style={TH}>来源</th>
                    <th style={TH}>招标单位 / 投标公司</th>
                    <th style={{ ...TH, textAlign: 'right' }}>审出</th>
                    <th style={{ ...TH, textAlign: 'right' }}>高风险</th>
                    <th style={TH}>状态</th>
                    <th style={{ ...TH, textAlign: 'right' }}>文件</th>
                    <th style={{ ...TH, textAlign: 'right' }}>耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <ReviewRow
                      key={row.review_id}
                      row={row}
                      onOpen={() => setActiveId(row.review_id)}
                      onOpenUser={onOpenUser}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <Pager total={total} page={page} pageSize={PAGE_SIZE} onPage={setPage} />
          </>
        )}
      </Card>

      {activeId && (
        <ReviewDrawer reviewId={activeId} onClose={() => setActiveId(null)} onChanged={onChanged} />
      )}
    </div>
  );
}
// ─────────────────────────── 项目模式：文件与报告 ───────────────────────────

function MarkdownPane({ reportId, onFallback }: { reportId: string; onFallback: () => void }) {
  const query = usePoll<AdminReportText>(
    async () => (await adminApi.reportMarkdown(reportId)).data,
    [reportId],
    null,
  );

  if (query.loading && !query.data) return <Spinner text="正在渲染报告…" />;
  if (query.error) {
    return (
      <div>
        <ErrorBar message={query.error} />
        <button type="button" style={BTN} onClick={onFallback}>改看原始 JSON</button>
      </div>
    );
  }
  if (!query.data?.content) return <Empty text="报告内容为空" />;
  return (
    <div style={{ maxHeight: '460px', overflowY: 'auto' }}>
      <MarkdownRenderer>{query.data.content}</MarkdownRenderer>
    </div>
  );
}

function JsonPane({ reportId }: { reportId: string }) {
  const query = usePoll<AdminReportJson>(
    async () => (await adminApi.reportJson(reportId)).data,
    [reportId],
    null,
  );

  if (query.loading && !query.data) return <Spinner text="正在加载报告…" />;
  if (query.error) return <ErrorBar message={query.error} />;
  const report = query.data;
  if (!report) return <Empty text="报告不存在" />;
  return (
    <div>
      <MetaGrid items={[
        { label: '所属项目', value: report.project_name },
        { label: '报告类型', value: report.type },
        { label: '风险等级', value: report.risk_level || '' },
        { label: '生成时间', value: fmtDateTime(report.created_at) },
      ]} />
      <pre style={{ margin: '10px 0 0', padding: '12px', background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', fontSize: '11px', lineHeight: 1.6, overflow: 'auto', maxHeight: '400px' }}>
        {JSON.stringify({ summary: report.summary, results: report.results }, null, 2)}
      </pre>
    </div>
  );
}

/**
 * 报告预览默认走 markdown：后端用 CheckReportExportSkill 渲染，该 skill 只做
 * 格式化、不调 LLM，所以管理员翻报告不产生 token 开销。渲染失败时给一个
 * 「改看原始 JSON」的退路，不至于因为导出链路异常就什么都看不到。
 */
function ReportViewer({ reportId, title }: { reportId: string; title: string }) {
  const [mode, setMode] = useState<'markdown' | 'json'>('markdown');
  return (
    <Card
      title={'报告预览 · ' + title}
      style={{ marginTop: '12px' }}
      extra={
        <span style={{ display: 'inline-flex', gap: '6px' }}>
          <button type="button" style={mode === 'markdown' ? BTN_PRIMARY : BTN} onClick={() => setMode('markdown')}>渲染视图</button>
          <button type="button" style={mode === 'json' ? BTN_PRIMARY : BTN} onClick={() => setMode('json')}>原始 JSON</button>
        </span>
      }
    >
      {mode === 'markdown'
        ? <MarkdownPane reportId={reportId} onFallback={() => setMode('json')} />
        : <JsonPane reportId={reportId} />}
    </Card>
  );
}

function ProjectDrawer({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const query = usePoll<AdminProjectFiles>(
    async () => (await adminApi.projectFiles(projectId)).data,
    [projectId],
    null,
  );
  const [busy, setBusy] = useState(false);
  const [reportId, setReportId] = useState<string | null>(null);
  const { notice, flash } = useFlash();
  const data = query.data;

  const download = async (documentId: string, name: string) => {
    setBusy(true);
    try {
      await adminApi.downloadDocument(documentId, name);
      flash('已开始下载：' + name);
    } catch (err) {
      flash('下载失败：' + errText(err), true);
    } finally {
      setBusy(false);
    }
  };

  const activeReport = data?.reports.find((item) => item.report_id === reportId) || null;

  return (
    <Drawer
      title={data?.project.name || '项目文件与报告'}
      subtitle={data
        ? [data.project.user_name || '未知用户', data.project.user_email, '创建于 ' + fmtDateTime(data.project.created_at)].filter(Boolean).join(' · ')
        : projectId}
      onClose={onClose}
      footer={
        <>
          <NoticeText notice={notice} />
          <span style={{ flex: 1 }} />
          <button type="button" style={BTN} onClick={query.reload} disabled={busy}><RefreshCw size={13} /> 刷新</button>
        </>
      }
    >
      <ErrorBar message={query.error} />
      {query.loading && !data ? <Spinner text="正在加载项目文件…" /> : !data ? (
        <Empty text="项目不存在" />
      ) : (
        <>
          <Card title={'文件（' + data.documents.length + '）'}>
            {data.documents.length === 0 ? <Empty text="这个项目还没有上传文件" /> : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={TH}>类型</th>
                      <th style={TH}>文件名</th>
                      <th style={{ ...TH, textAlign: 'right' }}>大小</th>
                      <th style={TH}>上传时间</th>
                      <th style={{ ...TH, textAlign: 'right' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.documents.map((doc) => (
                      <tr key={doc.document_id}>
                        <td style={TD}><Pill text={doc.type_label} color="#1d4ed8" bg="#eff6ff" /></td>
                        <td style={{ ...TD, maxWidth: '280px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={doc.original_name}>
                          {doc.original_name}
                        </td>
                        <td style={{ ...TD, textAlign: 'right', fontSize: '12px', whiteSpace: 'nowrap' }}>{doc.size_label}</td>
                        <td style={{ ...TD, fontSize: '12px', color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>{fmtDateTime(doc.created_at)}</td>
                        <td style={{ ...TD, textAlign: 'right' }}>
                          {doc.available ? (
                            <button type="button" style={BTN} disabled={busy} onClick={() => download(doc.document_id, doc.original_name)}>
                              <Download size={13} /> 下载
                            </button>
                          ) : (
                            <Pill text="文件已丢失" color="#b45309" bg="#fffbeb" />
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title={'审查报告（' + data.reports.length + '）'} style={{ marginTop: '12px' }}>
            {data.reports.length === 0 ? <Empty text="这个项目还没有生成审查报告" /> : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={TH}>类型</th>
                      <th style={TH}>风险等级</th>
                      <th style={TH}>生成时间</th>
                      <th style={{ ...TH, textAlign: 'right' }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.reports.map((report) => (
                      <tr key={report.report_id}>
                        <td style={TD}>{report.type}</td>
                        <td style={TD}><RiskPill value={report.risk_level} /></td>
                        <td style={{ ...TD, fontSize: '12px', color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>{fmtDateTime(report.created_at)}</td>
                        <td style={{ ...TD, textAlign: 'right' }}>
                          <button
                            type="button"
                            style={reportId === report.report_id ? BTN_PRIMARY : BTN}
                            disabled={!report.has_results}
                            onClick={() => setReportId(reportId === report.report_id ? null : report.report_id)}
                          >
                            <FileText size={13} /> {reportId === report.report_id ? '收起' : '查看'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {reportId && activeReport && (
            <ReportViewer reportId={reportId} title={activeReport.type + ' · ' + fmtDateTime(activeReport.created_at)} />
          )}
        </>
      )}
    </Drawer>
  );
}

function ProjectRow({ row, onOpen, onOpenUser }: {
  row: AdminProjectItem;
  onOpen: () => void;
  onOpenUser: (userId: string) => void;
}) {
  return (
    <tr style={{ cursor: 'pointer' }} onClick={onOpen}>
      <td style={{ ...TD, maxWidth: '260px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 500 }} title={row.name}>
        {row.name}
      </td>
      <td style={TD}>
        {row.user_id ? (
          <button
            type="button"
            onClick={(event) => { event.stopPropagation(); onOpenUser(row.user_id as string); }}
            style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 500, color: 'var(--color-primary)' }}
          >
            {row.user_name || '未知用户'}
          </button>
        ) : (
          <span style={{ color: '#94a3b8', fontSize: '13px' }}>{row.user_name || '（用户已删除）'}</span>
        )}
        <div style={{ fontSize: '11px', color: '#94a3b8' }}>{row.user_email || ''}</div>
      </td>
      <td style={{ ...TD, fontSize: '12px' }}>{row.status || '—'}</td>
      <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(row.document_count)}</td>
      <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(row.report_count)}</td>
      <td style={{ ...TD, fontSize: '12px', color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>{fmtDateTime(row.created_at)}</td>
    </tr>
  );
}

function ProjectsPanel({ range, reloadToken, onOpenUser }: {
  range: AdminRange;
  reloadToken: number;
  onOpenUser: (userId: string) => void;
}) {
  const [userId, setUserId] = useState('');
  const [keyword, setKeyword] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [activeId, setActiveId] = useState<string | null>(null);

  useEffect(() => { setPage(1); }, [range, userId, q, reloadToken]);

  const query = usePoll<{ total: number; items: AdminProjectItem[] }>(
    async () => {
      const res = await adminApi.allProjects({
        range,
        user_id: userId || undefined,
        q: q || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      return { total: res.data.total, items: res.data.items };
    },
    [range, userId, q, page, reloadToken],
    null,
  );

  const total = query.data?.total || 0;
  const items = query.data?.items || [];

  return (
    <div>
      <ErrorBar message={query.error} />
      <Card style={{ marginBottom: '12px', padding: '12px 16px' }}>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <UserFilter range={range} value={userId} onChange={setUserId} />
          <SearchBox
            value={keyword}
            onChange={setKeyword}
            onSearch={() => setQ(keyword.trim())}
            placeholder="项目名 / 用户"
          />
          <div style={{ marginLeft: 'auto' }}>
            <button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
          </div>
        </div>
        <div style={{ marginTop: '8px', fontSize: '11px', color: '#94a3b8' }}>
          点击任意一行，可查看并下载该项目下的招标 / 投标文件原件，以及历次审查报告。
        </div>
      </Card>

      <Card title={'全部项目（共 ' + fmtInt(total) + ' 个 · 区间 ' + range + '）'}>
        {query.loading && !query.data ? <Spinner text="正在加载项目…" /> : items.length === 0 ? (
          <Empty text="该条件下没有项目" />
        ) : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={TH}>项目名</th>
                    <th style={TH}>归属用户</th>
                    <th style={TH}>状态</th>
                    <th style={{ ...TH, textAlign: 'right' }}>文件</th>
                    <th style={{ ...TH, textAlign: 'right' }}>报告</th>
                    <th style={TH}>创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <ProjectRow key={row.project_id} row={row} onOpen={() => setActiveId(row.project_id)} onOpenUser={onOpenUser} />
                  ))}
                </tbody>
              </table>
            </div>
            <Pager total={total} page={page} pageSize={PAGE_SIZE} onPage={setPage} />
          </>
        )}
      </Card>

      {activeId && <ProjectDrawer projectId={activeId} onClose={() => setActiveId(null)} />}
    </div>
  );
}

// ─────────────────────────── 页签入口 ───────────────────────────

export default function FilesTab({ range, reloadToken, onOpenUser }: {
  range: AdminRange;
  reloadToken: number;
  onOpenUser: (userId: string) => void;
}) {
  const [sub, setSub] = useState<SubTab>('reviews');
  const [archiveToken, setArchiveToken] = useState(0);
  const bumpArchive = useCallback(() => setArchiveToken((value) => value + 1), []);

  return (
    <div>
      <StorageStrip reloadToken={reloadToken + archiveToken} onCleaned={bumpArchive} />

      <div style={{ display: 'inline-flex', gap: '4px', marginBottom: '12px', padding: '3px', background: '#f1f5f9', borderRadius: '8px' }}>
        {SUB_TABS.map((item) => {
          const active = sub === item.key;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setSub(item.key)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '6px',
                padding: '6px 12px', fontSize: '12px', borderRadius: '6px',
                border: 'none', cursor: 'pointer',
                background: active ? '#ffffff' : 'transparent',
                color: active ? 'var(--color-primary)' : 'var(--color-text-secondary)',
                fontWeight: active ? 600 : 400,
                boxShadow: active ? '0 1px 2px rgba(15,23,42,0.10)' : 'none',
              }}
            >
              <item.icon size={14} />
              {item.label}
            </button>
          );
        })}
      </div>

      {sub === 'reviews' ? (
        <ReviewsPanel
          key={'reviews-' + archiveToken}
          range={range}
          reloadToken={reloadToken}
          onOpenUser={onOpenUser}
          onChanged={bumpArchive}
        />
      ) : (
        <ProjectsPanel range={range} reloadToken={reloadToken} onOpenUser={onOpenUser} />
      )}
    </div>
  );
}