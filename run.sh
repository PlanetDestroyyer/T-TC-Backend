#!/bin/bash

echo "🔍 Checking dependencies..."

MISSING=""
which python      &>/dev/null || MISSING="$MISSING python"
which git         &>/dev/null || MISSING="$MISSING git"
which node        &>/dev/null || MISSING="$MISSING nodejs"
which cloudflared &>/dev/null || MISSING="$MISSING cloudflared"
which termux-battery-status &>/dev/null || MISSING="$MISSING termux-api"

if [ -n "$MISSING" ]; then
  echo "📦 Installing:$MISSING"
  pkg install -y $MISSING
else
  echo "✅ All packages installed"
fi

cd ~/termux_backend || { echo "❌ Backend folder not found. Run setup.sh first."; exit 1; }

if [ ! -d venv ]; then
  echo "🐍 Setting up virtual environment..."
  python -m venv venv
  source venv/bin/activate
  pip install --no-cache-dir -r requirements.txt
else
  source venv/bin/activate
fi

echo ""
echo "🚀 Starting TinyCell..."
./start.sh
