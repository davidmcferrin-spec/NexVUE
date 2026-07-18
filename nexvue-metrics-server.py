#!/usr/bin/env python3
"""
nexvue-metrics-server.py — usage/analytics metrics COLLECTOR for NexVUE.

This process does NOT listen on any network port or serve any HTTP at all —
it is a pure background poller that writes time-series samples into SQLite.
Reading the data back out is Apache + PHP's job (see nexvue-metrics.php),
querying the same SQLite file directly. That split means this collector has
zero firewall/port exposure of any kind: nothing to open, nothing to proxy,
no WebSocket, nothing for a security team to review on this side at all.

This is USAGE ANALYTICS, not health/uptime monitoring (that is CheckMK's job
per the project roadmap) — it answers "how much bandwidth did we serve last
week," "was this input locked all night," and "which IP was watching
channel 2 at 3am," not "is the service up right now."

Polls existing NexVUE services (and local /proc) on an interval and stores
time-series samples in SQLite (stdlib `sqlite3` — no pip, no new dependency):
  - MediaMTX Control API   -> per-path bandwidth (bytesSent delta), viewer
                              counts (readers), active-stream (ready) state
  - MediaMTX Control API   -> per-VIEWER session detail: remote IP, channel,
                              user (once Phase 2 auth exists), byte counts
                              (/v3/webrtcsessions/list — a separate, richer
                              endpoint from the paths list above)
  - nexvue-status daemon   -> per-input signal lock, detected format,
                              reference lock/format
  - /proc/stat, meminfo, loadavg -> host CPU %, memory used/total, load1
  - /sys/class/hwmon (+ thermal_zone fallback) -> CPU package °C and
                              iGPU °C when the driver exposes a sensor
  - intel_gpu_top -J (optional) -> iGPU Video/Render/VideoEnhance busy %
                              and frequency (capacity correlation for
                              Metrics — not a CheckMK substitute). Read from
                              ONE persistent child process (see _GpuStream),
                              never a kill-after-timeout one-shot: the tool
                              block-buffers stdout on a pipe, so short runs
                              died before their first flush and left the
                              iGPU charts empty.

Old samples are pruned hourly (NEXVUE_METRICS_RETENTION_DAYS, default 30) so
the SQLite file does not grow unbounded.

Runs as systemd service nexvue-metrics.service. No LISTEN_ADDR, no port —
there is genuinely nothing here to open a firewall rule for.
"""

import json
import logging
import os
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[nexvue-metrics] %(message)s")
log = logging.getLogger(__name__)

# ---- Configuration (env-overridable via systemd Environment= lines) -------------
DB_PATH = os.environ.get("NEXVUE_METRICS_DB", "/var/lib/nexvue/metrics.db")
POLL_INTERVAL_S = float(os.environ.get("NEXVUE_METRICS_POLL_INTERVAL_S", "15"))
RETENTION_DAYS = float(os.environ.get("NEXVUE_METRICS_RETENTION_DAYS", "30"))
PRUNE_EVERY_S = 3600  # retention sweep runs hourly, not every poll

MEDIAMTX_API_URL = os.environ.get("NEXVUE_MEDIAMTX_API_URL", "https://127.0.0.1:9997")
# When unset, try HTTP then HTTPS (status TLS is optional; matches nexvue-status.php).
# When set, use that single base URL only.
_STATUS_URL_ENV = os.environ.get("NEXVUE_STATUS_URL", "").strip()
FETCH_TIMEOUT_S = 5.0

_db_lock = threading.Lock()
# In-memory cumulative-counter baseline per channel, so MediaMTX's running
# byte totals can be turned into instantaneous bandwidth between samples.
# Touched only from the single poll_loop thread — no lock needed.
_prev_bytes: dict = {}
# Previous /proc/stat counters for CPU % between polls.
_prev_cpu: tuple | None = None
# Rate-limit intel_gpu_top failure logs (seconds between warnings).
_gpu_warn_last = 0.0
_GPU_WARN_EVERY_S = 300.0
# Rate-limit temperature sampling warnings (missing hwmon is common for iGPU).
_temp_warn_last = 0.0
_TEMP_WARN_EVERY_S = 300.0
HWMON_ROOT = os.environ.get("NEXVUE_HWMON_ROOT", "/sys/class/hwmon")
THERMAL_ROOT = os.environ.get("NEXVUE_THERMAL_ROOT", "/sys/class/thermal")
INTEL_GPU_TOP = os.environ.get("NEXVUE_INTEL_GPU_TOP", "intel_gpu_top")
# One intel_gpu_top sample period in ms. The tool streams continuously; the
# persistent reader thread (see _GpuStream) keeps only the newest sample.
INTEL_GPU_TOP_PERIOD_MS = int(float(os.environ.get("NEXVUE_INTEL_GPU_TOP_PERIOD_MS", "1000")))
# A sample older than this is treated as "no data" (tool died or stalled).
# Generous relative to the poll interval because intel_gpu_top block-buffers
# stdout when writing to a pipe — samples can arrive in multi-second bursts.
_GPU_STALE_AFTER_S = max(30.0, INTEL_GPU_TOP_PERIOD_MS / 1000.0 * 10)


