'use client';

import React, { useMemo } from 'react';
import { 
  ReactFlow, 
  Background, 
  Controls, 
  Panel,
  BackgroundVariant 
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useAppStore } from '@/store/useAppStore';
import TaskNodeCard from './TaskNodeCard';

// Register custom node types
const nodeTypes = {
  task: TaskNodeCard,
};

export default function WorkflowCanvas() {
  const { nodes, edges } = useAppStore();

  const defaultEdgeOptions = useMemo(() => ({
    animated: true,
    style: { stroke: '#00CCFF', strokeWidth: 2, opacity: 0.6 },
  }), []);

  return (
    <div className="relative h-full w-full bg-[#0c0e12] overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        fitView
        className="workflow-canvas"
      >
        <Background 
          color="#00CCFF" 
          gap={30} 
          size={1} 
          variant={BackgroundVariant.Dots} 
          className="opacity-5" 
        />
        
        <Panel position="top-right" className="bg-[#0c0e12]/80 backdrop-blur-md border border-white/10 rounded-xl p-2 m-4">
           <div className="flex items-center gap-3 px-2">
              <div className="flex items-center gap-2">
                 <div className="h-2 w-2 rounded-full bg-[#00CCFF] shadow-[0_0_8px_#00CCFF]" />
                 <span className="text-[10px] font-bold text-white/50 uppercase tracking-tighter">Live Sync Card</span>
              </div>
              <div className="h-3 w-[1px] bg-white/10" />
              <div className="flex items-center gap-2">
                 <div className="h-2 w-2 rounded-full bg-white/10" />
                 <span className="text-[10px] font-bold text-white/30 uppercase tracking-tighter">Static Cache</span>
              </div>
           </div>
        </Panel>

        <Controls 
          className="m-6 border border-white/10 !bg-[#0c0e12]/80 !backdrop-blur-xl !shadow-none !rounded-xl overflow-hidden" 
          showInteractive={false}
        />
      </ReactFlow>

      {/* Hero Label Overlay */}
      <div className="absolute top-8 left-8 z-10 pointer-events-none">
        <h2 className="text-2xl font-bold text-white/90 tracking-tight">Logical Transformation</h2>
        <div className="flex items-center gap-2 mt-1">
          <div className="h-[2px] w-8 bg-[#00CCFF]" />
          <p className="text-xs font-medium text-[#00CCFF]/60 uppercase tracking-widest">Workflow Engine V1.0</p>
        </div>
      </div>
    </div>
  );
}
