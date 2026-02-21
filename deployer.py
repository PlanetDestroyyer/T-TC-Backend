import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid

import psutil

import port_manager

APPS_DIR = os.path.expanduser("~/apps")
_REGISTRY = os.path.join(APPS_DIR, ".registry.json")
_ACTIVITY_LOG = os.path.join(APPS_DIR, ".activity.log")
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_pids: dict = {}         # {app_id: {app_pid, tunnel_pid}}
running_apps: dict = {}  # {app_id: pid} — shared with thermal manager
_deployments: dict = {}  # {deploy_id: progress dict}
_tunnel_fail: dict = {}  # {app_id: consecutive_fail_count}


# ─── Registry ────────────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(_REGISTRY):
        try:
            with open(_REGISTRY) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ Registry corrupt ({e}), resetting to empty")
            # Back up the corrupt file for inspection
            try:
                os.rename(_REGISTRY, _REGISTRY + ".corrupt")
            except OSError:
                pass
    return {"apps": {}}


def _save(reg: dict):
    os.makedirs(APPS_DIR, exist_ok=True)
    # Write to a temp file then atomically rename — prevents corruption if killed mid-write
    tmp = _REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, _REGISTRY)


# ─── Activity Log ─────────────────────────────────────────────────────────────

def _log_activity(action: str, app_id: str, detail: str = ""):
    """Append a timestamped activity entry to the global activity log."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "app": app_id,
        "detail": detail,
    }
    os.makedirs(APPS_DIR, exist_ok=True)
    with open(_ACTIVITY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"📋 [{action}] {app_id}" + (f" — {detail}" if detail else ""))


def get_activity_log(lines: int = 100) -> list[dict]:
    """Return the last N activity entries, newest first."""
    if not os.path.exists(_ACTIVITY_LOG):
        return []
    with open(_ACTIVITY_LOG) as f:
        all_lines = f.readlines()
    entries = []
    for line in reversed(all_lines[-lines:]):
        try:
            entries.append(json.loads(line.strip()))
        except Exception:
            pass
    return entries



# ─── Public API ──────────────────────────────────────────────────────────────

def get_all_apps() -> list:
    reg = _load()
    apps = []
    for app in reg["apps"].values():
        app = dict(app)
        pid = _pids.get(app["id"], {}).get("app_pid")
        app["status"] = "running" if (pid and _is_alive(pid)) else (
            "stopped" if app["status"] == "running" else app["status"]
        )
        apps.append(app)
    return apps


def get_app(app_id: str) -> dict | None:
    reg = _load()
    app = reg["apps"].get(app_id)
    if not app:
        return None
    app = dict(app)
    pid = _pids.get(app_id, {}).get("app_pid")
    app["status"] = "running" if (pid and _is_alive(pid)) else (
        "stopped" if app["status"] == "running" else app["status"]
    )
    return app


def get_app_metrics(app_id: str) -> dict:
    pid = _pids.get(app_id, {}).get("app_pid")
    if not pid or not _is_alive(pid):
        return {"status": "stopped", "ram_mb": 0, "cpu_percent": 0, "uptime_seconds": 0}
    try:
        proc = psutil.Process(pid)
        return {
            "status": "running",
            "ram_mb": round(proc.memory_info().rss / (1024 * 1024)),
            "cpu_percent": proc.cpu_percent(interval=0.5),
            "uptime_seconds": int(time.time() - proc.create_time()),
        }
    except psutil.NoSuchProcess:
        return {"status": "stopped", "ram_mb": 0, "cpu_percent": 0, "uptime_seconds": 0}


def get_app_logs(app_id: str, lines: int = 100) -> str:
    reg = _load()
    app = reg["apps"].get(app_id)
    if not app:
        return ""
    log_file = os.path.join(app["app_dir"], "app.log")
    if not os.path.exists(log_file):
        return ""
    with open(log_file) as f:
        return "".join(f.readlines()[-lines:])


def get_deploy_progress(deploy_id: str) -> dict | None:
    return _deployments.get(deploy_id)


# ─── App lifecycle ───────────────────────────────────────────────────────────

def stop_app(app_id: str) -> bool:
    pids = _pids.pop(app_id, {})
    running_apps.pop(app_id, None)
    _kill(pids.get("app_pid"))
    _kill(pids.get("proxy_pid"))
    # Keep tunnel alive intentionally — URL stays valid
    reg = _load()
    if app_id in reg["apps"]:
        reg["apps"][app_id]["status"] = "stopped"
        _save(reg)
    _log_activity("stop", app_id)
    return True


async def start_app(app_id: str) -> bool:
    reg = _load()
    app = reg["apps"].get(app_id)
    if not app:
        return False
    proc = _launch_app(app)
    if not proc:
        return False
    _pids.setdefault(app_id, {})["app_pid"] = proc.pid
    running_apps[app_id] = proc.pid
    reg["apps"][app_id]["status"] = "running"
    reg["apps"][app_id]["pid"] = proc.pid
    _save(reg)
    _log_activity("start", app_id)
    # Only start a new tunnel if none is alive
    existing_tunnel = _pids.get(app_id, {}).get("tunnel_pid")
    if not existing_tunnel or not _is_alive(existing_tunnel):
        asyncio.create_task(_setup_tunnel(app_id, app["port"]))
    return True


def delete_app(app_id: str) -> bool:
    # Kill everything including tunnel on explicit delete
    pids = _pids.pop(app_id, {})
    running_apps.pop(app_id, None)
    _kill(pids.get("app_pid"))
    _kill(pids.get("proxy_pid"))
    _kill(pids.get("tunnel_pid"))
    reg = _load()
    app = reg["apps"].pop(app_id, None)
    _save(reg)
    if app:
        shutil.rmtree(app["app_dir"], ignore_errors=True)
        _log_activity("delete", app_id, f"repo + venv wiped from {app['app_dir']}")
    port_manager.release(app_id)
    # Release proxy port
    data_ports = port_manager._load()
    data_ports["allocated"].pop(f"__proxy_{app_id}", None)
    port_manager._save(data_ports)
    return True


# ─── Deployment ──────────────────────────────────────────────────────────────

async def deploy(repo_url: str, app_name: str, app_type: str, auto_restart: bool) -> str:
    deploy_id = uuid.uuid4().hex[:8]
    _deployments[deploy_id] = {
        "status": "deploying",
        "app_name": app_name,
        "steps": [],
        "error": None,
        "app_id": None,
    }
    asyncio.create_task(_run_deploy(deploy_id, repo_url, app_name, app_type, auto_restart))
    return deploy_id


async def update_app(app_id: str) -> str:
    """Pull latest code, reinstall deps, restart. Returns a deploy_id for progress polling."""
    deploy_id = uuid.uuid4().hex[:8]
    _deployments[deploy_id] = {
        "status": "deploying",
        "app_name": app_id,
        "steps": [],
        "error": None,
        "app_id": app_id,
    }
    asyncio.create_task(_run_update(deploy_id, app_id))
    return deploy_id


async def _run_update(deploy_id: str, app_id: str):
    def step(msg: str, done: bool = False, err: bool = False):
        _deployments[deploy_id]["steps"].append({"msg": msg, "done": done, "error": err})
        print(f"  UPDATE [{deploy_id}]: {msg}")

    reg = _load()
    app = reg["apps"].get(app_id)
    if not app:
        _deployments[deploy_id]["status"] = "error"
        _deployments[deploy_id]["error"] = "App not found"
        return

    app_dir = app["app_dir"]
    app_type = app["type"]
    port = app["port"]

    try:
        step("Stopping app...")
        stop_app(app_id)
        step("App stopped", done=True)

        step("Pulling latest code...")
        await _run_async(["git", "pull"], cwd=app_dir, timeout=60)
        step("Code updated", done=True)

        step("Reinstalling dependencies...")
        await _install_deps(app_dir, app_type)
        step("Dependencies ready", done=True)

        step("Starting app...")
        proc = _launch_app(app)
        if not proc:
            raise RuntimeError("Failed to launch app")
        _pids.setdefault(app_id, {})["app_pid"] = proc.pid
        running_apps[app_id] = proc.pid
        await asyncio.sleep(8)
        step("App started", done=True)

        step("Creating public URL...")
        tunnel_url = await _setup_tunnel(app_id, port) or ""
        step(f"URL: {tunnel_url or 'unavailable'}", done=True)

        reg = _load()
        if app_id in reg["apps"]:
            reg["apps"][app_id]["status"] = "running"
            reg["apps"][app_id]["pid"] = proc.pid
            reg["apps"][app_id]["tunnel_url"] = tunnel_url
            _save(reg)

        _deployments[deploy_id]["status"] = "done"
        _log_activity("update", app_id, f"url={tunnel_url}")

    except Exception as e:
        msg = str(e) or f"{type(e).__name__} (no message)"
        step(f"Error: {msg}", err=True)
        _deployments[deploy_id]["status"] = "error"
        _deployments[deploy_id]["error"] = msg
        _log_activity("update_error", app_id, msg)



async def _run_deploy(deploy_id: str, repo_url: str, app_name: str, app_type: str, auto_restart: bool):
    app_dir = os.path.join(APPS_DIR, app_name)

    def step(msg: str, done: bool = False, err: bool = False):
        _deployments[deploy_id]["steps"].append({"msg": msg, "done": done, "error": err})
        print(f"  DEPLOY [{deploy_id}]: {msg}")

    try:
        _log_activity("deploy_start", app_name, repo_url)
        if os.path.exists(app_dir):
            raise RuntimeError(f"App '{app_name}' already exists. Delete it first.")

        step("Cloning repository...")
        await _run_async(["git", "clone", "--depth=1", repo_url, app_dir], timeout=120)
        step("Repository cloned", done=True)

        if app_type == "auto":
            app_type = _detect_type(app_dir)
        step(f"Framework: {app_type}", done=True)

        port = port_manager.allocate(app_name, app_type)
        step(f"Port allocated: {port}", done=True)

        step("Installing dependencies...")
        await _install_deps(app_dir, app_type)
        step("Dependencies installed", done=True)

        step("Starting server...")
        proc = _launch_app({"type": app_type, "app_dir": app_dir, "port": port})
        if not proc:
            raise RuntimeError(f"Unknown app type: {app_type}")
        await asyncio.sleep(8)
        step("Server started", done=True)

        step("Creating public URL...")
        tunnel_url = await _setup_tunnel(app_name, port) or ""
        step(f"URL: {tunnel_url or 'unavailable'}", done=True)

        _pids[app_name] = {"app_pid": proc.pid}
        running_apps[app_name] = proc.pid

        reg = _load()
        reg["apps"][app_name] = {
            "id": app_name,
            "name": app_name,
            "type": app_type,
            "repo_url": repo_url,
            "port": port,
            "status": "running",
            "pid": proc.pid,
            "tunnel_url": tunnel_url,
            "tunnel_status": "active" if tunnel_url else "dead",
            "app_dir": app_dir,
            "auto_restart": auto_restart,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "restart_count": 0,
        }
        _save(reg)

        _deployments[deploy_id]["status"] = "done"
        _deployments[deploy_id]["app_id"] = app_name
        _log_activity("deploy", app_name, f"type={app_type} url={tunnel_url}")

    except Exception as e:
        msg = str(e) or f"{type(e).__name__} (no message)"
        step(f"Error: {msg}", err=True)
        _deployments[deploy_id]["status"] = "error"
        _deployments[deploy_id]["error"] = msg
        port_manager.release(app_name)
        shutil.rmtree(app_dir, ignore_errors=True)
        _log_activity("deploy_error", app_name, msg)


# ─── Background tasks ────────────────────────────────────────────────────────

async def _probe_tunnel(url: str) -> bool:
    """Return True if the tunnel URL responds with any HTTP status."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sf", "--max-time", "8", "-o", "/dev/null", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=12)
        return proc.returncode == 0
    except Exception:
        return False


