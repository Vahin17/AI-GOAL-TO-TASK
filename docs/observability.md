# ARDO — Observability, Structured Logging & Metrics Architecture

## 1. Structured JSON Logging Architecture

In production (`ENVIRONMENT=production` or `STRUCTURED_LOGGING=true`), ARDO formats all application logs as single-line JSON objects adhering to standard observability standards (Datadog, Elastic, CloudWatch, Google Cloud Logging).

### 1.1 JSON Log Schema
```json
{
  "timestamp": "2026-11-07T16:20:00.123456Z",
  "level": "INFO",
  "logger": "orchestration.runtime_engine",
  "message": "[RuntimeEngine] Task t_reserve completed in 0.24s.",
  "correlation_id": "corr_9a8b7c6d5e4f"
}
```

### 1.2 Correlation ID Lifecycle
- Generated or extracted on every inbound HTTP request by [`CorrelationIdMiddleware`](file:///c:/Users/vahin/OneDrive/Documents/AI Goal-To-Task Planning Engine/backend/core/logging_config.py).
- Stored in a thread-safe `contextvars.ContextVar`.
- Attached to log lines automatically.
- Returned in the HTTP response header `X-Correlation-ID`.

### 1.3 Pre-Log Credential & Evidence Redaction
- All log lines pass through [`PrivacyRedactor.redact_text`](file:///c:/Users/vahin/OneDrive/Documents/AI Goal-To-Task Planning Engine/backend/execution/evidence/privacy_redactor.py) before reaching `stdout`.
- Masks JWT tokens, API keys (`sk-...`), passwords, SSNs, Luhn credit cards, and customer emails.

---

## 2. Health & Readiness Probes

### 2.1 Liveness Probe (`GET /api/v1/health`)
- **Purpose:** Verifies that the FastAPI process is alive, the event loop is responsive, and worker processes have not deadlocked.
- **Response:**
  ```json
  {
    "status": "ok",
    "service": "planning-engine",
    "ardo": "active",
    "timestamp": "2026-11-07T16:20:00Z"
  }
  ```

### 2.2 Readiness Probe (`GET /api/v1/health/ready`)
- **Purpose:** Validates that all downstream dependencies (PostgreSQL, MongoDB, AI provider key, ToolRegistry) are operational.
- **Success (HTTP 200):**
  ```json
  {
    "ready": true,
    "auth_database": { "status": "healthy", "backend": "postgresql", "latency_ms": 1.25 },
    "mongodb": { "status": "healthy", "backend": "mongodb", "latency_ms": 3.42 },
    "ai_configured": true,
    "browser_tool_registered": true,
    "environment": "production"
  }
  ```
- **Failure (HTTP 503):** Returns when critical dependencies (PostgreSQL or AI key) are unreachable, allowing container orchestrators (Kubernetes / ECS) to withhold traffic.

---

## 3. Operational Metrics (`GET /api/v1/metrics`)

ARDO exposes internal metrics tracked by [`OperationalMetricsCollector`](file:///c:/Users/vahin/OneDrive/Documents/AI Goal-To-Task Planning Engine/backend/core/metrics.py):
- **HTTP Metrics:** Total requests, requests by endpoint & status code, latency percentiles (`p50`, `p90`, `p99`, `avg_ms`).
- **Planning Metrics:** Completed plan count, error count, and planning generation latency percentiles.
- **Browser Execution:** Completed browser task count, execution duration percentiles, and outcome distributions (`verified`, `failed`, `inconclusive`, `needs_human_review`).
- **Resilience Indicators:** Database errors count, worker lease losses, outbox synchronization retries, and active session count.

