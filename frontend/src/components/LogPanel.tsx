import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import type { LogEntry } from '../types/workflow';

interface LogPanelProps {
  logs: LogEntry[];
  isOpen: boolean;
  onClose: () => void;
}

const levelColors: Record<string, string> = {
  info: 'text-zinc-600',
  warn: 'text-amber-600',
  error: 'text-red-600',
};

export default function LogPanel({ logs, isOpen, onClose }: LogPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed bottom-16 left-4 z-40 bg-white/95 backdrop-blur-sm rounded-t-2xl shadow-2xl border border-zinc-100 fade-in"
      style={{ width: 400, height: 200 }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-zinc-100">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-zinc-500">
            Logs
          </h3>
          <span className="text-[11px] text-zinc-400">{logs.length} entries</span>
        </div>
        <button
          onClick={onClose}
          className="flex items-center justify-center w-6 h-6 rounded-lg text-zinc-400 hover:text-zinc-600 hover:bg-zinc-100 transition-colors"
        >
          <X size={12} />
        </button>
      </div>

      {/* Log content */}
      <div ref={containerRef} className="overflow-y-auto p-3 font-mono text-xs" style={{ height: 156 }}>
        {logs.length === 0 && (
          <p className="text-zinc-300 text-center py-6">No logs yet. Run the workflow to see output.</p>
        )}
        {logs.map((log, i) => (
          <div key={i} className={`flex gap-3 py-1 border-b border-zinc-50 last:border-0 ${levelColors[log.level]}`}>
            <span className="text-zinc-300 shrink-0">{log.timestamp}</span>
            <span className="text-violet-400 shrink-0 w-28 truncate" title={log.nodeName}>
              [{log.nodeName}]
            </span>
            <span className="whitespace-pre-wrap">{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
