#!/usr/bin/env python3
"""
rsync_web.py - A single-file web UI for running rsync with live progress.

Run it with:
    python3 rsync_web.py

Then open:
    http://localhost:8001

Features:
- Browse the local filesystem in the browser to pick a source and
  destination folder (no typing full paths required, though you can also
  type them directly).
- Kick off an rsync job in a background thread/subprocess.
- Live progress bar, transfer speed, ETA and a scrolling log, updated via
  polling (no extra dependencies needed - pure standard library).
- Stop a running job.

Only the Python standard library is used, so there is nothing to pip
install. `rsync` itself must be installed and on PATH (it is on virtually
every Linux/macOS system by default).
"""

import datetime
import html
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8001

# ---------------------------------------------------------------------------
# Shared job state (protected by STATE_LOCK)
# ---------------------------------------------------------------------------
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "source": "",
    "destination": "",
    "percent": 0,
    "speed": "",
    "eta": "",
    "transferred": "",
    "current_file": "",
    "log": [],          # list of raw output lines (most recent last)
    "returncode": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}
PROCESS = {"proc": None}
MAX_LOG_LINES = 500

# ---------------------------------------------------------------------------
# Schedule state (protected by SCHEDULE_LOCK). Lives in memory only - it
# resets if you restart the script.
# ---------------------------------------------------------------------------
SCHEDULE_LOCK = threading.Lock()
SCHEDULE = {
    "enabled": False,
    "mode": "interval",        # "interval" or "daily"
    "interval_minutes": 60,
    "daily_time": "02:00",     # 24h "HH:MM", used when mode == "daily"
    "source": "",
    "destination": "",
    "opts": {},                 # same opts dict accepted by build_rsync_cmd
    "next_run": None,           # epoch seconds
    "last_run": None,           # epoch seconds
    "last_run_note": None,
}
DAILY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Matches rsync --info=progress2 lines, e.g.:
#   1,234,567  42%   12.34MB/s    0:00:07 (xfr#3, to-check=12/50)
PROGRESS_RE = re.compile(
    r"([\d,]+)\s+(\d{1,3})%\s+([\d.]+\w+/s)\s+(\d+:\d{2}:\d{2})"
)


def reset_state(source, destination):
    with STATE_LOCK:
        STATE.update({
            "running": True,
            "source": source,
            "destination": destination,
            "percent": 0,
            "speed": "",
            "eta": "",
            "transferred": "",
            "current_file": "",
            "log": [],
            "returncode": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        })


def append_log(line):
    with STATE_LOCK:
        STATE["log"].append(line)
        if len(STATE["log"]) > MAX_LOG_LINES:
            STATE["log"] = STATE["log"][-MAX_LOG_LINES:]


def run_rsync(cmd):
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
        PROCESS["proc"] = proc

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue

            m = PROGRESS_RE.search(line)
            if m:
                transferred, percent, speed, eta = m.groups()
                with STATE_LOCK:
                    STATE["transferred"] = transferred
                    STATE["percent"] = int(percent)
                    STATE["speed"] = speed
                    STATE["eta"] = eta
            elif not line.startswith(("sending incremental", "sent ", "total size")):
                # Treat non-progress, non-summary lines as the "current file"
                with STATE_LOCK:
                    STATE["current_file"] = line

            append_log(line)

        returncode = proc.wait()
        with STATE_LOCK:
            STATE["returncode"] = returncode
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            if returncode == 0:
                STATE["percent"] = 100
    except FileNotFoundError:
        with STATE_LOCK:
            STATE["error"] = "rsync was not found on PATH. Please install rsync."
            STATE["running"] = False
            STATE["finished_at"] = time.time()
    except Exception as exc:  # noqa: BLE001
        with STATE_LOCK:
            STATE["error"] = str(exc)
            STATE["running"] = False
            STATE["finished_at"] = time.time()
    finally:
        PROCESS["proc"] = None


