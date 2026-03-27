#!/usr/bin/env python3
"""
GitHub Actions Runner Dashboard
Visual monitoring for self-hosted runners on this Mac.
Shows runner status, active jobs, CPU/memory, recent workflow runs,
simulator state, live test progress, and streaming logs.

Usage: python3 runner-dashboard.py
       Press 'q' to quit, 'r' to force refresh
       Arrow keys or j/k to select a run, 'c' to cancel it, Esc to deselect
       'l' to toggle log pane, Tab to switch focus between runs and logs
"""

import collections
import curses
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# -- Configuration ------------------------------------------------------------

REPO = "bare-metal-coder/doorcash"
REFRESH_INTERVAL = 4  # seconds

RUNNERS = [
    {
        "name": "manas-mac",
        "label": "runner-1",
        "path": Path.home() / "Documents/Projects/github-runner",
        "job_type": "Unit Tests",
    },
    {
        "name": "manas-mac-2",
        "label": "runner-2",
        "path": Path.home() / "Documents/Projects/github-runner-2",
        "job_type": "UI Tests",
    },
]

LOCAL_RUNNER_NAMES = {r["name"] for r in RUNNERS}

# Cache: run_id -> list[JobInfo] for completed runs (they won't change)
_job_cache: dict[int, list] = {}

# -- Data Models ---------------------------------------------------------------


@dataclass
class RunnerStatus:
    name: str
    label: str
    job_type: str
    path: Path
    pid: int = 0
    online: bool = False
    busy: bool = False
    cpu_percent: float = 0.0
    mem_mb: float = 0.0
    current_job: str = ""
    last_job_time: str = ""
    uptime: str = ""
    worker_pids: list = field(default_factory=list)
    worker_cpu: float = 0.0
    worker_mem: float = 0.0


@dataclass
class JobInfo:
    name: str = ""
    runner_name: str = ""
    status: str = ""
    conclusion: str = ""
    started: str = ""
    completed: str = ""
    duration: str = ""


@dataclass
class WorkflowRun:
    id: int = 0
    name: str = ""
    status: str = ""
    conclusion: str = ""
    branch: str = ""
    started: str = ""
    duration: str = ""
    run_number: int = 0
    jobs: list = field(default_factory=list)  # list of JobInfo


@dataclass
class SystemStats:
    cpu_percent: float = 0.0
    mem_total_gb: float = 0.0
    mem_used_gb: float = 0.0
    mem_percent: float = 0.0
    load_avg: tuple = (0.0, 0.0, 0.0)
    xcode_processes: int = 0
    simulator_processes: int = 0


# -- Log Collector (background thread-based log streaming) ---------------------