async def monitor_loop():
    tick = 0
    while True:
        await asyncio.sleep(10)
        tick += 1
        reg = _load()
        changed = False

        for app_id, app in list(reg["apps"].items()):
            if app.get("status") != "running":
                continue

            # ── App crash check (every tick) ──────────────────────────────
            pid = _pids.get(app_id, {}).get("app_pid")
            if pid and not _is_alive(pid) and app.get("auto_restart"):
                print(f"⚠️ App '{app_id}' crashed, restarting...")
                proc = _launch_app(app)
                if proc:
                    _pids.setdefault(app_id, {})["app_pid"] = proc.pid
                    running_apps[app_id] = proc.pid
                    reg["apps"][app_id]["pid"] = proc.pid
                    reg["apps"][app_id]["restart_count"] = app.get("restart_count", 0) + 1
                    tunnel_pid = _pids.get(app_id, {}).get("tunnel_pid")
                    if not tunnel_pid or not _is_alive(tunnel_pid):
                        asyncio.create_task(_setup_tunnel(app_id, app["port"]))
                    changed = True

            # ── Tunnel keepalive + health check (every 12 ticks = 2 min) ──
            if tick % 12 == 0:
                tunnel_url = app.get("tunnel_url")
                if not tunnel_url:
                    # No URL yet — might be reconnecting already
                    continue
                alive = await _probe_tunnel(tunnel_url)
                if alive:
                    _tunnel_fail.pop(app_id, None)
                    if app.get("tunnel_status") != "active":
                        reg["apps"][app_id]["tunnel_status"] = "active"
                        changed = True
                else:
                    fails = _tunnel_fail.get(app_id, 0) + 1
                    _tunnel_fail[app_id] = fails
                    print(f"⚠️ Tunnel for '{app_id}' unresponsive (fail #{fails})")
                    if fails >= 2:
                        print(f"🔄 Restarting tunnel for '{app_id}'...")
                        reg["apps"][app_id]["tunnel_status"] = "reconnecting"
                        reg["apps"][app_id]["tunnel_url"] = None
                        changed = True
                        _tunnel_fail.pop(app_id, None)
                        asyncio.create_task(_setup_tunnel(app_id, app["port"]))
                    else:
                        reg["apps"][app_id]["tunnel_status"] = "checking"
                        changed = True

        if changed:
            _save(reg)


