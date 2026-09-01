/**
 * useARDOStore.ts — ARDO Orchestration State Store
 * ==================================================
 * Central Zustand store for all ARDO runtime state.
 * Manages graph state, execution queue, events, metrics, and WebSocket lifecycle.
 *
 * Design:
 *   - All state mutations are atomic (Zustand immer-style set())
 *   - applyEvent() handles all 9 event types without triggering full re-renders
 *   - WebSocket auto-manages reconnection via ARDOWebSocket class
 *   - No flickering: nodes update in-place using Map keyed by task_id
 */

import { create } from 'zustand';
import {
  ARDONodeState,
  ARDOSessionResponse,
  ARDOWebSocket,
  OrchestrationEvent,
  RescheduleEvent,
  SessionMetrics,
  StabilityEvent,
  TimelineMarker,
  WeightSuggestion,
  WhyNowContext,
  ardoApi,
  ARDOStartRequest,
  HistoryItem,
  BrowserEvidenceItem,
  KnowledgeStats,
} from '@/lib/ardo-api';

// ── Node status type ───────────────────────────────────────────────────────

export type ARDONodeStatus =
  | 'pending'
  | 'waiting'
  | 'active'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'approval_needed';

// ── Store State ────────────────────────────────────────────────────────────

interface ARDOState {
  // Session
  sessionId: string | null;
  currentGoal: string | null;
  mode: string;
  isSessionActive: boolean;
  wsStatus: 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error';

  // Graph
  nodeMap: Map<string, ARDONodeState>;
  executionQueue: string[];
  parallelGroups: string[][];

  // Active execution
  activeTaskId: string | null;
  activeBrowserAction: { taskId?: string; actionType?: string; selector?: string; url?: string } | null;
  retryingTaskIds: Set<string>;
  blockedNodeIds: Set<string>;
  approvalPendingIds: Set<string>;
  // Phase 5: Safety metadata per task (from backend SafetyDecision)
  safetyMetadataMap: Map<string, Record<string, unknown>>;
  deniedTaskIds: Set<string>;

  // Browser Evidence & History (Phase 11 & 12)
  browserEvidence: BrowserEvidenceItem[];
  executionHistory: HistoryItem[];
  knowledgeStats: KnowledgeStats | null;

  // Events
  executionEvents: OrchestrationEvent[];
  rescheduleLog: RescheduleEvent[];
  stabilityEvents: StabilityEvent[];

  // Explainability
  whyNow: WhyNowContext | null;

  // Metrics
  metrics: SessionMetrics | null;
  timelineMarkers: TimelineMarker[];
  weightSuggestion: WeightSuggestion | null;

  // Error / loading
  isLoading: boolean;
  error: string | null;

  // ── Actions ──────────────────────────────────────────────────────────

  startSession: (request: ARDOStartRequest) => Promise<void>;
  applyEvent: (event: OrchestrationEvent) => void;
  approveTask: (taskId: string) => Promise<void>;
  rejectTask: (taskId: string) => Promise<void>;
  resetOrchestration: () => void;
  refreshState: () => Promise<void>;
  refreshMetrics: () => Promise<void>;
  loadHistory: () => Promise<void>;
  loadEvidence: (sessionId?: string) => Promise<void>;
  loadKnowledgeStats: () => Promise<void>;
}

// ── Default metrics ────────────────────────────────────────────────────────

const DEFAULT_METRICS: SessionMetrics = {
  session_id: '',
  elapsed_seconds: 0,
  tasks_total: 0,
  tasks_completed: 0,
  tasks_failed: 0,
  tasks_blocked: 0,
  tasks_retried: 0,
  total_retry_count: 0,
  reschedule_count: 0,
  approval_count: 0,
  throughput_per_minute: 0,
  avg_latency_seconds: 0,
  success_rate: 0,
  timeline_markers: [],
};

// ── WebSocket instance (module-level, not in store) ────────────────────────
let _wsClient: ARDOWebSocket | null = null;

// ── Store ──────────────────────────────────────────────────────────────────

