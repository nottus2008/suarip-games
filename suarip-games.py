#!/usr/bin/env python3
"""
suarip-games 1.0: drop a game disc in, get a verified image + manifest on the NAS.

One config file holds everything (created on first run):
  ~/.config/suarip-games/config.json
Environment variables (SUARIP_NAS, SUARIP_MOUNT, SUARIP_SCRATCH, SUARIP_REDUMPER,
SUARIP_REDUMPER_ARGS, SUARIP_NTFY_URL/TOPIC/TOKEN, SUARIP_CONFIG) still override it.

Notifications: set "ntfy_url" (and "ntfy_topic", plus a token or user/password if the server
needs one) in the config, then run `suarip-games --test-notify`. "notify_on" lists the events
that send a message: clean, review, fallback, damaged, check-log, failed, skipped, ready.

On each disc it shows a summary (drive, disc, title, engine) and starts by itself after
`prompt_timeout` seconds, or you can edit title / disc number / engine first.
Use --yes to skip the prompt entirely.

Engines:
  redumper  byte-perfect dumper from the Redump project; BIN/CUE for CDs, ISO for DVDs
  builtin   dd / ddrescue, data discs only (--mode simple | rescue | auto)
  auto      CD -> redumper, DVD/BD -> builtin  (default)

Per-drive redumper settings live in config "drives", matched by a substring of the
drive's "VENDOR MODEL" text (run --list-drives to see it).

Layout on the NAS:
  <library>/<game-slug>/game.json
  <library>/<game-slug>/discs/<slug>-disc1/   (image files, logs, mapfile)

Requires: blockdev, blkid, udevadm, eject; ddrescue (builtin rescue); redumper (CDs).
Your user needs read access to the drive's /dev/srN and /dev/sgN (usually group 'cdrom').
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0"
SECTOR = 2048
Q_MINOR_FRACTION = 0.01   # Q errors below 1% of sectors are treated as normal subchannel noise
CONFIG_PATH = Path(os.environ.get("SUARIP_CONFIG", Path.home() / ".config" / "suarip-games" / "config.json"))
DEFAULT_CONFIG = {
    "_help": "drives: key is a substring of 'VENDOR MODEL' from --list-drives, e.g. "
             "\"GP40LB10\": {\"redumper_args\": \"--drive-type=GENERIC --drive-sector-order=DATA_SUB_C2 --speed=8\"}",
    "library": "~/suarip-games/library",
    "mount": "",
    "scratch": "~/suarip-games/scratch",
    "redumper": "",
    "redumper_command": "disc",
    "redumper_args": "",
    "prompt_timeout": 30,
    "slim": False,
    "give_up": "10m",
    "ntfy_url": "",
    "ntfy_topic": "suarip-games",
    "ntfy_token": "",
    "ntfy_user": "",
    "ntfy_password": "",
    "notify_on": ["clean", "review", "fallback", "damaged", "check-log", "failed", "skipped"],
    "desktop_notify": True,
    "drives": {},
}
ENV_OVERRIDES = {"SUARIP_NAS": "library", "SUARIP_MOUNT": "mount", "SUARIP_SCRATCH": "scratch",
                 "SUARIP_REDUMPER": "redumper", "SUARIP_REDUMPER_ARGS": "redumper_args",
                 "SUARIP_NTFY_URL": "ntfy_url", "SUARIP_NTFY_TOPIC": "ntfy_topic",
                 "SUARIP_NTFY_TOKEN": "ntfy_token"}

CFG = dict(DEFAULT_CONFIG)

HASH_EXT = {".iso", ".bin", ".cue"}                      # files we hash and record
RAW_EXT = {".scram", ".scrap", ".state", ".subcode"}     # big redumper intermediates (--slim drops them)
DISC_RE = re.compile(r"[\s_\-]*(?:dis[ck]|cd|dvd)[\s_\-]*(\d+)\s*$", re.I)
# matches both the final summary ("SCSI: 0 samples / C2: 12 samples / Q: 3" on separate lines)
# and the progress line ("errors: { SCSIs: 0, C2s: 12, Q: 3 }")
ERR_RE = re.compile(r"SCSIs?:\s*(\d+)\D*?C2s?:\s*(\d+)\D*?Q:\s*(\d+)", re.I | re.S)


class RipError(Exception):
    pass


def log(msg):
    print(f"[suarip-games] {msg}", flush=True)


def run(cmd):
    return subprocess.run(cmd, text=True, capture_output=True)


def run_child(cmd):
    """Run a long tool (dd, ddrescue, redumper) and return its exit code.

    Ctrl+C reaches the child too. Give it a few seconds to stop cleanly (ddrescue saves its
    mapfile, redumper saves its state) before killing it, then re-raise for the caller.
    """
    p = subprocess.Popen(cmd)
    try:
        return p.wait()
    except KeyboardInterrupt:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        raise


def need(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        raise RipError(f"missing tools: {', '.join(missing)}")


# ---------- notifications (ntfy, terminal bell, desktop popup) ----------

CURRENT = {}  # what the current rip is about, so failures can say which disc
NOTIFY_PRIORITY = {"damaged": "high", "check-log": "high", "failed": "high",
                   "skipped": "low", "ready": "low"}
NOTIFY_TAGS = {"clean": "white_check_mark", "review": "warning", "fallback": "arrows_counterclockwise",
               "damaged": "x", "check-log": "mag", "failed": "rotating_light",
               "skipped": "fast_forward", "ready": "cd", "test": "bell"}


def send_ntfy(title, message, priority="default", tags=""):
    """POST one message to the configured ntfy topic. Raises on any error."""
    url = CFG["ntfy_url"].rstrip("/")
    topic = CFG["ntfy_topic"].strip("/")
    if not url or not topic:
        raise ValueError("ntfy_url / ntfy_topic are not set in the config")
    req = urllib.request.Request(f"{url}/{topic}", data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title.encode("ascii", "replace").decode())  # header must be ASCII
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    if CFG["ntfy_token"]:
        req.add_header("Authorization", "Bearer " + CFG["ntfy_token"])
    elif CFG["ntfy_user"]:
        cred = base64.b64encode(f"{CFG['ntfy_user']}:{CFG['ntfy_password']}".encode()).decode()
        req.add_header("Authorization", "Basic " + cred)
    urllib.request.urlopen(req, timeout=10).close()


def notify(event, title, message):
    """Tell the user something happened, if this event is enabled. Never raises."""
    if event not in CFG["notify_on"]:
        return
    try:
        print("\a", end="", flush=True)  # terminal bell
        if CFG["desktop_notify"] and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], timeout=5, capture_output=True)
    except Exception:
        pass
    if not CFG["ntfy_url"]:
        return
    try:
        send_ntfy(title, message, NOTIFY_PRIORITY.get(event, "default"), NOTIFY_TAGS.get(event, ""))
    except Exception as e:  # a dead notification server must never break a rip
        log(f"WARNING: ntfy notification failed: {e}")


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


# ---------- config ----------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        log(f"created default config: {CONFIG_PATH}")
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def library():
    return Path(CFG["library"]).expanduser()


def nas_mount():
    return Path(CFG["mount"]).expanduser()


def scratch():
    return Path(CFG["scratch"]).expanduser()


# ---------- disc and drive inspection ----------

def udev_props(dev):
    r = run(["udevadm", "info", "--query=property", f"--name={dev}"])
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


def media_present(dev):
    return udev_props(dev).get("ID_CDROM_MEDIA") == "1"


def optical_drives():
    return sorted(f"/dev/{p.name}" for p in Path("/sys/class/block").glob("sr*"))


def find_disc_drive():
    for dev in optical_drives():
        if media_present(dev):
            return dev
    return None


def wait_for_disc(dev, present=True):
    while media_present(dev) != present:
        time.sleep(2)


def settle():
    time.sleep(3)  # let the drive spin up and udev settle
    run(["udevadm", "settle"])


def drive_model(dev):
    """'HL-DT-ST DVDRAM GP40LB10' style text from sysfs (stable across reboots, unlike /dev/srN)."""
    base = Path("/sys/class/block") / Path(dev).resolve().name / "device"

    def rd(name):
        try:
            return (base / name).read_text().strip()
        except OSError:
            return ""
    return f"{rd('vendor')} {rd('model')}".strip() or "unknown drive"


def drive_args(model):
    """(redumper args, matched profile key) for this drive."""
    for key, prof in CFG["drives"].items():
        if key.lower() in model.lower():
            return prof.get("redumper_args", ""), key
    return CFG["redumper_args"], None


def disc_kind(props):
    for kind in ("BD", "DVD", "CD"):
        if any(k.startswith(f"ID_CDROM_MEDIA_{kind}") and v == "1" for k, v in props.items()):
            return kind
    return "unknown"


def volume_label(dev, props):
    r = run(["blkid", "-s", "LABEL", "-o", "value", dev])
    return r.stdout.strip() or props.get("ID_FS_LABEL", "").strip() or "UNKNOWN_DISC"


def parse_label(label):
    """'FSX_DISC2' -> ('FSX', 2). No disc token -> (label, 1)."""
    m = DISC_RE.search(label)
    if m and label[: m.start()].strip(" _-"):
        return label[: m.start()].strip(" _-"), int(m.group(1))
    return label, 1


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def sg_device(dev):
    """/dev/sr0 -> /dev/sg1. redumper needs the SCSI generic node, not /dev/srN."""
    name = Path(dev).resolve().name
    d = Path("/sys/class/block") / name / "device" / "scsi_generic"
    if d.is_dir():
        for p in d.iterdir():
            return f"/dev/{p.name}"
    raise RipError(f"could not find the /dev/sg* node for {dev}")


def list_drives():
    for dev in optical_drives():
        model = drive_model(dev)
        extra, profile = drive_args(model)
        try:
            sg = sg_device(dev)
        except RipError:
            sg = "?"
        print(f"{dev}  {sg}  {model!r}  disc: {'yes' if media_present(dev) else 'no'}  "
              f"profile: {profile or '-'}  redumper args: {extra or '-'}")
    print(f"config file: {CONFIG_PATH}")


# ---------- disc analysis (decides which engine to try first) ----------

# File names that usually mean copy protection. A heuristic only: it picks the first method to
# try, and redumper's own protection scan stays the authority whenever it runs.
PROTECTION_MARKERS = {
    "SafeDisc": {"00000001.TMP", "CLCD16.DLL", "CLCD32.DLL", "CLOKSPL.EXE", "DPLAYERX.DLL"},
    "SecuROM": {"CMS16.DLL", "CMS_95.DLL", "CMS_NT.DLL", "SINTF16.DLL", "SINTF32.DLL", "SINTFNT.DLL"},
    "StarForce": {"PROTECT.DLL", "PROTECT.EXE"},
}


def disc_filenames(dev):
    """Upper-case file names on the disc, or None if they can't be listed (needs isoinfo)."""
    if not shutil.which("isoinfo"):
        return None
    try:
        r = subprocess.run(["isoinfo", "-f", "-J", "-i", dev], capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return {re.sub(r";\d+$", "", Path(line.strip()).name).upper() for line in r.stdout.splitlines()}


def protection_markers(names):
    return sorted(scheme for scheme, files in PROTECTION_MARKERS.items() if names & files)


def choose_engine(kind, audio, dev):
    """First engine to try. Returns (engine, reason, protection markers found)."""
    if kind != "CD":
        return "builtin", f"{kind} data disc: no drive-specific tricks needed", []
    if audio:
        return "redumper", f"{audio} audio track(s): dd would lose them", []
    names = disc_filenames(dev)
    if names is None:
        return "redumper", "could not list the disc's files to check for protection", []
    marks = protection_markers(names)
    if marks:
        return "redumper", "possible copy protection: " + ", ".join(marks), marks
    return "builtin", "data-only CD, no audio tracks, no protection markers found", []


def park_scratch(work, name):
    """Move leftover scratch files aside instead of deleting them (a failed dump can be reused)."""
    if work.exists() and any(work.iterdir()):
        parked = scratch() / "_failed" / f"{name}-{int(time.time())}"
        parked.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(parked))
        log(f"previous scratch kept at {parked} (delete it when you no longer need it)")
    work.mkdir(parents=True, exist_ok=True)


