#!/bin/bash
# Called by the agent after Update All.
# Waits for old agent to fully exit, pulls latest code, starts fresh.

AGENT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$AGENT_DIR"

echo "⏳ Waiting for old agent to exit..."
sleep 3

echo "🔄 git pull..."
git pull

echo "🚀 Starting agent..."
source "$AGENT_DIR/venv/bin/activate"
exec python "$AGENT_DIR/agent.py"
