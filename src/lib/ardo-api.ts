/**
 * ardo-api.ts — ARDO Frontend API Client
 * ========================================
 * HTTP and WebSocket client for all ARDO orchestration endpoints.
 */

const RAW_API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8001';
const API_BASE = RAW_API_BASE.replace(/\/+$/, '');
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
  plan_hash?: string;
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
  plan_hash?: string;
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
  id?: string;
  task_id: string;
  task_name: string;
  status: string;
  verification_status?: string;
  artifact_hash?: string;
  confidence_score?: number;
  captured_at?: number;
  content_payload?: string;
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

export interface GoalVerificationResponse {
  session_id: string;
  goal_text: string;
  overall_status: 'verified' | 'failed' | 'inconclusive' | 'needs_human_review';
  confidence_score: number;
  total_tasks: number;
  verified_tasks: number;
  failed_tasks: number;
  inconclusive_tasks: number;
  supporting_evidence_hashes: string[];
  explanation: string;
}

export interface PlaybookItem {
  id: string;
  user_id: string;
  goal_pattern: string;
  category: string;
  status: 'candidate' | 'promoted' | 'deprecated';
  version: number;
  success_count: number;
  failure_count: number;
  confidence_score: number;
  required_tools: string[];
  execution_steps: Array<Record<string, unknown>>;
  expected_outcomes: string[];
  created_at: number;
  updated_at: number;
  plan_hash?: string;
}

