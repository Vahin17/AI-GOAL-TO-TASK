'use client';

import React, { useRef, useEffect, useState } from 'react';
import { Send, Sparkles, Zap, Info, Globe, RotateCcw, ShieldAlert, CheckCircle2, Layers } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { useARDOStore } from '@/store/useARDOStore';
import { cn } from '@/lib/utils';
import { motion, AnimatePresence } from 'framer-motion';

const PRESET_GOALS = [
  {
    id: 'browser_demo',
    title: 'Playwright Browser E2E',
    icon: Globe,
    badge: 'Real Chromium',
    badgeClass: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    prompt: "Open the internal operations dashboard, search for project Phoenix, update the project note to 'Sprint 42 Verified Deployment', verify the result, and capture screenshot evidence.",
  },
  {
    id: 'memory_feedback',
    title: 'Closed Memory Loop',
    icon: RotateCcw,
    badge: 'RAG + Memory',
    badgeClass: 'bg-teal-50 text-teal-700 border-teal-200',
    prompt: "Update project notes on the internal operations dashboard again.",
  },
  {
    id: 'safety_block',
    title: 'Safety Policy BLOCK',
    icon: ShieldAlert,
    badge: 'Critical Gating',
    badgeClass: 'bg-rose-50 text-rose-700 border-rose-200',
    prompt: "Purge and overwrite root credential vault on administrative dashboard.",
  },
  {
    id: 'approval_gate',
    title: 'Human Approval Gate',
    icon: Layers,
    badge: 'Operator Sign-off',
    badgeClass: 'bg-amber-50 text-amber-700 border-amber-200',
    prompt: "Deploy container v1.2.0 to staging cluster",
  },
];

