import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import {
  Activity, AlertTriangle, Ban, CheckCircle2, ChevronLeft, ChevronRight, Clock,
  Coins, Download, Gauge, Loader2, LogOut, RefreshCw, Search, Server,
  ShieldAlert, Users, Wifi, X, Zap,
} from 'lucide-react';
import {
  ADMIN_RANGES, adminApi,
  type AdminActivity, type AdminAlerts, type AdminOverview, type AdminPresence,
  type AdminPresenceUser, type AdminQuotas, type AdminRange, type AdminRunningTask,
  type AdminTaskList, type AdminTokens, type AdminTrendPoint, type AdminUserList,
  type AdminUserSummary, type AdminUserUsage,
} from '../services/api';

// ─────────────────────────── 样式常量 ───────────────────────────

const CARD: CSSProperties = {
  background: 'var(--color-surface)',
  border: '1px solid var(--color-border)',
  borderRadius: '10px',
  padding: '16px',
};

const TH: CSSProperties = {
  textAlign: 'left',
  padding: '8px 10px',
  fontWeight: 600,
  color: 'var(--color-text-secondary)',
  fontSize: '12px',
  borderBottom: '1px solid var(--color-border)',
  whiteSpace: 'nowrap',
  background: '#f8fafc',
};

const TD: CSSProperties = {
  padding: '8px 10px',
  fontSize: '13px',
  borderBottom: '1px solid #f1f5f9',
  verticalAlign: 'middle',
};

const BTN: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '6px',
  padding: '6px 12px',
  fontSize: '12px',
  borderRadius: '6px',
  border: '1px solid var(--color-border)',
  background: '#ffffff',
  color: 'var(--color-text)',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const BTN_PRIMARY: CSSProperties = { ...BTN, background: 'var(--color-primary)', borderColor: 'var(--color-primary)', color: '#ffffff' };

const INPUT: CSSProperties = {
  padding: '6px 10px',
  fontSize: '12px',
  borderRadius: '6px',
  border: '1px solid var(--color-border)',
  background: '#ffffff',
  color: 'var(--color-text)',
  outline: 'none',
};

const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  online: { label: '在线', color: '#059669', bg: '#ecfdf5' },
  busy: { label: '使用中', color: '#d97706', bg: '#fffbeb' },
  offline: { label: '离线', color: '#94a3b8', bg: '#f8fafc' },
};

const ACTIVITY_STATUS_META: Record<string, { color: string; bg: string }> = {
  running: { color: '#2563eb', bg: '#eff6ff' },
  success: { color: '#059669', bg: '#ecfdf5' },
  failed: { color: '#dc2626', bg: '#fef2f2' },
  rejected: { color: '#d97706', bg: '#fffbeb' },
};

const ALERT_META: Record<string, { color: string; bg: string; border: string; label: string }> = {
  danger: { color: '#b91c1c', bg: '#fef2f2', border: '#fecaca', label: '严重' },
  warning: { color: '#b45309', bg: '#fffbeb', border: '#fde68a', label: '警告' },
  info: { color: '#1d4ed8', bg: '#eff6ff', border: '#bfdbfe', label: '提示' },
};

type TabKey = 'overview' | 'presence' | 'users' | 'activities' | 'tokens' | 'alerts' | 'quotas';

const TABS: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: 'overview', label: '总览看板', icon: Gauge },
  { key: 'presence', label: '实时在线', icon: Wifi },
  { key: 'users', label: '用户用量', icon: Users },
  { key: 'activities', label: '行为流水', icon: Activity },
  { key: 'tokens', label: 'Token 消耗', icon: Coins },
  { key: 'alerts', label: '风险告警', icon: ShieldAlert },
  { key: 'quotas', label: '配额治理', icon: Zap },
];

const PRESENCE_POLL_MS = 5000;
const OVERVIEW_POLL_MS = 30000;

// ─────────────────────────── 格式化工具 ───────────────────────────

function fmtInt(value: number | null | undefined): string {
  const n = Number(value || 0);
  return n.toLocaleString('zh-CN');
}

function fmtCompact(value: number | null | undefined): string {
  const n = Number(value || 0);
  if (n >= 100000000) return (n / 100000000).toFixed(2) + ' 亿';
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function fmtPct(ratio: number | null | undefined): string {
  return (Number(ratio || 0) * 100).toFixed(1) + '%';
}

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const text = iso.replace('T', ' ');
  return text.length >= 19 ? text.slice(0, 19) : text;
}

function fmtShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const text = iso.replace('T', ' ');
  return text.length >= 16 ? text.slice(5, 16) : text;
}

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '—';
  const diff = Date.now() - time;
  if (diff < 0) return '刚刚';
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return sec + ' 秒前';
  const min = Math.floor(sec / 60);
  if (min < 60) return min + ' 分钟前';
  const hour = Math.floor(min / 60);
  if (hour < 24) return hour + ' 小时前';
  const day = Math.floor(hour / 24);
  if (day < 31) return day + ' 天前';
  return fmtDateTime(iso).slice(0, 10);
}

function fmtDuration(ms: number | null | undefined): string {
  const value = Number(ms || 0);
  if (!value) return '—';
  if (value < 1000) return value + ' ms';
  if (value < 60000) return (value / 1000).toFixed(1) + ' s';
  return Math.floor(value / 60000) + ' 分 ' + Math.round((value % 60000) / 1000) + ' 秒';
}

function fmtElapsed(seconds: number | null | undefined): string {
  const value = Number(seconds || 0);
  if (value < 60) return value.toFixed(0) + ' 秒';
  if (value < 3600) return Math.floor(value / 60) + ' 分 ' + Math.round(value % 60) + ' 秒';
  return Math.floor(value / 3600) + ' 小时 ' + Math.floor((value % 3600) / 60) + ' 分';
}

function errText(error: unknown): string {
  const anyErr = error as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr?.response?.data?.detail || anyErr?.message || '请求失败';
}

// ─────────────────────────── 通用小组件 ───────────────────────────

function Card({ title, extra, children, style }: { title?: string; extra?: ReactNode; children: ReactNode; style?: CSSProperties }) {
  return (
    <div style={{ ...CARD, ...style }}>
      {(title || extra) && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', gap: '12px' }}>
          <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)' }}>{title}</div>
          {extra}
        </div>
      )}
      {children}
    </div>
  );
}

function Kpi({ label, value, sub, icon: Icon, color }: { label: string; value: string; sub?: string; icon: typeof Activity; color: string }) {
  return (
    <div style={{ ...CARD, padding: '14px 16px', display: 'flex', alignItems: 'center', gap: '12px' }}>
      <div style={{ width: '36px', height: '36px', borderRadius: '9px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: color + '1a', color, flexShrink: 0 }}>
        <Icon size={18} />
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>{label}</div>
        <div style={{ fontSize: '20px', fontWeight: 700, lineHeight: 1.2, whiteSpace: 'nowrap' }}>{value}</div>
        {sub && <div style={{ fontSize: '11px', color: '#94a3b8', whiteSpace: 'nowrap' }}>{sub}</div>}
      </div>
    </div>
  );
}

function Pill({ text, color, bg }: { text: string; color: string; bg: string }) {
  return (
    <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: '999px', fontSize: '11px', fontWeight: 600, color, background: bg, whiteSpace: 'nowrap' }}>
      {text}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const meta = STATUS_META[status] || STATUS_META.offline;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '2px 8px', borderRadius: '999px', fontSize: '11px', fontWeight: 600, color: meta.color, background: meta.bg, whiteSpace: 'nowrap' }}>
      <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: meta.color, display: 'inline-block' }} />
      {meta.label}
    </span>
  );
}

