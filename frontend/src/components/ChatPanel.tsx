import { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Loader2, Sparkles, User, Bot, Square, Trash2 } from 'lucide-react';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  data?: any;
  tokens?: number;
}

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
    <div className="w-[340px] shrink-0 flex flex-col border-r border-zinc-100 bg-zinc-50/50">
      {/* Header */}
      <div className="shrink-0 px-4 py-3 border-b border-zinc-100 bg-white">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-lg bg-rose-50 text-rose-500 flex items-center justify-center">
              <Sparkles size={12} />
            </div>
            <span className="text-xs font-semibold text-zinc-700">MicroSniper</span>
          </div>
          {totalTokens > 0 && (
            <span className="text-[9px] text-zinc-400 bg-zinc-50 px-1.5 py-0.5 rounded">⚡ {totalTokens} tokens</span>
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
        {contexts.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {contexts.map(c => (
              <span key={c.name} className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-600 border border-emerald-100">
                {c.name}
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
            <div className={`max-w-[85%] px-3 py-2 rounded-xl text-xs leading-relaxed ${
              msg.role === 'user'
                ? 'bg-rose-500 text-white'
                : 'bg-white text-zinc-600 border border-zinc-100 shadow-sm'
            }`}>
              {msg.content}
              {msg.data?.sample && msg.data.sample.length > 0 && (
                <div className="mt-1.5 px-2 py-1 rounded-lg bg-zinc-50 text-[10px] text-emerald-600 border border-zinc-100">
                  📊 {msg.data.sample.length} 条样例: {JSON.stringify(msg.data.sample[0]).slice(0, 120)}...
                </div>
              )}
            </div>
            {msg.role === 'user' && (
              <div className="shrink-0 w-5 h-5 rounded-md bg-rose-100 text-rose-500 flex items-center justify-center mt-0.5">
                <User size={10} />
              </div>
            )}
          </div>
        ))}

        {busy && (
          <div className="flex gap-2 justify-start">
            <div className="shrink-0 w-5 h-5 rounded-md bg-violet-100 text-violet-500 flex items-center justify-center mt-0.5">
              <Bot size={10} />
            </div>
            <div className="px-3 py-2 rounded-xl bg-white border border-zinc-100 shadow-sm">
              <Loader2 size={12} className="animate-spin text-violet-400" />
            </div>
          </div>
        )}
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