def shutdown_all():
    """Kill every running app and tunnel immediately."""
    for app_id in list(_pids.keys()):
        pids = _pids.pop(app_id, {})
        _kill(pids.get("app_pid"))
        _kill(pids.get("proxy_pid"))
        _kill(pids.get("tunnel_pid"))
    running_apps.clear()
    reg = _load()
    for app_id in reg["apps"]:
        reg["apps"][app_id]["status"] = "stopped"
    _save(reg)


async def update_all_apps() -> None:
    """Pull latest code and restart every registered app. Fully awaits all updates."""
    reg = _load()
    app_ids = list(reg["apps"].keys())
    if not app_ids:
        return

    # Build _run_update coroutines directly — do NOT use update_app() which
    # fire-and-forgets background tasks and returns immediately.
    tasks = []
    for app_id in app_ids:
        deploy_id = uuid.uuid4().hex[:8]
        _deployments[deploy_id] = {
            "status": "deploying",
            "app_name": app_id,
            "steps": [],
            "error": None,
            "app_id": app_id,
        }
        tasks.append(_run_update(deploy_id, app_id))

    # Run all app updates in parallel and wait for ALL to finish
    await asyncio.gather(*tasks, return_exceptions=True)


def restore_on_startup():
    reg = _load()
    for app_id, app in reg["apps"].items():
        pid = app.get("pid")
        if pid and _is_alive(pid):
            _pids[app_id] = {"app_pid": pid}
            running_apps[app_id] = pid
            print(f"✅ Restored '{app_id}' (pid {pid})")
        elif app["status"] == "running":
            reg["apps"][app_id]["status"] = "stopped"
    _save(reg)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int | None):
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _detect_type(app_dir: str) -> str:
    pkg = os.path.join(app_dir, "package.json")
    if os.path.exists(pkg):
        with open(pkg) as f:
            deps = json.load(f).get("dependencies", {})
        if "react" in deps or "react-dom" in deps:
            return "react"
    req = os.path.join(app_dir, "requirements.txt")
    if os.path.exists(req):
        content = open(req).read().lower()
        if "fastapi" in content:
            return "fastapi"
        if "flask" in content:
            return "flask"
    return "unknown"


