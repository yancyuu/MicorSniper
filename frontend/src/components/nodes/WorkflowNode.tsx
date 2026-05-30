import { memo, type ReactNode, useState, type FC, useCallback, useEffect, useRef } from 'react';
import { Handle, Position, getBezierPath, BaseEdge, type EdgeProps } from '@xyflow/react';
import type { Node } from '@xyflow/react';
import {
  Globe, List, Code, Brain, Terminal, Layers, MonitorSmartphone,
  ChevronDown, ChevronUp, Eye, Pencil, X,
} from 'lucide-react';
import type { WorkflowNodeData, NodeType, BrowserContext } from '../../types/workflow';

type WorkflowNodeProps = {
  data: WorkflowNodeData;
  selected?: boolean;
  id: string;
  onUpdateConfig?: (nodeId: string, config: Record<string, any>) => void;
  sessions?: BrowserContext[];
};

const themes: Record<NodeType, { bg: string; border: string; icon: string; badge: string; accent: string }> = {
  context:      { bg: 'from-violet-50 to-purple-50', border: 'border-violet-200/60', icon: 'bg-violet-500 text-white', badge: 'bg-violet-100 text-violet-600', accent: '#8b5cf6' },
  crawl:        { bg: 'from-blue-50 to-sky-50',       border: 'border-blue-200/60',   icon: 'bg-blue-500 text-white',   badge: 'bg-blue-100 text-blue-600',   accent: '#3b82f6' },
  batch_crawl:  { bg: 'from-indigo-50 to-blue-50',   border: 'border-indigo-200/60',  icon: 'bg-indigo-500 text-white', badge: 'bg-indigo-100 text-indigo-600', accent: '#6366f1' },
  css_extract:  { bg: 'from-emerald-50 to-teal-50',  border: 'border-emerald-200/60', icon: 'bg-emerald-500 text-white', badge: 'bg-emerald-100 text-emerald-600', accent: '#10b981' },
  ai_extract:   { bg: 'from-amber-50 to-orange-50',  border: 'border-amber-200/60',   icon: 'bg-amber-500 text-white',   badge: 'bg-amber-100 text-amber-600',   accent: '#f59e0b' },
  js_execute:   { bg: 'from-pink-50 to-rose-50',     border: 'border-pink-200/60',    icon: 'bg-pink-500 text-white',    badge: 'bg-pink-100 text-pink-600',    accent: '#ec4899' },
  paginate:     { bg: 'from-cyan-50 to-sky-50',      border: 'border-cyan-200/60',    icon: 'bg-cyan-500 text-white',    badge: 'bg-cyan-100 text-cyan-600',    accent: '#06b6d4' },
};

const icons: Record<NodeType, ReactNode> = {
  context: <MonitorSmartphone size={13} />, crawl: <Globe size={13} />, batch_crawl: <List size={13} />,
  css_extract: <Code size={13} />, ai_extract: <Brain size={13} />, js_execute: <Terminal size={13} />,
  paginate: <Layers size={13} />,
};

const summaries: Record<NodeType, (c: Record<string, any>) => string> = {
  context: (c) => c.session_name || '未选择',
  crawl: (c) => c.url ? c.url.replace(/^https?:\/\//, '').slice(0, 28) : '—',
  batch_crawl: (c) => { const n = c.urls?.split('\n').filter(Boolean).length || 0; return n ? `${n} URLs` : '—'; },
  css_extract: (c) => c.base_selector || '—',
  ai_extract: (c) => c.instruction?.slice(0, 22) || '—',
  js_execute: (c) => c.js_code?.slice(0, 22) || '—',
  paginate: (c) => `${c.max_pages ?? 5} 页`,
};

// ── Inline config field helpers ────────────────────────────────────

const fieldBase = 'w-full bg-white/80 border border-zinc-200/80 rounded-md px-2 py-1 text-[10px] text-zinc-700 placeholder-zinc-300 focus:outline-none focus:border-violet-300 transition-colors';
const labelCls = 'text-[9px] text-zinc-400 font-medium leading-none mb-0.5';

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className={labelCls}>{label}</span>
      {children}
    </label>
  );
}

// ── Per-type inline config forms ───────────────────────────────────

