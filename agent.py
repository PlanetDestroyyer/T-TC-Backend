from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import signal
import platform
import shutil
import psutil
import subprocess
import json
import asyncio
import ssl
import time
import uvicorn
from threading import Thread
import deployer

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

# ─── Thermal Management ───────────────────────────────────────────────────────

TEMP_WARM     = 43   # Warn user
TEMP_HOT      = 46   # Heavy warn + block deploys
TEMP_CRITICAL = 50   # Emergency: kill all apps

# Shared with deployer — populated when apps are deployed/started
running_apps = deployer.running_apps

# Last known thermal state (read by /thermal endpoint)
thermal_state: dict = {"temp": 0.0, "level": "normal", "last_check": 0}


def get_temperature() -> float:
    """Return device temperature in Celsius. Tries battery API then sysfs."""
    # Method 1: termux-battery-status (most accurate, returns battery temp)
    try:
        output = run_command("termux-battery-status", timeout=2)
        if output and output.strip().startswith("{"):
            data = json.loads(output)
            temp = data.get("temperature")
            if temp and float(temp) > 0:
                return float(temp)
    except Exception:
        pass

    # Method 2: sysfs thermal zones (CPU temp, millidegrees → Celsius)
    try:
        zones_raw = run_command("ls /sys/class/thermal/", timeout=0.5)
        for zone in zones_raw.split():
            if zone.startswith("thermal_zone"):
                raw = run_command(f"cat /sys/class/thermal/{zone}/temp", timeout=0.5)
                if raw and raw.lstrip("-").isdigit():
                    temp = int(raw) / 1000.0
                    if 20.0 <= temp <= 85.0:   # sanity range
                        return temp
    except Exception:
        pass

    return 0.0


def _thermal_level(temp: float) -> str:
    if temp >= TEMP_CRITICAL:
        return "emergency"
    if temp >= TEMP_HOT:
        return "critical"
    if temp >= TEMP_WARM:
        return "hot"
    if temp >= 40:
        return "warm"
    return "normal"


def _emergency_shutdown_apps() -> list:
    """SIGKILL all tracked deployed apps. Returns list of killed app IDs."""
    killed = []
    for app_id, pid in list(running_apps.items()):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(app_id)
            print(f"🔴 THERMAL: Killed app '{app_id}' (pid {pid})")
        except ProcessLookupError:
            pass   # already dead
        except Exception as e:
            print(f"⚠️ THERMAL: Could not kill '{app_id}' (pid {pid}): {e}")
        running_apps.pop(app_id, None)

    # Termux notification
    if killed:
        msg = f"Apps stopped: {', '.join(killed)}"
    else:
        msg = "No apps were running."
    run_command(
        f'termux-notification '
        f'--title "🔥 TinyCell Emergency Shutdown" '
        f'--content "Phone overheating! {msg}" '
        f'--priority max --vibrate 1000,500,1000',
        timeout=3,
    )
    return killed


async def _thermal_monitor_loop():
    """Background task: check temp every 30 s and act on thresholds."""
    global thermal_state
    while True:
        await asyncio.sleep(30)
        temp = await asyncio.to_thread(get_temperature)
        level = _thermal_level(temp)
        thermal_state = {"temp": temp, "level": level, "last_check": time.time()}
        print(f"🌡️  THERMAL: {temp}°C → {level}")

        if level == "emergency":
            print(f"🔴 THERMAL EMERGENCY: {temp}°C — killing all apps")
            await asyncio.to_thread(_emergency_shutdown_apps)

        elif level == "critical":
            run_command(
                f'termux-notification '
                f'--title "🚨 TinyCell Critical Temp" '
                f'--content "Temperature {temp}°C! Cool phone NOW or apps will stop." '
                f'--priority max --vibrate 500,250,500',
                timeout=3,
            )

        elif level == "hot":
            run_command(
                f'termux-notification '
                f'--title "⚠️ TinyCell High Temp" '
                f'--content "Temperature {temp}°C. Consider cooling phone." '
                f'--priority high',
                timeout=3,
            )


# ──────────────────────────────────────────────────────────────────────────────

