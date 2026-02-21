#!/bin/bash

echo "⬇️  Pulling latest changes..."
git pull

echo "🛑 Stopping existing backend..."
pkill -f "agent.py"    2>/dev/null
pkill -f "uvicorn"     2>/dev/null
pkill -f "cloudflared" 2>/dev/null
sleep 2
echo "✅ Old processes cleared"

echo "🔍 Checking packages..."
MISSING=""
which python      &>/dev/null || MISSING="$MISSING python"
which git         &>/dev/null || MISSING="$MISSING git"
which node        &>/dev/null || MISSING="$MISSING nodejs"
which cloudflared &>/dev/null || MISSING="$MISSING cloudflared"
which termux-battery-status &>/dev/null || MISSING="$MISSING termux-api"

if [ -n "$MISSING" ]; then
    echo "📦 Installing:$MISSING"
    pkg install -y $MISSING
fi

if [ ! -d venv ]; then
    echo "🐍 Setting up virtual environment..."
    python -m venv venv
fi

source venv/bin/activate
echo "📦 Installing/updating Python packages..."
pip install --no-cache-dir -q -r requirements.txt

echo "🚀 Starting backend..."
./start.sh
