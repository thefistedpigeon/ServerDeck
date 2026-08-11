#!/usr/bin/env python3
"""
ServerDeck - a single-file web UI for basic Debian/Ubuntu server management.

Features:
  * Overview: CPU, memory, storage, network activity, uptime, hostname rename, restart and shutdown.
  * Updates: refresh package lists, view available upgrades, install upgrades.
  * Backups: create/run rsync jobs and schedule them with cron or systemd timers.
  * Disks: mount existing filesystems and create safe UUID-based persistent mounts.
  * Network: inspect interfaces and configure DHCP or static IPv4 with NetworkManager.
  * CopyParty: download, configure, and manage a persistent CopyParty service.
  * Terminal: run commands in a persistent shell as the signed-in Linux user.

Runtime dependencies:
  * Python 3 standard library only.
  * Debian/Ubuntu system tools: apt-get, hostnamectl, systemctl/cron as applicable.
  * rsync is needed only for backup jobs and can be installed from the UI.

Run interactively:
  sudo python3 serverdeck.py

Install as a systemd service:
  sudo python3 serverdeck.py --install-service
  sudo python3 serverdeck.py --install-service -port 8081

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
import ipaddress
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
import urllib.request
import urllib.error
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

APP_NAME = "ServerDeck"
APP_VERSION = "0.9.7"
APP_BUILD = "2026-08-11-copyparty-permissions-delete"
DEFAULT_PORT = 9090
MAX_BODY = 1024 * 1024
MAX_TASK_OUTPUT = 300_000
MAX_TERMINAL_OUTPUT = 300_000
TERMINAL_IDLE_TIMEOUT = 3_600
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

COPYPARTY_DOWNLOAD_URL = "https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py"
COPYPARTY_INSTALL_DIR = pathlib.Path("/opt/copyparty")
COPYPARTY_SCRIPT_PATH = COPYPARTY_INSTALL_DIR / "copyparty-sfx.py"
COPYPARTY_MAX_DOWNLOAD = 128 * 1024 * 1024
COPYPARTY_THUMBNAIL_PACKAGES = ("python3-pil", "ffmpeg")
COPYPARTY_SERVICE_NAME = "copyparty.service"
COPYPARTY_SERVICE_PATH = pathlib.Path("/etc/systemd/system/copyparty.service")
COPYPARTY_SERVICE_MARKER = "# Managed by ServerDeck"
COPYPARTY_DEFAULT_PORT = 3923


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
        # Kept only so older Autostart data remains available on disk after an
        # upgrade. Version 0.7 no longer exposes or executes these entries.
        self.timers_file = self.data_dir / "autostart-jobs.json"
        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.password_file = pathlib.Path(args.password_file).expanduser() if args.password_file else self.data_dir / "admin-password.txt"
        self.password = ""
        self.generated_password = False

        if self.auth_mode == "pam" and not args.run_backup:
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
        self._network_lock = threading.Lock()
        self._last_network: Optional[Tuple[Dict[str, Tuple[int, int]], float]] = None

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
    def _read_network_counters() -> Dict[str, Tuple[int, int]]:
        counters: Dict[str, Tuple[int, int]] = {}
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                raw_name, raw_values = line.split(":", 1)
                name = raw_name.strip()
                if not name or name == "lo":
                    continue
                values = raw_values.split()
                if len(values) < 16:
                    continue
                try:
                    counters[name] = (int(values[0]), int(values[8]))
                except ValueError:
                    continue
        return counters

    def network(self) -> Dict[str, Any]:
        current = self._read_network_counters()
        sampled_at = time.monotonic()
        download_bytes_per_second = 0.0
        upload_bytes_per_second = 0.0

        with self._network_lock:
            previous_sample = self._last_network
            self._last_network = (current, sampled_at)

        if previous_sample is not None:
            previous, previous_at = previous_sample
            elapsed = sampled_at - previous_at
            if elapsed > 0:
                received_delta = 0
                transmitted_delta = 0
                for name, (received, transmitted) in current.items():
                    old = previous.get(name)
                    if old is None:
                        continue
                    received_delta += max(0, received - old[0])
                    transmitted_delta += max(0, transmitted - old[1])
                download_bytes_per_second = received_delta / elapsed
                upload_bytes_per_second = transmitted_delta / elapsed

        received_total = sum(item[0] for item in current.values())
        transmitted_total = sum(item[1] for item in current.values())
        interfaces = sorted(current)
        return {
            "download_bps": round(download_bytes_per_second, 1),
            "upload_bps": round(upload_bytes_per_second, 1),
            "download_human": human_bytes(int(download_bytes_per_second)) + "/s",
            "upload_human": human_bytes(int(upload_bytes_per_second)) + "/s",
            "received_total": received_total,
            "transmitted_total": transmitted_total,
            "received_total_human": human_bytes(received_total),
            "transmitted_total_human": human_bytes(transmitted_total),
            "interfaces": interfaces,
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
            "network": self.network(),
            "uptime": self.uptime(),
            "load_average": [round(item, 2) for item in load],
            "hostname": socket.gethostname(),
            "os": self.os_info(),
            "is_root": os.geteuid() == 0,
            "reboot_required": pathlib.Path("/var/run/reboot-required").exists(),
            "time": now_iso(),
        }


ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


def clean_terminal_output(value: str) -> str:
    """Convert terminal output into safe, readable plain text."""
    value = ANSI_ESCAPE_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned: List[str] = []
    for char in value:
        code = ord(char)
        if char == "\b":
            if cleaned and cleaned[-1] != "\n":
                cleaned.pop()
        elif char in {"\n", "\t"} or code >= 32:
            cleaned.append(char)
    return "".join(cleaned)


class TerminalSession:
    """A persistent plain-text shell attached to one authenticated web session."""

    def __init__(self, username: str):
        try:
            account = pwd.getpwnam(username)
        except KeyError as exc:
            raise ValueError(
                "Terminal access requires a real local Linux account. Sign in using PAM with your device username."
            ) from exc

        script_command = shutil.which("script")
        if not script_command:
            raise RuntimeError("The util-linux 'script' command is required for the Terminal page")

        shell = "/bin/bash" if pathlib.Path("/bin/bash").is_file() else account.pw_shell
        if not shell or not pathlib.Path(shell).is_file() or shell.endswith(("nologin", "false")):
            shell = "/bin/sh"
        shell_args = [shell]
        if pathlib.Path(shell).name == "bash":
            shell_args.extend(["--noprofile", "--norc", "-i"])
        else:
            shell_args.append("-i")

        home = account.pw_dir if account.pw_dir and pathlib.Path(account.pw_dir).is_dir() else "/"
        prompt = f"{username}@{socket.gethostname()}:\\w\\$ "
        inner_command = shlex.join(
            [
                "/usr/bin/env",
                "TERM=dumb",
                "HISTFILE=/dev/null",
                f"PS1={prompt}",
                "PROMPT_COMMAND=",
                *shell_args,
            ]
        )
        command = [script_command, "-qefc", inner_command, "/dev/null"]

        current_uid = os.geteuid()
        if account.pw_uid != current_uid:
            if current_uid != 0:
                raise PermissionError(
                    f"ServerDeck is not running as root and cannot start a shell as {username}"
                )
            runuser = shutil.which("runuser")
            if not runuser:
                raise RuntimeError("The util-linux 'runuser' command is required to switch terminal users")
            command = [runuser, "-u", username, "--", *command]

        env = os.environ.copy()
        env.update(
            {
                "HOME": home,
                "USER": username,
                "LOGNAME": username,
                "SHELL": shell,
                "TERM": "dumb",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            }
        )

        self.username = username
        self.home = home
        self.shell = shell
        self.lock = threading.RLock()
        self.output = ""
        self.output_start = 0
        self.output_end = 0
        self.created_at = time.time()
        self.last_used = self.created_at
        self.closed = False
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=home,
            env=env,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
        )
        self.reader = threading.Thread(
            target=self._read_output,
            name=f"serverdeck-terminal-{username}",
            daemon=True,
        )
        self.reader.start()

    def _append(self, value: str) -> None:
        value = clean_terminal_output(value)
        if not value:
            return
        with self.lock:
            self.output += value
            self.output_end += len(value)
            if len(self.output) > MAX_TERMINAL_OUTPUT:
                remove = len(self.output) - MAX_TERMINAL_OUTPUT
                self.output = self.output[remove:]
                self.output_start += remove

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                chunk = self.process.stdout.read(4096)
                if not chunk:
                    break
                self._append(chunk.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            returncode = self.process.poll()
            if returncode is None:
                try:
                    returncode = self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    returncode = None
            if not self.closed:
                suffix = "\n[Terminal session ended"
                if returncode is not None:
                    suffix += f" with status {returncode}"
                self._append(suffix + "]\n")

    def is_active(self) -> bool:
        return not self.closed and self.process.poll() is None

    def write(self, command: str) -> None:
        if not isinstance(command, str):
            raise ValueError("Expected a command")
        if "\x00" in command:
            raise ValueError("NUL bytes are not allowed")
        if len(command) > 32_768:
            raise ValueError("Command is too long")
        if not self.is_active() or self.process.stdin is None:
            raise RuntimeError("The terminal shell is not running")
        data = command.rstrip("\r\n").encode("utf-8") + b"\n"
        with self.lock:
            self.last_used = time.time()
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError("The terminal shell has stopped") from exc

    def interrupt(self) -> None:
        if not self.is_active() or self.process.stdin is None:
            raise RuntimeError("The terminal shell is not running")
        with self.lock:
            self.last_used = time.time()
            try:
                self.process.stdin.write(b"\x03")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError("Unable to interrupt the terminal command") from exc

    def clear(self) -> Dict[str, Any]:
        with self.lock:
            self.output = ""
            self.output_start = self.output_end
            self.last_used = time.time()
        return self.snapshot(self.output_end)

    def snapshot(self, offset: int = 0) -> Dict[str, Any]:
        with self.lock:
            self.last_used = time.time()
            reset = offset < self.output_start or offset > self.output_end
            if reset:
                chunk = self.output
            else:
                chunk = self.output[offset - self.output_start :]
            return {
                "active": self.is_active(),
                "username": self.username,
                "shell": self.shell,
                "home": self.home,
                "output": chunk,
                "offset": self.output_end,
                "reset": reset,
                "created": self.created_at,
            }

    def stop(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            process = self.process
            try:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write(b"exit\n")
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
        for stream in (process.stdin, process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class TerminalManager:
    def __init__(self, idle_timeout: int = TERMINAL_IDLE_TIMEOUT):
        self.idle_timeout = max(300, idle_timeout)
        self.lock = threading.RLock()
        self.sessions: Dict[str, TerminalSession] = {}
        self.closed = False
        self.reaper = threading.Thread(target=self._reap_loop, name="serverdeck-terminal-reaper", daemon=True)
        self.reaper.start()

    def _reap_loop(self) -> None:
        while not self.closed:
            time.sleep(60)
            now = time.time()
            stale: List[Tuple[str, TerminalSession]] = []
            with self.lock:
                for key, session in list(self.sessions.items()):
                    if now - session.last_used > self.idle_timeout:
                        stale.append((key, self.sessions.pop(key)))
            for _key, session in stale:
                session.stop()

    def start(self, key: str, username: str, restart: bool = False) -> Dict[str, Any]:
        old: Optional[TerminalSession] = None
        with self.lock:
            current = self.sessions.get(key)
            if current and current.is_active() and not restart:
                return current.snapshot(0)
            if current:
                old = self.sessions.pop(key)
        if old:
            old.stop()
        session = TerminalSession(username)
        with self.lock:
            self.sessions[key] = session
        return session.snapshot(0)

    def get(self, key: str) -> Optional[TerminalSession]:
        with self.lock:
            return self.sessions.get(key)

    def snapshot(self, key: str, offset: int = 0) -> Dict[str, Any]:
        session = self.get(key)
        if not session:
            return {
                "active": False,
                "username": "",
                "shell": "",
                "home": "",
                "output": "",
                "offset": 0,
                "reset": offset != 0,
            }
        return session.snapshot(offset)

    def stop(self, key: str) -> None:
        with self.lock:
            session = self.sessions.pop(key, None)
        if session:
            session.stop()

    def close_all(self) -> None:
        self.closed = True
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.stop()


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


def remove_deprecated_autostart_units(config: Config) -> List[str]:
    """Disable and remove startup units managed by older ServerDeck releases."""
    if not config.is_root:
        return []
    if not command_exists("systemctl") or not pathlib.Path("/run/systemd/system").exists():
        return []

    systemd_dir = pathlib.Path("/etc/systemd/system")
    unit_paths = list(systemd_dir.glob("serverdeck-task-*.timer")) + list(systemd_dir.glob("serverdeck-task-*.service"))
    if not unit_paths:
        return []

    warnings: List[str] = []
    for unit_path in unit_paths:
        result = run_command(["systemctl", "disable", "--now", unit_path.name], timeout=30)
        if result.returncode != 0:
            output = result.stdout.strip()
            if output and "does not exist" not in output.lower() and "not loaded" not in output.lower():
                warnings.append(output)
        try:
            unit_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(f"Could not remove {unit_path.name}: {exc}")

    reload_result = run_command(["systemctl", "daemon-reload"], timeout=30)
    if reload_result.returncode != 0:
        warnings.append(reload_result.stdout.strip() or "systemd daemon-reload failed after removing old Autostart units")
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
            source = match.group(4) or ""
            packages.append(
                {
                    "name": match.group(1),
                    "current": match.group(2) or "unknown",
                    "candidate": match.group(3),
                    "source": source,
                    # Both Ubuntu and Debian identify their security pockets/origins
                    # with the word "security" in apt-get's simulated upgrade output
                    # (for example noble-security or Debian-Security).
                    "security": "security" in source.lower(),
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
            "security_count": sum(1 for package in packages if package.get("security")),
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

    def full_upgrade(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Full upgrade requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            write("Running full upgrade (equivalent to: sudo apt full-upgrade -y)\n\n")
            code = stream_command(["apt-get", "update"], write, env=apt_environment())
            if code != 0:
                return code
            return stream_command(
                [
                    "apt-get",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    "full-upgrade",
                ],
                write,
                env=apt_environment(),
            )

        return self.tasks.start("apt-full-upgrade", "Full system upgrade", "apt", worker)

    def install_security(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Installing security updates requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            code = stream_command(["apt-get", "update"], write, env=apt_environment())
            if code != 0:
                return code

            available = self.available()
            security_packages = [package for package in available.get("packages", []) if package.get("security")]
            if not security_packages:
                write("\nNo security updates are currently available.\n")
                return 0

            write(f"\nInstalling {len(security_packages)} security update(s):\n")
            for package in security_packages:
                write(f"  {package['name']} {package['current']} -> {package['candidate']}\n")
            write("\n")

            # Pin each selected package to the candidate version that apt identified
            # as coming from a security origin. Dependencies required by those
            # packages may still be installed by apt as necessary.
            targets = [f"{package['name']}={package['candidate']}" for package in security_packages]
            return stream_command(
                [
                    "apt-get",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    "--only-upgrade",
                    "install",
                    *targets,
                ],
                write,
                env=apt_environment(),
            )

        return self.tasks.start("apt-security", "Install security updates", "apt", worker)

    def cleanup(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("APT cleanup requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            write("Running package cleanup (equivalent to: sudo apt autoclean && sudo apt autoremove -y)\n\n")
            code = stream_command(["apt-get", "autoclean"], write, env=apt_environment())
            if code != 0:
                return code
            write("\n")
            return stream_command(["apt-get", "autoremove", "-y"], write, env=apt_environment())

        return self.tasks.start("apt-cleanup", "Autoclean and autoremove", "apt", worker)

    def install_rsync(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Installing rsync requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            code = stream_command(["apt-get", "update"], write, env=apt_environment())
            if code != 0:
                return code
            return stream_command(["apt-get", "install", "-y", "rsync"], write, env=apt_environment())

        return self.tasks.start("install-rsync", "Install rsync", "apt", worker)


class CopyPartyManager:
    """Download CopyParty and manage a ServerDeck-owned systemd service."""

    def __init__(self, tasks: TaskRegistry, config: Config):
        self.tasks = tasks
        self.config = config
        self.config_path = config.data_dir / "copyparty-service.json"

    @staticmethod
    def _package_installed(name: str) -> bool:
        if not command_exists("dpkg-query"):
            return False
        result = run_command(
            ["dpkg-query", "-W", "-f=${Status}", name],
            timeout=15,
            env={**os.environ, "LC_ALL": "C"},
        )
        return result.returncode == 0 and "install ok installed" in result.stdout.lower()

    def _load_service_config(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        folder = data.get("folder") if isinstance(data.get("folder"), str) else ""
        user = data.get("user") if isinstance(data.get("user"), str) else ""
        return {"folder": folder, "user": user}

    def _save_service_config(self, folder: str, username: str) -> None:
        atomic_write(
            self.config_path,
            json.dumps({"folder": folder, "user": username}, indent=2, sort_keys=True) + "\n",
            0o600,
        )

    @staticmethod
    def _ensure_install_permissions() -> None:
        """Keep the system-wide CopyParty install traversable by its service user."""
        COPYPARTY_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(COPYPARTY_INSTALL_DIR, 0o755)
        except OSError as exc:
            raise RuntimeError(f"Unable to set permissions on {COPYPARTY_INSTALL_DIR}: {exc}") from exc
        if COPYPARTY_SCRIPT_PATH.exists():
            try:
                os.chmod(COPYPARTY_SCRIPT_PATH, 0o755)
            except OSError as exc:
                raise RuntimeError(f"Unable to make {COPYPARTY_SCRIPT_PATH} readable/executable: {exc}") from exc

    @staticmethod
    def _service_file_is_managed() -> bool:
        try:
            head = COPYPARTY_SERVICE_PATH.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            return False
        return COPYPARTY_SERVICE_MARKER in head

    @staticmethod
    def _systemctl_state() -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "service_exists": COPYPARTY_SERVICE_PATH.exists(),
            "service_managed": CopyPartyManager._service_file_is_managed(),
            "active": False,
            "enabled": False,
            "active_state": "unknown",
            "sub_state": "unknown",
            "pid": 0,
            "exit_status": None,
        }
        if not command_exists("systemctl"):
            state["active_state"] = "unavailable"
            state["sub_state"] = "systemctl not found"
            return state
        active = run_command(["systemctl", "is-active", COPYPARTY_SERVICE_NAME], timeout=10)
        enabled = run_command(["systemctl", "is-enabled", COPYPARTY_SERVICE_NAME], timeout=10)
        state["active"] = active.returncode == 0 and active.stdout.strip() == "active"
        state["enabled"] = enabled.returncode == 0 and enabled.stdout.strip() in {"enabled", "enabled-runtime"}
        show = run_command(
            [
                "systemctl", "show", COPYPARTY_SERVICE_NAME,
                "--property=ActiveState,SubState,MainPID,ExecMainStatus",
                "--no-pager",
            ],
            timeout=10,
        )
        if show.returncode == 0:
            values: Dict[str, str] = {}
            for line in show.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            state["active_state"] = values.get("ActiveState", state["active_state"])
            state["sub_state"] = values.get("SubState", state["sub_state"])
            try:
                state["pid"] = int(values.get("MainPID", "0") or "0")
            except ValueError:
                state["pid"] = 0
            try:
                state["exit_status"] = int(values.get("ExecMainStatus", "0") or "0")
            except ValueError:
                state["exit_status"] = None
        return state

    @staticmethod
    def _journal_tail() -> str:
        if not command_exists("journalctl") or not COPYPARTY_SERVICE_PATH.exists():
            return ""
        result = run_command(
            ["journalctl", "-u", COPYPARTY_SERVICE_NAME, "-n", "40", "--no-pager", "--output=short"],
            timeout=12,
        )
        return result.stdout[-20000:] if result.returncode == 0 else ""

    @staticmethod
    def _validate_service_user(username: str) -> pwd.struct_passwd:
        username = safe_text(username, 64)
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("The signed-in username is not valid for a Linux service")
        try:
            return pwd.getpwnam(username)
        except KeyError as exc:
            raise ValueError(
                "CopyParty must run as a local Linux account. Sign in to ServerDeck with a local device account before configuring the service."
            ) from exc

    @staticmethod
    def _validate_share_folder(raw_folder: Any) -> pathlib.Path:
        folder_text = safe_text(raw_folder, 4096)
        if not folder_text:
            raise ValueError("Choose a folder to share with CopyParty")
        candidate = pathlib.Path(folder_text)
        if not candidate.is_absolute():
            raise ValueError("The CopyParty folder must be an absolute server path")
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("The selected CopyParty folder does not exist") from exc
        if not resolved.is_dir():
            raise ValueError("The selected CopyParty path is not a directory")
        return resolved

    @staticmethod
    def _render_service_unit(folder: pathlib.Path, username: str) -> str:
        volume = f"{folder}::rw"
        return f"""{COPYPARTY_SERVICE_MARKER}\n[Unit]\nDescription=CopyParty file server (managed by ServerDeck)\nWants=network-online.target\nAfter=network-online.target local-fs.target\n\n[Service]\nType=simple\nUser={username}\nWorkingDirectory={COPYPARTY_INSTALL_DIR}\nExecStartPre=/usr/bin/test -x {COPYPARTY_SCRIPT_PATH}\nExecStartPre=/usr/bin/test -r {systemd_quote(str(folder))}\nExecStartPre=/usr/bin/test -w {systemd_quote(str(folder))}\nExecStartPre=/usr/bin/test -x {systemd_quote(str(folder))}\nExecStart={COPYPARTY_SCRIPT_PATH} -v {systemd_quote(volume)} -z\nRestart=on-failure\nRestartSec=3\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=full\nUMask=0022\n\n[Install]\nWantedBy=multi-user.target\n"""

    def status(self) -> Dict[str, Any]:
        script = COPYPARTY_SCRIPT_PATH
        installed = script.is_file()
        size = 0
        modified = None
        if installed:
            try:
                stat = script.stat()
                size = stat.st_size
                modified = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).astimezone().isoformat(timespec="seconds")
            except OSError:
                installed = False
        package_status = {name: self._package_installed(name) for name in COPYPARTY_THUMBNAIL_PACKAGES}
        service_config = self._load_service_config()
        service = self._systemctl_state()
        configured_folder = service_config.get("folder", "")
        configured_user = service_config.get("user", "")
        service.update(
            {
                "configured": bool(configured_folder and configured_user and service["service_managed"]),
                "folder": configured_folder,
                "user": configured_user,
                "port": COPYPARTY_DEFAULT_PORT,
                "command": f"{COPYPARTY_SCRIPT_PATH} -v {configured_folder}::rw -z" if configured_folder else "",
                "logs": self._journal_tail(),
            }
        )
        return {
            "installed": installed,
            "path": str(script),
            "download_url": COPYPARTY_DOWNLOAD_URL,
            "size": size,
            "size_human": human_bytes(size),
            "modified": modified,
            "thumbnail_packages": package_status,
            "thumbnails_ready": all(package_status.values()),
            "is_root": os.geteuid() == 0,
            "service": service,
        }

    def snapshot(self) -> Dict[str, Any]:
        return self.status()

    def download(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Downloading CopyParty to /opt requires root privileges")

        def worker(write: Callable[[str], None]) -> int:
            self._ensure_install_permissions()
            write(f"Downloading official CopyParty release:\n{COPYPARTY_DOWNLOAD_URL}\n\n")
            request = urllib.request.Request(
                COPYPARTY_DOWNLOAD_URL,
                headers={"User-Agent": f"ServerDeck/{APP_VERSION}"},
                method="GET",
            )
            temp_fd, temp_name = tempfile.mkstemp(prefix=".copyparty-sfx.", suffix=".tmp", dir=str(COPYPARTY_INSTALL_DIR))
            total = 0
            try:
                with os.fdopen(temp_fd, "wb") as target:
                    try:
                        with urllib.request.urlopen(request, timeout=45) as response:
                            length_header = response.headers.get("Content-Length")
                            if length_header:
                                try:
                                    expected = int(length_header)
                                except ValueError:
                                    expected = 0
                                if expected > COPYPARTY_MAX_DOWNLOAD:
                                    raise RuntimeError("CopyParty download is unexpectedly large")
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > COPYPARTY_MAX_DOWNLOAD:
                                    raise RuntimeError("CopyParty download exceeded the safety size limit")
                                target.write(chunk)
                                write(f"Downloaded {human_bytes(total)}\r")
                    except urllib.error.HTTPError as exc:
                        raise RuntimeError(f"CopyParty download failed with HTTP {exc.code}") from exc
                    except urllib.error.URLError as exc:
                        raise RuntimeError(f"CopyParty download failed: {exc.reason}") from exc
                    target.flush()
                    os.fsync(target.fileno())

                if total < 1024:
                    raise RuntimeError("Downloaded file is unexpectedly small; refusing to install it")
                head = pathlib.Path(temp_name).read_bytes()[:8192].lstrip().lower()
                if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                    raise RuntimeError("The download returned an HTML page instead of the CopyParty script")

                if COPYPARTY_SCRIPT_PATH.exists():
                    previous = COPYPARTY_SCRIPT_PATH.with_suffix(".py.previous")
                    try:
                        shutil.copy2(COPYPARTY_SCRIPT_PATH, previous)
                        os.chmod(previous, 0o755)
                        write(f"\nSaved previous copy as {previous}\n")
                    except OSError as exc:
                        write(f"\nWarning: could not save previous copy: {exc}\n")

                os.chmod(temp_name, 0o755)
                os.replace(temp_name, COPYPARTY_SCRIPT_PATH)
                self._ensure_install_permissions()
                write(f"\nInstalled CopyParty to {COPYPARTY_SCRIPT_PATH}\n")
                write(f"Install directory permissions: 755 ({COPYPARTY_INSTALL_DIR})\n")
                write(f"Script permissions: 755 ({COPYPARTY_SCRIPT_PATH})\n")
                return 0
            finally:
                try:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass

        return self.tasks.start("copyparty-download", "Download CopyParty", "copyparty-download", worker)

    def install_thumbnail_packages(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Installing thumbnail packages requires root privileges")
        if not command_exists("apt-get"):
            raise RuntimeError("apt-get is not available on this system")

        def worker(write: Callable[[str], None]) -> int:
            write("Installing CopyParty thumbnail dependencies.\n")
            write("Equivalent command: sudo apt install --no-install-recommends python3-pil ffmpeg\n\n")
            return stream_command(
                ["apt-get", "install", "-y", "--no-install-recommends", *COPYPARTY_THUMBNAIL_PACKAGES],
                write,
                env=apt_environment(),
            )

        return self.tasks.start("copyparty-thumbnails", "Install CopyParty thumbnail packages", "apt", worker)

    def configure_and_start(self, payload: Dict[str, Any], username: str) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Configuring the CopyParty service requires root privileges")
        if not COPYPARTY_SCRIPT_PATH.is_file():
            raise RuntimeError("Download CopyParty before installing its service")
        self._ensure_install_permissions()
        if not command_exists("systemctl"):
            raise RuntimeError("systemctl is not available on this system")
        self._validate_service_user(username)
        folder = self._validate_share_folder(payload.get("folder", ""))

        # The service runs as the signed-in Linux account. Verify that account can
        # traverse /opt/copyparty and read/execute the downloaded SFX before
        # installing the unit, so permission problems are reported immediately.
        if command_exists("runuser"):
            access = run_command(
                ["runuser", "-u", username, "--", "/usr/bin/test", "-r", str(COPYPARTY_SCRIPT_PATH)],
                timeout=10,
            )
            execute = run_command(
                ["runuser", "-u", username, "--", "/usr/bin/test", "-x", str(COPYPARTY_SCRIPT_PATH)],
                timeout=10,
            )
            if access.returncode != 0 or execute.returncode != 0:
                raise RuntimeError(
                    f"The service account {username} cannot read/execute {COPYPARTY_SCRIPT_PATH}. "
                    f"ServerDeck set {COPYPARTY_INSTALL_DIR} and the script to mode 755, but a parent-directory permission may still be blocking access."
                )

        if COPYPARTY_SERVICE_PATH.exists() and not self._service_file_is_managed():
            raise RuntimeError(
                f"{COPYPARTY_SERVICE_PATH} already exists and was not created by ServerDeck. Remove or rename that service before using this page."
            )

        unit = self._render_service_unit(folder, username)
        atomic_write(COPYPARTY_SERVICE_PATH, unit, 0o644)
        self._save_service_config(str(folder), username)
        if command_exists("systemd-analyze"):
            verify = run_command(["systemd-analyze", "verify", str(COPYPARTY_SERVICE_PATH)], timeout=20)
            if verify.returncode != 0:
                raise RuntimeError("Generated CopyParty service failed systemd validation:\n" + (verify.stdout.strip() or "unknown validation error"))
        run_command(["systemctl", "daemon-reload"], timeout=20, check=True)
        result = run_command(["systemctl", "enable", "--now", COPYPARTY_SERVICE_NAME], timeout=45)
        if result.returncode != 0:
            detail = result.stdout.strip() or "systemctl returned an error"
            raise RuntimeError("CopyParty service could not be started: " + detail)
        # Give a fast-failing service a moment to report its state.
        time.sleep(0.35)
        state = self._systemctl_state()
        if not state["active"]:
            logs = self._journal_tail().strip()
            detail = logs[-4000:] if logs else f"service state is {state['active_state']}/{state['sub_state']}"
            raise RuntimeError("CopyParty service did not stay running. Check folder permissions.\n" + detail)
        return {"message": f"CopyParty service is installed, running, and enabled at boot using {folder}.", "service": self.snapshot()["service"]}

    def service_action(self, action: str) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Managing the CopyParty service requires root privileges")
        action = safe_text(action, 16).lower()
        if action not in {"start", "stop", "restart"}:
            raise ValueError("Unsupported CopyParty service action")
        if not COPYPARTY_SERVICE_PATH.exists():
            raise RuntimeError("Configure the CopyParty service first")
        if not self._service_file_is_managed():
            raise RuntimeError("The existing CopyParty service is not managed by ServerDeck")
        result = run_command(["systemctl", action, COPYPARTY_SERVICE_NAME], timeout=45)
        if result.returncode != 0:
            raise RuntimeError(f"Unable to {action} CopyParty: " + (result.stdout.strip() or "systemctl returned an error"))
        if action in {"start", "restart"}:
            time.sleep(0.25)
        return {"message": f"CopyParty service {action} command completed.", "service": self.snapshot()["service"]}

    def delete_service(self) -> Dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("Deleting the CopyParty service requires root privileges")
        if not COPYPARTY_SERVICE_PATH.exists():
            try:
                self.config_path.unlink(missing_ok=True)
            except OSError:
                pass
            return {"message": "CopyParty service is not installed.", "service": self.snapshot()["service"]}
        if not self._service_file_is_managed():
            raise RuntimeError("The existing CopyParty service is not managed by ServerDeck and will not be deleted")
        if command_exists("systemctl"):
            run_command(["systemctl", "disable", "--now", COPYPARTY_SERVICE_NAME], timeout=45)
        try:
            COPYPARTY_SERVICE_PATH.unlink()
        except OSError as exc:
            raise RuntimeError(f"Unable to delete {COPYPARTY_SERVICE_PATH}: {exc}") from exc
        try:
            self.config_path.unlink(missing_ok=True)
        except OSError:
            pass
        if command_exists("systemctl"):
            run_command(["systemctl", "daemon-reload"], timeout=20)
            run_command(["systemctl", "reset-failed", COPYPARTY_SERVICE_NAME], timeout=20)
        return {
            "message": "CopyParty service was stopped, disabled, and deleted. The downloaded CopyParty script and shared files were left untouched.",
            "service": self.snapshot()["service"],
        }


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


FSTAB_PATH = pathlib.Path("/etc/fstab")
FSTAB_MARKER_PREFIX = "# ServerDeck managed mount: "
MOUNTPOINT_ROOTS = ("/mnt", "/media", "/srv")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _require_root(message: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError(message)


def _fstab_escape(value: str) -> str:
    return value.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("#", "\\043")


def _nearest_existing_parent(path: pathlib.Path) -> pathlib.Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def validate_mountpoint(raw: Any) -> str:
    value = safe_text(raw, 512)
    if not value.startswith("/"):
        raise ValueError("The mount folder must be an absolute path")
    path = pathlib.Path(os.path.normpath(value))
    if str(path) in {"/", "/boot", "/boot/firmware", "/usr", "/var", "/etc", "/home"}:
        raise ValueError("Choose a dedicated folder under /mnt, /media, or /srv")
    if not any(str(path) == root or str(path).startswith(root + "/") for root in MOUNTPOINT_ROOTS):
        raise ValueError("Mount folders must be under /mnt, /media, or /srv")
    existing = _nearest_existing_parent(path)
    if existing.is_symlink():
        raise ValueError("The mount folder cannot be inside a symbolic-link path")
    resolved = pathlib.Path(os.path.realpath(existing))
    if not any(str(resolved) == root or str(resolved).startswith(root + "/") for root in MOUNTPOINT_ROOTS):
        raise ValueError("The mount folder resolves outside /mnt, /media, or /srv")
    if path.exists() and path.is_symlink():
        raise ValueError("The mount folder cannot be a symbolic link")
    return str(path)


class DiskManager:
    """Read block-device state and manage conservative UUID-based mounts."""

    def __init__(self, fstab_path: pathlib.Path = FSTAB_PATH):
        self.fstab_path = fstab_path
        self._lock = threading.RLock()

    @staticmethod
    def _flatten(nodes: Iterable[Dict[str, Any]], parent: str = "") -> List[Dict[str, Any]]:
        flattened: List[Dict[str, Any]] = []
        for node in nodes:
            item = dict(node)
            if parent and not item.get("pkname"):
                item["pkname"] = parent
            children = item.pop("children", []) or []
            flattened.append(item)
            flattened.extend(DiskManager._flatten(children, str(item.get("name", ""))))
        return flattened

    @staticmethod
    def _lsblk_json() -> Dict[str, Any]:
        columns = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,RM,RO,MODEL,TRAN,PKNAME"
        result = run_command(["lsblk", "--json", "--bytes", "--output", columns], timeout=15)
        if result.returncode != 0:
            fallback = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,RM,RO,MODEL,TRAN,PKNAME"
            result = run_command(["lsblk", "--json", "--bytes", "--output", fallback], timeout=15)
        if result.returncode != 0:
            raise RuntimeError("Unable to read block devices: " + result.stdout.strip())
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("lsblk returned invalid device data") from exc
        if not isinstance(data, dict):
            raise RuntimeError("lsblk returned invalid device data")
        return data

    @staticmethod
    def _root_device_path() -> str:
        result = run_command(["findmnt", "-n", "-o", "SOURCE", "/"], timeout=10)
        if result.returncode != 0:
            return ""
        source = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return os.path.realpath(source) if source.startswith("/") else source

    def _fstab_text(self) -> str:
        try:
            return self.fstab_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _parse_fstab(text: str) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        managed_uuid = ""
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith(FSTAB_MARKER_PREFIX):
                managed_uuid = stripped[len(FSTAB_MARKER_PREFIX):].strip()
                continue
            if not stripped or stripped.startswith("#"):
                managed_uuid = ""
                continue
            fields = stripped.split()
            if len(fields) < 4:
                managed_uuid = ""
                continue
            source, target, fstype, options = fields[:4]
            uuid_value = source[5:] if source.startswith("UUID=") else ""
            entries.append(
                {
                    "source": source,
                    "target": target.replace("\\040", " ").replace("\\011", "\t"),
                    "fstype": fstype,
                    "options": options,
                    "uuid": uuid_value,
                    "managed": "true" if managed_uuid and managed_uuid == uuid_value else "false",
                }
            )
            managed_uuid = ""
        return entries

    @staticmethod
    def _mountpoints(item: Dict[str, Any]) -> List[str]:
        value = item.get("mountpoints", item.get("mountpoint"))
        if isinstance(value, list):
            return [str(entry) for entry in value if entry]
        if value:
            return [str(value)]
        return []

    def inventory(self) -> Dict[str, Any]:
        if not command_exists("lsblk") or not command_exists("findmnt"):
            return {"devices": [], "available": False, "error": "The util-linux disk tools are not available.", "root": os.geteuid() == 0}
        data = self._lsblk_json()
        flat = self._flatten(data.get("blockdevices", []) or [])
        root_path = self._root_device_path()
        by_path = {os.path.realpath(str(item.get("path", ""))): item for item in flat if item.get("path")}
        root_item = by_path.get(root_path)
        by_name = {str(item.get("name") or ""): item for item in flat if item.get("name")}

        def top_parent(name: str) -> str:
            seen = set()
            current_name = name
            while current_name and current_name not in seen:
                seen.add(current_name)
                current_item = by_name.get(current_name)
                parent_name = str(current_item.get("pkname") or "") if current_item else ""
                if not parent_name:
                    return current_name
                current_name = parent_name
            return current_name

        root_name = str(root_item.get("name") or "") if root_item else ""
        root_top = top_parent(root_name) if root_name else ""
        fstab_entries = self._parse_fstab(self._fstab_text())
        devices: List[Dict[str, Any]] = []
        for item in flat:
            path = str(item.get("path") or "")
            fstype = str(item.get("fstype") or "")
            uuid_value = str(item.get("uuid") or "")
            item_type = str(item.get("type") or "")
            if not path or item_type not in {"part", "disk", "crypt", "lvm"}:
                continue
            mountpoints = self._mountpoints(item)
            parent_name = str(item.get("pkname") or "")
            name = str(item.get("name") or "")
            system_device = bool(root_top and top_parent(name) == root_top) or os.path.realpath(path) == root_path
            critical_mount = any(point in {"/", "/boot", "/boot/firmware", "/usr", "/var", "/home", "/etc"} for point in mountpoints)
            matching = [entry for entry in fstab_entries if uuid_value and entry["uuid"] == uuid_value]
            managed_entry = next((entry for entry in matching if entry["managed"] == "true"), None)
            existing_entry = matching[0] if matching else None
            manageable = bool(fstype and uuid_value and not system_device and not critical_mount and not bool(item.get("ro")))
            devices.append(
                {
                    "name": name,
                    "path": path,
                    "type": item_type,
                    "size": int(item.get("size") or 0),
                    "size_human": human_bytes(int(item.get("size") or 0)),
                    "fstype": fstype,
                    "label": str(item.get("label") or ""),
                    "uuid": uuid_value,
                    "mountpoints": mountpoints,
                    "mounted": bool(mountpoints),
                    "mountpoint": mountpoints[0] if mountpoints else "",
                    "removable": bool(item.get("rm")),
                    "readonly": bool(item.get("ro")),
                    "model": str(item.get("model") or "").strip(),
                    "transport": str(item.get("tran") or ""),
                    "system_device": system_device or critical_mount,
                    "manageable": manageable,
                    "persistent": bool(existing_entry),
                    "managed_persistent": bool(managed_entry),
                    "persistent_target": existing_entry["target"] if existing_entry else "",
                    "persistent_options": existing_entry["options"] if existing_entry else "",
                }
            )
        devices.sort(key=lambda item: (not item["removable"] and item["transport"] != "usb", item["system_device"], item["path"]))
        return {"devices": devices, "available": True, "root": os.geteuid() == 0, "fstab": str(self.fstab_path)}

    def _device(self, path: Any) -> Dict[str, Any]:
        value = safe_text(path, 256)
        for device in self.inventory().get("devices", []):
            if device["path"] == value:
                return device
        raise ValueError("The selected disk or partition is no longer available")

    def _write_fstab(self, new_text: str) -> None:
        original = self._fstab_text()
        if self.fstab_path == FSTAB_PATH:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = pathlib.Path(f"/etc/fstab.serverdeck-{stamp}.bak")
            atomic_write(backup, original, 0o600)
        atomic_write(self.fstab_path, new_text, 0o644)

    @staticmethod
    def _remove_managed_entry(text: str, uuid_value: str) -> Tuple[str, bool]:
        lines = text.splitlines()
        output: List[str] = []
        removed = False
        index = 0
        marker = FSTAB_MARKER_PREFIX + uuid_value
        while index < len(lines):
            if lines[index].strip() == marker:
                removed = True
                index += 1
                if index < len(lines):
                    fields = lines[index].strip().split()
                    if fields and fields[0] == "UUID=" + uuid_value:
                        index += 1
                continue
            output.append(lines[index])
            index += 1
        return "\n".join(output).rstrip() + ("\n" if output else ""), removed

    def mount(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _require_root("Disk mounting requires ServerDeck to run as root")
        with self._lock:
            device = self._device(payload.get("device", ""))
            if not device["manageable"]:
                raise PermissionError("ServerDeck will not modify the system disk, read-only devices, or filesystems without a UUID")
            mountpoint = validate_mountpoint(payload.get("mountpoint", ""))
            persistent = bool(payload.get("persistent", False))
            noatime = bool(payload.get("noatime", False))
            automount = bool(payload.get("automount", False))
            pathlib.Path(mountpoint).mkdir(parents=True, exist_ok=True, mode=0o755)
            current = device.get("mountpoint", "")
            if current and current != mountpoint:
                result = run_command(["umount", current], timeout=60)
                if result.returncode != 0:
                    raise RuntimeError("Unable to unmount the current location: " + result.stdout.strip())
            if persistent:
                original = self._fstab_text()
                entries = self._parse_fstab(original)
                unmanaged = [entry for entry in entries if entry["uuid"] == device["uuid"] and entry["managed"] != "true"]
                if unmanaged:
                    raise RuntimeError(f"This filesystem already has an unmanaged /etc/fstab entry for {unmanaged[0]['target']}; edit it manually or remove it before using ServerDeck")
                cleaned, _ = self._remove_managed_entry(original, device["uuid"])
                options = ["defaults", "nofail", "x-systemd.device-timeout=10s"]
                if noatime:
                    options.append("noatime")
                if automount:
                    options.extend(["x-systemd.automount", "x-systemd.idle-timeout=5min"])
                pass_number = "2" if device["fstype"].lower() in {"ext2", "ext3", "ext4", "xfs", "btrfs"} else "0"
                entry = (
                    f"{FSTAB_MARKER_PREFIX}{device['uuid']}\n"
                    f"UUID={device['uuid']} {_fstab_escape(mountpoint)} {device['fstype']} {','.join(options)} 0 {pass_number}\n"
                )
                new_text = cleaned.rstrip() + "\n\n" + entry if cleaned.strip() else entry
                self._write_fstab(new_text)
                try:
                    if command_exists("systemctl"):
                        run_command(["systemctl", "daemon-reload"], timeout=20)
                    if current != mountpoint:
                        result = run_command(["mount", mountpoint], timeout=90)
                        if result.returncode != 0:
                            raise RuntimeError(result.stdout.strip() or "mount failed")
                except Exception:
                    atomic_write(self.fstab_path, original, 0o644)
                    if command_exists("systemctl"):
                        run_command(["systemctl", "daemon-reload"], timeout=20)
                    raise
            else:
                if current == mountpoint:
                    return {"message": f"{device['path']} is already mounted at {mountpoint}", "device": device["path"], "mountpoint": mountpoint}
                result = run_command(["mount", device["path"], mountpoint], timeout=90)
                if result.returncode != 0:
                    raise RuntimeError("Unable to mount the filesystem: " + result.stdout.strip())
            return {"message": f"Mounted {device['path']} at {mountpoint}", "device": device["path"], "mountpoint": mountpoint, "persistent": persistent}

    def unmount(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _require_root("Unmounting disks requires ServerDeck to run as root")
        with self._lock:
            device = self._device(payload.get("device", ""))
            if device["system_device"]:
                raise PermissionError("The system disk cannot be unmounted from ServerDeck")
            mountpoint = device.get("mountpoint", "")
            if not mountpoint:
                return {"message": f"{device['path']} is not mounted"}
            result = run_command(["umount", mountpoint], timeout=90)
            if result.returncode != 0:
                raise RuntimeError("Unable to unmount the filesystem. It may be busy: " + result.stdout.strip())
            return {"message": f"Unmounted {device['path']} from {mountpoint}"}

    def remove_persistence(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _require_root("Changing persistent mounts requires ServerDeck to run as root")
        with self._lock:
            device = self._device(payload.get("device", ""))
            original = self._fstab_text()
            updated, removed = self._remove_managed_entry(original, device.get("uuid", ""))
            if not removed:
                raise RuntimeError("ServerDeck does not own a persistent mount entry for this filesystem")
            self._write_fstab(updated)
            if command_exists("systemctl"):
                run_command(["systemctl", "daemon-reload"], timeout=20)
            return {"message": "Persistent mounting was removed. The current mount remains active until it is unmounted or the server restarts."}


class NetworkSettingsManager:
    """NetworkManager/Netplan interface inspection and IPv4 configuration."""

    def __init__(self, port: int):
        self.port = port
        self._lock = threading.RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _nmcli_available() -> bool:
        if not command_exists("nmcli"):
            return False
        result = run_command(["nmcli", "-t", "-f", "RUNNING", "general"], timeout=10)
        return result.returncode == 0 and result.stdout.strip().lower() in {"running", "yes", "true", "enabled"}

    @staticmethod
    def _netplan_available() -> bool:
        if not command_exists("netplan") or not pathlib.Path("/etc/netplan").is_dir():
            return False
        result = run_command(["netplan", "set", "--help"], timeout=10)
        return result.returncode == 0

    @staticmethod
    def _netplan_group(interface: str) -> str:
        return "wifis" if pathlib.Path("/sys/class/net", interface, "wireless").exists() else "ethernets"

    @staticmethod
    def _netplan_origin(interface: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", interface)
        return "99-serverdeck-" + safe_name

    @staticmethod
    def _netplan_path(interface: str) -> pathlib.Path:
        return pathlib.Path("/etc/netplan") / (NetworkSettingsManager._netplan_origin(interface) + ".yaml")

    @staticmethod
    def _netplan_get(interface: str, field: str) -> str:
        group = NetworkSettingsManager._netplan_group(interface)
        result = run_command(["netplan", "get", f"{group}.{interface}.{field}"], timeout=15)
        if result.returncode != 0:
            return ""
        return result.stdout.strip().strip('"')

    @staticmethod
    def _netplan_set(interface: str, field: str, value: str) -> None:
        group = NetworkSettingsManager._netplan_group(interface)
        origin = NetworkSettingsManager._netplan_origin(interface)
        result = run_command(["netplan", "set", "--origin-hint", origin, f"{group}.{interface}.{field}={value}"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError("Netplan rejected the settings: " + result.stdout.strip())

    @staticmethod
    def _netplan_apply() -> None:
        generated = run_command(["netplan", "generate"], timeout=60)
        if generated.returncode != 0:
            raise RuntimeError("Netplan validation failed: " + generated.stdout.strip())
        applied = run_command(["netplan", "apply"], timeout=90)
        if applied.returncode != 0:
            raise RuntimeError("Netplan could not apply the settings: " + applied.stdout.strip())

    @staticmethod
    def _active_profiles() -> Dict[str, Dict[str, str]]:
        if not NetworkSettingsManager._nmcli_available():
            return {}
        result = run_command(["nmcli", "-t", "--escape", "no", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], timeout=15)
        profiles: Dict[str, Dict[str, str]] = {}
        if result.returncode != 0:
            return profiles
        for line in result.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4:
                continue
            device, kind, state, connection = parts
            profiles[device] = {"type": kind, "state": state, "connection": "" if connection == "--" else connection}
        return profiles

    @staticmethod
    def _profile_value(profile: str, property_name: str) -> str:
        result = run_command(["nmcli", "-g", property_name, "connection", "show", profile], timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"Unable to read NetworkManager profile {profile}: {result.stdout.strip()}")
        return result.stdout.strip()

    def _profile_snapshot(self, profile: str) -> Dict[str, str]:
        properties = ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns", "ipv4.ignore-auto-dns", "connection.autoconnect"]
        values = {name: self._profile_value(profile, name) for name in properties}
        values["ipv4.addresses"] = ",".join(line.strip() for line in values["ipv4.addresses"].splitlines() if line.strip())
        values["ipv4.dns"] = ",".join(line.strip() for line in values["ipv4.dns"].splitlines() if line.strip())
        return values

    @staticmethod
    def _ip_inventory() -> List[Dict[str, Any]]:
        if not command_exists("ip"):
            return []
        result = run_command(["ip", "-j", "address", "show"], timeout=15)
        if result.returncode != 0:
            return []
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _default_gateways() -> Dict[str, str]:
        if not command_exists("ip"):
            return {}
        result = run_command(["ip", "-j", "route", "show", "default"], timeout=15)
        if result.returncode != 0:
            return {}
        try:
            routes = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        gateways: Dict[str, str] = {}
        for route in routes if isinstance(routes, list) else []:
            device = str(route.get("dev") or "")
            gateway = str(route.get("gateway") or "")
            if device and gateway and device not in gateways:
                gateways[device] = gateway
        return gateways

    @staticmethod
    def _dns_servers() -> List[str]:
        servers: List[str] = []
        try:
            for line in pathlib.Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] == "nameserver":
                    servers.append(fields[1])
        except OSError:
            pass
        return servers

    def inventory(self) -> Dict[str, Any]:
        profiles = self._active_profiles()
        nm_available = self._nmcli_available()
        netplan_available = not nm_available and self._netplan_available()
        gateways = self._default_gateways()
        dns = self._dns_servers()
        interfaces: List[Dict[str, Any]] = []
        for item in self._ip_inventory():
            name = str(item.get("ifname") or "")
            if not name or name == "lo":
                continue
            profile = profiles.get(name, {})
            ipv4 = []
            for address in item.get("addr_info", []) or []:
                if address.get("family") == "inet" and address.get("local"):
                    ipv4.append(f"{address['local']}/{address.get('prefixlen', 32)}")
            connection = profile.get("connection", "")
            method = ""
            profile_dns = ""
            if nm_available and connection:
                try:
                    method = self._profile_value(connection, "ipv4.method")
                    profile_dns = self._profile_value(connection, "ipv4.dns")
                except RuntimeError:
                    pass
            elif netplan_available:
                dhcp4 = self._netplan_get(name, "dhcp4").lower()
                method = "auto" if dhcp4 == "true" else ("manual" if ipv4 else "")
            interfaces.append(
                {
                    "name": name,
                    "state": str(item.get("operstate") or profile.get("state") or "unknown"),
                    "mac": str(item.get("address") or ""),
                    "mtu": int(item.get("mtu") or 0),
                    "type": profile.get("type", str(item.get("link_type") or "")),
                    "connection": connection or ("Netplan" if netplan_available else ""),
                    "ipv4": ipv4,
                    "gateway": gateways.get(name, ""),
                    "dns": [entry for entry in profile_dns.replace(",", " ").split() if entry] or dns,
                    "method": method,
                    "editable": bool((nm_available and connection or netplan_available) and name != "lo"),
                }
            )
        interfaces.sort(key=lambda entry: (entry["state"] not in {"UP", "up", "connected"}, entry["name"]))
        backend = "NetworkManager" if nm_available else ("Netplan" if netplan_available else "Read-only")
        if nm_available:
            explanation = "NetworkManager is active and interfaces with an active connection profile can be changed."
        elif netplan_available:
            explanation = "Netplan is available. ServerDeck stores per-interface overrides in /etc/netplan/99-serverdeck-*.yaml and validates them before applying."
        else:
            explanation = "No supported writable backend was detected. Install/enable NetworkManager or Netplan to change addresses from ServerDeck; interface details remain read-only."
        with self._lock:
            pending = [{"token": token, "interface": data["interface"], "target_ip": data.get("target_ip", ""), "expires": data["expires"]} for token, data in self._pending.items()]
        return {"interfaces": interfaces, "backend": backend, "editable": (nm_available or netplan_available) and os.geteuid() == 0, "root": os.geteuid() == 0, "explanation": explanation, "pending": pending}

    def _connection_profile(self, interface: str) -> str:
        profile = self._active_profiles().get(interface, {}).get("connection", "")
        if not profile:
            raise RuntimeError("The selected interface does not have an active NetworkManager connection profile")
        return profile

    @staticmethod
    def _parse_dns(raw: Any) -> List[str]:
        value = safe_text(raw or "", 512)
        if not value:
            return []
        entries = [item for item in re.split(r"[\s,]+", value) if item]
        if len(entries) > 6:
            raise ValueError("Enter no more than six DNS servers")
        for entry in entries:
            ipaddress.ip_address(entry)
        return entries

    @staticmethod
    def _modify_profile(profile: str, values: Dict[str, str]) -> None:
        args = ["nmcli", "connection", "modify", profile]
        for key, value in values.items():
            args.extend([key, value])
        result = run_command(args, timeout=40)
        if result.returncode != 0:
            raise RuntimeError("NetworkManager rejected the settings: " + result.stdout.strip())

    @staticmethod
    def _activate(profile: str, interface: str) -> None:
        result = run_command(["nmcli", "connection", "up", profile, "ifname", interface], timeout=90)
        if result.returncode != 0:
            raise RuntimeError("NetworkManager could not activate the connection: " + result.stdout.strip())

    def apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _require_root("Network changes require ServerDeck to run as root")
        nm_available = self._nmcli_available()
        netplan_available = not nm_available and self._netplan_available()
        if not nm_available and not netplan_available:
            raise RuntimeError("No supported writable network backend is active. Enable NetworkManager or Netplan first.")
        interface = safe_text(payload.get("interface", ""), 64)
        if not INTERFACE_RE.fullmatch(interface) or interface == "lo" or not pathlib.Path("/sys/class/net", interface).exists():
            raise ValueError("Choose a valid network interface")
        mode = safe_text(payload.get("mode", ""), 16).lower()
        if mode not in {"dhcp", "static"}:
            raise ValueError("Choose DHCP or static IPv4")
        profile = self._connection_profile(interface) if nm_available else "Netplan"
        previous = self._profile_snapshot(profile) if nm_available else {}
        netplan_path = self._netplan_path(interface) if netplan_available else None
        netplan_existed = bool(netplan_path and netplan_path.exists())
        netplan_previous = netplan_path.read_text(encoding="utf-8") if netplan_existed and netplan_path else ""
        target_ip = ""
        if mode == "dhcp":
            values = {
                "ipv4.method": "auto",
                "ipv4.addresses": "",
                "ipv4.gateway": "",
                "ipv4.dns": "",
                "ipv4.ignore-auto-dns": "no",
                "connection.autoconnect": "yes",
            }
        else:
            raw_address = safe_text(payload.get("address", ""), 64)
            try:
                address = ipaddress.IPv4Interface(raw_address)
            except ValueError as exc:
                raise ValueError("Enter the static address with its prefix, for example 192.168.1.50/24") from exc
            target_ip = str(address.ip)
            raw_gateway = safe_text(payload.get("gateway", ""), 64)
            if raw_gateway:
                try:
                    gateway = ipaddress.IPv4Address(raw_gateway)
                except ValueError as exc:
                    raise ValueError("Enter a valid IPv4 gateway") from exc
                if gateway not in address.network:
                    raise ValueError("The gateway must be inside the selected IPv4 subnet")
            dns = self._parse_dns(payload.get("dns", ""))
            values = {
                "ipv4.method": "manual",
                "ipv4.addresses": str(address),
                "ipv4.gateway": raw_gateway,
                "ipv4.dns": ",".join(dns),
                "ipv4.ignore-auto-dns": "yes" if dns else "no",
                "connection.autoconnect": "yes",
            }
        def apply_netplan_settings() -> None:
            self._netplan_set(interface, "dhcp4", "true" if mode == "dhcp" else "false")
            self._netplan_set(interface, "addresses", "null" if mode == "dhcp" else f"[{values['ipv4.addresses']}]")
            if mode == "dhcp" or not values["ipv4.gateway"]:
                self._netplan_set(interface, "routes", "null")
            else:
                self._netplan_set(interface, "routes", f"[{{to: default, via: {values['ipv4.gateway']}}}]")
            if mode == "dhcp" or not values["ipv4.dns"]:
                self._netplan_set(interface, "nameservers", "null")
            else:
                dns_yaml = ", ".join(values["ipv4.dns"].split(","))
                self._netplan_set(interface, "nameservers.addresses", f"[{dns_yaml}]")
            self._netplan_apply()

        def restore_netplan_settings() -> None:
            assert netplan_path is not None
            if netplan_existed:
                atomic_write(netplan_path, netplan_previous, 0o600)
            else:
                try:
                    netplan_path.unlink()
                except FileNotFoundError:
                    pass
            self._netplan_apply()

        token = secrets.token_urlsafe(32) if mode == "static" else ""
        confirmed = threading.Event()
        if token:
            with self._lock:
                self._pending[token] = {"event": confirmed, "interface": interface, "profile": profile, "target_ip": target_ip, "expires": time.time() + 120}

        def worker() -> None:
            time.sleep(2.0)
            try:
                if nm_available:
                    self._modify_profile(profile, values)
                    self._activate(profile, interface)
                else:
                    apply_netplan_settings()
                if token and not confirmed.wait(90):
                    print(f"Network change for {interface} was not confirmed; rolling back.", file=sys.stderr)
                    if nm_available:
                        self._modify_profile(profile, previous)
                        self._activate(profile, interface)
                    else:
                        restore_netplan_settings()
            except Exception as exc:
                print(f"Unable to apply network settings for {interface}: {exc}", file=sys.stderr)
                try:
                    if nm_available:
                        self._modify_profile(profile, previous)
                        self._activate(profile, interface)
                    else:
                        restore_netplan_settings()
                except Exception as rollback_exc:
                    print(f"Network rollback also failed for {interface}: {rollback_exc}", file=sys.stderr)
            finally:
                if token:
                    with self._lock:
                        self._pending.pop(token, None)

        threading.Thread(target=worker, name=f"serverdeck-network-{interface}", daemon=True).start()
        message = "DHCP settings will be applied in two seconds. The current browser connection may close." if mode == "dhcp" else "Static IPv4 settings will be applied in two seconds. Open the confirmation address within 90 seconds or ServerDeck will restore the previous settings."
        return {"accepted": True, "mode": mode, "interface": interface, "profile": profile, "backend": "NetworkManager" if nm_available else "Netplan", "target_ip": target_ip, "token": token, "message": message}

    def confirm(self, token: str) -> bool:
        token = token.strip()
        with self._lock:
            pending = self._pending.get(token)
            if not pending or time.time() > pending["expires"]:
                return False
            pending["event"].set()
            return True


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
*{box-sizing:border-box}html{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0%,rgba(55,105,180,.22),transparent 35%),var(--bg)}button,input,select,textarea{font:inherit}button{cursor:pointer}.shell{max-width:1180px;margin:0 auto;padding:0 22px 50px}.topbar{position:sticky;top:0;z-index:10;background:rgba(11,16,32,.9);backdrop-filter:blur(16px);border-bottom:1px solid var(--border)}.topbar-inner{max-width:1180px;margin:0 auto;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.2px}.brand-mark{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;color:#07111f;font-weight:900}.top-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.nav{display:flex;gap:7px;flex-wrap:wrap}.nav a{color:var(--muted);text-decoration:none;padding:9px 13px;border-radius:9px;font-weight:650}.nav a:hover,.nav a.active{background:var(--panel2);color:var(--text)}.power-menu-button{border:1px solid var(--border);background:transparent;color:var(--muted);padding:8px 11px;border-radius:9px;font-weight:750;display:inline-flex;align-items:center;gap:8px}.power-menu-button:hover,.power-menu-button:focus{background:var(--panel2);color:var(--text);outline:none}.power-symbol{font-size:1.15rem;line-height:1;color:var(--accent2)}.account{display:flex;align-items:center;gap:9px;padding-left:12px;border-left:1px solid var(--border)}.account-name{color:var(--muted);font-size:.86rem;font-weight:750}.logout-button{border:0;background:transparent;color:var(--accent);padding:6px;font-weight:750}.logout-button:hover{text-decoration:underline}.login-shell{min-height:100vh;display:grid;place-items:center;padding:22px}.login-card{width:min(430px,100%);background:linear-gradient(180deg,rgba(25,34,56,.98),rgba(19,26,45,.98));border:1px solid var(--border);border-radius:20px;padding:28px;box-shadow:var(--shadow)}.login-brand{display:flex;align-items:center;gap:12px;margin-bottom:24px}.login-card h1{margin:0 0 8px;font-size:1.8rem}.login-card p{color:var(--muted);line-height:1.5;margin:0 0 20px}.login-card .button{width:100%;margin-top:8px}main{padding-top:32px}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:24px}.hero h1{font-size:clamp(1.8rem,4vw,2.7rem);margin:0 0 8px}.hero p{margin:0;color:var(--muted);max-width:720px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}.card{background:linear-gradient(180deg,rgba(25,34,56,.96),rgba(19,26,45,.96));border:1px solid var(--border);border-radius:16px;padding:20px;box-shadow:var(--shadow)}.span-4{grid-column:span 4}.span-5{grid-column:span 5}.span-6{grid-column:span 6}.span-7{grid-column:span 7}.span-8{grid-column:span 8}.span-12{grid-column:span 12}.metric-label{font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:800}.metric-value{font-size:2rem;font-weight:850;margin:7px 0}.metric-detail{color:var(--muted);font-size:.92rem}.progress{height:9px;background:#0d1427;border-radius:999px;overflow:hidden;margin:16px 0 10px}.progress>span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:inherit;transition:width .35s ease}.section-title{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-bottom:15px}.section-title h2,.section-title h3{margin:0}.hostname-button{appearance:none;border:0;background:transparent;color:var(--text);padding:0;text-align:left}.hostname-button:hover .hostname{text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:4px}.hostname{font-size:1.7rem;font-weight:850}.pill{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:999px;font-size:.82rem;font-weight:750;background:#17243c;color:var(--muted);border:1px solid var(--border)}.pill.good{color:var(--accent2)}.pill.warn{color:var(--warning)}.pill.bad{color:var(--danger)}.button{border:1px solid var(--border);background:var(--panel2);color:var(--text);padding:10px 14px;border-radius:10px;font-weight:750;display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none}.button:hover{filter:brightness(1.12)}.button.primary{background:linear-gradient(135deg,#327de8,#5b9cff);border-color:#66a3ff}.button.danger{background:#3b1b28;border-color:#693043;color:#ffb9c0}.button.ghost{background:transparent}.button:disabled{opacity:.5;cursor:not-allowed}.actions{display:flex;gap:10px;flex-wrap:wrap}.notice{border:1px solid var(--border);background:rgba(25,34,56,.72);padding:13px 15px;border-radius:12px;color:var(--muted);line-height:1.45}.notice.warning{border-color:#6e552a;color:#ffd995;background:#2a2116}.notice.danger{border-color:#733141;color:#ffc0c7;background:#2c1620}.notice.good{border-color:#275e53;color:#a7f0d9;background:#122923}.stack{display:flex;flex-direction:column;gap:14px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.field{display:flex;flex-direction:column;gap:7px}.field.full{grid-column:1/-1}.field label{font-size:.88rem;color:var(--muted);font-weight:700}.input,.select,.textarea{width:100%;border:1px solid var(--border);background:#0e1528;color:var(--text);border-radius:10px;padding:11px 12px;outline:none}.input:focus,.select:focus,.textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(101,167,255,.13)}.input-with-button{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.browse-button{white-space:nowrap}.browser-box{width:min(720px,100%);max-height:min(760px,92vh);display:flex;flex-direction:column}.browser-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}.browser-path{padding:10px 12px;border:1px solid var(--border);border-radius:10px;background:#0e1528;overflow-wrap:anywhere;margin-bottom:12px}.browser-list{min-height:220px;max-height:420px;overflow:auto;border:1px solid var(--border);border-radius:12px;background:#0b1222}.browser-entry{width:100%;display:flex;align-items:center;gap:10px;text-align:left;border:0;border-bottom:1px solid var(--border);background:transparent;color:var(--text);padding:11px 13px}.browser-entry:last-child{border-bottom:0}.browser-entry:hover,.browser-entry:focus{background:var(--panel2);outline:none}.browser-entry-icon{color:var(--accent);font-weight:900}.browser-entry-name{overflow-wrap:anywhere}.browser-status{color:var(--muted);padding:36px 18px;text-align:center}.help{font-size:.8rem;color:var(--muted);line-height:1.45}.checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.check{display:flex;align-items:flex-start;gap:10px;border:1px solid var(--border);padding:11px;border-radius:11px;background:#11192c}.check input{margin-top:3px}.check strong{display:block;font-size:.92rem}.check small{display:block;color:var(--muted);margin-top:3px}.table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:650px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--border)}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;background:#11192c}tr:last-child td{border-bottom:0}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.output{background:#070b15;border:1px solid var(--border);border-radius:12px;padding:14px;min-height:180px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:#cfe0ff;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.job{border:1px solid var(--border);border-radius:14px;padding:16px;background:#11192c}.job-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.job h3{margin:0 0 5px}.path{color:var(--muted);font-size:.86rem;overflow-wrap:anywhere}.job-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.job-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.empty{text-align:center;color:var(--muted);padding:40px 20px}.modal{position:fixed;inset:0;background:rgba(3,6,13,.72);display:none;place-items:center;padding:20px;z-index:100}.modal.open{display:grid}.modal-box{width:min(500px,100%);background:var(--panel);border:1px solid var(--border);border-radius:17px;padding:22px;box-shadow:var(--shadow)}.modal-box h2{margin-top:0}.split{display:flex;align-items:center;justify-content:space-between;gap:12px}.spinner{width:17px;height:17px;border-radius:50%;border:2px solid rgba(255,255,255,.28);border-top-color:#fff;animation:spin .8s linear infinite;display:none}.busy .spinner{display:inline-block}@keyframes spin{to{transform:rotate(360deg)}}.footer{margin-top:35px;color:var(--muted);font-size:.82rem;text-align:center}.hidden{display:none!important}.device-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.device-card{border:1px solid var(--border);border-radius:14px;padding:16px;background:#11192c}.device-card h3{margin:0 0 5px}.device-details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}.device-details div{font-size:.84rem;color:var(--muted)}.device-details strong{display:block;color:var(--text);font-size:.9rem;margin-top:2px;overflow-wrap:anywhere}.interface-list{display:flex;flex-direction:column;gap:12px}.interface-card{border:1px solid var(--border);border-radius:14px;padding:16px;background:#11192c}.interface-card-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.interface-values{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:13px}.interface-values div{color:var(--muted);font-size:.8rem}.interface-values strong{display:block;color:var(--text);font-size:.9rem;margin-top:3px;overflow-wrap:anywhere}.terminal-card{padding:0;overflow:hidden}.terminal-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--border);background:#10182a}.terminal-output{margin:0;min-height:460px;max-height:65vh;overflow:auto;padding:18px;background:#050810;color:#d9e7ff;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;tab-size:4}.terminal-command{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:10px;padding:13px 16px;border-top:1px solid var(--border);background:#0b1222}.terminal-prompt{color:var(--accent2);font:700 1rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.terminal-input{border:0;background:transparent;color:var(--text);outline:none;min-width:0;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.terminal-input::placeholder{color:#68758f}.terminal-toolbar-group{display:flex;gap:8px;align-items:center;flex-wrap:wrap}@media(max-width:820px){.device-grid{grid-template-columns:1fr}.interface-values{grid-template-columns:1fr 1fr}.span-4,.span-5,.span-6,.span-7,.span-8{grid-column:span 12}.hero{flex-direction:column}.topbar-inner{align-items:flex-start;flex-direction:column}.form-grid,.checks{grid-template-columns:1fr}.field.full{grid-column:auto}.shell{padding-left:14px;padding-right:14px}.topbar-inner{padding-left:14px;padding-right:14px}.top-actions,.account{width:100%}.account{padding-left:0;border-left:0;justify-content:space-between}}
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
    items = [("overview", "/", "Overview"), ("updates", "/updates", "Updates"), ("backup", "/backup", "rSync"), ("disks", "/disks", "Disks"), ("network", "/network", "Network"), ("copyparty", "/copyparty", "CopyParty"), ("terminal", "/terminal", "Terminal")]
    links = "".join(f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>' for key, href, label in items)
    power_button = (
        '<button class="power-menu-button" id="power-menu-open" type="button" aria-haspopup="dialog" '
        'aria-controls="power-modal" title="Power Options"><span class="power-symbol" aria-hidden="true">&#x23FB;</span>'
        '<span class="power-label">Power Options</span></button>'
    )
    account = (
        f'<div class="account"><span class="account-name">{html.escape(username)}</span>'
        f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf_token)}">'
        '<button class="logout-button" type="submit">Log out</button></form></div>'
    )
    return f'<div class="top-actions"><nav class="nav" aria-label="Primary navigation">{links}</nav>{power_button}{account}</div>'


POWER_MODAL_HTML = r"""
<div class="modal" id="power-modal" role="dialog" aria-modal="true" aria-labelledby="power-title">
  <div class="modal-box">
    <div class="section-title">
      <div><h2 id="power-title">Power Options</h2><div class="metric-detail">Restart or safely shut down this server.</div></div>
      <span class="pill warn">Administrator only</span>
    </div>
    <div class="notice warning">Active SSH sessions, updates, Terminal commands, and rSync jobs will be interrupted.</div>
    <div class="actions" style="margin-top:18px">
      <button class="button" id="restart-btn"><span class="spinner"></span>Restart server</button>
      <button class="button danger" id="shutdown-btn"><span class="spinner"></span>Shut down server</button>
      <button class="button ghost" id="power-cancel">Cancel</button>
    </div>
    <div id="power-notice" class="notice hidden" style="margin-top:14px"></div>
  </div>
