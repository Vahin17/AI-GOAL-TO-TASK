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
  GoalVerificationResponse,
  KnowledgeStats,
} from '@/lib/ardo-api';

// ── Node status type ───────────────────────────────────────────────────────

export type ARDONodeStatus =
  | 'pending'
  | 'waiting'
  | 'ready'
  | 'active'
  | 'retrying'
  | 'completed'
  | 'verified'
  | 'failed'
  | 'blocked'
  | 'approval_needed'
  | 'paused'
  | 'cancelled';

// ── Store State ────────────────────────────────────────────────────────────

interface ARDOState {
  // Session
  sessionId: string | null;
  currentGoal: string | null;
  mode: string;
  isSessionActive: boolean;
  wsStatus: 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error';
  planHash: string | null;
  isFallbackPlan: boolean;
  fallbackReason: string | null;
  planningMode: 'primary_ai' | 'deterministic_fallback';
  primaryFailureCode: string | null;
  planningLimitations: string[];

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
  approvedTaskIds: Set<string>;
  // Phase 5: Safety metadata per task (from backend SafetyDecision)
  safetyMetadataMap: Map<string, Record<string, unknown>>;
  deniedTaskIds: Set<string>;

  // Browser Evidence & History (Phase 11 & 12 & Phase 3)
  browserEvidence: BrowserEvidenceItem[];
  goalVerification: GoalVerificationResponse | null;
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
  approveTask: (taskId: string, planHash?: string, notes?: string) => Promise<void>;
  rejectTask: (taskId: string) => Promise<void>;
  cancelSession: () => Promise<void>;
  resetOrchestration: () => void;
  refreshState: () => Promise<void>;
  refreshMetrics: () => Promise<void>;
  loadHistory: () => Promise<void>;
  loadEvidence: (sessionId?: string) => Promise<void>;
  loadGoalVerification: (sessionId?: string) => Promise<void>;
  loadKnowledgeStats: () => Promise<void>;
}

// ── Default metrics ────────────────────────────────────────────────────────

