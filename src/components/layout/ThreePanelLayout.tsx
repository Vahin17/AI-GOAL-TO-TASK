'use client';

import React from 'react';
import TopNavbar from './TopNavbar';
import ChatPanel from '../panels/ChatPanel';
import WorkflowCanvas from '../panels/WorkflowCanvas';
import ExecutionControl from '../panels/ExecutionControl';

export default function ThreePanelLayout() {
  return (
    <div className="flex h-screen w-full flex-col bg-background text-foreground selection:bg-primary/30 overflow-hidden font-sans p-4 gap-4">
      {/* SaaS Navbar Section */}
      <TopNavbar />

      {/* Main Content Workspace Layout */}
      <main className="flex flex-1 w-full gap-5 overflow-hidden">
        {/* Left: Chat Copilot (30%) */}
        <section className="w-[30%] h-full flex flex-col shrink-0 bg-white rounded-2xl shadow-sm border border-secondary/10 overflow-hidden">
          <ChatPanel />
        </section>

        {/* Center: Workflow Hero (48%) */}
        <section className="flex-1 h-full relative overflow-hidden bg-white rounded-2xl shadow-sm border border-secondary/10">
          <WorkflowCanvas />
        </section>

        {/* Right: Execution Control (22%) */}
        <section className="w-[22%] h-full flex flex-col shrink-0 bg-white rounded-2xl shadow-sm border border-secondary/10 overflow-hidden">
          <ExecutionControl />
        </section>
      </main>
    </div>
  );
}
