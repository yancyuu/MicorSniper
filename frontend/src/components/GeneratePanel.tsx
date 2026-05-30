import { useState } from 'react';
import { X, Sparkles, Loader2 } from 'lucide-react';

interface GeneratePanelProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onGenerate: (url: string, goal: string) => Promise<any>;
}

export default function GeneratePanel({ open, busy, onClose, onGenerate }: GeneratePanelProps) {
  const [url, setUrl] = useState('');
  const [goal, setGoal] = useState('');

  if (!open) return null;

  const canSubmit = url.trim().length > 0 && goal.trim().length > 0 && !busy;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    const res = await onGenerate(url.trim(), goal.trim());
    if (res && !res.needSession && !res.error) {
      onClose();
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/20 backdrop-blur-sm fade-in">
      <div className="w-[440px] bg-white rounded-2xl shadow-2xl border border-zinc-100 overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-100">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-violet-50 text-violet-500 flex items-center justify-center">
              <Sparkles size={15} />
            </div>
            <span className="text-sm font-semibold text-zinc-800">AI 生成爬取节点图</span>
          </div>
          <button
            onClick={onClose}
            className="flex items-center justify-center w-7 h-7 rounded-lg text-zinc-400 hover:text-zinc-600 hover:bg-zinc-100 transition-colors"
          >
            <X size={14} />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <label className="block text-xs font-medium text-zinc-500 mb-1.5">目标页面 URL</label>
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.taobao.com/search?q=按摩仪"
              className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-sm focus:outline-none focus:ring-2 focus:ring-violet-200 focus:border-violet-300"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-zinc-500 mb-1.5">采集目标</label>
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="采集每个商品的标题、价格、销量、链接"
              rows={3}
              className="w-full px-3 py-2 rounded-xl border border-zinc-200 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-violet-200 focus:border-violet-300"
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-zinc-100 bg-zinc-50/50">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-xl text-sm font-medium text-zinc-500 hover:bg-zinc-100 transition-colors"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-medium bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
            {busy ? '生成中…' : '生成'}
          </button>
        </div>
      </div>
    </div>
  );
}
