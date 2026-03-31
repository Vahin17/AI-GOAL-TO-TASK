'use client';

import React from 'react';
import { 
  Activity, 
  Terminal, 
  CheckCircle2, 
  Play, 
  Pause, 
  RotateCcw,
  Lightbulb,
  History
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';
import { motion, AnimatePresence } from 'framer-motion';

export default function ExecutionControl() {
  const { status, reasoning, logs } = useAppStore();

  return (
    <div className="flex h-full flex-col bg-[#0c0e12]/30 border-l border-white/5 overflow-y-auto custom-scrollbar">
      {/* Panel Header */}
      <div className="px-6 py-4 border-b border-white/5 flex items-center gap-2">
        <Activity className="h-4 w-4 text-[#00CCFF]" />
        <span className="text-sm font-semibold text-white/80 tracking-wide uppercase italic">Operational Insights</span>
      </div>

      <div className="p-6 space-y-6">
        
        {/* 1. Execution Status Card */}
        <section className="space-y-3">
          <div className="flex items-center justify-between">
             <h3 className="text-[10px] font-bold text-white/30 uppercase tracking-widest">Current Status</h3>
             {status !== 'idle' && (
                <span className="flex h-2 w-2 rounded-full bg-[#00CCFF] animate-ping" />
             )}
          </div>
          <div className="relative group">
            <div className="absolute -inset-0.5 bg-gradient-to-br from-[#00CCFF]/20 to-transparent rounded-2xl blur opacity-0 group-hover:opacity-100 transition duration-500" />
            <div className="relative bg-white/5 border border-white/10 rounded-2xl p-4 backdrop-blur-md">
              <div className="flex items-center gap-4">
                <div className={cn(
                  "h-12 w-12 rounded-xl flex items-center justify-center border shadow-inner",
                  status === 'idle' ? "bg-white/5 border-white/10" : "bg-[#00CCFF]/10 border-[#00CCFF]/30"
                )}>
                  {status === 'idle' && <Pause className="h-6 w-6 text-white/20" />}
                  {status === 'planning' && <Activity className="h-6 w-6 text-[#00CCFF] animate-pulse" />}
                  {status === 'executing' && <Play className="h-6 w-6 text-[#00CCFF]" />}
                  {status === 'completed' && <CheckCircle2 className="h-6 w-6 text-emerald-400" />}
                </div>
                <div className="flex flex-col">
                  <span className="text-sm font-semibold text-white/90 capitalize">{status}</span>
                  <span className="text-[10px] text-white/40 font-medium">Session ID: 5399476...</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* 2. Reasoning Summary */}
        <section className="space-y-3">
          <h3 className="text-[10px] font-bold text-white/30 uppercase tracking-widest flex items-center gap-2">
            <Lightbulb className="h-3 w-3" /> Reasoning Engine
          </h3>
          <div className="bg-[#0c0e12]/80 border border-white/5 rounded-2xl p-4 shadow-xl">
             <p className="text-xs text-white/60 leading-relaxed">
               {reasoning || 'Awaiting goal decomposition to initiate reasoning layer.'}
             </p>
          </div>
        </section>

        {/* 3. Operational Logs */}
        <section className="space-y-3 flex-1 flex flex-col">
          <h3 className="text-[10px] font-bold text-white/30 uppercase tracking-widest flex items-center gap-2">
            <Terminal className="h-3 w-3" /> Activity Logs
          </h3>
          <div className="flex-1 min-h-[160px] bg-black/40 rounded-2xl border border-white/5 font-mono text-[9px] p-4 overflow-y-auto space-y-2 custom-scrollbar">
            <AnimatePresence mode="popLayout">
              {logs.map((log, i) => (
                <motion.div 
                   key={i}
                   initial={{ opacity: 0, x: -5 }}
                   animate={{ opacity: 1, x: 0 }}
                   className="flex gap-2"
                >
                  <span className="text-[#00CCFF]/50 shrink-0 select-none">›</span>
                  <span className="text-white/40 leading-relaxed">{log}</span>
                </motion.div>
              ))}
            </AnimatePresence>
            {logs.length === 0 && (
              <div className="text-white/10 italic">No operational data recorded.</div>
            )}
          </div>
        </section>

        {/* 4. Action Controls */}
        <section className="pt-2 space-y-3">
           <h3 className="text-[10px] font-bold text-white/30 uppercase tracking-widest">Execute Controls</h3>
           <div className="grid grid-cols-2 gap-3">
              <button className="flex items-center justify-center gap-2 bg-white/5 border border-white/10 rounded-xl py-3 text-[11px] font-bold text-white/70 transition-all hover:bg-white/10 hover:text-white group">
                <RotateCcw className="h-3.5 w-3.5 group-hover:rotate-180 transition-transform duration-500" /> Reset Plan
              </button>
              <button 
                disabled={status === 'idle'}
                className={cn(
                  "flex items-center justify-center gap-2 rounded-xl py-3 text-[11px] font-bold shadow-[0_0_20px_-10px_rgba(0,204,255,0.4)] transition-all",
                  status !== 'idle' ? "bg-[#00CCFF] text-white hover:scale-[1.02] active:scale-95" : "bg-white/5 text-white/20 cursor-not-allowed border border-white/5"
                )}
              >
                <Play className="h-3.5 w-3.5 fill-current" /> Approve
              </button>
           </div>
        </section>

      </div>
    </div>
  );
}
