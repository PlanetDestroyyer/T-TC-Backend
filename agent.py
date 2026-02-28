from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mimetypes
import os
import sys
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
import re
from threading import Thread
import deployer

# ─── Persistent Logging ──────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.log")

class _Tee:
    """Mirror all stdout/stderr to main.log with timestamps. Appends across restarts."""
    def __init__(self, stream):
        self._stream = stream
        self._file = open(_LOG_PATH, "a", buffering=1, encoding="utf-8")

    def write(self, msg):
        self._stream.write(msg)
        if msg.strip():
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for line in msg.rstrip("\n").split("\n"):
                if line.strip():
                    self._file.write(f"[{ts}] {line}\n")

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        return self._stream.isatty()

sys.stdout = _Tee(sys.stdout)
sys.stderr = _Tee(sys.stderr)
print("=" * 60)
print(f"🟢 Agent process started  PID={os.getpid()}")
print("=" * 60)
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI()

# Enable CORS for React Native
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    ms = int((time.time() - t0) * 1000)
    print(f"[HTTP] {request.method} {request.url.path} → {response.status_code} ({ms}ms)")
    return response

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
    except Exception:
        return "unknown"

# ─── Thermal Management ───────────────────────────────────────────────────────

TEMP_MILD     = 40   # Inform user (warm level)
TEMP_WARM     = 43   # Warn user (hot level)
TEMP_HOT      = 46   # Heavy warn + block deploys (critical level)
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

    # Method 4: /proc/batt_id or similar Android proc paths (some proot envs)
    if not battery_info:
        try:
            for proc_path in ["/proc/batt_id", "/proc/battery"]:
                if os.path.exists(proc_path):
                    raw = open(proc_path).read().strip()
                    if raw.isdigit():
                        battery_info = {
                            "percentage": int(raw),
                            "plugged": False,
                            "status": "unknown",
                        }
                        break
        except Exception:
            pass

    if not battery_info:
        battery_info = {"percentage": -1, "plugged": False, "status": "unavailable"}

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
            "warm": TEMP_MILD,
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


@app.post("/system/update")
async def system_update():
    """
    Stop all running apps cleanly, then hand off to restart.sh which
    pulls latest code and starts a fresh agent.
    """
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    asyncio.create_task(_shutdown_and_restart(agent_dir))
    return {"status": "updating"}


async def _shutdown_and_restart(agent_dir: str):
    """Stop all apps, then hand off to restart.sh (pull + start)."""
    try:
        deployer.shutdown_all()
        print("✅ All apps stopped cleanly")
    except Exception as e:
        print(f"⚠️ shutdown_all error: {e}")

    restart_sh  = os.path.join(agent_dir, "restart.sh")
    restart_log = os.path.join(agent_dir, "restart.log")
    try:
        if os.path.exists(restart_sh):
            proc = subprocess.Popen(
                ["bash", restart_sh],
                start_new_session=True,   # detach — survives parent's os._exit
                stdin=subprocess.DEVNULL,
                stdout=open(restart_log, "w"),
                stderr=subprocess.STDOUT,
            )
            print(f"🚀 restart.sh launched (PID {proc.pid}, logs → restart.log)")
        else:
            print(f"⚠️ restart.sh not found at {restart_sh}")
    except Exception as e:
        print(f"⚠️ Failed to launch restart.sh: {e}")

    await asyncio.sleep(0.5)
    os._exit(0)  # Free port 8000; restart.sh takes over after sleep 3


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

# ─── NAS Public Server (SSH Tunnel — multi-provider with auto-reconnect) ──────
#
# Provider priority:
#   1. pinggy.io   — 60 min free sessions, faster, more reliable
#   2. localhost.run — 8 min sessions (auto-reconnect handles expiry)
#   3. serveo.net  — backup
#
# Auto-reconnect: a background task watches the SSH process and restarts
# it automatically when the session expires or disconnects. The frontend
# polls /nas/public/status and gets the new URL automatically.

_NAS_PROVIDERS = [
    {
        "name": "pinggy.io",
        "cmd": ["ssh", "-p", "443",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-T", "-n",
                "-R", "0:localhost:8000",
                "a.pinggy.io"],
        "url_re": re.compile(r"https://[a-z0-9-]+\.a\.pinggy\.io"),
    },
    {
        "name": "localhost.run",
        "cmd": ["ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-T", "-n",
                "-R", "80:localhost:8000",
                "nokey@localhost.run"],
        "url_re": re.compile(r"https://[a-z0-9-]+\.lhr\.life"),
    },
    {
        "name": "serveo.net",
        "cmd": ["ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-T", "-n",
                "-R", "80:localhost:8000",
                "serveo.net"],
        "url_re": re.compile(r"https://[a-z0-9-]+\.serveo\.net"),
    },
]

