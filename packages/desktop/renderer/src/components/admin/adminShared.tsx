import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { AlertTriangle, ChevronLeft, ChevronRight, Loader2, type LucideIcon } from 'lucide-react';

/**
 * 管理后台各页签共用的样式常量、格式化工具与原子组件。
 *
 * 原本这些都写在 AdminMonitorPage.tsx 里；新增「文件与报告」页签后拆到这里，
 * 避免两个文件各自拷一份样式常量、以后改主题色时两边漂移。
 */

// ─────────────────────────── 样式常量 // ───────────────────────────

export const CARD: CSSProperties = {
  background: 'var(--color-surface)',
  border: '1px solid var(--color-border)',
  borderRadius: '10px',
  padding: '16px',
};

export const TH: CSSProperties = {
  textAlign: 'left',
  padding: '8px 10px',
  fontWeight: 600,
  color: 'var(--color-text-secondary)',
  fontSize: '12px',
  borderBottom: '1px solid var(--color-border)',
  whiteSpace: 'nowrap',
  background: '#f8fafc',
};

export const TD: CSSProperties = {
  padding: '8px 10px',
  fontSize: '13px',
  borderBottom: '1px solid #f1f5f9',
  verticalAlign: 'middle',
};

export const BTN: CSSProperties = {
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

export const BTN_PRIMARY: CSSProperties = { ...BTN, background: 'var(--color-primary)', borderColor: 'var(--color-primary)', color: '#ffffff' };

export const INPUT: CSSProperties = {
  padding: '6px 10px',
  fontSize: '12px',
  borderRadius: '6px',
  border: '1px solid var(--color-border)',
  background: '#ffffff',
  color: 'var(--color-text)',
  outline: 'none',
};

/** 危险操作（删档案 / 清理）按钮，与 BTN 同形状但警示色。 */
export const BTN_DANGER: CSSProperties = { ...BTN, color: '#b91c1c', borderColor: '#fecaca' };

export const ACTIVITY_STATUS_META: Record<string, { color: string; bg: string }> = {
  running: { color: '#2563eb', bg: '#eff6ff' },
  success: { color: '#059669', bg: '#ecfdf5' },
  failed: { color: '#dc2626', bg: '#fef2f2' },
  rejected: { color: '#d97706', bg: '#fffbeb' },
};

// ─────────────────────────── 格式化工具 // ───────────────────────────

export function fmtInt(value: number | null | undefined): string {
  const n = Number(value || 0);
  return n.toLocaleString('zh-CN');
}

export function fmtCompact(value: number | null | undefined): string {
  const n = Number(value || 0);
  if (n >= 100000000) return (n / 100000000).toFixed(2) + ' 亿';
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

export function fmtPct(ratio: number | null | undefined): string {
  return (Number(ratio || 0) * 100).toFixed(1) + '%';
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const text = iso.replace('T', ' ');
  return text.length >= 19 ? text.slice(0, 19) : text;
}

export function fmtShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const text = iso.replace('T', ' ');
  return text.length >= 16 ? text.slice(5, 16) : text;
}

export function fmtRelative(iso: string | null | undefined): string {
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

export function fmtDuration(ms: number | null | undefined): string {
  const value = Number(ms || 0);
  if (!value) return '—';
  if (value < 1000) return value + ' ms';
  if (value < 60000) return (value / 1000).toFixed(1) + ' s';
  return Math.floor(value / 60000) + ' 分 ' + Math.round((value % 60000) / 1000) + ' 秒';
}

export function fmtElapsed(seconds: number | null | undefined): string {
  const value = Number(seconds || 0);
  if (value < 60) return value.toFixed(0) + ' 秒';
  if (value < 3600) return Math.floor(value / 60) + ' 分 ' + Math.round(value % 60) + ' 秒';
  return Math.floor(value / 3600) + ' 小时 ' + Math.floor((value % 3600) / 60) + ' 分';
}

export function errText(error: unknown): string {
  const anyErr = error as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr?.response?.data?.detail || anyErr?.message || '请求失败';
}

// ─────────────────────────── 原子组件 // ───────────────────────────

export function Card({ title, extra, children, style }: { title?: string; extra?: ReactNode; children: ReactNode; style?: CSSProperties }) {
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

export function Kpi({ label, value, sub, icon: Icon, color }: { label: string; value: string; sub?: string; icon: LucideIcon; color: string }) {
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

export function Pill({ text, color, bg }: { text: string; color: string; bg: string }) {
  return (
    <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: '999px', fontSize: '11px', fontWeight: 600, color, background: bg, whiteSpace: 'nowrap' }}>
      {text}
    </span>
  );
}

export function Spinner({ text }: { text?: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '32px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
      <Loader2 size={16} className="spin" />
      {text || '加载中…'}
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return (
    <div style={{ padding: '32px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>{text}</div>
  );
}

export function Pager({ total, page, pageSize, onPage }: { total: number; page: number; pageSize: number; onPage: (page: number) => void }) {
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

// ─────────────────────────── 数据拉取 Hook // ───────────────────────────

export function usePoll<T>(loader: () => Promise<T>, deps: unknown[], intervalMs: number | null) {
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

export function ErrorBar({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 12px', borderRadius: '8px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', fontSize: '12px', marginBottom: '12px' }}>
      <AlertTriangle size={14} />
      {message}
    </div>
  );
}