def _unverified_ssl_context() -> "ssl.SSLContext | None":
    """
    Context for calling our OWN loopback services (MediaMTX API, status
    daemon), which may be using a self-signed cert. Skipping verification is
    acceptable here specifically because this traffic never leaves 127.0.0.1
    — it is not fetching anything from the outside network. Do not reuse
    this pattern for any request that isn't strictly loopback-to-self.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_json(url: str) -> dict:
    ctx = _unverified_ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S, context=ctx) as resp:
        return json.loads(resp.read().decode())


def _status_base_urls() -> list[str]:
    """Base URLs for the status daemon (no trailing path). Env override wins."""
    if _STATUS_URL_ENV:
        return [_STATUS_URL_ENV.rstrip("/")]
    return ["http://127.0.0.1:9998", "https://127.0.0.1:9998"]


def _fetch_json_first(urls: list[str]) -> dict:
    """Try each URL in order; return the first successful JSON body.

    Raises the last exception if every candidate fails — callers log once.
    """
    last_exc: BaseException | None = None
    for url in urls:
        try:
            return _fetch_json(url)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise urllib.error.URLError("no status URL candidates")


def _compute_bandwidth_bps(prev_ts: int, prev_bytes: int, now_ts: int, now_bytes: int) -> float:
    """Bits/sec between two cumulative byte-counter samples. Returns 0 for a
    non-positive interval or a counter reset (e.g. the stream restarted and
    MediaMTX's counter started over) rather than a nonsensical negative rate."""
    dt = now_ts - prev_ts
    if dt <= 0 or now_bytes < prev_bytes:
        return 0.0
    return (now_bytes - prev_bytes) * 8 / dt


def _parse_proc_stat_cpu(text: str) -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) from a /proc/stat blob. First line
    must be the aggregate `cpu ` row. Raises ValueError if malformed."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            # cpu user nice system idle iowait irq softirq steal guest guest_nice
            nums = [int(x) for x in parts[1:]]
            if len(nums) < 4:
                raise ValueError("cpu line too short")
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
            total = sum(nums)
            return idle, total
    raise ValueError("no cpu line in /proc/stat")


def _compute_cpu_pct(prev_idle: int, prev_total: int, idle: int, total: int) -> float:
    """Percent non-idle CPU between two /proc/stat samples (0–100)."""
    d_total = total - prev_total
    d_idle = idle - prev_idle
    if d_total <= 0:
        return 0.0
    busy = d_total - d_idle
    if busy < 0:
        return 0.0
    return 100.0 * busy / d_total


def _parse_meminfo(text: str) -> tuple[int, int]:
    """Return (mem_used_bytes, mem_total_bytes) from /proc/meminfo text.
    Prefers MemAvailable for used = total − available."""
    vals: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.split()
        if not parts:
            continue
        # Values are kB
        vals[key] = int(parts[0]) * 1024
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", vals.get("MemFree", 0))
    used = max(0, total - available)
    return used, total


def _parse_loadavg(text: str) -> float:
    """1-minute load average from /proc/loadavg."""
    return float(text.split()[0])


