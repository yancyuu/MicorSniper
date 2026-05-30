import { Monitor } from 'lucide-react';

interface BrowserFrameProps {
  screenshot: string | null;
  isRunning: boolean;
}

export default function BrowserFrame({ screenshot, isRunning }: BrowserFrameProps) {
  return (
    <div
      className="absolute top-4 right-4 z-20 bg-white rounded-2xl shadow-2xl border border-zinc-100 overflow-hidden fade-in"
      style={{ width: 360 }}
    >
      {/* Chrome header */}
      <div className="flex items-center gap-1.5 px-3 py-2 bg-[#f5f5f5] border-b border-zinc-100">
        <span className="w-2 h-2 rounded-full bg-[#ff5f57]" />
        <span className="w-2 h-2 rounded-full bg-[#febc2e]" />
        <span className="w-2 h-2 rounded-full bg-[#28c840]" />
        <span className="ml-2 text-[11px] text-zinc-400 font-medium">Browser</span>
        {isRunning && (
          <div className="ml-auto flex items-center gap-1.5">
            <div className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            <span className="text-[10px] text-amber-500 font-medium">Live</span>
          </div>
        )}
      </div>

      {/* Browser preview */}
      <div className="relative" style={{ width: 360, height: 225 }}>
        {screenshot ? (
          <img
            src={`data:image/jpeg;base64,${screenshot}`}
            alt="Browser preview"
            className="w-full h-full object-cover"
            draggable={false}
          />
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center bg-zinc-50 gap-2">
            <div className="text-zinc-200">
              <Monitor size={32} />
            </div>
            <span className="text-xs text-zinc-300">No browser session</span>
          </div>
        )}
        {isRunning && screenshot && (
          <div className="absolute inset-0 screenshot-loading pointer-events-none" />
        )}
      </div>
    </div>
  );
}
