export default function ChatPanel() {
  return (
    <div className="flex h-full flex-col p-4">
      <div className="mb-4 text-sm font-semibold text-muted-foreground uppercase tracking-wider">
        AI Copilot
      </div>
      <div className="flex-1 overflow-y-auto rounded-lg border border-border/50 bg-black/20 p-4 shadow-inner">
        <div className="mb-2 text-sm text-muted-foreground">
          What do you want to build or setup today?
        </div>
        {/* Chat messages placeholder */}
      </div>
      <div className="mt-4">
        <input 
          type="text" 
          placeholder="Start your coding session..." 
          className="w-full rounded-md border border-border bg-background/50 px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all"
        />
      </div>
    </div>
  );
}