const DEFAULT_METRICS: SessionMetrics = {
  session_id: '',
  elapsed_seconds: 0,
  tasks_total: 0,
  tasks_pending: 0,
  tasks_ready: 0,
  tasks_running: 0,
  tasks_approval_required: 0,
  tasks_paused: 0,
  tasks_completed: 0,
  tasks_verified: 0,
  tasks_failed: 0,
  tasks_blocked: 0,
  tasks_cancelled: 0,
  tasks_retried: 0,
  total_retry_count: 0,
  recovery_count: 0,
  reschedule_count: 0,
  approval_count: 0,
  throughput_per_minute: 0,
  avg_latency_seconds: 0,
  success_rate: 0,
  execution_progress_pct: 0,
  verification_progress_pct: 0,
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
  planHash: null,
  isFallbackPlan: false,
  fallbackReason: null,
  planningMode: 'primary_ai',
  primaryFailureCode: null,
  planningLimitations: [],

  nodeMap: new Map(),
  executionQueue: [],
  parallelGroups: [],

  activeTaskId: null,
  activeBrowserAction: null,
  retryingTaskIds: new Set(),
  blockedNodeIds: new Set(),
  approvalPendingIds: new Set<string>(),
  approvedTaskIds: new Set<string>(),
  safetyMetadataMap: new Map<string, Record<string, unknown>>(),
  deniedTaskIds: new Set<string>(),

  browserEvidence: [],
  goalVerification: null,
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
    const { isSessionActive, isLoading } = get();
    if (isSessionActive || isLoading) {
      console.warn('[RUN-GUARD] startSession rejected: session already active or loading');
      return;
    }
    console.log('[RUN-GUARD] startSession called', request);
    set({
      isLoading: true,
      error: null,
      wsStatus: 'connecting',
      currentGoal: request.goal,
      browserEvidence: [],
      goalVerification: null,
      isFallbackPlan: false,
      fallbackReason: null,
      planningMode: 'primary_ai',
      primaryFailureCode: null,
      planningLimitations: [],
      executionEvents: [],
      timelineMarkers: [],
      rescheduleLog: [],
      stabilityEvents: [],
    });
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
        planHash: response.plan_hash ?? null,
        isFallbackPlan: Boolean(response.is_fallback),
        fallbackReason: response.fallback_reason ?? null,
        planningMode: response.planning_mode ?? (response.is_fallback ? 'deterministic_fallback' : 'primary_ai'),
        primaryFailureCode: response.primary_failure_code ?? null,
        planningLimitations: response.limitations ?? [],
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
        onConnect: () => {
          set({ wsStatus: 'connected' });
          get().refreshState();
        },
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
      const approvedTaskIds = new Set(state.approvedTaskIds);
      const deniedTaskIds = new Set(state.deniedTaskIds);
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

      const isTerminalStatus = (st: string) =>
        ['completed', 'verified', 'failed', 'blocked', 'cancelled'].includes(st);

      switch (event.event_type) {
        case 'task_started':
          if (node && !isTerminalStatus(node.status)) {
            nodeMap.set(taskId!, { ...node, status: 'active' });
          }
          activeTaskId = taskId ?? null;
          retryingTaskIds.delete(taskId ?? '');
          break;

        case 'task_completed': {
          const isVer = Boolean(
            event.status === 'verified' ||
            event.details?.is_verified === true ||
            event.details?.verification_status === 'verified'
          );
          const newStatus: ARDONodeStatus = isVer ? 'verified' : 'completed';
          const artHash = (event.details?.artifact_hash as string) || node?.artifact_hash;
          if (node) {
            nodeMap.set(taskId!, {
              ...node,
              status: newStatus,
              is_verified: isVer,
              verification_status: isVer ? 'verified' : 'unverified',
              artifact_hash: artHash,
            });
          }
          if (activeTaskId === taskId) activeTaskId = null;
          timelineMarkers.push({
            type: isVer ? 'verified' : 'complete',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
            latency: (event.details?.latency_seconds as number) ?? 0,
          });
          break;
        }

        case 'task_verified':
        case 'verification_completed': {
          const isVer = (event.status || '').toLowerCase() === 'verified' || event.details?.is_verified === true;
          if (node && isVer) {
            const artHash = (event.details?.artifact_hash as string) || node.artifact_hash;
            nodeMap.set(taskId!, {
              ...node,
              status: 'verified',
              is_verified: true,
              verification_status: 'verified',
              artifact_hash: artHash,
            });
          }
          break;
        }

        case 'task_failed': {
          const reason = (event.details?.reason as string) || (event.details?.error as string) || node?.failure_reason || undefined;
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'failed', failure_reason: reason });
          }
          if (activeTaskId === taskId) activeTaskId = null;
          timelineMarkers.push({
            type: 'fail',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
            reason: reason || undefined,
          });
          break;
        }

        case 'task_retrying':
          if (node && !isTerminalStatus(node.status)) {
            nodeMap.set(taskId!, {
              ...node,
              status: 'retrying',
              retry_count: (event.details?.retry_count as number) ?? node.retry_count + 1,
            });
          }
          retryingTaskIds.add(taskId ?? '');
          timelineMarkers.push({
            type: 'retry',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
            attempt: (event.details?.retry_count as number) ?? 0,
            delay: (event.details?.delay_seconds as number) ?? 0,
          });
          break;

        case 'task_blocked': {
          const cause = (event.details?.cause as string) || node?.failure_reason;
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'blocked', failure_reason: cause });
          }
          blockedNodeIds.add(taskId ?? '');
          approvalPendingIds.delete(taskId ?? '');
          if (taskId) deniedTaskIds.add(taskId);
          break;
        }

        case 'task_paused':
          if (taskId && node && !isTerminalStatus(node.status)) {
            nodeMap.set(taskId, { ...node, status: 'paused' });
          }
          break;

        case 'task_cancelled': {
          const cancelReason = (event.details?.reason as string) || 'Cancelled';
          if (taskId && node) {
            nodeMap.set(taskId, { ...node, status: 'cancelled', failure_reason: cancelReason });
          }
          if (activeTaskId === taskId) activeTaskId = null;
          break;
        }

        case 'task_approval_needed':
          if (node && !isTerminalStatus(node.status)) {
            nodeMap.set(taskId!, { ...node, status: 'approval_needed' });
          }
          approvalPendingIds.add(taskId ?? '');
          if (taskId && event.details) {
            const safetyMeta = event.details.safety_metadata as Record<string, unknown> | undefined;
            if (safetyMeta) {
              const updated = new Map(get().safetyMetadataMap);
              updated.set(taskId, safetyMeta);
              set({ safetyMetadataMap: updated });
            }
          }
          break;

        case 'task_approved':
          if (node) {
            nodeMap.set(taskId!, { ...node, status: 'waiting' });
          }
          approvalPendingIds.delete(taskId ?? '');
          if (taskId) approvedTaskIds.add(taskId);
          timelineMarkers.push({
            type: 'approve',
            task_id: taskId!,
            task_name: event.task_name ?? '',
            timestamp: event.timestamp,
          });
          break;

        case 'dependents_waiting': {
          const explanation = event.details?.explanation as WhyNowContext | undefined;
          if (explanation) whyNow = explanation;
          const waitingIds = (event.details?.newly_waiting as string[]) ?? [];
          for (const wid of waitingIds) {
            const wnode = nodeMap.get(wid);
            if (wnode && !isTerminalStatus(wnode.status)) nodeMap.set(wid, { ...wnode, status: 'waiting' });
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
          _wsClient?.disconnect();
          setTimeout(() => {
            const sid = get().sessionId;
            if (sid) {
              get().loadEvidence(sid);
              get().loadGoalVerification(sid);
            }
          }, 50);
          break;
      }

      // Idempotently compute all metrics directly from nodeMap (guarantees zero drift and zero double-counting)
      const allNodes = Array.from(nodeMap.values());
      const totalPlanned = allNodes.length;
      const pendingCount = allNodes.filter(n => n.status === 'pending').length;
      const readyCount = allNodes.filter(n => n.status === 'waiting' || n.status === 'ready').length;
      const runningCount = allNodes.filter(n => n.status === 'active' || n.status === 'retrying').length;
      const approvalCount = allNodes.filter(n => n.status === 'approval_needed').length;
      const pausedCount = allNodes.filter(n => n.status === 'paused').length;
      const verifiedCount = allNodes.filter(n => n.status === 'verified' || n.verification_status === 'verified').length;
      const completedCount = allNodes.filter(n => n.status === 'completed' && n.verification_status !== 'verified').length;
      const failedCount = allNodes.filter(n => n.status === 'failed').length;
      const blockedCount = allNodes.filter(n => n.status === 'blocked').length;
      const cancelledCount = allNodes.filter(n => n.status === 'cancelled').length;

      const finishedCount = verifiedCount + completedCount + failedCount + blockedCount + cancelledCount;
      const execProgress = totalPlanned > 0 ? Math.min(100, Number(((finishedCount / totalPlanned) * 100).toFixed(1))) : 0;
      const verifProgress = totalPlanned > 0 ? Math.min(100, Number(((verifiedCount / totalPlanned) * 100).toFixed(1))) : 0;
      const successRatio = totalPlanned > 0 ? (
        (failedCount + blockedCount + cancelledCount) > 0
          ? Number(((verifiedCount > 0 ? verifiedCount : (completedCount + verifiedCount)) / totalPlanned).toFixed(3))
          : (completedCount + verifiedCount) < totalPlanned
            ? Math.min(Number(((verifiedCount > 0 ? verifiedCount : (completedCount + verifiedCount)) / totalPlanned).toFixed(3)), 0.99)
            : Number(((verifiedCount > 0 ? verifiedCount : (completedCount + verifiedCount)) / totalPlanned).toFixed(3))
      ) : 0;

      metrics = {
        ...metrics,
        tasks_total: totalPlanned,
        tasks_pending: pendingCount,
        tasks_ready: readyCount,
        tasks_running: runningCount,
        tasks_approval_required: approvalCount,
        tasks_paused: pausedCount,
        tasks_completed: completedCount,
        tasks_verified: verifiedCount,
        tasks_failed: failedCount,
        tasks_blocked: blockedCount,
        tasks_cancelled: cancelledCount,
        execution_progress_pct: execProgress,
        verification_progress_pct: verifProgress,
        success_rate: successRatio,
      };

      return {
        executionEvents,
        nodeMap,
        activeTaskId,
        retryingTaskIds,
        blockedNodeIds,
        approvalPendingIds,
        approvedTaskIds,
        deniedTaskIds,
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

  approveTask: async (taskId, planHash, notes) => {
    const { sessionId } = get();
    if (!sessionId) return;
    set((state) => {
      const approved = new Set(state.approvedTaskIds);
      approved.add(taskId);
      const pending = new Set(state.approvalPendingIds);
      pending.delete(taskId);
      return { approvedTaskIds: approved, approvalPendingIds: pending };
    });
    try {
      const hashToSend = planHash ?? get().planHash ?? undefined;
      await ardoApi.approveTask(sessionId, taskId, true, hashToSend, notes);
    } catch (err) {
      set({ error: String(err) });
    }
  },

  rejectTask: async (taskId) => {
    const { sessionId } = get();
    if (!sessionId) return;
    set((state) => {
      const denied = new Set(state.deniedTaskIds);
      denied.add(taskId);
      const pending = new Set(state.approvalPendingIds);
      pending.delete(taskId);
      return { deniedTaskIds: denied, approvalPendingIds: pending };
    });
    try {
      await ardoApi.approveTask(sessionId, taskId, false);
    } catch (err) {
      set({ error: String(err) });
    }
  },

  cancelSession: async () => {
    const { sessionId } = get();
    if (!sessionId) return;
    try {
      await ardoApi.cancelSession(sessionId);
      await get().refreshState();
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
        planHash: state.plan_hash ?? get().planHash,
        isFallbackPlan: state.is_fallback !== undefined ? Boolean(state.is_fallback) : get().isFallbackPlan,
        fallbackReason: state.fallback_reason !== undefined ? (state.fallback_reason ?? null) : get().fallbackReason,
        planningMode: state.planning_mode ?? get().planningMode,
        primaryFailureCode: state.primary_failure_code !== undefined ? (state.primary_failure_code ?? null) : get().primaryFailureCode,
        planningLimitations: state.limitations ?? get().planningLimitations,
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
      console.log('[RUN-GUARD] history/evidence refresh - loadHistory');
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
      console.log('[RUN-GUARD] history/evidence refresh - loadEvidence', sid);
      const res = await ardoApi.getEvidence(sid);
      const currentSid = get().sessionId;
      if (res && res.evidence_items && (!currentSid || currentSid === sid)) {
        set({ browserEvidence: res.evidence_items });
      }
      get().loadGoalVerification(sid);
    } catch {
      // Graceful fallback
    }
  },

  loadGoalVerification: async (targetSessionId) => {
    const sid = targetSessionId || get().sessionId;
    if (!sid) return;
    try {
      const res = await ardoApi.getGoalVerification(sid);
      const currentSid = get().sessionId;
      if (res && (!currentSid || (res.session_id === sid && currentSid === sid))) {
        set({ goalVerification: res });
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
      isFallbackPlan: false,
      fallbackReason: null,
      planningMode: 'primary_ai',
      primaryFailureCode: null,
      planningLimitations: [],
      wsStatus: 'idle',
      nodeMap: new Map(),
      executionQueue: [],
      parallelGroups: [],
      activeTaskId: null,
      activeBrowserAction: null,
      retryingTaskIds: new Set(),
      blockedNodeIds: new Set(),
      approvalPendingIds: new Set(),
      approvedTaskIds: new Set(),
      safetyMetadataMap: new Map(),
      deniedTaskIds: new Set(),
      browserEvidence: [],
      goalVerification: null,
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
