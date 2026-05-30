import { type MouseEvent, useCallback, useEffect, useRef, useState } from 'react';
import {
  useNodesState,
  useEdgesState,
  addEdge,
  type Connection,
  type Node,
  type Edge,
  type NodeChange,
  type EdgeChange,
} from '@xyflow/react';
import type { WorkflowNodeData, NodeType, LogEntry, NodeStatus, BrowserContext } from '../types/workflow';
import { nodeLabels } from '../components/NodePalette';

const STORAGE_KEY = 'micro-sniper-workflow';

let nodeIdCounter = 1;

function createNodeId(): string {
  return `node_${nodeIdCounter++}`;
}

function saveToLocalStorage(nodes: Node[], edges: Edge[]) {
  try {
    const data = {
      nodes: nodes.map((n) => ({
        id: n.id,
        type: 'workflowNode',
        position: n.position,
        data: { ...n.data, screenshot: undefined },
      })),
      edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {}
}

function loadFromLocalStorage(): { nodes: Node[]; edges: Edge[] } | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function createNodeData(type: NodeType): WorkflowNodeData {
  return {
    label: nodeLabels[type],
    type,
    status: 'idle' as NodeStatus,
    config: {},
  };
}

export default function useWorkflow() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [workflowStatus, setWorkflowStatus] = useState<'idle' | 'running' | 'completed' | 'failed'>('idle');
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [runningNodeId, setRunningNodeId] = useState<string | null>(null);
  const [currentScreenshot, setCurrentScreenshot] = useState<string | null>(null);
  const [browserContexts, setBrowserContexts] = useState<BrowserContext[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const currentWorkflowIdRef = useRef<string | null>(null);

  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  nodesRef.current = nodes;
  edgesRef.current = edges;

  const selectedNode = nodes.find((n) => n.id === selectedNodeId) ?? null;
  const selectedNodeData = selectedNode ? (selectedNode.data as WorkflowNodeData) : null;

  // Save on page unload only
  useEffect(() => {
    const handleBeforeUnload = () => {
      saveToLocalStorage(nodesRef.current, edgesRef.current);
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, []);

  // Load from localStorage on mount — filter out unknown node types
  useEffect(() => {
    const saved = loadFromLocalStorage();
    if (saved && saved.nodes.length > 0) {
      const validTypes = new Set(['context', 'crawl', 'batch_crawl', 'css_extract', 'ai_extract', 'js_execute', 'paginate']);
      const validNodes = saved.nodes.filter((n: any) => validTypes.has(n.data?.type));
      const validNodeIds = new Set(validNodes.map((n: any) => n.id));
      const validEdges = saved.edges.filter((e: any) => validNodeIds.has(e.source) && validNodeIds.has(e.target));
      const maxId = validNodes.reduce((max: number, n: any) => {
        const num = parseInt(n.id.replace('node_', ''), 10);
        return isNaN(num) ? max : Math.max(max, num);
      }, 0);
      nodeIdCounter = maxId + 1;
      setNodes(validNodes);
      setEdges(validEdges);
    }
  }, []);

  const addLog = useCallback((nodeName: string, message: string, level: LogEntry['level'] = 'info') => {
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    setLogs((prev) => [...prev, { timestamp, nodeName, message, level }]);
  }, []);

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((eds) => addEdge(connection, eds));
    },
    [setEdges],
  );

  const addNode = useCallback(
    (type: NodeType, position: { x: number; y: number }) => {
      const id = createNodeId();
      const newNode: Node = {
        id,
        type: 'workflowNode',
        position,
        data: createNodeData(type),
      };
      setNodes((nds) => [...nds, newNode]);
    },
    [setNodes],
  );

  const updateNodeConfig = useCallback(
    (nodeId: string, config: Record<string, any>) => {
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== nodeId) return n;
          const data = n.data as WorkflowNodeData;
          return { ...n, data: { ...data, config } };
        }),
      );
    },
    [setNodes],
  );

  const updateNodeStatus = useCallback(
    (nodeId: string, status: NodeStatus, error?: string, screenshot?: string) => {
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== nodeId) return n;
          const data = n.data as WorkflowNodeData;
          return { ...n, data: { ...data, status, error, screenshot: screenshot ?? data.screenshot } };
        }),
      );
    },
    [setNodes],
  );

  const updateNodeScreenshot = useCallback(
    (nodeId: string, screenshot: string) => {
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== nodeId) return n;
          const data = n.data as WorkflowNodeData;
          return { ...n, data: { ...data, screenshot } };
        }),
      );
    },
    [setNodes],
  );

  // Map a backend node-graph onto the canvas — nodes appear one by one
  const applyGraph = useCallback(
    (rawNodes: any[], rawEdges: any[], stagger: boolean = true) => {
      const allNodes: Node[] = (rawNodes || []).map((n: any) => ({
        id: n.id,
        type: 'workflowNode',
        position: n.position,
        data: n.data,
      }));
      const allEdges: Edge[] = (rawEdges || []).map((e: any) => ({
        id: e.id,
        source: e.source,
        target: e.target,
      }));
      const maxId = allNodes.reduce((max: number, n: any) => {
        const num = parseInt(String(n.id).replace('node_', ''), 10);
        return isNaN(num) ? max : Math.max(max, num);
      }, 0);
      nodeIdCounter = maxId + 1;

      if (!stagger || allNodes.length <= 1) {
        setNodes(allNodes);
        setEdges(allEdges);
        return;
      }

      // Staggered entrance: add nodes one by one
      setNodes([]);
      setEdges([]);
      allNodes.forEach((node, i) => {
        setTimeout(() => {
          setNodes((prev) => [...prev, { ...node }]);
          // Add edges that connect to this node
          const newEdges = allEdges.filter(
            (e) => e.source === node.id || e.target === node.id
          );
          if (newEdges.length > 0) {
            setEdges((prev) => {
              const existingIds = new Set(prev.map((e) => e.id));
              const unique = newEdges.filter((e) => !existingIds.has(e.id));
              return [...prev, ...unique];
            });
          }
        }, i * 300);
      });
    },
    [setNodes, setEdges],
  );

  const saveWorkflow = useCallback(async () => {
    addLog('System', 'Saving workflow...');
    try {
      const payload = {
        name: 'Untitled Workflow',
        nodes: nodes.map((n) => ({
          id: n.id,
          type: (n.data as WorkflowNodeData).type,
          position: n.position,
          data: n.data,
        })),
        edges: edges.map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
        })),
      };

      const res = await fetch('/api/workflows/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      addLog('System', `Workflow saved with ID: ${data.id}`);

      // Also save to localStorage on explicit save
      saveToLocalStorage(nodes, edges);
    } catch (err: any) {
      addLog('System', `Save failed: ${err.message}`, 'error');
    }
  }, [nodes, edges, addLog]);

  const loadWorkflow = useCallback(async () => {
    addLog('System', 'Loading workflow...');
    try {
      const res = await fetch('/api/workflows/latest');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      applyGraph(data.nodes, data.edges);
      addLog('System', 'Workflow loaded successfully');
    } catch (err: any) {
      addLog('System', `Load failed: ${err.message}`, 'error');
    }
  }, [applyGraph, addLog]);

  const runWorkflow = useCallback(async () => {
    if (workflowStatus === 'running') return;

    setWorkflowStatus('running');
    setLogs([]);
    setRunningNodeId(null);
    setCurrentScreenshot(null);
    nodes.forEach((n) => updateNodeStatus(n.id, 'idle'));
    addLog('System', 'Starting workflow execution...');

    try {
      const payload = {
        nodes: nodes.map((n) => ({
          id: n.id,
          type: (n.data as WorkflowNodeData).type,
          position: n.position,
          data: n.data,
        })),
        edges: edges.map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
        })),
      };

      const res = await fetch('/api/workflows/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.workflow_id) {
        currentWorkflowIdRef.current = data.workflow_id;
        const wsUrl = `ws://${window.location.host}/ws/workflows/${data.workflow_id}`;
        const ws = new WebSocket(wsUrl);

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.node_id && msg.status) {
              updateNodeStatus(msg.node_id, msg.status, msg.error, msg.screenshot);
              if (msg.status === 'running') {
                setRunningNodeId(msg.node_id);
              }
              if (msg.screenshot) {
                setCurrentScreenshot(msg.screenshot);
              }
            }
            if (msg.log) {
              addLog(msg.node_name ?? 'System', msg.log, msg.level ?? 'info');
            }
            if (msg.status === 'completed' || msg.status === 'failed') {
              setRunningNodeId(null);
              setCurrentScreenshot(null);
              setWorkflowStatus(msg.status);
              ws.close();
            }
          } catch {
            addLog('System', String(event.data));
          }
        };

        ws.onerror = () => {
          addLog('System', 'WebSocket connection error', 'error');
          setWorkflowStatus('failed');
        };

        ws.onclose = () => {
          wsRef.current = null;
        };

        wsRef.current = ws;
      }
    } catch (err: any) {
      addLog('System', `Run failed: ${err.message}`, 'error');
      setWorkflowStatus('failed');
    }
  }, [workflowStatus, nodes, edges, updateNodeStatus, addLog]);

  const stopWorkflow = useCallback(async () => {
    addLog('System', 'Stopping workflow...');
    const wfId = currentWorkflowIdRef.current;
    try {
      if (wfId) {
        const res = await fetch(`/api/workflows/${encodeURIComponent(wfId)}/stop`, { method: 'POST' });
        if (!res.ok && res.status !== 404) {
          addLog('System', `Stop request failed: HTTP ${res.status}`, 'error');
        }
      }
    } catch (err: any) {
      addLog('System', `Stop failed: ${err.message}`, 'error');
    } finally {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      currentWorkflowIdRef.current = null;
      setRunningNodeId(null);
      setCurrentScreenshot(null);
      setWorkflowStatus('idle');
    }
  }, [addLog]);

  // T035: AI-generate a node graph from URL + goal via SSE streaming.
  const generateWorkflow = useCallback(
    async (url: string, goal: string, sessionName?: string) => {
      setIsGenerating(true);
      addLog('System', `AI 生成中: ${url}`);
      // Reset canvas for incremental build
      setNodes([]);
      setEdges([]);

      let nodeCount = 0;

      try {
        const res = await fetch('/api/workflows/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, goal, session_name: sessionName || null }),
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const e = await res.json();
            detail = e.detail ?? detail;
          } catch {}
          throw new Error(detail);
        }
        if (!res.body) throw new Error('No response body');

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // Parse SSE events from buffer
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || ''; // keep incomplete chunk

          for (const part of parts) {
            if (!part.trim()) continue;

            let eventType = 'message';
            let eventData = '';
            for (const line of part.split('\n')) {
              if (line.startsWith('event:')) {
                eventType = line.slice(6).trim();
              } else if (line.startsWith('data:')) {
                // SSE spec: concatenate multi-line data with newlines
                eventData += (eventData ? '\n' : '') + line.slice(5).trim();
              }
            }
            if (!eventData) continue;

            try {
              const payload = JSON.parse(eventData);

              if (eventType === 'node') {
                // Incrementally add a new node + edge
                const { node, edge } = payload;
                if (node) {
                  nodeCount++;
                  setNodes((prev) => {
                    const maxId = prev.reduce((max, n) => {
                      const num = parseInt(String(n.id).replace('node_', ''), 10);
                      return isNaN(num) ? max : Math.max(max, num);
                    }, 0);
                    nodeIdCounter = maxId + 1;
                    return [...prev, {
                      id: node.id,
                      type: 'workflowNode',
                      position: node.position,
                      data: node.data,
                    }];
                  });
                  if (edge) {
                    setEdges((prev) => [...prev, {
                      id: edge.id,
                      source: edge.source,
                      target: edge.target,
                    }]);
                  }
                  const nd = node.data;
                  addLog('System', `+ 节点: ${nd?.label || node.id} (${nd?.type || '?'})`);
                }
              } else if (eventType === 'need_session') {
                addLog('System', payload.message ?? '目标页面需要登录,请先选择一个已登录会话再生成。', 'warn');
                reader.cancel();
                return { needSession: true };
              } else if (eventType === 'done') {
                addLog('System', `✅ 生成完成: ${nodeCount} 个节点`);
              } else if (eventType === 'error') {
                throw new Error(payload.error || '生成失败');
              }
            } catch (parseErr: any) {
              // Only swallow JSON parse failures, re-throw everything else
              if (!(parseErr instanceof SyntaxError)) throw parseErr;
            }
          }
        }

        return { needSession: false };
      } catch (err: any) {
        addLog('System', `生成失败: ${err.message}`, 'error');
        return { error: err.message };
      } finally {
        setIsGenerating(false);
      }
    },
    [addLog, setNodes, setEdges],
  );

  // Natural-language driven generation: auto-extract URL + session matching
  const generateFromNL = useCallback(
    async (prompt: string) => {
      setIsGenerating(true);
      addLog('System', `🧠 解析: ${prompt}`);
      try {
        const res = await fetch('/api/workflows/smart-generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt }),
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const e = await res.json();
            detail = e.detail ?? detail;
          } catch {}
          throw new Error(detail);
        }
        const data = await res.json();

        if (data.error) {
          addLog('System', data.message ?? '生成失败', 'error');
          return data;
        }

        if (data.need_session) {
          addLog('System', data.message ?? '目标页面需要登录', 'warn');
          return data;
        }

        addLog('System', `📍 识别: ${data.parsed_url}${data.matched_session ? ` | 会话: ${data.matched_session}` : ''}`);
        applyGraph(data.nodes, data.edges);

        if (data.low_confidence) {
          addLog('System', '⚠️ 生成置信度低(试跑样例为空或字段缺失),节点图为草图,请人工确认', 'warn');
        }
        const sampleCount = Array.isArray(data.sample) ? data.sample.length : 0;
        addLog('System', `✅ 生成完成: ${data.nodes?.length ?? 0} 个节点,试跑样例 ${sampleCount} 条`);
        if (sampleCount > 0) {
          addLog('System', `样例预览: ${JSON.stringify(data.sample[0]).slice(0, 300)}`);
        }
        return data;
      } catch (err: any) {
        addLog('System', `生成失败: ${err.message}`, 'error');
        return { error: true, message: err.message };
      } finally {
        setIsGenerating(false);
      }
    },
    [addLog, applyGraph],
  );

  const onNodeClick = useCallback((_event: MouseEvent, node: Node) => {
    setSelectedNodeId(node.id);
  }, []);

  // Auto-scan browser cookies + load persisted sessions
  const refreshContexts = useCallback(async () => {
    try {
      // Auto-detect logged-in sites from browser
      await fetch('/api/sessions/scan', { method: 'POST' }).catch(() => {});

      // Load all sessions (scanned + previously saved)
      const res = await fetch('/api/sessions/');
      if (!res.ok) return;
      const data = await res.json();
      const ctxs: BrowserContext[] = (data || []).map((s: any) => ({
        id: s.name,
        name: s.name,
        url: s.url ?? '',
        screenshot: null,
        created_at: s.created_at ?? '',
      }));
      setBrowserContexts(ctxs);
    } catch {}
  }, []);

  useEffect(() => {
    refreshContexts();
  }, [refreshContexts]);

  const deleteBrowserContext = useCallback(async (id: string) => {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch {}
    setBrowserContexts((prev) => prev.filter((c) => c.id !== id));
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNodeId(null);
  }, []);

  const clearCanvas = useCallback(() => {
    setNodes([]);
    setEdges([]);
    setLogs([]);
    setWorkflowStatus('idle');
    setRunningNodeId(null);
    setCurrentScreenshot(null);
  }, [setNodes, setEdges]);

  return {
    nodes,
    edges,
    workflowStatus,
    logs,
    selectedNodeId,
    selectedNodeData,
    selectedNode,
    runningNodeId,
    currentScreenshot,
    browserContexts,
    onNodesChange: onNodesChange as (changes: NodeChange<Node>[]) => void,
    onEdgesChange: onEdgesChange as (changes: EdgeChange<Edge>[]) => void,
    onConnect,
    addNode,
    updateNodeConfig,
    updateNodeScreenshot,
    saveWorkflow,
    loadWorkflow,
    runWorkflow,
    stopWorkflow,
    generateWorkflow,
    generateFromNL,
    isGenerating,
    onNodeClick,
    onPaneClick,
    deleteBrowserContext,
    refreshContexts,
    clearCanvas,
  };
}
