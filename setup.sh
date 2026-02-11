#!/bin/bash

echo "Installing TinyCell Dependencies..."
termux-setup-storage
pkg update -y && pkg upgrade -y
pkg install python git rust binutils -y
pkg install python-pydantic -y || echo "Pre-built pydantic not found, will build from source..."

echo "Installing Python Dependencies..."
echo "Installing Python Dependencies..."
pip install --extra-index-url https://termux-user-repository.github.io/pypi/ pydantic-core
pip install -r requirements.txt

echo "Setup Complete. Run 'python agent.py' to start the agent."