def inspect_disc(dev):
    """What is in this drive, and what would the script do with it? (used by the web interface)"""
    model = drive_model(dev)
    out = {"dev": dev, "model": model, "profile": drive_args(model)[1], "disc": False}
    props = udev_props(dev)
    if props.get("ID_CDROM_MEDIA") != "1":
        return out
    kind = disc_kind(props)
    label = volume_label(dev, props)
    audio = int(props.get("ID_CDROM_MEDIA_TRACK_COUNT_AUDIO", "0") or 0)
    title, disc_number = parse_label(label)
    engine, why, marks = choose_engine(kind, audio, dev)
    out.update(disc=True, media=kind, label=label, audio=audio, title=title, disc_number=disc_number,
               engine=engine, why=why, protection_markers=marks)
    return out


def restore_partial(work, name):
    """For --resume: bring back the partial image and mapfile of an earlier attempt, if there is one."""
    def usable(d):
        return (d / f"{name}.map").exists() and (d / f"{name}.iso").exists()
    if usable(work):
        return True
    parked = sorted((scratch() / "_failed").glob(f"{name}-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in parked:
        if usable(d):
            park_scratch(work, name)  # moves anything else aside and recreates an empty work folder
            work.rmdir()
            shutil.move(str(d), str(work))
            log(f"resuming from the earlier attempt in {d.name}")
            return True
    return False


# ---------- interactive summary with timeout ----------

def read_line(timeout):
    """Read a line from stdin; None if the timeout expires first."""
    if timeout is not None:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
    return sys.stdin.readline()


def confirm_settings(info, timeout):
    """Show the plan; start on Enter or timeout. Returns False if the user skips the disc."""
    t = timeout
    while True:
        print()
        print(f"  Drive  : {info['drive']}")
        print(f"  Disc   : {info['media']}, label '{info['label']}', {info['audio']} audio track(s)")
        print(f"  Title  : {info['title']}")
        print(f"  Disc # : {info['disc']}")
        print(f"  Engine : {info['engine']}" + (f"   (drive profile: {info['profile']})" if info["profile"] else ""))
        if info.get("why"):
            print(f"  Why    : {info['why']}")
        hint = f"auto-start in {t}s" if t else "no timeout"
        print(f"  [Enter] start  [t] title  [d] disc #  [e] engine  [s] skip disc   ({hint})")
        print("> ", end="", flush=True)
        line = read_line(t)
        if line is None:
            print("(timeout: using defaults)")
            return True
        t = None  # once the user interacts, stop the countdown
        c = line.strip().lower()
        if c == "":
            return True
        if c == "s":
            return False
        if c == "t":
            print("Title: ", end="", flush=True)
            v = (read_line(None) or "").strip()
            info["title"] = v or info["title"]
        elif c == "d":
            print("Disc number: ", end="", flush=True)
            v = (read_line(None) or "").strip()
            if v.isdigit() and int(v) > 0:
                info["disc"] = int(v)
        elif c == "e":
            print("Engine (redumper/builtin): ", end="", flush=True)
            v = (read_line(None) or "").strip().lower()
            if v in ("redumper", "builtin"):
                info["engine"] = v


# ---------- engine: builtin (dd / ddrescue) ----------

def dd_copy(dev, out, size):
    log("builtin rip: dd...")
    cmd = ["dd", f"if={dev}", f"of={out}", f"bs={SECTOR}",
           f"count={size // SECTOR}", "status=progress"]
    return run_child(cmd) == 0


def valid_interval(text):
    """ddrescue time interval such as 90s, 10m, 2h (a bare number means seconds)."""
    return bool(re.fullmatch(r"\d+[smhd]?", str(text or "")))


def ddrescue_copy(dev, out, mapfile, retries, give_up=""):
    """Two ddrescue passes. give_up stops a pass after that long with no successful read,
    so a bad disc can never tie up a drive for hours."""
    base = ["ddrescue", "-b", str(SECTOR), "-d"]
    if valid_interval(give_up):
        base += ["-T", str(give_up)]
    log("builtin rip: ddrescue pass 1 (skip bad areas)...")
    run_child(base + ["-n", dev, str(out), str(mapfile)])
    log(f"builtin rip: ddrescue pass 2 (retry bad areas x{retries})...")
    run_child(base + ["-r", str(retries), dev, str(out), str(mapfile)])


def mapfile_stats(mapfile):
    """Return (bad_bytes, total_bytes) from a ddrescue mapfile."""
    bad = total = 0
    seen_header = False
    for line in Path(mapfile).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not seen_header:  # first data line is "current_pos status pass"
            seen_header = True
            continue
        _, size, status = line.split()[:3]
        size = int(size, 0)
        total += size
        if status != "+":
            bad += size
    return bad, total


def rip_builtin(args, dev, work, name, audio):
    need("dd", "blockdev")
    problem = bool(getattr(args, "damaged", False) or getattr(args, "resume", False))
    mode = "rescue" if problem else args.mode  # problem discs skip the plain dd attempt
    if mode in ("rescue", "auto"):
        need("ddrescue")
    if audio:
        log(f"WARNING: {audio} audio track(s); an ISO only holds the data track. "
            "Use the redumper engine for this disc.")
    size = int(run(["blockdev", "--getsize64", dev]).stdout.strip() or 0)
    if size <= 0:
        raise RipError("could not read disc size")

    iso, mapfile = work / f"{name}.iso", work / f"{name}.map"
    retries = args.retries or 3
    give_up = getattr(args, "give_up", None) or CFG["give_up"]
    used = {"simple": "dd", "rescue": "ddrescue"}.get(mode)
    if mode == "simple":
        if not dd_copy(dev, iso, size):
            raise RipError("dd hit a read error; retry with --mode rescue or --damaged")
    elif mode == "rescue":
        ddrescue_copy(dev, iso, mapfile, retries, give_up)
    else:  # auto
        if dd_copy(dev, iso, size):
            used = "dd"
        else:
            log("dd failed, falling back to ddrescue")
            iso.unlink(missing_ok=True)
            ddrescue_copy(dev, iso, mapfile, retries, give_up)
            used = "ddrescue"

    bad = mapfile_stats(mapfile)[0] if used == "ddrescue" and mapfile.exists() else 0
    if used == "ddrescue" and iso.exists() and iso.stat().st_size < size:
        with open(iso, "r+b") as f:  # an unread tail leaves the image short; pad it (the mapfile records the gap)
            f.truncate(size)
    if not iso.exists() or iso.stat().st_size != size:
        raise RipError("image size does not match disc size")
    out = {"engine": used, "primary": iso.name, "size_bytes": size,
           "status": "clean" if bad == 0 else "damaged", "unrecovered_bytes": bad}
    if problem:
        out["problem_disc_mode"] = True
    if getattr(args, "resume", False):
        out["resumed"] = True
    return out


# ---------- engine: redumper ----------

def redumper_errors(logfile):
    """Last SCSI/C2/Q error summary in the redumper log, or None."""
    if not Path(logfile).exists():
        return None
    found = None
    for m in ERR_RE.finditer(Path(logfile).read_text(errors="replace")):
        found = {"scsi": int(m[1]), "c2": int(m[2]), "q": int(m[3])}
    return found


def parse_protection(logfile):
    """The last 'protection: ...' line in the redumper log, or None."""
    if not Path(logfile).exists():
        return None
    found = None
    for m in re.finditer(r"^protection:\s*(.+?)\s*$", Path(logfile).read_text(errors="replace"), re.I | re.M):
        found = m[1]
    return found


def rip_redumper(args, dev, work, name, extra_args):
    exe = os.path.expanduser(CFG["redumper"]) or shutil.which("redumper")
    if not exe:
        raise RipError("redumper not found: set 'redumper' in the config or put it on PATH "
                       "(note: sudo drops PATH); or use the builtin engine")
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        raise RipError(f"redumper path '{exe}' is not an executable file "
                       "(is it a folder, misspelled, or missing chmod +x?)")
    sg = sg_device(dev)
    retries = args.retries or 20
    log(f"redumper rip on {sg}...")
    cmd = [exe, CFG["redumper_command"], f"--drive={sg}", f"--image-path={work}",
           f"--image-name={name}", f"--retries={retries}", "--overwrite"]
    cmd += shlex.split(extra_args)
    logfile = work / f"{name}.log"
    forced = False
    if run_child(cmd) != 0:
        split_failed = logfile.exists() and "data errors detected" in logfile.read_text(errors="replace")
        have_dump = (work / f"{name}.state").exists() or (work / f"{name}.scram").exists()
        if getattr(args, "force_split", False) and (split_failed or have_dump):
            # the raw dump is fine; redo the stages after it, forcing the track split
            log("track split found errors; forcing the split from the existing dump...")
            common = [f"--image-path={work}", f"--image-name={name}"]
            for stage, fatal in (("split", True), ("hash", True), ("info", False)):
                step = [exe, stage, *common] + (["--force"] if stage == "split" else [])
                if run_child(step) != 0:
                    if fatal:
                        raise RipError(f"redumper {stage} failed during the forced split; scratch kept at {work}")
                    log(f"WARNING: redumper {stage} failed; continuing")
            forced = True
        else:
            hint = ""
            if split_failed:
                hint = ("; the track split found errors. If they are only a few edge samples "
                        "(read the log), rerun with --force-split")
            raise RipError(f"redumper failed{hint}; scratch kept at {work}")

    primary = next((f"{name}{ext}" for ext in (".cue", ".iso") if (work / f"{name}{ext}").exists()), None)
    if not primary:
        raise RipError(f"redumper produced no .cue or .iso; scratch kept at {work}")

    errs = redumper_errors(work / f"{name}.log")
    # sectors in the finished image, to judge how big the subchannel (Q) error count really is
    bins = sum(f.stat().st_size for f in work.glob("*.bin"))
    sectors = bins // 2352 if bins else (work / f"{name}.iso").stat().st_size // SECTOR if (work / f"{name}.iso").exists() else 0
    if errs is not None:
        errs["sectors"] = sectors
    if errs is None:
        status = "check-log"   # couldn't parse the log; read it yourself
    elif errs["scsi"] > 0:
        status = "damaged"     # unreadable sectors
    elif errs["c2"] > 0:
        status = "review"      # drive-reported C2 errors: confirm with a second drive
    elif errs["q"] > 0 and (not sectors or errs["q"] / sectors > Q_MINOR_FRACTION):
        status = "review"      # many subchannel errors: the sector order may be wrong
    else:
        status = "clean"       # nothing unreadable; a few Q errors are normal (redumper repairs them)
    if forced:
        out_forced = True
        if status == "clean":
            status = "review"
    else:
        out_forced = False
    out = {"engine": "redumper", "primary": primary, "status": status}
    if out_forced:
        out["forced_split"] = True
    prot = parse_protection(work / f"{name}.log")
    if prot:
        out["protection_scan"] = prot
    if errs is not None:
        out["errors"] = errs
    return out


# ---------- hashing, copying, manifest ----------

def hash_file(path):
    crc, md5, sha1 = 0, hashlib.md5(), hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
            md5.update(chunk)
            sha1.update(chunk)
    return {"crc32": f"{crc & 0xFFFFFFFF:08x}", "md5": md5.hexdigest(), "sha1": sha1.hexdigest()}


def publish(work, dest, slim):
    """Copy a finished rip to the NAS and verify every copied file."""
    dest.mkdir(parents=True, exist_ok=True)
    records = []
    for src in sorted(p for p in work.iterdir() if p.is_file()):
        if slim and src.suffix.lower() in RAW_EXT:
            continue
        dst = dest / src.name
        shutil.copy2(src, dst)
        if src.stat().st_size != dst.stat().st_size:
            raise RipError(f"size mismatch copying {src.name}; scratch kept at {work}")
        rec = {"path": src.name, "size": src.stat().st_size}
        if src.suffix.lower() in HASH_EXT:
            h = hash_file(src)
            if hash_file(dst)["sha1"] != h["sha1"]:
                raise RipError(f"hash mismatch copying {src.name}; scratch kept at {work}")
            rec.update(h)
        records.append(rec)
    return records


def update_manifest(game_dir, title, platform, disc_entry, protection=None):
    """Add or replace one disc in game.json. Locked, so rips finishing together (different drives,
    or the web interface) cannot overwrite each other, and written via a temp file so a crash or
    a network hiccup can never leave a half-written manifest."""
    game_dir.mkdir(parents=True, exist_ok=True)
    mpath = game_dir / "game.json"
    with open(game_dir / ".game.json.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            pass  # some network filesystems refuse locks; carry on without one
        if mpath.exists():
            m = json.loads(mpath.read_text())
        else:
            m = {
                "title": title,
                "platform": platform,
                "discs": [],
                "install": {"merge_discs": False, "wine": {"arch": "", "deps": []}},
                "protection": "unknown",
                "run": "",
                "notes": "",
            }
        m["discs"] = sorted(
            [d for d in m["discs"] if d["disc"] != disc_entry["disc"]] + [disc_entry],
            key=lambda d: d["disc"],
        )
        m["install"]["merge_discs"] = len(m["discs"]) > 1
        if protection and m.get("protection") in (None, "", "unknown"):
            m["protection"] = protection
        tmp = mpath.with_name("game.json.tmp")
        tmp.write_text(json.dumps(m, indent=2) + "\n")
        os.replace(tmp, mpath)
    return mpath


# ---------- one disc, start to finish ----------

def rip_one(args, dev):
    need("blkid", "udevadm", "eject")

    if CFG["mount"]:  # library lives on a network share: refuse to run if it is not mounted
        mount = nas_mount()
        mount.exists()  # touching the path triggers a systemd automount
        if not os.path.ismount(mount):
            raise RipError(f"{mount} is not mounted; check the share")

    props = udev_props(dev)
    if props.get("ID_CDROM_MEDIA") != "1":
        raise RipError(f"no disc in {dev}")

    run(["umount", dev])  # redumper and dd both want it unmounted
    kind = disc_kind(props)
    label = volume_label(dev, props)
    audio = int(props.get("ID_CDROM_MEDIA_TRACK_COUNT_AUDIO", "0") or 0)
    base_title, guessed_disc = parse_label(label)
    model = drive_model(dev)
    extra_args, profile = drive_args(model)
    CURRENT.clear()
    CURRENT.update(drive=model, title=base_title)

    auto_engine = args.engine == "auto"
    marks = []
    if auto_engine:
        engine, why, marks = choose_engine(kind, audio, dev)
    else:
        engine, why = args.engine, "chosen with --engine"
    if (getattr(args, "damaged", False) or getattr(args, "resume", False)) and auto_engine:
        if audio:
            log(f"NOTE: {audio} audio track(s), so redumper stays in charge; "
                "--damaged/--resume only change the dd/ddrescue path.")
        else:
            engine, why = "builtin", "problem-disc mode: ddrescue with a give-up timer"
    info = {"drive": f"{dev}  {model}", "media": kind, "label": label, "audio": audio,
            "title": args.title or base_title, "disc": args.disc or guessed_disc,
            "engine": engine, "profile": profile, "why": why}

    if not args.yes and sys.stdin.isatty():
        if not confirm_settings(info, CFG["prompt_timeout"]):
            raise RipError("skipped by user")
    title, disc_no, engine = info["title"], info["disc"], info["engine"]
    CURRENT["title"] = f"{title} disc {disc_no}"
    slug = slugify(title)
    name = f"{slug}-disc{disc_no}"
    log(f"ripping '{title}' disc {disc_no} with {engine}")

    game_dir = library() / slug
    dest = game_dir / "discs" / name
    if dest.exists() and not args.force:
        raise RipError(f"{dest} already exists (use --force to overwrite)")

    work = scratch() / name
    if getattr(args, "resume", False) and engine == "builtin" and restore_partial(work, name):
        pass  # keep the earlier image and mapfile; ddrescue continues where it stopped
    else:
        park_scratch(work, name)

    started = time.time()
    fallback = {}
    if engine == "redumper":
        try:
            result = rip_redumper(args, dev, work, name, extra_args)
        except RipError as e:
            # data-only discs can still be saved as a plain ISO; audio discs can't
            if not auto_engine or audio or args.no_fallback or not shutil.which("dd"):
                raise
            log(f"redumper failed ({e}); data-only disc, falling back to dd/ddrescue")
            scan = parse_protection(work / f"{name}.log")
            fallback = {"fallback_from": "redumper", "redumper_error": str(e)[:300]}
            if scan:
                fallback["protection_scan"] = scan
            park_scratch(work, name)
            engine = "builtin"
            result = rip_builtin(args, dev, work, name, audio)
            result.update(fallback)
    else:
        result = rip_builtin(args, dev, work, name, audio)

    log(f"copying to NAS and verifying: {dest}")
    if dest.exists():
        shutil.rmtree(dest)
    try:
        files = publish(work, dest, args.slim or CFG["slim"])
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # no half-copied rip on the NAS
        raise

    primary = result.pop("primary")
    entry = {
        "disc": disc_no,
        "dir": f"discs/{name}",
        "primary": f"discs/{name}/{primary}",
        "volume_label": label,
        "media": kind,
        "drive": model,
        **result,
        "files": files,
        "ripped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rip_seconds": int(time.time() - started),
        "tool": f"suarip-games {VERSION}",
    }
    if engine == "redumper":
        entry["redumper_args"] = extra_args
    if marks:
        entry["protection_markers"] = marks
    prot = entry.get("protection_scan") or (("suspected: " + ", ".join(marks)) if marks else None)
    mpath = update_manifest(game_dir, title, args.platform, entry, protection=prot)

    shutil.rmtree(work, ignore_errors=True)
    log(f"done: {name} [{entry['status']}] via {entry['engine']}")
    log(f"manifest: {mpath}")
    event = "fallback" if fallback else entry["status"]
    msg = (f"{title} disc {disc_no}: {entry['status']} via {entry['engine']} "
           f"in {fmt_duration(entry['rip_seconds'])} ({model})")
    if fallback:
        msg += "\nredumper failed, saved as a plain ISO"
    if prot:
        msg += f"\nProtection: {prot}"
    notify(event, f"SuaRip Games: {title}", msg)
    if entry["status"] == "damaged":
        log("WARNING: errors reported. Check the log/mapfile; consider re-ripping in another drive.")
    if entry["status"] == "review":
        log("NOTE: no unreadable sectors, but C2/Q errors were reported. This can be a drive "
            "limitation; confirm by ripping in a second drive and comparing hashes.")
    if fallback:
        log("NOTE: saved as a plain ISO after redumper failed. The raw redumper dump is in "
            f"{scratch() / '_failed'} if you want to retry it on a better drive.")
    if entry["status"] == "check-log":
        log("NOTE: could not read an error summary from the redumper log; read it before trusting this rip.")


def pick_dev(args, wait):
    """Return the drive to use, optionally waiting for a disc to appear."""
    while True:
        dev = args.dev if args.dev != "auto" else find_disc_drive()
        if dev and media_present(dev):
            settle()
            return dev
        if not wait:
            raise RipError("no disc found in " + (args.dev if args.dev != "auto" else "any optical drive"))
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="Rip game discs to images + manifest on the NAS")
    ap.add_argument("--dev", default="auto", help="drive, e.g. /dev/sr1 (default: whichever drive has a disc)")
    ap.add_argument("--engine", choices=["auto", "redumper", "builtin"], default="auto")
    ap.add_argument("--mode", choices=["simple", "rescue", "auto"], default="auto",
                    help="builtin engine only: dd, ddrescue, or dd with ddrescue fallback")
    ap.add_argument("--title", help="game title (default: from volume label)")
    ap.add_argument("--disc", type=int, help="disc number (default: guessed from label)")
    ap.add_argument("--platform", default="pc")
    ap.add_argument("--retries", type=int, help="retry count (default 3 builtin, 20 redumper)")
    ap.add_argument("--slim", action="store_true",
                    help="don't copy redumper's large raw intermediates (.scram/.state/...)")
    ap.add_argument("--force-split", action="store_true",
                    help="redumper: write tracks even if the split found errors (status becomes 'review')")
    ap.add_argument("--damaged", action="store_true",
                    help="problem disc: go straight to ddrescue, give up after --give-up with no good reads, "
                         "and keep whatever was recovered (marked damaged)")
    ap.add_argument("--resume", action="store_true",
                    help="continue an earlier partial rip of this disc (image + mapfile), e.g. in a different drive")
    ap.add_argument("--give-up", metavar="TIME",
                    help="ddrescue gives up after this long with no successful read, e.g. 10m (config: give_up)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="don't fall back to dd/ddrescue when redumper fails on a data-only CD")
    ap.add_argument("--yes", action="store_true", help="skip the summary prompt, use defaults")
    ap.add_argument("--force", action="store_true", help="overwrite an existing rip")
    ap.add_argument("--no-eject", action="store_true")
    ap.add_argument("--watch", action="store_true", help="loop forever: rip each disc as it is inserted")
    ap.add_argument("--list-drives", action="store_true", help="show detected drives and profiles, then exit")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification, then exit")
    ap.add_argument("--version", action="version", version=f"suarip-games {VERSION}")
    args = ap.parse_args()
    if args.give_up and not valid_interval(args.give_up):
        ap.error("--give-up needs a time such as 90s, 10m or 2h")

    CFG.update(load_config())
    if args.test_notify:
        try:
            send_ntfy("SuaRip Games test", f"suarip-games {VERSION}: notifications work", "default", "bell")
            print(f"sent to {CFG['ntfy_url'].rstrip('/')}/{CFG['ntfy_topic']}")
        except Exception as e:
            print(f"ntfy test failed: {e}")
            sys.exit(1)
        return
    if args.list_drives:
        list_drives()
        return

    last_interrupt = 0.0
    while True:
        dev = None
        try:
            if args.watch:
                log("waiting for a disc... (Ctrl+C quits)")
            dev = pick_dev(args, wait=args.watch)  # Ctrl+C while waiting quits
            try:
                rip_one(args, dev)
            except KeyboardInterrupt:
                if time.time() - last_interrupt < 3:
                    raise  # second Ctrl+C within 3 seconds: really quit
                last_interrupt = time.time()
                log("SKIPPED this disc (Ctrl+C again within 3 seconds to quit). Any partial dump "
                    f"is in {scratch()} and will be moved to _failed/ on the next rip of this disc.")
                notify("skipped", "SuaRip Games: disc skipped", f"{CURRENT.get('title', 'disc')} was skipped")
        except RipError as e:
            log(f"ERROR: {e}")
            if str(e) == "skipped by user":
                notify("skipped", "SuaRip Games: disc skipped", f"{CURRENT.get('title', 'disc')} was skipped")
            else:
                notify("failed", "SuaRip Games: rip failed",
                       f"{CURRENT.get('title', 'disc')} ({CURRENT.get('drive', dev or 'drive')}): {e}")
            if not args.watch:
                sys.exit(1)
        finally:
            if dev and not args.no_eject:
                run(["eject", dev])
        if not args.watch:
            break
        if dev:
            wait_for_disc(dev, present=False)
            notify("ready", "SuaRip Games: load the next disc",
                   f"{CURRENT.get('drive', dev)} is free (tray open)")

if __name__ == "__main__":
    main()
