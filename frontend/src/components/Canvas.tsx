import { type DragEvent, type MouseEvent, useCallback, useMemo, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  BackgroundVariant,
  type ReactFlowInstance,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import WorkflowNode, { FlowEdge } from './nodes/WorkflowNode';
import type { NodeType, BrowserContext } from '../types/workflow';

interface CanvasProps {
  nodes: any[];
  edges: any[];
  activeNodeId?: string | null;
  onNodesChange: (changes: any[]) => void;
  onEdgesChange: (changes: any[]) => void;
  onConnect: (connection: any) => void;
  onDrop: (type: NodeType, position: { x: number; y: number }) => void;
  onNodeClick: (event: MouseEvent, node: any) => void;
  onPaneClick: () => void;
  onUpdateConfig?: (nodeId: string, config: Record<string, any>) => void;
  sessions?: BrowserContext[];
}

export default function Canvas({
  nodes,
  edges,
  activeNodeId,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onDrop,
  onNodeClick,
  onPaneClick,
  onUpdateConfig,
  sessions,
}: CanvasProps) {
  const reactFlowRef = useRef<HTMLDivElement>(null);
  const reactFlowInstance = useRef<ReactFlowInstance | null>(null);

  // Wrap nodeTypes to inject onUpdateConfig and sessions props
  const nodeTypes = useMemo(() => ({
    workflowNode: (props: any) => (
      <WorkflowNode
        {...props}
        onUpdateConfig={onUpdateConfig}
        sessions={sessions}
      />
    ),
  }), [onUpdateConfig, sessions]);

  const edgeTypes = useMemo(() => ({
    flowEdge: FlowEdge,
  }), []);

  const defaultEdgeOptions = useMemo(() => ({
    type: 'flowEdge',
    animated: false,
  }), []);

  const onInit = useCallback((instance: ReactFlowInstance) => {
    reactFlowInstance.current = instance;
  }, []);

  const onDragOver = useCallback((event: DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const handleDrop = useCallback(
    (event: DragEvent) => {
      event.preventDefault();

      const type = event.dataTransfer.getData('application/reactflow-nodetype') as NodeType;
      if (!type || !reactFlowInstance.current) return;

      const bounds = reactFlowRef.current?.getBoundingClientRect();
      if (!bounds) return;

      const position = reactFlowInstance.current.screenToFlowPosition({
        x: event.clientX - bounds.left,
        y: event.clientY - bounds.top,
      });

      onDrop(type, position);
    },
    [onDrop],
  );

  const isValidConnection = useCallback((connection: any) => {
    return connection.source !== connection.target;
  }, []);

  return (
    <div ref={reactFlowRef} style={{ width: '100%', height: '100%' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onInit={onInit}
        onDragOver={onDragOver}
        onDrop={handleDrop}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        isValidConnection={isValidConnection}
        fitView
        fitViewOptions={{ padding: 0.3, minZoom: 0.2, maxZoom: 1 }}
        defaultViewport={{ x: 0, y: 0, zoom: 0.7 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#f0f0f0" />
        <Controls
          className="!bg-white !border-zinc-200 !rounded-xl !shadow-lg [&>button]:!bg-white [&>button]:!border-zinc-200 [&>button]:!fill-zinc-400 [&>button:hover]:!bg-zinc-50 [&>button]:!rounded-lg"
          position="bottom-left"
        />
        <MiniMap
          className="!bg-white !border-zinc-200 !rounded-xl !shadow-lg"
          nodeColor={() => '#c4b5fd'}
          maskColor="rgba(255, 255, 255, 0.85)"
          position="bottom-right"
        />
      </ReactFlow>
    </div>
  );
}
