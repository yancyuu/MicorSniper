import { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Loader2, Sparkles, User, Bot, Square, Trash2, Check, X, Globe, Code, Database, Cpu, ChevronRight } from 'lucide-react';

/** A single tool execution step within a streaming response */
export interface ExecutionStep {
  id: string;
  label: string;
  status: 'running' | 'done' | 'error';
  detail?: string;
  nodeBadge?: string;   // e.g. "crawl", "css_extract"
  dataPreview?: any;    // sample data
  timestamp: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  data?: any;
  tokens?: number;
  /** Internal: streaming session ID */
  _streamId?: string;
  /** Internal: execution steps for this response */
  _steps?: ExecutionStep[];
  /** Internal: is agent still thinking/executing tools? */
  _thinking?: boolean;
  /** Internal: node count created during this response */
  _nodeCount?: number;
}

// Icon map for tool/node types
const TOOL_ICONS: Record<string, string> = {
  open_page: '🌐',
  generate_schema: '🔍',
  css_extract: '📐',
  ai_extract: '🤖',
  paginate: '📄',
  js_execute: '⚡',
  update_node: '✏️',
  re_run_node: '🔄',
  add_node: '➕',
  remove_node: '🗑️',
  finish: '✅',
};

const NODE_COLORS: Record<string, string> = {
  crawl: 'bg-blue-100 text-blue-600',
  css_extract: 'bg-emerald-100 text-emerald-600',
  ai_extract: 'bg-violet-100 text-violet-600',
  paginate: 'bg-amber-100 text-amber-600',
  js_execute: 'bg-orange-100 text-orange-600',
};

interface ChatPanelProps {
  messages: ChatMessage[];
  busy: boolean;
  contexts: { name: string; url: string }[];
  totalTokens: number;
  onSend: (message: string) => void;
  onStop?: () => void;
  onClearContext?: () => void;
}

