import { useState, useCallback, useEffect, useRef } from 'react';
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import useWorkflow from './hooks/useWorkflow';
import LogPanel from './components/LogPanel';
import Canvas from './components/Canvas';
import ChatPanel, { type ChatMessage, type ExecutionStep } from './components/ChatPanel';
import type { NodeType } from './types/workflow';

/** Parse thinking content to extract tool name hint */
function parseToolFromThinking(content: string): { tool: string; detail: string } {
  if (content.includes('打开')) return { tool: 'open_page', detail: content };
  if (content.includes('Schema') || content.includes('schema')) return { tool: 'generate_schema', detail: content };
  if (content.includes('CSS') || content.includes('css')) return { tool: 'css_extract', detail: content };
  if (content.includes('AI') || content.includes('ai')) return { tool: 'ai_extract', detail: content };
  return { tool: 'thinking', detail: content };
}

/** Map node type to readable label */
const NODE_TYPE_LABELS: Record<string, string> = {
  crawl: '爬取页面',
  css_extract: 'CSS提取',
  ai_extract: 'AI提取',
  paginate: '翻页',
  js_execute: 'JS执行',
};

function WorkflowEditor() {
  const {
    nodes, edges, workflowStatus, logs,
    selectedNodeId, selectedNodeData, selectedNode,
    runningNodeId, currentScreenshot, browserContexts,
    onNodesChange, onEdgesChange, onConnect, addNode,
    updateNodeConfig, saveWorkflow, loadWorkflow,
    runWorkflow, stopWorkflow, generateWorkflow,
    isGenerating, refreshContexts, onNodeClick, onPaneClick,
    clearCanvas,
  } = useWorkflow();

  const [isLogOpen, setIsLogOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [lastResult, setLastResult] = useState<any>(null);

  const clearContext = useCallback(() => {
    setMessages([]);
    setLastResult(null);
    clearCanvas();
    sessionIdRef.current = crypto.randomUUID();
  }, [clearCanvas]);
  const { fitView } = useReactFlow();
  const workflowRef = useRef<any>(null);
  const chatAbortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef(crypto.randomUUID());

  // Keep workflow ref updated
  useEffect(() => {
    workflowRef.current = { nodes, edges };
  }, [nodes, edges]);

  useEffect(() => {
    if (runningNodeId) {
      fitView({ nodes: [{ id: runningNodeId }], padding: 0.4, duration: 600 });
    }
  }, [runningNodeId, fitView]);

  const handleAddNode = useCallback(
    (type: NodeType) => {
      addNode(type, { x: 200 + Math.random() * 300, y: 100 + Math.random() * 200 });
    },
    [addNode],
  );

  const handleChatSend = useCallback(async (text: string) => {
    const userMsg: ChatMessage = {
      role: 'user', content: text, timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);
    setChatBusy(true);
    const abort = new AbortController();
    chatAbortRef.current = abort;

    // Placeholder for streaming assistant message with steps tracking
    const streamMsgId = `stream-${Date.now()}`;
    const initialMsg: ChatMessage = {
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      _streamId: streamMsgId,
      _thinking: true,
      _steps: [],
      _nodeCount: 0,
    };
    setMessages(prev => [...prev, initialMsg]);

    // Track steps outside React to avoid stale closures
    const steps: ExecutionStep[] = [];
    let stepCounter = 0;

    /** Add or update steps and sync to message state */
    const syncSteps = (extras: Partial<ChatMessage> = {}) => {
      setMessages(prev => prev.map(m =>
        (m as any)._streamId === streamMsgId
          ? { ...m, _steps: [...steps], ...extras } as ChatMessage
          : m
      ));
    };

    try {
      const res = await fetch('/api/workflows/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abort.signal,
        body: JSON.stringify({
          message: text,
          workflow: { nodes: nodes.map(n => ({ id: n.id, position: n.position, data: n.data })), edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target })) },
          last_result: lastResult,
          session_id: sessionIdRef.current,
        }),
      });

      if (!res.body) throw new Error('No response body');

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let nodeCount = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // NDJSON: split by newline, keep incomplete last line in buffer
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          let event: any;
          try {
            event = JSON.parse(line);
          } catch {
            continue;
          }

          const eventType = event.type;

          if (eventType === 'thinking') {
            // Mark previous step as done, add new running step
            if (steps.length > 0) {
              steps[steps.length - 1].status = 'done';
            }
            const { tool, detail } = parseToolFromThinking(event.content || '');
            stepCounter++;
            steps.push({
              id: `step-${stepCounter}`,
              label: tool,
              detail: detail,
              status: 'running',
              timestamp: Date.now(),
            });
            syncSteps({ _thinking: true });

          } else if (eventType === 'text') {
            // Agent reply text — mark all steps done
            steps.forEach(s => { if (s.status === 'running') s.status = 'done'; });
            syncSteps({ content: event.content, _thinking: false });

          } else if (eventType === 'node') {
            // Add node to canvas + update current step with badge
            const { node, edge, reconnect_edge } = event;
            if (node) {
              nodeCount++;
              const nodeType = node.data?.type || '';
              // Update current running step with node badge
              if (steps.length > 0 && steps[steps.length - 1].status === 'running') {
                steps[steps.length - 1].nodeBadge = nodeType;
                steps[steps.length - 1].detail = NODE_TYPE_LABELS[nodeType] || nodeType;
              }
              syncSteps({ _nodeCount: nodeCount });

              onNodesChange([{
                type: 'add',
                item: {
                  id: node.id,
                  type: 'workflowNode',
                  position: node.position,
                  data: node.data,
                },
              }]);
              if (edge) {
                onEdgesChange([{ type: 'add', item: {
                  id: edge.id, source: edge.source, target: edge.target,
                }}]);
              }
              if (reconnect_edge) {
                onEdgesChange([{ type: 'remove', id: reconnect_edge.id }]);
              }
            }

          } else if (eventType === 'node_update') {
            const node = event.node;
            if (node) {
              setNodes(prev => prev.map(n =>
                n.id === node.id ? { ...n, data: { ...n.data, ...node.data } } : n
              ));
            }

          } else if (eventType === 'node_remove') {
            const { node_id, remove_edges, new_edge } = event;
            setNodes(prev => prev.filter(n => n.id !== node_id));
            if (remove_edges) {
              remove_edges.forEach((eid: string) => {
                onEdgesChange([{ type: 'remove', id: eid }]);
              });
            }
            if (new_edge) {
              onEdgesChange([{ type: 'add', item: new_edge }]);
            }

          } else if (eventType === 'finish') {
            // Agent called finish — add finish step
            if (steps.length > 0) {
              steps[steps.length - 1].status = 'done';
            }
            stepCounter++;
            steps.push({
              id: `step-${stepCounter}`,
              label: 'finish',
              detail: event.summary || '工作流完成',
              status: 'done',
              timestamp: Date.now(),
            });
            syncSteps({ _thinking: false });

          } else if (eventType === 'done') {
            // Stream complete — finalize all steps
            steps.forEach(s => { if (s.status === 'running') s.status = 'done'; });
            syncSteps({
              _thinking: false,
              _streamId: undefined,
              content: undefined, // don't override if text was already set
              _nodeCount: nodeCount,
            });
            // Ensure content is set if not already
            setMessages(prev => prev.map(m => {
              if ((m as any)._streamId === streamMsgId) {
                return {
                  ...m,
                  _streamId: undefined,
                  content: m.content || (nodeCount > 0 ? `✅ 完成，共 ${nodeCount} 个节点` : '✅ 完成'),
                } as ChatMessage;
              }
              return m;
            }));

          } else if (eventType === 'error') {
            // Error — mark current step as error
            if (steps.length > 0) {
              steps[steps.length - 1].status = 'error';
            }
            syncSteps({ _thinking: false, _streamId: undefined });
            throw new Error(event.error || '执行失败');
          }
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setMessages(prev => prev.map(m =>
          (m as any)._streamId === streamMsgId
            ? { ...m, content: `❌ ${err.message}`, _thinking: false, _streamId: undefined } as ChatMessage
            : m
        ));
      }
    } finally {
      setChatBusy(false);
    }
  }, [lastResult, onNodesChange, onEdgesChange, clearCanvas]);

  const detectedSites = browserContexts.map(c => c.name).join(' · ');

  return (
    <div className="h-full flex bg-white text-zinc-900">
      {/* Left: Chat panel */}
      <ChatPanel
        messages={messages}
        busy={chatBusy || isGenerating}
        onSend={handleChatSend}
        onStop={() => { chatAbortRef.current?.abort(); setChatBusy(false); }}
        onClearContext={clearContext}
        contexts={browserContexts.map(c => ({ name: c.name, url: c.url }))}
        totalTokens={messages.reduce((sum, m) => sum + (m.tokens || 0), 0)}
      />

      {/* Right: Canvas + controls */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <div className="shrink-0 flex items-center justify-between px-4 py-2 border-b border-zinc-100 bg-white">
          <span className="text-xs font-semibold text-zinc-500">Micro-Sniper</span>
          <div className="flex items-center gap-3">
            {detectedSites && (
              <span className="text-[10px] text-zinc-400">登录态: {detectedSites}</span>
            )}
            <div className={`w-2 h-2 rounded-full ${
              chatBusy || isGenerating ? 'bg-amber-400 animate-pulse' :
              workflowStatus === 'running' ? 'bg-amber-400 animate-pulse' :
              workflowStatus === 'completed' ? 'bg-emerald-400' :
              workflowStatus === 'failed' ? 'bg-red-400' :
              'bg-zinc-200'
            }`} />
            <span className="text-[10px] text-zinc-400">
              {nodes.length} nodes
            </span>
          </div>
        </div>

        {/* Canvas */}
        <div className="flex-1 min-h-0 relative">
          <Canvas
            nodes={nodes}
            edges={edges}
            activeNodeId={runningNodeId}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onDrop={addNode}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            onUpdateConfig={updateNodeConfig}
            sessions={browserContexts}
          />
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ReactFlowProvider>
      <WorkflowEditor />
    </ReactFlowProvider>
  );
}
