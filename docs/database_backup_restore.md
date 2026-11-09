# ARDO — Database Backup, Restore & Disaster Recovery Procedures

## 1. Database Responsibilities & Storage Boundaries

ARDO maintains a strict dual-database architecture:
- **PostgreSQL 16 (Authoritative):** Owns identity, user accounts, password hashes, session tokens (SHA-256 hashed), OAuth accounts, audit events, plan approvals, execution evidence records, knowledge vectors (`pgvector`), and reusable playbooks.
- **MongoDB Atlas (Application & Profiles):** Owns non-sensitive user profile preferences, execution graph timelines, and debug session telemetry. ZERO credentials or passwords are ever stored in MongoDB.

---

## 2. PostgreSQL Backup & Restore

### 2.1 Automated Periodic Backups
Perform automated daily full logical dumps and continuous WAL (Write-Ahead Logging) archiving for Point-In-Time Recovery (PITR).

#### Logical Backup Command (`pg_dump`):
```bash
# Compressed custom-format archive of authoritative database
pg_dump -h "${PGHOST:-localhost}" \
        -p "${PGPORT:-5432}" \
        -U "${PGUSER:-postgres}" \
        -d "${PGDATABASE:-ai_goal_planner}" \
        -F c -b -v \
        -f "/var/backups/ardo/postgres_$(date +%Y%m%d_%H%M%S).dump"
```

### 2.2 PostgreSQL Restore Procedure
```bash
# 1. Terminate active backend connections to prevent mid-restore writes
psql -U postgres -d ai_goal_planner -c "
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE datname = 'ai_goal_planner' AND pid <> pg_backend_pid();"

# 2. Restore database schema and records from archive
pg_restore -h "${PGHOST:-localhost}" \
           -p "${PGPORT:-5432}" \
           -U "${PGUSER:-postgres}" \
           -d "${PGDATABASE:-ai_goal_planner}" \
           -v --clean --if-exists \
           "/var/backups/ardo/postgres_target.dump"

# 3. Verify pgvector extension and indexes
psql -U postgres -d ai_goal_planner -c "
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"
```

---

## 3. MongoDB Backup & Restore

### 3.1 Logical Backup (`mongodump`)
```bash
# Export all application collections with oplog for consistency
mongodump --uri="${MONGODB_URI}" \
          --db="ai_goal_planner" \
          --gzip \
          --archive="/var/backups/ardo/mongo_$(date +%Y%m%d_%H%M%S).gz"
```

### 3.2 MongoDB Restore Procedure (`mongorestore`)
```bash
# Restore collections with drop-existing safeguard
mongorestore --uri="${MONGODB_URI}" \
             --db="ai_goal_planner" \
             --gzip \
             --drop \
             --archive="/var/backups/ardo/mongo_target.gz"
```

---

## 4. Disaster Recovery & Resynchronization

In the event of a total MongoDB cluster loss or desynchronization:
1. Re-provision an empty MongoDB database.
2. Start the ARDO FastAPI backend.
3. The [`ProfileSyncWorker`](file:///c:/Users/vahin/OneDrive/Documents/AI Goal-To-Task Planning Engine/backend/core/sync_worker.py) will automatically inspect PostgreSQL's `outbox_events` and replay pending profile events.
4. For full resynchronization of all historical users, run the idempotent sync repair utility:
   ```bash
   python -m backend.core.sync_worker --repair-all
   ```
5. Check health status via the readiness probe:
   ```bash
   curl -s http://localhost:8000/api/v1/health/ready
   ```

