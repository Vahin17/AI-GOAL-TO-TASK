'use client';

import React from 'react';
import TopNavbar from './TopNavbar';
import ChatPanel from '../panels/ChatPanel';
import WorkflowCanvas from '../panels/WorkflowCanvas';
import ExecutionControl from '../panels/ExecutionControl';

export default function ThreePanelLayout() {
  return (
    <div className="flex h-screen w-full flex-col bg-[#0c0e12] text-white selection:bg-[#00CCFF]/30 overflow-hidden font-sans">
      {/* Premium Top Navbar */}
      <TopNavbar />

      {/* Main Content Workspace */}
      <main className="flex flex-1 w-full overflow-hidden">
        {/* Left: Chat Copilot (30%) */}
        <section className="w-[30%] h-full flex flex-col shrink-0">
          <ChatPanel />
        </section>

        {/* Center: Workflow Hero (48%) */}
        <section className="flex-1 h-full relative overflow-hidden bg-black/20">
          <WorkflowCanvas />
        </section>

        {/* Right: Execution Control (22%) */}
        <section className="w-[22%] h-full flex flex-col shrink-0">
          <ExecutionControl />
        </section>
      </main>
    </div>
  );
}
