from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import platform
import shutil
import psutil
import subprocess
import json
import asyncio
import ssl

app = FastAPI()

# Enable CORS for React Native
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def run_command(command):
    """Run a shell command and return the output"""
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception as e:
        return str(e)

def get_device_ip():
    """Get the device's WiFi IP address"""
    try:
        output = run_command("ifconfig wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}'")
        if output:
            return output
    except:
        pass
    return "unknown"

def get_system_stats():
    """Get comprehensive device status"""
    battery_info = {}
    try:
        battery_output = run_command("termux-battery-status")
        if battery_output and battery_output.strip().startswith("{"):
             battery_info = json.loads(battery_output)
    except:
        pass

    if not battery_info:
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_info = {
                    "percentage": battery.percent,
                    "plugged": battery.power_plugged,
                    "status": "charging" if battery.power_plugged else "discharging"
                }
        except Exception:
            battery_info = {
                "percentage": 0,
                "plugged": False,
                "status": "unavailable"
            }

    # Memory info with fallback
    try:
        memory_info = dict(psutil.virtual_memory()._asdict())
    except Exception:
        memory_info = {"total": 0, "available": 0, "percent": 0}

    # Disk info with fallback
    try:
        disk_info = dict(psutil.disk_usage('/')._asdict())
    except Exception:
        disk_info = {"total": 0, "free": 0, "percent": 0}

    return {
        "status": "online",
        "device_name": platform.node(),
        "machine": platform.machine(),
        "system": platform.system(),
        "processor": run_command("getprop ro.product.board") or platform.processor(),
        "android_version": run_command("getprop ro.build.version.release"),
        "battery": battery_info,
        "memory": memory_info,
        "disk": disk_info
    }

@app.get("/status")
def get_status():
    return get_system_stats()

@app.get("/ip")
def get_ip():
    """Return device IP so the app can auto-discover it"""
    return {"ip": get_device_ip()}

@app.get("/scan")
def scan_hardware():
    cpu_info = run_command("cat /proc/cpuinfo")
    mem_info = run_command("cat /proc/meminfo")
    thermal_zones = run_command("ls /sys/class/thermal/thermal_zone*/temp 2>/dev/null").split('\n')
    has_thermal = len(thermal_zones) > 0

    return {
        "cpu_info": cpu_info,
        "mem_info": mem_info,
        "thermal_support": has_thermal,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "tier": "Tier 1"
    }

# --- WebSocket for Real-Time Stats ---
@app.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            stats = get_system_stats()
            await websocket.send_json(stats)
            await asyncio.sleep(2) # Send updates every 2 seconds
    except WebSocketDisconnect:
        print("Client disconnected")

# --- File Browser (NAS Foundation) ---
@app.get("/files/{path:path}")
def list_files(path: str):
    """List files in a directory or serve a file"""
    # Security: Prevent escaping root (basic implementation)
    base_path = os.path.expanduser("~") # Use home directory
    target_path = os.path.abspath(os.path.join(base_path, path))
    
    if not target_path.startswith(base_path):
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    
    if os.path.isfile(target_path):
        return FileResponse(target_path)
    
    if os.path.isdir(target_path):
        items = []
        try:
            for entry in os.scandir(target_path):
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if not entry.is_dir() else 0
                })
        except PermissionError:
             return JSONResponse(status_code=403, content={"error": "Permission denied"})
             
        return {"path": path, "items": items}
        
    return JSONResponse(status_code=404, content={"error": "Not found"})

if __name__ == "__main__":
    import uvicorn
    
    cert_file = os.path.join(os.path.dirname(__file__), "cert.pem")
    key_file = os.path.join(os.path.dirname(__file__), "key.pem")
    
    device_ip = get_device_ip()
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        # HTTPS mode - accessible from other apps
        print(f"🔒 Starting HTTPS server...")
        print(f"📱 Device IP: {device_ip}")
        print(f"🌐 Access: https://{device_ip}:8443/status")
        uvicorn.run(app, host="0.0.0.0", port=8443,
                    ssl_keyfile=key_file, ssl_certfile=cert_file)
    else:
        # Fallback to HTTP
        print(f"⚠️  No SSL certs found. Running HTTP only (limited access).")
        print(f"Run setup.sh to generate SSL certificates.")
        uvicorn.run(app, host="0.0.0.0", port=8000)