class LogCollector:
    """Collects logs from simulators and xctest processes in the background."""

    def __init__(self, max_lines=500):
        self.lines = collections.deque(maxlen=max_lines)
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        self._tracked_pids = set()
        self._tracked_sims = set()

    def start(self):
        """Start a manager thread that discovers and tails log sources."""
        t = threading.Thread(target=self._manager_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        self._stop.set()

    def get_lines(self, n=50):
        with self.lock:
            return list(self.lines)[-n:]

    def _add_line(self, source, line):
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.lines.append((ts, source, line.rstrip()))

    def _manager_loop(self):
        """Periodically discover new log sources and spawn tailers."""
        while not self._stop.is_set():
            self._discover_xctest_logs()
            self._discover_sim_logs()
            self._stop.wait(3)

    def _discover_xctest_logs(self):
        """Find xcodebuild log files being written (tee output)."""
        out = run_cmd(
            "ps aux | grep 'tee.*xcodebuild\\|tee.*uitest\\|tee.*log' | grep -v grep",
            timeout=5,
        )
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            pid = parts[1]
            if pid in self._tracked_pids:
                continue
            full_cmd = run_cmd(f"ps -p {pid} -o args=", timeout=3)
            match = re.search(r"tee\s+(\S+)", full_cmd)
            if match:
                log_path = match.group(1)
                if os.path.exists(log_path):
                    self._tracked_pids.add(pid)
                    t = threading.Thread(
                        target=self._tail_file,
                        args=(log_path, "xctest"),
                        daemon=True,
                    )
                    t.start()
                    self._threads.append(t)

        # Also look for common CI log paths and xcresult session logs
        for pattern in [
            "/Users/*/Documents/Projects/github-runner*/_work/_temp/xcodebuild.log",
            "/Users/*/Documents/Projects/github-runner*/_work/_temp/uitest.log",
            "/tmp/uitest*.log",
            "/Users/*/Documents/Projects/github-runner*/_work/_temp/*.xcresult/**/Session-*.log",
        ]:
            for path in glob.glob(pattern):
                if path not in self._tracked_pids:
                    self._tracked_pids.add(path)
                    t = threading.Thread(
                        target=self._tail_file,
                        args=(path, "xctest"),
                        daemon=True,
                    )
                    t.start()
                    self._threads.append(t)

    def _discover_sim_logs(self):
        """Find booted simulators and stream their app logs."""
        out = run_cmd("xcrun simctl list devices booted -j", timeout=3)
        if not out:
            return
        try:
            data = json.loads(out)
        except Exception:
            return
        for runtime, devices in data.get("devices", {}).items():
            for dev in devices:
                udid = dev.get("udid", "")
                name = dev.get("name", "Simulator")
                if udid and udid not in self._tracked_sims:
                    self._tracked_sims.add(udid)
                    t = threading.Thread(
                        target=self._tail_sim_log,
                        args=(udid, name),
                        daemon=True,
                    )
                    t.start()
                    self._threads.append(t)

    def _tail_file(self, path, source):
        """Tail a log file, filtering for interesting lines."""
        try:
            with open(path, "rb") as f:
                try:
                    f.seek(-10240, 2)
                except OSError:
                    f.seek(0)
                f.readline()  # skip partial line

                while not self._stop.is_set():
                    raw = f.readline()
                    if raw:
                        line = raw.decode("utf-8", errors="replace")
                        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
                        filtered = self._filter_xctest_line(line)
                        if filtered:
                            self._add_line(source, filtered)
                    else:
                        self._stop.wait(0.2)
        except Exception:
            pass

    def _tail_sim_log(self, udid, name):
        """Stream simulator system log for app processes."""
        try:
            proc = subprocess.Popen(
                [
                    "xcrun", "simctl", "spawn", udid, "log", "stream",
                    "--predicate",
                    'process == "DoorCash" OR process == "DoorCashUITests-Runner"',
                    "--style", "compact",
                    "--level", "error",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            short_name = name.replace("iPhone ", "i").replace("iPad ", "iP")
            while not self._stop.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line and not line.startswith("Filtering"):
                    self._add_line(f"sim:{short_name}", line[:200])
            proc.terminate()
        except Exception:
            pass

    @staticmethod
    def _filter_xctest_line(line):
        """Filter xctest/xcodebuild log lines to show only interesting ones."""
        line = line.strip()
        if not line:
            return None
        if "Test Case" in line and ("passed" in line or "failed" in line):
            return line
        if "Executed" in line and "tests" in line:
            return line
        if "error:" in line.lower() or "fatal" in line.lower():
            return line
        if "signal kill" in line.lower() or "crash" in line.lower():
            return line
        if line.startswith("Compiling") or line.startswith("Linking"):
            return line
        if "Total:" in line or "Passed:" in line:
            return line
        return None


# -- Data Collection -----------------------------------------------------------


def run_cmd(cmd, timeout=10):
    """Run a shell command and return stdout."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        return ""


def get_runner_processes():
    """Find runner listener and worker PIDs with CPU/mem stats."""
    ps_out = run_cmd(
        "ps -eo pid,ppid,%cpu,rss,command | grep -i 'Runner\\.' | grep -v grep"
    )
    processes = []
    for line in ps_out.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            try:
                processes.append(
                    {
                        "pid": int(parts[0]),
                        "ppid": int(parts[1]),
                        "cpu": float(parts[2]),
                        "mem_kb": int(parts[3]),
                        "cmd": " ".join(parts[4:]),
                    }
                )
            except (ValueError, IndexError):
                pass
    return processes


def get_runner_log_status(runner_path):
    """Parse the latest runner log for job state."""
    diag_dir = runner_path / "_diag"
    if not diag_dir.exists():
        return "", "", ""

    runner_logs = sorted(diag_dir.glob("Runner_*.log"), key=os.path.getmtime)
    if not runner_logs:
        return "", "", ""

    latest_log = runner_logs[-1]
    current_job = ""
    last_time = ""
    state = "idle"

    try:
        with open(latest_log, "r") as f:
            lines = f.readlines()
            for line in lines[-200:]:
                if "Running job:" in line:
                    match = re.search(r"Running job: (.+)", line)
                    if match:
                        current_job = match.group(1).strip()
                    time_match = re.search(r"\[([\d-]+ [\d:]+Z)", line)
                    if time_match:
                        last_time = time_match.group(1)
                    state = "busy"
                elif "JobState: Online" in line:
                    state = "idle"
                    time_match = re.search(r"\[([\d-]+ [\d:]+Z)", line)
                    if time_match:
                        last_time = time_match.group(1)
                elif "Job completed" in line:
                    state = "idle"
    except Exception:
        pass

    return state, current_job, last_time


def get_gh_runner_status():
    """Get runner status from GitHub API."""
    out = run_cmd(
        f"gh api /repos/{REPO}/actions/runners --jq '.runners'"
    )
    if not out:
        return {}
    try:
        runners = json.loads(out)
        return {r["name"]: r for r in runners}
    except (json.JSONDecodeError, KeyError):
        return {}


def _fetch_jobs_for_run(run_id):
    """Fetch jobs for a single workflow run. Returns list of JobInfo for local runners."""
    out = run_cmd(
        f"gh api /repos/{REPO}/actions/runs/{run_id}/jobs "
        f"--jq '.jobs[] | {{name, runner_name, status, conclusion, started_at, completed_at}}'",
        timeout=15,
    )
    jobs = []
    if not out:
        return jobs
    for line in out.strip().split("\n"):
        try:
            d = json.loads(line)
            runner = d.get("runner_name") or ""
            if runner not in LOCAL_RUNNER_NAMES:
                continue
            ji = JobInfo(
                name=d.get("name", ""),
                runner_name=runner,
                status=d.get("status", ""),
                conclusion=d.get("conclusion", ""),
                started=d.get("started_at", ""),
                completed=d.get("completed_at", ""),
            )
            if ji.started and ji.completed:
                try:
                    s = datetime.fromisoformat(ji.started.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(ji.completed.replace("Z", "+00:00"))
                    delta = e - s
                    m = int(delta.total_seconds() // 60)
                    sec = int(delta.total_seconds() % 60)
                    ji.duration = f"{m}m {sec}s"
                except Exception:
                    ji.duration = "\u2014"
            elif ji.started:
                try:
                    s = datetime.fromisoformat(ji.started.replace("Z", "+00:00"))
                    elapsed = datetime.now(timezone.utc) - s
                    m = int(elapsed.total_seconds() // 60)
                    sec = int(elapsed.total_seconds() % 60)
                    ji.duration = f"{m}m {sec}s\u2026"
                except Exception:
                    ji.duration = "running"
            jobs.append(ji)
        except json.JSONDecodeError:
            pass
    return jobs


def get_workflow_runs(limit=12):
    """Get recent workflow runs that executed on local runners."""
    out = run_cmd(
        f"gh api '/repos/{REPO}/actions/runs?per_page={limit}' "
        f"--jq '.workflow_runs | .[] | {{id, name, status, conclusion, "
        f"head_branch, run_number, created_at, updated_at, run_started_at}}'"
    )
    if not out:
        return []

    raw_runs = []
    for line in out.strip().split("\n"):
        try:
            raw_runs.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    to_fetch = []
    for data in raw_runs:
        run_id = data.get("id", 0)
        is_completed = data.get("status") == "completed"
        if run_id in _job_cache:
            continue
        if is_completed or run_id not in _job_cache:
            to_fetch.append(data)

    fetched = {}
    if to_fetch:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(_fetch_jobs_for_run, d["id"]): d["id"]
                for d in to_fetch
            }
            for fut in as_completed(futures):
                run_id = futures[fut]
                try:
                    jobs = fut.result()
                    fetched[run_id] = jobs
                except Exception:
                    fetched[run_id] = []

    for data in raw_runs:
        run_id = data.get("id", 0)
        if run_id in fetched and data.get("status") == "completed":
            _job_cache[run_id] = fetched[run_id]

    runs = []
    for data in raw_runs:
        run_id = data.get("id", 0)
        jobs = _job_cache.get(run_id) or fetched.get(run_id, [])
        if not jobs:
            continue

        wr = WorkflowRun(
            id=run_id,
            name=data.get("name", ""),
            status=data.get("status", ""),
            conclusion=data.get("conclusion", ""),
            branch=data.get("head_branch", ""),
            started=data.get("run_started_at", ""),
            run_number=data.get("run_number", 0),
            jobs=jobs,
        )
        if data.get("run_started_at") and data.get("updated_at"):
            try:
                start = datetime.fromisoformat(
                    data["run_started_at"].replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    data["updated_at"].replace("Z", "+00:00")
                )
                delta = end - start
                mins = int(delta.total_seconds() // 60)
                secs = int(delta.total_seconds() % 60)
                wr.duration = f"{mins}m {secs}s"
            except Exception:
                wr.duration = "\u2014"
        runs.append(wr)
    return runs


def get_system_stats():
    """Get system-level CPU, memory, and process stats."""
    stats = SystemStats()

    try:
        load = os.getloadavg()
        stats.load_avg = load
    except Exception:
        pass

    vm_out = run_cmd("vm_stat")
    if vm_out:
        page_size = 16384
        ps_match = re.search(r"page size of (\d+)", vm_out)
        if ps_match:
            page_size = int(ps_match.group(1))

        free = 0
        active = 0
        inactive = 0
        speculative = 0
        wired = 0
        compressed = 0
        for line in vm_out.splitlines():
            if "Pages free:" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    free = int(m.group(1))
            elif "Pages active:" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    active = int(m.group(1))
            elif "Pages inactive:" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    inactive = int(m.group(1))
            elif "Pages speculative:" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    speculative = int(m.group(1))
            elif "Pages wired" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    wired = int(m.group(1))
            elif "Pages occupied by compressor:" in line:
                m = re.search(r"(\d+)", line.split(":")[1])
                if m:
                    compressed = int(m.group(1))

        total_pages = free + active + inactive + speculative + wired + compressed
        used_pages = active + wired + compressed
        stats.mem_total_gb = (total_pages * page_size) / (1024**3)
        stats.mem_used_gb = (used_pages * page_size) / (1024**3)
        if stats.mem_total_gb > 0:
            stats.mem_percent = (stats.mem_used_gb / stats.mem_total_gb) * 100

    mem_total = run_cmd("sysctl -n hw.memsize")
    if mem_total:
        try:
            stats.mem_total_gb = int(mem_total) / (1024**3)
        except ValueError:
            pass

    xcode_out = run_cmd("ps aux | grep -c '[x]codebuild\\|[X]code'")
    sim_out = run_cmd("ps aux | grep -c '[S]imulator\\|[s]imctl'")
    try:
        stats.xcode_processes = int(xcode_out)
    except ValueError:
        pass
    try:
        stats.simulator_processes = int(sim_out)
    except ValueError:
        pass

    cpu_out = run_cmd("ps -A -o %cpu | awk '{s+=$1} END {print s}'")
    try:
        stats.cpu_percent = float(cpu_out)
    except ValueError:
        pass

    return stats


# -- Simulator / Test Detection (from simdash) ---------------------------------


def get_booted_sims():
    """Return list of booted simulator names and UDIDs.
    Also detects headless parallel test clones from XCTestDevices."""
    sims = []

    # Standard booted simulators
    out = run_cmd("xcrun simctl list devices booted -j", timeout=3)
    if out:
        try:
            data = json.loads(out)
            for runtime, devices in data.get("devices", {}).items():
                os_match = re.search(r"iOS[- ](\d+\.\d+)", runtime)
                os_ver = os_match.group(1) if os_match else "?"
                for dev in devices:
                    sims.append(
                        {
                            "name": dev.get("name", "?"),
                            "udid": dev.get("udid", "")[:8],
                            "os": os_ver,
                            "state": "Booted",
                        }
                    )
        except Exception:
            pass

    # Detect parallel test clones from XCTestDevices
    # xcodebuild creates "Clone N of <device>" entries with active Runner processes
    xctest_devices = Path.home() / "Library/Developer/XCTestDevices"
    if xctest_devices.exists():
        for device_dir in xctest_devices.iterdir():
            if not device_dir.is_dir():
                continue
            plist = device_dir / "device.plist"
            if not plist.exists():
                continue
            udid = device_dir.name
            # Check if there's an active test runner for this clone
            runner_pid = run_cmd(
                f"ps aux | grep '{udid}' | grep 'Runner' | grep -v grep | awk '{{print $2}}' | head -1",
                timeout=2,
            )
            # Read clone name from plist
            name = run_cmd(f"/usr/libexec/PlistBuddy -c 'Print :name' '{plist}' 2>/dev/null", timeout=2)
            if not name:
                name = f"Clone ({udid[:8]})"
            runtime = run_cmd(f"/usr/libexec/PlistBuddy -c 'Print :runtime' '{plist}' 2>/dev/null", timeout=2)
            os_match = re.search(r"iOS[- .]([\d.]+)", runtime.replace("-", ".")) if runtime else None
            os_ver = os_match.group(1) if os_match else "?"
            sims.append(
                {
                    "name": name,
                    "udid": udid[:8],
                    "os": os_ver,
                    "state": f"PID:{runner_pid}" if runner_pid else "Idle",
                }
            )

    # Detect xcodebuild test destinations (fallback if no clones found)
    if not any(s["state"].startswith("PID:") for s in sims):
        xc_out = run_cmd("ps aux | grep 'xcodebuild.*test' | grep -v grep")
        for line in xc_out.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            pid = parts[1]
            full_cmd = run_cmd(f"ps -p {pid} -o args=", timeout=3)
            dest_match = re.search(r"name=([^,\s\"]+)", full_cmd)
            dest_name = dest_match.group(1) if dest_match else None
            workers_match = re.search(r"-parallel-testing-worker-count\s+(\d+)", full_cmd)
            workers = workers_match.group(1) if workers_match else "1"
            if dest_name and not any(s["name"] == dest_name for s in sims):
                sims.append(
                    {
                        "name": dest_name,
                        "udid": f"PID:{pid}",
                        "os": f"{workers}w",
                        "state": "Testing",
                    }
                )

    return sims


def get_test_suites():
    """Return list of active xcodebuild test processes with live progress."""
    out = run_cmd("ps aux | grep 'xcodebuild.*test' | grep -v grep")
    suites = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        pid = parts[1]
        elapsed = run_cmd(f"ps -p {pid} -o etime=", timeout=3).strip()
        full_cmd = run_cmd(f"ps -p {pid} -o args=", timeout=3)

        suite_match = re.search(r"-only-testing:(\S+)", full_cmd)
        suite = suite_match.group(1) if suite_match else "all"
        dest_match = re.search(r"name=([^,\s\"]+)", full_cmd)
        dest = dest_match.group(1) if dest_match else "?"

        result_path = ""
        rp_match = re.search(r"-resultBundlePath\s+(\S+)", full_cmd)
        if rp_match:
            result_path = rp_match.group(1)

        passed = failed = 0
        last_test = ""
        if result_path and os.path.isdir(result_path):
            session_logs = glob.glob(
                os.path.join(result_path, "**", "Session-*.log"), recursive=True
            )
            session_logs.sort(
                key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0,
                reverse=True,
            )
            if session_logs:
                try:
                    with open(session_logs[0], "r", errors="replace") as f:
                        for sline in f:
                            if "Test Case" in sline and "passed" in sline:
                                passed += 1
                                m = re.search(r"'(.+?)'", sline)
                                if m:
                                    last_test = m.group(1).split(".")[-1]
                            elif "Test Case" in sline and "failed" in sline:
                                failed += 1
                                m = re.search(r"'(.+?)'", sline)
                                if m:
                                    last_test = m.group(1).split(".")[-1]
                except Exception:
                    pass

        # Fallback: read from tee'd log files
        if passed == 0 and failed == 0:
            for log_pattern in [
                "/tmp/uitest*.log",
                "/Users/*/Documents/Projects/github-runner*/_work/_temp/xcodebuild.log",
            ]:
                for log_path in glob.glob(log_pattern):
                    try:
                        raw = subprocess.run(
                            ["strings", log_path],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        ).stdout
                        for sline in raw.splitlines():
                            if "Test Case" in sline and "passed" in sline:
                                passed += 1
                            elif "Test Case" in sline and "failed" in sline:
                                failed += 1
                                m = re.search(r"'(.+?)'", sline)
                                if m:
                                    last_test = m.group(1).split(".")[-1]
                    except Exception:
                        pass
                    if passed > 0:
                        break

        suites.append(
            {
                "pid": pid,
                "elapsed": elapsed,
                "suite": suite,
                "dest": dest,
                "passed": passed,
                "failed": failed,
                "last_test": last_test,
            }
        )
    return suites


def collect_all_data():
    """Collect all dashboard data."""
    processes = get_runner_processes()
    gh_status = get_gh_runner_status()
    sys_stats = get_system_stats()
    runs = get_workflow_runs()

    runner_statuses = []
    for cfg in RUNNERS:
        rs = RunnerStatus(
            name=cfg["name"],
            label=cfg["label"],
            job_type=cfg["job_type"],
            path=cfg["path"],
        )

        runner_path_str = str(cfg["path"])
        for proc in processes:
            if runner_path_str in proc["cmd"]:
                if "Runner.Listener" in proc["cmd"]:
                    rs.pid = proc["pid"]
                    rs.cpu_percent = proc["cpu"]
                    rs.mem_mb = proc["mem_kb"] / 1024
                    rs.online = True
                elif "Runner.Worker" in proc["cmd"]:
                    rs.worker_pids.append(proc["pid"])
                    rs.worker_cpu += proc["cpu"]
                    rs.worker_mem += proc["mem_kb"] / 1024

        if cfg["name"] in gh_status:
            api_data = gh_status[cfg["name"]]
            rs.online = api_data.get("status") == "online"
            rs.busy = api_data.get("busy", False)

        state, job, last_time = get_runner_log_status(cfg["path"])
        if state == "busy":
            rs.busy = True
            rs.current_job = job
        rs.last_job_time = last_time

        if rs.pid:
            etime = run_cmd(f"ps -o etime= -p {rs.pid}")
            rs.uptime = etime.strip()

        runner_statuses.append(rs)

    return runner_statuses, runs, sys_stats


# -- Rendering -----------------------------------------------------------------

# Color pair IDs
C_NORMAL = 0
C_HEADER = 1
C_GREEN = 2
C_RED = 3
C_YELLOW = 4
C_CYAN = 5
C_DIM = 6
C_BLUE = 7
C_MAGENTA = 8
C_STATUS_BAR = 9
C_BUSY_BG = 10


def init_colors():
    """Initialize color pairs."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_BLUE, curses.COLOR_BLUE, -1)
    curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_STATUS_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_BUSY_BG, curses.COLOR_BLACK, curses.COLOR_YELLOW)


def safe_addstr(win, y, x, text, attr=0):
    """Write text to window, clipping to avoid curses errors."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    available = max_x - x - 1
    if available <= 0:
        return
    win.addnstr(y, x, text, available, attr)


def draw_box(win, y, x, h, w, title="", color=C_NORMAL):
    """Draw a box with optional title."""
    max_y, max_x = win.getmaxyx()
    if y + h > max_y or x + w > max_x:
        h = min(h, max_y - y)
        w = min(w, max_x - x)
    if h < 2 or w < 2:
        return

    attr = curses.color_pair(color)

    safe_addstr(win, y, x, "\u250c" + "\u2500" * (w - 2) + "\u2510", attr)
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x, "\u2502", attr)
        safe_addstr(win, y + i, x + w - 1, "\u2502", attr)
    safe_addstr(win, y + h - 1, x, "\u2514" + "\u2500" * (w - 2) + "\u2518", attr)

    if title:
        title_str = f" {title} "
        tx = x + 2
        safe_addstr(win, y, tx, title_str, attr | curses.A_BOLD)


def draw_bar(win, y, x, width, pct, color=C_GREEN):
    """Draw a percentage bar."""
    filled = int((pct / 100) * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    c = color
    if pct > 80:
        c = C_RED
    elif pct > 60:
        c = C_YELLOW
    safe_addstr(win, y, x, bar, curses.color_pair(c))


def format_time_ago(iso_str):
    """Convert ISO timestamp to 'Xm ago' format."""
    if not iso_str:
        return "\u2014"
    try:
        t = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - t
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs // 60}m ago"
        elif secs < 86400:
            return f"{secs // 3600}h ago"
        else:
            return f"{secs // 86400}d ago"
    except Exception:
        return "\u2014"


def draw_header(win, max_x):
    """Draw the header bar."""
    header = f" GitHub Runner Dashboard \u2014 {REPO} "
    pad = " " * max(0, max_x - len(header) - 1)
    safe_addstr(win, 0, 0, header + pad, curses.color_pair(C_HEADER) | curses.A_BOLD)


def draw_runner_panel(win, y, x, w, runner, index):
    """Draw a single runner status panel."""
    h = 10
    draw_box(win, y, x, h, w, f"Runner {index + 1}: {runner.name}")

    col = x + 2
    row = y + 1

    # Status indicator
    if runner.busy:
        status = "\u25cf BUSY"
        status_color = C_YELLOW
    elif runner.online:
        status = "\u25cf ONLINE"
        status_color = C_GREEN
    else:
        status = "\u25cf OFFLINE"
        status_color = C_RED

    safe_addstr(win, row, col, "Status: ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 8, status, curses.color_pair(status_color) | curses.A_BOLD
    )

    safe_addstr(win, row, col + 22, "Label: ", curses.color_pair(C_DIM))
    safe_addstr(win, row, col + 29, runner.label, curses.color_pair(C_CYAN))
    row += 1

    # Current job
    safe_addstr(win, row, col, "Job:    ", curses.color_pair(C_DIM))
    if runner.busy and runner.current_job:
        safe_addstr(
            win,
            row,
            col + 8,
            runner.current_job,
            curses.color_pair(C_YELLOW) | curses.A_BOLD,
        )
    elif runner.current_job:
        safe_addstr(
            win, row, col + 8, f"Last: {runner.current_job}", curses.color_pair(C_DIM)
        )
    else:
        safe_addstr(win, row, col + 8, "idle", curses.color_pair(C_DIM))

    safe_addstr(win, row, col + 22, "Type: ", curses.color_pair(C_DIM))
    safe_addstr(win, row, col + 28, runner.job_type, curses.color_pair(C_CYAN))
    row += 1

    # PID and uptime
    safe_addstr(win, row, col, "PID:    ", curses.color_pair(C_DIM))
    safe_addstr(
        win,
        row,
        col + 8,
        str(runner.pid) if runner.pid else "\u2014",
        curses.color_pair(C_NORMAL),
    )
    safe_addstr(win, row, col + 22, "Uptime: ", curses.color_pair(C_DIM))
    safe_addstr(
        win,
        row,
        col + 30,
        runner.uptime if runner.uptime else "\u2014",
        curses.color_pair(C_NORMAL),
    )
    row += 1

    # CPU/mem for listener
    safe_addstr(win, row, col, "Listener CPU: ", curses.color_pair(C_DIM))
    cpu_str = f"{runner.cpu_percent:.1f}%"
    cpu_color = (
        C_GREEN
        if runner.cpu_percent < 50
        else (C_YELLOW if runner.cpu_percent < 80 else C_RED)
    )
    safe_addstr(win, row, col + 14, cpu_str, curses.color_pair(cpu_color))
    safe_addstr(
        win, row, col + 22, f"Mem: {runner.mem_mb:.0f} MB", curses.color_pair(C_DIM)
    )
    row += 1

    # Worker processes
    safe_addstr(win, row, col, "Workers:  ", curses.color_pair(C_DIM))
    if runner.worker_pids:
        worker_info = f"{len(runner.worker_pids)} active"
        safe_addstr(win, row, col + 10, worker_info, curses.color_pair(C_GREEN))
        safe_addstr(
            win,
            row,
            col + 22,
            f"CPU: {runner.worker_cpu:.1f}%  Mem: {runner.worker_mem:.0f} MB",
            curses.color_pair(C_DIM),
        )
    else:
        safe_addstr(win, row, col + 10, "none", curses.color_pair(C_DIM))
    row += 1

    # Last activity
    safe_addstr(win, row, col, "Last activity: ", curses.color_pair(C_DIM))
    safe_addstr(
        win,
        row,
        col + 15,
        runner.last_job_time if runner.last_job_time else "\u2014",
        curses.color_pair(C_DIM),
    )

    return h


def _match_sim_to_dest(sim_name, dest):
    """Check if a simulator clone belongs to a test destination."""
    # "Clone 2 of CI-iPhone" matches dest "CI-iPhone"
    # "Clone 1 of CI-iPhone-2" matches dest "CI-iPhone-2"
    if f"of {dest}" in sim_name:
        return True
    if sim_name == dest:
        return True
    return False


def draw_sim_and_tests_panel(win, y, x, w, sims, suites, wide=False):
    """Draw Simulators & Tests panel — one column per clone, all visible at once."""
    if not sims and not suites:
        h = 4
        draw_box(win, y, x, h, w, "Simulators & Tests")
        safe_addstr(win, y + 1, x + 2, "(no simulators or tests)", curses.color_pair(C_DIM))
        return h

    # Each clone gets its own column
    # Match each clone to its test suite via destination name
    # "Clone 2 of CI-iPhone-2" → dest "CI-iPhone-2"
    clone_data = []
    for s in sims:
        match = re.search(r"of (.+)", s["name"])
        parent_dest = match.group(1) if match else s["name"]
        clone_suite = next((su for su in suites if su.get("dest") == parent_dest), None)
        clone_data.append((s, clone_suite))

    num_cols = max(len(clone_data), 1)
    col_w = max((w - 2) // num_cols, 15)  # minimum 15 chars per column

    # Fixed height: icon + name + separator + suite status + last test = 5 rows
    content_rows = 5
    h = content_rows + 2  # borders
    draw_box(win, y, x, h, w, "Simulators & Tests")

    for i, (sim, suite) in enumerate(clone_data):
        cx = x + 1 + i * col_w
        avail = col_w - 2
        row = y + 1

        # Row 1: status icon + clone name
        if sim["state"].startswith("PID:"):
            icon, icon_color = "▶", C_YELLOW
        elif sim["state"] == "Idle":
            icon, icon_color = "○", C_DIM
        elif sim["state"] == "Booted":
            icon, icon_color = "●", C_GREEN
        else:
            icon, icon_color = "○", C_CYAN

        clone_num = re.search(r"Clone (\d+)", sim["name"])
        parent = re.search(r"of (.+)", sim["name"])
        if clone_num and parent:
            label = f"C{clone_num.group(1)} {parent.group(1)}"
        else:
            label = sim["name"]
        safe_addstr(win, row, cx, icon, curses.color_pair(icon_color) | curses.A_BOLD)
        safe_addstr(win, row, cx + 2, label[:avail - 2], curses.color_pair(C_NORMAL))
        row += 1

        # Row 2: iOS version + PID
        state_info = f"{sim['os']}"
        if sim["state"].startswith("PID:"):
            state_info += f"  {sim['state']}"
        safe_addstr(win, row, cx + 1, state_info[:avail], curses.color_pair(C_DIM))
        row += 1

        # Row 3: separator
        safe_addstr(win, row, cx, "─" * avail, curses.color_pair(C_DIM))
        row += 1

        # Row 4: test status
        if suite:
            total = suite["passed"] + suite["failed"]
            if suite["failed"] > 0:
                tag = f"{suite['passed']}P {suite['failed']}F/{total}"
                attr = C_RED
            else:
                tag = f"{suite['passed']}P/{total}"
                attr = C_GREEN
            safe_addstr(win, row, cx + 1, tag[:avail], curses.color_pair(attr) | curses.A_BOLD)
        else:
            safe_addstr(win, row, cx + 1, "idle", curses.color_pair(C_DIM))
        row += 1

        # Row 5: last test (truncated)
        if suite and suite.get("last_test"):
            test_name = suite["last_test"]
            # Extract just the method name
            if "]" in test_name:
                test_name = test_name.split()[-1].rstrip("]")
            safe_addstr(win, row, cx + 1, f"▸{test_name}"[:avail], curses.color_pair(C_DIM))
        row += 1

        # Column separator
        if i < num_cols - 1:
            sep_x = cx + col_w - 1
            for sy in range(y + 1, y + h - 1):
                safe_addstr(win, sy, sep_x, "│", curses.color_pair(C_DIM))

    return h


def draw_system_panel(win, y, x, w, stats):
    """Draw system stats panel."""
    h = 8
    draw_box(win, y, x, h, w, "System Resources")

    col = x + 2
    row = y + 1

    # Load average
    safe_addstr(win, row, col, "Load Avg: ", curses.color_pair(C_DIM))
    load_str = f"{stats.load_avg[0]:.2f}  {stats.load_avg[1]:.2f}  {stats.load_avg[2]:.2f}"
    load_color = (
        C_GREEN
        if stats.load_avg[0] < 4
        else (C_YELLOW if stats.load_avg[0] < 8 else C_RED)
    )
    safe_addstr(win, row, col + 10, load_str, curses.color_pair(load_color))
    safe_addstr(win, row, col + 35, "(1m  5m  15m)", curses.color_pair(C_DIM))
    row += 1

    # Total CPU
    safe_addstr(win, row, col, "Total CPU: ", curses.color_pair(C_DIM))
    cpu_pct = min(stats.cpu_percent, 999.9)
    safe_addstr(win, row, col + 11, f"{cpu_pct:.1f}%", curses.color_pair(C_NORMAL))
    bar_x = col + 20
    bar_w = w - 24
    if bar_w > 5:
        core_count_str = run_cmd("sysctl -n hw.ncpu")
        try:
            cores = int(core_count_str)
        except ValueError:
            cores = 8
        bar_pct = min((stats.cpu_percent / (cores * 100)) * 100, 100)
        draw_bar(win, row, bar_x, bar_w, bar_pct)
    row += 1

    # Memory
    safe_addstr(win, row, col, "Memory:    ", curses.color_pair(C_DIM))
    safe_addstr(
        win,
        row,
        col + 11,
        f"{stats.mem_used_gb:.1f} / {stats.mem_total_gb:.0f} GB ({stats.mem_percent:.0f}%)",
        curses.color_pair(C_NORMAL),
    )
    if bar_w > 5:
        draw_bar(win, row, bar_x, bar_w, stats.mem_percent)
    row += 1

    # Xcode / Simulator
    safe_addstr(win, row, col, "Xcode procs: ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 13, str(stats.xcode_processes), curses.color_pair(C_CYAN)
    )
    safe_addstr(win, row, col + 20, "Simulator procs: ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 37, str(stats.simulator_processes), curses.color_pair(C_CYAN)
    )
    row += 1

    # Cores
    cores_str = run_cmd("sysctl -n hw.ncpu")
    chip_str = run_cmd("sysctl -n machdep.cpu.brand_string")
    safe_addstr(win, row, col, "Chip: ", curses.color_pair(C_DIM))
    chip_display = chip_str if chip_str else "\u2014"
    safe_addstr(
        win,
        row,
        col + 6,
        f"{chip_display} ({cores_str} cores)",
        curses.color_pair(C_NORMAL),
    )

    return h


def cancel_workflow_run(run_id):
    """Cancel a workflow run via GitHub API."""
    result = run_cmd(f"gh run cancel {run_id} -R {REPO}", timeout=15)
    return result


def draw_workflow_panel(win, y, x, w, runs, max_rows=20, selected_run_idx=-1):
    """Draw recent workflow runs panel with per-job detail."""
    total_job_rows = sum(max(len(r.jobs), 1) for r in runs)
    content_rows = min(total_job_rows, max_rows)
    h = content_rows + 3  # border top + header + border bottom
    draw_box(win, y, x, h, w, "Runs on Local Runners")

    col = x + 2
    row = y + 1

    # Header
    safe_addstr(win, row, col, "ST", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(win, row, col + 4, "#", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(win, row, col + 10, "Branch", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(win, row, col + 34, "Job", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(
        win, row, col + 47, "Runner", curses.color_pair(C_DIM) | curses.A_BOLD
    )
    safe_addstr(
        win, row, col + 61, "Duration", curses.color_pair(C_DIM) | curses.A_BOLD
    )
    safe_addstr(win, row, col + 72, "When", curses.color_pair(C_DIM) | curses.A_BOLD)
    row += 1

    rows_used = 0
    for run_idx, run in enumerate(runs):
        if rows_used >= content_rows:
            break

        is_selected = run_idx == selected_run_idx

        first_job = True
        for job in run.jobs:
            if rows_used >= content_rows:
                break

            if is_selected:
                max_y_win, max_x_win = win.getmaxyx()
                fill_w = min(w - 3, max_x_win - col - 1)
                if fill_w > 0:
                    safe_addstr(
                        win, row, col - 1, " " * fill_w, curses.A_REVERSE
                    )

            if job.status == "in_progress":
                icon = "\u27f3"
                icon_color = C_YELLOW
            elif job.status == "queued":
                icon = "\u25cc"
                icon_color = C_DIM
            elif job.conclusion == "success":
                icon = "\u2713"
                icon_color = C_GREEN
            elif job.conclusion == "failure":
                icon = "\u2717"
                icon_color = C_RED
            elif job.conclusion == "cancelled":
                icon = "\u2298"
                icon_color = C_DIM
            elif job.conclusion == "skipped":
                icon = "\u2013"
                icon_color = C_DIM
            else:
                icon = "?"
                icon_color = C_DIM

            row_attr = curses.A_REVERSE if is_selected else 0

            safe_addstr(
                win,
                row,
                col,
                icon,
                curses.color_pair(icon_color) | curses.A_BOLD | row_attr,
            )

            if first_job:
                if is_selected:
                    safe_addstr(
                        win,
                        row,
                        col + 2,
                        "\u25b8",
                        curses.color_pair(C_CYAN) | curses.A_BOLD | row_attr,
                    )
                safe_addstr(
                    win,
                    row,
                    col + 4,
                    f"#{run.run_number}",
                    curses.color_pair(C_DIM) | row_attr,
                )
                branch_display = run.branch[:22] if run.branch else "\u2014"
                safe_addstr(
                    win,
                    row,
                    col + 10,
                    branch_display,
                    curses.color_pair(C_CYAN) | row_attr,
                )
                first_job = False
            else:
                safe_addstr(
                    win,
                    row,
                    col + 4,
                    "  \u2514",
                    curses.color_pair(C_DIM) | row_attr,
                )

            job_display = job.name[:12] if job.name else "\u2014"
            safe_addstr(
                win,
                row,
                col + 34,
                job_display,
                curses.color_pair(C_NORMAL) | row_attr,
            )

            runner_short = job.runner_name.replace("manas-mac", "mac")
            safe_addstr(
                win,
                row,
                col + 47,
                runner_short,
                curses.color_pair(C_MAGENTA) | row_attr,
            )

            safe_addstr(
                win,
                row,
                col + 61,
                job.duration or "\u2014",
                curses.color_pair(C_DIM) | row_attr,
            )

            safe_addstr(
                win,
                row,
                col + 72,
                format_time_ago(job.started),
                curses.color_pair(C_DIM) | row_attr,
            )

            row += 1
            rows_used += 1

    return h


def draw_log_pane(win, y, x, w, h, log_lines):
    """Draw the log pane at the bottom of the screen."""
    max_y, max_x = win.getmaxyx()

    # Draw separator line
    sep_text = "\u2500" * (w - 2)
    safe_addstr(win, y, x, " " + sep_text, curses.color_pair(C_DIM))
    safe_addstr(win, y, x + 2, " Logs (l=toggle, Tab=focus) ", curses.color_pair(C_CYAN) | curses.A_BOLD)

    log_row = y + 1
    if not log_lines:
        safe_addstr(
            win, log_row, x + 2, "(waiting for logs...)", curses.color_pair(C_DIM)
        )
        return

    for ts, source, text in log_lines:
        if log_row >= max_y - 1:  # leave room for footer
            break

        # Color based on content
        if "failed" in text.lower() or "error" in text.lower() or "crash" in text.lower():
            line_attr = curses.color_pair(C_RED)
        elif "passed" in text.lower():
            line_attr = curses.color_pair(C_GREEN)
        elif "Executed" in text:
            line_attr = curses.color_pair(C_CYAN) | curses.A_BOLD
        else:
            line_attr = curses.color_pair(C_DIM)

        src_tag = f"[{source}]"
        try:
            safe_addstr(win, log_row, x + 1, ts, curses.color_pair(C_DIM))
            safe_addstr(win, log_row, x + 10, src_tag, curses.color_pair(C_MAGENTA))
            content_start = x + 10 + len(src_tag) + 1
            remaining = w - (content_start - x) - 1
            if remaining > 0:
                safe_addstr(win, log_row, content_start, text[:remaining], line_attr)
        except curses.error:
            pass
        log_row += 1


def draw_footer(win, max_y, max_x, last_refresh, status_msg="", log_visible=True, focus="runs"):
    """Draw the status bar at the bottom."""
    now = datetime.now().strftime("%H:%M:%S")
    focus_str = f"focus:{focus}"
    log_str = "logs:ON" if log_visible else "logs:OFF"
    if status_msg:
        footer = f" {status_msg} | {now} "
    else:
        footer = (
            f" {last_refresh} | {REFRESH_INTERVAL}s | "
            f"[q] Quit  [r] Refresh  [\u2191\u2193] Select  [c] Cancel  "
            f"[l] Logs  [Tab] Focus  | {log_str} {focus_str} | {now} "
        )
    pad = " " * max(0, max_x - len(footer) - 1)
    msg_color = C_STATUS_BAR
    safe_addstr(win, max_y - 1, 0, footer + pad, curses.color_pair(msg_color))


# -- Main Loop -----------------------------------------------------------------


def main(stdscr):
    """Main curses loop."""
    curses.curs_set(0)
    init_colors()
    stdscr.timeout(500)
    stdscr.keypad(True)

    last_data = None
    last_fetch = 0
    fetch_count = 0
    selected_run_idx = -1
    status_msg = ""
    status_msg_expire = 0

    # Log pane state
    log_visible = True
    log_scroll_offset = 0
    focus = "runs"  # "runs" or "logs"

    # Start log collector
    log_collector = LogCollector()
    log_collector.start()

    # Sim / test data (fetched alongside main data)
    sims_data = []
    suites_data = []

    try:
        while True:
            now = time.time()
            max_y, max_x = stdscr.getmaxyx()

            # Clear expired status message
            if status_msg and now >= status_msg_expire:
                status_msg = ""

            # Check minimum terminal size
            if max_y < 20 or max_x < 60:
                stdscr.clear()
                safe_addstr(
                    stdscr,
                    max_y // 2,
                    max_x // 2 - 15,
                    "Terminal too small (need 60x20)",
                    curses.color_pair(C_RED),
                )
                stdscr.refresh()
                key = stdscr.getch()
                if key == ord("q"):
                    break
                continue

            # Handle input
            force_refresh = False
            key = stdscr.getch()
            if key == ord("q"):
                break
            elif key == ord("r"):
                force_refresh = True
            elif key == ord("l") or key == ord("L"):
                log_visible = not log_visible
            elif key == 9:  # Tab
                focus = "logs" if focus == "runs" else "runs"
            elif key == curses.KEY_UP or key == ord("k"):
                if focus == "runs":
                    if last_data and last_data[1]:
                        num_runs = len(last_data[1])
                        if selected_run_idx <= 0:
                            selected_run_idx = num_runs - 1
                        else:
                            selected_run_idx -= 1
                else:
                    log_scroll_offset += 3
            elif key == curses.KEY_DOWN or key == ord("j"):
                if focus == "runs":
                    if last_data and last_data[1]:
                        num_runs = len(last_data[1])
                        if selected_run_idx >= num_runs - 1:
                            selected_run_idx = 0
                        else:
                            selected_run_idx += 1
                else:
                    log_scroll_offset = max(0, log_scroll_offset - 3)
            elif key == 27:  # Escape
                selected_run_idx = -1
                focus = "runs"
            elif key == ord("c") or key == ord("C"):
                if (
                    last_data
                    and last_data[1]
                    and 0 <= selected_run_idx < len(last_data[1])
                ):
                    sel_run = last_data[1][selected_run_idx]
                    if sel_run.status in ("in_progress", "queued"):
                        status_msg = f"Cancelling run #{sel_run.run_number}..."
                        status_msg_expire = now + 3
                        stdscr.erase()
                        draw_footer(stdscr, max_y, max_x, "", status_msg, log_visible, focus)
                        stdscr.refresh()
                        cancel_workflow_run(sel_run.id)
                        status_msg = f"\u2713 Cancelled run #{sel_run.run_number} ({sel_run.branch})"
                        status_msg_expire = now + 5
                        force_refresh = True
                        _job_cache.pop(sel_run.id, None)
                    else:
                        status_msg = f"Run #{sel_run.run_number} is not active (status: {sel_run.status})"
                        status_msg_expire = now + 3
                elif selected_run_idx == -1:
                    status_msg = "Use \u2191\u2193 to select a run first"
                    status_msg_expire = now + 3

            if force_refresh or now - last_fetch >= REFRESH_INTERVAL:
                try:
                    runners, runs, sys_stats = collect_all_data()
                    last_data = (runners, runs, sys_stats)
                    last_fetch = now
                    fetch_count += 1
                    # Also fetch sim/test data
                    sims_data = get_booted_sims()
                    suites_data = get_test_suites()
                    # Clamp selection index
                    if selected_run_idx >= len(runs):
                        selected_run_idx = len(runs) - 1 if runs else -1
                except Exception:
                    if last_data is None:
                        last_data = ([], [], SystemStats())

            if last_data is None:
                stdscr.clear()
                safe_addstr(
                    stdscr,
                    max_y // 2,
                    max_x // 2 - 8,
                    "Loading...",
                    curses.color_pair(C_YELLOW),
                )
                stdscr.refresh()
                continue

            runners, runs, sys_stats = last_data

            # -- Draw everything --
            stdscr.erase()

            # Calculate log pane height
            log_pane_height = 0
            if log_visible:
                log_pane_height = max(max_y // 3, 6)

            # Available height for dashboard content (header + content + footer)
            dash_available = max_y - 2 - log_pane_height  # -1 header, -1 footer

            # Header
            draw_header(stdscr, max_x)

            current_y = 2
            panel_w = max_x - 2

            # Runner panels side by side if wide enough
            if max_x >= 110:
                half_w = (max_x - 3) // 2
                rh = 10
                for i, runner in enumerate(runners):
                    rx = 1 + i * (half_w + 1)
                    rh = draw_runner_panel(stdscr, current_y, rx, half_w, runner, i)
                current_y += rh + 1
            else:
                for i, runner in enumerate(runners):
                    rh = draw_runner_panel(stdscr, current_y, 1, panel_w, runner, i)
                    current_y += rh + 1

            # Simulators + Active Tests panel (compact)
            if current_y + 5 < max_y - log_pane_height - 2:
                sth = draw_sim_and_tests_panel(
                    stdscr, current_y, 1, panel_w, sims_data, suites_data,
                    wide=(max_x >= 110)
                )
                current_y += sth + 1

            # System stats
            if current_y + 8 < max_y - log_pane_height - 2:
                sh = draw_system_panel(stdscr, current_y, 1, panel_w, sys_stats)
                current_y += sh + 1

            # Workflow runs
            workflow_bottom_limit = max_y - log_pane_height - 2
            if current_y + 5 < workflow_bottom_limit:
                remaining = workflow_bottom_limit - current_y
                max_job_rows = remaining - 3
                if max_job_rows > 0:
                    wh = draw_workflow_panel(
                        stdscr,
                        current_y,
                        1,
                        panel_w,
                        runs,
                        max_rows=max_job_rows,
                        selected_run_idx=selected_run_idx,
                    )
                    current_y += wh + 1

            # Log pane (bottom portion)
            if log_visible and log_pane_height > 1:
                log_pane_y = max_y - log_pane_height - 1  # -1 for footer
                visible_log_count = log_pane_height - 1  # -1 for separator
                all_log_lines = log_collector.get_lines(visible_log_count + log_scroll_offset)
                if log_scroll_offset > 0 and log_scroll_offset < len(all_log_lines):
                    all_log_lines = all_log_lines[:-log_scroll_offset]
                visible_logs = all_log_lines[-visible_log_count:]
                draw_log_pane(stdscr, log_pane_y, 0, max_x, log_pane_height, visible_logs)

            # Footer
            refresh_str = datetime.now().strftime("%H:%M:%S")
            draw_footer(stdscr, max_y, max_x, refresh_str, status_msg, log_visible, focus)

            stdscr.refresh()
    finally:
        log_collector.stop()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        print("Dashboard closed.")
