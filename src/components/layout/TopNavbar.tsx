'use client';

import React from 'react';
import { 
  ChevronLeft, 
  ChevronRight, 
  History, 
  Settings, 
  User, 
  Zap,
  Search,
  Bell
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { cn } from '@/lib/utils';

export default function TopNavbar() {
  const { 
    currentStepIndex, 
    historySteps, 
    goBack, 
    goForward, 
    toggleHistory, 
    toggleSettings, 
    isHistoryOpen, 
    isSettingsOpen 
  } = useAppStore();

  const canGoBack = currentStepIndex > 0;
  const canGoForward = currentStepIndex < historySteps.length - 1;

  return (
    <header className="w-full bg-white border border-secondary/10 rounded-2xl shrink-0 h-[70px] shadow-sm flex items-center px-6 justify-between">
      {/* SaaS Branding SaaS Branding SaaS Branding SaaS Branding */}
      <div className="flex items-center gap-8">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary shadow-lg shadow-primary/20">
            <Zap className="h-6 w-6 text-foreground fill-foreground" />
          </div>
          <div className="flex flex-col">
            <h1 className="text-lg font-bold text-foreground leading-none">Copilot Dashboard</h1>
            <span className="text-[10px] font-bold uppercase tracking-widest text-secondary mt-1">Operational Control</span>
          </div>
        </div>

        <div className="h-8 w-[1px] bg-secondary/10" />

        {/* Improved Step Navigation Improved Step Navigation Improved Step Navigation Improved Step Navigation */}
        <div className="flex items-center gap-1 bg-background p-1 rounded-xl">
          <button
            onClick={goBack}
            disabled={!canGoBack}
            className={cn(
              "flex h-8 w-8 items-center justify-center rounded-lg transition-all",
              canGoBack ? "text-foreground hover:bg-white hover:text-primary shadow-xs" : "text-secondary/30 cursor-not-allowed"
            )}
            title="Undo Last Step"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <button
            onClick={goForward}
            disabled={!canGoForward}
            className={cn(
              "flex h-8 w-8 items-center justify-center rounded-lg transition-all",
              canGoForward ? "text-foreground hover:bg-white hover:text-primary shadow-xs" : "text-secondary/30 cursor-not-allowed"
            )}
            title="Redo Next Step"
          >
            <ChevronRight className="h-5 w-5" />
          </button>
        </div>
      </div>

      {/* Modern Dashboard Tools Modern Dashboard Tools Modern Dashboard Tools Modern Dashboard Tools */}
      <div className="flex items-center gap-4 flex-1 max-w-lg px-10">
        <div className="relative w-full group">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-secondary/40 group-focus-within:text-secondary transition-colors" />
          <input 
            placeholder="Search operational logs..." 
            className="w-full bg-background border border-secondary/5 rounded-xl py-2 pl-10 pr-4 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary focus:bg-white transition-all"
          />
        </div>
      </div>

      {/* User Actions & Profile User Actions & Profile User Actions & Profile User Actions & Profile */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <button 
            onClick={toggleHistory}
            className={cn(
              "flex h-10 w-10 items-center justify-center rounded-xl transition-all border",
              isHistoryOpen ? "bg-white border-primary text-primary" : "text-secondary hover:bg-background border-transparent"
            )}
          >
            <History className="h-5 w-5" />
          </button>
          <button className="flex h-10 w-10 items-center justify-center rounded-xl text-secondary hover:bg-background transition-all relative">
            <Bell className="h-5 w-5" />
            <span className="absolute top-2.5 right-2.5 h-2 w-2 bg-secondary rounded-full border-2 border-white" />
          </button>
          <button 
            onClick={toggleSettings}
            className={cn(
              "flex h-10 w-10 items-center justify-center rounded-xl transition-all border",
              isSettingsOpen ? "bg-white border-primary text-primary" : "text-secondary hover:bg-background border-transparent"
            )}
          >
            <Settings className="h-5 w-5" />
          </button>
        </div>

        <div className="h-6 w-[1px] bg-secondary/10" />

        <button className="flex items-center gap-3 pl-2 group">
          <div className="flex flex-col items-end">
            <span className="text-sm font-bold text-foreground group-hover:text-primary transition-colors">Vahin Singh</span>
            <span className="text-[10px] font-bold text-secondary uppercase tracking-tight">System Operator</span>
          </div>
          <div className="h-10 w-10 rounded-xl bg-background border border-secondary/10 flex items-center justify-center text-secondary group-hover:border-primary/30 group-hover:bg-primary/5 transition-all overflow-hidden relative">
            <User className="h-5 w-5" />
          </div>
        </button>
      </div>
    </header>
  );
}
