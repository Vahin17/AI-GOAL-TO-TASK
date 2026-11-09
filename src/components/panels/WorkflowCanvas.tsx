'use client';

import React, { useMemo, useEffect, useState } from 'react';
import { 
  ReactFlow, 
  Background, 
  Controls, 
  Panel,
  BackgroundVariant,
  ConnectionLineType,
  Node,
  Edge,
  Position
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useARDOStore } from '@/store/useARDOStore';
import TaskNodeCard from './TaskNodeCard';
import { motion } from 'framer-motion';
import * as dagre from 'dagre';

// Register custom node types
const nodeTypes = {
  task: TaskNodeCard,
};

const getLayoutedElements = (nodes: Node[], edges: Edge[], direction = 'TB') => {
  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));
  
  const isHorizontal = direction === 'LR';
  dagreGraph.setGraph({ rankdir: direction });

  nodes.forEach((node) => {
    dagreGraph.setNode(node.id, { width: 300, height: 150 });
  });

  edges.forEach((edge) => {
    dagreGraph.setEdge(edge.source, edge.target);
  });

  dagre.layout(dagreGraph);

  const newNodes = nodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);
    const newNode = { ...node };
    newNode.targetPosition = isHorizontal ? Position.Left : Position.Top;
    newNode.sourcePosition = isHorizontal ? Position.Right : Position.Bottom;

    // We are shifting the dagre node position (anchor=center center) to the top left
    // so it matches the React Flow node anchor point (top left).
    newNode.position = {
      x: nodeWithPosition.x - 300 / 2,
      y: nodeWithPosition.y - 150 / 2,
    };

    return newNode;
  });

  return { nodes: newNodes, edges };
};

export default function WorkflowCanvas() {
  const { nodeMap } = useARDOStore();

  const { nodes, edges } = useMemo(() => {
    if (nodeMap.size === 0) {
      return { nodes: [], edges: [] };
    }

    const rawNodes: Node[] = Array.from(nodeMap.values()).map(node => ({
      id: node.task_id,
      type: 'task',
      position: { x: 0, y: 0 },
      data: {
        label: node.task_name,
        status: node.status,
        priorityScore: node.priority_score,
        riskScore: node.risk_score,
        retryCount: node.retry_count,
        maxRetries: node.max_retries,
        executionType: node.execution_type,
        parallelGroup: node.parallel_group,
        approvalNeeded: node.approval_needed,
        taskId: node.task_id,
        eta: node.estimated_execution_time ? `${node.estimated_execution_time.toFixed(1)}s` : undefined
      }
    }));

    const rawEdges: Edge[] = Array.from(nodeMap.values()).flatMap(node => 
      node.dependency_ids.map(depId => ({
        id: `e${depId}-${node.task_id}`,
        source: depId,
        target: node.task_id,
        type: 'default',
        animated: true,
        style: { stroke: '#11999E', strokeWidth: 2, opacity: 0.15 }
      }))
    );

    return getLayoutedElements(rawNodes, rawEdges, 'TB');
  }, [nodeMap]);

  const defaultEdgeOptions = useMemo(() => ({
    type: ConnectionLineType.Bezier,
    animated: true,
    style: { 
      stroke: '#11999E', // secondary color
      strokeWidth: 2, 
      opacity: 0.15
    },
  }), []);

  return (
    <div className="relative h-full w-full min-h-[400px] min-w-[300px] bg-white overflow-hidden font-sans">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        fitView
        minZoom={0.2}
        className="workflow-canvas"
        style={{ width: '100%', height: '100%' }}
      >
        <Background 
          color="#30E3CA" // primary accent
          gap={30} 
          size={1} 
          variant={BackgroundVariant.Dots} 
          className="opacity-[0.12]" 
        />
        
        {/* Visual Map Legend Panel */}
        <Panel position="top-right" className="bg-white border border-secondary/10 rounded-2xl p-4 shadow-sm m-6 backdrop-blur-md">
           <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between gap-6 border-b border-secondary/5 pb-2">
                 <span className="text-[10px] font-bold text-secondary uppercase tracking-widest leading-none">Workflow Legend</span>
                 <div className="h-2 w-2 rounded-full bg-primary" />
              </div>
              <div className="flex flex-col gap-3">
                 <div className="flex items-center gap-3">
                    <div className="h-2.5 w-2.5 rounded-full bg-[#30E3CA] shadow-sm animate-pulse" />
                    <span className="text-[10px] font-bold text-foreground opacity-80 uppercase tracking-tight">Active Sequence</span>
                 </div>
                 <div className="flex items-center gap-3">
                    <div className="h-2.5 w-2.5 rounded-full bg-zinc-300 border border-zinc-200" />
                    <span className="text-[10px] font-bold text-secondary uppercase tracking-tight">Queued Task</span>
                 </div>
                 <div className="flex items-center gap-3">
                    <div className="h-2.5 w-2.5 rounded-full bg-violet-500 shadow-sm animate-pulse" />
                    <span className="text-[10px] font-bold text-secondary uppercase tracking-tight">Approval Needed</span>
                 </div>
              </div>
           </div>
        </Panel>

        <Controls 
          className="m-6 border border-secondary/10 !bg-white !shadow-sm !rounded-2xl overflow-hidden !p-1.5" 
          showInteractive={false}
        />
      </ReactFlow>

      {/* Hero Label Overlay */}
      <div className="absolute top-10 left-10 z-10 pointer-events-none">
        <div className="flex flex-col gap-2">
           <div className="flex items-center gap-2">
              <motion.div 
                initial={{ width: 0 }}
                animate={{ width: 30 }}
                transition={{ duration: 0.6 }}
                className="h-[2px] bg-primary rounded-full" 
              />
              <span className="text-[11px] font-black text-secondary uppercase tracking-[0.2em] translate-y-[0.5px]">Operational Blueprint</span>
           </div>
           <h2 className="text-3xl font-black text-foreground tracking-tighter leading-none">
             Goal Logic Graph
           </h2>
           <p className="text-sm font-semibold text-secondary/60 mt-2 max-w-[280px] leading-relaxed">
             Real-time decomposition of strategic objectives into executable task sequential nodes.
           </p>
        </div>
      </div>

      {/* Subtle Background Watermark */}
      <div className="absolute bottom-8 left-1/2 -translate-x-1/2 opacity-10 pointer-events-none select-none">
         <span className="text-[10px] font-black text-secondary uppercase tracking-[0.6em] italic">Proprietary logic mapping system v1.2</span>
      </div>
    </div>
  );
}