function ContextForm({ config, onChange, sessions }: { config: Record<string, any>; onChange: (k: string, v: any) => void; sessions?: BrowserContext[] }) {
  return (
    <Field label="登录会话">
      <select value={config.session_name ?? ''} onChange={(e) => onChange('session_name', e.target.value)} className={fieldBase}>
        <option value="">— 选择会话 —</option>
        {(sessions || []).map((s) => <option key={s.id} value={s.name}>{s.name}</option>)}
      </select>
    </Field>
  );
}

function CrawlForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="URL">
        <input type="text" value={config.url ?? ''} onChange={(e) => onChange('url', e.target.value)} placeholder="https://..." className={fieldBase} />
      </Field>
      <Field label="Wait Until">
        <select value={config.wait_until ?? 'domcontentloaded'} onChange={(e) => onChange('wait_until', e.target.value)} className={fieldBase}>
          <option value="domcontentloaded">DOM Content Loaded</option>
          <option value="load">Load</option>
          <option value="networkidle">Network Idle</option>
        </select>
      </Field>
    </div>
  );
}

function BatchCrawlForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="URLs (one per line)">
        <textarea value={config.urls ?? ''} onChange={(e) => onChange('urls', e.target.value)} placeholder={"https://...\nhttps://..."} rows={3} className={`${fieldBase} resize-y font-mono`} />
      </Field>
      <label className="flex items-center gap-1.5">
        <input type="checkbox" checked={config.use_upstream ?? false} onChange={(e) => onChange('use_upstream', e.target.checked)} className="rounded border-zinc-300 w-3 h-3" />
        <span className="text-[9px] text-zinc-500">Use upstream URLs</span>
      </label>
    </div>
  );
}

function CssExtractForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="Base Selector">
        <input type="text" value={config.base_selector ?? ''} onChange={(e) => onChange('base_selector', e.target.value)} placeholder=".product-item" className={`${fieldBase} font-mono`} />
      </Field>
      <Field label="Fields (JSON)">
        <textarea value={config.fields ?? ''} onChange={(e) => onChange('fields', e.target.value)} placeholder={'[{"name":"title","selector":"h3"}]'} rows={3} className={`${fieldBase} resize-y font-mono`} />
      </Field>
    </div>
  );
}

function AiExtractForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="Instruction">
        <textarea value={config.instruction ?? ''} onChange={(e) => onChange('instruction', e.target.value)} placeholder="Extract..." rows={2} className={`${fieldBase} resize-y`} />
      </Field>
      <Field label="Schema (JSON)">
        <textarea value={config.schema ?? ''} onChange={(e) => onChange('schema', e.target.value)} placeholder='{"type":"object"...}' rows={2} className={`${fieldBase} resize-y font-mono`} />
      </Field>
      <Field label="LLM Provider">
        <select value={config.provider ?? 'openai/qwen-plus'} onChange={(e) => onChange('provider', e.target.value)} className={fieldBase}>
          <option value="openai/qwen-plus">Qwen Plus</option>
          <option value="openai/qwen-turbo">Qwen Turbo</option>
          <option value="openai/gpt-4o-mini">GPT-4o Mini</option>
          <option value="ollama/llama3.3">Ollama Llama 3.3</option>
        </select>
      </Field>
    </div>
  );
}

function JsExecuteForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="JavaScript">
        <textarea value={config.js_code ?? ''} onChange={(e) => onChange('js_code', e.target.value)} placeholder={"document.querySelector('.next')?.click()"} rows={3} className={`${fieldBase} resize-y font-mono`} />
      </Field>
      <Field label="Wait (ms)">
        <input type="number" value={config.delay ?? 1000} onChange={(e) => onChange('delay', parseInt(e.target.value, 10) || 0)} min={0} max={10000} className={fieldBase} />
      </Field>
    </div>
  );
}