# Combined regex for fast URL detection regardless of provider
_NAS_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.(a\.pinggy\.io|lhr\.life|serveo\.net)"
)
_NAS_TUNNEL_LOG = os.path.expanduser("~/.nas_tunnel.log")
_NAS_CHUNKS_DIR = os.path.expanduser("~/.nas_chunks")
_nas_state: dict = {
    "proc": None,
    "url": None,
    "url_task": None,
    "monitor_task": None,
    "enabled": False,        # True while user wants tunnel running
    "provider_idx": 0,       # Which provider we last used successfully
}


def _nas_tunnel_alive() -> bool:
    proc = _nas_state.get("proc")
    return bool(proc and proc.poll() is None)


def _start_tunnel_proc(provider: dict) -> subprocess.Popen:
    """Launch the SSH subprocess for the given provider config."""
    os.makedirs(os.path.expanduser("~/.ssh"), mode=0o700, exist_ok=True)
    return subprocess.Popen(
        provider["cmd"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )


async def _read_tunnel_url(proc: subprocess.Popen, url_re: re.Pattern) -> str | None:
    """Read SSH stdout until we get a URL or process dies."""
    loop = asyncio.get_event_loop()

    def _read():
        try:
            for line in proc.stdout:
                print(f"[tunnel] {line.rstrip()}")
                m = url_re.search(line)
                if m:
                    return m.group(0)
        except Exception as e:
            print(f"[tunnel] read error: {e}")
        return None

    return await loop.run_in_executor(None, _read)


async def _tunnel_monitor():
    """
    Background task: keep the tunnel alive as long as _nas_state['enabled'].
    Tries providers in order; on failure rotates to the next one.
    Auto-reconnects seamlessly — frontend just polls /nas/public/status.
    """
    provider_count = len(_NAS_PROVIDERS)
    fail_streak = 0

    while _nas_state.get("enabled"):
        idx = _nas_state.get("provider_idx", 0) % provider_count
        provider = _NAS_PROVIDERS[idx]
        print(f"🔌 Starting tunnel via {provider['name']}…")

        try:
            proc = _start_tunnel_proc(provider)
            _nas_state["proc"] = proc
            _nas_state["url"] = None

            url = await _read_tunnel_url(proc, provider["url_re"])
            if url:
                print(f"✅ Tunnel URL ({provider['name']}): {url}")
                _nas_state["url"] = url
                fail_streak = 0
                # Wait for process to exit (session expiry / network drop)
                await asyncio.get_event_loop().run_in_executor(None, proc.wait)
                print(f"⚠️ Tunnel via {provider['name']} ended — reconnecting…")
            else:
                print(f"⚠️ {provider['name']} gave no URL — trying next provider")
                proc.terminate()
                fail_streak += 1
                # Rotate to next provider after 2 consecutive failures on current
                if fail_streak >= 2:
                    _nas_state["provider_idx"] = (idx + 1) % provider_count
                    fail_streak = 0
                    print(f"🔄 Switching to {_NAS_PROVIDERS[_nas_state['provider_idx']]['name']}")

        except Exception as e:
            print(f"[tunnel] error with {provider['name']}: {e}")
            fail_streak += 1
            if fail_streak >= 2:
                _nas_state["provider_idx"] = (idx + 1) % provider_count
                fail_streak = 0

        if _nas_state.get("enabled"):
            print("⏳ Reconnecting in 3s…")
            await asyncio.sleep(3)

    print("🔒 Tunnel monitor stopped")
    _nas_state["proc"] = None
    _nas_state["url"] = None


@app.get("/nas/public/status")
def nas_public_status():
    alive = _nas_tunnel_alive()
    if not alive and not _nas_state.get("enabled"):
        _nas_state["url"] = None
    return {
        "running": _nas_state.get("enabled", False),
        "url": _nas_state.get("url"),
    }


@app.post("/nas/public/start")
async def nas_public_start():
    if _nas_state.get("enabled"):
        return {"running": True, "url": _nas_state.get("url")}

    # Ensure openssh-client is available (auto-install if missing)
    import shutil as _shutil
    if not _shutil.which("ssh"):
        try:
            subprocess.run(["apk", "add", "--no-cache", "openssh-client"],
                           check=True, capture_output=True, timeout=60)
        except Exception as e:
            return JSONResponse(status_code=503, content={
                "running": False, "url": None,
                "error": f"openssh-client not available: {e}",
            })

    _nas_state["enabled"] = True
    _nas_state["provider_idx"] = 0  # start with pinggy.io
    # Cancel any existing monitor
    old = _nas_state.get("monitor_task")
    if old and not old.done():
        old.cancel()
    _nas_state["monitor_task"] = asyncio.create_task(_tunnel_monitor())
    return {"running": True, "url": None}


@app.post("/nas/public/stop")
def nas_public_stop():
    _nas_state["enabled"] = False
    task = _nas_state.get("monitor_task")
    if task and not task.done():
        task.cancel()
    _nas_state["monitor_task"] = None
    old_url_task = _nas_state.get("url_task")
    if old_url_task and not old_url_task.done():
        old_url_task.cancel()
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
    is_last: bool = Query(default=False),
    file: UploadFile = File(...),
):
    """Receive one chunk of a large file. Assembles automatically when is_last is True."""
    safe_name = os.path.basename(filename)
    if not safe_name:
        print(f"[NAS-CHUNK] Rejected: empty filename")
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})

    print(f"[NAS-CHUNK] Chunk {chunk_index} for {safe_name!r} (root={root!r} path={path!r} is_last={is_last})")
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
    if is_last:
        _, target_dir = _nas_resolve(root, path)
        if target_dir is None:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            print(f"[NAS-CHUNK] Access denied: root={root!r} path={path!r}")
            return JSONResponse(status_code=403, content={"error": "Access denied"})
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, safe_name)
        print(f"[NAS-CHUNK] Assembling {received} chunks → {dest!r}")
        try:
            with open(dest, "wb") as out:
                # The frontend uploaded chunks starting sequentially from 0
                for i in range(chunk_index + 1):
                    chunk_file = os.path.join(chunk_dir, f"{i:06d}")
                    if os.path.exists(chunk_file):
                        with open(chunk_file, "rb") as cf:
                            shutil.copyfileobj(cf, out)
                    else:
                        print(f"[NAS-CHUNK] Warning: missing chunk {i}")
            shutil.rmtree(chunk_dir, ignore_errors=True)
            size = os.path.getsize(dest)
            print(f"[NAS-CHUNK] Upload complete: {dest!r} ({size} bytes)")
            return {"status": "complete", "filename": safe_name, "size": size}
        except Exception as e:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            print(f"[NAS-CHUNK] Assembly error: {e!r}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    return {"status": "chunk_received", "received": received, "current_chunk": chunk_index}


# ─── NAS File Browser ─────────────────────────────────────────────────────────

# Resolve TinyCell folder — only use a storage path if its parent symlink exists
# (~/storage/* symlinks are created by termux-setup-storage; fall back to Termux home)
def _make_tinycell() -> str:
    candidates = [
        os.path.expanduser("~/storage/downloads/TinyCell"),
        os.path.expanduser("~/storage/shared/TinyCell"),
        os.path.expanduser("~/TinyCell"),  # always available inside Termux home
    ]
    for _p in candidates:
        parent = os.path.dirname(_p)
        if not os.path.exists(parent):
            print(f"[NAS] Skipping {_p} — parent does not exist")
            continue
        try:
            os.makedirs(_p, exist_ok=True)
            print(f"[NAS] TinyCell folder ready: {_p}")
            return _p
        except Exception as _e:
            print(f"[NAS] Could not create {_p}: {_e}")
    # Should never reach here
    return candidates[0]

_TINYCELL_DIR = _make_tinycell()

NAS_ROOTS: dict = {
    "tinycell": _TINYCELL_DIR,
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
        # Auto-create the shared_nas folder if it doesn't exist yet
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Could not create folder: {e}"})
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
def nas_download(root: str = Query(...), path: str = Query(...), download: bool = Query(default=False)):
    """Stream a file. Uses inline disposition by default so browsers preview it;
    pass ?download=1 to force a Save dialog."""
    _, target = _nas_resolve(root, path)
    if target is None:
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not os.path.exists(target) or not os.path.isfile(target):
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mime, _ = mimetypes.guess_type(target)
    mime = mime or "application/octet-stream"
    filename = os.path.basename(target)
    disposition = f'attachment; filename="{filename}"' if download else f'inline; filename="{filename}"'

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
            "Content-Disposition": disposition,
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
    print(f"[NAS] Upload request: root={root!r} path={path!r} file={file.filename!r}")
    _, target_dir = _nas_resolve(root, path)
    if target_dir is None:
        print(f"[NAS] Upload denied: invalid root={root!r} or path traversal in path={path!r}")
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    os.makedirs(target_dir, exist_ok=True)  # create folder if it doesn't exist yet
    if not os.path.isdir(target_dir):
        print(f"[NAS] Upload failed: target is not a directory: {target_dir!r}")
        return JSONResponse(status_code=400, content={"error": "Not a directory"})

    dest = os.path.join(target_dir, os.path.basename(file.filename or "upload"))
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        size = os.path.getsize(dest)
        print(f"[NAS] Uploaded OK: {dest!r} ({size} bytes)")
        return {"uploaded": file.filename, "size": size}
    except PermissionError:
        print(f"[NAS] Upload permission denied: {dest!r}")
        return JSONResponse(status_code=403, content={"error": "Write permission denied"})
    except Exception as e:
        print(f"[NAS] Upload error: {e!r}")
        return JSONResponse(status_code=500, content={"error": str(e)})


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