def get_system_stats():
    """Get comprehensive device status"""
    battery_info = {}
    print("  STATS: Getting battery...")

    # Method 1: Try sysfs first (fastest, works on most Android devices)
    try:
        capacity = run_command("cat /sys/class/power_supply/battery/capacity", timeout=0.5)
        status_str = run_command("cat /sys/class/power_supply/battery/status", timeout=0.5)
        if capacity and capacity.isdigit():
            battery_info = {
                "percentage": int(capacity),
                "plugged": status_str.lower() not in ["discharging", "not charging"],
                "status": status_str.lower() if status_str else "unknown"
            }
            print(f"  STATS: Battery from sysfs: {capacity}%")
    except Exception as e:
        print(f"  STATS: Sysfs failed: {e}")

    # Method 2: Try termux-battery-status only if sysfs failed
    # (requires Termux:API app to be installed)
    if not battery_info:
        try:
            battery_output = run_command("termux-battery-status", timeout=0.3)
            if battery_output and battery_output.strip().startswith("{"):
                battery_info = json.loads(battery_output)
                print("  STATS: Battery from termux-api")
        except:
            pass

    # Method 3: Final Fallback - psutil
    if not battery_info:
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_info = {
                    "percentage": battery.percent,
                    "plugged": battery.power_plugged,
                    "status": "charging" if battery.power_plugged else "discharging"
                }
                print("  STATS: Battery from psutil")
        except Exception:
            pass

    # Ensure we always have a struct (last resort)
    if not battery_info:
         battery_info = {
            "percentage": 0,
            "plugged": False,
            "status": "unavailable"
        }
         print("  STATS: Battery unavailable - using defaults")

    print("  STATS: Battery done")

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

    temp = get_temperature()
    return {
        "status": "online",
        "device_name": display_name,
        "machine": platform.machine(),
        "system": "Android", # Explicitly state Android for UI
        "processor": run_command("getprop ro.product.board") or platform.processor(),
        "android_version": run_command("getprop ro.build.version.release"),
        "battery": battery_info,
        "memory": memory_info,
        "disk": disk_info,
        "temperature": temp,
        "thermal_level": _thermal_level(temp),
    }

class DeployRequest(BaseModel):
    repo_url: str
    app_name: str
    app_type: str = "auto"
    auto_restart: bool = True


@app.on_event("startup")
async def startup_event():
    deployer.restore_on_startup()
    asyncio.create_task(_thermal_monitor_loop())
    asyncio.create_task(deployer.monitor_loop())
    print("🌡️  Thermal monitor started")
    print("🔄 App monitor started")


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

@app.get("/thermal")
def get_thermal():
    """Current temperature, threat level, and thresholds."""
    temp = get_temperature()
    level = _thermal_level(temp)
    return {
        "temperature": temp,
        "level": level,              # normal | warm | hot | critical | emergency
        "thresholds": {
            "warm": 40,
            "hot": TEMP_WARM,
            "critical": TEMP_HOT,
            "emergency": TEMP_CRITICAL,
        },
        "running_apps": list(running_apps.keys()),
    }


@app.post("/thermal/emergency-shutdown")
def manual_emergency_shutdown():
    """Manually trigger emergency shutdown of all running apps."""
    killed = _emergency_shutdown_apps()
    return {"killed": killed, "message": f"Stopped {len(killed)} app(s)"}


@app.post("/deploy")
async def deploy_app(req: DeployRequest):
    deploy_id = await deployer.deploy(req.repo_url, req.app_name, req.app_type, req.auto_restart)
    return {"deploy_id": deploy_id}


@app.get("/deploy/{deploy_id}/progress")
def get_deploy_progress(deploy_id: str):
    progress = deployer.get_deploy_progress(deploy_id)
    if not progress:
        return JSONResponse(status_code=404, content={"error": "Deploy ID not found"})
    return progress


@app.get("/apps")
def list_apps():
    return deployer.get_all_apps()


@app.get("/apps/{app_id}")
def get_app(app_id: str):
    app = deployer.get_app(app_id)
    if not app:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return app


@app.post("/apps/{app_id}/start")
async def start_app(app_id: str):
    ok = await deployer.start_app(app_id)
    return {"success": ok}


@app.post("/apps/{app_id}/stop")
def stop_app(app_id: str):
    return {"success": deployer.stop_app(app_id)}


@app.delete("/apps/{app_id}")
def delete_app(app_id: str):
    return {"success": deployer.delete_app(app_id)}


@app.get("/apps/{app_id}/metrics")
def get_app_metrics(app_id: str):
    return deployer.get_app_metrics(app_id)


@app.get("/apps/{app_id}/logs")
def get_app_logs(app_id: str, lines: int = 100):
    return {"logs": deployer.get_app_logs(app_id, lines)}


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
