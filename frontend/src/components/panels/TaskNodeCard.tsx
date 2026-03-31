'use client';

import React, { memo } from 'react';
import { Handle, Position, NodeProps, Node } from '@xyflow/react';
import { motion } from 'framer-motion';
import { CheckCircle2, Circle, Clock, AlertCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

export type TaskNodeData = {
  label: string;
  status?: 'pending' | 'active' | 'completed' | 'failed';
  reasoning?: string;
};

const TaskNodeCard = ({ data, selected }: NodeProps<Node<TaskNodeData>>) => {
  const status = data.status || 'pending';

  return (
    <motion.div
      initial={{ scale: 0.9, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      className={cn(
        "relative min-w-[240px] max-w-[300px] rounded-2xl p-0.5 transition-all duration-500",
        selected || status === 'active' 
          ? "bg-gradient-to-br from-[#00CCFF] to-[#0099FF] shadow-[0_0_30px_-10px_rgba(0,204,255,0.5)]" 
          : "bg-white/10"
      )}
    >
      <div className="rounded-[calc(1rem-2px)] bg-[#0c0e12]/90 backdrop-blur-xl p-4 h-full flex flex-col gap-3">
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <h3 className="text-sm font-semibold text-white/90 leading-tight">
            {data.label}
          </h3>
          <div className="shrink-0 pt-0.5">
            {status === 'completed' && <CheckCircle2 className="h-4 w-4 text-emerald-400" />}
            {status === 'active' && <Clock className="h-4 w-4 text-[#00CCFF] animate-pulse" />}
            {status === 'pending' && <Circle className="h-4 w-4 text-white/20" />}
            {status === 'failed' && <AlertCircle className="h-4 w-4 text-rose-400" />}
          </div>
        </div>

        {/* Reasoning logic */}
        {data.reasoning && (
           <p className="text-[11px] text-white/40 leading-relaxed line-clamp-2 italic border-l border-white/10 pl-2">
             {data.reasoning}
           </p>
        )}

        {/* Status Badge */}
        <div className="flex items-center justify-between mt-1">
          <span className={cn(
            "text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border",
            status === 'active' ? "bg-[#00CCFF]/10 border-[#00CCFF]/30 text-[#00CCFF]" : "bg-white/5 border-white/10 text-white/30"
          )}>
            {status}
          </span>
        </div>
      </div>
      
      {/* Handles */}
      <Handle 
        type="target" 
        position={Position.Top} 
        className="w-3 h-3 !bg-[#00CCFF] !border-2 !border-[#0c0e12] opacity-0 group-hover:opacity-100 transition-opacity" 
      />
      <Handle 
        type="source" 
        position={Position.Bottom} 
        className="w-3 h-3 !bg-[#00CCFF] !border-2 !border-[#0c0e12] opacity-0 group-hover:opacity-100 transition-opacity" 
      />
    </motion.div>
  );
};

export default memo(TaskNodeCard);