</div>
"""

POWER_JS = r"""
const restartButton=document.getElementById('restart-btn');
const shutdownButton=document.getElementById('shutdown-btn');
const powerModal=document.getElementById('power-modal');
const powerMenuButton=document.getElementById('power-menu-open');
const powerCancelButton=document.getElementById('power-cancel');
function closePowerModal(){powerModal.classList.remove('open')}
powerMenuButton.onclick=()=>{hideNotice('power-notice');powerModal.classList.add('open');restartButton.focus()};
powerCancelButton.onclick=closePowerModal;
powerModal.addEventListener('click',event=>{if(event.target===powerModal)closePowerModal()});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&powerModal.classList.contains('open'))closePowerModal()});
function setPowerButtonsDisabled(disabled){restartButton.disabled=disabled;shutdownButton.disabled=disabled;powerCancelButton.disabled=disabled;}
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
    ?'Restart this server now? Active SSH sessions, updates, Terminal commands and rSync jobs will be interrupted.'
    :'Shut down this server now? It will remain unavailable until it is physically powered on again.';
  if(!window.confirm(warning))return;
  setPowerButtonsDisabled(true);
  setBusy(restarting?restartButton:shutdownButton,true);
  showNotice('power-notice','Sending '+label+' request…','warning');
  try{
    const result=await api('/api/power',{method:'POST',body:{action:action,confirmation:action.toUpperCase()}});
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
"""


def page(title: str, active: str, body: str, script: str, csrf_token: str, username: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="csrf-token" content="{html.escape(csrf_token)}">'
        f'<title>{html.escape(title)} · {APP_NAME}</title><style>{CSS}</style></head>'
        f'<body><header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">SD</span><span>{APP_NAME}</span></div>'
        f'{nav(active, username, csrf_token)}</div></header>'
        f'<div class="shell"><main>{body}</main><div class="footer">{APP_NAME} {APP_VERSION} · Build {APP_BUILD} · Signed in as {html.escape(username)} · Keep this service on a trusted LAN, VPN, or HTTPS.</div></div>'
        f'{POWER_MODAL_HTML}<script>{COMMON_JS}\n{POWER_JS}\n{script}</script></body></html>'
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
  <article class="card span-7"><div class="section-title"><div><div class="metric-label">Hostname</div><button class="hostname-button" id="hostname-open" title="Click to rename"><span class="hostname" id="hostname">—</span></button></div><span class="pill">Click name to edit</span></div><div class="metric-detail" id="os-name">—</div></article>
  <article class="card span-5"><div class="metric-label">Uptime</div><div class="metric-value" id="uptime">—</div><div class="metric-detail" id="load">Load average: —</div></article>
  <article class="card span-4"><div class="metric-label">CPU usage</div><div class="metric-value" id="cpu-value">—</div><div class="progress"><span id="cpu-bar"></span></div><div class="metric-detail" id="cpu-detail">Reading processor activity…</div></article>
  <article class="card span-4"><div class="metric-label">Memory usage</div><div class="metric-value" id="mem-value">—</div><div class="progress"><span id="mem-bar"></span></div><div class="metric-detail" id="mem-detail">Reading memory…</div></article>
  <article class="card span-4"><div class="metric-label">System storage</div><div class="metric-value" id="disk-value">—</div><div class="progress"><span id="disk-bar"></span></div><div class="metric-detail" id="disk-detail">Reading root filesystem…</div></article>
  <article class="card span-6"><div class="metric-label">Download activity</div><div class="metric-value" id="network-download-value">—</div><div class="metric-detail" id="network-download-detail">Measuring received traffic…</div></article>
  <article class="card span-6"><div class="metric-label">Upload activity</div><div class="metric-value" id="network-upload-value">—</div><div class="metric-detail" id="network-upload-detail">Measuring transmitted traffic…</div></article>
  <article class="card span-12"><div class="split"><div><strong>Restart status</strong><div class="metric-detail" id="reboot-text">Checking…</div></div><span id="reboot-pill" class="pill">—</span></div></article>
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
    const network=data.network||{};
    const interfaces=Array.isArray(network.interfaces)&&network.interfaces.length?network.interfaces.join(', '):'no active non-loopback interfaces';
    document.getElementById('network-download-value').textContent=network.download_human||'0 B/s';
    document.getElementById('network-download-detail').textContent='Total received: '+(network.received_total_human||'0 B')+' · '+interfaces;
    document.getElementById('network-upload-value').textContent=network.upload_human||'0 B/s';
    document.getElementById('network-upload-detail').textContent='Total sent: '+(network.transmitted_total_human||'0 B')+' · '+interfaces;
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
loadStats(); statsTimer=setInterval(loadStats,2500);
"""

UPDATES_BODY = r"""
<section class="hero"><div><h1>Updates</h1><p>Refresh Debian or Ubuntu package information, review available upgrades, and install them in one operation.</p></div><span id="update-count" class="pill">Checking…</span></section>
<div id="updates-notice" class="notice warning">Standard update installation uses <span class="mono">apt-get upgrade</span>. <strong>Full Upgrade</strong> uses <span class="mono">apt-get full-upgrade</span> and may install or remove packages to complete dependency changes. Security-only installs are selected from packages whose APT candidate comes from a Debian/Ubuntu security origin. Review backups before major changes.</div>
<div class="grid" style="margin-top:18px">
  <article class="card span-12"><div class="section-title"><div><h2>Available updates</h2><div class="metric-detail" id="refresh-time">Package list refresh: unknown</div></div><div class="actions"><button class="button" id="refresh-btn"><span class="spinner"></span>Refresh list</button><button class="button" id="security-btn"><span class="spinner"></span><span id="security-btn-label">Only install security updates</span></button><button class="button primary" id="install-btn"><span class="spinner"></span>Install all updates</button><button class="button" id="full-upgrade-btn"><span class="spinner"></span>Full Upgrade</button><button class="button danger" id="cleanup-btn"><span class="spinner"></span>Autoclean &amp; Autoremove</button></div></div><div id="updates-table" class="table-wrap"><div class="empty">Checking for available packages…</div></div></article>
  <article class="card span-12"><div class="section-title"><h2>Operation output</h2><span id="task-state" class="pill">Idle</span></div><pre class="output" id="update-output">No update operation has been started in this browser session.</pre></article>
</div>
"""

UPDATES_JS = r"""
let latestTask=null;
async function loadUpdates(){
  try{
    const data=await api('/api/updates');
    const count=document.getElementById('update-count'); count.textContent=data.count+' update'+(data.count===1?'':'s'); count.className='pill '+(data.count?'warn':'good');
    const securityCount=Number(data.security_count||0);
    document.getElementById('security-btn-label').textContent='Only install security updates'+(securityCount?' ('+securityCount+')':'');
    document.getElementById('refresh-time').textContent='Package list refresh: '+(data.last_refresh?new Date(data.last_refresh).toLocaleString():'unknown')+' · Security updates: '+securityCount;
    const holder=document.getElementById('updates-table');
    if(!data.supported){holder.innerHTML='<div class="empty">APT is not available on this system.</div>';return}
    if(data.error && !data.packages.length){holder.innerHTML='<div class="empty">'+esc(data.error)+'</div>';return}
    if(!data.packages.length){holder.innerHTML='<div class="empty">This server is up to date.</div>';return}
    holder.innerHTML='<table><thead><tr><th>Package</th><th>Installed</th><th>Available</th><th>Source</th></tr></thead><tbody>'+data.packages.map(p=>'<tr><td><strong>'+esc(p.name)+'</strong></td><td class="mono">'+esc(p.current)+'</td><td class="mono">'+esc(p.candidate)+'</td><td>'+esc(p.source)+'</td></tr>').join('')+'</tbody></table>';
  }catch(error){showNotice('updates-notice',error.message,'danger')}
}
function watch(task,button){latestTask=task.id;const state=document.getElementById('task-state');state.textContent='Running';state.className='pill warn';setBusy(button,true);pollTask(task.id,'update-output',async finished=>{setBusy(button,false);state.textContent=finished.returncode===0?'Completed':'Failed';state.className='pill '+(finished.returncode===0?'good':'bad');await loadUpdates();});}
document.getElementById('refresh-btn').onclick=async function(){try{watch(await api('/api/updates/refresh',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
document.getElementById('security-btn').onclick=async function(){if(!confirm('Install only updates whose current APT candidate is from a Debian or Ubuntu security origin?'))return;try{watch(await api('/api/updates/security',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
document.getElementById('install-btn').onclick=async function(){if(!confirm('Install all currently available standard updates?'))return;try{watch(await api('/api/updates/install',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
document.getElementById('full-upgrade-btn').onclick=async function(){if(!confirm('Run a full system upgrade? APT may install new packages or remove packages when required to resolve dependency changes.'))return;try{watch(await api('/api/updates/full-upgrade',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
document.getElementById('cleanup-btn').onclick=async function(){if(!confirm('Run apt autoclean and remove packages that APT considers no longer required?'))return;try{watch(await api('/api/updates/cleanup',{method:'POST'}),this)}catch(error){showNotice('updates-notice',error.message,'danger')}};
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



DISKS_BODY = r"""
<section class="hero"><div><h1>Disks</h1><p>Mount existing filesystems and optionally make their mount folders persistent across restarts.</p></div><span id="disks-state" class="pill">Loading…</span></section>
<div class="notice warning" style="margin-bottom:18px">ServerDeck does not format or partition disks. It refuses to modify the detected system disk and writes persistent mounts by filesystem UUID. Unmount a disk before physically disconnecting it.</div>
<div id="disks-notice" class="notice hidden" style="margin-bottom:18px"></div>
<div class="grid">
  <section class="card span-7"><div class="section-title"><div><h2>Available filesystems</h2><div class="metric-detail">USB, removable, and additional non-system filesystems appear here.</div></div><button class="button" id="disks-refresh" type="button">Refresh</button></div><div id="device-list" class="device-grid"><div class="empty">Reading block devices…</div></div></section>
  <section class="card span-5"><div class="section-title"><div><h2>Mount filesystem</h2><div class="metric-detail">Choose a safe folder under /mnt, /media, or /srv.</div></div></div>
    <form id="disk-form" class="stack">
      <div class="field"><label for="disk-device">Filesystem</label><select class="select mono" id="disk-device" required><option value="">Choose a filesystem</option></select></div>
      <div class="field"><label for="disk-mountpoint">Mount folder</label><div class="input-with-button"><input class="input mono" id="disk-mountpoint" placeholder="/mnt/storage" required><button class="button browse-button" id="disk-browse" type="button">Browse</button></div><div class="help">Type a new folder path or select an existing folder. ServerDeck creates the final folder when necessary.</div></div>
      <label class="check"><input id="disk-persistent" type="checkbox" checked><span><strong>Mount automatically after startup</strong><small>Add a ServerDeck-managed UUID entry to /etc/fstab.</small></span></label>
      <label class="check"><input id="disk-noatime" type="checkbox"><span><strong>Reduce access-time writes</strong><small>Add the noatime option.</small></span></label>
      <label class="check"><input id="disk-automount" type="checkbox"><span><strong>Mount on first access</strong><small>Use a systemd automount unit and unmount after five idle minutes.</small></span></label>
      <button class="button primary" id="disk-mount" type="submit"><span class="spinner"></span>Mount filesystem</button>
    </form>
  </section>
</div>
<div class="modal" id="disk-browser-modal" role="dialog" aria-modal="true" aria-labelledby="disk-browser-title"><div class="modal-box browser-box"><div class="section-title"><div><h2 id="disk-browser-title">Choose mount folder</h2><div class="metric-detail">Browse folders on the server.</div></div><button class="button ghost" id="disk-browser-close" type="button">Close</button></div><div class="browser-toolbar"><button class="button" id="disk-browser-root" type="button">Root</button><button class="button" id="disk-browser-up" type="button">Up</button></div><div class="browser-path mono" id="disk-browser-path">/mnt</div><div id="disk-browser-list" class="browser-list"><div class="browser-status">Loading folders…</div></div><div id="disk-browser-notice" class="notice hidden" style="margin-top:12px"></div><div class="actions" style="margin-top:16px"><button class="button primary" id="disk-browser-select" type="button">Use this folder</button><button class="button ghost" id="disk-browser-cancel" type="button">Cancel</button></div></div></div>
"""

DISKS_JS = r"""
let diskData=[];
const diskSelect=document.getElementById('disk-device');
const diskMountpoint=document.getElementById('disk-mountpoint');
function suggestedMountpoint(device){const base=(device.label||device.name||'storage').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'')||'storage';return '/mnt/'+base;}
function deviceTitle(device){return (device.label?device.label+' · ':'')+device.path;}
function renderDisks(){const list=document.getElementById('device-list');list.textContent='';diskSelect.innerHTML='<option value="">Choose a filesystem</option>';const manageable=diskData.filter(item=>item.manageable);for(const device of manageable){const option=document.createElement('option');option.value=device.path;option.textContent=deviceTitle(device)+' · '+device.size_human+' · '+device.fstype;diskSelect.appendChild(option);}if(!diskData.length){list.innerHTML='<div class="empty">No block filesystems were detected.</div>';return;}for(const device of diskData){const card=document.createElement('article');card.className='device-card';const status=device.system_device?'System disk':device.mounted?'Mounted':'Not mounted';const badges='<span class="pill '+(device.system_device?'warn':device.mounted?'good':'')+'">'+esc(status)+'</span>'+(device.persistent?'<span class="pill good">Persistent</span>':'');card.innerHTML='<div class="device-card-head"><div><h3>'+esc(device.label||device.name)+'</h3><div class="path mono">'+esc(device.path)+'</div></div><div class="actions">'+badges+'</div></div><div class="device-details"><div>Size<strong>'+esc(device.size_human)+'</strong></div><div>Filesystem<strong>'+esc(device.fstype||'Unformatted / unsupported')+'</strong></div><div>Mount folder<strong>'+esc(device.mountpoint||device.persistent_target||'—')+'</strong></div><div>Connection<strong>'+esc(device.transport||((device.removable)?'removable':'internal'))+'</strong></div></div><div class="job-actions">'+(device.manageable?'<button class="button" type="button" data-configure="'+esc(device.path)+'">Configure</button>':'')+(device.mounted&&!device.system_device?'<button class="button" type="button" data-unmount="'+esc(device.path)+'">Unmount</button>':'')+(device.managed_persistent?'<button class="button danger" type="button" data-remove-persistence="'+esc(device.path)+'">Remove persistence</button>':'')+'</div>';list.appendChild(card);}list.querySelectorAll('[data-configure]').forEach(button=>button.onclick=()=>configureDisk(button.dataset.configure));list.querySelectorAll('[data-unmount]').forEach(button=>button.onclick=()=>unmountDisk(button.dataset.unmount));list.querySelectorAll('[data-remove-persistence]').forEach(button=>button.onclick=()=>removePersistence(button.dataset.removePersistence));}
async function loadDisks(){hideNotice('disks-notice');try{const data=await api('/api/disks');diskData=data.devices||[];const state=document.getElementById('disks-state');state.textContent=data.available?(data.root?'Disk tools ready':'Read only'):'Unavailable';state.className='pill '+(data.available&&data.root?'good':'warn');if(data.error)showNotice('disks-notice',data.error,'warning');renderDisks();}catch(error){showNotice('disks-notice',error.message,'danger')}}
function configureDisk(path){const device=diskData.find(item=>item.path===path);if(!device)return;diskSelect.value=device.path;diskMountpoint.value=device.persistent_target||device.mountpoint||suggestedMountpoint(device);document.getElementById('disk-persistent').checked=device.persistent||true;document.getElementById('disk-noatime').checked=(device.persistent_options||'').split(',').includes('noatime');document.getElementById('disk-automount').checked=(device.persistent_options||'').split(',').includes('x-systemd.automount');diskMountpoint.focus();window.scrollTo({top:0,behavior:'smooth'});}
diskSelect.onchange=()=>{const device=diskData.find(item=>item.path===diskSelect.value);if(device&&!diskMountpoint.value)diskMountpoint.value=device.persistent_target||device.mountpoint||suggestedMountpoint(device);};
document.getElementById('disk-form').onsubmit=async event=>{event.preventDefault();const button=document.getElementById('disk-mount');if(!diskSelect.value)return;const persistent=document.getElementById('disk-persistent').checked;if(!confirm((persistent?'Mount this filesystem and add a persistent startup entry?':'Mount this filesystem for the current boot only?')))return;setBusy(button,true);try{const result=await api('/api/disks/mount',{method:'POST',body:{device:diskSelect.value,mountpoint:diskMountpoint.value,persistent:persistent,noatime:document.getElementById('disk-noatime').checked,automount:document.getElementById('disk-automount').checked}});showNotice('disks-notice',result.message,'good');await loadDisks();}catch(error){showNotice('disks-notice',error.message,'danger')}finally{setBusy(button,false)}};
async function unmountDisk(path){if(!confirm('Unmount this filesystem now? Programs using files on it may fail.'))return;try{const result=await api('/api/disks/unmount',{method:'POST',body:{device:path}});showNotice('disks-notice',result.message,'good');await loadDisks();}catch(error){showNotice('disks-notice',error.message,'danger')}}
async function removePersistence(path){if(!confirm('Remove ServerDeck\'s persistent mount entry? The filesystem will remain mounted until you unmount it or restart.'))return;try{const result=await api('/api/disks/persistence/remove',{method:'POST',body:{device:path}});showNotice('disks-notice',result.message,'good');await loadDisks();}catch(error){showNotice('disks-notice',error.message,'danger')}}
document.getElementById('disks-refresh').onclick=loadDisks;
const diskBrowserModal=document.getElementById('disk-browser-modal'),diskBrowserList=document.getElementById('disk-browser-list'),diskBrowserPathLabel=document.getElementById('disk-browser-path');let diskBrowserPath='/mnt',diskBrowserParent='/';
function closeDiskBrowser(){diskBrowserModal.classList.remove('open')}
async function loadDiskBrowser(path){diskBrowserList.innerHTML='<div class="browser-status">Loading folders…</div>';hideNotice('disk-browser-notice');try{const data=await api('/api/filesystem?path='+encodeURIComponent(path));diskBrowserPath=data.path;diskBrowserParent=data.parent;diskBrowserPathLabel.textContent=data.path;document.getElementById('disk-browser-up').disabled=!data.parent;diskBrowserList.textContent='';if(!data.entries.length){diskBrowserList.innerHTML='<div class="browser-status">No subfolders are visible here.</div>';return;}for(const entry of data.entries){const button=document.createElement('button');button.type='button';button.className='browser-entry';button.innerHTML='<span class="browser-entry-icon">▸</span><span class="browser-entry-name mono">'+esc(entry.name)+'</span>';button.onclick=()=>loadDiskBrowser(entry.path);diskBrowserList.appendChild(button);}}catch(error){diskBrowserList.innerHTML='<div class="browser-status">Unable to display this folder.</div>';showNotice('disk-browser-notice',error.message,'danger')}}
document.getElementById('disk-browse').onclick=()=>{diskBrowserModal.classList.add('open');loadDiskBrowser(diskMountpoint.value||'/mnt')};document.getElementById('disk-browser-close').onclick=closeDiskBrowser;document.getElementById('disk-browser-cancel').onclick=closeDiskBrowser;document.getElementById('disk-browser-root').onclick=()=>loadDiskBrowser('/');document.getElementById('disk-browser-up').onclick=()=>{if(diskBrowserParent)loadDiskBrowser(diskBrowserParent)};document.getElementById('disk-browser-select').onclick=()=>{diskMountpoint.value=diskBrowserPath;closeDiskBrowser()};diskBrowserModal.addEventListener('click',event=>{if(event.target===diskBrowserModal)closeDiskBrowser()});
loadDisks();
"""

NETWORK_BODY = r"""
<section class="hero"><div><h1>Network</h1><p>View interface details and configure DHCP or static IPv4 settings.</p></div><span id="network-backend" class="pill">Loading…</span></section>
<div class="notice danger" style="margin-bottom:18px"><strong>Network changes can disconnect this browser and your SSH session.</strong> Verify the address, prefix and gateway before applying a static IP. Static changes automatically roll back after 90 seconds unless the new address is confirmed.</div>
<div id="network-notice" class="notice hidden" style="margin-bottom:18px"></div>
<div class="grid">
  <section class="card span-7"><div class="section-title"><div><h2>Interfaces</h2><div class="metric-detail" id="network-explanation">Reading network configuration…</div></div><button class="button" id="network-refresh" type="button">Refresh</button></div><div id="interface-list" class="interface-list"><div class="empty">Reading interfaces…</div></div></section>
  <section class="card span-5"><div class="section-title"><div><h2>IPv4 settings</h2><div class="metric-detail">Changes use NetworkManager when active, otherwise a ServerDeck-managed Netplan override.</div></div></div>
    <form id="network-form" class="stack">
      <div class="field"><label for="network-interface">Interface</label><select class="select mono" id="network-interface" required><option value="">Choose an interface</option></select></div>
      <div class="field"><label for="network-mode">Address method</label><select class="select" id="network-mode"><option value="dhcp">Automatic (DHCP)</option><option value="static">Static IPv4</option></select></div>
      <div id="network-static-fields" class="stack hidden">
        <div class="field"><label for="network-address">Address and prefix</label><input class="input mono" id="network-address" placeholder="192.168.1.50/24"><div class="help">Use CIDR notation. A /24 prefix is equivalent to 255.255.255.0.</div></div>
        <div class="field"><label for="network-gateway">Gateway</label><input class="input mono" id="network-gateway" placeholder="192.168.1.1"></div>
        <div class="field"><label for="network-dns">DNS servers</label><input class="input mono" id="network-dns" placeholder="1.1.1.1, 8.8.8.8"></div>
      </div>
      <button class="button primary" id="network-apply" type="submit"><span class="spinner"></span>Apply network settings</button>
    </form>
  </section>
