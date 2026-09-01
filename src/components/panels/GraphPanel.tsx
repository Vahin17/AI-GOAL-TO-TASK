'use client';

import { useARDOStore } from '@/store/useARDOStore';
import WorkflowCanvas from './WorkflowCanvas';

export default function GraphPanel() {
  const { isSessionActive } = useARDOStore();

  return (
    <div className="relative flex h-full w-full items-center justify-center overflow-hidden border-x border-border/30">
      {isSessionActive ? (
         <WorkflowCanvas />
      ) : (
        <div className="relative flex h-full w-full items-center justify-center bg-black/40 p-4 shadow-inset">
          <div className="absolute top-4 left-4 z-10">
            <h2 className="text-xl font-bold tracking-tight text-white/90">Task Workflow</h2>
            <p className="text-sm text-white/50">Plan and logic visualization</p>
          </div>
          
          <div className="flex flex-col items-center gap-6">
            <div className="rounded-xl border border-primary/30 bg-primary/10 px-6 py-4 shadow-[0_0_30px_-5px_var(--color-primary)] backdrop-blur-md">
              <div className="font-medium text-primary">Goal Understanding</div>
            </div>
            <div className="h-8 w-[2px] bg-gradient-to-b from-primary/50 to-muted/20" />
            <div className="rounded-xl border border-border bg-card/50 px-6 py-4 backdrop-blur-md">
              <div className="text-muted-foreground">Waiting for goal...</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
