import { useCallback, useEffect, useState } from 'react';
import { X, Scan, Loader2, Check, AlertCircle } from 'lucide-react';

interface BrowserSessionPanelProps {
  open: boolean;
  onClose: () => void;
  onSaved: (name: string) => void;
}

interface DetectedSite {
  name: string;
  url: string;
  label: string;
  cookie_count: number;
  auth_cookies_found: string[];
}

export default function BrowserSessionPanel({ open, onClose, onSaved }: BrowserSessionPanelProps) {
  const [sites, setSites] = useState<DetectedSite[]>([]);
  const [scanning, setScanning] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [imported, setImported] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setScanning(true);
    setError(null);
    setSites([]);
    setImported(new Set());
    try {
      const res = await fetch('/api/sessions/scan', { method: 'POST' });
      const data = await res.json();
      if (!data.success) throw new Error(data.error ?? '扫描失败');
      setSites(data.detected || []);
      if ((data.detected || []).length === 0) {
        setError('未检测到已登录的网站。请先在浏览器中登录目标网站。');
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setScanning(false);
    }
  }, []);

  // Auto-scan on open
  useEffect(() => {
    if (open) {
      scan();
    }
  }, [open, scan]);

  const importSite = useCallback(async (site: DetectedSite) => {
    setImporting(site.name);
    try {
      // Scan already saved the session; just notify parent
      setImported(prev => new Set(prev).add(site.name));
      onSaved(site.name);
    } finally {
      setImporting(null);
    }
  }, [onSaved]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 backdrop-blur-sm fade-in">
      <div className="bg-white rounded-2xl shadow-2xl border border-zinc-100 overflow-hidden" style={{ width: 'min(420px, 90vw)' }}>
        {/* Title */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-100">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-rose-50 text-rose-500 flex items-center justify-center">
              <Scan size={15} />
            </div>
            <span className="text-sm font-semibold text-zinc-800">检测浏览器登录状态</span>
          </div>
          <button onClick={onClose} className="flex items-center justify-center w-7 h-7 rounded-lg text-zinc-400 hover:text-zinc-600 hover:bg-zinc-100 transition-colors">
            <X size={14} />
          </button>
        </div>

        {error && (
          <div className="px-4 py-2 text-xs text-red-600 bg-red-50 border-b border-red-100">{error}</div>
        )}

        <div className="p-5 space-y-3 max-h-[60vh] overflow-y-auto">
          {/* Scanning state */}
          {scanning && (
            <div className="flex flex-col items-center gap-3 py-8">
              <Loader2 size={24} className="animate-spin text-rose-400" />
              <p className="text-sm text-zinc-500">正在扫描浏览器登录状态...</p>
            </div>
          )}

          {/* Site list */}
          {!scanning && sites.length > 0 && (
            <>
              <p className="text-xs text-zinc-400">
                检测到 {sites.length} 个已登录网站，点击导入作为爬取上下文
              </p>
              <div className="space-y-2">
                {sites.map(site => {
                  const done = imported.has(site.name);
                  return (
                    <div
                      key={site.name}
                      className={`flex items-center justify-between px-3 py-2.5 rounded-xl border transition-colors ${
                        done
                          ? 'bg-emerald-50 border-emerald-200'
                          : 'bg-zinc-50 border-zinc-100 hover:border-rose-200 hover:bg-rose-50/50 cursor-pointer'
                      }`}
                      onClick={() => !done && !importing && importSite(site)}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <div className={`w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold ${
                          done ? 'bg-emerald-100 text-emerald-600' : 'bg-white text-zinc-500 border border-zinc-200'
                        }`}>
                          {done ? <Check size={14} /> : site.label.charAt(0)}
                        </div>
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-zinc-700 truncate">{site.label}</p>
                          <p className="text-[10px] text-zinc-400 truncate">
                            {site.cookie_count} cookies · {site.auth_cookies_found.slice(0, 2).join(', ')}
                          </p>
                        </div>
                      </div>
                      {importing === site.name ? (
                        <Loader2 size={14} className="animate-spin text-rose-400" />
                      ) : done ? (
                        <span className="text-[10px] text-emerald-500 font-medium">已导入</span>
                      ) : (
                        <span className="text-[10px] text-rose-400 font-medium">点击导入</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {/* Empty state */}
          {!scanning && sites.length === 0 && !error && (
            <div className="flex flex-col items-center gap-3 py-8">
              <AlertCircle size={24} className="text-zinc-300" />
              <p className="text-sm text-zinc-400">请先在浏览器中登录目标网站</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-zinc-100 flex justify-between items-center">
          <button
            onClick={scan}
            disabled={scanning}
            className="flex items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            <Scan size={12} /> 重新扫描
          </button>
          <button
            onClick={onClose}
            className="px-4 py-1.5 rounded-lg text-xs font-medium text-zinc-500 hover:bg-zinc-100 transition-colors"
          >
            完成
          </button>
        </div>
      </div>
    </div>
  );
}