def _find_module(app_dir: str) -> str:
    for name in ["main", "app", "run", "server"]:
        if os.path.exists(os.path.join(app_dir, f"{name}.py")):
            return name
    return "main"


def _launch_app(app: dict) -> subprocess.Popen | None:
    t, d, p = app["type"], app["app_dir"], app["port"]
    env = {**os.environ, "PORT": str(p)}
    # Use the per-app venv Python for isolation (falls back to system python if venv missing)
    venv_python = os.path.join(d, ".venv", "bin", "python")
    py = venv_python if os.path.exists(venv_python) else sys.executable
    if t == "fastapi":
        cmd = [py, "-m", "uvicorn", f"{_find_module(d)}:app", "--host", "0.0.0.0", "--port", str(p)]
    elif t == "flask":
        env["FLASK_APP"] = f"{_find_module(d)}.py"
        cmd = [py, "-m", "flask", "run", "--host", "0.0.0.0", "--port", str(p)]
    elif t == "react":
        build = "build" if os.path.exists(os.path.join(d, "build")) else "dist"
        cmd = ["serve", "-s", build, "-l", str(p)]
    else:
        return None
    log = os.path.join(d, "app.log")
    with open(log, "a") as lf:
        return subprocess.Popen(cmd, cwd=d, stdout=lf, stderr=lf, env=env)


