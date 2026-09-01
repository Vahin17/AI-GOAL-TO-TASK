import { create } from 'zustand';

interface WorkspaceState {
  activeGoal: string | null;
  sessionStatus: 'idle' | 'planning' | 'waiting_approval' | 'executing' | 'completed';
  activePanel: 'chat' | 'graph' | 'control'; // Useful for mobile/responsive views
  
  setActiveGoal: (goal: string) => void;
  setSessionStatus: (status: WorkspaceState['sessionStatus']) => void;
  setActivePanel: (panel: WorkspaceState['activePanel']) => void;
  resetSession: () => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  activeGoal: null,
  sessionStatus: 'idle',
  activePanel: 'chat', // Default view for mobile

  setActiveGoal: (goal) => set({ activeGoal: goal }),
  setSessionStatus: (status) => set({ sessionStatus: status }),
  setActivePanel: (panel) => set({ activePanel: panel }),
  
  resetSession: () => set({
    activeGoal: null,
    sessionStatus: 'idle',
    activePanel: 'chat',
  }),
}));