def _read_millideg_c(path: Path) -> float | None:
    """Read a sysfs *_input file in millidegrees Celsius → °C, or None."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        milli = int(raw)
    except (OSError, ValueError):
        return None
    # Reject nonsense (uninitialized sensors sometimes report 0 or huge values).
    celsius = milli / 1000.0
    if celsius < 1.0 or celsius > 150.0:
        return None
    return celsius


def _hwmon_chip_dirs(hwmon_root: Path) -> list[tuple[str, Path]]:
    """Return [(chip_name, dir), ...] for each hwmonN under hwmon_root."""
    out: list[tuple[str, Path]] = []
    if not hwmon_root.is_dir():
        return out
    try:
        entries = sorted(hwmon_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.name.startswith("hwmon"):
            continue
        name_path = entry / "name"
        try:
            name = name_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if name:
            out.append((name, entry))
    return out


def _read_hwmon_temp1(chip_dir: Path) -> float | None:
    """Prefer temp1_input (package / primary); else first readable temp*_input."""
    primary = chip_dir / "temp1_input"
    got = _read_millideg_c(primary)
    if got is not None:
        return got
    try:
        candidates = sorted(chip_dir.glob("temp*_input"))
    except OSError:
        return None
    for path in candidates:
        got = _read_millideg_c(path)
        if got is not None:
            return got
    return None


def _read_cpu_temp_c(hwmon_root: Path, thermal_root: Path) -> float | None:
    """CPU package temperature from coretemp hwmon, else x86_pkg_temp zone."""
    for name, chip_dir in _hwmon_chip_dirs(hwmon_root):
        if name == "coretemp":
            got = _read_hwmon_temp1(chip_dir)
            if got is not None:
                return got
    if not thermal_root.is_dir():
        return None
    try:
        zones = sorted(thermal_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for zone in zones:
        if not zone.name.startswith("thermal_zone"):
            continue
        try:
            ztype = (zone / "type").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if ztype != "x86_pkg_temp":
            continue
        got = _read_millideg_c(zone / "temp")
        if got is not None:
            return got
    return None


def _read_gpu_temp_c(hwmon_root: Path) -> float | None:
    """iGPU temperature from i915 or xe hwmon (None when driver has no node)."""
    for name, chip_dir in _hwmon_chip_dirs(hwmon_root):
        if name in ("i915", "xe"):
            got = _read_hwmon_temp1(chip_dir)
            if got is not None:
                return got
    return None


def sample_temps(
    hwmon_root: str | Path | None = None,
    thermal_root: str | Path | None = None,
) -> tuple[float | None, float | None]:
    """
    Return (cpu_temp_c, gpu_temp_c). Either may be None when sysfs has no
    usable sensor — never invent a value. Paths are overridable for tests.
    """
    global _temp_warn_last
    hw_root = Path(hwmon_root if hwmon_root is not None else HWMON_ROOT)
    th_root = Path(thermal_root if thermal_root is not None else THERMAL_ROOT)
    cpu_c: float | None = None
    gpu_c: float | None = None
    try:
        cpu_c = _read_cpu_temp_c(hw_root, th_root)
        gpu_c = _read_gpu_temp_c(hw_root)
    except OSError as exc:
        now = time.time()
        if now - _temp_warn_last >= _TEMP_WARN_EVERY_S:
            log.warning("temperature sample failed: %s", exc)
            _temp_warn_last = now
        return None, None
    if cpu_c is None:
        now = time.time()
        if now - _temp_warn_last >= _TEMP_WARN_EVERY_S:
            log.warning(
                "CPU package temperature unavailable — no coretemp / x86_pkg_temp under %s",
                hw_root,
            )
            _temp_warn_last = now
    return cpu_c, gpu_c


def sample_host() -> tuple[float | None, int, int, float]:
    """Read host CPU/memory/load. cpu_pct is None on the first sample (no
    prior /proc/stat baseline yet)."""
    global _prev_cpu
    cpu_pct: float | None = None
    try:
        stat_text = Path("/proc/stat").read_text(encoding="utf-8")
        idle, total = _parse_proc_stat_cpu(stat_text)
        if _prev_cpu is not None:
            cpu_pct = _compute_cpu_pct(_prev_cpu[0], _prev_cpu[1], idle, total)
        _prev_cpu = (idle, total)
    except (OSError, ValueError) as exc:
        log.warning("host cpu sample failed: %s", exc)

    mem_used, mem_total = 0, 0
    try:
        mem_used, mem_total = _parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("host mem sample failed: %s", exc)

    load1 = 0.0
    try:
        load1 = _parse_loadavg(Path("/proc/loadavg").read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError) as exc:
        log.warning("host loadavg sample failed: %s", exc)

    return cpu_pct, mem_used, mem_total, load1


def _classify_gpu_engine(name: str) -> str | None:
    """Map an intel_gpu_top engine name to video | enhance | render."""
    n = name.lower().replace(" ", "")
    if "videoenhance" in n or "vebox" in n or n.startswith("vecs"):
        return "enhance"
    if n.startswith("video") or n.startswith("vcs") or "/vcs" in n:
        return "video"
    if "render" in n or n.startswith("rcs") or "3d" in n:
        return "render"
    return None


def _parse_intel_gpu_top_json(text: str) -> dict:
    """
    Parse one sample from intel_gpu_top -J output text.

    Returns dict with keys gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct,
    gpu_freq_mhz (values may be None if absent). Raises ValueError if unusable.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("empty intel_gpu_top output")
    # Man page: wrap NDJSON in [] — also accept a single object or first object.
    if raw.startswith("["):
        arr = json.loads(raw)
        if not arr:
            raise ValueError("empty JSON array")
        obj = arr[0]
    else:
        obj, _ = json.JSONDecoder().raw_decode(raw)

    if not isinstance(obj, dict):
        raise ValueError("sample is not an object")
    return _extract_gpu_sample(obj)


