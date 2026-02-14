#!/bin/bash

echo "Installing TinyCell Dependencies..."
termux-setup-storage
pkg update -y && pkg upgrade -y
pkg install python git termux-api openssl-tool -y

echo "Setting up Virtual Environment..."
python -m venv venv
source venv/bin/activate

echo "Installing Python Dependencies..."
# Install all dependencies at once - pip will handle pydantic-core automatically
# Using --no-cache-dir to avoid storage issues and speed up installation
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir -r requirements.txt

# Generate SSL certificate for HTTPS (required for Android app communication)
echo "Generating SSL Certificate..."
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes \
        -subj "/CN=TinyCell/O=TinyCell/C=IN" \
        -addext "subjectAltName=IP:127.0.0.1,IP:0.0.0.0,DNS:localhost" 2>/dev/null
    echo "✅ SSL Certificate generated!"
else
    echo "✅ SSL Certificate already exists."
fi

# Create a simple start script
echo "#!/bin/bash" > start.sh
echo "cd \$(dirname \$0)" >> start.sh
echo "" >> start.sh
echo "# Get device IP using Python (most reliable)" >> start.sh
echo 'DEVICE_IP=$(./venv/bin/python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((\"8.8.8.8\", 80)); print(s.getsockname()[0]); s.close()" 2>/dev/null)' >> start.sh
echo 'if [ -z "$DEVICE_IP" ]; then DEVICE_IP="unknown"; fi' >> start.sh
echo "" >> start.sh
echo "echo '✅ Local Agent Started!'" >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo "echo '📱 NOW: Return to the TinyCell App'" >> start.sh
echo 'echo "👆 Press the \"I have Run The Command\" button"' >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo 'echo "🌐 Your Device IP: $DEVICE_IP"' >> start.sh
echo 'echo "🔒 HTTPS URL: https://$DEVICE_IP:8443"' >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo 'echo "📋 Enter this IP in the TinyCell app: $DEVICE_IP"' >> start.sh
echo "echo '---------------------------------------------------'" >> start.sh
echo "echo 'Acquiring wake lock...'" >> start.sh
echo "termux-wake-lock" >> start.sh
echo "./venv/bin/python agent.py" >> start.sh
chmod +x start.sh

echo ""
echo "✅ Setup Complete!"
echo "-----------------------------------------------"
echo "Run this command to start the agent:"
echo "./start.sh"
echo "-----------------------------------------------"
