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
import uvicorn
from threading import Thread

app = FastAPI()

# Enable CORS for React Native
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def run_command(command, timeout=1):
    """Run a shell command and return the output"""
    try:
        # Add timeout to prevent hanging forever
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"⚠️ Command timed out: {command}")
        return ""
    except Exception as e:
        print(f"❌ Command failed: {command} - {e}")
        return str(e)

def get_device_ip():
    """Get the device's WiFi IP address"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "unknown"

def get_system_stats():
    """Get comprehensive device status"""
    battery_info = {}
    print("  STATS: Getting battery...")
    try:
        battery_output = run_command("termux-battery-status")
        if battery_output and battery_output.strip().startswith("{"):
             battery_info = json.loads(battery_output)
    except:
        pass
    print("  STATS: Battery done")

    # Fallback: Try reading directly from sysfs (works on many Androids even without root)
    if not battery_info or battery_info.get("status") == "unavailable":
        try:
            # Note: This might fail on some devices without root, but works on many
            capacity = run_command("cat /sys/class/power_supply/battery/capacity", timeout=0.5)
            status_str = run_command("cat /sys/class/power_supply/battery/status", timeout=0.5)
            if capacity and capacity.isdigit():
                battery_info = {
                    "percentage": int(capacity),
                    "plugged": status_str.lower() != "discharging",
                    "status": status_str.lower() if status_str else "unknown"
                }
        except Exception as e:
            print(f"  STATS: Sysfs read failed: {e}")

    # Final Fallback: psutil
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
            pass

    # Ensure we always have a struct
    if not battery_info:
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
    # Disk info - Use current directory (Termux home) for more relevant stats
    try:
        disk_info = dict(psutil.disk_usage('.')._asdict())
    except Exception:
        disk_info = {"total": 0, "free": 0, "percent": 0}

    # Get real device name
    device_model = run_command("getprop ro.product.model") or platform.node()
    device_man = run_command("getprop ro.product.manufacturer")
    if device_man and device_model:
        display_name = f"{device_man} {device_model}"
    else:
        display_name = device_model

    return {
        "status": "online",
        "device_name": display_name,
        "machine": platform.machine(),
        "system": "Android", # Explicitly state Android for UI
        "processor": run_command("getprop ro.product.board") or platform.processor(),
        "android_version": run_command("getprop ro.build.version.release"),
        "battery": battery_info,
        "memory": memory_info,
        "disk": disk_info
    }

@app.get("/")
def read_root():
    return {"status": "ok", "message": "TinyCell Agent is Running"}

@app.get("/status")
def get_status():
    return get_system_stats()

@app.get("/ip")
def get_ip():
    """Return device IP for information purposes"""
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
    print("🔌 WebSocket connection attempt...")
    await websocket.accept()
    print("✅ WebSocket accepted!")
    
    try:
        # Send immediate welcome message
        await websocket.send_json({"message": "Connected!", "status": "ok"})
        print("📤 Sent welcome message")
        
        while True:
            # Use asyncio.to_thread to run the blocking get_system_stats function
            # This prevents the main event loop from blocking
            print("🔄 Loop: Fetching stats...")
            stats = await asyncio.to_thread(get_system_stats)
            
            print(f"📤 Sending stats: {stats.get('status')}")
            await websocket.send_json(stats)
            await asyncio.sleep(2)
            
    except WebSocketDisconnect:
        print("🔌 Client disconnected")
    except Exception as e:
        print(f"❌ WebSocket error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

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

def run_https_server():
    """Run HTTPS server for browser access"""
    cert_file = os.path.join(os.path.dirname(__file__), "cert.pem")
    key_file = os.path.join(os.path.dirname(__file__), "key.pem")
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        device_ip = get_device_ip()
        print(f"🔒 HTTPS server starting on https://{device_ip}:8443")
        uvicorn.run(app, host="0.0.0.0", port=8443,
                    ssl_keyfile=key_file, ssl_certfile=cert_file,
                    log_level="warning")
    else:
        print("⚠️  No SSL certs - HTTPS disabled. Browser access unavailable.")

if __name__ == "__main__":
    cert_file = os.path.join(os.path.dirname(__file__), "cert.pem")
    key_file = os.path.join(os.path.dirname(__file__), "key.pem")
    
    device_ip = get_device_ip()
    
    print("=" * 60)
    print("🚀 TinyCell Agent - Dual Server Mode")
    print("=" * 60)
    print(f"📱 Device IP: {device_ip}")
    print(f"📲 App Access:     http://127.0.0.1:8000/status")
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        print(f"🌐 Browser Access: https://{device_ip}:8443/status")
        print("=" * 60)
        
        # Start HTTPS server in background thread
        https_thread = Thread(target=run_https_server, daemon=True)
        https_thread.start()
        
        # Run HTTP server on 0.0.0.0 (required for WiFi IP access)
        print(f"🔓 HTTP server starting on http://{device_ip}:8000")
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
    else:
        print("⚠️  SSL certs not found - running HTTP only")
        print("=" * 60)
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
