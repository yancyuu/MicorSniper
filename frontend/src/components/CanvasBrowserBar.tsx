import { Plus, X, GripHorizontal } from 'lucide-react';
import { Monitor } from 'lucide-react';

export interface BrowserContext {
  id: string;
  name: string;
  url: string;
  screenshot: string | null;
  created_at: string;
}

interface CanvasBrowserBarProps {
  contexts: BrowserContext[];
  onCreateContext: () => void;
  onDeleteContext: (id: string) => void;
  onDragStart: (e: React.DragEvent, context: BrowserContext) => void;
}

export default function CanvasBrowserBar({
  contexts,
  onCreateContext,
  onDeleteContext,
  onDragStart,
}: CanvasBrowserBarProps) {
  return (
    <div className="absolute top-0 left-0 right-0 z-20 flex items-center gap-3 pl-36 pr-4 py-3 bg-white/90 backdrop-blur-sm border-b border-zinc-100">
      <span className="text-xs text-zinc-400 font-medium shrink-0">浏览器上下文</span>

      <div className="flex items-center gap-2 flex-1 overflow-x-auto">
        {contexts.map((ctx) => (
          <div
            key={ctx.id}
            draggable
            onDragStart={(e) => onDragStart(e, ctx)}
            className="group relative shrink-0 rounded-xl border border-zinc-200 bg-zinc-50 hover:bg-white hover:border-violet-300 cursor-grab active:cursor-grabbing transition-all duration-150"
            style={{ width: 120, height: 80 }}
          >
            {/* Drag handle indicator */}
            <div className="absolute top-1 left-1/2 -translate-x-1/2 text-zinc-300 opacity-0 group-hover:opacity-100 transition-opacity">
              <GripHorizontal size={10} />
            </div>

            {/* Delete button */}
            <button
              onClick={() => onDeleteContext(ctx.id)}
              className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-red-400 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity z-10"
            >
              <X size={8} />
            </button>

            {/* Preview */}
            <div className="w-full h-full overflow-hidden rounded-[10px]">
              {ctx.screenshot ? (
                <img
                  src={`data:image/jpeg;base64,${ctx.screenshot}`}
                  alt={ctx.name}
                  className="w-full h-full object-cover"
                  draggable={false}
                />
              ) : (
                <div className="w-full h-full flex flex-col items-center justify-center text-zinc-300">
                  <Monitor size={16} />
                </div>
              )}
            </div>

            {/* Name */}
            <div className="absolute bottom-0 left-0 right-0 px-1 py-0.5 bg-black/50 rounded-b-[10px]">
              <span className="text-[9px] text-white truncate block">{ctx.name}</span>
            </div>
          </div>
        ))}

        {/* Add context button */}
        <button
          onClick={onCreateContext}
          className="shrink-0 flex items-center justify-center w-16 h-16 rounded-xl border-2 border-dashed border-zinc-200 text-zinc-300 hover:border-violet-400 hover:text-violet-400 transition-colors duration-150"
        >
          <Plus size={18} />
        </button>
      </div>
    </div>
  );
}