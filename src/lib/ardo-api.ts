/**
 * ardo-api.ts — ARDO Frontend API Client
 * ========================================
 * HTTP and WebSocket client for all ARDO orchestration endpoints.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
const WS_BASE = API_BASE.replace(/^http/, 'ws');

// ── Types ──────────────────────────────────────────────────────────────────

export interface ARDOStartRequest {
  goal: string;
  mode?: 'safe' | 'sequential' | 'parallel' | 'aggressive';
}

export interface ARDONodeState {
  task_id: string;
  task_name: string;
  dependency_ids: string[];
  status: string;
  priority_score: number;
  risk_score: number;
  retry_count: number;
  max_retries: number;
  retry_confidence: number;
  estimated_execution_time: number;
  actual_execution_time: number | null;
  execution_type: string;
  execution_mode: string;
  fallback_available: boolean;
  parallel_group: number | null;
  approval_needed: boolean;
  execution_logs: string[];
  starvation_counter: number;
}

export interface ARDOSessionResponse {
  session_id: string;
  mode: string;
  task_count: number;
  parallel_groups: string[][];
  initial_queue: string[];
  initial_graph: ARDONodeState[];
}

export interface OrchestrationEvent {
  event_type: string;
  session_id: string;
  task_id?: string;
  task_name?: string;
  status?: string;
  details?: Record<string, unknown>;
  timestamp: number;
}

export interface ARDOStateResponse {
  session_id: string;
  mode: string;
  execution_queue: string[];
  graph_state: ARDONodeState[];
  parallel_groups: string[][];
  metrics: SessionMetrics;
  why_now?: WhyNowContext;
  reschedule_log: RescheduleEvent[];
  stability_events: StabilityEvent[];
  weight_suggestion?: WeightSuggestion;
}

export interface SessionMetrics {
  session_id: string;
  elapsed_seconds: number;
  tasks_total: number;
  tasks_completed: number;
  tasks_failed: number;
  tasks_blocked: number;
  tasks_retried: number;
  total_retry_count: number;
  reschedule_count: number;
  approval_count: number;
  throughput_per_minute: number;
  avg_latency_seconds: number;
  success_rate: number;
  timeline_markers: TimelineMarker[];
}

export interface TimelineMarker {
  type: 'start' | 'complete' | 'fail' | 'retry' | 'reschedule' | 'approve';
  task_id: string;
  task_name: string;
  timestamp: number;
  latency?: number;
  attempt?: number;
  delay?: number;
  reason?: string;
}

export interface WhyNowContext {
  task_id: string;
  task_name: string;
  final_score: number;
  factor_breakdown: Record<string, number>;
  decision_reason: string;
  selected_factors: string[];
  why_now?: string;
  timestamp: number;
}

export interface RescheduleEvent {
  trigger_task_id: string;
  trigger_event: string;
  previous_queue: string[];
  new_queue: string[];
  reason: string;
  timestamp: number;
}

export interface StabilityEvent {
  event_type: string;
  affected_tasks: string[];
  action_taken: string;
  timestamp: number;
}

export interface WeightSuggestion {
  suggested_weights: Record<string, number>;
  rationale: string;
  confidence: number;
  based_on_n_outcomes: number;
}

export interface SchedulingExplanation {
  task_id: string;
  task_name: string;
  final_score: number;
  factor_breakdown: Record<string, number>;
  raw_factors: Record<string, number>;
  decision_reason: string;
  selected_factors: string[];
  rescheduling_cause?: string;
  why_now?: string;
  timestamp: number;
}

export interface HistoryItem {
  session_id: string;
  goal: string;
  mode: string;
  task_count: number;
  tasks_completed?: number;
  tasks_failed?: number;
  tasks_blocked?: number;
  status: string;
  duration_seconds: number;
  timestamp: number;
}

export interface BrowserEvidenceItem {
  task_id: string;
  task_name: string;
  status: string;
  action: {
    action_type?: string;
    url?: string;
    selector?: string;
    value?: string;
    description?: string;
  };
  result: {
    success?: boolean;
    text_extracted?: string;
    screenshot_base64?: string;
    duration_ms?: number;
    metadata?: Record<string, unknown>;
    error?: string;
  };
}

export interface SessionEvidenceResponse {
  session_id: string;
  evidence_count: number;
  evidence_items: BrowserEvidenceItem[];
}

export interface KnowledgeStats {
  vector_backend: string;
  total_knowledge_chunks: number;
  total_historical_memories: number;
  embedding_dimension: number;
  is_cloud_embedding: boolean;
}

export interface MemoryItem {
  memory_id: string;
  goal_text: string;
  plan_id?: string;
  session_id?: string;
  memory_type: string;
  task_summary: string;
  outcome_status: string;
  execution_metrics?: Record<string, unknown>;
  relevance_score?: number;
  created_at?: number;
}

// ── HTTP API ───────────────────────────────────────────────────────────────

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const error = await res.text();
    throw new Error(`ARDO API [${res.status}]: ${error}`);
  }
  return res.json() as Promise<T>;
}

export const ardoApi = {
  /** Start a new ARDO orchestration session */
  startOrchestration: (body: ARDOStartRequest) =>
    request<ARDOSessionResponse>('/orchestrate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** Get current state of an ARDO session */
  getState: (sessionId: string) =>
    request<ARDOStateResponse>(`/orchestrate/${sessionId}/state`),

  /** Get execution metrics for a session */
  getMetrics: (sessionId: string) =>
    request<SessionMetrics>(`/orchestrate/${sessionId}/metrics`),

  /** Approve or reject a safety-gated task */
  approveTask: (sessionId: string, taskId: string, approved = true) =>
    request<{ status: string; task_id: string }>(`/orchestrate/${sessionId}/approve/${taskId}`, {
      method: 'POST',
      body: JSON.stringify({ approved }),
    }),

  /** Get scheduling explanation for a specific task */
  getExplanation: (sessionId: string, taskId: string) =>
    request<{ task_id: string; explanations: SchedulingExplanation[] }>(
      `/orchestrate/${sessionId}/explain/${taskId}`
    ),

  /** Get the active "Why This Task Now?" context */
  getWhyNow: (sessionId: string) =>
    request<WhyNowContext>(`/orchestrate/${sessionId}/why-now`),

  /** Plan only (no ARDO session) */
  createPlan: (goal: string) =>
    request<unknown>('/plan', { method: 'POST', body: JSON.stringify({ goal }) }),

  /** Get historical execution sessions */
  getHistory: () =>
    request<{ history: HistoryItem[] }>('/orchestrate/history'),

  /** Get browser execution and screenshot evidence for a session */
  getEvidence: (sessionId: string) =>
    request<SessionEvidenceResponse>(`/orchestrate/${sessionId}/evidence`),

  /** Get knowledge base and semantic memory stats */
  getKnowledgeStats: () =>
    request<KnowledgeStats>('/knowledge/stats'),

  /** Search historical execution memories */
  getMemories: (q: string) =>
    request<MemoryItem[]>(`/knowledge/memories?q=${encodeURIComponent(q)}`),
};

