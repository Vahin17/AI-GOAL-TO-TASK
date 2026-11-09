'use client';

import React, { useState, useRef, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { 
  ChevronLeft, 
  ChevronRight, 
  History, 
  Settings, 
  User, 
  Zap,
  Search,
  Bell,
  Home,
  LogOut,
  ChevronDown,
  ShieldCheck
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { useAuthStore } from '@/store/useAuthStore';
import { cn } from '@/lib/utils';

export default function TopNavbar() {
  const router = useRouter();
  const { user, logout } = useAuthStore();
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(event.target as Node)) {
        setProfileOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

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
      {/* SaaS Branding */}
      <div className="flex items-center gap-6">
        <Link href="/" className="flex items-center gap-3 group" title="Return to ARDO Landing Page">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary shadow-lg shadow-primary/20 group-hover:scale-105 transition-transform">
            <Zap className="h-6 w-6 text-foreground fill-foreground" />
          </div>
          <div className="flex flex-col">
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold text-foreground leading-none">Copilot Dashboard</h1>
              <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-secondary hover:text-foreground bg-secondary/10 hover:bg-secondary/20 px-2 py-0.5 rounded-md transition-colors">
                <Home className="h-3 w-3" />
                Landing
              </span>
            </div>
            <span className="text-[10px] font-bold uppercase tracking-widest text-secondary mt-1">Operational Control</span>
          </div>
        </Link>

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

        {/* User Profile & Dropdown Menu */}
        <div className="relative" ref={profileRef}>
          <button 
            onClick={() => setProfileOpen(!profileOpen)}
            className="flex items-center gap-3 pl-2 group cursor-pointer"
            title="User Profile & Settings"
          >
            <div className="flex flex-col items-end">
              <span className="text-sm font-bold text-foreground group-hover:text-primary transition-colors">
                {user?.full_name || 'System Operator'}
              </span>
              <span className="text-[10px] font-bold text-secondary uppercase tracking-tight">
                {user?.email ? user.email.split('@')[0] : 'Operator'}
              </span>
            </div>
            <div className="h-10 w-10 rounded-xl bg-background border border-secondary/10 flex items-center justify-center text-secondary group-hover:border-primary/30 group-hover:bg-primary/5 transition-all overflow-hidden relative">
              <User className="h-5 w-5" />
            </div>
          </button>

          {profileOpen && (
            <div className="absolute right-0 mt-2 w-64 rounded-2xl bg-white border border-slate-200 shadow-xl p-3 z-50 animate-in fade-in zoom-in-95 duration-150">
              <div className="p-2 border-b border-slate-100 mb-2">
                <span className="text-sm font-bold text-slate-900 block truncate">
                  {user?.full_name || 'System Operator'}
                </span>
                <span className="text-xs text-slate-500 block truncate font-mono">
                  {user?.email || 'authenticated@ardo.ai'}
                </span>
                <div className="inline-flex items-center gap-1 mt-1.5 px-2 py-0.5 rounded-full bg-emerald-50 text-[10px] font-semibold text-emerald-700">
                  <ShieldCheck className="w-3 h-3 text-emerald-600" />
                  <span>Session Active (Argon2id)</span>
                </div>
              </div>

              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs font-semibold text-rose-600 hover:bg-rose-50 transition-colors cursor-pointer text-left"
              >
                <LogOut className="w-4 h-4 text-rose-500" />
                <span>Sign Out</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
