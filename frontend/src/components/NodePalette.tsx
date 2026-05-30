import type { ReactNode } from 'react';
import { Globe, List, Code, Brain, Terminal, Layers, MonitorSmartphone } from 'lucide-react';
import type { NodeType } from '../types/workflow';

interface PaletteItem {
  type: NodeType;
  label: string;
  icon: ReactNode;
  description: string;
  iconBg: string;
}

export const paletteItems: PaletteItem[] = [
  {
    type: 'context',
    label: '上下文',
    icon: <MonitorSmartphone size={16} />,
    description: '绑定已登录会话(cookie/profile)',
    iconBg: 'bg-rose-50 text-rose-500',
  },
  {
    type: 'crawl',
    label: '爬取页面',
    icon: <Globe size={16} />,
    description: 'crawl4ai arun，返回 Markdown',
    iconBg: 'bg-blue-50 text-blue-500',
  },
  {
    type: 'batch_crawl',
    label: '批量爬取',
    icon: <List size={16} />,
    description: 'arun_many 并行爬取多个 URL',
    iconBg: 'bg-indigo-50 text-indigo-500',
  },
  {
    type: 'css_extract',
    label: 'CSS 提取',
    icon: <Code size={16} />,
    description: 'JsonCssExtractionStrategy',
    iconBg: 'bg-cyan-50 text-cyan-500',
  },
  {
    type: 'ai_extract',
    label: 'AI 提取',
    icon: <Brain size={16} />,
    description: 'LLMExtractionStrategy',
    iconBg: 'bg-violet-50 text-violet-500',
  },
  {
    type: 'js_execute',
    label: '执行 JS',
    icon: <Terminal size={16} />,
    description: '点击、滚动、关闭弹窗',
    iconBg: 'bg-amber-50 text-amber-500',
  },
  {
    type: 'paginate',
    label: '翻页采集',
    icon: <Layers size={16} />,
    description: '多页循环 + 提取',
    iconBg: 'bg-emerald-50 text-emerald-500',
  },
];

export const nodeLabels: Record<NodeType, string> = {
  context: '上下文',
  crawl: 'Crawl Page',
  batch_crawl: 'Batch Crawl',
  css_extract: 'CSS Extract',
  ai_extract: 'AI Extract',
  js_execute: 'Execute JS',
  paginate: 'Paginate',
};