def _start_tunnel_process(app_id: str, port: int) -> subprocess.Popen:
    log = os.path.join(APPS_DIR, app_id, "tunnel.log")
    with open(log, "w") as f:
        return subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=f, stderr=subprocess.STDOUT,
        )


async def _wait_for_tunnel_url(app_id: str, timeout: int = 30) -> str:
    log = os.path.join(APPS_DIR, app_id, "tunnel.log")
    for _ in range(timeout):
        await asyncio.sleep(1)
        try:
            m = _URL_RE.search(open(log).read())
            if m:
                return m.group(0)
        except FileNotFoundError:
            pass
    return ""


async def _setup_tunnel(app_id: str, port: int) -> str:
    """Start cloudflared directly → app port, save URL, return it."""
    await asyncio.sleep(3)
    tunnel_proc = _start_tunnel_process(app_id, port)
    _pids.setdefault(app_id, {})["tunnel_pid"] = tunnel_proc.pid
    url = await _wait_for_tunnel_url(app_id)
    reg = _load()
    if app_id in reg["apps"]:
        reg["apps"][app_id]["tunnel_url"] = url or None
        reg["apps"][app_id]["tunnel_status"] = "active" if url else "dead"
        _save(reg)
    print(f"🌐 Tunnel ready for '{app_id}': {url}")
    return url





async def _run_async(cmd: list[str], cwd: str = None, timeout: int = 120) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "Command failed")
    return stdout.decode()


async def _install_deps(app_dir: str, app_type: str):
    if app_type in ("flask", "fastapi"):
        # Create a per-app isolated venv so packages never touch the global/TinyCell env
        venv_dir = os.path.join(app_dir, ".venv")
        if not os.path.exists(venv_dir):
            await _run_async([sys.executable, "-m", "venv", venv_dir], timeout=60)
        venv_pip = os.path.join(venv_dir, "bin", "pip")
        req = os.path.join(app_dir, "requirements.txt")
        if os.path.exists(req):
            await _run_async(
                [venv_pip, "install", "--no-cache-dir", "-r", "requirements.txt"],
                cwd=app_dir, timeout=600,
            )
    elif app_type == "react":
        # Install serve globally only if not already present
        import shutil as _shutil
        if not _shutil.which("serve"):
            await _run_async(["npm", "install", "-g", "serve"], timeout=120)
        await _run_async(["npm", "install"], cwd=app_dir, timeout=900)
        await _run_async(["npm", "run", "build"], cwd=app_dir, timeout=900)