function PaginateForm({ config, onChange }: { config: Record<string, any>; onChange: (k: string, v: any) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Field label="Next JS">
        <input type="text" value={config.next_js ?? ''} onChange={(e) => onChange('next_js', e.target.value)} placeholder="document.querySelector('.next')?.click()" className={`${fieldBase} font-mono`} />
      </Field>
      <Field label="Max Pages">
        <input type="number" value={config.max_pages ?? 5} onChange={(e) => onChange('max_pages', parseInt(e.target.value, 10) || 1)} min={1} max={100} className={fieldBase} />
      </Field>
      <Field label="Page Delay (ms)">
        <input type="number" value={config.page_delay ?? 2000} onChange={(e) => onChange('page_delay', parseInt(e.target.value, 10) || 1000)} min={500} max={10000} className={fieldBase} />
      </Field>
    </div>
  );
}

const CONFIG_FORMS: Partial<Record<NodeType, FC<{ config: Record<string, any>; onChange: (k: string, v: any) => void }>>> = {
  crawl: CrawlForm,
  batch_crawl: BatchCrawlForm,
  css_extract: CssExtractForm,
  ai_extract: AiExtractForm,
  js_execute: JsExecuteForm,
  paginate: PaginateForm,
};

// ── Data preview ───────────────────────────────────────────────────

function DataPreview({ label, data, color }: { label: string; data: any; color: string }) {
  const [open, setOpen] = useState(false);
  if (!data) return null;

  let preview = '';
  let details = '';
  if (Array.isArray(data)) {
    preview = `${data.length} 条`;
    details = JSON.stringify(data.slice(0, 3), null, 1);
    if (data.length > 3) details += '\n  ...';
  } else if (typeof data === 'object') {
    const keys = Object.keys(data);
    preview = `${keys.length} 字段`;
    details = JSON.stringify(data, null, 1).slice(0, 200);
  } else {
    preview = String(data).slice(0, 20);
  }

  return (
    <div>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        className={`flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-medium hover:opacity-80 ${color}`}
      >
        {open ? <ChevronUp size={7} /> : <ChevronDown size={7} />}
        {label}: {preview}
      </button>
      {open && (
        <pre className="mt-1 px-2 py-1 rounded bg-white/80 text-[8px] text-zinc-500 max-h-24 overflow-auto border border-zinc-100 leading-tight whitespace-pre-wrap">
          {details}
        </pre>
      )}
    </div>
  );
}

function ScreenshotPreview({ src }: { src: string }) {
  const [open, setOpen] = useState(false);
  if (!src) return null;
  return (
    <div>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        className="flex items-center gap-1 text-[9px] text-blue-500 font-medium hover:text-blue-600"
      >
        <Eye size={8} />
        {open ? '隐藏截图' : '查看截图'}
      </button>
      {open && (
        <div className="mt-1 rounded-lg overflow-hidden border border-zinc-200">
          <img src={`data:image/jpeg;base64,${src}`} alt="screenshot" className="w-full" />
        </div>
      )}
    </div>
  );
}

// ── Main node component ────────────────────────────────────────────

