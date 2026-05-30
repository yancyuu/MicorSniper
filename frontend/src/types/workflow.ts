export type NodeStatus = 'idle' | 'running' | 'success' | 'error';

export type NodeType = 'context' | 'crawl' | 'batch_crawl' | 'css_extract' | 'ai_extract' | 'js_execute' | 'paginate';

export interface BrowserContext {
  id: string;
  name: string;
  url: string;
  screenshot: string | null;
  created_at: string;
}

export interface WorkflowNodeData {
  label: string;
  type: NodeType;
  status: NodeStatus;
  config: Record<string, any>;
  result?: any;
  error?: string;
  screenshot?: string | null;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
}

export interface Workflow {
  id: string;
  name: string;
  nodes: any[];
  edges: any[];
  status: 'idle' | 'running' | 'completed' | 'failed';
  created_at: string;
  updated_at: string;
}

export interface LogEntry {
  timestamp: string;
  nodeName: string;
  message: string;
  level: 'info' | 'warn' | 'error';
}
