#!/usr/bin/env python3
"""
ServerDeck - a single-file web UI for basic Debian/Ubuntu server management.

Features:
  * Overview: CPU, memory, storage, uptime, hostname rename, restart and shutdown.
  * Updates: refresh package lists, view available upgrades, install upgrades.
  * Backups: create/run rsync jobs and schedule them with cron or systemd timers.
  * Autostart: run Python scripts or commands whenever the system starts.

Runtime dependencies:
  * Python 3 standard library only.
  * Debian/Ubuntu system tools: apt-get, hostnamectl, systemctl/cron as applicable.
  * rsync is needed only for backup jobs and can be installed from the UI.

Run interactively:
  sudo python3 serverdeck.py

Install as a systemd service:
  sudo python3 serverdeck.py --install-service

By default, web login uses Linux PAM and accepts the username/password of a
local account that belongs to the sudo group. A static application password is
still available with --auth-mode static.

Security note: the built-in web server does not provide TLS. Device account
passwords must only be entered over a trusted LAN, VPN, or HTTPS reverse proxy.
Do not expose this service directly to the public internet.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import datetime as dt
import errno
import functools
import grp
import hashlib
import hmac
import html
import http.cookies
import http.server
import json
import os
import pathlib
import pwd
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

APP_NAME = "ServerDeck"
APP_VERSION = "0.5.0"
APP_BUILD = "2026-07-30-autostart-page"
DEFAULT_PORT = 8081
MAX_BODY = 1024 * 1024
MAX_TASK_OUTPUT = 300_000
HOSTNAME_RE = re.compile(r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?$")
JOB_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
CRON_FIELD_RE = re.compile(r"^[A-Za-z0-9*/?,\-]+$")
CHMOD_RE = re.compile(r"^[A-Za-z0-9,+\-=]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SESSION_COOKIE = "serverdeck_session"
PAM_SERVICE = "serverdeck"
PAM_CONFIG_PATH = pathlib.Path("/etc/pam.d/serverdeck")
PAM_CONFIG = """#%PAM-1.0
# Managed by ServerDeck. Local account authentication for the web UI.
@include common-auth
@include common-account
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def atomic_write(path: pathlib.Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(
    args: List[str],
    *,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stdout}")
    return result


def stream_command(
    args: List[str],
    write: Callable[[str], None],
    *,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> int:
    write("$ " + shlex.join(args) + "\n")
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        env=env,
        cwd=cwd,
    )
    assert process.stdout is not None
    for line in process.stdout:
        write(line)
    return process.wait()


def apt_environment() -> Dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DEBIAN_FRONTEND": "noninteractive",
            "APT_LISTCHANGES_FRONTEND": "none",
            "NEEDRESTART_MODE": "a",
            "LC_ALL": "C",
        }
    )
    return env


def safe_text(value: Any, max_len: int = 4096) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Newlines and NUL bytes are not allowed")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"Value is too long (maximum {max_len} characters)")
    return value


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class PAMError(RuntimeError):
    pass


class PAMAuthenticator:
    """Minimal Linux-PAM client implemented with ctypes (no Python package)."""

    PAM_SUCCESS = 0
    PAM_PROMPT_ECHO_OFF = 1
    PAM_PROMPT_ECHO_ON = 2
    PAM_ERROR_MSG = 3
    PAM_TEXT_INFO = 4
    PAM_CONV_ERR = 19

    class PamMessage(ctypes.Structure):
        _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]

    class PamResponse(ctypes.Structure):
        _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]

    CONVERSATION = ctypes.CFUNCTYPE(
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.POINTER(PamMessage)),
        ctypes.POINTER(ctypes.POINTER(PamResponse)),
        ctypes.c_void_p,
    )

    class PamConv(ctypes.Structure):
        pass

    PamConv._fields_ = [("conv", CONVERSATION), ("appdata_ptr", ctypes.c_void_p)]

    def __init__(self, service: str = PAM_SERVICE):
        pam_name = ctypes.util.find_library("pam") or "libpam.so.0"
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        try:
            self.libpam = ctypes.CDLL(pam_name)
            self.libc = ctypes.CDLL(libc_name)
        except OSError as exc:
            raise PAMError("Linux PAM libraries are not available") from exc

        self.libpam.pam_start.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(self.PamConv),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.libpam.pam_start.restype = ctypes.c_int
        self.libpam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.libpam.pam_authenticate.restype = ctypes.c_int
        self.libpam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.libpam.pam_acct_mgmt.restype = ctypes.c_int
        self.libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.libpam.pam_end.restype = ctypes.c_int
        self.libpam.pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.libpam.pam_strerror.restype = ctypes.c_char_p

        self.libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        self.libc.calloc.restype = ctypes.c_void_p
        self.libc.strdup.argtypes = [ctypes.c_char_p]
        self.libc.strdup.restype = ctypes.c_void_p
        self.libc.free.argtypes = [ctypes.c_void_p]
        self.libc.free.restype = None
        self.service = service.encode("utf-8")

    def authenticate(self, username: str, password: str) -> bool:
        if not USERNAME_RE.fullmatch(username) or not password:
            return False

        username_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        libc = self.libc

        @self.CONVERSATION
        def conversation(num_msg, messages, responses, _appdata):
            if num_msg <= 0:
                return self.PAM_CONV_ERR
            raw = libc.calloc(num_msg, ctypes.sizeof(self.PamResponse))
            if not raw:
                return self.PAM_CONV_ERR
            response_array = ctypes.cast(raw, ctypes.POINTER(self.PamResponse))
            try:
                for index in range(num_msg):
                    style = messages[index].contents.msg_style
                    if style == self.PAM_PROMPT_ECHO_OFF:
                        answer = password_bytes
                    elif style == self.PAM_PROMPT_ECHO_ON:
                        answer = username_bytes
                    elif style in (self.PAM_ERROR_MSG, self.PAM_TEXT_INFO):
                        answer = None
                    else:
                        raise ValueError("Unsupported PAM prompt")
                    if answer is not None:
                        duplicated = libc.strdup(answer)
                        if not duplicated:
                            raise MemoryError
                        response_array[index].resp = ctypes.cast(duplicated, ctypes.c_char_p)
                    response_array[index].resp_retcode = 0
                responses[0] = response_array
                return self.PAM_SUCCESS
            except Exception:
                for index in range(num_msg):
                    pointer = ctypes.cast(response_array[index].resp, ctypes.c_void_p).value
                    if pointer:
                        libc.free(pointer)
                libc.free(raw)
                return self.PAM_CONV_ERR

        conv = self.PamConv(conversation, None)
        handle = ctypes.c_void_p()
        result = self.libpam.pam_start(self.service, username_bytes, ctypes.byref(conv), ctypes.byref(handle))
        if result != self.PAM_SUCCESS:
            return False
        final_result = result
        try:
            final_result = self.libpam.pam_authenticate(handle, 0)
            if final_result != self.PAM_SUCCESS:
                return False
            final_result = self.libpam.pam_acct_mgmt(handle, 0)
            return final_result == self.PAM_SUCCESS
        finally:
            self.libpam.pam_end(handle, final_result)


def ensure_pam_config() -> None:
    if PAM_CONFIG_PATH.exists():
        return
    if os.geteuid() != 0:
        raise PermissionError(
            f"{PAM_CONFIG_PATH} is missing; run ServerDeck once with sudo or use --install-service"
        )
    atomic_write(PAM_CONFIG_PATH, PAM_CONFIG, 0o644)


def user_group_names(username: str) -> set[str]:
    account = pwd.getpwnam(username)
    gids = set(os.getgrouplist(username, account.pw_gid))
    names: set[str] = set()
    for gid in gids:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def account_is_authorized(username: str, allowed_groups: List[str]) -> bool:
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        return False
    shell = account.pw_shell.rsplit("/", 1)[-1]
    if shell in {"false", "nologin"}:
        return False
    if not allowed_groups or "*" in allowed_groups:
        return True
    try:
        return bool(user_group_names(username).intersection(allowed_groups))
    except OSError:
        return False


class SessionStore:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self.lock = threading.RLock()
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def create(self, username: str) -> Tuple[str, Dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = {
            "username": username,
            "csrf": secrets.token_urlsafe(32),
            "created": now,
            "expires": now + self.ttl_seconds,
        }
        with self.lock:
            self.sessions[token] = session
            self._purge_locked(now)
        return token, dict(session)

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        now = time.time()
        with self.lock:
            self._purge_locked(now)
            session = self.sessions.get(token)
            if not session:
                return None
            session["expires"] = now + self.ttl_seconds
            return dict(session)

    def destroy(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def _purge_locked(self, now: float) -> None:
        expired = [token for token, session in self.sessions.items() if session["expires"] <= now]
        for token in expired:
            self.sessions.pop(token, None)


class Config:
    def __init__(self, args: argparse.Namespace):
        self.host = args.host
        self.port = args.port
        self.auth_mode = "none" if args.no_auth else args.auth_mode
        self.no_auth = self.auth_mode == "none"
        self.username = args.username
        self.auth_groups = [item.strip() for item in args.auth_groups.split(",") if item.strip()]
        self.session_ttl = max(300, int(args.session_ttl))
        self.secure_cookie = bool(args.secure_cookie)
        self.script_path = pathlib.Path(__file__).resolve()
        self.is_root = os.geteuid() == 0

        if args.data_dir:
            self.data_dir = pathlib.Path(args.data_dir).expanduser().resolve()
        elif self.is_root:
            self.data_dir = pathlib.Path("/var/lib/serverdeck")
        else:
            self.data_dir = pathlib.Path.home() / ".local" / "share" / "serverdeck"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except PermissionError:
            pass

        self.jobs_file = self.data_dir / "backup-jobs.json"
        # Autostart jobs use their own file. On first launch, migrate only legacy
        # tasks that were explicitly configured to run at boot. Recurring timer
        # jobs are intentionally not converted into startup jobs.
        self.timers_file = self.data_dir / "autostart-jobs.json"
        legacy_timers_file = self.data_dir / "timer-jobs.json"
        if not self.timers_file.exists() and legacy_timers_file.exists():
            try:
                legacy_jobs = json.loads(legacy_timers_file.read_text(encoding="utf-8"))
                migrated = []
                if isinstance(legacy_jobs, list):
                    for legacy_job in legacy_jobs:
                        if not isinstance(legacy_job, dict):
                            continue
                        schedule = legacy_job.get("schedule", {})
                        if isinstance(schedule, dict) and schedule.get("trigger") == "boot":
                            item = dict(legacy_job)
                            item["schedule"] = {
                                "enabled": bool(schedule.get("enabled", True)),
                                "trigger": "boot",
                                "boot_delay": int(schedule.get("boot_delay", 30)),
                            }
                            migrated.append(item)
                atomic_write(self.timers_file, json.dumps(migrated, indent=2, sort_keys=True) + "\n", 0o600)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                atomic_write(self.timers_file, "[]\n", 0o600)
        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.password_file = pathlib.Path(args.password_file).expanduser() if args.password_file else self.data_dir / "admin-password.txt"
        self.password = ""
        self.generated_password = False

        if self.auth_mode == "pam" and not args.run_backup and not args.run_autostart:
            ensure_pam_config()
        elif self.auth_mode == "static":
            self.password = self._resolve_password(args.password)

    def _resolve_password(self, cli_password: Optional[str]) -> str:
        env_password = os.environ.get("SERVERDECK_PASSWORD", "")
        if cli_password:
            return cli_password
        if env_password:
            return env_password
        if self.password_file.exists():
            value = self.password_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(18)
        atomic_write(self.password_file, value + "\n", 0o600)
        self.generated_password = True
        return value


class SystemStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_cpu: Optional[Tuple[int, int]] = None

    @staticmethod
    def _read_cpu() -> Tuple[int, int]:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            fields = handle.readline().split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total

    def cpu_percent(self) -> float:
        with self._lock:
            current = self._read_cpu()
            if self._last_cpu is None:
                self._last_cpu = current
                time.sleep(0.12)
                current = self._read_cpu()
            previous = self._last_cpu
            self._last_cpu = current
        idle_delta = current[0] - previous[0]
        total_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)

    @staticmethod
    def memory() -> Dict[str, Any]:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, raw = line.split(":", 1)
                value = int(raw.strip().split()[0]) * 1024
                info[key] = value
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - available)
        percent = round((used / total * 100.0), 1) if total else 0.0
        return {
            "total": total,
            "used": used,
            "available": available,
            "percent": percent,
            "total_human": human_bytes(total),
            "used_human": human_bytes(used),
            "available_human": human_bytes(available),
        }

    @staticmethod
    def storage() -> Dict[str, Any]:
        usage = shutil.disk_usage("/")
        percent = round(usage.used / usage.total * 100.0, 1) if usage.total else 0.0
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": percent,
            "total_human": human_bytes(usage.total),
            "used_human": human_bytes(usage.used),
            "free_human": human_bytes(usage.free),
        }

    @staticmethod
    def uptime() -> Dict[str, Any]:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
        return {"seconds": int(seconds), "human": human_duration(seconds)}

    @staticmethod
    def os_info() -> str:
        values: Dict[str, str] = {}
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as handle:
                for line in handle:
                    if "=" not in line:
                        continue
                    key, value = line.rstrip().split("=", 1)
                    values[key] = value.strip('"')
        except OSError:
            return "Linux"
        return values.get("PRETTY_NAME", values.get("NAME", "Linux"))

    def snapshot(self) -> Dict[str, Any]:
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [0.0, 0.0, 0.0]
        return {
            "cpu_percent": self.cpu_percent(),
            "cpu_count": os.cpu_count() or 1,
            "memory": self.memory(),
            "storage": self.storage(),
            "uptime": self.uptime(),
            "load_average": [round(item, 2) for item in load],
            "hostname": socket.gethostname(),
            "os": self.os_info(),
            "is_root": os.geteuid() == 0,
            "reboot_required": pathlib.Path("/var/run/reboot-required").exists(),
            "time": now_iso(),
        }