function WorkflowNode({ data, selected, id, onUpdateConfig, sessions }: WorkflowNodeProps) {
  const [editing, setEditing] = useState(false);
  // Local config mirror to prevent losing focus during re-renders
  const [localConfig, setLocalConfig] = useState(data.config);
  const prevConfigRef = useRef(data.config);
  useEffect(() => {
    // Sync from parent only when not actively editing
    if (!editing && prevConfigRef.current !== data.config) {
      setLocalConfig(data.config);
      prevConfigRef.current = data.config;
    }
  }, [data.config, editing]);

  const status = data.status ?? 'idle';
  const running = status === 'running';
  const done = status === 'success';
  const fail = status === 'error';
  const t = themes[data.type];

  const resultData = data.result;
  const resultCount = Array.isArray(resultData) ? resultData.length
    : resultData && typeof resultData === 'object' ? Object.keys(resultData).length : 0;
  const screenshot = data.screenshot;

  const handleConfigChange = (key: string, value: any) => {
    const next = { ...localConfig, [key]: value };
    setLocalConfig(next);
    onUpdateConfig?.(id, next);
  };

  const hasOutput = resultCount > 0;
  const ConfigForm = data.type === 'context' ? null : CONFIG_FORMS[data.type];

  const cardWidth = editing ? 280 : 240;

  return (
    <div className="node-enter group" style={{ width: cardWidth }}>
      {/* Glow */}
      <div className={`absolute -inset-2 rounded-2xl transition-all duration-500 pointer-events-none ${
        running ? 'bg-amber-400/10 blur-md' :
        done ? 'bg-emerald-400/5 blur-sm' :
        fail ? 'bg-red-400/10 blur-sm' :
        selected ? `bg-[${t.accent}]/5 blur-sm` :
        'opacity-0 group-hover:opacity-100 group-hover:bg-zinc-400/5 group-hover:blur-sm'
      }`} />

      <div className={`relative rounded-xl border transition-all duration-300 overflow-hidden bg-gradient-to-br ${t.bg} ${
        running ? `${t.border} ring-2 ring-amber-300/40` :
        selected ? `${t.border} ring-2 ring-violet-300/40 shadow-lg` :
        `${t.border} shadow-sm hover:shadow-md`
      }`} style={{ backdropFilter: 'blur(8px)' }}>

        {/* Header */}
        <div className="flex items-center gap-2 px-3 py-2">
          <div className={`shrink-0 w-6 h-6 rounded-lg flex items-center justify-center shadow-sm ${t.icon}`}>
            {icons[data.type]}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[11px] font-semibold text-zinc-700 truncate">{data.label}</div>
            <div className="text-[9px] text-zinc-400 truncate mt-0.5">
              {fail && data.error ? data.error.slice(0, 22) : summaries[data.type](data.config)}
            </div>
          </div>
          <div className="flex items-center gap-1">
            {selected && !running && (
              <button
                onClick={(e) => { e.stopPropagation(); setEditing(!editing); }}
                className="w-5 h-5 rounded flex items-center justify-center text-zinc-400 hover:text-violet-500 hover:bg-violet-50 transition-colors"
              >
                {editing ? <X size={10} /> : <Pencil size={10} />}
              </button>
            )}
            <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              running ? 'bg-amber-400 animate-pulse' : done ? 'bg-emerald-400' : fail ? 'bg-red-400' : 'bg-zinc-200'
            }`} />
          </div>
        </div>

        {/* Inline config (expandable) */}
        {editing && selected && (
          <div className="px-3 pb-2 pt-1 border-t border-white/40 space-y-1.5">
            {data.type === 'context' ? (
              <ContextForm config={localConfig} onChange={handleConfigChange} sessions={sessions} />
            ) : ConfigForm ? (
              <ConfigForm config={localConfig} onChange={handleConfigChange} />
            ) : null}
          </div>
        )}

        {/* Input / Output section */}
        {(hasOutput || screenshot) && (
          <div className="px-3 pb-2 space-y-0.5 border-t border-white/40 pt-1.5">
            {hasOutput && (
              <DataPreview label="输出" data={resultData} color="bg-emerald-100 text-emerald-600" />
            )}
            <ScreenshotPreview src={screenshot} />
          </div>
        )}
      </div>

      <Handle type="target" position={Position.Top}
        className="!w-2 !h-2 !bg-white !border-2 !border-zinc-300 !-top-1 !rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
      <Handle type="source" position={Position.Bottom}
        className="!w-2 !h-2 !bg-white !border-2 !border-zinc-300 !-bottom-1 !rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
    </div>
  );
}

export default memo(WorkflowNode);

/* ── Custom animated edge ────────────────────────────────────── */
export function FlowEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style = {}, markerEnd,
}: EdgeProps) {
  const [edgePath] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  return (
    <>
      <path d={edgePath} fill="none" stroke="#c4b5fd" strokeWidth={4} strokeOpacity={0.15} />
      <path d={edgePath} fill="none" stroke="#a78bfa" strokeWidth={1.5} strokeOpacity={0.6}
        style={{ ...style, transition: 'stroke 0.3s' }} markerEnd={markerEnd} />
      <path d={edgePath} fill="none" stroke="url(#edge-gradient)" strokeWidth={2}
        strokeDasharray="6 4" className="edge-flow" />
      <defs>
        <linearGradient id="edge-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stopColor="#a78bfa" stopOpacity={0.8} />
          <stop offset="100%" stopColor="#c4b5fd" stopOpacity={0.3} />
        </linearGradient>
      </defs>
    </>
  );
}