export const useARDOStore = create<ARDOState>((set, get) => ({
  // Initial state
  sessionId: null,
  currentGoal: null,
  mode: 'safe',
  isSessionActive: false,
  wsStatus: 'idle',

  nodeMap: new Map(),
  executionQueue: [],
  parallelGroups: [],

  activeTaskId: null,
  activeBrowserAction: null,
  retryingTaskIds: new Set(),
  blockedNodeIds: new Set(),
  approvalPendingIds: new Set<string>(),
  safetyMetadataMap: new Map<string, Record<string, unknown>>(),
  deniedTaskIds: new Set<string>(),

  browserEvidence: [],
  executionHistory: [],
  knowledgeStats: null,

  executionEvents: [],
  rescheduleLog: [],
  stabilityEvents: [],

  whyNow: null,
  metrics: null,
  timelineMarkers: [],
  weightSuggestion: null,

  isLoading: false,
  error: null,

  // ── startSession ─────────────────────────────────────────────────────

  startSession: async (request) => {
    set({ isLoading: true, error: null, wsStatus: 'connecting', currentGoal: request.goal, browserEvidence: [] });
    try {
      const response: ARDOSessionResponse = await ardoApi.startOrchestration(request);

      // Build node map from initial graph
      const nodeMap = new Map<string, ARDONodeState>();
      for (const node of response.initial_graph) {
        nodeMap.set(node.task_id, node);
      }

      set({
        sessionId: response.session_id,
        mode: response.mode,
        isSessionActive: true,
        nodeMap,
        executionQueue: response.initial_queue,
        parallelGroups: response.parallel_groups,
        metrics: { ...DEFAULT_METRICS, session_id: response.session_id, tasks_total: response.task_count },
        isLoading: false,
      });

      // Connect WebSocket
      _wsClient?.disconnect();
      _wsClient = new ARDOWebSocket({
        sessionId: response.session_id,
        onEvent: (event) => get().applyEvent(event),
        onConnect: () => set({ wsStatus: 'connected' }),
        onDisconnect: () => set({ wsStatus: 'disconnected' }),
        onError: () => set({ wsStatus: 'error' }),
      });
      _wsClient.connect();

    } catch (err) {
      set({ isLoading: false, error: String(err), wsStatus: 'error' });
    }
  },

  // ── applyEvent ───────────────────────────────────────────────────────

  applyEvent: (event) => {
    set((state) => {
      // Append to event log (cap at 200)
      const executionEvents = [...state.executionEvents, event].slice(-200);

      // Update node map in-place
      const nodeMap = new Map(state.nodeMap);
      let activeTaskId = state.activeTaskId;
      const retryingTaskIds = new Set(state.retryingTaskIds);
      const blockedNodeIds = new Set(state.blockedNodeIds);
      const approvalPendingIds = new Set(state.approvalPendingIds);
      let whyNow = state.whyNow;
      let metrics = state.metrics ?? { ...DEFAULT_METRICS };
      const timelineMarkers = [...state.timelineMarkers];
      let rescheduleLog = state.rescheduleLog;
      const stabilityEvents = state.stabilityEvents;
      const weightSuggestion = state.weightSuggestion;
      let executionQueue = state.executionQueue;
      let isSessionActive = state.isSessionActive;

      const taskId = event.task_id;
      const node = taskId ? nodeMap.get(taskId) : undefined;

      switch (event.event_type) {
        case 'task_started':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'active' });
          }
          activeTaskId = taskId ?? null;
          retryingTaskIds.delete(taskId ?? '');
          break;

        case 'task_completed':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'completed' });
          }
          if (activeTaskId === taskId) activeTaskId = null;
          metrics = {
            ...metrics,
            tasks_completed: (metrics.tasks_completed ?? 0) + 1,
          };
          timelineMarkers.push({
            type: 'complete',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
            latency: (event.details?.latency_seconds as number) ?? 0,
          });
          break;

        case 'task_failed':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'failed' });
          }
          if (activeTaskId === taskId) activeTaskId = null;
          metrics = {
            ...metrics,
            tasks_failed: (metrics.tasks_failed ?? 0) + 1,
          };
          timelineMarkers.push({
            type: 'fail',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
          });
          break;

        case 'task_retrying':
          if (node) {
            nodeMap.set(taskId!, {
              ...node,
              status: 'retrying',
              retry_count: (event.details?.retry_count as number) ?? node.retry_count + 1,
            });
          }
          retryingTaskIds.add(taskId ?? '');
          metrics = {
            ...metrics,
            total_retry_count: (metrics.total_retry_count ?? 0) + 1,
          };
          timelineMarkers.push({
            type: 'retry',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
            attempt: (event.details?.retry_count as number) ?? 0,
            delay: (event.details?.delay_seconds as number) ?? 0,
          });
          break;

        case 'task_blocked':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'blocked' });
          }
          blockedNodeIds.add(taskId ?? '');
          metrics = {
            ...metrics,
            tasks_blocked: (metrics.tasks_blocked ?? 0) + 1,
          };
          break;

        case 'task_approval_needed':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'approval_needed' });
          }
          approvalPendingIds.add(taskId ?? '');
          // Phase 5: Capture safety metadata for the approval panel
          if (taskId && event.details) {
            const safetyMeta = event.details.safety_metadata as Record<string, unknown> | undefined;
            if (safetyMeta) {
              const updated = new Map(get().safetyMetadataMap);
              updated.set(taskId, safetyMeta);
              set({ safetyMetadataMap: updated });
            }
          }
          metrics = {
            ...metrics,
            approval_count: (metrics.approval_count ?? 0) + 1,
          };
          break;

        case 'task_approved':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'active' });
          }
          approvalPendingIds.delete(taskId ?? '');
          timelineMarkers.push({
            type: 'approve',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
          });
          break;

        case 'dependents_waiting': {
          // Update explanation / why-now
          const explanation = event.details?.explanation as WhyNowContext | undefined;
          if (explanation) whyNow = explanation;
          // Update waiting tasks
          const waitingIds = (event.details?.newly_waiting as string[]) ?? [];
          for (const wid of waitingIds) {
            const wnode = nodeMap.get(wid);
            if (wnode) nodeMap.set(wid, { ...wnode, status: 'waiting' });
          }
          break;
        }

        case 'queue_reordered': {
          const newQueue = (event.details?.new_queue_preview as string[]) ?? [];
          if (newQueue.length > 0) executionQueue = newQueue;
          rescheduleLog = [
            ...rescheduleLog,
            {
              trigger_task_id: taskId ?? '',
              trigger_event: 'reordered',
              previous_queue: executionQueue,
              new_queue: newQueue,
              reason: (event.details?.reason as string) ?? '',
              timestamp: event.timestamp,
            },
          ].slice(-50);
          metrics = {
            ...metrics,
            reschedule_count: (metrics.reschedule_count ?? 0) + 1,
          };
          timelineMarkers.push({
            type: 'reschedule',
            task_id: taskId ?? '',
            task_name: '',
            timestamp: event.timestamp,
            reason: (event.details?.reason as string) ?? '',
          });
          break;
        }

        case 'session_complete':
          isSessionActive = false;
          activeTaskId = null;
          if (event.details) {
            metrics = { ...metrics, ...(event.details as Partial<SessionMetrics>) };
          }
          break;
      }

      return {
        executionEvents,
        nodeMap,
        activeTaskId,
        retryingTaskIds,
        blockedNodeIds,
        approvalPendingIds,
        whyNow,
        metrics,
        timelineMarkers: timelineMarkers.slice(-100),
        rescheduleLog,
        stabilityEvents,
        weightSuggestion,
        executionQueue,
        isSessionActive,
      };
    });
  },

  // ── approveTask ───────────────────────────────────────────────────────

  approveTask: async (taskId) => {
    const { sessionId } = get();
    if (!sessionId) return;
    try {
      await ardoApi.approveTask(sessionId, taskId, true);
      // The WS will emit task_approved event which updates the store
    } catch (err) {
      set({ error: String(err) });
    }
  },

  rejectTask: async (taskId) => {
    const { sessionId } = get();
    if (!sessionId) return;
    try {
      await ardoApi.approveTask(sessionId, taskId, false);
    } catch (err) {
      set({ error: String(err) });
    }
  },

  // ── refreshState ──────────────────────────────────────────────────────

  refreshState: async () => {
    const { sessionId } = get();
    if (!sessionId) return;
    try {
      const state = await ardoApi.getState(sessionId);
      const nodeMap = new Map<string, ARDONodeState>();
      for (const node of state.graph_state as ARDONodeState[]) {
        nodeMap.set(node.task_id, node);
      }
      set({
        nodeMap,
        executionQueue: state.execution_queue,
        parallelGroups: state.parallel_groups,
        whyNow: state.why_now ?? null,
        rescheduleLog: state.reschedule_log,
        stabilityEvents: state.stability_events,
        weightSuggestion: state.weight_suggestion ?? null,
        metrics: state.metrics as SessionMetrics,
      });
    } catch {
      // Silent fail — WS events keep state fresh
    }
  },

  refreshMetrics: async () => {
    const { sessionId } = get();
    if (!sessionId) return;
    try {
      const m = await ardoApi.getMetrics(sessionId);
      set({ metrics: m, timelineMarkers: m.timeline_markers ?? [] });
    } catch {
      // Silent fail
    }
  },

  // ── History, Evidence & Knowledge (Phase 11/12) ─────────────────────────

  loadHistory: async () => {
    try {
      const res = await ardoApi.getHistory();
      if (res && res.history) {
        set({ executionHistory: res.history });
      }
    } catch {
      // Graceful fallback
    }
  },

  loadEvidence: async (targetSessionId) => {
    const sid = targetSessionId || get().sessionId;
    if (!sid) return;
    try {
      const res = await ardoApi.getEvidence(sid);
      if (res && res.evidence_items) {
        set({ browserEvidence: res.evidence_items });
      }
    } catch {
      // Graceful fallback
    }
  },

  loadKnowledgeStats: async () => {
    try {
      const stats = await ardoApi.getKnowledgeStats();
      if (stats) {
        set({ knowledgeStats: stats });
      }
    } catch {
      // Graceful fallback
    }
  },

  // ── resetOrchestration ────────────────────────────────────────────────

  resetOrchestration: () => {
    _wsClient?.disconnect();
    _wsClient = null;
    set({
      sessionId: null,
      currentGoal: null,
      mode: 'safe',
      isSessionActive: false,
      wsStatus: 'idle',
      nodeMap: new Map(),
      executionQueue: [],
      parallelGroups: [],
      activeTaskId: null,
      activeBrowserAction: null,
      retryingTaskIds: new Set(),
      blockedNodeIds: new Set(),
      approvalPendingIds: new Set(),
      safetyMetadataMap: new Map(),
      deniedTaskIds: new Set(),
      browserEvidence: [],
      executionEvents: [],
      rescheduleLog: [],
      stabilityEvents: [],
      whyNow: null,
      metrics: null,
      timelineMarkers: [],
      weightSuggestion: null,
      isLoading: false,
      error: null,
    });
  },
}));