export default function ChatPanel({ messages, busy, contexts, totalTokens, onSend, onStop, onClearContext }: ChatPanelProps) {
  const [input, setInput] = useState('');
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = useCallback(() => {
    if (!input.trim() || busy) return;
    onSend(input.trim());
    setInput('');
  }, [input, busy, onSend]);

  return (
    <div className="w-[380px] shrink-0 flex flex-col border-r border-zinc-100 bg-zinc-50/50">
      {/* Header */}
      <div className="shrink-0 px-4 py-3 border-b border-zinc-100 bg-white">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-lg bg-rose-50 text-rose-500 flex items-center justify-center">
              <Sparkles size={12} />
            </div>
            <span className="text-xs font-semibold text-zinc-700">MicroSniper</span>
          </div>
          <div className="flex items-center gap-2">
            {totalTokens > 0 && (
              <span className="text-[9px] text-zinc-400 bg-zinc-50 px-1.5 py-0.5 rounded">⚡ {totalTokens}</span>
            )}
            {messages.length > 0 && onClearContext && (
              <button
                onClick={onClearContext}
                className="w-5 h-5 rounded flex items-center justify-center text-zinc-300 hover:text-red-400 hover:bg-red-50 transition-colors"
                title="清空上下文"
              >
                <Trash2 size={10} />
              </button>
            )}
          </div>
        </div>
        {contexts.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {contexts.map(c => (
              <span key={c.name} className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-600 border border-emerald-100">
                🔗 {c.name}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Messages */}
      <div ref={listRef} className="flex-1 overflow-y-auto p-3 space-y-3 min-h-0">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3 py-12">
            <div className="w-10 h-10 rounded-2xl bg-rose-50 text-rose-400 flex items-center justify-center">
              <Sparkles size={18} />
            </div>
            <div>
              <p className="text-xs font-medium text-zinc-500">描述你想爬取的内容</p>
              <p className="text-[10px] text-zinc-400 mt-1">例如：爬取淘宝上手机壳的价格和销量</p>
            </div>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`flex gap-2 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {msg.role === 'assistant' && (
              <div className="shrink-0 w-5 h-5 rounded-md bg-violet-100 text-violet-500 flex items-center justify-center mt-0.5">
                <Bot size={10} />
              </div>
            )}

            {msg.role === 'user' ? (
              <div className="max-w-[85%] px-3 py-2 rounded-xl text-xs leading-relaxed bg-rose-500 text-white">
                {msg.content}
              </div>
            ) : (
              <div className="max-w-[92%] rounded-xl border border-zinc-100 bg-white shadow-sm overflow-hidden">
                {/* Execution Steps Timeline */}
                {msg._steps && msg._steps.length > 0 && (
                  <div className="px-3 pt-2.5 pb-1">
                    <div className="space-y-0">
                      {msg._steps.map((step, si) => (
                        <div key={step.id} className="flex items-start gap-2 relative">
                          {/* Timeline connector */}
                          {si < (msg._steps?.length || 0) - 1 && (
                            <div className="absolute left-[7px] top-[18px] w-px h-full bg-zinc-100" />
                          )}
                          {/* Status dot */}
                          <div className="shrink-0 mt-[3px]">
                            {step.status === 'running' ? (
                              <div className="w-3.5 h-3.5 rounded-full bg-violet-100 flex items-center justify-center">
                                <div className="w-2 h-2 rounded-full bg-violet-500 animate-pulse" />
                              </div>
                            ) : step.status === 'done' ? (
                              <div className="w-3.5 h-3.5 rounded-full bg-emerald-100 flex items-center justify-center">
                                <Check size={8} className="text-emerald-500" />
                              </div>
                            ) : (
                              <div className="w-3.5 h-3.5 rounded-full bg-red-100 flex items-center justify-center">
                                <X size={8} className="text-red-500" />
                              </div>
                            )}
                          </div>
                          {/* Step content */}
                          <div className="flex-1 min-w-0 pb-2">
                            <div className="flex items-center gap-1.5">
                              <span className="text-[10px]">
                                {TOOL_ICONS[step.label] || '🔧'}
                              </span>
                              <span className={`text-[11px] font-medium ${
                                step.status === 'running' ? 'text-violet-600' :
                                step.status === 'done' ? 'text-zinc-500' : 'text-red-500'
                              }`}>
                                {step.detail || step.label}
                              </span>
                              {step.nodeBadge && (
                                <span className={`text-[8px] px-1 py-0.5 rounded font-medium ${NODE_COLORS[step.nodeBadge] || 'bg-zinc-100 text-zinc-500'}`}>
                                  +{step.nodeBadge}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Streaming Text */}
                {msg.content && (
                  <div className="px-3 py-2 text-xs leading-relaxed text-zinc-700">
                    {msg.content}
                    {/* Streaming cursor */}
                    {msg._streamId && busy && !msg._thinking && (
                      <span className="inline-block w-1.5 h-3 bg-violet-400 animate-pulse ml-0.5 align-middle rounded-sm" />
                    )}
                  </div>
                )}

                {/* Waiting indicator (no steps, no content, just streaming) */}
                {!msg.content && (!msg._steps || msg._steps.length === 0) && msg._streamId && (
                  <div className="px-3 py-2.5 flex items-center gap-2">
                    <Loader2 size={12} className="animate-spin text-violet-400" />
                    <span className="text-[10px] text-zinc-400">分析中...</span>
                  </div>
                )}

                {/* Data sample preview */}
                {msg.data?.sample && msg.data.sample.length > 0 && (
                  <div className="mx-3 mb-2 px-2 py-1.5 rounded-lg bg-zinc-50 border border-zinc-100">
                    <div className="flex items-center gap-1 mb-1">
                      <Database size={8} className="text-emerald-500" />
                      <span className="text-[9px] font-medium text-emerald-600">{msg.data.sample.length} 条样例数据</span>
                    </div>
                    <pre className="text-[9px] text-zinc-500 leading-tight overflow-x-auto max-h-16 overflow-y-auto">
                      {JSON.stringify(msg.data.sample[0], null, 1).slice(0, 200)}
                    </pre>
                  </div>
                )}
              </div>
            )}

            {msg.role === 'user' && (
              <div className="shrink-0 w-5 h-5 rounded-md bg-rose-100 text-rose-500 flex items-center justify-center mt-0.5">
                <User size={10} />
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Input */}
      <div className="shrink-0 p-3 border-t border-zinc-100 bg-white">
        <div className="flex items-center gap-2 bg-zinc-50 rounded-xl px-3 py-2 border border-zinc-200 focus-within:border-rose-300 focus-within:bg-white transition-colors">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
            placeholder="描述要爬取的内容，或调整节点参数..."
            className="flex-1 bg-transparent text-xs text-zinc-700 placeholder-zinc-300 focus:outline-none"
            disabled={busy}
          />
          <button
            onClick={busy ? onStop : handleSubmit}
            disabled={!busy && !input.trim()}
            className={`shrink-0 w-7 h-7 rounded-lg text-white flex items-center justify-center transition-colors ${
              busy
                ? 'bg-red-500 hover:bg-red-600'
                : 'bg-rose-500 hover:bg-rose-600 disabled:opacity-30 disabled:cursor-not-allowed'
            }`}
          >
            {busy ? <Square size={10} /> : <Send size={12} />}
          </button>
        </div>
      </div>
    </div>
  );
}