</div>
"""

NETWORK_JS = r"""
let networkInterfaces=[];
const networkSelect=document.getElementById('network-interface'),networkMode=document.getElementById('network-mode'),staticFields=document.getElementById('network-static-fields'),networkApply=document.getElementById('network-apply');
function renderInterfaces(){const list=document.getElementById('interface-list');list.textContent='';networkSelect.innerHTML='<option value="">Choose an interface</option>';for(const item of networkInterfaces){if(item.editable){const option=document.createElement('option');option.value=item.name;option.textContent=item.name+(item.connection?' · '+item.connection:'');networkSelect.appendChild(option);}const card=document.createElement('article');card.className='interface-card';card.innerHTML='<div class="interface-card-head"><div><h3 style="margin:0">'+esc(item.name)+'</h3><div class="path">'+esc(item.type||'network interface')+(item.connection?' · '+esc(item.connection):'')+'</div></div><span class="pill '+((item.state==='UP'||item.state==='up'||item.state==='connected')?'good':'')+'">'+esc(item.state)+'</span></div><div class="interface-values"><div>IPv4<strong>'+esc((item.ipv4||[]).join(', ')||'—')+'</strong></div><div>Gateway<strong>'+esc(item.gateway||'—')+'</strong></div><div>DNS<strong>'+esc((item.dns||[]).join(', ')||'—')+'</strong></div><div>Method<strong>'+esc(item.method||'unknown')+'</strong></div><div>MAC address<strong>'+esc(item.mac||'—')+'</strong></div><div>MTU<strong>'+esc(String(item.mtu||'—'))+'</strong></div></div>';list.appendChild(card);}if(!networkInterfaces.length)list.innerHTML='<div class="empty">No non-loopback interfaces were detected.</div>';}
async function loadNetwork(){hideNotice('network-notice');try{const data=await api('/api/network');networkInterfaces=data.interfaces||[];const backend=document.getElementById('network-backend');backend.textContent=data.backend;backend.className='pill '+(data.editable?'good':'warn');document.getElementById('network-explanation').textContent=data.explanation;networkApply.disabled=!data.editable;renderInterfaces();}catch(error){showNotice('network-notice',error.message,'danger')}}
function fillNetworkForm(){const item=networkInterfaces.find(entry=>entry.name===networkSelect.value);if(!item)return;networkMode.value=item.method==='manual'?'static':'dhcp';document.getElementById('network-address').value=(item.ipv4||[])[0]||'';document.getElementById('network-gateway').value=item.gateway||'';document.getElementById('network-dns').value=(item.dns||[]).join(', ');toggleStatic();}
function toggleStatic(){staticFields.classList.toggle('hidden',networkMode.value!=='static')}
networkSelect.onchange=fillNetworkForm;networkMode.onchange=toggleStatic;
document.getElementById('network-form').onsubmit=async event=>{event.preventDefault();if(!networkSelect.value)return;const isStatic=networkMode.value==='static';const warning=isStatic?'Apply this static IPv4 configuration? The browser and SSH may disconnect. The old configuration will be restored unless the new address is confirmed within 90 seconds.':'Switch this interface to DHCP? Its address may change and this browser may disconnect.';if(!confirm(warning))return;setBusy(networkApply,true);try{const result=await api('/api/network/apply',{method:'POST',body:{interface:networkSelect.value,mode:networkMode.value,address:document.getElementById('network-address').value,gateway:document.getElementById('network-gateway').value,dns:document.getElementById('network-dns').value}});showNotice('network-notice',result.message,'warning');if(result.token&&result.target_ip){const port=location.port?':'+location.port:'';const url='http://'+result.target_ip+port+'/network/confirm?token='+encodeURIComponent(result.token);showNotice('network-notice',result.message+' Redirecting to '+url+' …','warning');window.setTimeout(()=>window.location.assign(url),8000);}else{window.setTimeout(()=>window.location.reload(),12000);}}catch(error){showNotice('network-notice',error.message,'danger');setBusy(networkApply,false)}};
document.getElementById('network-refresh').onclick=loadNetwork;toggleStatic();loadNetwork();
"""


COPYPARTY_BODY = r"""
<section class="hero"><div><h1>CopyParty</h1><p>Download CopyParty, add optional thumbnail support, and run it automatically as a managed system service.</p></div><span id="copyparty-state" class="pill">Checking…</span></section>
<div class="notice warning" style="margin-bottom:18px">The CopyParty script is downloaded directly from its official GitHub <span class="mono">latest</span> release URL. The simple service configured below deliberately exposes the selected folder as <strong>anonymous read/write</strong>; anyone who can reach CopyParty can read and upload files there. The service runs as the Linux account used to configure it, not as root.</div>
<div id="copyparty-notice" class="notice hidden" style="margin-bottom:18px"></div>
<div class="grid">
  <article class="card span-7">
    <div class="section-title"><div><h2>CopyParty script</h2><div class="metric-detail">Official self-extracting Python release.</div></div><span id="copyparty-installed-pill" class="pill">Checking…</span></div>
    <div class="stack">
      <div class="split"><div><strong>Install path</strong><div class="metric-detail mono" id="copyparty-path">/opt/copyparty/copyparty-sfx.py</div></div><div style="text-align:right"><strong id="copyparty-size">—</strong><div class="metric-detail" id="copyparty-modified">Not downloaded</div></div></div>
      <div class="field"><label>Official release URL</label><div class="input mono" style="height:auto;min-height:42px;overflow-wrap:anywhere" id="copyparty-url">https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py</div></div>
      <div class="actions"><button class="button primary" id="copyparty-download" type="button"><span class="spinner"></span><span id="copyparty-download-label">Download CopyParty</span></button></div>
    </div>
  </article>
  <article class="card span-5">
    <div class="section-title"><div><h2>Thumbnail support</h2><div class="metric-detail">Optional packages used for image/video thumbnails.</div></div><span id="thumbnail-pill" class="pill">Checking…</span></div>
    <p class="metric-detail">Installs the Debian/Ubuntu packages:</p>
    <pre class="output" style="min-height:76px;max-height:90px">apt install --no-install-recommends python3-pil ffmpeg</pre>
    <div id="thumbnail-packages" class="metric-detail" style="margin:12px 0">Checking package status…</div>
    <button class="button" id="thumbnail-install" type="button"><span class="spinner"></span><span id="thumbnail-install-label">Install thumbnail packages</span></button>
  </article>
  <article class="card span-12">
    <div class="section-title"><div><h2>Install service and run</h2><div class="metric-detail">Creates <span class="mono">copyparty.service</span>, enables it at boot, and starts it immediately.</div></div><span id="copyparty-service-pill" class="pill">Not configured</span></div>
    <div class="form-grid">
      <div class="field full"><label for="copyparty-folder">Shared folder</label><div class="input-with-button"><input class="input mono" id="copyparty-folder" placeholder="/srv/copyparty" autocomplete="off"><button class="button browse-button" id="copyparty-folder-browse" type="button">Browse</button></div><div class="help">The folder must already exist. CopyParty will use it as the web root with read/write access.</div></div>
      <div class="field full"><label>Service command</label><pre class="output" id="copyparty-command" style="min-height:72px;max-height:110px">/opt/copyparty/copyparty-sfx.py -v /path/to/folder::rw -z</pre></div>
    </div>
    <div class="split" style="margin-top:14px;align-items:flex-end;flex-wrap:wrap">
      <div class="metric-detail" id="copyparty-service-detail">Service has not been configured.</div>
      <div class="actions"><a class="button hidden" id="copyparty-open" target="_blank" rel="noopener">Open CopyParty</a><button class="button primary" id="copyparty-configure" type="button"><span class="spinner"></span>Install Service &amp; Start</button><button class="button" id="copyparty-start" type="button">Start</button><button class="button" id="copyparty-restart" type="button">Restart</button><button class="button danger" id="copyparty-stop" type="button">Stop</button><button class="button danger" id="copyparty-delete" type="button">Delete Service</button></div>
    </div>
    <div style="margin-top:16px"><div class="metric-label" style="margin-bottom:7px">Recent service log</div><pre class="output" id="copyparty-service-log" style="min-height:130px;max-height:300px">No service log yet.</pre></div>
  </article>
  <article class="card span-12"><div class="section-title"><h2>Operation output</h2><span id="copyparty-task-state" class="pill">Idle</span></div><pre class="output" id="copyparty-output">No CopyParty download or package operation has been started in this browser session.</pre></article>