class JobStore:
    DEFAULTS = {
        "dry_run": False,
        "archive": True,
        "itemize_changes": True,
        "verbose": True,
        "human_readable": True,
        "partial_progress": True,
        "update": True,
        "chmod_enabled": False,
        "chmod": "",
    }

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_write(self.path, "[]\n", 0o600)

    def _load_unlocked(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._load_unlocked()

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for job in self._load_unlocked():
                if job.get("id") == job_id:
                    return job
        return None

    def save(self, payload: Dict[str, Any], job_id: Optional[str] = None) -> Dict[str, Any]:
        job = self.validate(payload, job_id=job_id)
        with self.lock:
            jobs = self._load_unlocked()
            replaced = False
            for index, existing in enumerate(jobs):
                if existing.get("id") == job["id"]:
                    job["created_at"] = existing.get("created_at", job["created_at"])
                    jobs[index] = job
                    replaced = True
                    break
            if not replaced:
                jobs.append(job)
            atomic_write(self.path, json.dumps(jobs, indent=2, sort_keys=True) + "\n", 0o600)
        return job

    def delete(self, job_id: str) -> bool:
        with self.lock:
            jobs = self._load_unlocked()
            filtered = [job for job in jobs if job.get("id") != job_id]
            if len(filtered) == len(jobs):
                return False
            atomic_write(self.path, json.dumps(filtered, indent=2, sort_keys=True) + "\n", 0o600)
            return True

    @classmethod
    def validate(cls, payload: Dict[str, Any], job_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Invalid backup job")
        name = safe_text(payload.get("name", ""), 80)
        source = safe_text(payload.get("source", ""), 4096)
        destination = safe_text(payload.get("destination", ""), 4096)
        if not JOB_NAME_RE.match(name):
            raise ValueError("Enter a job name between 1 and 80 characters")
        if not source or not destination:
            raise ValueError("Source and destination are required")
        if source.startswith("-") or destination.startswith("-"):
            raise ValueError("Source and destination cannot begin with a dash")

        options_raw = payload.get("options", {})
        if not isinstance(options_raw, dict):
            options_raw = {}
        options = dict(cls.DEFAULTS)
        for key in cls.DEFAULTS:
            if key == "chmod":
                continue
            options[key] = bool(options_raw.get(key, cls.DEFAULTS[key]))
        chmod_value = safe_text(options_raw.get("chmod", ""), 100)
        if options["chmod_enabled"]:
            if not chmod_value or not CHMOD_RE.match(chmod_value):
                raise ValueError("Enter a valid rsync --chmod value without spaces")
            options["chmod"] = chmod_value
        else:
            options["chmod"] = ""

        scheduler = payload.get("scheduler", {})
        if not isinstance(scheduler, dict):
            scheduler = {}
        backend = safe_text(scheduler.get("backend", "manual"), 20).lower()
        enabled = bool(scheduler.get("enabled", False))
        preset = safe_text(scheduler.get("preset", "daily"), 20).lower()
        expression = safe_text(scheduler.get("expression", ""), 200)
        allowed_backends = {"manual", "cron", "systemd"}
        allowed_presets = {"hourly", "daily", "weekly", "monthly", "custom"}
        if backend not in allowed_backends:
            raise ValueError("Invalid scheduler backend")
        if preset not in allowed_presets:
            raise ValueError("Invalid schedule preset")
        if backend == "manual":
            enabled = False
        if enabled and preset == "custom":
            if backend == "cron":
                validate_cron(expression)
            elif backend == "systemd":
                if not expression or len(expression) > 200 or any(ch in expression for ch in "\r\n\x00"):
                    raise ValueError("Enter a valid systemd OnCalendar expression")

        timestamp = now_iso()
        return {
            "id": job_id or uuid.uuid4().hex[:12],
            "name": name,
            "source": source,
            "destination": destination,
            "options": options,
            "scheduler": {
                "backend": backend,
                "enabled": enabled,
                "preset": preset,
                "expression": expression,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }


class TimerStore:
    """Persistent definitions for commands and scripts that run at system startup."""

    ALLOWED_KINDS = {"python", "command"}

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_write(self.path, "[]\n", 0o600)

    def _load_unlocked(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._load_unlocked()

    def get(self, timer_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for job in self._load_unlocked():
                if job.get("id") == timer_id:
                    return job
        return None

    def save(self, payload: Dict[str, Any], timer_id: Optional[str] = None) -> Dict[str, Any]:
        job = self.validate(payload, timer_id=timer_id)
        with self.lock:
            jobs = self._load_unlocked()
            replaced = False
            for index, existing in enumerate(jobs):
                if existing.get("id") == job["id"]:
                    job["created_at"] = existing.get("created_at", job["created_at"])
                    jobs[index] = job
                    replaced = True
                    break
            if not replaced:
                jobs.append(job)
            atomic_write(self.path, json.dumps(jobs, indent=2, sort_keys=True) + "\n", 0o600)
        return job

    def delete(self, timer_id: str) -> bool:
        with self.lock:
            jobs = self._load_unlocked()
            filtered = [job for job in jobs if job.get("id") != timer_id]
            if len(filtered) == len(jobs):
                return False
            atomic_write(self.path, json.dumps(filtered, indent=2, sort_keys=True) + "\n", 0o600)
            return True

    @classmethod
    def validate(cls, payload: Dict[str, Any], timer_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Invalid autostart item")
        name = safe_text(payload.get("name", ""), 80)
        if not JOB_NAME_RE.match(name):
            raise ValueError("Enter a name between 1 and 80 characters")

        kind = safe_text(payload.get("kind", "command"), 20).lower()
        if kind not in cls.ALLOWED_KINDS:
            raise ValueError("Task type must be Python script or command")

        command = safe_text(payload.get("command", ""), 4096)
        script = safe_text(payload.get("script", ""), 4096)
        interpreter = safe_text(payload.get("interpreter", "/usr/bin/python3"), 4096)
        arguments = safe_text(payload.get("arguments", ""), 4096)
        working_directory = safe_text(payload.get("working_directory", ""), 4096)
        run_as_user = safe_text(payload.get("run_as_user", "root"), 64)

        try:
            pwd.getpwnam(run_as_user)
        except KeyError as exc:
            raise ValueError(f"Local user does not exist: {run_as_user}") from exc

        if working_directory:
            working_directory = os.path.expanduser(working_directory)
            if not os.path.isabs(working_directory):
                raise ValueError("Working directory must be an absolute path")
            working_directory = os.path.normpath(working_directory)

        if kind == "python":
            if not script:
                raise ValueError("Choose a Python script")
            script = os.path.expanduser(script)
            if not os.path.isabs(script):
                raise ValueError("Python script path must be absolute")
            script = os.path.normpath(script)
            interpreter = os.path.expanduser(interpreter)
            if not os.path.isabs(interpreter):
                raise ValueError("Python interpreter path must be absolute")
            interpreter = os.path.normpath(interpreter)
            _parse_arguments(arguments, "Python arguments")
            command = ""
        else:
            if not command:
                raise ValueError("Enter a command to run")
            parsed = _parse_arguments(command, "Command")
            if not parsed:
                raise ValueError("Enter a command to run")
            script = ""
            interpreter = "/usr/bin/python3"
            arguments = ""

        schedule_raw = payload.get("schedule", {})
        if not isinstance(schedule_raw, dict):
            schedule_raw = {}
        enabled = bool(schedule_raw.get("enabled", True))
        try:
            boot_delay = int(schedule_raw.get("boot_delay", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("Startup delay must be a whole number of seconds") from exc
        if not 0 <= boot_delay <= 3600:
            raise ValueError("Startup delay must be between 0 and 3600 seconds")

        timestamp = now_iso()
        return {
            "id": timer_id or uuid.uuid4().hex[:12],
            "name": name,
            "kind": kind,
            "command": command,
            "script": script,
            "interpreter": interpreter,
            "arguments": arguments,
            "working_directory": working_directory,
            "run_as_user": run_as_user,
            "schedule": {
                "enabled": enabled,
                "trigger": "boot",
                "boot_delay": boot_delay,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }


def _parse_arguments(value: str, label: str) -> List[str]:
    try:
        result = shlex.split(value, posix=True)
    except ValueError as exc:
        raise ValueError(f"{label} contain unmatched quotes or invalid escaping") from exc
    if len(result) > 256:
        raise ValueError(f"{label} contain too many values")
    return result


def build_timer_args(job: Dict[str, Any]) -> List[str]:
    if job.get("kind") == "python":
        return [job["interpreter"], job["script"], *_parse_arguments(job.get("arguments", ""), "Python arguments")]
    return _parse_arguments(job.get("command", ""), "Command")


def timer_display_command(job: Dict[str, Any]) -> str:
    try:
        return shlex.join(build_timer_args(job))
    except ValueError:
        return job.get("command") or job.get("script") or ""


def local_task_users() -> List[str]:
    users: List[Tuple[int, str]] = []
    blocked_shells = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}
    for account in pwd.getpwall():
        if account.pw_name == "root" or (account.pw_uid >= 1000 and account.pw_shell not in blocked_shells):
            users.append((account.pw_uid, account.pw_name))
    return [name for _uid, name in sorted(set(users), key=lambda item: (item[0] != 0, item[0], item[1]))]


def validate_cron(expression: str) -> None:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("A cron schedule must contain exactly five fields")
    if any(not CRON_FIELD_RE.match(field) for field in fields):
        raise ValueError("The cron schedule contains unsupported characters")


def cron_expression(job: Dict[str, Any]) -> str:
    scheduler = job["scheduler"]
    preset = scheduler.get("preset", "daily")
    mapping = {
        "hourly": "0 * * * *",
        "daily": "0 2 * * *",
        "weekly": "0 2 * * 0",
        "monthly": "0 2 1 * *",
    }
    if preset == "custom":
        expression = scheduler.get("expression", "")
        validate_cron(expression)
        return expression
    return mapping[preset]


def systemd_calendar(job: Dict[str, Any]) -> str:
    scheduler = job["scheduler"]
    preset = scheduler.get("preset", "daily")
    mapping = {
        "hourly": "hourly",
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
    }
    if preset == "custom":
        value = scheduler.get("expression", "").strip()
        if not value or any(ch in value for ch in "\r\n\x00"):
            raise ValueError("Invalid systemd calendar expression")
        return value
    return mapping[preset]


def build_rsync_args(job: Dict[str, Any]) -> List[str]:
    options = job.get("options", {})
    args = ["rsync"]
    if options.get("dry_run"):
        args.append("--dry-run")
    if options.get("archive"):
        args.append("--archive")
    if options.get("itemize_changes"):
        args.append("--itemize-changes")
    if options.get("verbose"):
        args.append("--verbose")
    if options.get("human_readable"):
        args.append("--human-readable")
    if options.get("partial_progress"):
        args.append("-P")
    if options.get("update"):
        args.append("--update")
    if options.get("chmod_enabled"):
        chmod_value = options.get("chmod", "")
        if not CHMOD_RE.match(chmod_value):
            raise ValueError("Invalid --chmod value")
        args.append(f"--chmod={chmod_value}")
    args.extend(["--", job["source"], job["destination"]])
    return args


class SchedulerManager:
    def __init__(self, config: Config, store: JobStore):
        self.config = config
        self.store = store
        self.lock = threading.RLock()
        self.cron_path = pathlib.Path("/etc/cron.d/serverdeck-backups")
        self.systemd_dir = pathlib.Path("/etc/systemd/system")

    def capability(self) -> Dict[str, Any]:
        return {
            "root": self.config.is_root,
            "cron_available": command_exists("cron") or command_exists("crond") or self.cron_path.parent.exists(),
            "systemd_available": command_exists("systemctl") and pathlib.Path("/run/systemd/system").exists(),
        }

    def sync(self) -> List[str]:
        warnings: List[str] = []
        if not self.config.is_root:
            if any(job.get("scheduler", {}).get("enabled") for job in self.store.list()):
                warnings.append("Schedules were saved but cannot be installed unless ServerDeck runs as root.")
            return warnings
        with self.lock:
            try:
                self._sync_cron()
            except Exception as exc:
                warnings.append(f"Cron schedules: {exc}")
            try:
                self._sync_systemd()
            except Exception as exc:
                warnings.append(f"Systemd timers: {exc}")
        return warnings

    def _runner_command(self, job_id: str, shell_style: bool = False) -> str:
        args = [
            "/usr/bin/python3",
            str(self.config.script_path),
            "--run-backup",
            job_id,
            "--data-dir",
            str(self.config.data_dir),
        ]
        if shell_style:
            return shlex.join(args)
        return " ".join(systemd_quote(arg) for arg in args)

    def _sync_cron(self) -> None:
        jobs = [
            job
            for job in self.store.list()
            if job.get("scheduler", {}).get("enabled") and job.get("scheduler", {}).get("backend") == "cron"
        ]
        if not jobs:
            try:
                self.cron_path.unlink()
            except FileNotFoundError:
                pass
            return
        lines = [
            "# Managed by ServerDeck. Manual edits will be overwritten.",
            "SHELL=/bin/sh",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "",
        ]
        for job in jobs:
            expression = cron_expression(job)
            command = self._runner_command(job["id"], shell_style=True)
            lines.append(f"{expression} root {command}")
        atomic_write(self.cron_path, "\n".join(lines) + "\n", 0o644)

    def _sync_systemd(self) -> None:
        if not command_exists("systemctl") or not pathlib.Path("/run/systemd/system").exists():
            jobs = [
                job
                for job in self.store.list()
                if job.get("scheduler", {}).get("enabled") and job.get("scheduler", {}).get("backend") == "systemd"
            ]
            if jobs:
                raise RuntimeError("systemd is not active on this machine")
            return

        expected: Dict[str, Dict[str, Any]] = {}
        for job in self.store.list():
            scheduler = job.get("scheduler", {})
            if scheduler.get("enabled") and scheduler.get("backend") == "systemd":
                expected[job["id"]] = job

        for timer_path in self.systemd_dir.glob("serverdeck-backup-*.timer"):
            job_id = timer_path.stem.removeprefix("serverdeck-backup-")
            if job_id not in expected:
                run_command(["systemctl", "disable", "--now", timer_path.name], timeout=30)
                try:
                    timer_path.unlink()
                except FileNotFoundError:
                    pass
                service_path = timer_path.with_suffix(".service")
                try:
                    service_path.unlink()
                except FileNotFoundError:
                    pass

        for job_id, job in expected.items():
            unit_base = f"serverdeck-backup-{job_id}"
            service_path = self.systemd_dir / f"{unit_base}.service"
            timer_path = self.systemd_dir / f"{unit_base}.timer"
            service = "\n".join(
                [
                    "[Unit]",
                    f"Description=ServerDeck backup: {job['name']}",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    f"ExecStart={self._runner_command(job_id, shell_style=False)}",
                    "Nice=10",
                    "IOSchedulingClass=best-effort",
                    "IOSchedulingPriority=7",
                    "",
                ]
            )
            timer = "\n".join(
                [
                    "[Unit]",
                    f"Description=Schedule ServerDeck backup: {job['name']}",
                    "",
                    "[Timer]",
                    f"OnCalendar={systemd_calendar(job)}",
                    "Persistent=true",
                    f"Unit={unit_base}.service",
                    "",
                    "[Install]",
                    "WantedBy=timers.target",
                    "",
                ]
            )
            atomic_write(service_path, service, 0o644)
            atomic_write(timer_path, timer, 0o644)

        run_command(["systemctl", "daemon-reload"], timeout=30, check=True)
        for job_id in expected:
            result = run_command(["systemctl", "enable", "--now", f"serverdeck-backup-{job_id}.timer"], timeout=30)
            if result.returncode != 0:
                raise RuntimeError(result.stdout.strip() or "Could not enable timer")


class TimerSchedulerManager:
    """Install managed systemd services that run once during system startup."""

    def __init__(self, config: Config, store: TimerStore):
        self.config = config
        self.store = store
        self.lock = threading.RLock()
        self.systemd_dir = pathlib.Path("/etc/systemd/system")

    def capability(self) -> Dict[str, Any]:
        return {
            "root": self.config.is_root,
            "systemd_available": command_exists("systemctl") and pathlib.Path("/run/systemd/system").exists(),
        }

    def _runner_command(self, timer_id: str) -> str:
        args = [
            "/usr/bin/python3",
            str(self.config.script_path),
            "--run-autostart",
            timer_id,
            "--data-dir",
            str(self.config.data_dir),
        ]
        return " ".join(systemd_quote(arg) for arg in args)

    def _service_text(self, job: Dict[str, Any]) -> str:
        schedule = job.get("schedule", {})
        lines = [
            "[Unit]",
            f"Description=ServerDeck autostart: {job['name']}",
            "After=local-fs.target network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
        ]
        delay = int(schedule.get("boot_delay", 0))
        if delay > 0:
            lines.append(f"ExecStartPre=/bin/sleep {delay}")
        lines.extend(
            [
                f"ExecStart={self._runner_command(job['id'])}",
                "Nice=5",
                "IOSchedulingClass=best-effort",
                "IOSchedulingPriority=6",
                "TimeoutStartSec=infinity",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )
        return "\n".join(lines)

    def sync(self) -> List[str]:
        enabled = {job["id"]: job for job in self.store.list() if job.get("schedule", {}).get("enabled")}
        if not self.config.is_root:
            return ["Autostart items were saved but cannot be installed unless ServerDeck runs as root."] if enabled else []
        if not command_exists("systemctl") or not pathlib.Path("/run/systemd/system").exists():
            return ["Autostart items were saved, but systemd is not active on this machine."] if enabled else []

        warnings: List[str] = []
        with self.lock:
            expected_ids = set(enabled)

            # Remove every legacy recurring task timer. The Autostart page only
            # manages boot services and must never retain calendar schedules.
            for timer_path in self.systemd_dir.glob("serverdeck-task-*.timer"):
                run_command(["systemctl", "disable", "--now", timer_path.name], timeout=30)
                try:
                    timer_path.unlink()
                except FileNotFoundError:
                    pass

            for service_path in self.systemd_dir.glob("serverdeck-task-*.service"):
                item_id = service_path.stem.removeprefix("serverdeck-task-")
                if item_id not in expected_ids:
                    run_command(["systemctl", "disable", service_path.name], timeout=30)
                    try:
                        service_path.unlink()
                    except FileNotFoundError:
                        pass

            for item_id, job in enabled.items():
                service_path = self.systemd_dir / f"serverdeck-task-{item_id}.service"
                atomic_write(service_path, self._service_text(job), 0o644)

            reload_result = run_command(["systemctl", "daemon-reload"], timeout=30)
            if reload_result.returncode != 0:
                return [reload_result.stdout.strip() or "systemd daemon-reload failed"]

            for item_id, job in enabled.items():
                result = run_command(["systemctl", "enable", f"serverdeck-task-{item_id}.service"], timeout=30)
                if result.returncode != 0:
                    warnings.append(result.stdout.strip() or f"Could not enable {job['name']}")
        return warnings


class TaskRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def is_active(self, group: str) -> bool:
        with self.lock:
            return any(task.get("group") == group and task.get("active") for task in self.tasks.values())

    def start(self, kind: str, label: str, group: str, worker: Callable[[Callable[[str], None]], int]) -> Dict[str, Any]:
        with self.lock:
            if self.is_active(group):
                raise RuntimeError("A related operation is already running")
            task_id = uuid.uuid4().hex[:12]
            task: Dict[str, Any] = {
                "id": task_id,
                "kind": kind,
                "label": label,
                "group": group,
                "active": True,
                "output": "",
                "returncode": None,
                "error": None,
                "started_at": now_iso(),
                "finished_at": None,
            }
            self.tasks[task_id] = task
            while len(self.tasks) > 40:
                self.tasks.popitem(last=False)

        def write(text: str) -> None:
            with self.lock:
                task["output"] = (task["output"] + str(text))[-MAX_TASK_OUTPUT:]

        def run() -> None:
            try:
                returncode = worker(write)
                with self.lock:
                    task["returncode"] = int(returncode)
            except Exception as exc:
                write("\nERROR: " + str(exc) + "\n")
                with self.lock:
                    task["returncode"] = 1
                    task["error"] = str(exc)
            finally:
                with self.lock:
                    task["active"] = False
                    task["finished_at"] = now_iso()

        thread = threading.Thread(target=run, daemon=True, name=f"serverdeck-{kind}-{task_id}")
        thread.start()
        return self.snapshot(task_id) or task

    def snapshot(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def latest(self, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.lock:
            values = list(self.tasks.values())
            for task in reversed(values):
                if kind is None or task.get("kind") == kind:
                    return dict(task)
        return None


class UpdateManager:
    def __init__(self, tasks: TaskRegistry):
        self.tasks = tasks

    @staticmethod
    def available() -> Dict[str, Any]:
        if not command_exists("apt-get"):
            return {"supported": False, "error": "apt-get was not found", "packages": [], "count": 0}
        result = run_command(
            ["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"],
            timeout=90,
            env={**os.environ, "LC_ALL": "C"},
        )
        packages: List[Dict[str, str]] = []
        pattern = re.compile(r"^Inst\s+(\S+)(?:\s+\[([^\]]+)\])?\s+\((\S+)(?:\s+(.+?))?\)$")
        for line in result.stdout.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            packages.append(
                {
                    "name": match.group(1),
                    "current": match.group(2) or "unknown",
                    "candidate": match.group(3),
                    "source": match.group(4) or "",
                }
            )
        lists_dir = pathlib.Path("/var/lib/apt/lists")
        last_refresh: Optional[str] = None
        try:
            mtimes = [entry.stat().st_mtime for entry in lists_dir.iterdir() if entry.is_file()]
            if mtimes:
                last_refresh = dt.datetime.fromtimestamp(max(mtimes), dt.timezone.utc).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
        return {
            "supported": True,
            "error": None if result.returncode == 0 else result.stdout.strip(),
            "packages": packages,
            "count": len(packages),
            "last_refresh": last_refresh,
            "reboot_required": pathlib.Path("/var/run/reboot-required").exists(),
        }

    def refresh(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Refreshing package lists requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            return stream_command(["apt-get", "update"], write, env=apt_environment())

        return self.tasks.start("apt-refresh", "Refresh package lists", "apt", worker)

    def install(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Installing updates requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            code = stream_command(["apt-get", "update"], write, env=apt_environment())
            if code != 0:
                return code
            return stream_command(
                [
                    "apt-get",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    "upgrade",
                ],
                write,
                env=apt_environment(),
            )

        return self.tasks.start("apt-install", "Install system updates", "apt", worker)

    def install_rsync(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Installing rsync requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            code = stream_command(["apt-get", "update"], write, env=apt_environment())
            if code != 0:
                return code
            return stream_command(["apt-get", "install", "-y", "rsync"], write, env=apt_environment())

        return self.tasks.start("install-rsync", "Install rsync", "apt", worker)


def update_hosts_file(old_hostname: str, new_hostname: str) -> None:
    """Keep Debian/Ubuntu's local hostname mapping valid after a rename.

    sudo resolves the machine hostname before executing a command.  A stale or
    missing 127.0.1.1 entry therefore produces "unable to resolve host".
    Always create or replace that entry instead of only changing an exact old
    alias, because installations vary in whether they store a short name, FQDN,
    or no 127.0.1.1 line at all.
    """
    hosts_path = pathlib.Path("/etc/hosts")
    if not hosts_path.exists():
        return
    original = hosts_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    new_hostname = new_hostname.rstrip(".")
    new_short = new_hostname.split(".", 1)[0]
    wanted_aliases = [new_hostname]
    if new_short != new_hostname:
        wanted_aliases.append(new_short)

    replacement = "127.0.1.1\t" + " ".join(dict.fromkeys(wanted_aliases))
    found = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if fields and fields[0] == "127.0.1.1":
            lines[index] = replacement
            found = True
            break
    if not found:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(replacement)

    updated = "\n".join(lines).rstrip("\n") + "\n"
    if updated == original:
        return
    backup = hosts_path.with_name("hosts.serverdeck.bak")
    if not backup.exists():
        atomic_write(backup, original, 0o644)
    atomic_write(hosts_path, updated, 0o644)


def change_hostname(new_hostname: str) -> str:
    if os.geteuid() != 0:
        raise PermissionError("Changing the hostname requires root privileges")
    new_hostname = safe_text(new_hostname, 253).rstrip(".")
    if not HOSTNAME_RE.match(new_hostname):
        raise ValueError("Use letters, numbers, dots and hyphens; each label must start and end with a letter or number")
    old_hostname = socket.gethostname()
    if command_exists("hostnamectl"):
        result = run_command(["hostnamectl", "set-hostname", new_hostname], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stdout.strip() or "hostnamectl failed")
    else:
        atomic_write(pathlib.Path("/etc/hostname"), new_hostname + "\n", 0o644)
        result = run_command(["hostname", new_hostname], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stdout.strip() or "hostname command failed")
    update_hosts_file(old_hostname, new_hostname)
    return new_hostname


POWER_ACTIONS: Dict[str, Tuple[List[str], str]] = {
    "restart": (["systemctl", "--no-block", "reboot"], "Restart requested. The server will go offline briefly."),
    "shutdown": (["systemctl", "--no-block", "poweroff"], "Shutdown requested. The server will power off shortly."),
}


def schedule_power_action(action: str, confirmation: str) -> str:
    """Validate and queue a host restart or shutdown after the HTTP reply is sent."""
    if os.geteuid() != 0:
        raise PermissionError("Restart and shutdown require root privileges")
    action = safe_text(action, 16).lower()
    expected_confirmation = action.upper()
    if action not in POWER_ACTIONS:
        raise ValueError("Power action must be restart or shutdown")
    if not hmac.compare_digest(safe_text(confirmation, 16), expected_confirmation):
        raise ValueError(f"Power action confirmation must be {expected_confirmation}")
    if not command_exists("systemctl"):
        raise RuntimeError("systemctl is not available on this system")

    command, message = POWER_ACTIONS[action]

    def worker() -> None:
        # Give the HTTP response time to reach the browser before systemd stops
        # networking and the ServerDeck service.
        time.sleep(1.5)
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            print(f"Unable to execute power action {action}: {exc}", file=sys.stderr)

    threading.Thread(target=worker, name=f"serverdeck-power-{action}", daemon=True).start()
    return message


def backup_log_path(config: Config, job_id: str) -> pathlib.Path:
    return config.logs_dir / f"backup-{job_id}.log"


def run_backup_sync(config: Config, store: JobStore, job_id: str, write_extra: Optional[Callable[[str], None]] = None) -> int:
    job = store.get(job_id)
    if not job:
        raise ValueError("Backup job not found")
    if not command_exists("rsync"):
        raise RuntimeError("rsync is not installed")
    args = build_rsync_args(job)
    log_path = backup_log_path(config, job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        header = f"\n===== {now_iso()} | {job['name']} =====\n"
        log.write(header)
        log.flush()
        if write_extra:
            write_extra(header)

        def write(text: str) -> None:
            log.write(text)
            log.flush()
            if write_extra:
                write_extra(text)

        code = stream_command(args, write, env={**os.environ, "LC_ALL": "C"})
        footer = f"\nFinished with exit code {code} at {now_iso()}\n"
        write(footer)
        return code



def timer_log_path(config: Config, timer_id: str) -> pathlib.Path:
    return config.logs_dir / f"autostart-{timer_id}.log"


def run_timer_sync(config: Config, store: TimerStore, timer_id: str, write_extra: Optional[Callable[[str], None]] = None) -> int:
    job = store.get(timer_id)
    if not job:
        raise ValueError("Autostart item not found")
    args = build_timer_args(job)
    if not args:
        raise ValueError("Autostart command is empty")

    account = pwd.getpwnam(job["run_as_user"])
    if os.geteuid() != 0 and account.pw_uid != os.geteuid():
        raise PermissionError("Running an autostart item as another user requires root privileges")

    working_directory = job.get("working_directory") or account.pw_dir or "/"
    if not os.path.isdir(working_directory):
        raise ValueError(f"Working directory does not exist: {working_directory}")

    env = os.environ.copy()
    env.update(
        {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
    )
    command = list(args)
    if "/" not in command[0]:
        resolved = shutil.which(command[0], path=env["PATH"])
        if not resolved:
            raise RuntimeError(f"Executable was not found: {command[0]}")
        command[0] = resolved
    if os.geteuid() == 0 and account.pw_uid != 0:
        runuser = shutil.which("runuser")
        if not runuser:
            raise RuntimeError("runuser is required to execute a task as a non-root account")
        command = [runuser, "-u", account.pw_name, "--", *command]

    log_path = timer_log_path(config, timer_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        header = (
            f"\n===== {now_iso()} | {job['name']} | user={account.pw_name} =====\n"
            f"Command: {shlex.join(args)}\n"
            f"Working directory: {working_directory}\n"
        )
        log.write(header)
        log.flush()
        if write_extra:
            write_extra(header)

        def write(text: str) -> None:
            log.write(text)
            log.flush()
            if write_extra:
                write_extra(text)

        try:
            code = stream_command(command, write, env=env, cwd=working_directory)
        except FileNotFoundError as exc:
            write(f"ERROR: executable not found: {exc.filename or args[0]}\n")
            code = 127
        footer = f"\nFinished with exit code {code} at {now_iso()}\n"
        write(footer)
        return code


def read_log_tail(path: pathlib.Path, max_bytes: int = 200_000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "No log output yet."


def browse_directory(raw_path: str, include_files: bool = False) -> Dict[str, Any]:
    """Return folders, and optionally files, visible to ServerDeck."""
    requested = safe_text(raw_path or "/", 4096)
    expanded = os.path.expanduser(requested)
    if not os.path.isabs(expanded):
        raise ValueError("Folder browser paths must be absolute")

    original = os.path.normpath(os.path.abspath(expanded))
    current = original

    # A destination may not exist yet. Start in its nearest existing parent so
    # the user can still browse from a useful location.
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            raise ValueError("No accessible parent folder was found")
        current = parent

    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(current) as iterator:
            for entry in iterator:
                try:
                    is_directory = entry.is_dir(follow_symlinks=True)
                    if not is_directory and not include_files:
                        continue
                    if not is_directory and not entry.is_file(follow_symlinks=True):
                        continue
                    entries.append(
                        {
                            "name": entry.name,
                            "path": os.path.join(current, entry.name),
                            "symlink": entry.is_symlink(),
                            "directory": is_directory,
                        }
                    )
                except OSError:
                    # Some virtual filesystems contain entries that disappear or
                    # cannot be inspected between readdir and stat. Skip them.
                    continue
    except PermissionError as exc:
        raise PermissionError(f"Permission denied while reading {current}") from exc
    except OSError as exc:
        raise RuntimeError(f"Unable to read {current}: {exc.strerror or exc}") from exc

    entries.sort(key=lambda item: (not item.get("directory", True), item["name"].startswith("."), item["name"].casefold()))
    parent = os.path.dirname(current)
    return {
        "path": current,
        "parent": None if parent == current else parent,
        "entries": entries,
        "adjusted_from": original if original != current else None,
    }


CSS = r"""
:root{color-scheme:dark;--bg:#0b1020;--panel:#131a2d;--panel2:#192238;--border:#293552;--text:#eef3ff;--muted:#9da9c4;--accent:#65a7ff;--accent2:#7ce3c2;--danger:#ff6b7a;--warning:#ffc86b;--shadow:0 18px 50px rgba(0,0,0,.25)}
*{box-sizing:border-box}html{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0%,rgba(55,105,180,.22),transparent 35%),var(--bg)}button,input,select,textarea{font:inherit}button{cursor:pointer}.shell{max-width:1180px;margin:0 auto;padding:0 22px 50px}.topbar{position:sticky;top:0;z-index:10;background:rgba(11,16,32,.9);backdrop-filter:blur(16px);border-bottom:1px solid var(--border)}.topbar-inner{max-width:1180px;margin:0 auto;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.2px}.brand-mark{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;color:#07111f;font-weight:900}.top-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.nav{display:flex;gap:7px;flex-wrap:wrap}.nav a{color:var(--muted);text-decoration:none;padding:9px 13px;border-radius:9px;font-weight:650}.nav a:hover,.nav a.active{background:var(--panel2);color:var(--text)}.account{display:flex;align-items:center;gap:9px;padding-left:12px;border-left:1px solid var(--border)}.account-name{color:var(--muted);font-size:.86rem;font-weight:750}.logout-button{border:0;background:transparent;color:var(--accent);padding:6px;font-weight:750}.logout-button:hover{text-decoration:underline}.login-shell{min-height:100vh;display:grid;place-items:center;padding:22px}.login-card{width:min(430px,100%);background:linear-gradient(180deg,rgba(25,34,56,.98),rgba(19,26,45,.98));border:1px solid var(--border);border-radius:20px;padding:28px;box-shadow:var(--shadow)}.login-brand{display:flex;align-items:center;gap:12px;margin-bottom:24px}.login-card h1{margin:0 0 8px;font-size:1.8rem}.login-card p{color:var(--muted);line-height:1.5;margin:0 0 20px}.login-card .button{width:100%;margin-top:8px}main{padding-top:32px}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:24px}.hero h1{font-size:clamp(1.8rem,4vw,2.7rem);margin:0 0 8px}.hero p{margin:0;color:var(--muted);max-width:720px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}.card{background:linear-gradient(180deg,rgba(25,34,56,.96),rgba(19,26,45,.96));border:1px solid var(--border);border-radius:16px;padding:20px;box-shadow:var(--shadow)}.span-4{grid-column:span 4}.span-5{grid-column:span 5}.span-6{grid-column:span 6}.span-7{grid-column:span 7}.span-8{grid-column:span 8}.span-12{grid-column:span 12}.metric-label{font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:800}.metric-value{font-size:2rem;font-weight:850;margin:7px 0}.metric-detail{color:var(--muted);font-size:.92rem}.progress{height:9px;background:#0d1427;border-radius:999px;overflow:hidden;margin:16px 0 10px}.progress>span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:inherit;transition:width .35s ease}.section-title{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-bottom:15px}.section-title h2,.section-title h3{margin:0}.hostname-button{appearance:none;border:0;background:transparent;color:var(--text);padding:0;text-align:left}.hostname-button:hover .hostname{text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:4px}.hostname{font-size:1.7rem;font-weight:850}.pill{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:999px;font-size:.82rem;font-weight:750;background:#17243c;color:var(--muted);border:1px solid var(--border)}.pill.good{color:var(--accent2)}.pill.warn{color:var(--warning)}.pill.bad{color:var(--danger)}.button{border:1px solid var(--border);background:var(--panel2);color:var(--text);padding:10px 14px;border-radius:10px;font-weight:750;display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none}.button:hover{filter:brightness(1.12)}.button.primary{background:linear-gradient(135deg,#327de8,#5b9cff);border-color:#66a3ff}.button.danger{background:#3b1b28;border-color:#693043;color:#ffb9c0}.button.ghost{background:transparent}.button:disabled{opacity:.5;cursor:not-allowed}.actions{display:flex;gap:10px;flex-wrap:wrap}.notice{border:1px solid var(--border);background:rgba(25,34,56,.72);padding:13px 15px;border-radius:12px;color:var(--muted);line-height:1.45}.notice.warning{border-color:#6e552a;color:#ffd995;background:#2a2116}.notice.danger{border-color:#733141;color:#ffc0c7;background:#2c1620}.notice.good{border-color:#275e53;color:#a7f0d9;background:#122923}.stack{display:flex;flex-direction:column;gap:14px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.field{display:flex;flex-direction:column;gap:7px}.field.full{grid-column:1/-1}.field label{font-size:.88rem;color:var(--muted);font-weight:700}.input,.select,.textarea{width:100%;border:1px solid var(--border);background:#0e1528;color:var(--text);border-radius:10px;padding:11px 12px;outline:none}.input:focus,.select:focus,.textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(101,167,255,.13)}.input-with-button{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.browse-button{white-space:nowrap}.browser-box{width:min(720px,100%);max-height:min(760px,92vh);display:flex;flex-direction:column}.browser-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}.browser-path{padding:10px 12px;border:1px solid var(--border);border-radius:10px;background:#0e1528;overflow-wrap:anywhere;margin-bottom:12px}.browser-list{min-height:220px;max-height:420px;overflow:auto;border:1px solid var(--border);border-radius:12px;background:#0b1222}.browser-entry{width:100%;display:flex;align-items:center;gap:10px;text-align:left;border:0;border-bottom:1px solid var(--border);background:transparent;color:var(--text);padding:11px 13px}.browser-entry:last-child{border-bottom:0}.browser-entry:hover,.browser-entry:focus{background:var(--panel2);outline:none}.browser-entry-icon{color:var(--accent);font-weight:900}.browser-entry-name{overflow-wrap:anywhere}.browser-status{color:var(--muted);padding:36px 18px;text-align:center}.help{font-size:.8rem;color:var(--muted);line-height:1.45}.checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.check{display:flex;align-items:flex-start;gap:10px;border:1px solid var(--border);padding:11px;border-radius:11px;background:#11192c}.check input{margin-top:3px}.check strong{display:block;font-size:.92rem}.check small{display:block;color:var(--muted);margin-top:3px}.table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:650px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--border)}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;background:#11192c}tr:last-child td{border-bottom:0}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.output{background:#070b15;border:1px solid var(--border);border-radius:12px;padding:14px;min-height:180px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:#cfe0ff;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.job{border:1px solid var(--border);border-radius:14px;padding:16px;background:#11192c}.job-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.job h3{margin:0 0 5px}.path{color:var(--muted);font-size:.86rem;overflow-wrap:anywhere}.job-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.job-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.empty{text-align:center;color:var(--muted);padding:40px 20px}.modal{position:fixed;inset:0;background:rgba(3,6,13,.72);display:none;place-items:center;padding:20px;z-index:100}.modal.open{display:grid}.modal-box{width:min(500px,100%);background:var(--panel);border:1px solid var(--border);border-radius:17px;padding:22px;box-shadow:var(--shadow)}.modal-box h2{margin-top:0}.split{display:flex;align-items:center;justify-content:space-between;gap:12px}.spinner{width:17px;height:17px;border-radius:50%;border:2px solid rgba(255,255,255,.28);border-top-color:#fff;animation:spin .8s linear infinite;display:none}.busy .spinner{display:inline-block}@keyframes spin{to{transform:rotate(360deg)}}.footer{margin-top:35px;color:var(--muted);font-size:.82rem;text-align:center}.hidden{display:none!important}@media(max-width:820px){.span-4,.span-5,.span-6,.span-7,.span-8{grid-column:span 12}.hero{flex-direction:column}.topbar-inner{align-items:flex-start;flex-direction:column}.form-grid,.checks{grid-template-columns:1fr}.field.full{grid-column:auto}.shell{padding-left:14px;padding-right:14px}.topbar-inner{padding-left:14px;padding-right:14px}.top-actions,.account{width:100%}.account{padding-left:0;border-left:0;justify-content:space-between}}
"""

COMMON_JS = r"""
const csrf = document.querySelector('meta[name="csrf-token"]').content;
async function api(path, options={}) {
  const headers = Object.assign({'Accept':'application/json'}, options.headers || {});
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  if ((options.method || 'GET').toUpperCase() !== 'GET') headers['X-ServerDeck-Token'] = csrf;
  const response = await fetch(path, Object.assign({}, options, {headers}));
  if (response.status === 401) { window.location.assign('/login'); throw new Error('Your login session has expired'); }
  let data;
  try { data = await response.json(); } catch (_) { data = {error: await response.text()}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function esc(value) { const d=document.createElement('div'); d.textContent=value ?? ''; return d.innerHTML; }
function setBusy(button, busy=true) { if (!button) return; button.disabled=busy; button.classList.toggle('busy', busy); }
function showNotice(id, message, kind='') { const el=document.getElementById(id); if(!el)return; el.textContent=message; el.className='notice '+kind; el.classList.remove('hidden'); }
function hideNotice(id){ const el=document.getElementById(id); if(el)el.classList.add('hidden'); }
async function pollTask(taskId, outputId, done) {
  const output = document.getElementById(outputId);
  const timer = setInterval(async () => {
    try {
      const task = await api('/api/tasks/'+encodeURIComponent(taskId));
      if (output) { output.textContent = task.output || 'Starting…'; output.scrollTop=output.scrollHeight; }
      if (!task.active) { clearInterval(timer); if(done) done(task); }
    } catch (error) { clearInterval(timer); if(output) output.textContent += '\n'+error.message; }
  }, 900);
}
"""


def nav(active: str, username: str, csrf_token: str) -> str:
    items = [("overview", "/", "Overview"), ("updates", "/updates", "Updates"), ("backup", "/backup", "rSync"), ("autostart", "/autostart", "Autostart")]
    links = "".join(f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>' for key, href, label in items)
    account = (
        f'<div class="account"><span class="account-name">{html.escape(username)}</span>'
        f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf_token)}">'
        '<button class="logout-button" type="submit">Log out</button></form></div>'
    )
    return f'<div class="top-actions"><nav class="nav" aria-label="Primary navigation">{links}</nav>{account}</div>'


def page(title: str, active: str, body: str, script: str, csrf_token: str, username: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="csrf-token" content="{html.escape(csrf_token)}">'
        f'<title>{html.escape(title)} · {APP_NAME}</title><style>{CSS}</style></head>'
        f'<body><header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">SD</span><span>{APP_NAME}</span></div>'
        f'{nav(active, username, csrf_token)}</div></header>'
        f'<div class="shell"><main>{body}</main><div class="footer">{APP_NAME} {APP_VERSION} · Build {APP_BUILD} · Signed in as {html.escape(username)} · Keep this service on a trusted LAN, VPN, or HTTPS.</div></div>'
        f'<script>{COMMON_JS}\n{script}</script></body></html>'
    )


def login_page(error: str = "", username: str = "") -> str:
    notice = f'<div class="notice danger" style="margin-bottom:16px">{html.escape(error)}</div>' if error else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Log in · {APP_NAME}</title><style>{CSS}</style></head><body>'
        '<div class="login-shell"><main class="login-card">'
        f'<div class="login-brand"><span class="brand-mark">SD</span><strong>{APP_NAME}</strong></div>'
        '<h1>Log in to this server</h1><p>Use the username and password of a local Linux account that belongs to an authorised administrator group.</p>'
        f'{notice}<form method="post" action="/login">'
        f'<div class="field"><label for="username">Device username</label><input class="input" id="username" name="username" value="{html.escape(username)}" autocomplete="username" required autofocus maxlength="64"></div>'
        '<div class="field" style="margin-top:14px"><label for="password">Device password</label><input class="input" id="password" name="password" type="password" autocomplete="current-password" required></div>'
        '<button class="button primary" type="submit">Log in</button></form>'
        f'<div class="help" style="margin-top:18px">Passwords are checked by Linux PAM and are never stored by {APP_NAME}. Use HTTPS or a trusted VPN when accessing the page across a network.</div>'
        '</main></div></body></html>'
    )


OVERVIEW_BODY = r"""
<section class="hero"><div><h1>Server overview</h1><p>Live health information and basic identity settings for this machine.</p></div><span id="root-pill" class="pill">Checking access…</span></section>
<div id="overview-notice" class="notice hidden"></div>
<div class="grid" style="margin-top:18px">
  <article class="card span-4"><div class="metric-label">CPU usage</div><div class="metric-value" id="cpu-value">—</div><div class="progress"><span id="cpu-bar"></span></div><div class="metric-detail" id="cpu-detail">Reading processor activity…</div></article>
  <article class="card span-4"><div class="metric-label">Memory usage</div><div class="metric-value" id="mem-value">—</div><div class="progress"><span id="mem-bar"></span></div><div class="metric-detail" id="mem-detail">Reading memory…</div></article>
  <article class="card span-4"><div class="metric-label">System storage</div><div class="metric-value" id="disk-value">—</div><div class="progress"><span id="disk-bar"></span></div><div class="metric-detail" id="disk-detail">Reading root filesystem…</div></article>
  <article class="card span-7"><div class="section-title"><div><div class="metric-label">Hostname</div><button class="hostname-button" id="hostname-open" title="Click to rename"><span class="hostname" id="hostname">—</span></button></div><span class="pill">Click name to edit</span></div><div class="metric-detail" id="os-name">—</div></article>
  <article class="card span-5"><div class="metric-label">Uptime</div><div class="metric-value" id="uptime">—</div><div class="metric-detail" id="load">Load average: —</div></article>
  <article class="card span-12"><div class="split"><div><strong>Restart status</strong><div class="metric-detail" id="reboot-text">Checking…</div></div><span id="reboot-pill" class="pill">—</span></div></article>
  <article class="card span-12"><div class="section-title"><div><h2>Power controls</h2><div class="metric-detail">Restart or safely shut down this server. Active SSH sessions, backups and updates will be interrupted.</div></div><span class="pill warn">Administrator only</span></div><div class="actions"><button class="button" id="restart-btn"><span class="spinner"></span>Restart server</button><button class="button danger" id="shutdown-btn"><span class="spinner"></span>Shut down server</button></div><div id="power-notice" class="notice hidden" style="margin-top:14px"></div></article>
</div>
<div class="modal" id="hostname-modal" role="dialog" aria-modal="true" aria-labelledby="hostname-title"><div class="modal-box"><h2 id="hostname-title">Rename this server</h2><p class="metric-detail">Use a simple name such as <span class="mono">media-server</span>. Existing connections may need to reconnect using the new name.</p><div class="field"><label for="hostname-input">New hostname</label><input class="input" id="hostname-input" maxlength="253" autocomplete="off"></div><div id="hostname-error" class="notice danger hidden" style="margin-top:12px"></div><div class="actions" style="margin-top:18px"><button class="button primary" id="hostname-save"><span class="spinner"></span>Save hostname</button><button class="button ghost" id="hostname-cancel">Cancel</button></div></div></div>
"""

OVERVIEW_JS = r"""
let lastStats=null;
async function loadStats(){
  try{
    const data=await api('/api/stats'); lastStats=data;
    document.getElementById('cpu-value').textContent=data.cpu_percent.toFixed(1)+'%'; document.getElementById('cpu-bar').style.width=data.cpu_percent+'%'; document.getElementById('cpu-detail').textContent=data.cpu_count+' logical CPU'+(data.cpu_count===1?'':'s');
    document.getElementById('mem-value').textContent=data.memory.percent.toFixed(1)+'%'; document.getElementById('mem-bar').style.width=data.memory.percent+'%'; document.getElementById('mem-detail').textContent=data.memory.used_human+' used of '+data.memory.total_human;
    document.getElementById('disk-value').textContent=data.storage.percent.toFixed(1)+'%'; document.getElementById('disk-bar').style.width=data.storage.percent+'%'; document.getElementById('disk-detail').textContent=data.storage.free_human+' free of '+data.storage.total_human;
    document.getElementById('hostname').textContent=data.hostname; document.getElementById('os-name').textContent=data.os; document.getElementById('uptime').textContent=data.uptime.human; document.getElementById('load').textContent='Load average: '+data.load_average.join(' · ');
    const root=document.getElementById('root-pill'); root.textContent=data.is_root?'Administrator mode':'Read-only mode'; root.className='pill '+(data.is_root?'good':'warn');
    const reboot=document.getElementById('reboot-pill'); reboot.textContent=data.reboot_required?'Restart recommended':'No restart needed'; reboot.className='pill '+(data.reboot_required?'warn':'good'); document.getElementById('reboot-text').textContent=data.reboot_required?'Installed updates have requested a system restart.':'The system has not requested a restart.';
    hideNotice('overview-notice');
  }catch(error){showNotice('overview-notice',error.message,'danger')}
}
const modal=document.getElementById('hostname-modal');
document.getElementById('hostname-open').onclick=()=>{document.getElementById('hostname-input').value=lastStats?.hostname||''; modal.classList.add('open'); document.getElementById('hostname-input').focus();};
document.getElementById('hostname-cancel').onclick=()=>modal.classList.remove('open'); modal.addEventListener('click',e=>{if(e.target===modal)modal.classList.remove('open')});
document.getElementById('hostname-save').onclick=async function(){setBusy(this,true);hideNotice('hostname-error');try{const value=document.getElementById('hostname-input').value;await api('/api/hostname',{method:'POST',body:{hostname:value}});modal.classList.remove('open');showNotice('overview-notice','Hostname changed successfully.','good');await loadStats();}catch(error){showNotice('hostname-error',error.message,'danger')}finally{setBusy(this,false)}};
const restartButton=document.getElementById('restart-btn');
const shutdownButton=document.getElementById('shutdown-btn');
let statsTimer=null;
function setPowerButtonsDisabled(disabled){restartButton.disabled=disabled;shutdownButton.disabled=disabled;}
async function waitForRestart(){
  try{
    const response=await fetch('/api/stats',{cache:'no-store'});
    if(response.status===401){window.location.assign('/login');return}
    if(response.ok){window.location.reload();return}
  }catch(_){ }
  window.setTimeout(waitForRestart,3000);
}
async function requestPower(action){
  const restarting=action==='restart';
  const label=restarting?'restart':'shut down';
  const warning=restarting
    ?'Restart this server now? Active SSH sessions, updates and backup jobs will be interrupted.'
    :'Shut down this server now? It will remain unavailable until it is physically powered on again.';
  if(!window.confirm(warning))return;
  setPowerButtonsDisabled(true);
  setBusy(restarting?restartButton:shutdownButton,true);
  showNotice('power-notice','Sending '+label+' request…','warning');
  try{
    const result=await api('/api/power',{method:'POST',body:{action:action,confirmation:action.toUpperCase()}});
    if(statsTimer)window.clearInterval(statsTimer);
    showNotice('power-notice',restarting?result.message+' Waiting for ServerDeck to return…':result.message+' When its activity LEDs have settled, it is safe to disconnect power.','warning');
    if(restarting)window.setTimeout(waitForRestart,7000);
  }catch(error){
    showNotice('power-notice',error.message,'danger');
    setPowerButtonsDisabled(false);
    setBusy(restartButton,false);
    setBusy(shutdownButton,false);
  }
}
restartButton.onclick=()=>requestPower('restart');
shutdownButton.onclick=()=>requestPower('shutdown');
loadStats(); statsTimer=setInterval(loadStats,2500);
"""

UPDATES_BODY = r"""
<section class="hero"><div><h1>Updates</h1><p>Refresh Debian or Ubuntu package information, review available upgrades, and install them in one operation.</p></div><span id="update-count" class="pill">Checking…</span></section>
<div id="updates-notice" class="notice warning">Update installation uses <span class="mono">apt-get upgrade</span>, preserving existing configuration files where possible. Review backups before major changes.</div>
<div class="grid" style="margin-top:18px">
  <article class="card span-12"><div class="section-title"><div><h2>Available updates</h2><div class="metric-detail" id="refresh-time">Package list refresh: unknown</div></div><div class="actions"><button class="button" id="refresh-btn"><span class="spinner"></span>Refresh list</button><button class="button primary" id="install-btn"><span class="spinner"></span>Install all updates</button></div></div><div id="updates-table" class="table-wrap"><div class="empty">Checking for available packages…</div></div></article>
  <article class="card span-12"><div class="section-title"><h2>Operation output</h2><span id="task-state" class="pill">Idle</span></div><pre class="output" id="update-output">No update operation has been started in this browser session.</pre></article>
</div>
"""

UPDATES_JS = r"""
let latestTask=null;
async function loadUpdates(){
  try{
    const data=await api('/api/updates');
    const count=document.getElementById('update-count'); count.textContent=data.count+' update'+(data.count===1?'':'s'); count.className='pill '+(data.count?'warn':'good');
    document.getElementById('refresh-time').textContent='Package list refresh: '+(data.last_refresh?new Date(data.last_refresh).toLocaleString():'unknown');
    const holder=document.getElementById('updates-table');
    if(!data.supported){holder.innerHTML='<div class="empty">APT is not available on this system.</div>';return}
    if(data.error && !data.packages.length){holder.innerHTML='<div class="empty">'+esc(data.error)+'</div>';return}
    if(!data.packages.length){holder.innerHTML='<div class="empty">This server is up to date.</div>';return}
    holder.innerHTML='<table><thead><tr><th>Package</th><th>Installed</th><th>Available</th><th>Source</th></tr></thead><tbody>'+data.packages.map(p=>'<tr><td><strong>'+esc(p.name)+'</strong></td><td class="mono">'+esc(p.current)+'</td><td class="mono">'+esc(p.candidate)+'</td><td>'+esc(p.source)+'</td></tr>').join('')+'</tbody></table>';
  }catch(error){showNotice('updates-notice',error.message,'danger')}
}
function watch(task,button){latestTask=task.id;const state=document.getElementById('task-state');state.textContent='Running';state.className='pill warn';setBusy(button,true);pollTask(task.id,'update-output',async finished=>{setBusy(button,false);state.textContent=finished.returncode===0?'Completed':'Failed';state.className='pill '+(finished.returncode===0?'good':'bad');await loadUpdates();});}
document.getElementById('refresh-btn').onclick=async function(){try{watch(await api('/api/updates/refresh',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
document.getElementById('install-btn').onclick=async function(){if(!confirm('Install all currently available standard updates?'))return;try{watch(await api('/api/updates/install',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
loadUpdates();
"""

BACKUP_BODY = r"""
<section class="hero"><div><h1>rSync</h1><p>Create reusable rsync jobs, run them immediately, or schedule them with cron or systemd timers.</p></div><span id="rsync-pill" class="pill">Checking rsync…</span></section>
<div id="backup-notice" class="notice hidden"></div>
<div class="grid" style="margin-top:18px">
  <article class="card span-5"><div class="section-title"><h2 id="form-title">New rSync job</h2><button class="button ghost hidden" id="cancel-edit">Cancel edit</button></div>
    <form id="backup-form" class="stack">
      <input type="hidden" id="job-id">
      <div class="field"><label for="job-name">Job name</label><input class="input" id="job-name" maxlength="80" required placeholder="Photos to backup drive"></div>
      <div class="field"><label for="job-source">Source</label><div class="input-with-button"><input class="input mono" id="job-source" required placeholder="/srv/photos/"><button class="button browse-button" type="button" data-browse-target="job-source" data-browse-label="source">Browse</button></div><div class="help">Choose a local folder or type a path. A trailing slash copies the contents of a folder rather than the folder itself.</div></div>
      <div class="field"><label for="job-destination">Destination</label><div class="input-with-button"><input class="input mono" id="job-destination" required placeholder="/mnt/backup/photos/"><button class="button browse-button" type="button" data-browse-target="job-destination" data-browse-label="destination">Browse</button></div><div class="help">Choose a local folder or type a path. Remote rsync destinations such as <span class="mono">user@server:/path/</span> are also accepted.</div></div>
      <div><div class="metric-label" style="margin-bottom:10px">Rsync options</div><div class="checks">
        <label class="check"><input type="checkbox" id="opt-dry"><span><strong>--dry-run</strong><small>Show changes without copying.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-archive" checked><span><strong>--archive</strong><small>Preserve common file attributes.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-itemize" checked><span><strong>--itemize-changes</strong><small>Describe every changed item.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-verbose" checked><span><strong>--verbose</strong><small>Provide detailed output.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-human" checked><span><strong>--human-readable</strong><small>Use readable file sizes.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-progress" checked><span><strong>-P</strong><small>Keep partial files and show progress.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-update" checked><span><strong>--update</strong><small>Do not overwrite newer destination files.</small></span></label>
        <label class="check"><input type="checkbox" id="opt-chmod-enabled"><span><strong>--chmod=</strong><small>Apply a permission transform.</small></span></label>
      </div></div>
      <div class="field hidden" id="chmod-field"><label for="opt-chmod">--chmod value</label><input class="input mono" id="opt-chmod" placeholder="Du=rwx,Dgo=rx,Fu=rw,Fgo=r"></div>
      <div class="form-grid"><div class="field"><label for="scheduler-backend">Schedule using</label><select class="select" id="scheduler-backend"><option value="manual">Manual only</option><option value="systemd">Systemd timer</option><option value="cron">Cron</option></select></div><div class="field" id="preset-field"><label for="schedule-preset">Frequency</label><select class="select" id="schedule-preset"><option value="hourly">Hourly</option><option value="daily" selected>Daily at 02:00</option><option value="weekly">Weekly, Sunday 02:00</option><option value="monthly">Monthly, day 1 at 02:00</option><option value="custom">Custom</option></select></div></div>
      <div class="field hidden" id="expression-field"><label id="expression-label" for="schedule-expression">Custom schedule</label><input class="input mono" id="schedule-expression"><div class="help" id="expression-help"></div></div>
      <label class="check hidden" id="schedule-enabled-wrap"><input type="checkbox" id="schedule-enabled" checked><span><strong>Enable this schedule</strong><small>The timer or cron entry is installed when the job is saved.</small></span></label>
      <button class="button primary" type="submit" id="save-job"><span class="spinner"></span>Save job</button>
    </form>
  </article>
  <section class="span-7 stack"><article class="card"><div class="section-title"><div><h2>Saved jobs</h2><div class="metric-detail">Run, edit, inspect, or remove a job.</div></div><button class="button hidden" id="install-rsync"><span class="spinner"></span>Install rsync</button></div><div id="jobs-list" class="stack"><div class="empty">Loading jobs…</div></div></article>
  <article class="card"><div class="section-title"><h2>rSync output</h2><span id="backup-state" class="pill">Idle</span></div><pre class="output" id="backup-output">Select “Run now” or “View log” on a saved job.</pre></article></section>
</div>
<div class="modal" id="path-browser-modal" role="dialog" aria-modal="true" aria-labelledby="path-browser-title">
  <div class="modal-box browser-box">
    <div class="section-title"><div><h2 id="path-browser-title">Choose folder</h2><div class="metric-detail">Folders shown here are on the server.</div></div><button class="button ghost" type="button" id="path-browser-close">Close</button></div>
    <div class="browser-toolbar"><button class="button" type="button" id="path-browser-up">Up one folder</button><button class="button" type="button" id="path-browser-root">Root /</button></div>
    <div class="browser-path mono" id="path-browser-current">/</div>
    <div id="path-browser-notice" class="notice warning hidden" style="margin-bottom:12px"></div>
    <div class="browser-list" id="path-browser-list"><div class="browser-status">Loading folders…</div></div>
    <div class="actions" style="margin-top:16px"><button class="button primary" type="button" id="path-browser-select">Use this folder</button><button class="button ghost" type="button" id="path-browser-cancel">Cancel</button></div>
  </div>
</div>
"""

BACKUP_JS = r"""
let jobs=[];
const backend=document.getElementById('scheduler-backend'),preset=document.getElementById('schedule-preset');
function updateScheduleFields(){const manual=backend.value==='manual',custom=preset.value==='custom';document.getElementById('preset-field').classList.toggle('hidden',manual);document.getElementById('schedule-enabled-wrap').classList.toggle('hidden',manual);document.getElementById('expression-field').classList.toggle('hidden',manual||!custom);const cron=backend.value==='cron';document.getElementById('expression-label').textContent=cron?'Cron expression':'Systemd OnCalendar expression';document.getElementById('expression-help').textContent=cron?'Five fields, for example: 30 2 * * 1-5':'For example: Mon..Fri *-*-* 02:30:00';document.getElementById('schedule-expression').placeholder=cron?'30 2 * * 1-5':'Mon..Fri *-*-* 02:30:00';}
backend.onchange=updateScheduleFields;preset.onchange=updateScheduleFields;document.getElementById('opt-chmod-enabled').onchange=e=>document.getElementById('chmod-field').classList.toggle('hidden',!e.target.checked);
const pathBrowserModal=document.getElementById('path-browser-modal'),pathBrowserList=document.getElementById('path-browser-list'),pathBrowserCurrent=document.getElementById('path-browser-current'),pathBrowserUp=document.getElementById('path-browser-up');
let pathBrowserTarget=null,pathBrowserPath='/',pathBrowserParent=null;
function closePathBrowser(){pathBrowserModal.classList.remove('open');pathBrowserTarget=null;}
function browserStartingPath(value){const path=(value||'').trim();if(!path||!path.startsWith('/')||path.includes('://'))return '/';return path;}
async function loadPathBrowser(path){pathBrowserList.innerHTML='<div class="browser-status">Loading folders…</div>';document.getElementById('path-browser-select').disabled=true;hideNotice('path-browser-notice');try{const data=await api('/api/filesystem?path='+encodeURIComponent(path));pathBrowserPath=data.path;pathBrowserParent=data.parent;pathBrowserCurrent.textContent=data.path;pathBrowserUp.disabled=!data.parent;document.getElementById('path-browser-select').disabled=false;if(data.adjusted_from)showNotice('path-browser-notice','That path does not exist yet. Showing the nearest existing folder: '+data.path,'warning');pathBrowserList.textContent='';if(!data.entries.length){pathBrowserList.innerHTML='<div class="browser-status">No subfolders are visible here.</div>';return}for(const entry of data.entries){const button=document.createElement('button');button.type='button';button.className='browser-entry';const icon=document.createElement('span');icon.className='browser-entry-icon';icon.textContent='▸';const name=document.createElement('span');name.className='browser-entry-name mono';name.textContent=entry.name+(entry.symlink?' →':'');button.append(icon,name);button.onclick=()=>loadPathBrowser(entry.path);pathBrowserList.appendChild(button)}}catch(error){pathBrowserList.innerHTML='<div class="browser-status">Unable to display this folder.</div>';showNotice('path-browser-notice',error.message,'danger');pathBrowserUp.disabled=true;}}
function openPathBrowser(targetId,label){pathBrowserTarget=document.getElementById(targetId);document.getElementById('path-browser-title').textContent='Choose '+label+' folder';pathBrowserModal.classList.add('open');loadPathBrowser(browserStartingPath(pathBrowserTarget.value));}
document.querySelectorAll('[data-browse-target]').forEach(button=>button.onclick=()=>openPathBrowser(button.dataset.browseTarget,button.dataset.browseLabel));
document.getElementById('path-browser-up').onclick=()=>{if(pathBrowserParent)loadPathBrowser(pathBrowserParent)};
document.getElementById('path-browser-root').onclick=()=>loadPathBrowser('/');
document.getElementById('path-browser-select').onclick=()=>{if(pathBrowserTarget){pathBrowserTarget.value=pathBrowserPath;pathBrowserTarget.dispatchEvent(new Event('change',{bubbles:true}))}closePathBrowser()};
document.getElementById('path-browser-close').onclick=closePathBrowser;document.getElementById('path-browser-cancel').onclick=closePathBrowser;pathBrowserModal.addEventListener('click',event=>{if(event.target===pathBrowserModal)closePathBrowser()});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&pathBrowserModal.classList.contains('open'))closePathBrowser()});
function resetForm(){document.getElementById('backup-form').reset();document.getElementById('job-id').value='';document.getElementById('form-title').textContent='New rSync job';document.getElementById('cancel-edit').classList.add('hidden');document.getElementById('opt-archive').checked=true;document.getElementById('opt-itemize').checked=true;document.getElementById('opt-verbose').checked=true;document.getElementById('opt-human').checked=true;document.getElementById('opt-progress').checked=true;document.getElementById('opt-update').checked=true;backend.value='manual';preset.value='daily';document.getElementById('schedule-enabled').checked=true;document.getElementById('chmod-field').classList.add('hidden');updateScheduleFields();}
function payload(){return {name:document.getElementById('job-name').value,source:document.getElementById('job-source').value,destination:document.getElementById('job-destination').value,options:{dry_run:document.getElementById('opt-dry').checked,archive:document.getElementById('opt-archive').checked,itemize_changes:document.getElementById('opt-itemize').checked,verbose:document.getElementById('opt-verbose').checked,human_readable:document.getElementById('opt-human').checked,partial_progress:document.getElementById('opt-progress').checked,update:document.getElementById('opt-update').checked,chmod_enabled:document.getElementById('opt-chmod-enabled').checked,chmod:document.getElementById('opt-chmod').value},scheduler:{backend:backend.value,enabled:document.getElementById('schedule-enabled').checked,preset:preset.value,expression:document.getElementById('schedule-expression').value}}}
async function loadJobs(){try{const data=await api('/api/backups');jobs=data.jobs;const pill=document.getElementById('rsync-pill');pill.textContent=data.rsync_available?'rsync ready':'rsync not installed';pill.className='pill '+(data.rsync_available?'good':'warn');document.getElementById('install-rsync').classList.toggle('hidden',data.rsync_available||!data.capabilities.root);renderJobs();if(data.warnings?.length)showNotice('backup-notice',data.warnings.join(' '),'warning')}catch(error){showNotice('backup-notice',error.message,'danger')}}
function scheduleText(job){const s=job.scheduler;if(!s.enabled||s.backend==='manual')return 'Manual only';const labels={hourly:'Hourly',daily:'Daily',weekly:'Weekly',monthly:'Monthly',custom:s.expression};return (s.backend==='systemd'?'Systemd':'Cron')+' · '+labels[s.preset]}
function optionsText(job){const map=[['dry_run','dry-run'],['archive','archive'],['itemize_changes','itemize'],['verbose','verbose'],['human_readable','human-readable'],['partial_progress','-P'],['update','update']];const values=map.filter(([k])=>job.options[k]).map(([,v])=>v);if(job.options.chmod_enabled)values.push('chmod='+job.options.chmod);return values.join(' · ')||'No optional flags'}
function renderJobs(){const holder=document.getElementById('jobs-list');if(!jobs.length){holder.innerHTML='<div class="empty">No rSync jobs have been created.</div>';return}holder.innerHTML=jobs.map(job=>'<div class="job"><div class="job-head"><div><h3>'+esc(job.name)+'</h3><div class="path mono">'+esc(job.source)+' → '+esc(job.destination)+'</div></div><span class="pill '+(job.scheduler.enabled?'good':'')+'">'+esc(scheduleText(job))+'</span></div><div class="job-meta"><span class="pill">'+esc(optionsText(job))+'</span></div><div class="job-actions"><button class="button primary" onclick="runJob(\''+job.id+'\')">Run now</button><button class="button" onclick="editJob(\''+job.id+'\')">Edit</button><button class="button" onclick="viewLog(\''+job.id+'\')">View log</button><button class="button danger" onclick="deleteJob(\''+job.id+'\')">Delete</button></div></div>').join('')}
window.editJob=id=>{const job=jobs.find(j=>j.id===id);if(!job)return;document.getElementById('job-id').value=job.id;document.getElementById('job-name').value=job.name;document.getElementById('job-source').value=job.source;document.getElementById('job-destination').value=job.destination;for(const [id,key] of [['opt-dry','dry_run'],['opt-archive','archive'],['opt-itemize','itemize_changes'],['opt-verbose','verbose'],['opt-human','human_readable'],['opt-progress','partial_progress'],['opt-update','update'],['opt-chmod-enabled','chmod_enabled']])document.getElementById(id).checked=!!job.options[key];document.getElementById('opt-chmod').value=job.options.chmod||'';document.getElementById('chmod-field').classList.toggle('hidden',!job.options.chmod_enabled);backend.value=job.scheduler.backend;preset.value=job.scheduler.preset;document.getElementById('schedule-enabled').checked=job.scheduler.enabled;document.getElementById('schedule-expression').value=job.scheduler.expression||'';document.getElementById('form-title').textContent='Edit rSync job';document.getElementById('cancel-edit').classList.remove('hidden');updateScheduleFields();window.scrollTo({top:0,behavior:'smooth'})};
window.deleteJob=async id=>{if(!confirm('Delete this backup job and its installed schedule?'))return;try{await api('/api/backups/'+id,{method:'DELETE'});showNotice('backup-notice','Backup job deleted.','good');await loadJobs()}catch(error){showNotice('backup-notice',error.message,'danger')}};
window.runJob=async id=>{try{const task=await api('/api/backups/'+id+'/run',{method:'POST'});const state=document.getElementById('backup-state');state.textContent='Running';state.className='pill warn';document.getElementById('backup-output').textContent='Starting backup…';pollTask(task.id,'backup-output',finished=>{state.textContent=finished.returncode===0?'Completed':'Failed';state.className='pill '+(finished.returncode===0?'good':'bad')})}catch(error){showNotice('backup-notice',error.message,'danger')}};
window.viewLog=async id=>{try{const data=await api('/api/backups/'+id+'/log');document.getElementById('backup-output').textContent=data.log;document.getElementById('backup-output').scrollTop=document.getElementById('backup-output').scrollHeight}catch(error){showNotice('backup-notice',error.message,'danger')}};
document.getElementById('backup-form').onsubmit=async e=>{e.preventDefault();const button=document.getElementById('save-job'),id=document.getElementById('job-id').value;setBusy(button,true);try{const result=await api('/api/backups'+(id?'/'+id:''),{method:id?'PUT':'POST',body:payload()});showNotice('backup-notice','Backup job saved.'+(result.warnings?.length?' '+result.warnings.join(' '):''),result.warnings?.length?'warning':'good');resetForm();await loadJobs()}catch(error){showNotice('backup-notice',error.message,'danger')}finally{setBusy(button,false)}};
document.getElementById('cancel-edit').onclick=resetForm;
document.getElementById('install-rsync').onclick=async function(){setBusy(this,true);try{const task=await api('/api/rsync/install',{method:'POST'});document.getElementById('backup-state').textContent='Installing';pollTask(task.id,'backup-output',async finished=>{setBusy(this,false);document.getElementById('backup-state').textContent=finished.returncode===0?'Installed':'Failed';await loadJobs()})}catch(error){setBusy(this,false);showNotice('backup-notice',error.message,'danger')}};
resetForm();loadJobs();
"""


AUTOSTART_BODY = r"""
<section class="hero"><div><h1>Autostart</h1><p>Add Python scripts or system commands that should run automatically whenever this server starts.</p></div><span id="autostart-pill" class="pill">Checking systemd…</span></section>
<div id="autostart-notice" class="notice warning">Startup commands can modify files or affect services, especially when run as root. Test each item with “Run now” before enabling it for startup.</div>
<div class="grid" style="margin-top:18px">
  <article class="card span-5"><div class="section-title"><h2 id="autostart-form-title">New autostart item</h2><button class="button ghost hidden" id="autostart-cancel-edit" type="button">Cancel edit</button></div>
    <form id="autostart-form" class="stack">
      <input type="hidden" id="autostart-id">
      <div class="field"><label for="autostart-name">Item name</label><input class="input" id="autostart-name" maxlength="80" required placeholder="Start media indexer"></div>
      <div class="form-grid"><div class="field"><label for="autostart-kind">Type</label><select class="select" id="autostart-kind"><option value="python">Python script</option><option value="command">System command</option></select></div><div class="field"><label for="autostart-user">Run as user</label><select class="select" id="autostart-user"></select></div></div>
      <div id="autostart-python-fields" class="stack">
        <div class="field"><label for="autostart-script">Python script</label><div class="input-with-button"><input class="input mono" id="autostart-script" placeholder="/opt/scripts/start_service.py"><button class="button browse-button" type="button" data-autostart-browse="file" data-target="autostart-script">Browse</button></div></div>
        <div class="field"><label for="autostart-interpreter">Python interpreter</label><input class="input mono" id="autostart-interpreter" value="/usr/bin/python3"></div>
        <div class="field"><label for="autostart-arguments">Script arguments</label><input class="input mono" id="autostart-arguments" placeholder="--config /etc/myapp/config.json --quiet"><div class="help">Normal command-line quoting is supported, such as <span class="mono">--name &quot;Media Library&quot;</span>.</div></div>
      </div>
      <div id="autostart-command-fields" class="field hidden"><label for="autostart-command">Command and arguments</label><textarea class="textarea mono" id="autostart-command" rows="3" placeholder="/usr/local/bin/my-service --start"></textarea><div class="help">Commands are executed directly, not through a shell. Pipes, variables and redirection require an explicit command such as <span class="mono">/bin/sh -c '...'</span>.</div></div>
      <div class="field"><label for="autostart-working-directory">Working directory (optional)</label><div class="input-with-button"><input class="input mono" id="autostart-working-directory" placeholder="Uses the selected user's home folder"><button class="button browse-button" type="button" data-autostart-browse="folder" data-target="autostart-working-directory">Browse</button></div></div>
      <div class="field"><label for="autostart-delay">Startup delay (seconds)</label><input class="input" id="autostart-delay" type="number" min="0" max="3600" value="30"><div class="help">Useful when the command needs networking, disks, or other services to finish starting first.</div></div>
      <label class="check"><input type="checkbox" id="autostart-enabled" checked><span><strong>Enable at startup</strong><small>Install the systemd service when this item is saved.</small></span></label>
      <button class="button primary" type="submit" id="autostart-save"><span class="spinner"></span>Save autostart item</button>
    </form>
  </article>
  <section class="span-7 stack"><article class="card"><div class="section-title"><div><h2>Saved autostart items</h2><div class="metric-detail">Enabled items run once during each system startup.</div></div></div><div id="autostart-list" class="stack"><div class="empty">Loading autostart items…</div></div></article>
  <article class="card"><div class="section-title"><h2>Command output</h2><span id="autostart-state" class="pill">Idle</span></div><pre class="output" id="autostart-output">Select “Run now” or “View log” on a saved item.</pre></article></section>
</div>
<div class="modal" id="autostart-browser-modal" role="dialog" aria-modal="true" aria-labelledby="autostart-browser-title"><div class="modal-box browser-box"><div class="section-title"><div><h2 id="autostart-browser-title">Choose location</h2><div class="metric-detail" id="autostart-browser-subtitle">Items shown here are on the server.</div></div><button class="button ghost" type="button" id="autostart-browser-close">Close</button></div><div class="browser-toolbar"><button class="button" type="button" id="autostart-browser-up">Up one folder</button><button class="button" type="button" id="autostart-browser-root">Root /</button></div><div class="browser-path mono" id="autostart-browser-current">/</div><div id="autostart-browser-notice" class="notice warning hidden" style="margin-bottom:12px"></div><div class="browser-list" id="autostart-browser-list"><div class="browser-status">Loading…</div></div><div class="actions" style="margin-top:16px"><button class="button primary" type="button" id="autostart-browser-select">Use selection</button><button class="button ghost" type="button" id="autostart-browser-cancel">Cancel</button></div></div></div>
"""

AUTOSTART_JS = r"""
let autostartJobs=[],autostartCurrentUser='root';
const autostartKind=document.getElementById('autostart-kind');
function updateAutostartFields(){const python=autostartKind.value==='python';document.getElementById('autostart-python-fields').classList.toggle('hidden',!python);document.getElementById('autostart-command-fields').classList.toggle('hidden',python)}
autostartKind.onchange=updateAutostartFields;
const autostartBrowserModal=document.getElementById('autostart-browser-modal'),autostartBrowserList=document.getElementById('autostart-browser-list'),autostartBrowserCurrent=document.getElementById('autostart-browser-current'),autostartBrowserUp=document.getElementById('autostart-browser-up'),autostartBrowserSelect=document.getElementById('autostart-browser-select');
let autostartBrowserTarget=null,autostartBrowserMode='folder',autostartBrowserPath='/',autostartBrowserParent=null,autostartSelectedFile='';
function closeAutostartBrowser(){autostartBrowserModal.classList.remove('open');autostartBrowserTarget=null}
function autostartBrowserStart(value){const path=(value||'').trim();if(!path||!path.startsWith('/'))return '/';return path}
async function loadAutostartBrowser(path){autostartBrowserList.innerHTML='<div class="browser-status">Loading…</div>';autostartBrowserSelect.disabled=autostartBrowserMode==='file';hideNotice('autostart-browser-notice');try{const data=await api('/api/filesystem?path='+encodeURIComponent(path)+(autostartBrowserMode==='file'?'&files=1':''));autostartBrowserPath=data.path;autostartBrowserParent=data.parent;autostartBrowserCurrent.textContent=data.path;autostartBrowserUp.disabled=!data.parent;if(data.adjusted_from)showNotice('autostart-browser-notice','Showing the nearest existing folder: '+data.path,'warning');autostartBrowserList.textContent='';if(autostartBrowserMode==='folder')autostartBrowserSelect.disabled=false;if(!data.entries.length){autostartBrowserList.innerHTML='<div class="browser-status">No matching items are visible here.</div>';return}for(const entry of data.entries){const button=document.createElement('button');button.type='button';button.className='browser-entry';const icon=document.createElement('span');icon.className='browser-entry-icon';icon.textContent=entry.directory?'▸':'PY';const name=document.createElement('span');name.className='browser-entry-name mono';name.textContent=entry.name+(entry.symlink?' →':'');button.append(icon,name);if(entry.directory){button.onclick=()=>loadAutostartBrowser(entry.path)}else{button.onclick=()=>{autostartSelectedFile=entry.path;autostartBrowserCurrent.textContent=entry.path;autostartBrowserList.querySelectorAll('.browser-entry').forEach(item=>item.style.background='');button.style.background='var(--panel2)';autostartBrowserSelect.disabled=false}}autostartBrowserList.appendChild(button)}}catch(error){autostartBrowserList.innerHTML='<div class="browser-status">Unable to display this location.</div>';showNotice('autostart-browser-notice',error.message,'danger')}}
function openAutostartBrowser(button){autostartBrowserTarget=document.getElementById(button.dataset.target);autostartBrowserMode=button.dataset.autostartBrowse;autostartSelectedFile='';document.getElementById('autostart-browser-title').textContent=autostartBrowserMode==='file'?'Choose Python script':'Choose working directory';document.getElementById('autostart-browser-subtitle').textContent='Items shown here are on the server.';autostartBrowserSelect.textContent=autostartBrowserMode==='file'?'Use this file':'Use this folder';autostartBrowserModal.classList.add('open');loadAutostartBrowser(autostartBrowserStart(autostartBrowserTarget.value))}
document.querySelectorAll('[data-autostart-browse]').forEach(button=>button.onclick=()=>openAutostartBrowser(button));autostartBrowserUp.onclick=()=>{if(autostartBrowserParent)loadAutostartBrowser(autostartBrowserParent)};document.getElementById('autostart-browser-root').onclick=()=>loadAutostartBrowser('/');autostartBrowserSelect.onclick=()=>{if(autostartBrowserTarget)autostartBrowserTarget.value=autostartBrowserMode==='file'?autostartSelectedFile:autostartBrowserPath;closeAutostartBrowser()};document.getElementById('autostart-browser-close').onclick=closeAutostartBrowser;document.getElementById('autostart-browser-cancel').onclick=closeAutostartBrowser;autostartBrowserModal.addEventListener('click',event=>{if(event.target===autostartBrowserModal)closeAutostartBrowser()});
function autostartPayload(){return {name:document.getElementById('autostart-name').value,kind:autostartKind.value,script:document.getElementById('autostart-script').value,interpreter:document.getElementById('autostart-interpreter').value,arguments:document.getElementById('autostart-arguments').value,command:document.getElementById('autostart-command').value,working_directory:document.getElementById('autostart-working-directory').value,run_as_user:document.getElementById('autostart-user').value,schedule:{enabled:document.getElementById('autostart-enabled').checked,trigger:'boot',boot_delay:Number(document.getElementById('autostart-delay').value||0)}}}
function resetAutostartForm(){document.getElementById('autostart-form').reset();const userSelect=document.getElementById('autostart-user');if([...userSelect.options].some(option=>option.value===autostartCurrentUser))userSelect.value=autostartCurrentUser;else if([...userSelect.options].some(option=>option.value==='root'))userSelect.value='root';document.getElementById('autostart-id').value='';document.getElementById('autostart-form-title').textContent='New autostart item';document.getElementById('autostart-cancel-edit').classList.add('hidden');autostartKind.value='python';document.getElementById('autostart-interpreter').value='/usr/bin/python3';document.getElementById('autostart-delay').value='30';document.getElementById('autostart-enabled').checked=true;updateAutostartFields()}
function autostartStatus(job){const s=job.schedule||{};return s.enabled?'At startup · '+(s.boot_delay||0)+'s delay':'Disabled'}
function renderAutostart(){const holder=document.getElementById('autostart-list');if(!autostartJobs.length){holder.innerHTML='<div class="empty">No autostart items have been created.</div>';return}holder.innerHTML=autostartJobs.map(job=>'<div class="job"><div class="job-head"><div><h3>'+esc(job.name)+'</h3><div class="path mono">'+esc(job.display_command)+'</div></div><span class="pill '+(job.schedule.enabled?'good':'')+'">'+esc(autostartStatus(job))+'</span></div><div class="job-meta"><span class="pill">Run as '+esc(job.run_as_user)+'</span>'+(job.working_directory?'<span class="pill mono">cwd '+esc(job.working_directory)+'</span>':'')+'</div><div class="job-actions"><button class="button primary" onclick="runAutostart(\''+job.id+'\')">Run now</button><button class="button" onclick="editAutostart(\''+job.id+'\')">Edit</button><button class="button" onclick="viewAutostartLog(\''+job.id+'\')">View log</button><button class="button danger" onclick="deleteAutostart(\''+job.id+'\')">Delete</button></div></div>').join('')}
async function loadAutostart(){try{const data=await api('/api/autostart');autostartCurrentUser=data.current_user||'root';autostartJobs=data.jobs;const userSelect=document.getElementById('autostart-user'),previous=userSelect.value;userSelect.innerHTML=data.users.map(user=>'<option value="'+esc(user)+'">'+esc(user)+'</option>').join('');if(data.users.includes(previous))userSelect.value=previous;else if(data.users.includes(data.current_user))userSelect.value=data.current_user;else if(data.users.includes('root'))userSelect.value='root';const pill=document.getElementById('autostart-pill');pill.textContent=data.capabilities.systemd_available?'systemd ready':'systemd unavailable';pill.className='pill '+(data.capabilities.systemd_available?'good':'warn');renderAutostart();if(data.warnings?.length)showNotice('autostart-notice',data.warnings.join(' '),'warning')}catch(error){showNotice('autostart-notice',error.message,'danger')}}
window.editAutostart=id=>{const job=autostartJobs.find(item=>item.id===id);if(!job)return;document.getElementById('autostart-id').value=job.id;document.getElementById('autostart-name').value=job.name;autostartKind.value=job.kind;document.getElementById('autostart-script').value=job.script||'';document.getElementById('autostart-interpreter').value=job.interpreter||'/usr/bin/python3';document.getElementById('autostart-arguments').value=job.arguments||'';document.getElementById('autostart-command').value=job.command||'';document.getElementById('autostart-working-directory').value=job.working_directory||'';document.getElementById('autostart-user').value=job.run_as_user;document.getElementById('autostart-delay').value=job.schedule?.boot_delay??30;document.getElementById('autostart-enabled').checked=!!job.schedule?.enabled;document.getElementById('autostart-form-title').textContent='Edit autostart item';document.getElementById('autostart-cancel-edit').classList.remove('hidden');updateAutostartFields();window.scrollTo({top:0,behavior:'smooth'})};
window.deleteAutostart=async id=>{if(!confirm('Delete this autostart item and remove its installed startup service?'))return;try{const result=await api('/api/autostart/'+id,{method:'DELETE'});showNotice('autostart-notice','Autostart item deleted.'+(result.warnings?.length?' '+result.warnings.join(' '):''),result.warnings?.length?'warning':'good');await loadAutostart()}catch(error){showNotice('autostart-notice',error.message,'danger')}};
window.runAutostart=async id=>{try{const task=await api('/api/autostart/'+id+'/run',{method:'POST'});const state=document.getElementById('autostart-state');state.textContent='Running';state.className='pill warn';document.getElementById('autostart-output').textContent='Starting command…';pollTask(task.id,'autostart-output',finished=>{state.textContent=finished.returncode===0?'Completed':'Failed';state.className='pill '+(finished.returncode===0?'good':'bad')})}catch(error){showNotice('autostart-notice',error.message,'danger')}};
window.viewAutostartLog=async id=>{try{const data=await api('/api/autostart/'+id+'/log');const output=document.getElementById('autostart-output');output.textContent=data.log;output.scrollTop=output.scrollHeight}catch(error){showNotice('autostart-notice',error.message,'danger')}};
document.getElementById('autostart-form').onsubmit=async event=>{event.preventDefault();const button=document.getElementById('autostart-save'),id=document.getElementById('autostart-id').value;setBusy(button,true);try{const result=await api('/api/autostart'+(id?'/'+id:''),{method:id?'PUT':'POST',body:autostartPayload()});showNotice('autostart-notice','Autostart item saved.'+(result.warnings?.length?' '+result.warnings.join(' '):''),result.warnings?.length?'warning':'good');resetAutostartForm();await loadAutostart()}catch(error){showNotice('autostart-notice',error.message,'danger')}finally{setBusy(button,false)}};document.getElementById('autostart-cancel-edit').onclick=resetAutostartForm;resetAutostartForm();loadAutostart();
"""


class ServerDeckServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], handler: type, app: "Application"):
        self.app = app
        super().__init__(address, handler)


class Application:
    def __init__(self, config: Config):
        self.config = config
        self.stats = SystemStats()
        self.jobs = JobStore(config.jobs_file)
        self.timer_jobs = TimerStore(config.timers_file)
        self.tasks = TaskRegistry()
        self.updates = UpdateManager(self.tasks)
        self.scheduler = SchedulerManager(config, self.jobs)
        self.timer_scheduler = TimerSchedulerManager(config, self.timer_jobs)
        self.sessions = SessionStore(config.session_ttl)
        self.no_auth_session = {"username": "local", "csrf": secrets.token_urlsafe(32)}
        self.pam = PAMAuthenticator() if config.auth_mode == "pam" else None
        self.startup_warnings = self.scheduler.sync()
        self.timer_startup_warnings = self.timer_scheduler.sync()

    def authenticate_credentials(self, username: str, password: str) -> bool:
        username = username.strip()
        if self.config.auth_mode == "pam":
            if not account_is_authorized(username, self.config.auth_groups):
                return False
            assert self.pam is not None
            try:
                return self.pam.authenticate(username, password)
            except Exception as exc:
                print(f"PAM authentication error: {exc}", file=sys.stderr)
                return False
        if self.config.auth_mode == "static":
            return hmac.compare_digest(username, self.config.username) and hmac.compare_digest(password, self.config.password)
        return True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"ServerDeck/{APP_VERSION}"
    sys_version = ""

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.client_address[0]} {fmt % args}\n")

    def _headers(self, content_type: str, length: int, status: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-ServerDeck-Version", APP_VERSION)
        self.send_header("X-ServerDeck-Build", APP_BUILD)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self._headers(content_type, len(data), status, extra_headers)
        self.wfile.write(data)

    def send_html(self, content: str, status: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_bytes(content.encode("utf-8"), "text/html; charset=utf-8", status, extra_headers)

    def send_json(self, payload: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def redirect(self, location: str, extra_headers: Optional[Dict[str, str]] = None) -> None:
        headers = {"Location": location}
        headers.update(extra_headers or {})
        self.send_bytes(b"", "text/plain; charset=utf-8", 303, headers)

    def error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def cookie_token(self) -> str:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def authenticate(self) -> bool:
        if self.app.config.no_auth:
            self.current_session = dict(self.app.no_auth_session)
            self.current_token = ""
            return True
        token = self.cookie_token()
        session = self.app.sessions.get(token)
        if session:
            self.current_session = session
            self.current_token = token
            return True
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            self.error_json(401, "Login required")
        else:
            self.redirect("/login")
        return False

    def require_csrf(self, form_token: str = "") -> bool:
        provided = form_token or self.headers.get("X-ServerDeck-Token", "")
        expected = getattr(self, "current_session", {}).get("csrf", "")
        if expected and hmac.compare_digest(provided, expected):
            return True
        self.error_json(403, "Security token missing or invalid; reload the page and try again")
        return False

    def read_json(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Invalid request length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Invalid JSON request")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value


    def read_form(self) -> Dict[str, str]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Invalid request length")
        if length < 0 or length > 16_384:
            raise ValueError("Form is too large")
        raw = self.rfile.read(length).decode("utf-8", errors="strict")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True, max_num_fields=20)
        return {key: values[-1] for key, values in parsed.items()}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/login":
            if self.app.config.no_auth:
                self.redirect("/")
                return
            session = self.app.sessions.get(self.cookie_token())
            if session:
                self.redirect("/")
            else:
                self.send_html(login_page())
            return
        if not self.authenticate():
            return
        try:
            username = self.current_session["username"]
            csrf = self.current_session["csrf"]
            if path == "/":
                self.send_html(page("Overview", "overview", OVERVIEW_BODY, OVERVIEW_JS, csrf, username))
            elif path == "/updates":
                self.send_html(page("Updates", "updates", UPDATES_BODY, UPDATES_JS, csrf, username))
            elif path == "/backup":
                self.send_html(page("rSync", "backup", BACKUP_BODY, BACKUP_JS, csrf, username))
            elif path == "/autostart":
                self.send_html(page("Autostart", "autostart", AUTOSTART_BODY, AUTOSTART_JS, csrf, username))
            elif path == "/timers":
                self.redirect("/autostart")
            elif path == "/api/stats":
                self.send_json(self.app.stats.snapshot())
            elif path == "/api/updates":
                self.send_json(self.app.updates.available())
            elif path == "/api/backups":
                self.send_json(
                    {
                        "jobs": self.app.jobs.list(),
                        "rsync_available": command_exists("rsync"),
                        "capabilities": self.app.scheduler.capability(),
                        "warnings": self.app.startup_warnings,
                    }
                )
            elif path == "/api/autostart":
                jobs = []
                for job in self.app.timer_jobs.list():
                    item = dict(job)
                    item["display_command"] = timer_display_command(job)
                    jobs.append(item)
                self.send_json(
                    {
                        "jobs": jobs,
                        "capabilities": self.app.timer_scheduler.capability(),
                        "users": local_task_users(),
                        "current_user": username,
                        "warnings": self.app.timer_startup_warnings,
                    }
                )
            elif path == "/api/filesystem":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                requested_path = query.get("path", ["/"])[0]
                include_files = query.get("files", ["0"])[0].lower() in {"1", "true", "yes"}
                self.send_json(browse_directory(requested_path, include_files=include_files))
            elif path.startswith("/api/tasks/"):
                task_id = path.rsplit("/", 1)[-1]
                task = self.app.tasks.snapshot(task_id)
                if task:
                    self.send_json(task)
                else:
                    self.error_json(404, "Task not found")
            elif re.fullmatch(r"/api/backups/[A-Za-z0-9_-]+/log", path):
                job_id = path.split("/")[3]
                if not self.app.jobs.get(job_id):
                    self.error_json(404, "Backup job not found")
                else:
                    self.send_json({"log": read_log_tail(backup_log_path(self.app.config, job_id))})
            elif re.fullmatch(r"/api/autostart/[A-Za-z0-9_-]+/log", path):
                timer_id = path.split("/")[3]
                if not self.app.timer_jobs.get(timer_id):
                    self.error_json(404, "Autostart item not found")
                else:
                    self.send_json({"log": read_log_tail(timer_log_path(self.app.config, timer_id))})
            elif path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", 204)
            else:
                self.error_json(404, "Not found")
        except subprocess.TimeoutExpired:
            self.error_json(504, "A system command timed out")
        except PermissionError as exc:
            self.error_json(403, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.error_json(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.error_json(500, str(exc))

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/login":
            try:
                form = self.read_form()
                username = form.get("username", "").strip()
                password = form.get("password", "")
                if self.app.authenticate_credentials(username, password):
                    token, _session = self.app.sessions.create(username)
                    cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.app.config.session_ttl}"
                    if self.app.config.secure_cookie:
                        cookie += "; Secure"
                    self.redirect("/", {"Set-Cookie": cookie})
                else:
                    time.sleep(1.0)
                    self.send_html(login_page("The username or password was not accepted, or this account is not authorised.", username), 401)
            except (ValueError, UnicodeDecodeError):
                self.send_html(login_page("The login request was invalid."), 400)
            return
        if not self.authenticate():
            return
        if path == "/logout":
            try:
                form = self.read_form()
                if not self.require_csrf(form.get("csrf", "")):
                    return
                if self.current_token:
                    self.app.sessions.destroy(self.current_token)
                cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                if self.app.config.secure_cookie:
                    cookie += "; Secure"
                self.redirect("/login", {"Set-Cookie": cookie})
            except (ValueError, UnicodeDecodeError):
                self.error_json(400, "Invalid logout request")
            return
        if not self.require_csrf():
            return
        try:
            if path == "/api/hostname":
                payload = self.read_json()
                hostname = change_hostname(payload.get("hostname", ""))
                self.send_json({"hostname": hostname})
            elif path == "/api/power":
                payload = self.read_json()
                action = safe_text(payload.get("action", ""), 16).lower()
                message = schedule_power_action(action, payload.get("confirmation", ""))
                self.send_json({"accepted": True, "action": action, "message": message}, 202)
            elif path == "/api/updates/refresh":
                self.send_json(self.app.updates.refresh(), 202)
            elif path == "/api/updates/install":
                self.send_json(self.app.updates.install(), 202)
            elif path == "/api/rsync/install":
                self.send_json(self.app.updates.install_rsync(), 202)
            elif path == "/api/backups":
                job = self.app.jobs.save(self.read_json())
                warnings = self.app.scheduler.sync()
                self.send_json({"job": job, "warnings": warnings}, 201)
            elif path == "/api/autostart":
                job = self.app.timer_jobs.save(self.read_json())
                warnings = self.app.timer_scheduler.sync()
                self.send_json({"job": job, "warnings": warnings}, 201)
            elif re.fullmatch(r"/api/backups/[A-Za-z0-9_-]+/run", path):
                job_id = path.split("/")[3]
                job = self.app.jobs.get(job_id)
                if not job:
                    self.error_json(404, "Backup job not found")
                    return

                def worker(write: Callable[[str], None]) -> int:
                    return run_backup_sync(self.app.config, self.app.jobs, job_id, write)

                task = self.app.tasks.start("backup", f"Run backup: {job['name']}", f"backup:{job_id}", worker)
                self.send_json(task, 202)
            elif re.fullmatch(r"/api/autostart/[A-Za-z0-9_-]+/run", path):
                timer_id = path.split("/")[3]
                job = self.app.timer_jobs.get(timer_id)
                if not job:
                    self.error_json(404, "Autostart item not found")
                    return

                def timer_worker(write: Callable[[str], None]) -> int:
                    return run_timer_sync(self.app.config, self.app.timer_jobs, timer_id, write)

                task = self.app.tasks.start("autostart", f"Run autostart item: {job['name']}", f"autostart:{timer_id}", timer_worker)
                self.send_json(task, 202)
            else:
                self.error_json(404, "Not found")
        except PermissionError as exc:
            self.error_json(403, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.error_json(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.error_json(500, str(exc))

    def do_PUT(self) -> None:
        if not self.authenticate() or not self.require_csrf():
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        backup_match = re.fullmatch(r"/api/backups/([A-Za-z0-9_-]+)", path)
        timer_match = re.fullmatch(r"/api/autostart/([A-Za-z0-9_-]+)", path)
        if not backup_match and not timer_match:
            self.error_json(404, "Not found")
            return
        try:
            if backup_match:
                job_id = backup_match.group(1)
                if not self.app.jobs.get(job_id):
                    self.error_json(404, "Backup job not found")
                    return
                job = self.app.jobs.save(self.read_json(), job_id=job_id)
                warnings = self.app.scheduler.sync()
            else:
                assert timer_match is not None
                job_id = timer_match.group(1)
                if not self.app.timer_jobs.get(job_id):
                    self.error_json(404, "Autostart item not found")
                    return
                job = self.app.timer_jobs.save(self.read_json(), timer_id=job_id)
                warnings = self.app.timer_scheduler.sync()
            self.send_json({"job": job, "warnings": warnings})
        except PermissionError as exc:
            self.error_json(403, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.error_json(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.error_json(500, str(exc))

    def do_DELETE(self) -> None:
        if not self.authenticate() or not self.require_csrf():
            return
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        backup_match = re.fullmatch(r"/api/backups/([A-Za-z0-9_-]+)", path)
        timer_match = re.fullmatch(r"/api/autostart/([A-Za-z0-9_-]+)", path)
        if not backup_match and not timer_match:
            self.error_json(404, "Not found")
            return
        try:
            if backup_match:
                job_id = backup_match.group(1)
                if not self.app.jobs.delete(job_id):
                    self.error_json(404, "Backup job not found")
                    return
                warnings = self.app.scheduler.sync()
            else:
                assert timer_match is not None
                job_id = timer_match.group(1)
                if not self.app.timer_jobs.delete(job_id):
                    self.error_json(404, "Autostart item not found")
                    return
                warnings = self.app.timer_scheduler.sync()
            self.send_json({"deleted": True, "warnings": warnings})
        except Exception as exc:
            traceback.print_exc()
            self.error_json(500, str(exc))


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def install_service(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        print("ERROR: --install-service must be run with sudo/root.", file=sys.stderr)
        return 1
    if args.auth_mode == "pam":
        ensure_pam_config()
    source = pathlib.Path(__file__).resolve()
    install_dir = pathlib.Path("/opt/serverdeck")
    target = install_dir / "serverdeck.py"
    install_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    os.chmod(target, 0o755)
    data_dir = pathlib.Path(args.data_dir or "/var/lib/serverdeck")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    service = f"""[Unit]
Description=ServerDeck web server management
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {systemd_quote(str(target))} --host {systemd_quote(args.host)} --port {args.port} --data-dir {systemd_quote(str(data_dir))} --auth-mode {systemd_quote(args.auth_mode)} --auth-groups {systemd_quote(args.auth_groups)} --session-ttl {args.session_ttl}{" --secure-cookie" if args.secure_cookie else ""}
WorkingDirectory={install_dir}
Restart=on-failure
RestartSec=3
User=root
UMask=0077
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
"""
    service_path = pathlib.Path("/etc/systemd/system/serverdeck.service")
    atomic_write(service_path, service, 0o644)
    if command_exists("systemd-analyze"):
        verify = run_command(["systemd-analyze", "verify", str(service_path)], timeout=30)
        if verify.returncode != 0:
            print("Generated systemd unit did not pass validation:", file=sys.stderr)
            print(verify.stdout, file=sys.stderr)
            return verify.returncode or 1
    result = run_command(["systemctl", "daemon-reload"], timeout=30)
    if result.returncode == 0:
        result = run_command(["systemctl", "enable", "serverdeck.service"], timeout=30)
    if result.returncode == 0:
        result = run_command(["systemctl", "restart", "serverdeck.service"], timeout=45)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        return result.returncode or 1
    print(f"{APP_NAME} installed and started.")
    print(f"Open: http://{local_ip()}:{args.port}/")
    if args.auth_mode == "pam":
        print(f"Login: use a local Linux account in: {args.auth_groups}")
    elif args.auth_mode == "static":
        print(f"Static login username: {args.username}")
        print(f"Password file: {data_dir / 'admin-password.txt'}")
    else:
        print("WARNING: authentication is disabled.")
    print("Use a trusted LAN/VPN, or place the service behind an HTTPS reverse proxy before entering device passwords.")
    return 0


def uninstall_service() -> int:
    if os.geteuid() != 0:
        print("ERROR: --uninstall-service must be run with sudo/root.", file=sys.stderr)
        return 1
    run_command(["systemctl", "disable", "--now", "serverdeck.service"], timeout=30)
    systemd_dir = pathlib.Path("/etc/systemd/system")
    for pattern in ("serverdeck-task-*.timer", "serverdeck-task-*.service", "serverdeck-backup-*.timer", "serverdeck-backup-*.service"):
        for unit_path in systemd_dir.glob(pattern):
            run_command(["systemctl", "disable", "--now", unit_path.name], timeout=30)
            try:
                unit_path.unlink()
            except FileNotFoundError:
                pass
    for path in [pathlib.Path("/etc/systemd/system/serverdeck.service"), pathlib.Path("/opt/serverdeck/serverdeck.py")]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        if PAM_CONFIG_PATH.exists() and PAM_CONFIG_PATH.read_text(encoding="utf-8") == PAM_CONFIG:
            PAM_CONFIG_PATH.unlink()
    except OSError:
        pass
    run_command(["systemctl", "daemon-reload"], timeout=30)
    print("ServerDeck service and managed schedule units removed. Backup and autostart job data were left in place.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-file Debian/Ubuntu server management web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Address to listen on (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default: {DEFAULT_PORT})")
    parser.add_argument("--auth-mode", choices=("pam", "static", "none"), default="pam", help="Authentication mode (default: pam device accounts)")
    parser.add_argument("--auth-groups", default="sudo", help="Comma-separated groups allowed to use PAM login; use * for any local account (default: sudo)")
    parser.add_argument("--session-ttl", type=int, default=28_800, help="Login idle timeout in seconds (default: 28800 / 8 hours)")
    parser.add_argument("--secure-cookie", action="store_true", help="Mark login cookie Secure when served through HTTPS")
    parser.add_argument("--username", default="admin", help="Static-auth username (default: admin)")
    parser.add_argument("--password", help="Static-auth password; prefer SERVERDECK_PASSWORD or --password-file")
    parser.add_argument("--password-file", help="File containing the static-auth password")
    parser.add_argument("--no-auth", action="store_true", help="Alias for --auth-mode none (trusted testing networks only)")
    parser.add_argument("--data-dir", help="Persistent data directory")
    parser.add_argument("--run-backup", metavar="JOB_ID", help=argparse.SUPPRESS)
    parser.add_argument("--run-autostart", "--run-timer", dest="run_autostart", metavar="ITEM_ID", help=argparse.SUPPRESS)
    parser.add_argument("--install-service", action="store_true", help="Install and start a systemd service")
    parser.add_argument("--uninstall-service", action="store_true", help="Remove the systemd service and installed script")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    if args.no_auth:
        args.auth_mode = "none"
    if args.install_service:
        return install_service(args)
    if args.uninstall_service:
        return uninstall_service()

    config = Config(args)
    if args.run_backup:
        store = JobStore(config.jobs_file)
        try:
            return run_backup_sync(config, store, args.run_backup)
        except Exception as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            return 1
    if args.run_autostart:
        store = TimerStore(config.timers_file)
        try:
            return run_timer_sync(config, store, args.run_autostart)
        except Exception as exc:
            print(f"Autostart item failed: {exc}", file=sys.stderr)
            return 1

    app = Application(config)
    try:
        server = ServerDeckServer((config.host, config.port), Handler, app)
    except OSError as exc:
        if exc.errno == errno.EACCES:
            print(f"ERROR: Permission denied opening port {config.port}.", file=sys.stderr)
        else:
            print(f"ERROR: Could not start server: {exc}", file=sys.stderr)
        return 1

    url_host = local_ip() if config.host in {"0.0.0.0", "::"} else config.host
    print(f"{APP_NAME} {APP_VERSION} ({APP_BUILD})")
    print(f"Open: http://{url_host}:{config.port}/")
    if config.auth_mode == "none":
        print("WARNING: Authentication is disabled.")
    elif config.auth_mode == "pam":
        print("Authentication: Linux PAM device accounts")
        print("Allowed groups: " + ", ".join(config.auth_groups))
    else:
        print(f"Static username: {config.username}")
        if config.generated_password:
            print(f"Generated password: {config.password}")
        print(f"Password file: {config.password_file}")
    if not config.is_root:
        print("Running without root: hostname changes, package operations, power actions, and system schedules and autostart installation will be unavailable.")
    print("Press Ctrl+C to stop.")

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
