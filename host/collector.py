#!/usr/bin/env python3
"""Usage collector for the TURZX 3.5" screen.

Polls Claude (subscription usage endpoint) and Codex quota plus this Mac's
CPU / GPU / RAM / disk, and writes the latest payload every 3 s to
~/Library/Application Support/QuotaFloat/snapshot.json (mode 600), which
screen/turing_screen draws. Read-only: it never refreshes or stores tokens.
AntiGravity quota is added when antigravity_quota.py sits next to this file.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    import psutil               # optional; system gauges show --% if absent
except ImportError:
    psutil = None

try:
    from antigravity_quota import poll_antigravity   # not "antigravity": that is a stdlib easter egg that opens a browser
except ImportError:             # public build: the AntiGravity cell stays --%
    async def poll_antigravity():
        return None

CODEX_POLL_INTERVAL = 30
CODEX_STALE = 90
POLL_INTERVAL = 90          # seconds between Claude usage polls
SNAPSHOT = Path(os.environ.get("TURZX_SNAPSHOT") or
                Path.home() / "Library/Application Support/QuotaFloat/snapshot.json")
SNAPSHOT_INTERVAL = 3

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


# Subscription usage endpoint (the same one Claude Code's /usage reads). A plain
# GET — consumes no model quota — that returns every rate-limit window at once,
# INCLUDING per-model "weekly_scoped" limits (e.g. Fable 5) that the /v1/messages
# response headers do NOT expose. The `User-Agent: claude-code/*` header is
# REQUIRED — without it this endpoint 429-throttles aggressively.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}

# ChatGPT Codex usage (the 5th row on the display). The Codex desktop app / CLI
# keeps its OAuth token in ~/.codex/auth.json and refreshes it itself (10-day
# JWT), so we only read it. The usage endpoint is the one Codex's own /status
# reads — a plain GET that consumes no quota.
# ponytail: no token refresh here; if the app is logged out the row shows "--".
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

ANTIGRAVITY_STALE = 180

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()




def _mins_until(iso_ts) -> int:
    """Minutes from now until an ISO-8601 `resets_at` (e.g. '2026-07-12T10:00:00+00:00')."""
    if not iso_ts:
        return -1
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return -1
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    return int(round(mins)) if mins > 0 else 0


async def poll_codex() -> dict | None:
    """Return the explicit 7-day Codex window, never a shorter substitute."""
    try:
        tok = json.loads(CODEX_AUTH_PATH.read_text())["tokens"]
        headers = {
            "Authorization": f"Bearer {tok['access_token']}",
            "chatgpt-account-id": tok["account_id"],
            "User-Agent": "codex_cli_rs/0.60.0",
        }
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(CODEX_USAGE_URL, headers=headers)
        if resp.status_code >= 400:
            log(f"Codex usage HTTP {resp.status_code}")
            return None
        js = resp.json()
        rl = js.get("rate_limit") or {}
        windows = (rl.get("primary_window"), rl.get("secondary_window"))
        weekly = next((w for w in windows if isinstance(w, dict)
                       and w.get("limit_window_seconds") == 604800), None)
        if weekly is None:
            log("Codex usage has no weekly window")
            return None
        pct = weekly.get("used_percent")
        if (isinstance(pct, bool) or not isinstance(pct, (int, float))
                or not math.isfinite(pct) or not 0 <= pct <= 100):
            log("Codex weekly usage unavailable or invalid")
            return None
        secs = weekly.get("reset_after_seconds")
        if secs is None:
            reset_at = weekly.get("reset_at")
            if (not isinstance(reset_at, bool) and isinstance(reset_at, (int, float))
                    and math.isfinite(reset_at) and reset_at > 0):
                secs = reset_at - time.time()
        reset_mins = -1
        if (not isinstance(secs, bool) and isinstance(secs, (int, float))
                and math.isfinite(secs)):
            reset_mins = max(0, int(round(secs / 60)))

        result = {"c": pct, "cr": reset_mins}
        # Same authenticated response/account as the weekly window. Never use
        # rounded display percent to infer exhaustion (99.9 is still quota).
        if pct >= 100:
            credits = js.get("credits") or {}
            amount = credits.get("balance") if isinstance(credits, dict) else None
            try:
                balance = float(amount) if isinstance(amount, (int, float, str)) and not isinstance(amount, bool) else -1
                if not math.isfinite(balance) or balance < 0:
                    balance = -1
            except (ValueError, OverflowError):
                balance = -1
            result.update(cm=1, cc=balance, cs=0)
        return result
    except (OSError, KeyError, AttributeError, TypeError, ValueError, httpx.HTTPError) as e:
        log(f"Codex usage failed ({type(e).__name__})")
        return None


async def poll_claude(token: str | None) -> dict | None:
    if not token:
        log("Claude credentials unavailable")
        return None
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as e:
        log(f"Usage API failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        log(f"Usage API HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log("Usage API returned non-JSON")
        return None

    # Prefer the structured `limits[]` array — it carries the per-model
    # "weekly_scoped" entry (Fable 5) alongside session (5h) and weekly_all (7d).
    session_pct = weekly_pct = fable_pct = 0
    session_reset = weekly_reset = fable_reset = -1
    session_sev = "normal"

    for lim in (data.get("limits") or []):
        kind = lim.get("kind")
        pct = int(round(lim.get("percent") or 0))
        rst = _mins_until(lim.get("resets_at"))
        if kind == "session":
            session_pct, session_reset = pct, rst
            session_sev = lim.get("severity") or "normal"
        elif kind == "weekly_all":
            weekly_pct, weekly_reset = pct, rst
        elif kind == "weekly_scoped":
            model = (lim.get("scope") or {}).get("model") or {}
            if "fable" in (model.get("display_name") or "").lower():
                fable_pct, fable_reset = pct, rst

    # Fallback to the flat objects if `limits[]` is absent (older schema).
    if not data.get("limits"):
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}
        session_pct = int(round(fh.get("utilization") or 0))
        session_reset = _mins_until(fh.get("resets_at"))
        weekly_pct = int(round(sd.get("utilization") or 0))
        weekly_reset = _mins_until(sd.get("resets_at"))

    payload = {
        "s": session_pct,
        "sr": session_reset,
        "w": weekly_pct,
        "wr": weekly_reset,
        "f": fable_pct,          # Fable 5 weekly-scoped utilization (0 if no such limit)
        "fr": fable_reset,       # minutes until the Fable limit resets (-1 if none)
        "st": "allowed" if session_sev == "normal" else "limited",
        "ok": True,
    }
    return payload


def unavailable_payload() -> dict:
    return {"s": -1, "sr": -1, "w": -1, "wr": -1, "f": -1, "fr": -1,
            "st": "unavailable", "ok": False, "a": -1, "ar": -1, "ab": -1}


async def poll_api(token: str | None) -> dict:
    """Poll each provider independently; a failed provider has no cached success."""
    results = await asyncio.gather(poll_claude(token), poll_codex(), poll_antigravity(),
                                   return_exceptions=True)
    payload = unavailable_payload()
    for name, result in zip(("Claude", "Codex", "AntiGravity"), results):
        if isinstance(result, Exception):
            log(f"{name} usage failed ({type(result).__name__})")
        elif result is not None:
            payload.update(result)
    return payload


# --- Mac system load (CPU / GPU / RAM / disk) -------------------------------
# Same sources as the 35inchENG Turing monitor, none of which need sudo:
#   CPU  psutil.cpu_percent since the previous sample, across all cores
#   GPU  ioreg IOAccelerator "Device Utilization %"
#   RAM  (total - available) / total — the basis of psutil's own percent, so the
#        "used / total GB" caption always agrees with the ring
#   Disk the Data volume ("/" is only the read-only system snapshot) and the two
#        external drives; percent is used / (used + free), the same basis as the
#        "GB free" caption
# A reading that can't be taken is -1 (the display shows --%), never 0, and a
# sample older than SYSTEM_STALE is dropped rather than replayed.
SYSTEM_INTERVAL = 3          # seconds between system-load samples
SYSTEM_STALE = 15            # drop a sample this old (sampler thread stuck)
DATA_VOLUME = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
# Percent key, free-GB key, mount point. An unplugged drive reads -1.
EXTERNAL_VOLUMES = ()      # the screen shows the Mac drive only
EXTERNAL_INTERVAL = 30       # seconds between external drive reads
# cpu/gpu/ram/dsk: percent · ru/rt: RAM used/total GB · df: disk free GB ·
# nc/ng: CPU logical cores / GPU cores · sp/sf, hp/hf: external drives
SYSTEM_KEYS = ("cpu", "gpu", "ram", "dsk", "ru", "rt", "df", "nc", "ng",
               "sp", "sf", "hp", "hf")


def unavailable_system() -> dict:
    return {key: -1 for key in SYSTEM_KEYS}


def _pct(value) -> int:
    """Round a finite 0-100 reading to an int percent; anything else is -1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1
    if not math.isfinite(value) or not 0 <= value <= 100:
        return -1
    return int(round(value))


