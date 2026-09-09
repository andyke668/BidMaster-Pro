import { useState, useRef } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  BookOpen,
  Download,
  Loader2,
  Upload,
  XCircle,
} from 'lucide-react';
import { checkApi } from '../services/api';
import StepHeader from '../components/common/StepHeader';

type ReviewDimension =
  | 'fileParse'
  | 'projectInfo'
  | 'disqualification'
  | 'scoring'
  | 'pricing'
  | 'delivery'
  | 'starParams'
  | 'materials'
  | 'timeline'
  | 'contractTerms';

const dimensionLabels: Record<ReviewDimension, string> = {
  fileParse: '文件解析',
  projectInfo: '项目信息抽取',
  disqualification: '废标项扫描',
  scoring: '评分项对照',
  pricing: '分项报价核对',
  delivery: '交付时间对比',
  starParams: '▲参数核对',
  materials: '证明材料对照',
  timeline: '时间节点核对',
  contractTerms: '合同条款提取',
};

const dimensionKeys = Object.keys(dimensionLabels) as ReviewDimension[];

export default function CheckPage() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [checkProgress, setCheckProgress] = useState('');
  const [tenderFile, setTenderFile] = useState<File | null>(null);
  const [bidFile, setBidFile] = useState<File | null>(null);
  const tenderFileRef = useRef<HTMLInputElement>(null);
  const bidFileRef = useRef<HTMLInputElement>(null);
  const [companyName, setCompanyName] = useState('');
  const [schoolName, setSchoolName] = useState('');
  const [reviewResult, setReviewResult] = useState<Record<string, unknown> | null>(null);
  const [reviewFileName, setReviewFileName] = useState('');
  const [reviewDownloading, setReviewDownloading] = useState(false);
  const [reviewProgress, setReviewProgress] = useState<Record<string, { pct: number; label: string }>>({});
  const [reviewOverallProgress, setReviewOverallProgress] = useState(0);

  const isReviewDisabled = loading
    || !bidFile
    || !tenderFile
    || !companyName.trim()
    || !schoolName.trim();

  const resetProgress = () => {
    setReviewOverallProgress(2);
    setReviewProgress({
      fileParse: { pct: 15, label: '解析中' },
      ...Object.fromEntries(dimensionKeys.slice(1).map(key => [key, { pct: 0, label: '待开始' }])),
    } as Record<ReviewDimension, { pct: number; label: string }>);
  };

  const handleTenderBidReview = async () => {
    if (!bidFile || !tenderFile) {
      setError('投标书和招标文件都必须上传');
      return;
    }

    setLoading(true);
    setError('');
    setReviewResult(null);
    setCheckProgress('');
    resetProgress();

    try {
      setReviewProgress(prev => ({
        ...prev,
        fileParse: { pct: 100, label: '完成' },
        ...Object.fromEntries(
          dimensionKeys.slice(1).map(key => [key, { pct: 30, label: '进行中' }]),
        ),
      }));

      const res = await checkApi.tenderBidReview(bidFile, tenderFile, companyName, schoolName);
      const taskId = (res.data as Record<string, unknown>)?.task_id as string;
      if (!taskId) throw new Error('审查任务提交失败');

      setCheckProgress('投标文件审查任务已提交...');
      const result = await checkApi.pollCheckTask(taskId, (message, progress) => {
        setCheckProgress(message);
        if (typeof progress === 'number') {
          setReviewOverallProgress(Math.max(2, progress));
        }
      });

      const payload = result as Record<string, unknown>;
      const data = (payload.data || {}) as Record<string, unknown>;
      if (!payload.success) throw new Error((payload.error as string) || '审查失败');

      setReviewProgress(Object.fromEntries(
        dimensionKeys.map(key => [key, { pct: 100, label: '完成' }]),
      ));
      setReviewOverallProgress(100);
      if (data.file_name) setReviewFileName(data.file_name as string);
      setReviewResult(payload);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '审查失败');
      setReviewProgress({});
      setReviewOverallProgress(0);
    } finally {
      setLoading(false);
      setCheckProgress('');
    }
  };

  const handleDownloadReview = async () => {
    if (!reviewFileName) return;
    setReviewDownloading(true);
    try {
      const res = await checkApi.downloadTenderBidReview(reviewFileName);
      const blob = new Blob([res.data as unknown as BlobPart], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = reviewFileName;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '下载失败');
    } finally {
      setReviewDownloading(false);
    }
  };

  const renderUploadZone = (
    label: string,
    file: File | null,
    setFile: (file: File | null) => void,
    inputRef: React.RefObject<HTMLInputElement | null>,
  ) => (
    <div>
      <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>
        {label} <span style={{ color: '#dc2626' }}>*</span>
      </label>
      <div
        onClick={() => inputRef.current?.click()}
        style={{
          border: `2px dashed ${file ? '#059669' : 'var(--color-border)'}`,
          borderRadius: '10px',
          padding: '20px',
          textAlign: 'center',
          cursor: 'pointer',
          background: file ? '#ecfdf5' : '#f8fafc',
          transition: 'all 0.2s',
        }}
      >
        <Upload size={24} color={file ? '#059669' : '#94a3b8'} style={{ margin: '0 auto 8px' }} />
        <div style={{ fontSize: '13px', fontWeight: 500 }}>{file ? file.name : '点击上传文件'}</div>
        <div style={{ fontSize: '11px', color: file ? '#059669' : 'var(--color-text-secondary)', marginTop: '4px' }}>
          {file ? `${(file.size / 1024).toFixed(1)} KB` : '支持 .docx .pdf .txt .md 格式'}
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept=".docx,.pdf,.txt,.md"
        onChange={(event) => {
          const selected = event.target.files?.[0];
          if (selected) setFile(selected);
        }}
        style={{ display: 'none' }}
      />
      {file && (
        <button
          onClick={(event) => {
            event.stopPropagation();
            if (inputRef.current) inputRef.current.value = '';
            setFile(null);
          }}
          style={{
            marginTop: '8px',
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '6px',
            padding: '8px 12px',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: '6px',
            cursor: 'pointer',
            fontSize: '12px',
            fontWeight: 600,
            color: '#dc2626',
          }}
        >
          <XCircle size={14} /> 删除文件
        </button>
      )}
    </div>
  );

  const renderReviewProgress = () => {
    if (!reviewProgress || Object.keys(reviewProgress).length === 0) return null;
    return (
      <div style={{ marginTop: '16px', background: 'var(--color-surface)', borderRadius: '12px', padding: '20px', border: '1px solid var(--color-border)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <span style={{ fontSize: '14px', fontWeight: 600 }}>审查进度</span>
          <span style={{ fontSize: '18px', fontWeight: 700, color: '#2563eb' }}>{reviewOverallProgress}%</span>
        </div>
        <div style={{ height: '8px', background: '#e5e7eb', borderRadius: '4px', overflow: 'hidden', marginBottom: '16px' }}>
          <div style={{ width: `${reviewOverallProgress}%`, background: reviewOverallProgress === 100 ? '#059669' : '#2563eb', height: '100%', borderRadius: '4px', transition: 'width 0.3s' }} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {Object.entries(reviewProgress).map(([key, status]) => (
            <div key={key} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <span style={{ width: '110px', fontSize: '12px', color: 'var(--color-text-secondary)', textAlign: 'right', flexShrink: 0 }}>
                {dimensionLabels[key as ReviewDimension] || key}
              </span>
              <div style={{ flex: 1, background: '#e5e7eb', borderRadius: '4px', height: '6px', overflow: 'hidden' }}>
                <div style={{ width: `${status.pct}%`, background: status.pct === 100 ? '#059669' : '#3b82f6', height: '100%', borderRadius: '4px', transition: 'width 0.3s' }} />
              </div>
              <span style={{ width: '44px', fontSize: '11px', color: status.pct === 100 ? '#059669' : status.pct > 0 ? '#3b82f6' : '#9ca3af', flexShrink: 0 }}>{status.label}</span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="page-fade-in">
      <StepHeader
        step={3}
        title="投标检查"
        subtitle="上传招标文件与投标书，固定12页模板交叉审查并生成 Excel 报告"
        color="#d97706"
        nextPath="/format"
        nextLabel="下一步：文档输出"
      />

      <div style={{ maxWidth: '960px', margin: '0 auto' }}>
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
            <BookOpen size={18} color="#d97706" />
            <h3 style={{ fontSize: '16px', fontWeight: 600, margin: 0 }}>投标文件审查</h3>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '20px' }}>
            {renderUploadZone('招标文件', tenderFile, setTenderFile, tenderFileRef)}
            {renderUploadZone('投标书', bidFile, setBidFile, bidFileRef)}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '20px', marginTop: '16px' }}>
            <div>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>公司名称（投标方）</label>
              <input type="text" value={companyName} onChange={(event) => setCompanyName(event.target.value)} placeholder="如：XX信息科技有限公司" style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '13px', background: 'var(--color-surface)' }} />
            </div>
            <div>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>学校名称（招标方/业主）</label>
              <input type="text" value={schoolName} onChange={(event) => setSchoolName(event.target.value)} placeholder="如：XX大学" style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '13px', background: 'var(--color-surface)' }} />
            </div>
          </div>

          <button
            onClick={handleTenderBidReview}
            disabled={isReviewDisabled}
            style={{
              width: '100%',
              marginTop: '20px',
              padding: '12px',
              background: '#d97706',
              color: 'white',
              border: 'none',
              borderRadius: '8px',
              cursor: isReviewDisabled ? 'not-allowed' : 'pointer',
              fontSize: '14px',
              fontWeight: 600,
              opacity: isReviewDisabled ? 0.5 : 1,
            }}
          >
            {loading ? (checkProgress || '审查中...') : '开始审查 →'}
          </button>
        </div>

        {renderReviewProgress()}

        {error && (
          <div style={{ marginTop: '16px', padding: '12px', background: '#fef2f2', borderRadius: '8px', color: '#dc2626', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={16} /> {error}
          </div>
        )}

        {reviewResult && (() => {
          const data = (reviewResult.data || {}) as Record<string, unknown>;
          const counts = (data.dimension_counts || {}) as Record<string, number>;
          const total = Number(data.total_items || 0);
          const high = Number(data.high_count || 0);
          const sum = Object.values(counts).reduce((accumulator, value) => accumulator + Number(value || 0), 0);
          return (
            <div style={{ marginTop: '20px', background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <h3 style={{ fontSize: '16px', fontWeight: 600, margin: 0 }}>投标文件审查结果</h3>
                {reviewFileName && (
                  <button
                    onClick={handleDownloadReview}
                    disabled={reviewDownloading}
                    style={{
                      padding: '8px 18px',
                      background: '#059669',
                      color: 'white',
                      border: 'none',
                      borderRadius: '8px',
                      cursor: reviewDownloading ? 'not-allowed' : 'pointer',
                      fontSize: '13px',
                      fontWeight: 500,
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      opacity: reviewDownloading ? 0.6 : 1,
                    }}
                  >
                    {reviewDownloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                    下载 Excel 报告
                  </button>
                )}
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '16px', marginBottom: '16px' }}>
                <div style={{ background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '8px', padding: '16px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#dc2626' }}>{high}</div>
                  <div style={{ fontSize: '12px', color: '#dc2626', marginTop: '4px' }}>高风险条目</div>
                </div>
                <div style={{ background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: '8px', padding: '16px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#059669' }}>{total}</div>
                  <div style={{ fontSize: '12px', color: '#059669', marginTop: '4px' }}>审查总条数</div>
                </div>
                <div style={{ background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: '8px', padding: '16px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#2563eb' }}>{sum}</div>
                  <div style={{ fontSize: '12px', color: '#2563eb', marginTop: '4px' }}>审查合计</div>
                </div>
                <div style={{ background: Number(data.guardrail_missing || 0) > 0 ? '#fffbeb' : '#f0fdf4', border: `1px solid ${Number(data.guardrail_missing || 0) > 0 ? '#fde68a' : '#bbf7d0'}`, borderRadius: '8px', padding: '16px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: Number(data.guardrail_missing || 0) > 0 ? '#d97706' : '#059669' }}>
                    {String(data.guardrail_missing || 0)}/{String(data.guardrail_total || 0)}
                  </div>
                  <div style={{ fontSize: '12px', color: Number(data.guardrail_missing || 0) > 0 ? '#d97706' : '#059669', marginTop: '4px' }}>护栏未覆盖</div>
                </div>
              </div>

              <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                {Object.entries(counts).map(([key, value]) => (
                  <div key={key} style={{ padding: '8px 14px', background: 'var(--color-bg)', border: '1px solid var(--color-border)', borderRadius: '8px', fontSize: '12px' }}>
                    <span style={{ fontWeight: 500 }}>{dimensionLabels[key as ReviewDimension] || key}</span>
                    <span style={{ color: 'var(--color-text-secondary)', marginLeft: '8px' }}>{String(value)} 条</span>
                  </div>
                ))}
              </div>

              <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--color-text-secondary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <AlertCircle size={12} />
                每条结果均带原文出处，可溯源到招标文件原文；报告列出事实清单，不下结论。
              </div>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