def _extract_gpu_sample(obj: dict) -> dict:
    """Pull the four stored metrics out of one parsed intel_gpu_top JSON object."""
    engines = obj.get("engines")
    video_vals: list[float] = []
    render_vals: list[float] = []
    enhance_vals: list[float] = []

    if isinstance(engines, dict):
        items = engines.items()
    elif isinstance(engines, list):
        items = []
        for e in engines:
            if isinstance(e, dict):
                items.append((e.get("name") or e.get("engine") or "", e))
    else:
        items = []

    for name, info in items:
        if not isinstance(info, dict):
            continue
        busy = info.get("busy")
        if busy is None:
            continue
        try:
            pct = float(busy)
        except (TypeError, ValueError):
            continue
        kind = _classify_gpu_engine(str(name))
        if kind == "video":
            video_vals.append(pct)
        elif kind == "enhance":
            enhance_vals.append(pct)
        elif kind == "render":
            render_vals.append(pct)

    freq_mhz = None
    freq = obj.get("frequency")
    if isinstance(freq, dict):
        for key in ("actual", "requested", "cur", "current"):
            if freq.get(key) is not None:
                try:
                    freq_mhz = float(freq[key])
                    break
                except (TypeError, ValueError):
                    pass
    elif isinstance(obj.get("freq"), (int, float)):
        freq_mhz = float(obj["freq"])

    return {
        "gpu_video_pct": max(video_vals) if video_vals else None,
        "gpu_render_pct": max(render_vals) if render_vals else None,
        "gpu_video_enhance_pct": max(enhance_vals) if enhance_vals else None,
        "gpu_freq_mhz": freq_mhz,
    }


def _consume_stream_objects(buf: str) -> "tuple[list[dict], str]":
    """
    Extract every complete JSON object from an intel_gpu_top -J stream buffer.

    Handles both output shapes seen across intel-gpu-tools versions: bare
    concatenated pretty-printed objects (older builds) and a never-closing
    array — "[\\n{...},\\n{...}" — (newer builds). Returns (objects, remainder)
    where remainder is the unconsumed tail (usually a partial object still
    being written).
    """
    objs: list[dict] = []
    dec = json.JSONDecoder()
    i, n = 0, len(buf)
    while True:
        while i < n and buf[i] in " \t\r\n,[]":
            i += 1
        if i >= n:
            return objs, ""
        try:
            obj, end = dec.raw_decode(buf, i)
        except json.JSONDecodeError:
            return objs, buf[i:]
        if isinstance(obj, dict):
            objs.append(obj)
        i = end