export default function ChatPanel() {
  const { messages, isTyping, addMessage, setTyping, addLog } = useAppStore();
  const { startSession, mode: activeMode } = useARDOStore();
  const [inputValue, setInputValue] = useState('');
  const [selectedMode, setSelectedMode] = useState<'safe' | 'sequential' | 'parallel' | 'aggressive'>('safe');
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isTyping]);

  const handleSend = async (customPrompt?: string) => {
    const textToSend = (customPrompt || inputValue).trim();
    if (!textToSend || isTyping) return;

    setInputValue('');
    addMessage({ role: 'user', content: textToSend });
    setTyping(true);
    addLog(`Goal synthesis requested: "${textToSend}" (Mode: ${selectedMode})`);

    try {
      await startSession({ goal: textToSend, mode: selectedMode });
      addMessage({ 
        role: 'assistant', 
        content: `Target objective analyzed and compiled into ARDO task DAG. Real-time orchestration active in ${selectedMode.toUpperCase()} mode.` 
      });
      addLog(`Reasoning Engine: ARDO session initialized successfully.`);
    } catch (err) {
      addMessage({ 
        role: 'assistant', 
        content: `Failed to synthesize plan: ${err}` 
      });
      addLog(`Error: ${err}`);
    } finally {
      setTyping(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex h-full flex-col bg-white overflow-hidden font-sans relative">
      {/* Header */}
      <div className="px-5 py-3.5 border-b border-secondary/10 flex items-center justify-between bg-white shrink-0">
        <div className="flex items-center gap-2">
           <Zap className="h-4 w-4 text-[#11999E]" />
           <h2 className="text-xs font-bold text-foreground uppercase tracking-wider">Goal Input & Dispatch</h2>
        </div>
        <div className="flex items-center gap-1.5">
           <span className="text-[10px] font-semibold text-slate-500">Mode:</span>
           <select 
             value={selectedMode} 
             onChange={(e) => setSelectedMode(e.target.value as any)}
             className="text-[10px] font-bold uppercase bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5 text-slate-700 outline-none cursor-pointer"
           >
             <option value="safe">Safe (Bounded)</option>
             <option value="parallel">Parallel</option>
             <option value="sequential">Sequential</option>
             <option value="aggressive">Aggressive</option>
           </select>
        </div>
      </div>

      {/* Message List */}
      <div 
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-5 py-5 space-y-4 scroll-smooth custom-scrollbar"
      >
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center text-center space-y-4 py-4 opacity-90">
            <div className="h-12 w-12 rounded-xl bg-teal-50 border border-teal-100 flex items-center justify-center">
              <Sparkles className="h-6 w-6 text-[#11999E]" />
            </div>
            <div className="max-w-xs flex flex-col gap-1">
              <p className="text-xs font-bold text-foreground">AI Execution Engine Ready</p>
              <p className="text-[11px] text-slate-500 leading-snug">
                Enter a natural language goal below or launch one of the verified demo scenarios:
              </p>
            </div>

            {/* Quick Demo Scenarios */}
            <div className="w-full space-y-2 pt-2 text-left">
              {PRESET_GOALS.map((item) => {
                const IconComponent = item.icon;
                return (
                  <button
                    key={item.id}
                    onClick={() => handleSend(item.prompt)}
                    className="w-full p-2.5 rounded-xl border border-slate-200 bg-slate-50 hover:bg-teal-50/50 hover:border-teal-300 transition-all text-left group flex flex-col gap-1 shadow-xs"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5 font-bold text-xs text-slate-800 group-hover:text-teal-800">
                        <IconComponent className="h-3.5 w-3.5 text-[#11999E]" />
                        <span>{item.title}</span>
                      </div>
                      <span className={cn("text-[9px] font-bold px-1.5 py-0.5 rounded border", item.badgeClass)}>
                        {item.badge}
                      </span>
                    </div>
                    <p className="text-[10px] text-slate-500 line-clamp-2 leading-relaxed">
                      {item.prompt}
                    </p>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <AnimatePresence initial={false} mode="popLayout">
          {messages.map((msg) => (
            <motion.div 
              key={msg.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className={cn(
                "flex flex-col space-y-1.5",
                msg.role === 'user' ? "items-end" : "items-start"
              )}
            >
              <div className={cn(
                "flex items-center gap-1.5",
                msg.role === 'user' ? "flex-row-reverse text-right" : "flex-row text-left"
              )}>
                <div className={cn(
                  "h-5 w-5 rounded flex items-center justify-center text-[9px] font-bold border",
                  msg.role === 'user' ? "bg-slate-800 border-slate-800 text-white" : "bg-teal-600 border-teal-600 text-white"
                )}>
                  {msg.role === 'user' ? 'U' : 'E'}
                </div>
                <span className="text-[9px] font-bold uppercase tracking-wider text-slate-400">
                  {msg.role === 'user' ? 'Operator' : 'Execution Engine'}
                </span>
              </div>
              <div className={cn(
                "max-w-[92%] rounded-xl px-4 py-2.5 text-xs leading-relaxed font-medium shadow-xs border",
                msg.role === 'user' 
                  ? "bg-slate-900 text-white border-slate-900 rounded-tr-none" 
                  : "bg-slate-50 text-slate-800 border-slate-200 rounded-tl-none font-sans"
              )}>
                {msg.content}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>

        {isTyping && (
           <motion.div 
             initial={{ opacity: 0, scale: 0.95 }}
             animate={{ opacity: 1, scale: 1 }}
             className="flex flex-col items-start space-y-1.5"
           >
              <div className="flex items-center gap-1.5 bg-teal-50 border border-teal-200 rounded px-2 py-0.5">
                 <Sparkles className="h-3 w-3 text-teal-600 animate-pulse" />
                 <span className="text-[9px] font-bold uppercase tracking-wider text-teal-800">Planning & Grounding...</span>
              </div>
              <div className="bg-slate-50 rounded-xl rounded-tl-none px-4 py-2.5 border border-slate-200 flex gap-1 items-center">
                 <div className="h-1.5 w-1.5 rounded-full bg-teal-400 animate-bounce" />
                 <div className="h-1.5 w-1.5 rounded-full bg-teal-600 animate-bounce [animation-delay:0.2s]" />
                 <div className="h-1.5 w-1.5 rounded-full bg-teal-800 animate-bounce [animation-delay:0.4s]" />
              </div>
           </motion.div>
        )}
      </div>

      {/* Input Section */}
      <div className="p-4 bg-white border-t border-slate-200 shadow-lg relative">
        <div className="relative">
          <textarea
            rows={2}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Enter goal or operational instructions..."
            className="w-full bg-slate-50 border border-slate-200 rounded-xl py-2.5 pl-3.5 pr-12 text-xs text-slate-900 focus:outline-none focus:ring-1 focus:ring-teal-500 focus:border-teal-500 focus:bg-white transition-all font-medium placeholder:text-slate-400 resize-none max-h-32"
          />
          <div className="absolute right-2.5 top-1/2 -translate-y-1/2">
            <button 
              onClick={() => handleSend()}
              disabled={!inputValue.trim() || isTyping}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-lg transition-all shadow-xs active:scale-95",
                inputValue.trim() && !isTyping
                  ? "bg-[#11999E] text-white shadow-teal-500/20 hover:bg-[#0e7c80]"
                  : "bg-slate-200 text-slate-400 cursor-not-allowed shadow-none"
              )}
            >
              <Send className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
        <div className="flex items-center justify-between mt-2 px-1 text-[9px] text-slate-400 font-medium">
          <span>Shift + Enter for new line</span>
          <span>PostgreSQL + pgvector Grounded</span>
        </div>
      </div>
    </div>
  );
}
