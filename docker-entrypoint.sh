#!/bin/sh
set -e

echo "=========================================================="
echo "  Personal Task Board (PTB) Production Container Starting"
echo "=========================================================="

# 1. Automated Neo4j Pre-flight & Schema Migration
if [ -n "$NEO4J_URI" ]; then
    echo ">> Checking Neo4j authoritative domain store on $NEO4J_URI..."
    python scripts/init_neo4j_ci.py || echo "Warning: Neo4j pre-flight returned non-zero, supervisor will handle reconnect."
fi

# 2. Command Dispatching
if [ "$1" = "run" ] || [ -z "$1" ]; then
    echo ">> Starting PTB Process Supervisor on $PTB_HOST (REST: $PTB_PORT, FastMCP: $PTB_MCP_PORT)..."
    exec python scripts/ptb_cli.py run --host "${PTB_HOST:-0.0.0.0}" --port "${PTB_PORT:-8000}" --mcp-port "${PTB_MCP_PORT:-8001}"
elif [ "$1" = "doctor" ]; then
    echo ">> Running PTB Diagnostic Doctor..."
    shift
    exec python scripts/ptb_cli.py doctor "$@"
elif [ "$1" = "init" ]; then
    echo ">> Running PTB Database Initialization..."
    exec python scripts/ptb_cli.py init
elif [ "$1" = "status" ]; then
    exec python scripts/ptb_cli.py status
elif [ "$1" = "ptb" ]; then
    shift
    exec python scripts/ptb_cli.py "$@"
else
    exec "$@"
fi