class _GpuStream:
    """
    Persistent intel_gpu_top -J reader.

    Why persistent: the previous design forked intel_gpu_top on every poll,
    killed it after a short timeout, and parsed whatever stdout the kill left
    behind. When stdout is a pipe (not a TTY), intel_gpu_top block-buffers
    its output, so a short-lived run is routinely killed before the first
    buffer flush — empty stdout, empty iGPU charts — even though the very
    same command shows live numbers when run interactively. Keeping ONE
    long-lived child and reading its stream continuously makes buffering
    irrelevant (every flush eventually arrives and we always serve the
    newest sample), and costs one PMU client instead of a fork per poll.

    A daemon thread drains stdout and keeps only the latest parsed sample;
    a second daemon thread drains stderr (bounded) so the child can never
    block on a full pipe and we have diagnostics if it exits. sample_gpu()
    pulls the newest sample and transparently restarts the child (with
    backoff) if it died.
    """

    _RESTART_BACKOFF_S = 30.0
    _MAX_BUFFER_CHARS = 1_000_000  # garbage guard — a sample is ~1 KB

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: "subprocess.Popen | None" = None
        self._latest: "tuple[float, dict] | None" = None  # (monotonic ts, sample)
        self._next_start = 0.0  # monotonic time before which restarts are suppressed
        self._stderr_tail = ""

    # -- public ---------------------------------------------------------------

    def latest(self) -> "dict | None":
        """Newest parsed sample, or None if the child is dead/silent/stale."""
        self._ensure_running()
        with self._lock:
            if self._latest is None:
                return None
            ts, sample = self._latest
            if time.monotonic() - ts > _GPU_STALE_AFTER_S:
                return None
            return dict(sample)

    # -- internals ------------------------------------------------------------

    def _ensure_running(self) -> None:
        global _gpu_warn_last
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            now_mono = time.monotonic()
            if now_mono < self._next_start:
                return
            self._next_start = now_mono + self._RESTART_BACKOFF_S
            died = self._proc
            self._proc = None

        if died is not None:
            now = time.time()
            if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
                tail = self._stderr_tail.strip()
                log.warning(
                    "intel_gpu_top exited (rc=%s)%s — restarting",
                    died.returncode,
                    f": {tail}" if tail else "",
                )
                _gpu_warn_last = now

        try:
            proc = subprocess.Popen(
                [INTEL_GPU_TOP, "-J", "-s", str(INTEL_GPU_TOP_PERIOD_MS), "-o", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            now = time.time()
            if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
                log.warning("intel_gpu_top not found — install intel-gpu-tools for iGPU metrics")
                _gpu_warn_last = now
            return
        except OSError as exc:
            now = time.time()
            if now - _gpu_warn_last >= _GPU_WARN_EVERY_S:
                log.warning("intel_gpu_top start failed: %s", exc)
                _gpu_warn_last = now
            return

        with self._lock:
            self._proc = proc
            self._stderr_tail = ""
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()

    def _read_stdout(self, proc: "subprocess.Popen") -> None:
        buf = ""
        stdout = proc.stdout
        assert stdout is not None
        while True:
            try:
                chunk = stdout.read1(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            objs, buf = _consume_stream_objects(buf)
            if len(buf) > self._MAX_BUFFER_CHARS:
                buf = ""
            for obj in objs:
                sample = _extract_gpu_sample(obj)
                # Skip objects with no usable metric (e.g. header/period-only
                # records) so they never clobber a real sample.
                if all(v is None for v in sample.values()):
                    continue
                with self._lock:
                    self._latest = (time.monotonic(), sample)

    def _read_stderr(self, proc: "subprocess.Popen") -> None:
        stderr = proc.stderr
        assert stderr is not None
        while True:
            try:
                chunk = stderr.read1(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._stderr_tail = (self._stderr_tail
                                     + chunk.decode("utf-8", errors="replace"))[-2048:]


_gpu_stream = _GpuStream()


def sample_gpu() -> dict:
    """
    Newest iGPU engine sample from the persistent intel_gpu_top stream.
    Returns a dict with all-None fields when no fresh sample is available
    (tool missing, no permission, child restarting, first period not yet
    elapsed) — host CPU/memory collection is unaffected either way.
    """
    got = _gpu_stream.latest()
    if got is not None:
        return got
    return {
        "gpu_video_pct": None,
        "gpu_render_pct": None,
        "gpu_video_enhance_pct": None,
        "gpu_freq_mhz": None,
    }


# ---- Database --------------------------------------------------------------------

def _ensure_db_readable() -> None:
    """
    Make the SQLite file (and its WAL-mode sidecar files) world-readable so
    PHP running as the web server's own user (commonly www-data, not this
    service's `nexvue` user) can open it directly — no group-membership
    fiddling needed. Safe: this data (bandwidth, viewer IPs/channels) is
    explicitly meant to be served publicly via Apache anyway, so read access
    isn't a meaningful exposure. Called after init and after every poll
    cycle, since WAL mode creates/recreates the -wal/-shm sidecars over time.
    """
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{DB_PATH}{suffix}")
        if p.exists():
            try:
                p.chmod(0o644)
            except OSError as exc:
                log.warning("could not chmod %s: %s", p, exc)


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # WAL mode lets this collector (writer) and PHP (reader, via Apache)
        # coexist without "database is locked" contention. PHP must open its
        # connection read-only (see nexvue-metrics.php) rather than needing
        # any special handling on this side.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER NOT NULL,
                channel TEXT NOT NULL,
                bandwidth_bps REAL,
                readers INTEGER,
                ready INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_samples_channel_ts ON samples(channel, ts);
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

            CREATE TABLE IF NOT EXISTS totals (
                ts INTEGER NOT NULL PRIMARY KEY,
                active_streams INTEGER,
                total_readers INTEGER,
                total_bandwidth_bps REAL
            );

            CREATE TABLE IF NOT EXISTS input_status (
                ts INTEGER NOT NULL,
                device_index INTEGER NOT NULL,
                card_name TEXT,
                input_locked INTEGER,
                input_mode TEXT,
                reference_locked INTEGER,
                reference_mode TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_input_status_dev_ts ON input_status(device_index, ts);

            -- One row per WebRTC viewer session (state="read" only — this
            -- excludes our own encoders' "publish" sessions). Upserted by
            -- session_id each poll: first_seen is set once, last_seen and
            -- bytes_sent update every cycle the session is still active, so
            -- a single row gives the full lifecycle of one viewer watching
            -- one channel — IP, channel, when they joined, how long, how
            -- much data, and (once Phase 2 auth exists) which user.
            CREATE TABLE IF NOT EXISTS viewer_sessions (
                session_id TEXT PRIMARY KEY,
                remote_addr TEXT,
                channel TEXT,
                user_agent TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                bytes_sent INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_channel ON viewer_sessions(channel);
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_remote ON viewer_sessions(remote_addr);
            CREATE INDEX IF NOT EXISTS idx_viewer_sessions_last_seen ON viewer_sessions(last_seen);

            -- Host capacity samples (CPU/memory/load + optional iGPU engines
            -- and package/GPU temperatures) for Metrics correlation. Not a
            -- health monitor — CheckMK remains Phase 4 for that. GPU engine
            -- columns filled when intel_gpu_top works; temp columns when
            -- sysfs hwmon exposes coretemp / i915|xe sensors.
            CREATE TABLE IF NOT EXISTS host_samples (
                ts INTEGER NOT NULL PRIMARY KEY,
                cpu_pct REAL,
                mem_used_bytes INTEGER,
                mem_total_bytes INTEGER,
                load1 REAL,
                gpu_video_pct REAL,
                gpu_render_pct REAL,
                gpu_video_enhance_pct REAL,
                gpu_freq_mhz REAL,
                cpu_temp_c REAL,
                gpu_temp_c REAL
            );
        """)
        # Safe migrations for databases created before later columns existed —
        # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite.
        for ddl in (
            "ALTER TABLE viewer_sessions ADD COLUMN user TEXT",
            "ALTER TABLE host_samples ADD COLUMN gpu_video_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_render_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_video_enhance_pct REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_freq_mhz REAL",
            "ALTER TABLE host_samples ADD COLUMN cpu_temp_c REAL",
            "ALTER TABLE host_samples ADD COLUMN gpu_temp_c REAL",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    log.info("database ready at %s", DB_PATH)
    _ensure_db_readable()


def prune_old_samples() -> None:
    cutoff = int(time.time() - RETENTION_DAYS * 86400)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        for table in ("samples", "totals", "input_status", "host_samples"):
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        # viewer_sessions has no `ts` column (it's a lifecycle row, not a
        # timestamped sample) — prune on last_seen instead.
        conn.execute("DELETE FROM viewer_sessions WHERE last_seen < ?", (cutoff,))
        conn.commit()
    log.info("pruned samples older than %s (%.0f day retention)", cutoff, RETENTION_DAYS)


# ---- Collection loop ---------------------------------------------------------------

def poll_once() -> None:
    now = int(time.time())

    # --- MediaMTX: bandwidth, viewers, active-stream state, per path ---
    try:
        data = _fetch_json(f"{MEDIAMTX_API_URL}/v3/paths/list")
        rows = []
        active_streams = 0
        total_readers = 0
        total_bw = 0.0
        for item in data.get("items", []):
            name = item.get("name", "")
            ready = bool(item.get("ready"))
            readers = len(item.get("readers", []))
            bytes_sent = int(item.get("bytesSent", 0))

            bw = 0.0
            prev = _prev_bytes.get(name)
            if prev:
                bw = _compute_bandwidth_bps(prev[0], prev[1], now, bytes_sent)
            _prev_bytes[name] = (now, bytes_sent)

            rows.append((now, name, bw, readers, int(ready)))
            if ready:
                active_streams += 1
            total_readers += readers
            total_bw += bw

        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO samples (ts, channel, bandwidth_bps, readers, ready) VALUES (?,?,?,?,?)",
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO totals (ts, active_streams, total_readers, total_bandwidth_bps) "
                "VALUES (?,?,?,?)",
                (now, active_streams, total_readers, total_bw),
            )
            conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("MediaMTX API poll failed: %s", exc)

    # --- MediaMTX: per-viewer session drill-down (IP + channel + duration) ---
    # /v3/paths/list only gives a reader COUNT per path; the actual remote
    # address and per-session detail live on this separate endpoint.
    try:
        data = _fetch_json(f"{MEDIAMTX_API_URL}/v3/webrtcsessions/list")
        rows = []
        for item in data.get("items", []):
            # state is "read" (viewer) or "publish" (our own encoders) —
            # only viewers are relevant for this drill-down.
            if item.get("state") != "read":
                continue
            rows.append((
                item.get("id", ""),
                item.get("remoteAddr", ""),
                item.get("path", ""),
                item.get("userAgent", ""),
                now,  # first_seen — only used on first INSERT, see ON CONFLICT below
                now,  # last_seen — updated every poll while the session persists
                int(item.get("outboundBytes", 0)),
                item.get("user", ""),  # blank until Phase 2 auth exists
            ))
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO viewer_sessions "
                "(session_id, remote_addr, channel, user_agent, first_seen, last_seen, bytes_sent, user) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "last_seen=excluded.last_seen, bytes_sent=excluded.bytes_sent, user=excluded.user",
                rows,
            )
            conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("MediaMTX webrtcsessions poll failed: %s", exc)

    # --- Status daemon: per-input lock/format history ---
    try:
        status_urls = [f"{base}/status" for base in _status_base_urls()]
        data = _fetch_json_first(status_urls)
        if not data.get("stale"):
            rows = [
                (
                    now,
                    d.get("index"),
                    d.get("name", ""),
                    int(bool(d.get("input_locked"))),
                    d.get("input_mode", "unknown"),
                    int(bool(d.get("reference_locked"))),
                    d.get("reference_mode", "unknown"),
                )
                for d in data.get("devices", [])
            ]
            with _db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.executemany(
                    "INSERT INTO input_status (ts, device_index, card_name, input_locked, "
                    "input_mode, reference_locked, reference_mode) VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
        log.warning("status daemon poll failed: %s", exc)

    # --- Host CPU / memory / load + optional iGPU engines + package/GPU temps ---
    cpu_pct, mem_used, mem_total, load1 = sample_host()
    gpu = sample_gpu()
    cpu_temp_c, gpu_temp_c = sample_temps()
    if cpu_pct is not None:
        try:
            with _db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO host_samples "
                    "(ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1, "
                    " gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct, gpu_freq_mhz, "
                    " cpu_temp_c, gpu_temp_c) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        now, cpu_pct, mem_used, mem_total, load1,
                        gpu.get("gpu_video_pct"), gpu.get("gpu_render_pct"),
                        gpu.get("gpu_video_enhance_pct"), gpu.get("gpu_freq_mhz"),
                        cpu_temp_c, gpu_temp_c,
                    ),
                )
                conn.commit()
        except OSError as exc:
            log.warning("host_samples write failed: %s", exc)


def poll_loop() -> None:
    last_prune = 0.0
    while True:
        poll_once()
        _ensure_db_readable()
        if time.time() - last_prune > PRUNE_EVERY_S:
            try:
                prune_old_samples()
            except OSError as exc:
                log.warning("prune failed: %s", exc)
            last_prune = time.time()
        time.sleep(POLL_INTERVAL_S)


def main() -> None:
    init_db()
    log.info("collector running, polling every %.1fs, db=%s (no HTTP server — "
              "reads happen via PHP querying this SQLite file directly)",
              POLL_INTERVAL_S, DB_PATH)
    poll_loop()  # runs forever on the main thread — nothing else to do


if __name__ == "__main__":
    main()
