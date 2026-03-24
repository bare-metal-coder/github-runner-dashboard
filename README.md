# GitHub Runner Dashboard

A terminal-based (curses) dashboard for monitoring self-hosted GitHub Actions runners on macOS.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## What it shows

- **Runner panels** — online/busy/offline status, current job, PID, uptime, CPU/memory for listener and worker processes
- **System resources** — load average, CPU usage bar, memory usage bar, Xcode and Simulator process counts, chip info
- **Runs on local runners** — per-job status with runner assignment, duration, and recency (filtered to only show runs that executed on your local runners)

## Requirements

- Python 3.10+ (uses stdlib only — no pip install needed)
- `gh` CLI authenticated (`gh auth login`)
- Self-hosted GitHub Actions runners running on the same machine

## Setup

Edit the configuration at the top of `runner-dashboard.py` to match your setup:

```python
REPO = "your-org/your-repo"

RUNNERS = [
    {
        "name": "runner-name-from-github",
        "label": "runner-1",
        "path": Path.home() / "path/to/runner",
        "job_type": "Unit Tests",
    },
]
```

## Usage

```bash
python3 runner-dashboard.py
```

### Controls

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh |

Auto-refreshes every 4 seconds.

## How it works

- Reads runner diagnostic logs locally for real-time busy/idle state detection
- Queries the GitHub API (`gh api`) for runner status and workflow run history
- Fetches per-run job details in parallel to identify which runner handled each job
- Caches completed run data to minimize API calls
- Uses `ps`, `vm_stat`, and `sysctl` for system resource monitoring

## License

MIT
