from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mimetypes
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
    """Return device temperature in Celsius."""
    # Method 1: termux-battery-status (most accurate)
    try:
        out = run_command("termux-battery-status", timeout=4)
        if out and out.strip().startswith("{"):
            temp = json.loads(out).get("temperature")
            if temp and float(temp) > 0:
                return float(temp)
    except Exception:
        pass

    # Method 2: sysfs thermal zones
    try:
        import os
        def _sysfs(p):
            try:
                with open(p) as f: return f.read().strip()
            except: return None
        for zone in sorted(os.listdir("/sys/class/thermal/")):
            if not zone.startswith("thermal_zone"):
                continue
            raw = _sysfs(f"/sys/class/thermal/{zone}/temp")
            if raw and raw.lstrip("-").isdigit():
                temp = int(raw) / 1000.0
                if 20.0 <= temp <= 85.0:
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

    # Method 1: termux-battery-status (richest data, needs Termux:API app running)
    try:
        out = run_command("termux-battery-status", timeout=4)
        if out and out.strip().startswith("{"):
            data = json.loads(out)
            battery_info = {
                "percentage": data.get("percentage", 0),
                "plugged": data.get("plugged", "UNPLUGGED") != "UNPLUGGED",
                "status": data.get("status", "unknown").lower(),
                "health": data.get("health", ""),
                "temperature": data.get("temperature", 0),
            }
    except Exception:
        pass

    # Method 2: sysfs (no Termux:API needed)
    if not battery_info:
        def _sysfs(path):
            try:
                with open(path) as f: return f.read().strip()
            except: return None
        for root in ["/sys/class/power_supply/battery", "/sys/class/power_supply/Battery",
                     "/sys/class/power_supply/BAT0", "/sys/class/power_supply/bms"]:
            cap = _sysfs(f"{root}/capacity")
            if cap and cap.isdigit():
                st = _sysfs(f"{root}/status") or "unknown"
                battery_info = {
                    "percentage": int(cap),
                    "plugged": st.lower() not in ("discharging", "not charging"),
                    "status": st.lower(),
                }
                break

    # Method 3: psutil
    if not battery_info:
        try:
            b = psutil.sensors_battery()
            if b:
                battery_info = {
                    "percentage": b.percent,
                    "plugged": b.power_plugged,
                    "status": "charging" if b.power_plugged else "discharging",
                }
        except Exception:
            pass

    if not battery_info:
        battery_info = {"percentage": 0, "plugged": False, "status": "unavailable"}

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

@app.get("/ping")
def ping():
    return {"service": "tinycell", "version": "1.0"}

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


@app.post("/shutdown")
async def shutdown_server():
    """Stop all apps, NAS server, and terminate the agent process."""
    try:
        deployer.shutdown_all()
    except Exception as e:
        print(f"⚠️ shutdown_all error: {e}")
    try:
        nas_proc = _nas_state.get("proc")
        if nas_proc:
            nas_proc.terminate()
        _nas_state["proc"] = None
        _nas_state["url"] = None
    except Exception as e:
        print(f"⚠️ NAS teardown error: {e}")
    asyncio.create_task(_kill_self())
    return {"status": "shutting_down"}


async def _kill_self():
    await asyncio.sleep(0.5)
    os.kill(os.getpid(), signal.SIGTERM)


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


@app.post("/apps/{app_id}/update")
async def update_app(app_id: str):
    """Pull latest code, reinstall deps, restart app. Poll /deploy/{id}/progress for steps."""
    deploy_id = await deployer.update_app(app_id)
    return {"deploy_id": deploy_id}



@app.delete("/apps/{app_id}")
def delete_app(app_id: str):
    return {"success": deployer.delete_app(app_id)}


@app.get("/apps/{app_id}/metrics")
def get_app_metrics(app_id: str):
    return deployer.get_app_metrics(app_id)


@app.get("/apps/{app_id}/logs")
def get_app_logs(app_id: str, lines: int = 100):
    return {"logs": deployer.get_app_logs(app_id, lines)}


@app.get("/activity")
def get_activity(lines: int = 100):
    """Return the last N backend activity events (deploy, stop, start, delete, update, errors)."""
    return {"events": deployer.get_activity_log(lines)}


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

# ─── NAS Public Server (Cloudflare Tunnel) ───────────────────────────────────

import re as _re

_NAS_URL_RE = _re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_NAS_TUNNEL_LOG = os.path.expanduser("~/.nas_tunnel.log")
_NAS_CHUNKS_DIR = os.path.expanduser("~/.nas_chunks")
_nas_state: dict = {"proc": None, "url": None}


def _nas_tunnel_alive() -> bool:
    proc = _nas_state.get("proc")
    return bool(proc and proc.poll() is None)


async def _wait_nas_url():
    for _ in range(40):
        await asyncio.sleep(1)
        try:
            m = _NAS_URL_RE.search(open(_NAS_TUNNEL_LOG).read())
            if m:
                _nas_state["url"] = m.group(0)
                print(f"🌐 NAS public URL: {_nas_state['url']}")
                return
        except Exception:
            pass
    print("⚠️ NAS tunnel URL not found within 40s")


@app.get("/nas/public/status")
def nas_public_status():
    alive = _nas_tunnel_alive()
    if not alive:
        _nas_state["url"] = None
    return {"running": alive, "url": _nas_state.get("url")}