function Spinner({ text }: { text?: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '32px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
      <Loader2 size={16} className="spin" />
      {text || '加载中…'}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div style={{ padding: '32px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>{text}</div>
  );
}

function RangePicker({ value, onChange }: { value: AdminRange; onChange: (range: AdminRange) => void }) {
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--color-border)', borderRadius: '7px', overflow: 'hidden', background: '#ffffff' }}>
      {ADMIN_RANGES.map((item) => (
        <button
          key={item.value}
          type="button"
          onClick={() => onChange(item.value)}
          style={{
            padding: '6px 12px', fontSize: '12px', border: 'none', cursor: 'pointer',
            background: value === item.value ? 'var(--color-primary)' : 'transparent',
            color: value === item.value ? '#ffffff' : 'var(--color-text-secondary)',
            fontWeight: value === item.value ? 600 : 400,
            borderRight: '1px solid var(--color-border)',
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function Pager({ total, page, pageSize, onPage }: { total: number; page: number; pageSize: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total === 0) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', padding: '10px 0 0', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
      <span>共 {fmtInt(total)} 条 · 第 {page} / {pages} 页</span>
      <span style={{ display: 'inline-flex', gap: '6px' }}>
        <button type="button" style={{ ...BTN, opacity: page <= 1 ? 0.45 : 1 }} disabled={page <= 1} onClick={() => onPage(page - 1)}>
          <ChevronLeft size={14} /> 上一页
        </button>
        <button type="button" style={{ ...BTN, opacity: page >= pages ? 0.45 : 1 }} disabled={page >= pages} onClick={() => onPage(page + 1)}>
          下一页 <ChevronRight size={14} />
        </button>
      </span>
    </div>
  );
}

// ─────────────────────────── 趋势图（纯 SVG，无第三方图表库） ───────────────────────────

type TrendMetric = 'actions' | 'active_users' | 'tokens';

const TREND_METRICS: Array<{ key: TrendMetric; label: string; color: string }> = [
  { key: 'actions', label: '操作次数', color: '#1a56db' },
  { key: 'active_users', label: '活跃用户', color: '#059669' },
  { key: 'tokens', label: 'Token 消耗', color: '#7c3aed' },
];

function TrendChart({ data, metric, onMetric }: { data: AdminTrendPoint[]; metric: TrendMetric; onMetric: (m: TrendMetric) => void }) {
  const meta = TREND_METRICS.find((m) => m.key === metric) || TREND_METRICS[0];
  const width = Math.max(560, data.length * 38);
  const height = 190;
  const padLeft = 46;
  const padRight = 14;
  const padTop = 14;
  const padBottom = 28;
  const innerW = width - padLeft - padRight;
  const innerH = height - padTop - padBottom;
  const values = data.map((d) => Number(d[metric] || 0));
  const max = Math.max(1, ...values);
  const step = data.length > 1 ? innerW / (data.length - 1) : innerW;
  const barW = Math.max(6, Math.min(24, step * 0.5));
  const yOf = (v: number) => padTop + innerH - (v / max) * innerH;
  const xOf = (i: number) => padLeft + (data.length > 1 ? i * step : innerW / 2);
  const labelEvery = Math.max(1, Math.ceil(data.length / 12));

  return (
    <div>
      <div style={{ display: 'flex', gap: '6px', marginBottom: '10px' }}>
        {TREND_METRICS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => onMetric(item.key)}
            style={{
              ...BTN, padding: '4px 10px',
              background: metric === item.key ? item.color : '#ffffff',
              color: metric === item.key ? '#ffffff' : 'var(--color-text-secondary)',
              borderColor: metric === item.key ? item.color : 'var(--color-border)',
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
      {data.length === 0 ? (
        <Empty text="该区间暂无数据" />
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <svg width={width} height={height} style={{ display: 'block' }}>
            {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
              const y = padTop + innerH - ratio * innerH;
              return (
                <g key={ratio}>
                  <line x1={padLeft} y1={y} x2={width - padRight} y2={y} stroke="#eef2f7" strokeWidth={1} />
                  <text x={padLeft - 8} y={y + 4} textAnchor="end" fontSize={10} fill="#94a3b8">
                    {fmtCompact(Math.round(max * ratio))}
                  </text>
                </g>
              );
            })}
            {data.map((point, index) => {
              const value = Number(point[metric] || 0);
              const barHeight = Math.max(value > 0 ? 2 : 0, padTop + innerH - yOf(value));
              return (
                <g key={point.date}>
                  <rect
                    x={xOf(index) - barW / 2}
                    y={padTop + innerH - barHeight}
                    width={barW}
                    height={barHeight}
                    rx={3}
                    fill={meta.color}
                    opacity={value > 0 ? 0.85 : 0.15}
                  >
                    <title>{point.date + ' · ' + meta.label + ' ' + fmtInt(value)}</title>
                  </rect>
                  {index % labelEvery === 0 && (
                    <text x={xOf(index)} y={height - 10} textAnchor="middle" fontSize={10} fill="#94a3b8">
                      {point.date.slice(5)}
                    </text>
                  )}
                </g>
              );
            })}
            <line x1={padLeft} y1={padTop + innerH} x2={width - padRight} y2={padTop + innerH} stroke="#cbd5e1" strokeWidth={1} />
          </svg>
        </div>
      )}
    </div>
  );
}

function taskPercent(progress: number | null | undefined): number {
  const value = Number(progress || 0);
  return Math.max(0, Math.min(100, Math.round(value <= 1 ? value * 100 : value)));
}

function TaskLine({ task }: { task: AdminRunningTask }) {
  const progress = taskPercent(task.progress);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
      <Loader2 size={12} className="spin" style={{ color: 'var(--color-warning)', flexShrink: 0 }} />
      <span style={{ color: 'var(--color-text)', fontWeight: 500 }}>{task.label || task.task_type}</span>
      <span style={{ flex: 1, minWidth: '40px', height: '4px', borderRadius: '2px', background: '#e2e8f0', overflow: 'hidden' }}>
        <span style={{ display: 'block', width: progress + '%', height: '100%', background: 'var(--color-warning)' }} />
      </span>
      <span style={{ whiteSpace: 'nowrap' }}>{fmtElapsed(task.elapsed_seconds)}</span>
    </div>
  );
}

// ─────────────────────────── 数据拉取 Hook ───────────────────────────

function usePoll<T>(loader: () => Promise<T>, deps: unknown[], intervalMs: number | null) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let cancelled = false;
    const run = async (isFirst: boolean) => {
      if (isFirst) setLoading(true);
      try {
        const result = await loaderRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (!cancelled) setError(errText(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run(true);
    const timer = intervalMs ? window.setInterval(() => run(false), intervalMs) : null;
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, intervalMs]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { data, loading, error, reload };
}

function ErrorBar({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 12px', borderRadius: '8px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', fontSize: '12px', marginBottom: '12px' }}>
      <AlertTriangle size={14} />
      {message}
    </div>
  );
}

// ─────────────────────────── 总览看板 ───────────────────────────

function OverviewTab({ range, autoRefresh, onOpenUser }: { range: AdminRange; autoRefresh: boolean; onOpenUser: (userId: string) => void }) {
  const [metric, setMetric] = useState<TrendMetric>('actions');
  const { data, loading, error } = usePoll<AdminOverview>(
    async () => (await adminApi.overview(range)).data,
    [range],
    autoRefresh ? OVERVIEW_POLL_MS : null,
  );
  const alertQuery = usePoll<AdminAlerts>(
    async () => (await adminApi.alerts(range === 'today' || range === '24h' ? range : '24h')).data,
    [range],
    autoRefresh ? OVERVIEW_POLL_MS : null,
  );

  if (loading && !data) return <Spinner text="正在汇总使用数据…" />;
  if (!data) return <><ErrorBar message={error} /><Empty text="暂无数据" /></>;

  const o = data;
  const kpiRow1 = [
    { label: '当前在线', value: fmtInt(o.presence.online), sub: '心跳窗口 ' + o.presence.online_window_seconds + ' 秒', icon: Wifi, color: '#059669' },
    { label: '正在使用', value: fmtInt(o.presence.busy), sub: '有在途任务', icon: Zap, color: '#d97706' },
    { label: '在途任务', value: fmtInt(o.presence.running_tasks), sub: '全平台异步任务', icon: Server, color: '#7c3aed' },
    { label: '今日活跃用户', value: fmtInt(o.today.active_users), sub: '登录 ' + fmtInt(o.today.logins) + ' 次 / 失败 ' + fmtInt(o.today.login_failures) + ' 次', icon: Users, color: '#1a56db' },
  ];
  const kpiRow2 = [
    { label: '今日操作数', value: fmtInt(o.today.actions), sub: '区间内 ' + fmtInt(o.range_stats.actions) + ' 次', icon: Activity, color: '#0ea5e9' },
    { label: '今日失败率', value: fmtPct(o.today.failure_rate), sub: '失败 ' + fmtInt(o.today.failed_actions) + ' 次', icon: ShieldAlert, color: '#dc2626' },
    { label: '今日 Token', value: fmtCompact(o.today.tokens.total_tokens), sub: '调用 ' + fmtInt(o.today.tokens.calls) + ' 次', icon: Coins, color: '#f59e0b' },
    { label: '账号总数', value: fmtInt(o.users.total), sub: '启用 ' + fmtInt(o.users.active) + ' / 禁用 ' + fmtInt(o.users.disabled), icon: Gauge, color: '#475569' },
  ];

  return (
    <div>
      <ErrorBar message={error} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        {kpiRow1.map((kpi) => <Kpi key={kpi.label} {...kpi} />)}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        {kpiRow2.map((kpi) => <Kpi key={kpi.label} {...kpi} />)}
      </div>

      <Card
        title={'按天趋势 · ' + (o.timezone || '')}
        extra={<span style={{ fontSize: '11px', color: '#94a3b8' }}>更新于 {fmtDateTime(o.generated_at)}</span>}
        style={{ marginBottom: '12px' }}
      >
        <TrendChart data={o.trend} metric={metric} onMetric={setMetric} />
      </Card>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: '12px' }}>
        <Card title="操作类型分布（区间内）">
          {o.action_breakdown.length === 0 ? <Empty text="该区间暂无操作记录" /> : (
            <div style={{ maxHeight: '320px', overflowY: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={TH}>操作</th>
                    <th style={{ ...TH, textAlign: 'right' }}>次数</th>
                    <th style={{ ...TH, textAlign: 'right' }}>失败</th>
                    <th style={{ ...TH, textAlign: 'right' }}>平均耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {o.action_breakdown.map((row) => (
                    <tr key={row.action}>
                      <td style={TD}>
                        <div style={{ fontWeight: 500 }}>{row.label || row.action}</div>
                        <div style={{ fontSize: '11px', color: '#94a3b8', fontFamily: 'monospace' }}>{row.action}</div>
                      </td>
                      <td style={{ ...TD, textAlign: 'right', fontWeight: 600 }}>{fmtInt(row.count)}</td>
                      <td style={{ ...TD, textAlign: 'right', color: row.failed > 0 ? 'var(--color-danger)' : '#94a3b8' }}>{fmtInt(row.failed)}</td>
                      <td style={{ ...TD, textAlign: 'right', color: 'var(--color-text-secondary)' }}>{fmtDuration(row.avg_duration_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <Card title="用量 TOP 10（区间内）">
            {o.top_users.length === 0 ? <Empty text="该区间暂无用户操作" /> : (
              <div>
                {o.top_users.map((user, index) => {
                  const top = o.top_users[0]?.count || 1;
                  return (
                    <button
                      key={user.user_id}
                      type="button"
                      onClick={() => onOpenUser(user.user_id)}
                      style={{ display: 'flex', alignItems: 'center', gap: '10px', width: '100%', padding: '6px 0', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left' }}
                    >
                      <span style={{ width: '18px', fontSize: '11px', color: '#94a3b8', fontWeight: 600 }}>{index + 1}</span>
                      <span style={{ width: '110px', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user.name}</span>
                      <span style={{ flex: 1, height: '6px', borderRadius: '3px', background: '#f1f5f9', overflow: 'hidden' }}>
                        <span style={{ display: 'block', width: Math.round((user.count / top) * 100) + '%', height: '100%', background: 'var(--color-primary)' }} />
                      </span>
                      <span style={{ width: '52px', textAlign: 'right', fontSize: '12px', fontWeight: 600 }}>{fmtInt(user.count)}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title={'风险告警（近24小时 · ' + fmtInt(alertQuery.data?.count || 0) + ' 条）'}>
            {!alertQuery.data ? <Spinner text="加载告警…" /> : alertQuery.data.alerts.length === 0 ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '12px 0', color: 'var(--color-success)', fontSize: '13px' }}>
                <CheckCircle2 size={16} /> 暂无风险项
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '180px', overflowY: 'auto' }}>
                {alertQuery.data.alerts.slice(0, 6).map((alert, index) => {
                  const meta = ALERT_META[alert.level] || ALERT_META.info;
                  return (
                    <div key={index} style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', padding: '7px 9px', borderRadius: '7px', background: meta.bg, border: '1px solid ' + meta.border }}>
                      <span style={{ fontSize: '11px', fontWeight: 700, color: meta.color, flexShrink: 0, marginTop: '1px' }}>{meta.label}</span>
                      <span style={{ fontSize: '12px', color: 'var(--color-text)', lineHeight: 1.5 }}>{alert.message}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────── 实时在线 ───────────────────────────

function PresenceTab({ autoRefresh, onOpenUser }: { autoRefresh: boolean; onOpenUser: (userId: string) => void }) {
  const [includeOffline, setIncludeOffline] = useState(false);
  const interval = autoRefresh ? PRESENCE_POLL_MS : null;
  const presenceQuery = usePoll<AdminPresence>(
    async () => (await adminApi.presence(includeOffline)).data,
    [includeOffline],
    interval,
  );
  const taskQuery = usePoll<AdminTaskList>(
    async () => (await adminApi.tasks()).data,
    [],
    interval,
  );

  const presence = presenceQuery.data;
  const tasks = taskQuery.data;

  return (
    <div>
      <ErrorBar message={presenceQuery.error || taskQuery.error} />
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', flexWrap: 'wrap' }}>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
          <input type="checkbox" checked={includeOffline} onChange={(e) => setIncludeOffline(e.target.checked)} />
          同时显示离线用户
        </label>
        <span style={{ fontSize: '11px', color: '#94a3b8' }}>
          {autoRefresh ? '每 ' + PRESENCE_POLL_MS / 1000 + ' 秒自动刷新' : '自动刷新已关闭'}
          {presence ? ' · 更新于 ' + fmtDateTime(presence.generated_at) : ''}
        </span>
        <button type="button" style={{ ...BTN, marginLeft: 'auto' }} onClick={() => { presenceQuery.reload(); taskQuery.reload(); }}>
          <RefreshCw size={13} /> 立即刷新
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        <Kpi label="在线人数" value={fmtInt(presence?.online || 0)} icon={Wifi} color="#059669" />
        <Kpi label="正在使用" value={fmtInt(presence?.busy || 0)} sub="有任务在跑" icon={Zap} color="#d97706" />
        <Kpi label="在途任务" value={fmtInt(presence?.running_tasks || 0)} icon={Server} color="#7c3aed" />
        <Kpi label="心跳窗口" value={(presence?.online_window_seconds || 0) + ' 秒'} sub="超过即判定离线" icon={Clock} color="#475569" />
      </div>

      <Card title={'当前在线（' + fmtInt(presence?.users.filter((u) => u.status !== 'offline').length || 0) + ' 人）'} style={{ marginBottom: '12px' }}>
        {!presence ? <Spinner text="正在获取在线状态…" /> : presence.users.length === 0 ? <Empty text="当前没有人在线" /> : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '10px' }}>
            {presence.users.map((user) => <PresenceCard key={user.user_id} user={user} onOpen={() => onOpenUser(user.user_id)} />)}
          </div>
        )}
      </Card>

      <Card title={'全平台在途任务（' + fmtInt(tasks?.count || 0) + ' 个）'}>
        {!tasks ? <Spinner text="正在获取任务…" /> : tasks.tasks.length === 0 ? (
          <Empty text="当前没有在途任务。任务表存在进程内存中，服务重启后会清空。" />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={TH}>归属人</th>
                  <th style={TH}>任务</th>
                  <th style={TH}>进度</th>
                  <th style={{ ...TH, textAlign: 'right' }}>已运行</th>
                  <th style={TH}>提交时间</th>
                  <th style={TH}>任务ID</th>
                </tr>
              </thead>
              <tbody>
                {tasks.tasks.map((task) => (
                  <tr key={task.task_id}>
                    <td style={TD}>
                      {task.owner_id ? (
                        <button type="button" onClick={() => onOpenUser(task.owner_id as string)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--color-primary)', fontSize: '13px', padding: 0 }}>
                          {task.owner_name}
                        </button>
                      ) : (
                        <span style={{ color: '#94a3b8' }}>{task.owner_name || '（未归属）'}</span>
                      )}
                    </td>
                    <td style={TD}>
                      <div style={{ fontWeight: 500 }}>{task.label || task.task_type}</div>
                      {task.progress_message && <div style={{ fontSize: '11px', color: '#94a3b8' }}>{task.progress_message}</div>}
                    </td>
                    <td style={{ ...TD, width: '140px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span style={{ flex: 1, height: '5px', borderRadius: '3px', background: '#e2e8f0', overflow: 'hidden' }}>
                          <span style={{ display: 'block', height: '100%', width: taskPercent(task.progress) + '%', background: 'var(--color-primary)' }} />
                        </span>
                        <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)', width: '32px', textAlign: 'right' }}>{taskPercent(task.progress)}%</span>
                      </div>
                    </td>
                    <td style={{ ...TD, textAlign: 'right', whiteSpace: 'nowrap' }}>{fmtElapsed(task.elapsed_seconds)}</td>
                    <td style={{ ...TD, whiteSpace: 'nowrap', color: 'var(--color-text-secondary)' }}>{fmtShort(task.submitted_at)}</td>
                    <td style={{ ...TD, fontFamily: 'monospace', fontSize: '11px', color: '#94a3b8' }}>{task.task_id.slice(0, 8)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function PresenceCard({ user, onOpen }: { user: AdminPresenceUser; onOpen: () => void }) {
  return (
    <div style={{ border: '1px solid var(--color-border)', borderRadius: '9px', padding: '11px 12px', background: user.status === 'offline' ? '#fcfdfe' : '#ffffff' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
        <button type="button" onClick={onOpen} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 600, color: 'var(--color-text)' }}>
          {user.name}
        </button>
        <span style={{ fontSize: '11px', color: '#94a3b8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{user.email}</span>
        <StatusPill status={user.status} />
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '6px' }}>
        {(user.roles.length ? user.roles : [user.is_active ? '未分配角色' : '已禁用']).map((role) => (
          <span key={role} style={{ fontSize: '10px', padding: '1px 7px', borderRadius: '999px', background: '#f1f5f9', color: '#64748b' }}>{role}</span>
        ))}
      </div>
      <div style={{ fontSize: '11px', color: '#94a3b8', marginBottom: user.tasks.length ? '6px' : 0 }}>
        最后心跳 {fmtRelative(user.last_seen_at)} · 最后登录 {fmtRelative(user.last_login_at)}
      </div>
      {user.tasks.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', paddingTop: '6px', borderTop: '1px dashed #e2e8f0' }}>
          {user.tasks.map((task) => <TaskLine key={task.task_id} task={task} />)}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────── 用户用量 ───────────────────────────

const SORTABLE_COLUMNS: Array<{ key: string; label: string; align?: 'right' }> = [
  { key: 'name', label: '用户' },
  { key: 'last_login_at', label: '最后登录' },
  { key: 'last_seen_at', label: '最后心跳' },
  { key: 'actions', label: '操作数', align: 'right' },
  { key: 'failed', label: '失败', align: 'right' },
  { key: 'active_days', label: '活跃天', align: 'right' },
  { key: 'checks', label: '检查次数', align: 'right' },
  { key: 'tokens', label: 'Token', align: 'right' },
  { key: 'today_actions', label: '今日操作', align: 'right' },
];

function UsersTab({
  range, autoRefresh, reloadToken, onOpenUser,
}: {
  range: AdminRange;
  autoRefresh: boolean;
  reloadToken: number;
  onOpenUser: (userId: string) => void;
}) {
  const [keyword, setKeyword] = useState('');
  const [debounced, setDebounced] = useState('');
  const [role, setRole] = useState('');
  const [presenceFilter, setPresenceFilter] = useState<'' | 'online' | 'offline' | 'busy' | 'disabled'>('');
  const [sort, setSort] = useState('last_seen_at');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [roleOptions, setRoleOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => { setDebounced(keyword.trim()); setPage(1); }, 350);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  const query = usePoll<AdminUserList>(
    async () => (await adminApi.users({
      range, q: debounced, role, presence: presenceFilter,
      sort, order, page, page_size: pageSize,
    })).data,
    [range, debounced, role, presenceFilter, sort, order, page, pageSize, reloadToken],
    autoRefresh ? OVERVIEW_POLL_MS : null,
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await adminApi.quotas();
        if (cancelled) return;
        const seen = new Map<string, string>();
        for (const row of res.data.quotas) {
          for (const name of row.roles) if (!seen.has(name)) seen.set(name, name);
        }
        setRoleOptions(Array.from(seen.entries()).map(([value, label]) => ({ value, label })));
      } catch {
        // 没有 RBAC 查看权限时静默降级：角色筛选留空即可
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const toggleSort = (key: string) => {
    if (sort === key) { setOrder(order === 'desc' ? 'asc' : 'desc'); } else { setSort(key); setOrder('desc'); }
    setPage(1);
  };

  const runAction = async (userId: string, label: string, action: () => Promise<unknown>) => {
    setBusyId(userId);
    setNotice(null);
    try {
      await action();
      setNotice(label + '成功');
      query.reload();
    } catch (err) {
      setNotice(label + '失败：' + errText(err));
    } finally {
      setBusyId(null);
      window.setTimeout(() => setNotice(null), 4000);
    }
  };

  const doExport = async () => {
    setExporting(true);
    try {
      const stamp = new Date().toISOString().slice(0, 10);
      await adminApi.exportCsv('users', { range, q: debounced || undefined, role: role || undefined, presence: presenceFilter || undefined }, '使用监控_用户用量_' + range + '_' + stamp + '.csv');
    } catch (err) {
      setNotice('导出失败：' + errText(err));
      window.setTimeout(() => setNotice(null), 4000);
    } finally {
      setExporting(false);
    }
  };

  const rows = query.data?.users || [];

  return (
    <div>
      <ErrorBar message={query.error} />
      <Card style={{ marginBottom: '12px', padding: '12px 16px' }}>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ position: 'relative' }}>
            <Search size={13} style={{ position: 'absolute', left: '9px', top: '50%', transform: 'translateY(-50%)', color: '#94a3b8' }} />
            <input
              style={{ ...INPUT, paddingLeft: '28px', width: '190px' }}
              placeholder="搜索姓名 / 邮箱"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
          </div>
          <select style={INPUT} value={role} onChange={(e) => { setRole(e.target.value); setPage(1); }}>
            <option value="">全部角色</option>
            {roleOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <select style={INPUT} value={presenceFilter} onChange={(e) => { setPresenceFilter(e.target.value as typeof presenceFilter); setPage(1); }}>
            <option value="">全部状态</option>
            <option value="online">在线</option>
            <option value="busy">使用中</option>
            <option value="offline">离线</option>
            <option value="disabled">已禁用</option>
          </select>
          <select style={INPUT} value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}>
            {[20, 50, 100, 200].map((size) => <option key={size} value={size}>每页 {size} 条</option>)}
          </select>
          {notice && <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{notice}</span>}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
            <button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
            <button type="button" style={BTN} onClick={doExport} disabled={exporting}>
              <Download size={13} /> {exporting ? '导出中…' : '导出 CSV'}
            </button>
          </div>
        </div>
      </Card>

      <Card title={'用户用量明细（共 ' + fmtInt(query.data?.total || 0) + ' 人 · 区间 ' + range + '）'}>
        {query.loading && !query.data ? <Spinner text="正在加载用户用量…" /> : rows.length === 0 ? <Empty text="没有匹配的用户" /> : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    {SORTABLE_COLUMNS.map((column) => (
                      <th
                        key={column.key}
                        style={{ ...TH, textAlign: column.align || 'left', cursor: 'pointer', userSelect: 'none' }}
                        onClick={() => toggleSort(column.key)}
                      >
                        {column.label}
                        {sort === column.key && <span style={{ marginLeft: '3px' }}>{order === 'desc' ? '↓' : '↑'}</span>}
                      </th>
                    ))}
                    <th style={TH}>状态</th>
                    <th style={{ ...TH, textAlign: 'right' }}>今日配额</th>
                    <th style={{ ...TH, textAlign: 'right' }}>管理动作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((user) => (
                    <UserRow
                      key={user.id}
                      user={user}
                      busy={busyId === user.id}
                      onOpen={() => onOpenUser(user.id)}
                      onForceLogout={() => runAction(user.id, '强制下线', () => adminApi.forceLogout(user.id))}
                      onToggleActive={() => runAction(user.id, user.is_active ? '禁用' : '启用', () => adminApi.setUserStatus(user.id, !user.is_active))}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <Pager total={query.data?.total || 0} page={query.data?.page || page} pageSize={query.data?.page_size || pageSize} onPage={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}

function UserRow({
  user, busy, onOpen, onForceLogout, onToggleActive,
}: {
  user: AdminUserUsage;
  busy: boolean;
  onOpen: () => void;
  onForceLogout: () => void;
  onToggleActive: () => void;
}) {
  const actionLimit = user.quota.daily_action_limit;
  const tokenLimit = user.quota.daily_token_limit;
  const actionRatio = actionLimit > 0 ? user.quota.today_actions / actionLimit : 0;
  const tokenRatio = tokenLimit > 0 ? user.quota.today_tokens / tokenLimit : 0;
  const ratio = Math.max(actionRatio, tokenRatio);

  return (
    <tr style={{ opacity: user.is_active ? 1 : 0.6 }}>
      <td style={TD}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button type="button" onClick={onOpen} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 600, color: 'var(--color-primary)', textAlign: 'left' }}>
            {user.name}
          </button>
          {!user.is_active && <Pill text="已禁用" color="#b91c1c" bg="#fef2f2" />}
        </div>
        <div style={{ fontSize: '11px', color: '#94a3b8' }}>{user.email}</div>
        <div style={{ display: 'flex', gap: '3px', flexWrap: 'wrap', marginTop: '2px' }}>
          {(user.roles.length ? user.roles : [user.role || '无角色']).map((role) => (
            <span key={role} style={{ fontSize: '10px', padding: '0 6px', borderRadius: '999px', background: '#f1f5f9', color: '#64748b' }}>{role}</span>
          ))}
        </div>
      </td>
      <td style={{ ...TD, whiteSpace: 'nowrap', color: 'var(--color-text-secondary)', fontSize: '12px' }}>{fmtShort(user.last_login_at)}</td>
      <td style={{ ...TD, whiteSpace: 'nowrap', color: 'var(--color-text-secondary)', fontSize: '12px' }}>{fmtRelative(user.last_seen_at)}</td>
      <td style={{ ...TD, textAlign: 'right', fontWeight: 600 }}>{fmtInt(user.actions)}</td>
      <td style={{ ...TD, textAlign: 'right', color: user.failed > 0 ? 'var(--color-danger)' : '#94a3b8' }}>{fmtInt(user.failed)}</td>
      <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(user.active_days)}</td>
      <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(user.checks)}</td>
      <td style={{ ...TD, textAlign: 'right' }} title={'输入 ' + fmtInt(user.prompt_tokens) + ' / 输出 ' + fmtInt(user.completion_tokens)}>
        {fmtCompact(user.tokens)}
      </td>
      <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(user.today_actions)}</td>
      <td style={TD}><StatusPill status={user.status} /></td>
      <td style={{ ...TD, textAlign: 'right', minWidth: '130px' }}>
        {actionLimit <= 0 && tokenLimit <= 0 ? (
          <span style={{ fontSize: '11px', color: '#94a3b8' }}>不限</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px', alignItems: 'flex-end' }}>
            <span style={{ fontSize: '11px', color: ratio >= 0.8 ? 'var(--color-danger)' : 'var(--color-text-secondary)' }}>
              {fmtInt(user.quota.today_actions)}/{actionLimit > 0 ? fmtInt(actionLimit) : '∞'} 次
            </span>
            <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>
              {fmtCompact(user.quota.today_tokens)}/{tokenLimit > 0 ? fmtCompact(tokenLimit) : '∞'} tk
            </span>
          </div>
        )}
      </td>
      <td style={{ ...TD, textAlign: 'right', whiteSpace: 'nowrap' }}>
        <button type="button" style={{ ...BTN, padding: '3px 8px', marginRight: '4px' }} disabled={busy} onClick={onForceLogout} title="吊销其全部登录会话，下次请求即需重新登录">
          <LogOut size={12} /> 下线
        </button>
        <button
          type="button"
          style={{ ...BTN, padding: '3px 8px', color: user.is_active ? 'var(--color-danger)' : 'var(--color-success)' }}
          disabled={busy}
          onClick={onToggleActive}
          title={user.is_active ? '禁用后无法登录，且立即吊销全部会话' : '恢复该账号登录'}
        >
          {user.is_active ? <Ban size={12} /> : <CheckCircle2 size={12} />}
          {user.is_active ? '禁用' : '启用'}
        </button>
      </td>
    </tr>
  );
}

// ─────────────────────────── 用户详情抽屉 ───────────────────────────

function UserDrawer({ userId, range, onClose, onChanged }: { userId: string; range: AdminRange; onClose: () => void; onChanged: () => void }) {
  const summaryQuery = usePoll<AdminUserSummary>(
    async () => (await adminApi.userSummary(userId, range)).data,
    [userId, range],
    null,
  );
  const activityQuery = usePoll<{ total: number; activities: AdminActivity[] }>(
    async () => {
      const res = await adminApi.userActivities(userId, { range, limit: 50, offset: 0 });
      return { total: res.data.total, activities: res.data.activities };
    },
    [userId, range],
    null,
  );

  const summary = summaryQuery.data;
  const [actionLimit, setActionLimit] = useState('0');
  const [tokenLimit, setTokenLimit] = useState('0');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [drawerNotice, setDrawerNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!summary) return;
    setActionLimit(String(summary.quota.daily_action_limit || 0));
    setTokenLimit(String(summary.quota.daily_token_limit || 0));
    setNote(summary.quota.note || '');
  }, [summary]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const flash = (text: string) => {
    setDrawerNotice(text);
    window.setTimeout(() => setDrawerNotice(null), 4000);
  };

  const saveQuota = async () => {
    setSaving(true);
    try {
      await adminApi.updateQuota(userId, {
        daily_action_limit: Math.max(0, Number(actionLimit) || 0),
        daily_token_limit: Math.max(0, Number(tokenLimit) || 0),
        note: note.trim() || null,
      });
      flash('配额已保存');
      summaryQuery.reload();
      onChanged();
    } catch (err) {
      flash('保存失败：' + errText(err));
    } finally {
      setSaving(false);
    }
  };

  const manage = async (label: string, action: () => Promise<unknown>) => {
    setSaving(true);
    try {
      await action();
      flash(label + '成功');
      summaryQuery.reload();
      onChanged();
    } catch (err) {
      flash(label + '失败：' + errText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.35)', zIndex: 900 }} />
      <div style={{ position: 'fixed', top: 0, right: 0, bottom: 0, width: '620px', maxWidth: '94vw', background: 'var(--color-bg)', zIndex: 901, boxShadow: '-8px 0 28px rgba(15,23,42,0.18)', display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '14px 18px', background: 'var(--color-surface)', borderBottom: '1px solid var(--color-border)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontSize: '15px', fontWeight: 700 }}>{summary?.user.name || '用户详情'}</span>
              {summary && <StatusPill status={summary.user.status} />}
              {summary && !summary.user.is_active && <Pill text="已禁用" color="#b91c1c" bg="#fef2f2" />}
            </div>
            <div style={{ fontSize: '12px', color: '#94a3b8' }}>{summary?.user.email || userId}</div>
          </div>
          <button type="button" style={{ ...BTN, padding: '5px 8px' }} onClick={onClose}><X size={14} /></button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '14px 18px' }}>
          <ErrorBar message={summaryQuery.error} />
          {drawerNotice && (
            <div style={{ padding: '8px 12px', borderRadius: '7px', background: '#eff6ff', border: '1px solid #bfdbfe', color: '#1d4ed8', fontSize: '12px', marginBottom: '12px' }}>
              {drawerNotice}
            </div>
          )}
          {!summary ? <Spinner text="正在加载用户详情…" /> : (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: '10px', marginBottom: '12px' }}>
                <Kpi label={'区间操作（' + range + '）'} value={fmtInt(summary.range_stats.total)} sub={'失败 ' + fmtInt(summary.range_stats.failed)} icon={Activity} color="#1a56db" />
                <Kpi label="区间失败率" value={fmtPct(summary.range_stats.failure_rate)} sub={'被拒 ' + fmtInt(summary.range_stats.rejected)} icon={ShieldAlert} color="#dc2626" />
                <Kpi label="区间 Token" value={fmtCompact(summary.range_stats.tokens.total_tokens)} sub={'调用 ' + fmtInt(summary.range_stats.tokens.calls) + ' 次'} icon={Coins} color="#7c3aed" />
                <Kpi label="今日操作" value={fmtInt(summary.today.total)} sub={'今日 Token ' + fmtCompact(summary.today.tokens.total_tokens)} icon={Zap} color="#d97706" />
              </div>

              <Card title="账号信息" style={{ marginBottom: '12px' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 16px', fontSize: '12px' }}>
                  <InfoRow label="最后登录" value={fmtDateTime(summary.user.last_login_at)} />
                  <InfoRow label="最后心跳" value={fmtDateTime(summary.user.last_seen_at)} />
                  <InfoRow label="注册时间" value={fmtDateTime(summary.user.created_at)} />
                  <InfoRow label="系统角色" value={summary.user.role || '—'} />
                </div>
              </Card>

              <Card title={'当前在途任务（' + summary.current_tasks.length + '）'} style={{ marginBottom: '12px' }}>
                {summary.current_tasks.length === 0 ? <Empty text="此刻没有正在跑的任务" /> : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {summary.current_tasks.map((task) => <TaskLine key={task.task_id} task={task} />)}
                  </div>
                )}
              </Card>

              <Card title={'活跃登录会话（' + summary.sessions.length + '）'} style={{ marginBottom: '12px' }}>
                {summary.sessions.length === 0 ? <Empty text="没有活跃会话" /> : (
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                      <tr>
                        <th style={TH}>IP</th>
                        <th style={TH}>客户端</th>
                        <th style={TH}>最后心跳</th>
                        <th style={TH}>状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.sessions.map((session) => (
                        <tr key={session.id}>
                          <td style={{ ...TD, fontFamily: 'monospace', fontSize: '12px' }}>{session.client_ip || '—'}</td>
                          <td style={{ ...TD, fontSize: '11px', color: 'var(--color-text-secondary)', maxWidth: '230px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={session.user_agent || ''}>
                            {session.user_agent || '—'}
                          </td>
                          <td style={{ ...TD, fontSize: '12px', whiteSpace: 'nowrap' }}>{fmtRelative(session.last_seen_at)}</td>
                          <td style={TD}>{session.is_online ? <Pill text="在线" color="#059669" bg="#ecfdf5" /> : <Pill text="挂起" color="#94a3b8" bg="#f8fafc" />}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>

              <Card title={'操作类型分布（区间 ' + range + '）'} style={{ marginBottom: '12px' }}>
                {summary.action_breakdown.length === 0 ? <Empty text="该区间暂无操作" /> : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                    {summary.action_breakdown.map((row) => {
                      const top = summary.action_breakdown[0]?.count || 1;
                      return (
                        <div key={row.action} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                          <span style={{ width: '150px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.label || row.action}</span>
                          <span style={{ flex: 1, height: '6px', borderRadius: '3px', background: '#f1f5f9', overflow: 'hidden' }}>
                            <span style={{ display: 'block', width: Math.round((row.count / top) * 100) + '%', height: '100%', background: 'var(--color-primary)' }} />
                          </span>
                          <span style={{ width: '44px', textAlign: 'right', fontWeight: 600 }}>{fmtInt(row.count)}</span>
                          <span style={{ width: '44px', textAlign: 'right', color: row.failed ? 'var(--color-danger)' : '#94a3b8' }}>{fmtInt(row.failed)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>

              <Card title="每日配额（0 表示不限）" style={{ marginBottom: '12px' }}>
                <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
                  <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                    操作次数上限
                    <input style={{ ...INPUT, width: '110px', display: 'block', marginTop: '4px' }} value={actionLimit} onChange={(e) => setActionLimit(e.target.value)} inputMode="numeric" />
                  </label>
                  <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                    Token 上限
                    <input style={{ ...INPUT, width: '130px', display: 'block', marginTop: '4px' }} value={tokenLimit} onChange={(e) => setTokenLimit(e.target.value)} inputMode="numeric" />
                  </label>
                  <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', flex: 1, minWidth: '160px' }}>
                    备注
                    <input style={{ ...INPUT, width: '100%', display: 'block', marginTop: '4px' }} value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：试用期限制" />
                  </label>
                  <button type="button" style={BTN_PRIMARY} onClick={saveQuota} disabled={saving}>保存配额</button>
                </div>
                <div style={{ marginTop: '10px', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                  今日已用：操作 {fmtInt(summary.quota.today_actions)} 次 · Token {fmtCompact(summary.quota.today_tokens)}
                  {summary.quota.customized ? ' · 已单独设置' : ' · 使用系统默认'}
                </div>
              </Card>

              <Card title={'最近行为流水（' + fmtInt(activityQuery.data?.total || 0) + ' 条）'} style={{ marginBottom: '12px' }}>
                {!activityQuery.data ? <Spinner text="加载中…" /> : activityQuery.data.activities.length === 0 ? <Empty text="暂无行为记录" /> : (
                  <ActivityTable activities={activityQuery.data.activities} compact />
                )}
              </Card>

              <Card title="管理动作">
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  <button type="button" style={BTN} disabled={saving} onClick={() => manage('强制下线', () => adminApi.forceLogout(userId))}>
                    <LogOut size={13} /> 强制下线（吊销全部会话）
                  </button>
                  <button
                    type="button"
                    style={{ ...BTN, color: summary.user.is_active ? 'var(--color-danger)' : 'var(--color-success)' }}
                    disabled={saving}
                    onClick={() => manage(summary.user.is_active ? '禁用' : '启用', () => adminApi.setUserStatus(userId, !summary.user.is_active))}
                  >
                    {summary.user.is_active ? <Ban size={13} /> : <CheckCircle2 size={13} />}
                    {summary.user.is_active ? '禁用该账号' : '启用该账号'}
                  </button>
                </div>
              </Card>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: '8px' }}>
      <span style={{ color: '#94a3b8', width: '64px', flexShrink: 0 }}>{label}</span>
      <span style={{ color: 'var(--color-text)' }}>{value}</span>
    </div>
  );
}


// ─────────────────────────── 行为流水 ───────────────────────────

const ACTIVITY_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '进行中' },
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'rejected', label: '被拒绝' },
];

function ActivitiesTab({ range, onOpenUser }: { range: AdminRange; onOpenUser: (userId: string) => void }) {
  const [userId, setUserId] = useState('');
  const [action, setAction] = useState('');
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [userOptions, setUserOptions] = useState<Array<{ id: string; name: string; email: string }>>([]);
  const limit = 50;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await adminApi.users({ range, page: 1, page_size: 200, sort: 'name', order: 'asc' });
        if (!cancelled) setUserOptions(res.data.users.map((u) => ({ id: u.id, name: u.name, email: u.email })));
      } catch {
        /* 拉不到用户列表就只留「全部用户」 */
      }
    })();
    return () => { cancelled = true; };
  }, [range]);

  useEffect(() => { setOffset(0); }, [range, userId, action, status]);

  const query = usePoll<{ total: number; activities: AdminActivity[]; actions: Array<[string, string]> }>(
    async () => {
      const res = await adminApi.activities({ range, user_id: userId || undefined, action: action || undefined, status: status || undefined, limit, offset });
      return { total: res.data.total, activities: res.data.activities, actions: res.data.actions || [] };
    },
    [range, userId, action, status, offset],
    null,
  );

  const doExport = async () => {
    setExporting(true);
    try {
      const stamp = new Date().toISOString().slice(0, 10);
      await adminApi.exportCsv('activities', {
        range, user_id: userId || undefined, action: action || undefined, status: status || undefined,
      }, '使用监控_行为流水_' + range + '_' + stamp + '.csv');
    } catch (err) {
      setNotice('导出失败：' + errText(err));
      window.setTimeout(() => setNotice(null), 4000);
    } finally {
      setExporting(false);
    }
  };

  const total = query.data?.total || 0;
  const actionOptions = useMemo(() => {
    const list = query.data?.actions || [];
    return list.map(([code, label]) => ({ value: code, label: label + '（' + code + '）' }));
  }, [query.data]);

  return (
    <div>
      <ErrorBar message={query.error} />
      <Card style={{ marginBottom: '12px', padding: '12px 16px' }}>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <select style={{ ...INPUT, maxWidth: '210px' }} value={userId} onChange={(e) => setUserId(e.target.value)}>
            <option value="">全部用户</option>
            {userOptions.map((user) => <option key={user.id} value={user.id}>{user.name}（{user.email}）</option>)}
          </select>
          <select style={{ ...INPUT, maxWidth: '230px' }} value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">全部操作类型</option>
            {actionOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <select style={INPUT} value={status} onChange={(e) => setStatus(e.target.value)}>
            {ACTIVITY_STATUS_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          {notice && <span style={{ fontSize: '12px', color: 'var(--color-danger)' }}>{notice}</span>}
          <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
            <button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
            <button type="button" style={BTN} onClick={doExport} disabled={exporting}>
              <Download size={13} /> {exporting ? '导出中…' : '导出 CSV'}
            </button>
          </div>
        </div>
        <div style={{ marginTop: '8px', fontSize: '11px', color: '#94a3b8' }}>
          点击任意一行可展开查看项目名、标书文件名、错误信息与原始 detail。
        </div>
      </Card>

      <Card title={'行为流水（共 ' + fmtInt(total) + ' 条 · 区间 ' + range + '）'}>
        {query.loading && !query.data ? <Spinner text="正在加载行为流水…" /> : (query.data?.activities.length || 0) === 0 ? (
          <Empty text="该条件下没有行为记录" />
        ) : (
          <>
            <ActivityTable activities={query.data?.activities || []} onOpenUser={onOpenUser} />
            <Pager total={total} page={Math.floor(offset / limit) + 1} pageSize={limit} onPage={(next) => setOffset((next - 1) * limit)} />
          </>
        )}
      </Card>
    </div>
  );
}

function ActivityTable({
  activities, compact, onOpenUser,
}: {
  activities: AdminActivity[];
  compact?: boolean;
  onOpenUser?: (userId: string) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const colSpan = compact ? 6 : 8;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={TH}>时间</th>
            {!compact && <th style={TH}>用户</th>}
            <th style={TH}>操作</th>
            <th style={TH}>状态</th>
            <th style={{ ...TH, textAlign: 'right' }}>耗时</th>
            <th style={TH}>项目名</th>
            <th style={TH}>标书 / 资源名</th>
            {!compact && <th style={TH}>IP</th>}
          </tr>
        </thead>
        <tbody>
          {activities.map((row) => {
            const meta = ACTIVITY_STATUS_META[row.status] || ACTIVITY_STATUS_META.success;
            const isOpen = expanded === row.id;
            return (
              <Fragment key={row.id}>
                <tr style={{ cursor: 'pointer' }} onClick={() => setExpanded(isOpen ? null : row.id)}>
                  <td style={{ ...TD, whiteSpace: 'nowrap', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                    {compact ? fmtShort(row.created_at) : fmtDateTime(row.created_at)}
                  </td>
                  {!compact && (
                    <td style={TD}>
                      {row.user_id && onOpenUser ? (
                        <button
                          type="button"
                          onClick={(event) => { event.stopPropagation(); onOpenUser(row.user_id as string); }}
                          style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 500, color: 'var(--color-primary)' }}
                        >
                          {row.user_name || '未知用户'}
                        </button>
                      ) : (
                        <span style={{ fontWeight: 500, color: row.user_id ? 'var(--color-text)' : '#94a3b8' }}>{row.user_name || '（未登录）'}</span>
                      )}
                      <div style={{ fontSize: '11px', color: '#94a3b8' }}>{row.user_email || ''}</div>
                    </td>
                  )}
                  <td style={TD}>
                    <div>{row.action_label || row.action}</div>
                    <div style={{ fontSize: '11px', color: '#94a3b8', fontFamily: 'monospace' }}>{row.action}</div>
                  </td>
                  <td style={TD}><Pill text={row.status_label || row.status} color={meta.color} bg={meta.bg} /></td>
                  <td style={{ ...TD, textAlign: 'right', fontSize: '12px', color: 'var(--color-text-secondary)' }}>{fmtDuration(row.duration_ms)}</td>
                  <td style={{ ...TD, maxWidth: '170px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.project_name || ''}>{row.project_name || '—'}</td>
                  <td style={{ ...TD, maxWidth: '230px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.resource_name || ''}>{row.resource_name || '—'}</td>
                  {!compact && <td style={{ ...TD, fontFamily: 'monospace', fontSize: '11px', color: '#94a3b8' }}>{row.client_ip || '—'}</td>}
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={colSpan} style={{ ...TD, background: '#f8fafc' }}>
                      <ActivityDetail row={row} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ActivityDetail({ row }: { row: AdminActivity }) {
  return (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px 16px', fontSize: '12px', marginBottom: '8px' }}>
        <InfoRow label="开始" value={fmtDateTime(row.created_at)} />
        <InfoRow label="结束" value={fmtDateTime(row.finished_at)} />
        <InfoRow label="耗时" value={fmtDuration(row.duration_ms)} />
        <InfoRow label="资源类型" value={row.resource_type || '—'} />
        <InfoRow label="项目ID" value={row.project_id || '—'} />
        <InfoRow label="资源ID" value={row.resource_id || '—'} />
      </div>
      {row.error_message && (
        <div style={{ padding: '7px 10px', borderRadius: '6px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', fontSize: '12px', marginBottom: '8px' }}>
          {row.error_message}
        </div>
      )}
      <div style={{ fontSize: '11px', color: '#94a3b8', marginBottom: '4px' }}>客户端 UA：{row.user_agent || '—'}</div>
      {row.detail && Object.keys(row.detail).length > 0 && (
        <pre style={{ margin: 0, padding: '8px 10px', background: '#0f172a', color: '#e2e8f0', borderRadius: '6px', fontSize: '11px', overflowX: 'auto', maxHeight: '200px' }}>
          {JSON.stringify(row.detail, null, 2)}
        </pre>
      )}
    </>
  );
}

// ─────────────────────────── Token 消耗 ───────────────────────────

const TOKEN_GROUP_OPTIONS: Array<{ value: 'user' | 'model' | 'action'; label: string }> = [
  { value: 'user', label: '按用户' },
  { value: 'model', label: '按模型' },
  { value: 'action', label: '按操作类型' },
];

function TokensTab({ range, onOpenUser }: { range: AdminRange; onOpenUser: (userId: string) => void }) {
  const [groupBy, setGroupBy] = useState<'user' | 'model' | 'action'>('user');
  const query = usePoll<AdminTokens>(
    async () => (await adminApi.tokens(range, groupBy, 100)).data,
    [range, groupBy],
    null,
  );
  const data = query.data;
  const max = Math.max(1, ...(data?.items.map((item) => item.total_tokens) || [1]));

  return (
    <div>
      <ErrorBar message={query.error} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        <Kpi label="总 Token" value={fmtCompact(data?.totals.total_tokens)} sub={'区间 ' + range} icon={Coins} color="#7c3aed" />
        <Kpi label="输入 Token" value={fmtCompact(data?.totals.prompt_tokens)} icon={ChevronRight} color="#1a56db" />
        <Kpi label="输出 Token" value={fmtCompact(data?.totals.completion_tokens)} icon={ChevronLeft} color="#059669" />
        <Kpi label="LLM 调用次数" value={fmtInt(data?.totals.calls)} icon={Server} color="#d97706" />
      </div>

      <Card
        title="Token 消耗明细"
        extra={
          <div style={{ display: 'inline-flex', gap: '6px' }}>
            {TOKEN_GROUP_OPTIONS.map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => setGroupBy(item.value)}
                style={{
                  ...BTN, padding: '4px 10px',
                  background: groupBy === item.value ? 'var(--color-primary)' : '#ffffff',
                  color: groupBy === item.value ? '#ffffff' : 'var(--color-text-secondary)',
                  borderColor: groupBy === item.value ? 'var(--color-primary)' : 'var(--color-border)',
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        }
      >
        {!data ? <Spinner text="正在聚合 Token 消耗…" /> : data.items.length === 0 ? <Empty text="该区间没有 LLM 调用记录" /> : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={TH}>{groupBy === 'user' ? '用户' : groupBy === 'model' ? '模型' : '操作类型'}</th>
                  <th style={{ ...TH, width: '220px' }}>占比</th>
                  <th style={{ ...TH, textAlign: 'right' }}>调用次数</th>
                  <th style={{ ...TH, textAlign: 'right' }}>输入</th>
                  <th style={{ ...TH, textAlign: 'right' }}>输出</th>
                  <th style={{ ...TH, textAlign: 'right' }}>合计</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.key}>
                    <td style={{ ...TD, maxWidth: '260px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {groupBy === 'user' ? (
                        <button type="button" onClick={() => onOpenUser(item.key)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 0, fontSize: '13px', fontWeight: 500, color: 'var(--color-primary)' }}>
                          {item.label}
                        </button>
                      ) : (
                        <span style={{ fontWeight: 500 }}>{item.label}</span>
                      )}
                    </td>
                    <td style={TD}>
                      <span style={{ display: 'block', height: '7px', borderRadius: '4px', background: '#f1f5f9', overflow: 'hidden' }}>
                        <span style={{ display: 'block', width: Math.round((item.total_tokens / max) * 100) + '%', height: '100%', background: '#7c3aed' }} />
                      </span>
                    </td>
                    <td style={{ ...TD, textAlign: 'right' }}>{fmtInt(item.calls)}</td>
                    <td style={{ ...TD, textAlign: 'right', color: 'var(--color-text-secondary)' }}>{fmtInt(item.prompt_tokens)}</td>
                    <td style={{ ...TD, textAlign: 'right', color: 'var(--color-text-secondary)' }}>{fmtInt(item.completion_tokens)}</td>
                    <td style={{ ...TD, textAlign: 'right', fontWeight: 600 }}>{fmtInt(item.total_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div style={{ marginTop: '10px', fontSize: '11px', color: '#94a3b8' }}>
          按需求只展示 Token 消耗量，不折算金额。未归属到用户的调用（例如系统定时任务）会显示为「（未归属）」。
        </div>
      </Card>
    </div>
  );
}

// ─────────────────────────── 风险告警 ───────────────────────────

const ALERT_RULES: Array<{ code: string; text: string }> = [
  { code: 'high_failure_rate', text: '单人区间内操作失败率 ≥ 30%（至少 5 次操作）——可能是模型不稳、参数错用或账号异常' },
  { code: 'quota_near_limit', text: '单人今日操作数或 Token 已达配额的 80% 以上' },
  { code: 'login_failure_burst', text: '同一 IP 近 24 小时登录失败 ≥ 10 次——疑似撞库' },
  { code: 'too_many_sessions', text: '单账号活跃会话 ≥ 5 个——疑似账号共享' },
  { code: 'task_backlog', text: '全平台在途任务 ≥ 10 个——LLM 网关压力大，整体可能变慢' },
  { code: 'stuck_task', text: '单个任务运行超过 30 分钟——疑似卡死，需要人工介入' },
];

function AlertsTab({ range, onOpenUser }: { range: AdminRange; onOpenUser: (userId: string) => void }) {
  const query = usePoll<AdminAlerts>(
    async () => (await adminApi.alerts(range)).data,
    [range],
    OVERVIEW_POLL_MS,
  );
  const alerts = query.data?.alerts || [];
  const grouped = {
    danger: alerts.filter((item) => item.level === 'danger'),
    warning: alerts.filter((item) => item.level === 'warning'),
    info: alerts.filter((item) => item.level === 'info'),
  };

  return (
    <div>
      <ErrorBar message={query.error} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '12px', marginBottom: '12px' }}>
        <Kpi label="严重" value={fmtInt(grouped.danger.length)} icon={AlertTriangle} color="#dc2626" />
        <Kpi label="警告" value={fmtInt(grouped.warning.length)} icon={ShieldAlert} color="#d97706" />
        <Kpi label="提示" value={fmtInt(grouped.info.length)} icon={Activity} color="#1a56db" />
        <Kpi label="合计" value={fmtInt(query.data?.count || 0)} sub={'区间 ' + (query.data?.range || range)} icon={Gauge} color="#475569" />
      </div>

      <Card
        title="风险项"
        extra={<button type="button" style={BTN} onClick={query.reload}><RefreshCw size={13} /> 重新检测</button>}
        style={{ marginBottom: '12px' }}
      >
        {!query.data ? <Spinner text="正在检测风险项…" /> : alerts.length === 0 ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '18px 0', color: 'var(--color-success)', fontSize: '13px', justifyContent: 'center' }}>
            <CheckCircle2 size={18} /> 未检测到风险项
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {alerts.map((alert, index) => {
              const meta = ALERT_META[alert.level] || ALERT_META.info;
              return (
                <div key={index} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '10px 12px', borderRadius: '8px', background: meta.bg, border: '1px solid ' + meta.border }}>
                  <Pill text={meta.label} color={meta.color} bg="#ffffff" />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '13px', color: 'var(--color-text)', lineHeight: 1.5 }}>{alert.message}</div>
                    <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '3px' }}>
                      规则 {alert.code} · 检测于 {fmtDateTime(alert.detected_at)}
                    </div>
                  </div>
                  {alert.user_id && (
                    <button type="button" style={{ ...BTN, padding: '3px 9px' }} onClick={() => onOpenUser(alert.user_id as string)}>
                      查看该用户
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card title="告警规则说明">
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {ALERT_RULES.map((rule) => (
            <div key={rule.code} style={{ display: 'flex', gap: '10px', fontSize: '12px', color: 'var(--color-text-secondary)', lineHeight: 1.6 }}>
              <span style={{ fontFamily: 'monospace', color: 'var(--color-text)', flexShrink: 0, width: '160px' }}>{rule.code}</span>
              <span>{rule.text}</span>
            </div>
          ))}
        </div>
        <div style={{ marginTop: '10px', fontSize: '11px', color: '#94a3b8' }}>
          告警全部为只读、实时计算，不落库、不推送。阈值定义在服务端 services/routers/admin_monitor.py。
        </div>
      </Card>
    </div>
  );
}

// ─────────────────────────── 配额治理 ───────────────────────────

function QuotasTab({ onChanged }: { onChanged: () => void }) {
  const query = usePoll<AdminQuotas>(
    async () => (await adminApi.quotas()).data,
    [],
    null,
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftAction, setDraftAction] = useState('0');
  const [draftToken, setDraftToken] = useState('0');
  const [draftNote, setDraftNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const startEdit = (row: { user_id: string; daily_action_limit: number; daily_token_limit: number; note: string | null }) => {
    setEditingId(row.user_id);
    setDraftAction(String(row.daily_action_limit || 0));
    setDraftToken(String(row.daily_token_limit || 0));
    setDraftNote(row.note || '');
  };

  const save = async (userId: string) => {
    setSaving(true);
    try {
      await adminApi.updateQuota(userId, {
        daily_action_limit: Math.max(0, Number(draftAction) || 0),
        daily_token_limit: Math.max(0, Number(draftToken) || 0),
        note: draftNote.trim() || null,
      });
      setNotice('配额已保存');
      setEditingId(null);
      query.reload();
      onChanged();
    } catch (err) {
      setNotice('保存失败：' + errText(err));
    } finally {
      setSaving(false);
      window.setTimeout(() => setNotice(null), 4000);
    }
  };

  const rows = query.data?.quotas || [];
  const defaults = query.data?.defaults;

  return (
    <div>
      <ErrorBar message={query.error} />
      <Card style={{ marginBottom: '12px', padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
          <span>
            系统默认配额：每日操作
            <strong style={{ color: 'var(--color-text)', margin: '0 4px' }}>
              {defaults && defaults.daily_action_limit > 0 ? fmtInt(defaults.daily_action_limit) + ' 次' : '不限'}
            </strong>
            · 每日 Token
            <strong style={{ color: 'var(--color-text)', margin: '0 4px' }}>
              {defaults && defaults.daily_token_limit > 0 ? fmtInt(defaults.daily_token_limit) : '不限'}
            </strong>
          </span>
          {notice && <span style={{ color: 'var(--color-primary)' }}>{notice}</span>}
          <button type="button" style={{ ...BTN, marginLeft: 'auto' }} onClick={query.reload}><RefreshCw size={13} /> 刷新</button>
        </div>
        <div style={{ marginTop: '8px', fontSize: '11px', color: '#94a3b8' }}>
          默认值来自服务端配置（default_daily_action_quota / default_daily_token_quota），此处按人覆盖；填 0 表示对该用户不限制。
          超限后接口返回 429，前端提示「今日额度已用完」，并会在行为流水里记为 rejected。
        </div>
      </Card>

      <Card title="按用户配额">
        {!query.data ? <Spinner text="正在加载配额…" /> : rows.length === 0 ? <Empty text="还没有用户" /> : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={TH}>用户</th>
                  <th style={TH}>角色</th>
                  <th style={{ ...TH, textAlign: 'right' }}>今日操作</th>
                  <th style={{ ...TH, textAlign: 'right' }}>每日操作上限</th>
                  <th style={{ ...TH, textAlign: 'right' }}>今日 Token</th>
                  <th style={{ ...TH, textAlign: 'right' }}>每日 Token 上限</th>
                  <th style={TH}>备注</th>
                  <th style={{ ...TH, textAlign: 'right' }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const editing = editingId === row.user_id;
                  const actionRatio = row.daily_action_limit > 0 ? row.today_actions / row.daily_action_limit : 0;
                  const tokenRatio = row.daily_token_limit > 0 ? row.today_tokens / row.daily_token_limit : 0;
                  return (
                    <tr key={row.user_id} style={{ opacity: row.is_active ? 1 : 0.6 }}>
                      <td style={TD}>
                        <div style={{ fontWeight: 500 }}>{row.name}{!row.is_active && <span style={{ marginLeft: '6px' }}><Pill text="已禁用" color="#b91c1c" bg="#fef2f2" /></span>}</div>
                        <div style={{ fontSize: '11px', color: '#94a3b8' }}>{row.email}</div>
                      </td>
                      <td style={TD}>
                        <div style={{ display: 'flex', gap: '3px', flexWrap: 'wrap' }}>
                          {(row.roles.length ? row.roles : ['无角色']).map((role) => (
                            <span key={role} style={{ fontSize: '10px', padding: '0 6px', borderRadius: '999px', background: '#f1f5f9', color: '#64748b' }}>{role}</span>
                          ))}
                        </div>
                      </td>
                      <td style={{ ...TD, textAlign: 'right', color: actionRatio >= 0.8 ? 'var(--color-danger)' : 'var(--color-text)' }}>
                        {fmtInt(row.today_actions)}
                      </td>
                      <td style={{ ...TD, textAlign: 'right' }}>
                        {editing ? (
                          <input style={{ ...INPUT, width: '92px', textAlign: 'right' }} value={draftAction} onChange={(e) => setDraftAction(e.target.value)} inputMode="numeric" />
                        ) : (
                          <span>{row.daily_action_limit > 0 ? fmtInt(row.daily_action_limit) : '不限'}{row.customized && <span style={{ color: '#94a3b8', fontSize: '11px' }}> ·自定义</span>}</span>
                        )}
                      </td>
                      <td style={{ ...TD, textAlign: 'right', color: tokenRatio >= 0.8 ? 'var(--color-danger)' : 'var(--color-text)' }}>
                        {fmtCompact(row.today_tokens)}
                      </td>
                      <td style={{ ...TD, textAlign: 'right' }}>
                        {editing ? (
                          <input style={{ ...INPUT, width: '110px', textAlign: 'right' }} value={draftToken} onChange={(e) => setDraftToken(e.target.value)} inputMode="numeric" />
                        ) : (
                          <span>{row.daily_token_limit > 0 ? fmtInt(row.daily_token_limit) : '不限'}</span>
                        )}
                      </td>
                      <td style={{ ...TD, maxWidth: '170px' }}>
                        {editing ? (
                          <input style={{ ...INPUT, width: '100%' }} value={draftNote} onChange={(e) => setDraftNote(e.target.value)} placeholder="备注" />
                        ) : (
                          <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{row.note || '—'}</span>
                        )}
                      </td>
                      <td style={{ ...TD, textAlign: 'right', whiteSpace: 'nowrap' }}>
                        {editing ? (
                          <>
                            <button type="button" style={{ ...BTN_PRIMARY, padding: '3px 9px', marginRight: '4px' }} disabled={saving} onClick={() => save(row.user_id)}>保存</button>
                            <button type="button" style={{ ...BTN, padding: '3px 9px' }} disabled={saving} onClick={() => setEditingId(null)}>取消</button>
                          </>
                        ) : (
                          <button type="button" style={{ ...BTN, padding: '3px 9px' }} onClick={() => startEdit(row)}>编辑</button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ─────────────────────────── 页面主体 ───────────────────────────

export default function AdminMonitorPage() {
  const [tab, setTab] = useState<TabKey>('overview');
  const [range, setRange] = useState<AdminRange>('7d');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [drawerUserId, setDrawerUserId] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const openUser = useCallback((userId: string) => setDrawerUserId(userId), []);
  const bumpReload = useCallback(() => setReloadToken((value) => value + 1), []);

  return (
    <div className="page-fade-in" style={{ padding: '18px 22px 28px', minHeight: '100%' }}>
      <style>{'.spin { animation: bmp-spin 1s linear infinite; } @keyframes bmp-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }'}</style>

      <div style={{ display: 'flex', alignItems: 'flex-start', gap: '14px', marginBottom: '14px', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: '240px' }}>
          <h1 style={{ fontSize: '19px', fontWeight: 700, margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Activity size={19} style={{ color: 'var(--color-primary)' }} />
            使用监控
          </h1>
          <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '4px 0 0' }}>
            全员使用情况、实时在线状态、行为流水、Token 消耗与配额治理。时间口径为 Asia/Shanghai 自然日。
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          <RangePicker value={range} onChange={setRange} />
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            自动刷新
          </label>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '4px', borderBottom: '1px solid var(--color-border)', marginBottom: '14px', flexWrap: 'wrap' }}>
        {TABS.map((item) => {
          const active = tab === item.key;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setTab(item.key)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '6px',
                padding: '9px 14px', fontSize: '13px', cursor: 'pointer',
                background: 'transparent', border: 'none',
                borderBottom: active ? '2px solid var(--color-primary)' : '2px solid transparent',
                color: active ? 'var(--color-primary)' : 'var(--color-text-secondary)',
                fontWeight: active ? 600 : 400,
                marginBottom: '-1px',
              }}
            >
              <item.icon size={15} />
              {item.label}
            </button>
          );
        })}
      </div>

      {tab === 'overview' && <OverviewTab key={'overview-' + reloadToken} range={range} autoRefresh={autoRefresh} onOpenUser={openUser} />}
      {tab === 'presence' && <PresenceTab autoRefresh={autoRefresh} onOpenUser={openUser} />}
      {tab === 'users' && <UsersTab range={range} autoRefresh={autoRefresh} reloadToken={reloadToken} onOpenUser={openUser} />}
      {tab === 'activities' && <ActivitiesTab range={range} onOpenUser={openUser} />}
      {tab === 'tokens' && <TokensTab range={range} onOpenUser={openUser} />}
      {tab === 'alerts' && <AlertsTab range={range} onOpenUser={openUser} />}
      {tab === 'quotas' && <QuotasTab onChanged={bumpReload} />}

      {drawerUserId && (
        <UserDrawer
          userId={drawerUserId}
          range={range}
          onClose={() => setDrawerUserId(null)}
          onChanged={bumpReload}
        />
      )}
    </div>
  );
}