# Absolute path: launchd's PATH (see the plist) has no /usr/sbin, so a bare
# "ioreg" is not found there and the GPU gauge would sit at --%.
IOREG_BIN = "/usr/sbin/ioreg"


def _read_ioreg_gpu() -> str:
    return subprocess.run([IOREG_BIN, "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                          capture_output=True, text=True, timeout=2).stdout


def parse_gpu(ioreg_out: str) -> tuple[int, int]:
    """(utilization %, core count) from ioreg output; -1 for whatever is missing."""
    util = re.search(r'"Device Utilization %"\s*=\s*(\d+)', ioreg_out or "")
    cores = re.search(r'"gpu-core-count"\s*=\s*(\d+)', ioreg_out or "")
    return (_pct(int(util.group(1))) if util else -1,
            int(cores.group(1)) if cores else -1)


def read_external_volumes() -> dict:
    """Percent and free GB per drive. A mount point that isn't mounted is -1: the
    empty folder left behind would otherwise report the Mac's own disk."""
    stats = {}
    for pct_key, free_key, mount in EXTERNAL_VOLUMES:
        stats[pct_key] = stats[free_key] = -1
        try:
            if psutil is not None and os.path.ismount(mount):
                du = psutil.disk_usage(mount)
                if du.used + du.free > 0:
                    stats[pct_key] = _pct(du.used / (du.used + du.free) * 100)
                    stats[free_key] = int(du.free // 10**9)
        except OSError:
            pass
    return stats


class SystemSampler:
    """Samples this Mac's load on its own short cadence, independent of API polls."""

    def __init__(self) -> None:
        self._stats = unavailable_system()
        self._sampled_at = None
        self._warned: set[str] = set()   # log each failing source once, not every 3s
        self._external = {key: -1 for row in EXTERNAL_VOLUMES for key in row[:2]}
        self._external_thread = None
        self._external_at = None
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)   # prime: next call measures from now
            except Exception:
                pass

    def _warn(self, source: str, err: Exception) -> None:
        if source not in self._warned:
            self._warned.add(source)
            log(f"System {source} unavailable ({type(err).__name__}: {err})")

    def _refresh_external(self) -> None:
        self._external = read_external_volumes()

    def _kick_external(self) -> None:
        # Own thread: a dying USB drive can hang statfs, and that must not freeze
        # the CPU/GPU/RAM readings along with it.
        now = time.monotonic()
        if self._external_at is not None and now - self._external_at < EXTERNAL_INTERVAL:
            return
        if self._external_thread is not None and self._external_thread.is_alive():
            self._external = {key: -1 for key in self._external}   # hung: can't vouch for the numbers
            return
        self._external_at = now
        self._external_thread = threading.Thread(target=self._refresh_external, daemon=True)
        self._external_thread.start()

    def sample_once(self) -> dict:
        stats = unavailable_system()
        stats.update(self._external)
        self._kick_external()
        if psutil is None:
            self._warn("psutil", ImportError("psutil not installed"))
        else:
            try:
                stats["cpu"] = _pct(psutil.cpu_percent(interval=None))
                stats["nc"] = psutil.cpu_count(logical=True) or -1
            except Exception as e:
                self._warn("CPU", e)
            try:
                vm = psutil.virtual_memory()
                if vm.total > 0:
                    used = vm.total - vm.available
                    stats["ram"] = _pct(used / vm.total * 100)
                    if stats["ram"] >= 0:
                        stats["ru"] = round(used / 2**30, 1)
                        stats["rt"] = int(round(vm.total / 2**30))
            except Exception as e:
                self._warn("RAM", e)
            try:
                du = psutil.disk_usage(DATA_VOLUME)
                if du.used + du.free > 0:
                    stats["dsk"] = _pct(du.used / (du.used + du.free) * 100)
                    if stats["dsk"] >= 0:
                        stats["df"] = int(du.free // 10**9)
            except Exception as e:
                self._warn("disk", e)
        try:
            stats["gpu"], stats["ng"] = parse_gpu(_read_ioreg_gpu())
        except Exception as e:
            self._warn("GPU", e)
        return stats

    def latest(self) -> dict:
        if self._sampled_at is None or time.monotonic() - self._sampled_at > SYSTEM_STALE:
            return unavailable_system()
        return dict(self._stats)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self._stats = await asyncio.to_thread(self.sample_once)
            self._sampled_at = time.monotonic()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=SYSTEM_INTERVAL)
            except asyncio.TimeoutError:
                pass



class UsageCollector:
    """One API polling schedule shared by BLE and USB, including failed polls.

    With a SystemSampler attached, every snapshot also carries the latest
    machine load.
    """

    def __init__(self, system: SystemSampler | None = None) -> None:
        self._payload = unavailable_payload()
        self._sampled_at = time.monotonic()
        self._antigravity_reset_at = None
        self._antigravity_sampled_at = self._sampled_at
        self._codex_sampled_at = self._sampled_at
        self.revision = 0
        self.system = system

    async def poll_once(self) -> None:
        token = await asyncio.to_thread(read_token)
        self._payload = await poll_api(token)
        self._antigravity_reset_at = self._payload.pop("_a_reset_at", None)
        self._sampled_at = time.monotonic()
        self._codex_sampled_at = self._sampled_at
        self._antigravity_sampled_at = self._payload.pop("_a_sampled_at", self._sampled_at)
        self.revision += 1
        log("Usage sample: " + json.dumps(self._payload, separators=(",", ":")))

    async def poll_codex_once(self) -> None:
        result = await poll_codex()
        for key in ("c", "cr", "cm", "cc", "cs"):
            self._payload.pop(key, None)
        self._payload.update(result or {"c": -1, "cr": -1})
        self._codex_sampled_at = time.monotonic()
        self.revision += 1

    def snapshot(self, with_system: bool = True) -> dict:
        """Latest payload with fresh countdowns and clock.

        with_system=False leaves out the machine-load keys.
        """
        payload = dict(self._payload)
        elapsed = max(0.0, time.monotonic() - self._sampled_at)
        for key in ("sr", "wr", "fr", "cr"):
            if payload.get(key, -1) >= 0:
                payload[key] = max(0, int(round((payload[key] * 60 - elapsed) / 60)))
        codex_age = max(0.0, time.monotonic() - self._codex_sampled_at)
        if payload.get("cr", -1) >= 0:
            payload["cr"] = max(0, int(round((self._payload["cr"] * 60 - codex_age) / 60)))
        if codex_age > CODEX_STALE:
            if payload.get("cm") == 1:
                payload["cs"] = 1  # keep last known amount, visibly old
            else:
                payload.update(c=-1, cr=-1)
        antigravity_age = max(0.0, time.monotonic() - self._antigravity_sampled_at)
        if (antigravity_age > ANTIGRAVITY_STALE or
                (self._antigravity_reset_at is not None and
                 time.time() >= self._antigravity_reset_at)):
            payload.update({"a": -1, "ar": -1, "ab": -1})
        elif self._antigravity_reset_at is not None:
            payload["ar"] = max(0, int(round((self._antigravity_reset_at - time.time()) / 60)))
        lt = time.localtime()
        payload["t"] = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        if with_system and self.system is not None:
            payload.update(self.system.latest())
        return payload

    async def run(self, stop_event: asyncio.Event) -> None:
        # One collector owns all provider reads; Codex updates every 30s while
        # Claude/AntiGravity retain their existing 90s schedule. No overlap.
        next_full = next_codex = 0.0
        while not stop_event.is_set():
            started = time.monotonic()
            if started >= next_full:
                await self.poll_once()
                next_full = started + POLL_INTERVAL
            else:
                await self.poll_codex_once()
            next_codex = started + CODEX_POLL_INTERVAL
            delay = max(0.1, min(next_full, next_codex) - time.monotonic())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def publish_snapshot(collector: UsageCollector, path: Path = SNAPSHOT) -> None:
    """Atomic write, readable only by this user."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    envelope = {"version": 1, "updated_at": time.time(), "payload": collector.snapshot()}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(envelope, stream, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


async def publish_loop(stop_event: asyncio.Event, collector: UsageCollector) -> None:
    while not stop_event.is_set():
        try:
            publish_snapshot(collector)
        except (OSError, ValueError) as e:
            log(f"Snapshot write failed ({type(e).__name__})")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SNAPSHOT_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    log(f"Collector started; snapshot -> {SNAPSHOT}")
    system = SystemSampler()
    collector = UsageCollector(system)
    await asyncio.gather(system.run(stop_event), collector.run(stop_event),
                         publish_loop(stop_event, collector))


if __name__ == "__main__":
    asyncio.run(main())