@app.post("/nas/public/start")
async def nas_public_start():
    if _nas_tunnel_alive():
        return {"running": True, "url": _nas_state.get("url")}
    try:
        open(_NAS_TUNNEL_LOG, "w").close()
    except Exception:
        pass
    with open(_NAS_TUNNEL_LOG, "w") as lf:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://localhost:8000"],
            stdout=lf, stderr=subprocess.STDOUT,
        )
    _nas_state["proc"] = proc
    _nas_state["url"] = None
    asyncio.create_task(_wait_nas_url())
    return {"running": True, "url": None}


@app.post("/nas/public/stop")
def nas_public_stop():
    proc = _nas_state.get("proc")
    if proc:
        proc.terminate()
    _nas_state["proc"] = None
    _nas_state["url"] = None
    return {"status": "stopped"}


@app.get("/nas/ui")
def nas_ui():
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nas_ui.html")
    if os.path.exists(ui_path):
        return FileResponse(ui_path, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "UI not found"})


@app.post("/nas/upload-chunk")
async def nas_upload_chunk(
    root: str = Query(...),
    path: str = Query(default=""),
    filename: str = Query(...),
    chunk_index: int = Query(...),
    total_chunks: int = Query(...),
    file: UploadFile = File(...),
):
    """Receive one chunk of a large file. Assembles automatically when all chunks arrive."""
    safe_name = os.path.basename(filename)
    if not safe_name:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})

    chunk_dir = os.path.join(_NAS_CHUNKS_DIR, safe_name)
    os.makedirs(chunk_dir, exist_ok=True)

    chunk_path = os.path.join(chunk_dir, f"{chunk_index:06d}")
    with open(chunk_path, "wb") as f:
        while True:
            data = await file.read(65536)
            if not data:
                break
            f.write(data)

    received = len(os.listdir(chunk_dir))
    if received >= total_chunks:
        _, target_dir = _nas_resolve(root, path)
        if target_dir is None:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return JSONResponse(status_code=403, content={"error": "Access denied"})
        dest = os.path.join(target_dir, safe_name)
        try:
            with open(dest, "wb") as out:
                for i in range(total_chunks):
                    with open(os.path.join(chunk_dir, f"{i:06d}"), "rb") as cf:
                        shutil.copyfileobj(cf, out)
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return {"status": "complete", "filename": safe_name, "size": os.path.getsize(dest)}
        except Exception as e:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            return JSONResponse(status_code=500, content={"error": str(e)})

    return {"status": "chunk_received", "received": received, "total": total_chunks}


# ─── NAS File Browser ─────────────────────────────────────────────────────────

NAS_ROOTS: dict = {
    "home":      os.path.expanduser("~"),
    "shared":    os.path.expanduser("~/storage/shared"),
    "downloads": os.path.expanduser("~/storage/downloads"),
    "dcim":      os.path.expanduser("~/storage/dcim"),
}

def _nas_resolve(root_name: str, subpath: str):
    """Resolve root + subpath to absolute path. Returns (root_abs, target_abs) or (None, None)."""
    root_raw = NAS_ROOTS.get(root_name)
    if not root_raw:
        return None, None
    root_abs = os.path.realpath(root_raw)
    target_abs = os.path.realpath(os.path.join(root_abs, subpath.lstrip("/")))
    if not target_abs.startswith(root_abs):
        return None, None  # block path traversal
    return root_abs, target_abs


@app.get("/nas/roots")
def nas_roots():
    """List available NAS storage roots."""
    result = {}
    for name, raw in NAS_ROOTS.items():
        expanded = os.path.expanduser(raw)
        result[name] = {
            "path": expanded,
            "available": os.path.isdir(expanded),
        }
    return result


@app.get("/nas/browse")
def nas_browse(root: str = Query(...), path: str = Query(default="")):
    """List directory contents."""
    _, target = _nas_resolve(root, path)
    if target is None:
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.exists(target):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if not os.path.isdir(target):
        return JSONResponse(status_code=400, content={"error": "Not a directory"})

    items = []
    try:
        for entry in os.scandir(target):
            try:
                stat = entry.stat(follow_symlinks=False)
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "size": stat.st_size if not entry.is_dir() else 0,
                    "modified": int(stat.st_mtime),
                })
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": "Permission denied"})

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"root": root, "path": path, "items": items}


@app.get("/nas/download")
def nas_download(root: str = Query(...), path: str = Query(...)):
    """Stream a file download."""
    _, target = _nas_resolve(root, path)
    if target is None:
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.exists(target) or not os.path.isfile(target):
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mime, _ = mimetypes.guess_type(target)
    mime = mime or "application/octet-stream"
    filename = os.path.basename(target)

    def streamer():
        with open(target, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        streamer(),
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/nas/upload")
async def nas_upload(
    root: str = Query(...),
    path: str = Query(default=""),
    file: UploadFile = File(...),
):
    """Upload a file to the given directory. Streams in 64 KB chunks."""
    _, target_dir = _nas_resolve(root, path)
    if target_dir is None:
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.isdir(target_dir):
        return JSONResponse(status_code=400, content={"error": "Not a directory"})

    dest = os.path.join(target_dir, file.filename or "upload")
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": "Write permission denied"})

    return {"uploaded": file.filename, "size": os.path.getsize(dest)}


@app.delete("/nas/delete")
def nas_delete(root: str = Query(...), path: str = Query(...)):
    """Delete a file or empty directory."""
    _, target = _nas_resolve(root, path)
    if target is None:
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.exists(target):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    try:
        if os.path.isdir(target):
            os.rmdir(target)
        else:
            os.remove(target)
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": "Permission denied"})
    except OSError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"deleted": path}


# --- Legacy File Browser (kept for backward compat) ---
@app.get("/files/{path:path}")
def list_files(path: str):
    base_path = os.path.expanduser("~")
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
