'use client';

import React from 'react';
import { 
  ChevronLeft, 
  ChevronRight, 
  History, 
  Settings, 
  User, 
  Zap 
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';

export default function TopNavbar() {
  const { currentStepIndex, historySteps, goBack, goForward } = useAppStore();

  const canGoBack = currentStepIndex > 0;
  const canGoForward = currentStepIndex < historySteps.length - 1;

  return (
    <header className="sticky top-0 z-50 w-full border-b border-white/5 bg-[#0c0e12]/80 backdrop-blur-xl shrink-0 h-[70px]">
      <div className="flex h-full items-center justify-between px-6">
        {/* Left: Branding & Nav Controls */}
        <div className="flex items-center gap-8">
          <div className="flex items-center gap-3 group translate-y-[2px]">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-[#00CCFF] to-[#0099FF] shadow-[0_0_15px_rgba(0,204,255,0.4)] transition-transform group-hover:scale-105">
              <Zap className="h-6 w-6 text-white fill-white" />
            </div>
            <div className="flex flex-col">
              <span className="text-lg font-bold tracking-tight text-white/90">AI Copilot</span>
              <span className="text-[10px] font-medium uppercase tracking-widest text-[#00CCFF]/60 -mt-1">Operational Engine</span>
            </div>
          </div>

          <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 p-1 backdrop-blur-md">
            <button
              onClick={goBack}
              disabled={!canGoBack}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-full transition-all",
                canGoBack ? "text-white/70 hover:bg-white/10 hover:text-white" : "text-white/20 cursor-not-allowed"
              )}
            >
              <ChevronLeft className="h-5 w-5" />
            </button>
            <div className="h-4 w-[1px] bg-white/10 mx-1" />
            <button
              onClick={goForward}
              disabled={!canGoForward}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-full transition-all",
                canGoForward ? "text-white/70 hover:bg-white/10 hover:text-white" : "text-white/20 cursor-not-allowed"
              )}
            >
              <ChevronRight className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Right: Actions & Profile */}
        <div className="flex items-center gap-4">
          <button className="flex h-10 w-10 items-center justify-center rounded-xl text-white/50 transition-colors hover:bg-white/5 hover:text-[#00CCFF]">
            <History className="h-5 w-5" />
          </button>
          <button className="flex h-10 w-10 items-center justify-center rounded-xl text-white/50 transition-colors hover:bg-white/5 hover:text-[#00CCFF]">
            <Settings className="h-5 w-5" />
          </button>
          <div className="h-6 w-[1px] bg-white/10 mx-2" />
          <div className="flex items-center gap-3 pl-2">
            <div className="flex flex-col items-end">
              <span className="text-sm font-medium text-white/90">Vahin S.</span>
              <span className="text-[10px] text-white/40">Premium Tier</span>
            </div>
            <div className="flex h-10 w-10 items-center justify-center rounded-full border border-[#00CCFF]/20 bg-gradient-to-br from-[#1a202c] to-[#0c0e12] p-[1px]">
              <div className="flex h-full w-full items-center justify-center rounded-full bg-[#0c0e12] overflow-hidden">
                <User className="h-5 w-5 text-white/40" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}