export interface SessionEvidenceResponse {
  session_id: string;
  evidence_count: number;
  evidence_items: BrowserEvidenceItem[];
  goal_verification?: GoalVerificationResponse;
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

export interface UserProfile {
  id: string;
  full_name: string;
  email: string;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
  last_login_at?: string | null;
}

export interface RegisterPayload {
  full_name: string;
  email: string;
  password: string;
  confirm_password: string;
  terms_accepted: boolean;
}

export interface LoginPayload {
  email: string;
  password: string;
  remember_me?: boolean;
}

export interface OAuthStatus {
  google_enabled: boolean;
  facebook_enabled: boolean;
}

export interface MongoUserProfile {
  user_id: string;
  email: string;
  full_name: string;
  profile_picture?: string | null;
  account_status: string;
  auth_providers: string[];
  preferences: {
    theme?: string;
    notifications_enabled?: boolean;
    [key: string]: unknown;
  };
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
  profile_sync_version: number;
}

// ── HTTP API ───────────────────────────────────────────────────────────────

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers,
  });

  if (!res.ok) {
    let errorDetail = '';
    try {
      const json = await res.json();
      if (typeof json.detail === 'object' && json.detail !== null) {
        errorDetail = json.detail.message || json.detail.reason || JSON.stringify(json.detail);
      } else {
        errorDetail = json.detail || json.message || JSON.stringify(json);
      }
    } catch {
      errorDetail = await res.text();
    }
    throw new Error(errorDetail || `ARDO API [${res.status}]`);
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
  approveTask: (sessionId: string, taskId: string, approved = true, planHash?: string, notes?: string) =>
    request<{
      status: string;
      task_id: string;
      session_id?: string;
      plan_hash?: string;
      approval_id?: string;
      expires_at?: string;
    }>(`/orchestrate/${sessionId}/approve/${taskId}`, {
      method: 'POST',
      body: JSON.stringify({ approved, plan_hash: planHash, notes }),
    }),

  /** Resume an interrupted orchestration session from checkpoint */
  resumeSession: (sessionId: string) =>
    request<{
      status: string;
      session_id: string;
      worker_id: string;
      plan_hash: string;
      restored_completed_tasks: number;
      remaining_tasks: number;
      total_tasks: number;
    }>(`/orchestrate/${sessionId}/resume`, { method: 'POST' }),

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

  /** Get goal outcome verification (Phase 3 Objective C) */
  getGoalVerification: (sessionId: string) =>
    request<GoalVerificationResponse>(`/orchestrate/${sessionId}/verify`),

  /** List reusable playbooks (Phase 3 Objective D) */
  getPlaybooks: (params?: { category?: string; status?: string }) => {
    const query = new URLSearchParams();
    if (params?.category) query.append('category', params.category);
    if (params?.status) query.append('status', params.status);
    const qs = query.toString();
    return request<{ playbooks: PlaybookItem[]; total: number }>(`/playbooks${qs ? `?${qs}` : ''}`);
  },

  /** Promote candidate playbook to promoted */
  promotePlaybook: (playbookId: string) =>
    request<{ status: string; playbook_id: string }>(`/playbooks/${playbookId}/promote`, {
      method: 'POST',
    }),

  /** Deprecate a playbook */
  deprecatePlaybook: (playbookId: string) =>
    request<{ status: string; playbook_id: string }>(`/playbooks/${playbookId}/deprecate`, {
      method: 'POST',
    }),

  /** Get knowledge base and semantic memory stats */
  getKnowledgeStats: () =>
    request<KnowledgeStats>('/knowledge/stats'),

  /** Search historical execution memories */
  getMemories: (q: string) =>
    request<MemoryItem[]>(`/knowledge/memories?q=${encodeURIComponent(q)}`),

  // ── Authentication Endpoints ──
  register: (body: RegisterPayload) =>
    request<UserProfile>('/auth/register', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  login: (body: LoginPayload) =>
    request<UserProfile>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  logout: () =>
    request<{ status: string; message: string }>('/auth/logout', {
      method: 'POST',
    }),

  getMe: () =>
    request<UserProfile>('/auth/me'),

  forgotPassword: (email: string) =>
    request<{ status: string; message: string }>('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email }),
    }),

  resetPassword: (body: { token: string; new_password: string; confirm_password: string }) =>
    request<{ status: string; message: string }>('/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  changePassword: (body: { current_password: string; new_password: string; confirm_password: string }) =>
    request<{ status: string; message: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getOAuthStatus: () =>
    request<OAuthStatus>('/auth/oauth-status'),

  getGoogleAuthUrl: (next = '/app') =>
    request<{ authorization_url: string; provider: string }>(`/auth/google?next=${encodeURIComponent(next)}`),

  getFacebookAuthUrl: (next = '/app') =>
    request<{ authorization_url: string; provider: string }>(`/auth/facebook?next=${encodeURIComponent(next)}`),

  getUserProfile: () =>
    request<MongoUserProfile>('/users/me'),

  updateUserProfile: (body: { full_name?: string; profile_picture?: string; preferences?: Record<string, unknown> }) =>
    request<MongoUserProfile>('/users/me', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  getAuthMethods: () =>
    request<{ user_id: string; auth_methods: Array<{ provider: string; email?: string; linked_at?: string }> }>('/users/me/auth-methods'),

  unlinkAuthMethod: (provider: string) =>
    request<{ status: string; message: string }>(`/users/me/auth-methods/${provider}`, {
      method: 'DELETE',
    }),

  getWsTicket: (sessionId: string) =>
    request<{ ticket: string; session_id: string; expires_in_seconds: number }>('/auth/ws-ticket', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    }),
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

  async connect(): Promise<void> {
    if (this.disposed) return;
    const { sessionId, onEvent, onConnect, onDisconnect, onError, maxReconnects = 5 } =
      this.options;

    let url = `${WS_BASE}/ws/orchestrate/${sessionId}`;
    try {
      const ticketRes = await ardoApi.getWsTicket(sessionId);
      if (ticketRes?.ticket) {
        url += `?ticket=${encodeURIComponent(ticketRes.ticket)}`;
      }
    } catch {
      // Fall back to cookie-based authentication if ticket endpoint is unreachable
    }

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

    this.ws.onclose = (event: CloseEvent) => {
      onDisconnect?.();
      // Code 1000 indicates clean session termination; do not reconnect
      if (event.code === 1000 || this.disposed) {
        this.disposed = true;
        return;
      }
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