</div>
<div class="modal" id="copyparty-browser-modal" role="dialog" aria-modal="true" aria-labelledby="copyparty-browser-title"><div class="modal-box browser-box"><div class="section-title"><div><h2 id="copyparty-browser-title">Choose CopyParty folder</h2><div class="metric-detail">Browse folders on the server.</div></div><button class="button ghost" id="copyparty-browser-close" type="button">Close</button></div><div class="browser-toolbar"><button class="button" id="copyparty-browser-root" type="button">Root</button><button class="button" id="copyparty-browser-up" type="button">Up</button></div><div class="browser-path mono" id="copyparty-browser-path">/</div><div id="copyparty-browser-list" class="browser-list"><div class="browser-status">Loading folders…</div></div><div id="copyparty-browser-notice" class="notice hidden" style="margin-top:12px"></div><div class="actions" style="margin-top:16px"><button class="button primary" id="copyparty-browser-select" type="button">Use this folder</button><button class="button ghost" id="copyparty-browser-cancel" type="button">Cancel</button></div></div></div>
"""

COPYPARTY_JS = r"""
let copypartyStatus=null,copypartyFolderDirty=false,copypartyBrowserPath='/',copypartyBrowserParent=null;
const cpFolder=document.getElementById('copyparty-folder'),cpConfigure=document.getElementById('copyparty-configure'),cpBrowser=document.getElementById('copyparty-browser-modal');
function updateCopyPartyCommand(){const folder=cpFolder.value.trim()||'/path/to/folder';document.getElementById('copyparty-command').textContent='/opt/copyparty/copyparty-sfx.py -v '+JSON.stringify(folder+'::rw')+' -z';}
function copyPartyUrl(port){let host=location.hostname;if(host.includes(':')&&!host.startsWith('['))host='['+host+']';return location.protocol+'//'+host+':'+port+'/';}
async function loadCopyParty(){
  try{
    const data=await api('/api/copyparty');copypartyStatus=data;
    const state=document.getElementById('copyparty-state');state.textContent=data.is_root?'Ready':'Read only';state.className='pill '+(data.is_root?'good':'warn');
    const installed=document.getElementById('copyparty-installed-pill');installed.textContent=data.installed?'Downloaded':'Not downloaded';installed.className='pill '+(data.installed?'good':'warn');
    document.getElementById('copyparty-path').textContent=data.path||'/opt/copyparty/copyparty-sfx.py';
    document.getElementById('copyparty-url').textContent=data.download_url||'';
    document.getElementById('copyparty-size').textContent=data.installed?(data.size_human||'—'):'—';
    document.getElementById('copyparty-modified').textContent=data.installed&&data.modified?'Updated '+new Date(data.modified).toLocaleString():'Not downloaded';
    document.getElementById('copyparty-download-label').textContent=data.installed?'Download latest / update':'Download CopyParty';
    const packages=data.thumbnail_packages||{};const packageParts=Object.keys(packages).map(name=>name+': '+(packages[name]?'installed':'missing'));
    document.getElementById('thumbnail-packages').textContent=packageParts.join(' · ')||'Package status unavailable';
    const thumb=document.getElementById('thumbnail-pill');thumb.textContent=data.thumbnails_ready?'Installed':'Optional';thumb.className='pill '+(data.thumbnails_ready?'good':'warn');
    const thumbButton=document.getElementById('thumbnail-install');thumbButton.disabled=!!data.thumbnails_ready;document.getElementById('thumbnail-install-label').textContent=data.thumbnails_ready?'Thumbnail packages installed':'Install thumbnail packages';
    const svc=data.service||{};const svcPill=document.getElementById('copyparty-service-pill');
    if(svc.active){svcPill.textContent='Running';svcPill.className='pill good'}else if(svc.configured){svcPill.textContent='Stopped';svcPill.className='pill warn'}else if(svc.service_exists&&!svc.service_managed){svcPill.textContent='External service';svcPill.className='pill bad'}else{svcPill.textContent='Not configured';svcPill.className='pill'}
    if(!copypartyFolderDirty&&svc.folder){cpFolder.value=svc.folder;updateCopyPartyCommand();}
    const detail=[];if(svc.user)detail.push('Runs as '+svc.user);if(svc.folder)detail.push('folder '+svc.folder);detail.push(svc.enabled?'starts at boot':'not enabled at boot');if(svc.pid)detail.push('PID '+svc.pid);document.getElementById('copyparty-service-detail').textContent=detail.join(' · ');
    document.getElementById('copyparty-service-log').textContent=svc.logs||'No journal entries are available yet.';
    cpConfigure.disabled=!data.installed||!data.is_root||!!(svc.service_exists&&!svc.service_managed);
    document.getElementById('copyparty-start').disabled=!svc.configured||svc.active;document.getElementById('copyparty-restart').disabled=!svc.configured;document.getElementById('copyparty-stop').disabled=!svc.configured||!svc.active;document.getElementById('copyparty-delete').disabled=!svc.service_managed;
    const open=document.getElementById('copyparty-open');if(svc.active){open.href=copyPartyUrl(svc.port||3923);open.classList.remove('hidden')}else open.classList.add('hidden');
    if(svc.service_exists&&!svc.service_managed)showNotice('copyparty-notice','A copyparty.service already exists but is not managed by ServerDeck, so ServerDeck will not overwrite it.','warning');else hideNotice('copyparty-notice');
  }catch(error){showNotice('copyparty-notice',error.message,'danger')}
}
function watchCopyParty(task,button){const state=document.getElementById('copyparty-task-state');state.textContent='Running';state.className='pill warn';setBusy(button,true);pollTask(task.id,'copyparty-output',async finished=>{setBusy(button,false);state.textContent=finished.returncode===0?'Completed':'Failed';state.className='pill '+(finished.returncode===0?'good':'bad');await loadCopyParty();});}
document.getElementById('copyparty-download').onclick=async function(){const updating=copypartyStatus&&copypartyStatus.installed;if(!confirm(updating?'Download the latest official CopyParty script and replace the installed copy? The previous file will be kept as copyparty-sfx.py.previous.':'Download the latest official CopyParty script to /opt/copyparty/copyparty-sfx.py?'))return;try{watchCopyParty(await api('/api/copyparty/download',{method:'POST'}),this)}catch(error){showNotice('copyparty-notice',error.message,'danger')}};
document.getElementById('thumbnail-install').onclick=async function(){if(!confirm('Install python3-pil and ffmpeg without recommended extra packages?'))return;try{watchCopyParty(await api('/api/copyparty/thumbnails',{method:'POST'}),this)}catch(error){showNotice('copyparty-notice',error.message,'danger')}};
cpFolder.addEventListener('input',()=>{copypartyFolderDirty=true;updateCopyPartyCommand()});
cpConfigure.onclick=async function(){const folder=cpFolder.value.trim();if(!folder){showNotice('copyparty-notice','Choose a folder first.','danger');return}if(!confirm('Install the CopyParty system service using '+folder+', start it now, and enable it at boot? Anyone who can reach CopyParty will be able to read and upload files in this folder.'))return;setBusy(this,true);try{const result=await api('/api/copyparty/service/configure',{method:'POST',body:{folder:folder}});copypartyFolderDirty=false;showNotice('copyparty-notice',result.message,'good');await loadCopyParty()}catch(error){showNotice('copyparty-notice',error.message,'danger')}finally{setBusy(this,false)}};
async function copyPartyServiceAction(action){if((action==='stop'||action==='restart')&&!confirm((action==='stop'?'Stop':'Restart')+' the CopyParty service now?'))return;try{const result=await api('/api/copyparty/service/'+action,{method:'POST'});showNotice('copyparty-notice',result.message,'good');await loadCopyParty()}catch(error){showNotice('copyparty-notice',error.message,'danger')}}
document.getElementById('copyparty-start').onclick=()=>copyPartyServiceAction('start');document.getElementById('copyparty-stop').onclick=()=>copyPartyServiceAction('stop');document.getElementById('copyparty-restart').onclick=()=>copyPartyServiceAction('restart');
document.getElementById('copyparty-delete').onclick=async function(){if(!confirm('Delete the ServerDeck-managed CopyParty service? This stops CopyParty and removes it from system startup. The downloaded script and shared files will NOT be deleted.'))return;setBusy(this,true);try{const result=await api('/api/copyparty/service/delete',{method:'POST'});copypartyFolderDirty=false;cpFolder.value='';updateCopyPartyCommand();showNotice('copyparty-notice',result.message,'good');await loadCopyParty()}catch(error){showNotice('copyparty-notice',error.message,'danger')}finally{setBusy(this,false)}};
async function loadCopyPartyBrowser(path){document.getElementById('copyparty-browser-list').innerHTML='<div class="browser-status">Loading folders…</div>';hideNotice('copyparty-browser-notice');try{const data=await api('/api/filesystem?path='+encodeURIComponent(path));copypartyBrowserPath=data.path;copypartyBrowserParent=data.parent;document.getElementById('copyparty-browser-path').textContent=data.path;document.getElementById('copyparty-browser-up').disabled=!data.parent;const list=document.getElementById('copyparty-browser-list');list.textContent='';if(!data.entries.length){list.innerHTML='<div class="browser-status">No subfolders are visible here.</div>';return}for(const entry of data.entries){const button=document.createElement('button');button.type='button';button.className='browser-entry';button.innerHTML='<span class="browser-entry-icon">▸</span><span class="browser-entry-name mono">'+esc(entry.name)+'</span>';button.onclick=()=>loadCopyPartyBrowser(entry.path);list.appendChild(button)}}catch(error){showNotice('copyparty-browser-notice',error.message,'danger')}}
document.getElementById('copyparty-folder-browse').onclick=()=>{cpBrowser.classList.add('open');loadCopyPartyBrowser(cpFolder.value.trim()||'/')};document.getElementById('copyparty-browser-root').onclick=()=>loadCopyPartyBrowser('/');document.getElementById('copyparty-browser-up').onclick=()=>copypartyBrowserParent&&loadCopyPartyBrowser(copypartyBrowserParent);document.getElementById('copyparty-browser-select').onclick=()=>{cpFolder.value=copypartyBrowserPath;copypartyFolderDirty=true;updateCopyPartyCommand();cpBrowser.classList.remove('open')};document.getElementById('copyparty-browser-close').onclick=document.getElementById('copyparty-browser-cancel').onclick=()=>cpBrowser.classList.remove('open');cpBrowser.addEventListener('click',event=>{if(event.target===cpBrowser)cpBrowser.classList.remove('open')});
updateCopyPartyCommand();loadCopyParty();
"""


TERMINAL_BODY = r"""
<section class="hero"><div><h1>Terminal</h1><p>Run commands in a persistent shell as the Linux account used to sign in.</p></div><span id="terminal-state" class="pill">Connecting…</span></section>
<div class="notice warning" style="margin-bottom:18px">Terminal access is equivalent to an SSH session for this account. Use ServerDeck only over a trusted LAN, VPN, or HTTPS connection.</div>
<div id="terminal-notice" class="notice hidden" style="margin-bottom:18px"></div>
<article class="card terminal-card">
  <div class="terminal-toolbar">
    <div><strong id="terminal-identity">Starting shell…</strong><div class="help" id="terminal-detail">Commands and output remain in memory only.</div></div>
    <div class="terminal-toolbar-group"><button class="button" id="terminal-interrupt" type="button">Interrupt</button><button class="button" id="terminal-clear" type="button">Clear</button><button class="button" id="terminal-restart" type="button">Restart shell</button><button class="button danger" id="terminal-stop" type="button">End shell</button></div>
  </div>
  <pre class="terminal-output" id="terminal-output" tabindex="0" aria-live="polite">Starting terminal…\n</pre>
  <form class="terminal-command" id="terminal-form"><span class="terminal-prompt" aria-hidden="true">$</span><input class="terminal-input" id="terminal-input" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Enter a command" aria-label="Terminal command"><button class="button primary" id="terminal-run" type="submit">Run</button></form>
