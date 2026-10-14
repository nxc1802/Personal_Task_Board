# Multi-stage production build for Personal Task Board (PTB)
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install uv for deterministic high-speed package resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy dependency manifests first for maximum Docker layer caching
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml ./packages/contracts/
COPY packages/database/pyproject.toml ./packages/database/
COPY packages/graph_memory/pyproject.toml ./packages/graph_memory/
COPY services/acquisition/pyproject.toml ./services/acquisition/
COPY services/processing/pyproject.toml ./services/processing/
COPY services/intelligence/pyproject.toml ./services/intelligence/
COPY services/application/pyproject.toml ./services/application/
COPY services/mcp/pyproject.toml ./services/mcp/

# Install base dependencies into isolated virtual environment
RUN uv sync --frozen --no-dev --no-install-workspace

# -----------------------------------------------------------------------------
# Final Production Runner Stage
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PTB_HOST=0.0.0.0 \
    PTB_PORT=8000 \
    PTB_MCP_PORT=8001

# Install curl for HTTP health probes and CA certificates
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user
RUN groupadd -r ptbuser && useradd -r -g ptbuser -d /app -s /bin/bash ptbuser

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /app/.venv /app/.venv

# Copy entire repository source tree
COPY --chown=ptbuser:ptbuser . /app

# Sync and install workspace packages
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
RUN uv sync --frozen --no-dev

# Initialize runtime state directories with non-root ownership
RUN mkdir -p /app/data/openwebui /app/data/playwright /app/logs \
    && chown -R ptbuser:ptbuser /app/data /app/logs \
    && chmod +x /app/docker-entrypoint.sh

USER ptbuser

EXPOSE 8000 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["run"]
