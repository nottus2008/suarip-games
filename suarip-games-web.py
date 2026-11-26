#!/usr/bin/env python3
"""
suarip-games-web: a small local web interface for suarip-games. Standard library only.

  ./suarip-games-web.py                                  # http://127.0.0.1:8765
  ./suarip-games-web.py --host 0.0.0.0 --token SECRET    # reachable on your LAN, token required

It imports suarip-games.py from the same folder for drive and config handling, and starts each
rip as a separate suarip-games process, so the web interface and the command line behave the same
way and share one config file. Run it as your normal user, never with sudo.
"""
import argparse
import grp
import hmac
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "suarip-games.py"
WEB = HERE / "web"

spec = importlib.util.spec_from_file_location("suarip_games", SCRIPT)
sg = importlib.util.module_from_spec(spec)
sys.modules["suarip_games"] = sg
spec.loader.exec_module(sg)

TOKEN = None
JOBS = {}              # drive node -> job dict
LOCK = threading.Lock()
MAX_LINES = 3000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROGRESS = re.compile(r"^\|?\s*\[\s*\d+%\]|\bbytes\b.*\bcopied\b")

# (tool, needed for, apt package)
TOOLS = [
    ("redumper", "CD ripping (audio and protected discs)", ""),
    ("ddrescue", "damaged discs", "gddrescue"),
    ("dd", "data discs", ""),
    ("eject", "ejecting finished discs", "eject"),
    ("blkid", "reading disc labels", ""),
    ("udevadm", "detecting discs", ""),
    ("isoinfo", "checking CDs for protection files", "genisoimage"),
    ("lsscsi", "listing drives", "lsscsi"),
    ("bchunk", "converting BIN/CUE to ISO (optional)", "bchunk"),
    ("7z", "testing images (optional)", "p7zip-full"),
    ("notify-send", "desktop popups (optional)", "libnotify-bin"),
]
EDITABLE_STR = {"library", "mount", "scratch", "redumper", "redumper_args", "ntfy_url", "ntfy_topic",
                "ntfy_token", "ntfy_user", "ntfy_password"}
SECRETS = {"ntfy_token", "ntfy_password"}
EVENTS = {"clean", "review", "fallback", "damaged", "check-log", "failed", "skipped", "ready"}


# ---------- helpers ----------

def drives():
    return sg.optical_drives()


def valid_drive(dev):
    if dev not in drives():
        raise ValueError(f"unknown drive: {dev}")
    return dev


def nearest_existing(path):
    p = Path(path).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def resolve_redumper():
    exe = os.path.expanduser(sg.CFG.get("redumper", "")) or shutil.which("redumper")
    return exe if exe and os.path.isfile(exe) and os.access(exe, os.X_OK) else None


def tool_version(path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).strip().splitlines()[0][:80]
    except Exception:
        return ""


# ---------- jobs ----------

