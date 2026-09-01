import { create } from 'zustand';
import { Node, Edge } from '@xyflow/react';

export type TaskStatus = 'pending' | 'active' | 'completed' | 'failed';

export type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
};

export type ExecutionStatus = 'idle' | 'planning' | 'executing' | 'completed' | 'failed';

export type HistoryStep = {
  nodes: Node[];
  edges: Edge[];
  status: ExecutionStatus;
  reasoning: string;
  logs: string[];
};

interface AppState {
  // Chat State
  messages: Message[];
  isTyping: boolean;
  
  // History / Navigation State
  historySteps: HistoryStep[];
  currentStepIndex: number;
  
  // Current Visual State
  nodes: Node[];
  edges: Edge[];
  status: ExecutionStatus;
  reasoning: string;
  logs: string[];

  // UI States (Modals/Drawers)
  isHistoryOpen: boolean;
  isSettingsOpen: boolean;

  // Actions
  addMessage: (message: Omit<Message, 'id' | 'timestamp'>) => void;
  setTyping: (isTyping: boolean) => void;
  updateWorkflow: (nodes: Node[], edges: Edge[], reasoning: string, status: ExecutionStatus) => void;
  addLog: (log: string) => void;
  
  // Navigation Actions
  goBack: () => void;
  goForward: () => void;
  
  // Execution Actions
  approvePlan: () => void;
  resetSession: () => void;
  
  // UI Actions
  toggleHistory: () => void;
  toggleSettings: () => void;
}

export const useAppStore = create<AppState>((set, get) => ({
  messages: [],
  isTyping: false,
  
  historySteps: [{
    nodes: [],
    edges: [],
    status: 'idle',
    reasoning: 'Welcome to AI Copilot. Enter a goal to begin.',
    logs: ['System initialized.']
  }],
  currentStepIndex: 0,
  
  nodes: [],
  edges: [],
  status: 'idle',
  reasoning: 'Welcome to AI Copilot. Enter a goal to begin.',
  logs: ['System initialized.'],

  isHistoryOpen: false,
  isSettingsOpen: false,

  addMessage: (msg) => set((state) => ({
    messages: [...state.messages, { ...msg, id: Math.random().toString(36).substr(2, 9), timestamp: Date.now() }]
  })),

  setTyping: (isTyping) => set({ isTyping }),

  updateWorkflow: (nodes, edges, reasoning, status) => set((state) => {
    const newStep: HistoryStep = {
      nodes,
      edges,
      status,
      reasoning,
      logs: state.logs
    };
    const newHistory = state.historySteps.slice(0, state.currentStepIndex + 1);
    newHistory.push(newStep);
    
    return {
      nodes,
      edges,
      reasoning,
      status,
      historySteps: newHistory,
      currentStepIndex: newHistory.length - 1
    };
  }),

  addLog: (log) => set((state) => ({
    logs: [...state.logs, `[${new Date().toLocaleTimeString()}] ${log}`]
  })),

  goBack: () => set((state) => {
    if (state.currentStepIndex > 0) {
      const prevStep = state.historySteps[state.currentStepIndex - 1];
      return {
        currentStepIndex: state.currentStepIndex - 1,
        nodes: prevStep.nodes,
        edges: prevStep.edges,
        status: prevStep.status,
        reasoning: prevStep.reasoning,
        logs: prevStep.logs
      };
    }
    return state;
  }),

  goForward: () => set((state) => {
    if (state.currentStepIndex < state.historySteps.length - 1) {
      const nextStep = state.historySteps[state.currentStepIndex + 1];
      return {
        currentStepIndex: state.currentStepIndex + 1,
        nodes: nextStep.nodes,
        edges: nextStep.edges,
        status: nextStep.status,
        reasoning: nextStep.reasoning,
        logs: nextStep.logs
      };
    }
    return state;
  }),

  approvePlan: () => {
    const { addLog } = get();
    addLog('Operator approved plan. Waiting for orchestrator...');
  },

  resetSession: () => set({
    messages: [],
    isTyping: false,
    historySteps: [{
      nodes: [],
      edges: [],
      status: 'idle',
      reasoning: 'Welcome to AI Copilot. Enter a goal to begin.',
      logs: ['System initialized.']
    }],
    currentStepIndex: 0,
    nodes: [],
    edges: [],
    status: 'idle',
    reasoning: 'Welcome to AI Copilot. Enter a goal to begin.',
    logs: ['System reset. Session cleared.']
  }),

  toggleHistory: () => set((state) => ({ isHistoryOpen: !state.isHistoryOpen })),
  toggleSettings: () => set((state) => ({ isSettingsOpen: !state.isSettingsOpen })),
}));
