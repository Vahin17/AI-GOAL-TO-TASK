'use client';

import React, { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useAuthStore } from '@/store/useAuthStore';
import { Cpu } from 'lucide-react';

interface AuthGuardProps {
  children: React.ReactNode;
}

export default function AuthGuard({ children }: AuthGuardProps) {
  const router = useRouter();
  const pathname = usePathname();
  const { isAuthenticated, isLoading, checkAuth } = useAuthStore();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      const nextUrl = pathname ? `/login?next=${encodeURIComponent(pathname)}` : '/login';
      router.replace(nextUrl);
    }
  }, [isLoading, isAuthenticated, router, pathname]);

  if (isLoading) {
    return (
      <div className="min-h-screen w-full flex flex-col items-center justify-center bg-slate-950 text-white font-sans">
        <div className="flex flex-col items-center gap-4 animate-pulse">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-tr from-emerald-600 via-teal-600 to-cyan-600 flex items-center justify-center shadow-2xl shadow-emerald-500/30">
            <Cpu className="w-8 h-8 text-white animate-spin" style={{ animationDuration: '8s' }} />
          </div>
          <div className="flex flex-col items-center gap-1">
            <span className="text-xl font-bold tracking-tight text-white">ARDO Engine</span>
            <span className="text-xs text-emerald-400 font-mono tracking-widest uppercase">
              Verifying Security Session...
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return null;
  }

  return <>{children}</>;
}

