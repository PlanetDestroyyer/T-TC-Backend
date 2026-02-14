#!/bin/bash

echo "Installing TinyCell Dependencies..."
termux-setup-storage
pkg update -y && pkg upgrade -y
pkg install python git termux-api -y

echo "Setting up Virtual Environment..."
python -m venv venv
source venv/bin/activate

echo "Installing Python Dependencies..."
# Install all dependencies at once - pip will handle pydantic-core automatically
# Using --no-cache-dir to avoid storage issues and speed up installation
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir -r requirements.txt

# Create a simple start script
echo "#!/bin/bash" > start.sh
echo "cd \$(dirname \$0)" >> start.sh
echo "echo '✅ Local Agent Started!'" >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo "echo '📱 NOW: Return to the TinyCell App'" >> start.sh
echo 'echo "👆 Press the \"I have Run The Command\" button"' >> start.sh
echo "echo '⏳ Wait a few seconds for connection...'" >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo "echo 'Acquiring wake lock to prevent Android from killing the process...'" >> start.sh
echo "termux-wake-lock" >> start.sh
echo "./venv/bin/python agent.py" >> start.sh
chmod +x start.sh

echo "Setup Complete!"
echo "-----------------------------------------------"
echo "Run this command to start the agent:"
echo "./start.sh"
echo "-----------------------------------------------"
