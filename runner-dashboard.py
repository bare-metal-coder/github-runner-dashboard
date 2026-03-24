#!/usr/bin/env python3
"""
GitHub Actions Runner Dashboard
Visual monitoring for self-hosted runners on this Mac.
Shows runner status, active jobs, CPU/memory, and recent workflow runs.

Usage: python3 tools/runner-dashboard.py
       Press 'q' to quit, 'r' to force refresh
"""

import curses
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

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

# ── Data Models ────────────────────────────────────────────────────────────────


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


# ── Data Collection ────────────────────────────────────────────────────────────


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

    # Find latest Runner log
    runner_logs = sorted(diag_dir.glob("Runner_*.log"), key=os.path.getmtime)
    if not runner_logs:
        return "", "", ""

    latest_log = runner_logs[-1]
    current_job = ""
    last_time = ""
    state = "idle"

    # Read last 200 lines for recent state
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
            # Duration
            if ji.started and ji.completed:
                try:
                    s = datetime.fromisoformat(ji.started.replace("Z", "+00:00"))
                    e = datetime.fromisoformat(ji.completed.replace("Z", "+00:00"))
                    delta = e - s
                    m = int(delta.total_seconds() // 60)
                    sec = int(delta.total_seconds() % 60)
                    ji.duration = f"{m}m {sec}s"
                except Exception:
                    ji.duration = "—"
            elif ji.started:
                # In progress — show elapsed
                try:
                    s = datetime.fromisoformat(ji.started.replace("Z", "+00:00"))
                    elapsed = datetime.now(timezone.utc) - s
                    m = int(elapsed.total_seconds() // 60)
                    sec = int(elapsed.total_seconds() % 60)
                    ji.duration = f"{m}m {sec}s…"
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

    # Parse run metadata
    raw_runs = []
    for line in out.strip().split("\n"):
        try:
            raw_runs.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Identify which runs need job fetches (not in cache)
    to_fetch = []
    for data in raw_runs:
        run_id = data.get("id", 0)
        is_completed = data.get("status") == "completed"
        if run_id in _job_cache:
            continue  # already cached
        if is_completed or run_id not in _job_cache:
            to_fetch.append(data)

    # Fetch jobs in parallel
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

    # Update cache for completed runs
    for data in raw_runs:
        run_id = data.get("id", 0)
        if run_id in fetched and data.get("status") == "completed":
            _job_cache[run_id] = fetched[run_id]

    # Build WorkflowRun list, filtering to runs with local-runner jobs
    runs = []
    for data in raw_runs:
        run_id = data.get("id", 0)
        jobs = _job_cache.get(run_id) or fetched.get(run_id, [])
        if not jobs:
            continue  # no jobs on local runners — skip

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
        # Overall duration
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
                wr.duration = "—"
        runs.append(wr)
    return runs


def get_system_stats():
    """Get system-level CPU, memory, and process stats."""
    stats = SystemStats()

    # Load average
    try:
        load = os.getloadavg()
        stats.load_avg = load
    except Exception:
        pass

    # Memory via vm_stat
    vm_out = run_cmd("vm_stat")
    if vm_out:
        page_size = 16384  # Apple Silicon default
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

    # Get actual total memory from sysctl
    mem_total = run_cmd("sysctl -n hw.memsize")
    if mem_total:
        try:
            stats.mem_total_gb = int(mem_total) / (1024**3)
        except ValueError:
            pass

    # Count Xcode/simulator processes
    xcode_out = run_cmd(
        "ps aux | grep -c '[x]codebuild\\|[X]code'"
    )
    sim_out = run_cmd(
        "ps aux | grep -c '[S]imulator\\|[s]imctl'"
    )
    try:
        stats.xcode_processes = int(xcode_out)
    except ValueError:
        pass
    try:
        stats.simulator_processes = int(sim_out)
    except ValueError:
        pass

    # CPU usage (quick sample)
    cpu_out = run_cmd("ps -A -o %cpu | awk '{s+=$1} END {print s}'")
    try:
        stats.cpu_percent = float(cpu_out)
    except ValueError:
        pass

    return stats


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

        # Match process info
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

        # GitHub API status
        if cfg["name"] in gh_status:
            api_data = gh_status[cfg["name"]]
            rs.online = api_data.get("status") == "online"
            rs.busy = api_data.get("busy", False)

        # Log-based status
        state, job, last_time = get_runner_log_status(cfg["path"])
        if state == "busy":
            rs.busy = True
            rs.current_job = job
        rs.last_job_time = last_time

        # Process uptime
        if rs.pid:
            etime = run_cmd(f"ps -o etime= -p {rs.pid}")
            rs.uptime = etime.strip()

        runner_statuses.append(rs)

    return runner_statuses, runs, sys_stats


# ── Rendering ──────────────────────────────────────────────────────────────────

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

    # Top border
    safe_addstr(win, y, x, "┌" + "─" * (w - 2) + "┐", attr)
    # Sides
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x, "│", attr)
        safe_addstr(win, y + i, x + w - 1, "│", attr)
    # Bottom border
    safe_addstr(win, y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)

    # Title
    if title:
        title_str = f" {title} "
        tx = x + 2
        safe_addstr(win, y, tx, title_str, attr | curses.A_BOLD)


def draw_bar(win, y, x, width, pct, color=C_GREEN):
    """Draw a percentage bar."""
    filled = int((pct / 100) * width)
    bar = "█" * filled + "░" * (width - filled)
    c = color
    if pct > 80:
        c = C_RED
    elif pct > 60:
        c = C_YELLOW
    safe_addstr(win, y, x, bar, curses.color_pair(c))


def format_time_ago(iso_str):
    """Convert ISO timestamp to 'Xm ago' format."""
    if not iso_str:
        return "—"
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
        return "—"


def draw_header(win, max_x):
    """Draw the header bar."""
    header = f" 🖥  GitHub Runner Dashboard — {REPO} "
    pad = " " * (max_x - len(header) - 1)
    safe_addstr(win, 0, 0, header + pad, curses.color_pair(C_HEADER) | curses.A_BOLD)


def draw_runner_panel(win, y, x, w, runner, index):
    """Draw a single runner status panel."""
    h = 10
    draw_box(win, y, x, h, w, f"Runner {index + 1}: {runner.name}")

    col = x + 2
    row = y + 1

    # Status indicator
    if runner.busy:
        status = "● BUSY"
        status_color = C_YELLOW
    elif runner.online:
        status = "● ONLINE"
        status_color = C_GREEN
    else:
        status = "● OFFLINE"
        status_color = C_RED

    safe_addstr(win, row, col, "Status: ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 8, status, curses.color_pair(status_color) | curses.A_BOLD
    )

    safe_addstr(win, row, col + 22, f"Label: ", curses.color_pair(C_DIM))
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
        safe_addstr(win, row, col + 8, f"Last: {runner.current_job}", curses.color_pair(C_DIM))
    else:
        safe_addstr(win, row, col + 8, "idle", curses.color_pair(C_DIM))

    safe_addstr(win, row, col + 22, f"Type: ", curses.color_pair(C_DIM))
    safe_addstr(win, row, col + 28, runner.job_type, curses.color_pair(C_CYAN))
    row += 1

    # PID and uptime
    safe_addstr(win, row, col, "PID:    ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 8, str(runner.pid) if runner.pid else "—", curses.color_pair(C_NORMAL)
    )
    safe_addstr(win, row, col + 22, "Uptime: ", curses.color_pair(C_DIM))
    safe_addstr(
        win, row, col + 30, runner.uptime if runner.uptime else "—", curses.color_pair(C_NORMAL)
    )
    row += 1

    # CPU/mem for listener
    safe_addstr(win, row, col, "Listener CPU: ", curses.color_pair(C_DIM))
    cpu_str = f"{runner.cpu_percent:.1f}%"
    cpu_color = C_GREEN if runner.cpu_percent < 50 else (C_YELLOW if runner.cpu_percent < 80 else C_RED)
    safe_addstr(win, row, col + 14, cpu_str, curses.color_pair(cpu_color))
    safe_addstr(win, row, col + 22, f"Mem: {runner.mem_mb:.0f} MB", curses.color_pair(C_DIM))
    row += 1

    # Worker processes
    safe_addstr(win, row, col, "Workers:  ", curses.color_pair(C_DIM))
    if runner.worker_pids:
        worker_info = f"{len(runner.worker_pids)} active"
        safe_addstr(win, row, col + 10, worker_info, curses.color_pair(C_GREEN))
        safe_addstr(win, row, col + 22, f"CPU: {runner.worker_cpu:.1f}%  Mem: {runner.worker_mem:.0f} MB", curses.color_pair(C_DIM))
    else:
        safe_addstr(win, row, col + 10, "none", curses.color_pair(C_DIM))
    row += 1

    # Last activity
    safe_addstr(win, row, col, "Last activity: ", curses.color_pair(C_DIM))
    safe_addstr(
        win,
        row,
        col + 15,
        runner.last_job_time if runner.last_job_time else "—",
        curses.color_pair(C_DIM),
    )

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
    load_color = C_GREEN if stats.load_avg[0] < 4 else (C_YELLOW if stats.load_avg[0] < 8 else C_RED)
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
        # Normalize to 100% (multi-core can exceed 100)
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
    safe_addstr(win, row, col + 13, str(stats.xcode_processes), curses.color_pair(C_CYAN))
    safe_addstr(win, row, col + 20, "Simulator procs: ", curses.color_pair(C_DIM))
    safe_addstr(win, row, col + 37, str(stats.simulator_processes), curses.color_pair(C_CYAN))
    row += 1

    # Cores
    cores_str = run_cmd("sysctl -n hw.ncpu")
    chip_str = run_cmd("sysctl -n machdep.cpu.brand_string")
    safe_addstr(win, row, col, "Chip: ", curses.color_pair(C_DIM))
    chip_display = chip_str if chip_str else "—"
    safe_addstr(win, row, col + 6, f"{chip_display} ({cores_str} cores)", curses.color_pair(C_NORMAL))

    return h


def draw_workflow_panel(win, y, x, w, runs, max_rows=20):
    """Draw recent workflow runs panel with per-job detail."""
    # Count total rows needed: 1 header + per-run rows (1 per job)
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
    safe_addstr(win, row, col + 47, "Runner", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(win, row, col + 61, "Duration", curses.color_pair(C_DIM) | curses.A_BOLD)
    safe_addstr(win, row, col + 72, "When", curses.color_pair(C_DIM) | curses.A_BOLD)
    row += 1

    rows_used = 0
    for run in runs:
        if rows_used >= content_rows:
            break
        first_job = True
        for job in run.jobs:
            if rows_used >= content_rows:
                break

            # Status icon (from job, not run)
            if job.status == "in_progress":
                icon = "⟳"
                icon_color = C_YELLOW
            elif job.status == "queued":
                icon = "◌"
                icon_color = C_DIM
            elif job.conclusion == "success":
                icon = "✓"
                icon_color = C_GREEN
            elif job.conclusion == "failure":
                icon = "✗"
                icon_color = C_RED
            elif job.conclusion == "cancelled":
                icon = "⊘"
                icon_color = C_DIM
            elif job.conclusion == "skipped":
                icon = "–"
                icon_color = C_DIM
            else:
                icon = "?"
                icon_color = C_DIM

            safe_addstr(win, row, col, icon, curses.color_pair(icon_color) | curses.A_BOLD)

            # Show run number and branch only on first job row
            if first_job:
                safe_addstr(win, row, col + 4, f"#{run.run_number}", curses.color_pair(C_DIM))
                branch_display = run.branch[:22] if run.branch else "—"
                safe_addstr(win, row, col + 10, branch_display, curses.color_pair(C_CYAN))
                first_job = False
            else:
                safe_addstr(win, row, col + 4, "  └", curses.color_pair(C_DIM))

            # Job name
            job_display = job.name[:12] if job.name else "—"
            safe_addstr(win, row, col + 34, job_display, curses.color_pair(C_NORMAL))

            # Runner name (short form)
            runner_short = job.runner_name.replace("manas-mac", "mac")
            safe_addstr(win, row, col + 47, runner_short, curses.color_pair(C_MAGENTA))

            # Duration
            safe_addstr(win, row, col + 61, job.duration or "—", curses.color_pair(C_DIM))

            # When
            safe_addstr(win, row, col + 72, format_time_ago(job.started), curses.color_pair(C_DIM))

            row += 1
            rows_used += 1

    return h


def draw_footer(win, max_y, max_x, last_refresh):
    """Draw the status bar at the bottom."""
    now = datetime.now().strftime("%H:%M:%S")
    footer = f" Last refresh: {last_refresh} │ Next: {REFRESH_INTERVAL}s │ [q] Quit  [r] Refresh │ {now} "
    pad = " " * max(0, max_x - len(footer) - 1)
    safe_addstr(
        win, max_y - 1, 0, footer + pad, curses.color_pair(C_STATUS_BAR)
    )


# ── Main Loop ──────────────────────────────────────────────────────────────────


def main(stdscr):
    """Main curses loop."""
    curses.curs_set(0)  # Hide cursor
    init_colors()
    stdscr.timeout(500)  # 500ms input timeout for responsive key handling

    last_data = None
    last_fetch = 0
    fetch_count = 0

    while True:
        now = time.time()
        max_y, max_x = stdscr.getmaxyx()

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

        # Fetch data periodically
        force_refresh = False
        key = stdscr.getch()
        if key == ord("q"):
            break
        elif key == ord("r"):
            force_refresh = True

        if force_refresh or now - last_fetch >= REFRESH_INTERVAL:
            try:
                runners, runs, sys_stats = collect_all_data()
                last_data = (runners, runs, sys_stats)
                last_fetch = now
                fetch_count += 1
            except Exception as e:
                # On error, keep last data
                if last_data is None:
                    last_data = ([], [], SystemStats())

        if last_data is None:
            stdscr.clear()
            safe_addstr(stdscr, max_y // 2, max_x // 2 - 8, "Loading...", curses.color_pair(C_YELLOW))
            stdscr.refresh()
            continue

        runners, runs, sys_stats = last_data

        # ── Draw everything ──
        stdscr.erase()

        # Header
        draw_header(stdscr, max_x)

        current_y = 2

        # Runner panels side by side if wide enough, stacked otherwise
        panel_w = max_x - 2
        if max_x >= 110:
            # Side by side
            half_w = (max_x - 3) // 2
            for i, runner in enumerate(runners):
                rx = 1 + i * (half_w + 1)
                rh = draw_runner_panel(stdscr, current_y, rx, half_w, runner, i)
            current_y += rh + 1
        else:
            # Stacked
            for i, runner in enumerate(runners):
                rh = draw_runner_panel(stdscr, current_y, 1, panel_w, runner, i)
                current_y += rh + 1

        # System stats
        if current_y + 8 < max_y - 2:
            sh = draw_system_panel(stdscr, current_y, 1, panel_w, sys_stats)
            current_y += sh + 1

        # Workflow runs
        if current_y + 5 < max_y - 2:
            remaining = max_y - current_y - 2
            max_job_rows = remaining - 3  # leave room for box borders + header
            if max_job_rows > 0:
                wh = draw_workflow_panel(
                    stdscr, current_y, 1, panel_w, runs, max_rows=max_job_rows
                )
                current_y += wh + 1

        # Footer
        refresh_str = datetime.now().strftime("%H:%M:%S")
        draw_footer(stdscr, max_y, max_x, refresh_str)

        stdscr.refresh()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        print("Dashboard closed.")