def pump(job):
    fd = job["proc"].stdout.fileno()
    buf = ""
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk.decode(errors="replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for line in parts:
            line = ANSI.sub("", line).rstrip()
            if not line:
                continue
            with LOCK:
                if PROGRESS.search(line):
                    job["progress"] = line
                else:
                    job["lines"].append(line)
                    job["n"] += 1
                    del job["lines"][:-MAX_LINES]
    job["exit"] = job["proc"].wait()
    job["ended"] = time.time()


def start_job(dev, opts):
    valid_drive(dev)
    with LOCK:
        old = JOBS.get(dev)
        if old and old["exit"] is None:
            raise ValueError("this drive is already ripping")
    cmd = [sys.executable, str(SCRIPT), "--dev", dev, "--yes"]
    title = str(opts.get("title") or "").strip()[:200]
    if title:
        cmd += ["--title", title]
    if opts.get("disc"):
        cmd += ["--disc", str(int(opts["disc"]))]
    if opts.get("engine") in ("redumper", "builtin"):
        cmd += ["--engine", opts["engine"]]
    if opts.get("force_split"):
        cmd.append("--force-split")
    if opts.get("overwrite"):
        cmd.append("--force")
    if opts.get("damaged"):
        cmd.append("--damaged")
    if opts.get("resume"):
        cmd.append("--resume")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    job = {"proc": proc, "lines": [], "n": 0, "progress": "", "exit": None, "title": title or "(from label)",
           "started": time.time(), "ended": None}
    with LOCK:
        JOBS[dev] = job
    threading.Thread(target=pump, args=(job,), daemon=True).start()
    return job_info(job)


def skip_job(dev):
    valid_drive(dev)
    job = JOBS.get(dev)
    if not job or job["exit"] is not None:
        raise ValueError("nothing is running on this drive")
    os.killpg(job["proc"].pid, signal.SIGINT)  # same as pressing Ctrl+C in a terminal


def job_info(job):
    if not job:
        return None
    end = job["ended"] or time.time()
    return {"running": job["exit"] is None, "exit": job["exit"], "title": job["title"],
            "elapsed": int(end - job["started"])}


def job_log(dev, since):
    job = JOBS.get(dev)
    if not job:
        return {"lines": [], "next": 0, "progress": "", "job": None}
    with LOCK:
        start = job["n"] - len(job["lines"])
        lines = job["lines"][max(0, since - start):]
        return {"lines": lines, "next": job["n"], "progress": job["progress"], "job": job_info(job)}


# ---------- API: status, setup, config ----------

def api_status():
    out = []
    for dev in drives():
        model = sg.drive_model(dev)
        try:
            has_disc = sg.media_present(dev)
        except Exception:
            has_disc = False
        out.append({"dev": dev, "model": model, "profile": sg.drive_args(model)[1], "disc": has_disc,
                    "job": job_info(JOBS.get(dev))})
    return {"drives": out}


def check_tools():
    rows = []
    for name, needed, apt in TOOLS:
        path = resolve_redumper() if name == "redumper" else shutil.which(name)
        rows.append({"name": name, "needed": needed, "apt": apt, "found": bool(path), "path": path or "",
                     "version": tool_version(path) if path and name in ("redumper", "ddrescue") else ""})
    pkgs = [r["apt"] for r in rows if not r["found"] and r["apt"]]
    return {"tools": rows,
            "install_command": ("sudo apt install " + " ".join(pkgs)) if pkgs else "",
            "redumper_url": "https://github.com/superg/redumper/releases"}


def check_permissions():
    checks = []
    try:
        gid = grp.getgrnam("cdrom").gr_gid
        ok = gid in os.getgroups() or os.getgid() == gid
    except KeyError:
        ok = None
    checks.append({"name": "Member of the cdrom group", "ok": ok,
                   "fix": "sudo usermod -aG cdrom $USER   (then log out and back in)"})
    for dev in drives():
        try:
            node = sg.sg_device(dev)
        except Exception:
            node = None
        ok = os.access(dev, os.R_OK) and bool(node) and os.access(node, os.R_OK | os.W_OK)
        checks.append({"name": f"Access to {dev} and {node or 'its /dev/sg node'}", "ok": ok,
                       "fix": "check cdrom group membership and udev rules"})
    if sg.CFG.get("mount"):
        mount = Path(sg.CFG["mount"]).expanduser()
        checks.append({"name": f"{mount} is mounted", "ok": os.path.ismount(mount),
                       "fix": "mount the network share, then reload"})
    for label, key in (("Library folder", "library"), ("Scratch folder", "scratch")):
        path = nearest_existing(sg.CFG[key])
        free = shutil.disk_usage(path).free / 1e9
        writable = os.access(path, os.W_OK)
        checks.append({"name": f"{label} is writable ({free:.0f} GB free)", "ok": writable and free > 12,
                       "fix": "pick a folder you can write to with at least 12 GB free"})
    return {"checks": checks}


def public_config():
    cfg = {k: v for k, v in sg.CFG.items() if not k.startswith("_")}
    for k in SECRETS:
        cfg[k + "_set"] = bool(cfg.get(k))
        cfg[k] = ""
    return cfg


def save_config(update):
    path = sg.CONFIG_PATH
    cur = json.loads(path.read_text()) if path.exists() else dict(sg.DEFAULT_CONFIG)
    for k, v in update.items():
        if k in EDITABLE_STR:
            if not isinstance(v, str):
                raise ValueError(f"{k} must be text")
            if k in SECRETS and not v:
                continue  # blank means keep the stored secret
            cur[k] = v.strip()
        elif k == "prompt_timeout":
            cur[k] = max(0, int(v))
        elif k in ("slim", "desktop_notify"):
            cur[k] = bool(v)
        elif k == "notify_on":
            cur[k] = sorted(e for e in v if e in EVENTS)
        elif k == "drives":
            if not isinstance(v, dict) or not all(isinstance(a, dict) and isinstance(a.get("redumper_args", ""), str)
                                                  for a in v.values()):
                raise ValueError("drives must map a model name to {redumper_args}")
            cur[k] = {str(name): {"redumper_args": a.get("redumper_args", "").strip()} for name, a in v.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2) + "\n")
    tmp.replace(path)
    fresh = sg.load_config()  # read first, so a bad file can never leave the live config empty
    sg.CFG.clear()
    sg.CFG.update(fresh)
    return public_config()