def build_rsync_cmd(source, destination, opts):
    cmd = ["rsync", "--human-readable", "--info=progress2"]
    if opts.get("archive", True):
        cmd.append("--archive")
    if opts.get("no_inc_recursive", True):
        cmd.append("--no-inc-recursive")
    if opts.get("delete"):
        cmd.append("--delete")
    if opts.get("dry_run"):
        cmd.append("--dry-run")
    if opts.get("compress"):
        cmd.append("-z")

    chmod_val = (opts.get("chmod") or "").strip()
    if opts.get("chmod_enabled") and chmod_val:
        cmd.append("--chmod=" + chmod_val)

    chown_val = (opts.get("chown") or "").strip()
    if opts.get("chown_enabled") and chown_val:
        cmd.append("--chown=" + chown_val)

    src = source
    if opts.get("contents_only") and not src.endswith(os.sep):
        src = src + os.sep

    cmd.extend([src, destination])
    return cmd


# ---------------------------------------------------------------------------
# Scheduler - a lightweight background thread that checks every few seconds
# whether a scheduled job is due, and if so kicks it off using the same
# run_rsync() machinery as a manually-started job.
# ---------------------------------------------------------------------------
def compute_next_run(mode, interval_minutes, daily_time, from_ts=None):
    now = from_ts if from_ts is not None else time.time()
    if mode == "daily":
        m = DAILY_TIME_RE.match((daily_time or "").strip())
        hh, mm = (int(m.group(1)), int(m.group(2))) if m else (2, 0)
        now_dt = datetime.datetime.fromtimestamp(now)
        target = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_dt:
            target += datetime.timedelta(days=1)
        return target.timestamp()
    # "interval" mode (default/fallback)
    try:
        minutes = max(1, float(interval_minutes))
    except (TypeError, ValueError):
        minutes = 60
    return now + minutes * 60


def scheduler_loop():
    while True:
        time.sleep(5)
        with SCHEDULE_LOCK:
            sched = dict(SCHEDULE)

        if not sched["enabled"] or sched["next_run"] is None:
            continue
        if time.time() < sched["next_run"]:
            continue

        with STATE_LOCK:
            already_running = STATE["running"]
        if already_running:
            # A manual (or previous scheduled) job is still going.
            # Leave next_run as-is; we'll retry on the next tick.
            continue

        source = sched["source"]
        destination = sched["destination"]

        if not source or not os.path.isdir(source):
            with SCHEDULE_LOCK:
                SCHEDULE["last_run_note"] = "Skipped: source folder not found (" + str(source) + ")"
                SCHEDULE["next_run"] = compute_next_run(
                    sched["mode"], sched["interval_minutes"], sched["daily_time"]
                )
            continue

        try:
            os.makedirs(destination, exist_ok=True)
            cmd = build_rsync_cmd(source, destination, sched["opts"])
        except Exception as exc:  # noqa: BLE001
            with SCHEDULE_LOCK:
                SCHEDULE["last_run_note"] = "Skipped: " + str(exc)
                SCHEDULE["next_run"] = compute_next_run(
                    sched["mode"], sched["interval_minutes"], sched["daily_time"]
                )
            continue

        reset_state(source, destination)
        append_log("$ [scheduled run] " + " ".join(cmd))
        thread = threading.Thread(target=run_rsync, args=(cmd,), daemon=True)
        thread.start()

        with SCHEDULE_LOCK:
            SCHEDULE["last_run"] = time.time()
            SCHEDULE["last_run_note"] = "Started"
            SCHEDULE["next_run"] = compute_next_run(
                sched["mode"], sched["interval_minutes"], sched["daily_time"]
            )


