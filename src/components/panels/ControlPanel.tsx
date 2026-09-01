export default function ControlPanel() {
  return (
    <div className="flex h-full flex-col p-4">
      <div className="mb-4 text-sm font-semibold text-muted-foreground uppercase tracking-wider">
        Execution & Control
      </div>
      <div className="flex-1 space-y-4">
        {/* Placeholder for control logic */}
        <div className="rounded-lg border border-border bg-card/30 p-4 backdrop-blur shadow-sm">
          <h3 className="font-medium text-white/80">Status</h3>
          <p className="mt-1 text-xs text-muted-foreground">No active execution</p>
        </div>
        
        <div className="rounded-lg border border-border bg-card/30 p-4 backdrop-blur shadow-sm">
          <h3 className="font-medium text-white/80">Pending Actions</h3>
          <p className="mt-2 text-xs text-muted-foreground italic">
            Waiting for plan approval...
          </p>
          <button className="mt-4 w-full rounded-md bg-primary/20 text-primary py-2 text-sm font-medium hover:bg-primary/30 transition-colors disabled:opacity-50" disabled>
            Execute Plan
          </button>
        </div>
      </div>
    </div>
  );
}
