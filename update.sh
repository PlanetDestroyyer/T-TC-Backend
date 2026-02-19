#!/bin/bash

echo "⬇️  Updating backend..."
git pull

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

[ ! -d venv ] && python -m venv venv && source venv/bin/activate && pip install --no-cache-dir -r requirements.txt || source venv/bin/activate

echo "🚀 Starting backend..."
./start.sh
