# ARDO — Production Deployment, Operations & Architecture Guide

## 1. System Architecture & Responsibilities

ARDO (AI Goal-To-Task Planning Engine) is organized into high-cohesion, decoupled tiers:

```mermaid
flowchart TD
    subgraph ClientTier["Client Tier (Public Web)"]
        CLIENT[Next.js 16 Web Dashboard]
    end

    subgraph IngressTier["Ingress & Edge Security"]
        NGINX[NGINX Reverse Proxy / TLS Gateway]
        NGINX -->|HTTPS :443| APP_ROUTER[Next.js SSR :3000]
        NGINX -->|/api/v1, /ws| FASTAPI[FastAPI Backend :8000]
    end

    subgraph BackendTier["ARDO Application Services"]
        FASTAPI --> GUARDS[Security, Rate Limiting, CORS & CSRF Middlewares]
        GUARDS --> PLAN[LLM Planning & PlanCritic]
        GUARDS --> SCHED[ARDOScheduler & DAG Builder]
        SCHED --> ENG[RuntimeEngine & State Machine]
        ENG --> VAULT[Cryptographic EvidenceVault]
        ENG --> VERIF[SemanticVerifier Outcome Engine]
        ENG --> AGENT[Playwright BrowserAgent]
    end

    subgraph EgressBoundary["Egress Boundary (Isolated Network)"]
        AGENT --> SQUID[Squid Outbound Proxy :3128]
        SQUID -->|Public Web Only| INTERNET[(Public Internet)]
    end

    subgraph PersistenceTier["Authoritative Persistence"]
        FASTAPI --> PG[(PostgreSQL 16 + pgvector)]
        FASTAPI --> MONGO[(MongoDB Atlas)]
        FASTAPI --> WORKER[ProfileSyncWorker Transactional Outbox]
        WORKER --> MONGO
    end
```

---

## 2. Production Environment Variables Baseline

| Variable Name | Required | Default / Format | Description |
| :--- | :---: | :--- | :--- |
| `ENVIRONMENT` | **Yes** | `production` | Set to `production` to activate strict fail-fast validation. |
| `DEBUG` | **Yes** | `false` | Must be `false` in production. |
| `LOG_LEVEL` | No | `INFO` | Logging level (`INFO`, `WARNING`, `ERROR`). |
| `STRUCTURED_LOGGING` | No | `true` | Formats stdout logs as single-line JSON. |
| `POSTGRES_URI` | **Yes** | `postgresql://user:pass@host:5432/dbname` | Authoritative PostgreSQL 16 connection string with `pgvector`. |
| `AUTH_DB_BACKEND` | **Yes** | `postgres` | Must be `postgres`. SQLite is forbidden in production. |
| `MONGODB_URI` | **Yes** | `mongodb://host:27017/dbname` | MongoDB connection URI for profile and timeline storage. |
| `DATABASE_NAME` | No | `ai_goal_planner` | Target MongoDB database name. |
| `COOKIE_SECURE` | **Yes** | `true` | Enforces `Secure` flag on HTTPS session cookies. |
| `COOKIE_SAMESITE` | No | `lax` | SameSite cookie policy (`lax` or `strict`). |
| `SESSION_COOKIE_NAME` | No | `ardo_session` | Name of opaque session cookie. |
| `ALLOWED_ORIGINS` | **Yes** | `https://ardo.example.com` | Comma-separated list of exact HTTPS frontend origins. |
| `CSRF_TRUSTED_ORIGINS`| No | `${ALLOWED_ORIGINS}` | Trusted origins for state-changing requests. |
| `GEMINI_API_KEY` | **Yes** | `AQ....` | Google Gemini API key for goal planning and reasoning. |
| `HEADLESS_BROWSER` | No | `true` | Always `true` for headless container execution. |
| `BROWSER_EGRESS_PROXY`| **Yes** | `http://squid-egress:3128` | URL of controlled egress proxy for Playwright workers. |
| `GOOGLE_CLIENT_ID` | Optional | `...apps.googleusercontent.com` | Google OAuth Client ID (optional). |
| `GOOGLE_CLIENT_SECRET`| Optional| `GOCSPX-...` | Google OAuth Client Secret (optional). |
| `FACEBOOK_APP_ID` | Optional | `...` | Facebook App ID (optional). |
| `FACEBOOK_APP_SECRET`| Optional| `...` | Facebook App Secret (optional). |

---

## 3. Database Migration Execution

Database migrations must be executed explicitly prior to application startup:
```bash
# 1. Activate Python virtual environment
source .venv/bin/activate  # or .\.venv\Scripts\Activate.ps1 on Windows

# 2. Run authoritative PostgreSQL schema initialization & vector migrations
export PYTHONPATH="backend"
python -m backend.knowledge.migrations.run_pgvector_migration

# 3. Verify PostgreSQL tables and indexes
python -c "from core.auth_db import AuthDatabase; db = AuthDatabase.get_instance(); print(db.check_health())"
```

---

## 4. Production Deployment via Docker Compose

```bash
# 1. Copy and populate production secrets
cp .env.example .env.production
nano .env.production

# 2. Build and launch services in detached mode
docker compose -f deploy/docker-compose.prod.yml --env-file .env.production up -d --build

# 3. Validate service health
docker compose -f deploy/docker-compose.prod.yml ps
curl -s http://localhost:8000/api/v1/health/ready
```

---

## 5. NGINX Reverse Proxy Configuration

```nginx
upstream ardo_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

upstream ardo_frontend {
    server 127.0.0.1:3000;
    keepalive 32;
}

server {
    listen 80;
    server_name ardo.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ardo.example.com;

    ssl_certificate /etc/letsencrypt/live/ardo.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ardo.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Maximum request body size limit
    client_max_body_size 2M;

    # Frontend Next.js routes
    location / {
        proxy_pass http://ardo_frontend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # Backend API routes
    location /api/ {
        proxy_pass http://ardo_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # WebSocket real-time telemetry streaming
    location /api/v1/ws/ {
        proxy_pass http://ardo_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

---

## 6. Troubleshooting & Operational Runbooks

### 6.1 Database Disconnection / Outage
- **Symptom:** `/api/v1/health/ready` returns 503; logs indicate PostgreSQL `OperationalError`.
- **Action:**
  1. Check PostgreSQL service status: `systemctl status postgresql` or `docker logs ardo-postgres`.
  2. Verify connection string, user credentials, and pgvector extension.
  3. Once restored, ARDO automatically reconnects on the next query. The `ProfileSyncWorker` will replay any queued outbox events.

### 6.2 Browser Egress Failure / Navigation Errors
- **Symptom:** Browser tasks fail with `ERR_PROXY_CONNECTION_FAILED` or `ERR_ACCESS_DENIED`.
- **Action:**
  1. Check Squid proxy logs: `tail -f /var/log/squid/access.log`.
  2. Verify that the target domain resolves to a public IP and does NOT fall into RFC 1918 or metadata IP ranges.
  3. Confirm that Squid proxy is running: `curl -x http://squid-egress:3128 https://example.com/`.

---

## 7. Known Operational Limitations

1. **OAuth Live Testing:** Google and Facebook OAuth workflows are implemented and cryptographically validated with PKCE, state tokens, and nonce checks, but live end-user authentication requires real production provider credentials (`GOOGLE_CLIENT_ID`, `FACEBOOK_APP_ID`).
2. **MongoDB Asynchronous Eventual Consistency:** PostgreSQL is the synchronous authoritative source for identity. MongoDB Atlas user preferences reflect changes asynchronously via the transactional outbox worker. A brief propagation delay (< 3 seconds) is normal under standard load.