// ── WebSocket ──────────────────────────────────────────────────────────────

export interface ARDOWebSocketOptions {
  sessionId: string;
  onEvent: (event: OrchestrationEvent) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onError?: (error: Event) => void;
  maxReconnects?: number;
}

export class ARDOWebSocket {
  private ws: WebSocket | null = null;
  private reconnectCount = 0;
  private disposed = false;

  constructor(private readonly options: ARDOWebSocketOptions) {}

  connect(): void {
    if (this.disposed) return;
    const { sessionId, onEvent, onConnect, onDisconnect, onError, maxReconnects = 5 } =
      this.options;

    const url = `${WS_BASE}/ws/orchestrate/${sessionId}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.reconnectCount = 0;
      onConnect?.();
    };

    this.ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as OrchestrationEvent;
        onEvent(event);
      } catch {
        // Ignore malformed messages
      }
    };

    this.ws.onclose = () => {
      onDisconnect?.();
      if (!this.disposed && this.reconnectCount < maxReconnects) {
        const delay = Math.min(1000 * 2 ** this.reconnectCount, 15000);
        this.reconnectCount++;
        setTimeout(() => this.connect(), delay);
      }
    };

    this.ws.onerror = (e) => onError?.(e);
  }

  disconnect(): void {
    this.disposed = true;
    this.ws?.close();
    this.ws = null;
  }
}