# ---------------------------------------------------------------------------
# HTML / JS front-end (single page, no external assets)
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>rsync web</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #262b36; --text: #e6e8ee;
    --muted: #8b93a7; --accent: #4f8cff; --good: #38c172; --bad: #e3342f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 20px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px;
  }
  label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  input[type=text] {
    width: 100%; padding: 8px 10px; background: #0f1115; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; font-size: 13px;
  }
  .row { display: flex; gap: 8px; margin-bottom: 10px; }
  .row input[type=text] { flex: 1; }
  button {
    background: #232838; color: var(--text); border: 1px solid var(--border);
    padding: 8px 12px; border-radius: 6px; cursor: pointer; font-size: 13px;
  }
  button:hover { background: #2b3145; }
  button.primary { background: var(--accent); border-color: var(--accent); color: white; }
  button.primary:hover { background: #3d7bff; }
  button.danger { background: var(--bad); border-color: var(--bad); color: white; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .browser {
    margin-top: 10px; max-height: 220px; overflow-y: auto;
    border: 1px solid var(--border); border-radius: 6px; background: #0f1115;
  }
  .browser .entry {
    padding: 6px 10px; font-size: 13px; cursor: pointer; border-bottom: 1px solid #1c202a;
  }
  .browser .entry:hover { background: #1c202a; }
  .browser .path { color: var(--muted); font-size: 12px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
  .opts { display: flex; flex-wrap: wrap; gap: 14px; margin: 14px 0; font-size: 13px; align-items: center; }
  .opts label { display: flex; align-items: center; gap: 6px; color: var(--text); }
  .opts input[type=text] {
    width: 160px; padding: 5px 8px; background: #0f1115; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; font-size: 12px;
  }
  .opts input[type=text]:disabled { opacity: 0.4; cursor: not-allowed; }
  .actions { display: flex; gap: 10px; margin-top: 6px; }
  .progress-wrap { margin-top: 20px; }
  .bar-bg { background: #0f1115; border: 1px solid var(--border); border-radius: 8px; height: 22px; overflow: hidden; }
  .bar-fill { background: linear-gradient(90deg, var(--accent), #7db1ff); height: 100%; width: 0%; transition: width .3s ease; }
  .stats { display: flex; gap: 18px; margin-top: 8px; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
  .stats b { color: var(--text); }
  .status-pill {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; margin-left: 8px;
  }
  .status-idle { background: #2b3145; color: var(--muted); }
  .status-running { background: #2a3f2a; color: var(--good); }
  .status-error { background: #3f2a2a; color: var(--bad); }
  .status-done { background: #2a3f2a; color: var(--good); }
  pre#log {
    margin-top: 14px; background: #0a0c10; border: 1px solid var(--border);
    border-radius: 8px; padding: 12px; height: 260px; overflow-y: auto;
    font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
  }
  .full { grid-column: 1 / -1; }
  .schedule-status { margin-top: 10px; font-size: 13px; color: var(--muted); }
  .schedule-status.active { color: var(--good); }
  .schedule-panel {
    margin-top: 12px; padding: 14px; border: 1px dashed var(--border);
    border-radius: 8px; background: #12151c;
  }
  .schedule-panel select, .schedule-panel input[type=number], .schedule-panel input[type=time] {
    padding: 5px 8px; background: #0f1115; color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; font-size: 12px;
  }
</style>
</head>
<body>

<h1>rsync web <span id="status" class="status-pill status-idle">idle</span></h1>

<div class="grid">
  <div class="panel">
    <label>Source folder</label>
    <div class="row">
      <input type="text" id="source" placeholder="/path/to/source">
      <button onclick="openBrowser('source')">Browse</button>
    </div>
    <div id="browser-source" class="browser" style="display:none"></div>
  </div>

  <div class="panel">
    <label>Destination folder</label>
    <div class="row">
      <input type="text" id="destination" placeholder="/path/to/destination">
      <button onclick="openBrowser('destination')">Browse</button>
    </div>
    <div id="browser-destination" class="browser" style="display:none"></div>
  </div>

  <div class="panel full">
    <div class="opts">
      <label><input type="checkbox" id="archive" checked> Archive mode (--archive)</label>
      <label><input type="checkbox" id="contents_only" checked> Copy contents of source (not the folder itself)</label>
      <label><input type="checkbox" id="delete"> Delete extraneous files in destination</label>
      <label><input type="checkbox" id="compress"> Compress during transfer</label>
      <label><input type="checkbox" id="dry_run"> Dry run (no changes)</label>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="chmod_enabled" onchange="toggleInput('chmod')"> Set permissions (--chmod=)</label>
      <input type="text" id="chmod" placeholder="e.g. D755,F644" disabled>
      <label><input type="checkbox" id="chown_enabled" onchange="toggleInput('chown')"> Set owner:group (--chown=)</label>
      <input type="text" id="chown" placeholder="e.g. user:group" disabled>
    </div>
    <div class="actions">
      <button class="primary" id="startBtn" onclick="startSync()">Start rsync</button>
      <button class="danger" id="stopBtn" onclick="stopSync()" disabled>Stop</button>
      <button id="scheduleToggleBtn" onclick="toggleSchedulePanel()">Schedule...</button>
    </div>
    <div id="schedule-status" class="schedule-status">No schedule set.</div>

    <div id="schedule-panel" class="schedule-panel" style="display:none">
      <div class="opts">
        <label style="min-width:110px">Run:
          <select id="sched_mode" onchange="scheduleModeChanged()">
            <option value="interval">Repeat every</option>
            <option value="daily">Daily at</option>
          </select>
        </label>
        <span id="sched_interval_fields">
          <input type="number" id="sched_interval_value" min="1" value="60" style="width:70px">
          <select id="sched_interval_unit">
            <option value="minutes">minutes</option>
            <option value="hours">hours</option>
          </select>
        </span>
        <span id="sched_daily_fields" style="display:none">
          <input type="time" id="sched_daily_time" value="02:00">
        </span>
      </div>
      <div class="actions">
        <button class="primary" onclick="saveSchedule()">Save &amp; enable schedule</button>
        <button class="danger" onclick="cancelSchedule()">Cancel schedule</button>
      </div>
      <div style="margin-top:8px; font-size:12px; color:var(--muted)">
        Uses the source, destination and options set above. The schedule runs
        for as long as this script keeps running, and only while a page is
        loaded is not required - it lives on the server. Restarting the
        script clears the schedule.
      </div>
    </div>

    <div class="progress-wrap">
      <div class="bar-bg"><div class="bar-fill" id="bar"></div></div>
      <div class="stats">
        <span><b id="pct">0%</b></span>
        <span>Speed: <b id="speed">-</b></span>
        <span>ETA: <b id="eta">-</b></span>
        <span>Transferred: <b id="transferred">-</b></span>
      </div>
      <div style="margin-top:8px; font-size:12px; color:var(--muted)">Current: <span id="current">-</span></div>
    </div>

    <pre id="log"></pre>
  </div>
</div>

<script>
let pollTimer = null;

async function browsePath(path) {
  const res = await fetch('/browse?path=' + encodeURIComponent(path || ''));
  return res.json();
}

async function openBrowser(which) {
  const box = document.getElementById('browser-' + which);
  const input = document.getElementById(which);
  const startPath = input.value || '';
  box.style.display = 'block';
  await renderBrowser(which, startPath);
}

async function renderBrowser(which, path) {
  const box = document.getElementById('browser-' + which);
  const input = document.getElementById(which);
  const data = await browsePath(path);
  if (data.error) {
    box.innerHTML = '<div class="path">' + data.error + '</div>';
    return;
  }
  input.value = data.path;
  let htmlStr = '<div class="path">' + data.path + '</div>';
  htmlStr += '<div class="entry" onclick="selectHere(\\''+which+'\\')"><b>&#10003; Use this folder</b></div>';
  if (data.parent !== null) {
    htmlStr += '<div class="entry" onclick="renderBrowser(\\''+which+'\\', \\''+escapeAttr(data.parent)+'\\')">.. (parent folder)</div>';
  }
  for (const d of data.dirs) {
    htmlStr += '<div class="entry" onclick="renderBrowser(\\''+which+'\\', \\''+escapeAttr(d.path)+'\\')">&#128193; ' + escapeHtml(d.name) + '</div>';
  }
  box.innerHTML = htmlStr;
}

function selectHere(which) {
  document.getElementById('browser-' + which).style.display = 'none';
}

function toggleInput(id) {
  const enabled = document.getElementById(id + '_enabled').checked;
  const input = document.getElementById(id);
  input.disabled = !enabled;
  if (enabled) input.focus();
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escapeAttr(s) {
  return s.replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");
}

function collectOpts() {
  return {
    archive: document.getElementById('archive').checked,
    contents_only: document.getElementById('contents_only').checked,
    delete: document.getElementById('delete').checked,
    compress: document.getElementById('compress').checked,
    dry_run: document.getElementById('dry_run').checked,
    chmod_enabled: document.getElementById('chmod_enabled').checked,
    chmod: document.getElementById('chmod').value,
    chown_enabled: document.getElementById('chown_enabled').checked,
    chown: document.getElementById('chown').value,
  };
}

async function startSync() {
  const body = Object.assign({
    source: document.getElementById('source').value,
    destination: document.getElementById('destination').value,
  }, collectOpts());
  const res = await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || 'Failed to start');
    return;
  }
  document.getElementById('log').textContent = '';
  startPolling();
}

async function stopSync() {
  await fetch('/stop', {method: 'POST'});
}

function setStatusPill(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = 'status-pill status-' + cls;
}

async function poll() {
  const res = await fetch('/status');
  const s = await res.json();

  document.getElementById('bar').style.width = s.percent + '%';
  document.getElementById('pct').textContent = s.percent + '%';
  document.getElementById('speed').textContent = s.speed || '-';
  document.getElementById('eta').textContent = s.eta || '-';
  document.getElementById('transferred').textContent = s.transferred || '-';
  document.getElementById('current').textContent = s.current_file || '-';
  document.getElementById('log').textContent = s.log.join('\\n');
  document.getElementById('log').scrollTop = document.getElementById('log').scrollHeight;

  document.getElementById('startBtn').disabled = s.running;
  document.getElementById('stopBtn').disabled = !s.running;

  if (s.running) {
    setStatusPill('running', 'running');
  } else if (s.error) {
    setStatusPill('error: ' + s.error, 'error');
    stopPolling();
  } else if (s.returncode === 0) {
    setStatusPill('done', 'done');
    stopPolling();
  } else if (s.returncode !== null) {
    setStatusPill('exited (' + s.returncode + ')', 'error');
    stopPolling();
  } else {
    setStatusPill('idle', 'idle');
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 700);
  poll();
}
function stopPolling() {
  // keep polling a bit slower so the final state is still visible, but stop hammering
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// -- Scheduling ----------------------------------------------------------
function toggleSchedulePanel() {
  const p = document.getElementById('schedule-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}

function scheduleModeChanged() {
  const mode = document.getElementById('sched_mode').value;
  document.getElementById('sched_interval_fields').style.display = mode === 'interval' ? 'inline-block' : 'none';
  document.getElementById('sched_daily_fields').style.display = mode === 'daily' ? 'inline-block' : 'none';
}

async function saveSchedule() {
  const mode = document.getElementById('sched_mode').value;
  let interval_minutes = 60;
  if (mode === 'interval') {
    const val = parseFloat(document.getElementById('sched_interval_value').value) || 1;
    const unit = document.getElementById('sched_interval_unit').value;
    interval_minutes = unit === 'hours' ? val * 60 : val;
  }
  const body = Object.assign({
    mode: mode,
    interval_minutes: interval_minutes,
    daily_time: document.getElementById('sched_daily_time').value || '02:00',
    source: document.getElementById('source').value,
    destination: document.getElementById('destination').value,
  }, collectOpts());

  const res = await fetch('/schedule', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || 'Failed to save schedule');
    return;
  }
  renderSchedule(data.schedule);
}

async function cancelSchedule() {
  const res = await fetch('/schedule/cancel', {method: 'POST'});
  const data = await res.json();
  renderSchedule(data.schedule);
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return (h > 0 ? h + 'h ' : '') + m + 'm ' + s + 's';
}

function renderSchedule(s) {
  const el = document.getElementById('schedule-status');
  if (!s || !s.enabled) {
    el.textContent = 'No schedule set.';
    el.className = 'schedule-status';
    return;
  }
  const desc = s.mode === 'daily'
    ? ('Scheduled daily at ' + s.daily_time)
    : ('Scheduled every ' + s.interval_minutes + ' min');
  let nextStr = '-';
  if (s.next_run) {
    nextStr = formatDuration(s.next_run - (Date.now() / 1000));
  }
  let text = desc + ' — next run in ' + nextStr;
  if (s.last_run) {
    text += ' (last run: ' + new Date(s.last_run * 1000).toLocaleString() + ')';
  }
  el.textContent = text;
  el.className = 'schedule-status active';
}

async function pollSchedule() {
  const res = await fetch('/schedule');
  const s = await res.json();
  renderSchedule(s);
}
setInterval(pollSchedule, 3000);
pollSchedule();

// One-time pre-fill of the schedule panel's fields from any saved schedule,
// so re-opening the panel (or reloading the page) shows what's active
// without repeatedly overwriting fields the user is actively editing.
fetch('/schedule').then(r => r.json()).then(s => {
  if (!s || !s.mode) return;
  document.getElementById('sched_mode').value = s.mode;
  document.getElementById('sched_daily_time').value = s.daily_time || '02:00';
  if (s.interval_minutes) {
    if (s.interval_minutes % 60 === 0) {
      document.getElementById('sched_interval_value').value = s.interval_minutes / 60;
      document.getElementById('sched_interval_unit').value = 'hours';
    } else {
      document.getElementById('sched_interval_value').value = s.interval_minutes;
      document.getElementById('sched_interval_unit').value = 'minutes';
    }
  }
  scheduleModeChanged();
  if (s.enabled) {
    if (s.source) document.getElementById('source').value = s.source;
    if (s.destination) document.getElementById('destination').value = s.destination;
  }
});

// Initial load: reflect current server-side state (e.g. after page refresh)
poll().then(() => {
  fetch('/status').then(r => r.json()).then(s => { if (s.running) startPolling(); });
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "RsyncWeb/1.0"

    def log_message(self, fmt, *args):
        # Quieter server log; comment out to see request logs.
        pass

    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(PAGE)
        elif parsed.path == "/status":
            with STATE_LOCK:
                self._send_json(dict(STATE))
        elif parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            path = qs.get("path", [""])[0]
            self._handle_browse(path)
        elif parsed.path == "/schedule":
            with SCHEDULE_LOCK:
                self._send_json(dict(SCHEDULE))
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/start":
            self._handle_start()
        elif parsed.path == "/stop":
            self._handle_stop()
        elif parsed.path == "/schedule":
            self._handle_schedule_save()
        elif parsed.path == "/schedule/cancel":
            self._handle_schedule_cancel()
        else:
            self._send_json({"error": "not found"}, status=404)

    # -- handlers ------------------------------------------------------
    def _handle_browse(self, path):
        try:
            base = path.strip() or os.path.expanduser("~")
            base = os.path.abspath(os.path.expanduser(base))
            if not os.path.isdir(base):
                self._send_json({"error": "Not a directory: " + base})
                return
            entries = []
            try:
                with os.scandir(base) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                                entries.append(entry.name)
                        except OSError:
                            continue
            except PermissionError:
                self._send_json({"error": "Permission denied: " + base})
                return

            entries.sort(key=str.lower)
            parent = os.path.dirname(base) if base != os.path.dirname(base) else None
            self._send_json({
                "path": base,
                "parent": parent,
                "dirs": [{"name": n, "path": os.path.join(base, n)} for n in entries],
            })
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)})

    def _parse_opts(self, body):
        """Parse and validate the shared rsync option fields from a JSON body.
        Returns (opts_dict, error_message). error_message is None on success.
        """
        chmod_enabled = bool(body.get("chmod_enabled", False))
        chmod_val = (body.get("chmod") or "").strip()
        if chmod_enabled and not chmod_val:
            return None, "Enter a value for --chmod= (e.g. D755,F644) or uncheck it."

        chown_enabled = bool(body.get("chown_enabled", False))
        chown_val = (body.get("chown") or "").strip()
        if chown_enabled and not chown_val:
            return None, "Enter a value for --chown= (e.g. user:group) or uncheck it."

        opts = {
            "archive": bool(body.get("archive", True)),
            "contents_only": bool(body.get("contents_only", True)),
            "delete": bool(body.get("delete", False)),
            "compress": bool(body.get("compress", False)),
            "dry_run": bool(body.get("dry_run", False)),
            "no_inc_recursive": True,
            "chmod_enabled": chmod_enabled,
            "chmod": chmod_val,
            "chown_enabled": chown_enabled,
            "chown": chown_val,
        }
        return opts, None

    def _handle_start(self):
        with STATE_LOCK:
            already_running = STATE["running"]
        if already_running:
            self._send_json({"error": "A sync is already running."}, status=409)
            return

        body = self._read_json_body()
        source = (body.get("source") or "").strip()
        destination = (body.get("destination") or "").strip()

        if not source or not destination:
            self._send_json({"error": "Both source and destination are required."}, status=400)
            return

        source = os.path.abspath(os.path.expanduser(source))
        destination = os.path.abspath(os.path.expanduser(destination))

        if not os.path.isdir(source):
            self._send_json({"error": "Source folder does not exist: " + source}, status=400)
            return

        os.makedirs(destination, exist_ok=True)

        opts, error = self._parse_opts(body)
        if error:
            self._send_json({"error": error}, status=400)
            return

        cmd = build_rsync_cmd(source, destination, opts)
        reset_state(source, destination)
        append_log("$ " + " ".join(cmd))

        thread = threading.Thread(target=run_rsync, args=(cmd,), daemon=True)
        thread.start()

        self._send_json({"ok": True, "cmd": cmd})

    def _handle_stop(self):
        proc = PROCESS.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
            self._send_json({"ok": True, "message": "Stop signal sent."})
        else:
            self._send_json({"ok": False, "message": "No running process."})

    def _handle_schedule_save(self):
        body = self._read_json_body()
        source = (body.get("source") or "").strip()
        destination = (body.get("destination") or "").strip()

        if not source or not destination:
            self._send_json({"error": "Both source and destination are required."}, status=400)
            return

        source = os.path.abspath(os.path.expanduser(source))
        destination = os.path.abspath(os.path.expanduser(destination))

        if not os.path.isdir(source):
            self._send_json({"error": "Source folder does not exist: " + source}, status=400)
            return

        mode = body.get("mode") if body.get("mode") in ("interval", "daily") else "interval"

        interval_minutes = 60
        if mode == "interval":
            try:
                interval_minutes = float(body.get("interval_minutes", 60))
            except (TypeError, ValueError):
                interval_minutes = 0
            if interval_minutes < 1:
                self._send_json({"error": "Enter a repeat interval of at least 1 minute."}, status=400)
                return

        daily_time = (body.get("daily_time") or "02:00").strip()
        if mode == "daily" and not DAILY_TIME_RE.match(daily_time):
            self._send_json({"error": "Enter a valid time in HH:MM (24-hour) format."}, status=400)
            return

        opts, error = self._parse_opts(body)
        if error:
            self._send_json({"error": error}, status=400)
            return

        next_run = compute_next_run(mode, interval_minutes, daily_time)

        with SCHEDULE_LOCK:
            SCHEDULE.update({
                "enabled": True,
                "mode": mode,
                "interval_minutes": interval_minutes,
                "daily_time": daily_time,
                "source": source,
                "destination": destination,
                "opts": opts,
                "next_run": next_run,
                "last_run_note": None,
            })
            snapshot = dict(SCHEDULE)

        self._send_json({"ok": True, "schedule": snapshot})

    def _handle_schedule_cancel(self):
        with SCHEDULE_LOCK:
            SCHEDULE["enabled"] = False
            SCHEDULE["next_run"] = None
            snapshot = dict(SCHEDULE)
        self._send_json({"ok": True, "schedule": snapshot})


def main():
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"rsync web running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