</article>
<div class="help" style="margin-top:12px">The shell keeps its current directory between commands. Password prompts automatically mask the input field. Use <strong>Interrupt</strong> for a running command. Full-screen terminal programs such as <span class="mono">nano</span>, <span class="mono">vim</span>, and <span class="mono">top</span> are not supported by this plain-text console.</div>
"""

TERMINAL_JS = r"""
const terminalOutput=document.getElementById('terminal-output');
const terminalInput=document.getElementById('terminal-input');
const terminalState=document.getElementById('terminal-state');
const terminalIdentity=document.getElementById('terminal-identity');
const terminalDetail=document.getElementById('terminal-detail');
let terminalOffset=0;
let terminalPoll=null;
let terminalActive=false;
let commandHistory=[];
let historyIndex=0;
function setTerminalState(active){terminalActive=active;terminalState.textContent=active?'Shell running':'Shell stopped';terminalState.className='pill '+(active?'good':'bad');document.getElementById('terminal-run').disabled=!active;document.getElementById('terminal-interrupt').disabled=!active;document.getElementById('terminal-stop').disabled=!active;terminalInput.disabled=!active;}
function applyTerminal(data){const nearBottom=terminalOutput.scrollHeight-terminalOutput.scrollTop-terminalOutput.clientHeight<70;if(data.reset)terminalOutput.textContent=data.output||'';else if(data.output)terminalOutput.textContent+=data.output;terminalOffset=Number(data.offset||0);setTerminalState(!!data.active);if(data.username){terminalIdentity.textContent=data.username+' on '+location.hostname;terminalDetail.textContent=(data.shell||'shell')+' · home '+(data.home||'—')+' · output is not saved by ServerDeck';}const tail=terminalOutput.textContent.slice(-240);const passwordPrompt=/password[^\n]*:\s*$/i.test(tail);terminalInput.type=passwordPrompt?'password':'text';terminalInput.placeholder=passwordPrompt?'Enter password':'Enter a command';if(nearBottom)terminalOutput.scrollTop=terminalOutput.scrollHeight;}
async function startTerminal(restart=false){hideNotice('terminal-notice');try{const data=await api('/api/terminal/start',{method:'POST',body:{restart:restart}});terminalOffset=0;terminalOutput.textContent='';applyTerminal(data);terminalInput.focus();if(!terminalPoll)terminalPoll=setInterval(pollTerminal,700);}catch(error){setTerminalState(false);showNotice('terminal-notice',error.message,'danger');terminalOutput.textContent='Unable to start terminal.\n';}}
async function pollTerminal(){try{const data=await api('/api/terminal?offset='+encodeURIComponent(terminalOffset));applyTerminal(data);}catch(error){if(terminalPoll){clearInterval(terminalPoll);terminalPoll=null;}showNotice('terminal-notice',error.message,'danger');}}
document.getElementById('terminal-form').onsubmit=async event=>{event.preventDefault();const command=terminalInput.value;const masked=terminalInput.type==='password';if(!terminalActive)return;if(command.trim()&&!masked){commandHistory.push(command);if(commandHistory.length>100)commandHistory.shift();historyIndex=commandHistory.length;}terminalInput.value='';terminalInput.type='text';terminalInput.placeholder='Enter a command';try{await api('/api/terminal/input',{method:'POST',body:{command:command}});terminalInput.focus();window.setTimeout(pollTerminal,80);}catch(error){showNotice('terminal-notice',error.message,'danger');}};
terminalInput.addEventListener('keydown',event=>{if(event.key==='ArrowUp'){event.preventDefault();if(commandHistory.length){historyIndex=Math.max(0,historyIndex-1);terminalInput.value=commandHistory[historyIndex]||'';terminalInput.setSelectionRange(terminalInput.value.length,terminalInput.value.length);}}else if(event.key==='ArrowDown'){event.preventDefault();if(commandHistory.length){historyIndex=Math.min(commandHistory.length,historyIndex+1);terminalInput.value=historyIndex<commandHistory.length?commandHistory[historyIndex]:'';terminalInput.setSelectionRange(terminalInput.value.length,terminalInput.value.length);}}});
document.getElementById('terminal-interrupt').onclick=async()=>{try{await api('/api/terminal/interrupt',{method:'POST'});window.setTimeout(pollTerminal,80);terminalInput.focus();}catch(error){showNotice('terminal-notice',error.message,'danger')}};
document.getElementById('terminal-clear').onclick=async()=>{try{const data=await api('/api/terminal/clear',{method:'POST'});terminalOutput.textContent='';terminalOffset=Number(data.offset||0);terminalInput.focus();}catch(error){showNotice('terminal-notice',error.message,'danger')}};
document.getElementById('terminal-restart').onclick=()=>{if(confirm('Restart this web terminal shell? Any command currently running in it will be stopped.'))startTerminal(true)};
document.getElementById('terminal-stop').onclick=async()=>{if(!confirm('End this web terminal shell?'))return;try{await api('/api/terminal/stop',{method:'POST'});setTerminalState(false);terminalOutput.textContent+='\n[Terminal session ended]\n';terminalOffset=0;}catch(error){showNotice('terminal-notice',error.message,'danger')}};
window.addEventListener('beforeunload',()=>{if(terminalPoll)clearInterval(terminalPoll)});
startTerminal(false);
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
        self.tasks = TaskRegistry()
        self.updates = UpdateManager(self.tasks)
        self.scheduler = SchedulerManager(config, self.jobs)
        self.sessions = SessionStore(config.session_ttl)
        self.terminals = TerminalManager()
        self.disks = DiskManager()
        self.network = NetworkSettingsManager(config.port)
        self.copyparty = CopyPartyManager(self.tasks, config)
        self.no_auth_session = {"username": "local", "csrf": secrets.token_urlsafe(32)}
        self.pam = PAMAuthenticator() if config.auth_mode == "pam" else None
        self.startup_warnings = self.scheduler.sync()
        self.startup_warnings.extend(remove_deprecated_autostart_units(config))

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

    def terminal_key(self) -> str:
        username = getattr(self, "current_session", {}).get("username", "")
        return self.current_token or f"no-auth:{username}"

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
        if path == "/network/confirm":
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            token = query.get("token", [""])[0]
            confirmed = self.app.network.confirm(token)
            if confirmed:
                self.send_html("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Network confirmed · ServerDeck</title><style>" + CSS + "</style></head><body><div class=\"login-shell\"><main class=\"login-card\"><div class=\"login-brand\"><span class=\"brand-mark\">SD</span><strong>ServerDeck</strong></div><h1>Network settings confirmed</h1><p>The new static address will be kept. Log in again to continue.</p><a class=\"button primary\" href=\"/login\">Continue to login</a></main></div></body></html>")
            else:
                self.send_html("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Confirmation expired · ServerDeck</title><style>" + CSS + "</style></head><body><div class=\"login-shell\"><main class=\"login-card\"><h1>Confirmation unavailable</h1><p>The token is invalid or expired. ServerDeck may restore the previous network settings.</p></main></div></body></html>", 400)
            return
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
            elif path == "/disks":
                self.send_html(page("Disks", "disks", DISKS_BODY, DISKS_JS, csrf, username))
            elif path == "/network":
                self.send_html(page("Network", "network", NETWORK_BODY, NETWORK_JS, csrf, username))
            elif path == "/copyparty":
                self.send_html(page("CopyParty", "copyparty", COPYPARTY_BODY, COPYPARTY_JS, csrf, username))
            elif path == "/terminal":
                self.send_html(page("Terminal", "terminal", TERMINAL_BODY, TERMINAL_JS, csrf, username))
            elif path in {"/autostart", "/timers"}:
                self.redirect("/")
            elif path == "/api/stats":
                self.send_json(self.app.stats.snapshot())
            elif path == "/api/updates":
                self.send_json(self.app.updates.available())
            elif path == "/api/disks":
                self.send_json(self.app.disks.inventory())
            elif path == "/api/network":
                self.send_json(self.app.network.inventory())
            elif path == "/api/copyparty":
                self.send_json(self.app.copyparty.snapshot())
            elif path == "/api/backups":
                self.send_json(
                    {
                        "jobs": self.app.jobs.list(),
                        "rsync_available": command_exists("rsync"),
                        "capabilities": self.app.scheduler.capability(),
                        "warnings": self.app.startup_warnings,
                    }
                )
            elif path == "/api/terminal":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                try:
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                except ValueError:
                    raise ValueError("Invalid terminal output offset")
                self.send_json(self.app.terminals.snapshot(self.terminal_key(), offset))
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
                    self.app.terminals.stop(self.terminal_key())
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
            if path == "/api/terminal/start":
                payload = self.read_json()
                restart = bool(payload.get("restart", False))
                self.send_json(self.app.terminals.start(self.terminal_key(), self.current_session["username"], restart))
            elif path == "/api/terminal/input":
                payload = self.read_json()
                session = self.app.terminals.get(self.terminal_key())
                if not session:
                    raise RuntimeError("Start the terminal shell first")
                session.write(payload.get("command", ""))
                self.send_json({"accepted": True})
            elif path == "/api/terminal/interrupt":
                session = self.app.terminals.get(self.terminal_key())
                if not session:
                    raise RuntimeError("The terminal shell is not running")
                session.interrupt()
                self.send_json({"accepted": True})
            elif path == "/api/terminal/clear":
                session = self.app.terminals.get(self.terminal_key())
                if not session:
                    raise RuntimeError("The terminal shell is not running")
                self.send_json(session.clear())
            elif path == "/api/terminal/stop":
                self.app.terminals.stop(self.terminal_key())
                self.send_json({"stopped": True})
            elif path == "/api/hostname":
                payload = self.read_json()
                hostname = change_hostname(payload.get("hostname", ""))
                self.send_json({"hostname": hostname})
            elif path == "/api/power":
                payload = self.read_json()
                action = safe_text(payload.get("action", ""), 16).lower()
                message = schedule_power_action(action, payload.get("confirmation", ""))
                self.send_json({"accepted": True, "action": action, "message": message}, 202)
            elif path == "/api/disks/mount":
                self.send_json(self.app.disks.mount(self.read_json()))
            elif path == "/api/disks/unmount":
                self.send_json(self.app.disks.unmount(self.read_json()))
            elif path == "/api/disks/persistence/remove":
                self.send_json(self.app.disks.remove_persistence(self.read_json()))
            elif path == "/api/network/apply":
                self.send_json(self.app.network.apply(self.read_json()), 202)
            elif path == "/api/copyparty/download":
                self.send_json(self.app.copyparty.download(), 202)
            elif path == "/api/copyparty/thumbnails":
                self.send_json(self.app.copyparty.install_thumbnail_packages(), 202)
            elif path == "/api/copyparty/service/configure":
                self.send_json(self.app.copyparty.configure_and_start(self.read_json(), self.current_session["username"]))
            elif path == "/api/copyparty/service/start":
                self.send_json(self.app.copyparty.service_action("start"))
            elif path == "/api/copyparty/service/stop":
                self.send_json(self.app.copyparty.service_action("stop"))
            elif path == "/api/copyparty/service/restart":
                self.send_json(self.app.copyparty.service_action("restart"))
            elif path == "/api/copyparty/service/delete":
                self.send_json(self.app.copyparty.delete_service())
            elif path == "/api/updates/refresh":
                self.send_json(self.app.updates.refresh(), 202)
            elif path == "/api/updates/install":
                self.send_json(self.app.updates.install(), 202)
            elif path == "/api/updates/full-upgrade":
                self.send_json(self.app.updates.full_upgrade(), 202)
            elif path == "/api/updates/security":
                self.send_json(self.app.updates.install_security(), 202)
            elif path == "/api/updates/cleanup":
                self.send_json(self.app.updates.cleanup(), 202)
            elif path == "/api/rsync/install":
                self.send_json(self.app.updates.install_rsync(), 202)
            elif path == "/api/backups":
                job = self.app.jobs.save(self.read_json())
                warnings = self.app.scheduler.sync()
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
        if not backup_match:
            self.error_json(404, "Not found")
            return
        try:
            job_id = backup_match.group(1)
            if not self.app.jobs.get(job_id):
                self.error_json(404, "Backup job not found")
                return
            job = self.app.jobs.save(self.read_json(), job_id=job_id)
            warnings = self.app.scheduler.sync()
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
        if not backup_match:
            self.error_json(404, "Not found")
            return
        try:
            job_id = backup_match.group(1)
            if not self.app.jobs.delete(job_id):
                self.error_json(404, "Backup job not found")
                return
            warnings = self.app.scheduler.sync()
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
    print("ServerDeck service and managed schedule units removed. Backup data and legacy Autostart data were left in place.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-file Debian/Ubuntu server management web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Address to listen on (default: 0.0.0.0)")
    parser.add_argument(
        "-port", "--port",
        dest="port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"TCP port for interactive use or service installation (default: {DEFAULT_PORT})",
    )
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
    parser.add_argument("--install-service", action="store_true", help="Install and start a systemd service")
    parser.add_argument("--uninstall-service", action="store_true", help="Remove the systemd service and installed script")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("-port/--port must be between 1 and 65535")
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
        print("Running without root: hostname changes, package operations, disk/network changes, power actions, system schedules, and terminal user switching will be unavailable.")
    print("Press Ctrl+C to stop.")

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.terminals.close_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