# ---------- API: folder browser, library ----------

def browse(path, files):
    p = Path(path or "~").expanduser()
    try:
        p = p.resolve()
        if not p.is_dir():
            p = p.parent
        entries = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir or files:
                entries.append({"name": e.name, "dir": is_dir})
    except OSError as e:
        return {"path": str(p), "parent": str(p.parent), "entries": [], "error": str(e)}
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "entries": entries[:500]}


def library_summary():
    lib = sg.library()
    games = []
    if lib.is_dir():
        for gj in sorted(lib.glob("*/game.json")):
            try:
                m = json.loads(gj.read_text())
            except Exception:
                games.append({"folder": gj.parent.name, "title": gj.parent.name, "error": "unreadable manifest", "discs": []})
                continue
            keys = ("disc", "engine", "status", "drive", "ripped_at", "fallback_from", "media")
            games.append({"folder": gj.parent.name, "title": m.get("title"), "protection": m.get("protection"),
                          "discs": [{k: d.get(k) for k in keys} for d in m.get("discs", [])]})
    return {"path": str(lib), "games": games}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "suarip-games-web"

    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = (WEB / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.route(url, None)

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 1_000_000)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "bad JSON"}, 400)
        self.route(urlparse(self.path), data)

    def route(self, url, data):
        if not url.path.startswith("/api/"):
            return self.send_json({"error": "not found"}, 404)
        if not self.authorized():
            return self.send_json({"error": "token required"}, 401)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            self.send_json(self.dispatch(url.path, q, data))
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # keep the server alive whatever a request does
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def dispatch(self, path, q, data):
        if data is None:  # GET
            if path == "/api/status":
                return api_status()
            if path == "/api/setup":
                return {**check_tools(), **check_permissions(), "config": public_config(),
                        "config_path": str(sg.CONFIG_PATH), "version": sg.VERSION}
            if path == "/api/browse":
                return browse(q.get("path", ""), q.get("files") == "1")
            if path == "/api/library":
                return library_summary()
            if path == "/api/log":
                return job_log(valid_drive(q.get("dev", "")), int(q.get("since", 0) or 0))
            raise ValueError("unknown endpoint")
        if path == "/api/config":
            return save_config(data)
        if path == "/api/inspect":
            dev = valid_drive(data.get("dev", ""))
            job = JOBS.get(dev)
            if job and job["exit"] is None:
                raise ValueError("this drive is busy ripping")
            return sg.inspect_disc(dev)
        if path == "/api/start":
            return start_job(data.get("dev", ""), data)
        if path == "/api/skip":
            skip_job(data.get("dev", ""))
            return {"ok": True}
        if path == "/api/eject":
            dev = valid_drive(data.get("dev", ""))
            job = JOBS.get(dev)
            if job and job["exit"] is None:
                raise ValueError("this drive is busy ripping")
            sg.run(["eject", dev])
            return {"ok": True}
        raise ValueError("unknown endpoint")


def main():
    ap = argparse.ArgumentParser(description="Local web interface for suarip-games")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", help="require this token (or set SUARIP_WEB_TOKEN); mandatory for non-local hosts")
    args = ap.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("Run this as your normal user, not root.")
    global TOKEN
    TOKEN = args.token or os.environ.get("SUARIP_WEB_TOKEN")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        sys.exit("Listening beyond this machine needs --token. This page can write files, run tools and eject drives.")

    sg.CFG.update(sg.load_config())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"suarip-games-web {sg.VERSION}: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        running = [d for d, j in JOBS.items() if j["exit"] is None]
        if running:
            print(f"\nNote: rips on {', '.join(running)} keep running in the background.")


if __name__ == "__main__":
    main()
