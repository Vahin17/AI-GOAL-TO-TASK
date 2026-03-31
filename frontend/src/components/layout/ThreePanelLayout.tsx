import ChatPanel from '../panels/ChatPanel';
import GraphPanel from '../panels/GraphPanel';
import ControlPanel from '../panels/ControlPanel';

export default function ThreePanelLayout() {
  return (
    <main className="flex h-screen w-full bg-background overflow-hidden text-foreground selection:bg-primary/30">
      {/* 
        Grid Setup: 
        Left panel: 1fr (approx 25%) 
        Center panel: 2fr (approx 50%) 
        Right panel: 1fr (approx 25%) 
      */}
      <div className="grid h-full w-full grid-cols-1 md:grid-cols-4 lg:grid-cols-4">
        {/* Chat / Setup Panel */}
        <section className="col-span-1 hidden h-full flex-col bg-card/30 md:flex">
          <ChatPanel />
        </section>

        {/* Interactive Graph Panel */}
        <section className="col-span-1 h-full md:col-span-2 relative">
          <GraphPanel />
        </section>

        {/* Execution Control Panel */}
        <section className="col-span-1 hidden h-full bg-card/30 md:flex">
          <ControlPanel />
        </section>
      </div>
    </main>
  );
}
