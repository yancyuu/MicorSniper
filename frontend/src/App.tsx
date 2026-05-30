import { useState, useCallback, useEffect, useRef } from 'react';
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import useWorkflow from './hooks/useWorkflow';
import LogPanel from './components/LogPanel';
import Canvas from './components/Canvas';
import ChatPanel, { type ChatMessage } from './components/ChatPanel';
import type { NodeType } from './types/workflow';

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
      let replyText = '';
      let isGenerate = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';

        for (const part of parts) {
          if (!part.trim()) continue;

          let eventType = 'message';
          let eventData = '';
          for (const line of part.split('\n')) {
            if (line.startsWith('event:')) {
              eventType = line.slice(6).trim();
            } else if (line.startsWith('data:')) {
              eventData += (eventData ? '\n' : '') + line.slice(5).trim();
            }
          }
          if (!eventData) continue;

          try {
            const payload = JSON.parse(eventData);

            if (eventType === 'reply') {
              replyText = payload.reply || '处理中...';
              isGenerate = payload.intent === 'generate';
              if (isGenerate) clearCanvas();  // Only clear for new generation
              const botMsg: ChatMessage = {
                role: 'assistant',
                content: replyText,
                timestamp: new Date().toISOString(),
              };
              setMessages(prev => [...prev, botMsg]);
            } else if (eventType === 'node') {
              // Add node dynamically one by one
              const { node, edge, reconnect_edge } = payload;
              if (node) {
                nodeCount++;
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
                    id: edge.id,
                    source: edge.source,
                    target: edge.target,
                  }}]);
                }
                // Handle reconnection if node was inserted between two existing nodes
                if (reconnect_edge) {
                  onEdgesChange([{ type: 'remove', id: reconnect_edge.id }]);
                }
              }
            } else if (eventType === 'node_update') {
              // Update an existing node in place
              const node = payload.node;
              if (node) {
                setNodes(prev => prev.map(n =>
                  n.id === node.id ? { ...n, data: { ...n.data, ...node.data } } : n
                ));
              }
            } else if (eventType === 'node_remove') {
              // Remove a node and its edges
              const { node_id, remove_edges, new_edge } = payload;
              setNodes(prev => prev.filter(n => n.id !== node_id));
              if (remove_edges) {
                remove_edges.forEach((eid: string) => {
                  onEdgesChange([{ type: 'remove', id: eid }]);
                });
              }
              if (new_edge) {
                onEdgesChange([{ type: 'add', item: new_edge }]);
              }
            } else if (eventType === 'result') {
              // Non-generate intent result
              const botMsg: ChatMessage = {
                role: 'assistant',
                content: payload.reply || '处理完成',
                timestamp: new Date().toISOString(),
                data: payload,
              };
              setMessages(prev => [...prev, botMsg]);

              if (payload.nodes && payload.edges) {
                clearCanvas();
                payload.nodes.forEach((n: any) => {
                  onNodesChange([{ type: 'add', item: {
                    id: n.id, type: 'workflowNode', position: n.position, data: n.data,
                  }}]);
                });
                payload.edges.forEach((e: any) => {
                  onEdgesChange([{ type: 'add', item: e }]);
                });
              }

              // Export
              if (payload.export) {
                const blob = new Blob(
                  [JSON.stringify(payload.export.data, null, 2)],
                  { type: 'application/json' }
                );
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `export.${payload.export.format}`;
                a.click();
                URL.revokeObjectURL(url);
              }
            } else if (eventType === 'items') {
              // Update last result
              setLastResult(payload);
            } else if (eventType === 'done') {
              // Finished — update reply with node count
              if (nodeCount > 0) {
                setMessages(prev => {
                  const last = prev[prev.length - 1];
                  if (last?.role === 'assistant') {
                    return [...prev.slice(0, -1), { ...last, content: `✅ 完成 ${nodeCount} 个节点` }];
                  }
                  return prev;
                });
              }
            } else if (eventType === 'error') {
              throw new Error(payload.error || '执行失败');
            }
          } catch (parseErr: any) {
            if (!(parseErr instanceof SyntaxError)) throw parseErr;
          }
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        const errMsg: ChatMessage = {
          role: 'assistant',
          content: `❌ ${err.message}`,
          timestamp: new Date().toISOString(),
        };
        setMessages(prev => [...prev, errMsg]);
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
