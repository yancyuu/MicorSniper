import { useState, useRef, useEffect } from 'react';
import { Plus, Play, Square, Save, FolderOpen, FileText } from 'lucide-react';
import { paletteItems } from './NodePalette';
import type { NodeType } from '../types/workflow';

interface BottomBarProps {
  workflowStatus: 'idle' | 'running' | 'completed' | 'failed';
  onSave: () => void;
  onLoad: () => void;
  onRun: () => void;
  onStop: () => void;
  onAddNode: (type: NodeType) => void;
  onToggleLogs: () => void;
  isLogOpen: boolean;
}

export default function BottomBar({
  workflowStatus,
  onSave,
  onLoad,
  onRun,
  onStop,
  onAddNode,
  onToggleLogs,
  isLogOpen,
}: BottomBarProps) {
  const isRunning = workflowStatus === 'running';
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!dropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [dropdownOpen]);

  const statusColor = {
    idle: 'bg-zinc-300',
    running: 'bg-amber-500 animate-pulse',
    completed: 'bg-emerald-500',
    failed: 'bg-red-500',
  };

  const statusLabel = {
    idle: 'Ready',
    running: 'Running',
    completed: 'Done',
    failed: 'Failed',
  };

  return (
    <div className="h-14 bg-white border-t border-zinc-200 flex items-center px-4 gap-2 shrink-0">
      {/* Add node button */}
      <div className="relative" ref={dropdownRef}>
        <button
          onClick={() => setDropdownOpen(!dropdownOpen)}
          className="flex items-center justify-center w-9 h-9 rounded-xl bg-zinc-100 hover:bg-zinc-200 text-zinc-600 transition-colors duration-150"
          title="Add node"
        >
          <Plus size={18} />
        </button>
        {dropdownOpen && (
          <div className="absolute bottom-full left-0 mb-2 w-[280px] bg-white rounded-2xl shadow-2xl border border-zinc-100 p-2 fade-in z-50">
            <div className="grid grid-cols-2 gap-1.5">
              {paletteItems.map((item) => (
                <button
                  key={item.type}
                  onClick={() => {
                    onAddNode(item.type);
                    setDropdownOpen(false);
                  }}
                  className="flex items-center gap-2.5 p-2.5 rounded-xl hover:bg-zinc-50 transition-colors duration-150 text-left"
                >
                  <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${item.iconBg}`}>
                    {item.icon}
                  </div>
                  <div className="flex flex-col min-w-0">
                    <span className="text-sm font-medium text-zinc-800">{item.label}</span>
                    <span className="text-[11px] text-zinc-400 truncate">{item.description}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="w-px h-6 bg-zinc-200 mx-1" />

      {/* Run / Stop */}
      {isRunning ? (
        <button
          onClick={onStop}
          className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-medium bg-red-50 text-red-600 hover:bg-red-100 transition-colors duration-150"
        >
          <Square size={14} />
          Stop
        </button>
      ) : (
        <button
          onClick={onRun}
          className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-medium bg-violet-600 text-white hover:bg-violet-700 transition-colors duration-150"
        >
          <Play size={14} />
          Run
        </button>
      )}

      <div className="w-px h-6 bg-zinc-200 mx-1" />

      {/* Save / Load */}
      <button
        onClick={onSave}
        disabled={isRunning}
        className="flex items-center justify-center w-9 h-9 rounded-xl text-zinc-400 hover:text-zinc-600 hover:bg-zinc-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
        title="Save"
      >
        <Save size={16} />
      </button>
      <button
        onClick={onLoad}
        disabled={isRunning}
        className="flex items-center justify-center w-9 h-9 rounded-xl text-zinc-400 hover:text-zinc-600 hover:bg-zinc-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-150"
        title="Load"
      >
        <FolderOpen size={16} />
      </button>

      <div className="w-px h-6 bg-zinc-200 mx-1" />

      {/* Logs toggle */}
      <button
        onClick={onToggleLogs}
        className={`flex items-center justify-center w-9 h-9 rounded-xl transition-colors duration-150 ${isLogOpen ? 'bg-violet-50 text-violet-600' : 'text-zinc-400 hover:text-zinc-600 hover:bg-zinc-50'}`}
        title="Toggle logs"
      >
        <FileText size={16} />
      </button>

      {/* Status */}
      <div className="ml-auto flex items-center gap-2">
        <div className={`w-2 h-2 rounded-full ${statusColor[workflowStatus]}`} />
        <span className="text-xs text-zinc-400 font-medium">{statusLabel[workflowStatus]}</span>
      </div>
    </div>
  );
}
