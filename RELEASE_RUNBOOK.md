# Personal Task Board (PTB) — Production Release Runbook & Operations Guide

## 1. System Topology & Release Specification

| Component | Container Name | Bind Address / Port | Purpose | Default Security Profile |
|:---|:---|:---|:---|:---|
| **Neo4j Single-Store** | `ptb_neo4j_prod` | `127.0.0.1:7687`, `127.0.0.1:7474` | Authoritative entity store & temporal graph memory | Non-root, password authentication, APOC restricted |
| **PTB Application Service** | `ptb_app_prod` | `127.0.0.1:8000` | REST API (14 business + 6 control endpoints) | Non-root `ptbuser`, CORS origin restricted, API key optional |
| **FastMCP Server** | `ptb_app_prod` | `127.0.0.1:8001` | SSE & stdio protocol for Coding Agents | Read-only mode default (10 tools); write mode (19 tools) with RBAC |
| **OpenWebUI Frontend** | `ptb_openwebui_prod` | `127.0.0.1:3000` | Local web chat, tools & embedded Kanban board | `WEBUI_AUTH=True` in production |

---

## 2. Pre-Flight Verification & Release Gates

Before deploying or declaring release readiness, execute the automated release readiness suite:

```bash
# 1. Run 14 Behavioral Release Gates
uv run pytest tests/test_release_readiness.py -v

# 2. Verify Dataset Integrity Manifests
uv run python -m benchmarks.cli validate --dataset-dir benchmarks/dataset/mini_smoke
uv run python -m benchmarks.cli validate --dataset-dir benchmarks/dataset/ptb_500

# 3. Verify Non-Docker Unit & Contract Tests
uv run pytest -m "not external_integration" -q
```

All commands must exit with code `0`. Any failure blocks deployment.

---

## 3. Production Deployment Procedure

### 3.1 Initial Environment Setup
1. Copy production configuration template:
   ```bash
   cp .env.example .env
   ```
2. Configure mandatory production parameters in `.env`:
   ```ini
   NEO4J_PASSWORD=<SECURE_RANDOM_PASSWORD>
   TENANT_ID=production-tenant
   CURRENT_USER_NAME=Production User
   WEBUI_AUTH=True
   PTB_ENABLE_DESTRUCTIVE_API=false
   PTB_CORS_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
   ```
3. If LLM capabilities are enabled, inject provider keys:
   ```ini
   LLM_PROVIDER=openai_compatible
   LLM_BASE_URL=https://api.openai.com/v1
   LLM_API_KEY=<SECRET_KEY>
   LLM_MODEL=gpt-4o-mini
   ```

### 3.2 Single-Command Production Launch
```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Container startup flow:
1. `neo4j` starts and runs APOC initialization and health probe.
2. `ptb-app` awaits `neo4j` healthy status (`condition: service_healthy`).
3. Entrypoint applies schema constraints (`python scripts/init_neo4j_ci.py`).
4. `ptb-app` supervisor launches REST (`:8000`) and FastMCP (`:8001`).
5. `open-webui` connects to `ptb-app` via `http://ptb-app:8000`.

---

## 4. Health Checks & Verification

### 4.1 Truthful Health Endpoint
```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
```
Expected output:
```json
{
  "status": "healthy",
  "service": "ptb-application",
  "neo4j": "connected",
  "subsystems": {
    "database": "healthy",
    "mcp": "healthy",
    "processing": "healthy"
  }
}
```

### 4.2 Comprehensive Diagnostic Doctor
```bash
docker compose -f docker-compose.prod.yml exec ptb-app python scripts/ptb_cli.py doctor
```
Or for live LLM probe:
```bash
docker compose -f docker-compose.prod.yml exec ptb-app python scripts/ptb_cli.py doctor --deep
```

### 4.3 FastMCP Health Endpoint
```bash
curl -s http://127.0.0.1:8001/health
```

---

## 5. Rollback & Disaster Recovery Procedures

### 5.1 Immediate Container Rollback
If an upgrade or release exhibits anomalies:
```bash
# 1. Stop current containers
docker compose -f docker-compose.prod.yml down

# 2. Revert to previous release tag / commit
git checkout tags/<PREVIOUS_RELEASE_TAG>

# 3. Rebuild and launch previous stable version
docker compose -f docker-compose.prod.yml up -d --build
```

### 5.2 Neo4j Database Backup & Restore
- **Backup**:
  ```bash
  docker compose -f docker-compose.prod.yml stop neo4j
  docker run --rm -v ptb_neo4j_prod_data:/data -v $(pwd)/backups:/backup alpine tar czvf /backup/neo4j_$(date +%Y%m%d_%H%M%S).tar.gz /data
  docker compose -f docker-compose.prod.yml start neo4j
  ```
- **Restore**:
  ```bash
  docker compose -f docker-compose.prod.yml stop neo4j
  docker run --rm -v ptb_neo4j_prod_data:/data -v $(pwd)/backups:/backup alpine sh -c "rm -rf /data/* && tar xzvf /backup/<BACKUP_FILE>.tar.gz -C /"
  docker compose -f docker-compose.prod.yml start neo4j
  ```

---

## 6. Incident Response & Troubleshooting Matrix

| Bug Code | Symptom | Immediate Diagnostic Action | Remediation |
|:---|:---|:---|:---|
| `PTB-STORAGE-001` | Neo4j unavailable, supervisor halts | `docker compose -f docker-compose.prod.yml ps neo4j` | Check Neo4j memory limits, volume disk space, and password synchronization. |
| `PTB-CKPT-001` | Ingestion checkpoint error | Check Neo4j `IngestionCheckpoint` nodes | Run `python scripts/ptb_cli.py doctor` to verify checkpoint composite keys. |
| `PTB-L1-001` | Microsoft session expired | Status reported as `AUTH_EXPIRED` | Execute re-authentication: `docker compose exec ptb-app ptb login microsoft`. |
| `PTB-L2-001` | Raw event processing failure | Event state marked `RETRY` | Verify LLM provider connectivity and payload formatting. |
| `PTB-LLM-001` | LLM API unavailable | Extraction enters fallback mode | Verify `LLM_API_KEY`, quota, and proxy endpoints. |
| `PTB-GRAPH-001` | Graphiti sync failure | Tasks saved to Neo4j; sync `RETRY` | Background worker will retry; trigger manual rebuild via `ptb graph rebuild` if persistent. |
| `PTB-OWUI-001` | OpenWebUI cannot reach backend | Browser shows "APPLICATION SERVICE OFFLINE" | Check if `ptb-app` port 8000 is running and `PTB_CORS_ORIGINS` includes UI origin. |

---

## 7. Security & Compliance Checklist

- [x] All service ports bound strictly to `127.0.0.1` (`localhost`).
- [x] Application container runs as unprivileged user `ptbuser`.
- [x] `WEBUI_AUTH=True` enforced in production deployment profile.
- [x] `PTB_ENABLE_DESTRUCTIVE_API=false` enforced to block unauthorized database resets.
- [x] Zero runtime test doubles or mock databases in production code paths.
- [x] All checkpoints enforce composite key tenant isolation: `sha256(tenant_id:source_type:stream_id)`.
- [x] FastMCP defaults to 10 read-only query tools for third-party AI assistants.
