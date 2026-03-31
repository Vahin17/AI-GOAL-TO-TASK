'use client';

import React, { useRef, useEffect } from 'react';
import { Send, Sparkles, User, CornerDownLeft } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';

export default function ChatPanel() {
  const { messages, isTyping, addMessage, setTyping, updateWorkflow, addLog } = useAppStore();
  const [inputValue, setInputValue] = React.useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isTyping]);

  const handleSend = async () => {
    if (!inputValue.trim() || isTyping) return;

    const userText = inputValue.trim();
    setInputValue('');
    addMessage({ role: 'user', content: userText });
    setTyping(true);
    addLog(`User goal received: "${userText}"`);

    // Mock Interaction Flow
    setTimeout(() => {
      addMessage({ role: 'assistant', content: 'Understood. I am decomposing your goal into a structured task workflow. Please review the center panel.' });
      
      // Update Graph with Mock Data
      const mockNodes = [
        { id: '1', type: 'input', position: { x: 250, y: 0 }, data: { label: 'Goal Initialization' } },
        { id: '2', type: 'default', position: { x: 250, y: 150 }, data: { label: 'Task Analysis' } },
        { id: '3', type: 'output', position: { x: 250, y: 300 }, data: { label: 'Workflow Ready' } }
      ];
      const mockEdges = [
        { id: 'e1-2', source: '1', target: '2', animated: true },
        { id: 'e2-3', source: '2', target: '3', animated: true }
      ];
      
      updateWorkflow(mockNodes, mockEdges, 'The goal has been analyzed and the primary execution steps are mapped.', 'planning');
      addLog('Workflow decomposition complete.');
      setTyping(false);
    }, 1500);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex h-full flex-col bg-[#0c0e12]/30 border-r border-white/5 relative">
      {/* Thread Header */}
      <div className="px-6 py-4 border-b border-white/5 flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-[#00CCFF]" />
        <span className="text-sm font-semibold text-white/80 tracking-wide uppercase italic">Operational Thread</span>
      </div>

      {/* Message List */}
      <div 
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-6 py-8 space-y-8 scroll-smooth custom-scrollbar"
      >
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-center space-y-4 opacity-40">
            <div className="h-16 w-16 rounded-full border border-[#00CCFF]/20 flex items-center justify-center bg-[#00CCFF]/5">
              <Zap className="h-8 w-8 text-[#00CCFF]" />
            </div>
            <div className="max-w-[200px]">
              <p className="text-sm text-white">How can I assist your operation today?</p>
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <div 
            key={msg.id}
            className={cn(
              "flex flex-col space-y-3 animate-in fade-in slide-in-from-bottom-2 duration-300",
              msg.role === 'user' ? "items-end" : "items-start"
            )}
          >
            <div className={cn(
              "flex items-center gap-2",
              msg.role === 'user' ? "flex-row-reverse" : "flex-row"
            )}>
              <div className={cn(
                "h-7 w-7 rounded-lg flex items-center justify-center border",
                msg.role === 'user' ? "bg-white/5 border-white/10" : "bg-[#00CCFF]/10 border-[#00CCFF]/20"
              )}>
                {msg.role === 'user' ? <User className="h-4 w-4 text-white/50" /> : <Sparkles className="h-4 w-4 text-[#00CCFF]" />}
              </div>
              <span className="text-[10px] font-bold uppercase tracking-widest text-white/30">
                {msg.role === 'user' ? 'Operator' : 'Assistant'}
              </span>
            </div>
            <div className={cn(
              "max-w-[85%] rounded-2xl px-5 py-3 text-sm leading-relaxed",
              msg.role === 'user' 
                ? "bg-white/5 text-white/90 border border-white/10 rounded-tr-none" 
                : "bg-[#0c0e12] text-white/80 border border-[#00CCFF]/20 shadow-[0_0_20px_-10px_rgba(0,204,255,0.3)] rounded-tl-none"
            )}>
              {msg.content}
            </div>
          </div>
        ))}

        {isTyping && (
          <div className="flex flex-col items-start space-y-3 animate-pulse">
             <div className="flex items-center gap-2">
                <div className="h-7 w-7 rounded-lg flex items-center justify-center border bg-[#00CCFF]/10 border-[#00CCFF]/20">
                  <Sparkles className="h-4 w-4 text-[#00CCFF]" />
                </div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-white/30">Assistant</span>
             </div>
             <div className="bg-[#0c0e12] rounded-2xl rounded-tl-none px-5 py-3 border border-[#00CCFF]/20 flex gap-1 items-center">
                <div className="h-1.5 w-1.5 rounded-full bg-[#00CCFF]/50 animate-bounce" />
                <div className="h-1.5 w-1.5 rounded-full bg-[#00CCFF]/50 animate-bounce [animation-delay:0.2s]" />
                <div className="h-1.5 w-1.5 rounded-full bg-[#00CCFF]/50 animate-bounce [animation-delay:0.4s]" />
             </div>
          </div>
        )}
      </div>

      {/* Input Bar */}
      <div className="p-6">
        <div className="relative group">
          <div className="absolute -inset-1 bg-gradient-to-r from-[#00CCFF] to-[#0099FF] rounded-2xl opacity-10 blur transition duration-500 group-hover:opacity-20" />
          <div className="relative bg-[#0c0e12]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-1.5 focus-within:border-[#00CCFF]/40 transition-colors">
            <textarea
              rows={1}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Command your operation..."
              className="w-full bg-transparent border-none focus:ring-0 text-sm py-3 px-4 resize-none h-auto max-h-32 text-white placeholder:text-white/20 overflow-hidden"
            />
            <div className="flex items-center justify-between px-3 pb-2 pt-1">
              <div className="flex items-center gap-2">
                <kbd className="hidden sm:inline-flex h-5 items-center gap-1 rounded border border-white/5 bg-white/5 px-1.5 font-sans text-[10px] font-medium text-white/20">
                  <span className="text-xs">⌘</span> Enter to send
                </kbd>
              </div>
              <button 
                onClick={handleSend}
                disabled={!inputValue.trim() || isTyping}
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-xl transition-all",
                  inputValue.trim() && !isTyping
                    ? "bg-[#00CCFF] text-white shadow-[0_0_15px_rgba(0,204,255,0.4)] hover:scale-105"
                    : "bg-white/5 text-white/20 cursor-not-allowed"
                )}
              >
                <Send className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Zap(props: any) {
  return (
    <svg
      {...props}
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  )
}
