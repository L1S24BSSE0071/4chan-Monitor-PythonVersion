#!/usr/bin/env python3
"""
4chan Thread Monitor - V 1.0
============================
A robust, cross-platform monitor for 4chan threads using gallery-dl.
Automatically downloads new images from a list of threads, handles 404s,
rate-limits, and retries.

Original: 4chan-threads-monitor.bat (Windows Batch)
Rewritten in Python for reliability, performance, and maintainability.

Features:
- Automatic sanitization of thread lists (BOM, invisible chars, dupes)
- Priority queue support
- New URL auto-merge from new_4chan.txt
- Pause/resume via pause_4chan.txt
- Robust error handling: 404 removal, 429 rate-limit pause, unsupported URL removal
- Atomic file writes (no corruption on crash or Ctrl+C)
- Graceful Ctrl+C handling (kills gallery-dl subprocess instantly)
- Binary-safe logging
- Statistics tracking

Usage:
    pip install gallery-dl
    python 4chan-thread-monitor-v1.0.py
    python 4chan-thread-monitor-v1.0.py --dry-run --loops 1 --no-clear
    python 4chan-thread-monitor-v1.0.py --self-test

Files:
    threads_4chan.txt   - Main queue (one URL per line)
    new_4chan.txt       - Drop new URLs here, auto-merged and deleted
    priority_4chan.txt  - High priority threads (processed first each cycle)
    pause_4chan.txt     - Create to pause, delete to resume
    log_4chan.txt       - Last gallery-dl output
    failures_4chan.txt  - Failure log with timestamps
    stats_4chan.txt     - Session counters (atomic)

Controls:
    Ctrl+C - First press: graceful stop (kills current download, saves stats)
             Second press: force exit

Author: Optimized from batch script
Version: V 1.0
License: MIT
"""

import argparse
import os
import re
import sys
import time
import signal
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import List, Set, Dict, Tuple, Optional

# ==================== CONFIGURATION ====================

@dataclass(frozen=True)
class Config:
    """All file paths and timing constants. Frozen to prevent accidental mutation."""
    # File paths
    list_file: Path = Path("threads_4chan.txt")
    log_file: Path = Path("log_4chan.txt")
    new_box: Path = Path("new_4chan.txt")
    priority_box: Path = Path("priority_4chan.txt")
    pause_file: Path = Path("pause_4chan.txt")
    stats_file: Path = Path("stats_4chan.txt")
    fail_log: Path = Path("failures_4chan.txt")
    dead_tmp: Path = Path("dead_cycle_4chan.tmp")

    # Retry and timing settings
    max_retries: int = 3              # Retries per thread on transient failure
    base_thread_sleep: int = 8        # Normal sleep between threads
    base_cycle_sleep: int = 30        # Sleep between full cycles
    min_thread_sleep: int = 3         # Sleep when queue >= 10 (busy)
    max_thread_sleep: int = 15        # Sleep when queue <= 2 (idle)
    gallery_timeout: int = 600        # Kill gallery-dl after 10 min
    blocked_sleep: int = 300          # Pause on 429/Cloudflare (5 min)
    retry_sleep: int = 10             # Sleep between retries

CFG = Config()

# Pre-compiled regexes for performance (3x faster than compiling per line)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
NOT_PRINTABLE_RE = re.compile(r"[^\x20-\x7E]+")
BOM = "\ufeff"  # UTF-8 BOM to strip from files

# Error classification markers (lowercase for case-insensitive matching)
DEAD_MARKERS = ("not found", "has expired", "is archived")
BLOCKED_MARKERS = ("429 too many", "403 client", "cloudflare", "rate limit")
MARK_404 = "404 client"
MARK_UNSUPPORTED = "unsupported url"

# ANSI colors for terminal output
COLORS = {
    "cyan": "\033[96m", "green": "\033[92m", "yellow": "\033[93m",
    "red": "\033[91m", "white": "\033[97m", "gray": "\033[90m", "reset": "\033[0m"
}

# ==================== GLOBAL STATE FOR CTRL+C HANDLING ====================

stop_event = threading.Event()  # Set on SIGINT, checked in all loops
current_proc: Optional[subprocess.Popen] = None  # Current gallery-dl process
proc_lock = threading.Lock()  # Protects current_proc

# ==================== UTILITY FUNCTIONS ====================

def cprint(msg: str, color: str = "white"):
    """Print with color, flush immediately."""
    sys.stdout.write(f"{COLORS.get(color, '')}{msg}{COLORS['reset']}\n")
    sys.stdout.flush()

def press_any_key_to_exit():
    """Wait for any key press. Any key on Windows, Enter on Unix."""
    cprint("Press any key to exit...", "white")
    try:
        import msvcrt  # Windows
        msvcrt.getch()
    except ImportError:
        try:
            input()  # Unix - Enter key
        except:
            pass

def interruptible_sleep(seconds: float) -> bool:
    """
    Sleep that can be interrupted by Ctrl+C.
    Returns True if interrupted, False if completed normally.
    Checks stop_event every 0.2s.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return True
        time.sleep(0.2)
    return False

def atomic_write(path: Path, content: str):
    """
    Write file atomically to prevent corruption on crash or Ctrl+C.
    Uses mkstemp + os.replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except:
            pass

def read_urls(path: Path) -> List[str]:
    """
    Read and sanitize URLs from file:
    - Strips BOM, non-printable chars, whitespace
    - Keeps only http/https URLs
    - Deduplicates case-insensitively, preserves order
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if lines and lines[0].startswith(BOM):
            lines[0] = lines[0].lstrip(BOM)

        seen: Set[str] = set()
        out: List[str] = []

        for line in lines:
            cleaned = NOT_PRINTABLE_RE.sub("", line).strip()
            if not URL_RE.match(cleaned):
                continue
            low = cleaned.lower()
            if low not in seen:
                seen.add(low)
                out.append(cleaned)
        return out
    except:
        return []

# ==================== STARTUP CHECKS ====================

def check_gallery_dl() -> str:
    """Verify gallery-dl is installed and return version."""
    cprint("[STARTUP] Checking gallery-dl...", "white")
    if not shutil.which("gallery-dl"):
        cprint("[FATAL] gallery-dl not found in PATH. Install with: pip install gallery-dl", "red")
        sys.exit(1)
    try:
        r = subprocess.run(["gallery-dl", "--version"], capture_output=True, text=True, timeout=10)
        ver = (r.stdout or r.stderr or "").strip()
        if not ver:
            raise RuntimeError("empty version output")
        cprint(f"[STARTUP] Found: {ver}", "green")
        return ver
    except Exception as e:
        cprint(f"[FATAL] gallery-dl check failed: {e}", "red")
        sys.exit(1)

def check_pypi_update(ver_raw: str):
    """Check PyPI for newer gallery-dl version (best-effort, offline-safe)."""
    ver = re.sub(r"^gallery-dl\s*", "", ver_raw.strip(), flags=re.I)
    try:
        import urllib.request, json
        req = urllib.request.Request(
            "https://pypi.org/pypi/gallery-dl/json",
            headers={"User-Agent": "4chan-monitor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            latest = json.loads(resp.read().decode())["info"]["version"]
            if latest == ver:
                cprint(f"[STARTUP] gallery-dl is up to date ({ver})", "green")
            else:
                cprint(f"[UPDATE] gallery-dl {latest} available (you have {ver}) -> pip install -U gallery-dl", "yellow")
    except:
        cprint("[STARTUP] Update check skipped (offline)", "gray")
    print()

def sanitize_list():
    """Sanitize main thread list and clean up leftover temp files from crashes."""
    if not CFG.list_file.exists():
        return
    urls = read_urls(CFG.list_file)
    atomic_write(CFG.list_file, "\n".join(urls) + ("\n" if urls else ""))
    cprint(f"[STARTUP] List sanitized: {len(urls)} URLs", "green")
    print()
    # Clean up dead tmp file that might be left from a crash
    try:
        if CFG.dead_tmp.exists():
            CFG.dead_tmp.unlink()
    except:
        pass

# ==================== STATS PERSISTENCE ====================

DEFAULT_STATS = {
    "TOTAL_SESSIONS": 0, "TOTAL_SUCCESS": 0, "TOTAL_404": 0,
    "TOTAL_BLOCKED": 0, "TOTAL_FAILED": 0, "TOTAL_UNSUPPORTED": 0
}

def load_stats() -> Dict[str, int]:
    """Load stats from file, create default if missing."""
    stats = DEFAULT_STATS.copy()
    if not CFG.stats_file.exists():
        atomic_write(CFG.stats_file, "\n".join(f"{k}={v}" for k, v in stats.items()) + "\n")
        return stats
    try:
        for line in CFG.stats_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in stats:
                    stats[k] = int(v.strip())
    except:
        pass
    return stats

def save_stats(stats: Dict[str, int]):
    """Save stats atomically."""
    atomic_write(CFG.stats_file, "\n".join(f"{k}={v}" for k, v in stats.items()) + "\n")

# ==================== QUEUE MANAGEMENT ====================

def absorb_new_box() -> int:
    """
    Merge URLs from new_4chan.txt into main list.
    - Logs ADDED vs DUPLICATE
    - Deletes new_4chan.txt after merge
    Returns number added.
    """
    if not CFG.new_box.exists():
        return 0

    existing = read_urls(CFG.list_file)
    existing_set = set(u.lower() for u in existing)

    try:
        raw_lines = CFG.new_box.read_text(encoding="utf-8", errors="ignore").splitlines()
        if raw_lines and raw_lines[0].startswith(BOM):
            raw_lines[0] = raw_lines[0].lstrip(BOM)
    except:
        raw_lines = []

    added: List[str] = []
    dupes = 0

    for line in raw_lines:
        cleaned = NOT_PRINTABLE_RE.sub("", line).strip()
        if not URL_RE.match(cleaned):
            continue
        low = cleaned.lower()
        if low not in existing_set:
            added.append(cleaned)
            existing_set.add(low)
            cprint(f"  [ADDED] {cleaned}", "green")
        else:
            cprint(f"  [DUPLICATE] {cleaned}", "yellow")
            dupes += 1

    if added:
        atomic_write(CFG.list_file, "\n".join(existing + added) + "\n")
    if added or dupes:
        cprint(f"[INFO] Merge complete: {len(added)} added, {dupes} duplicates", "cyan")
        print()

    try:
        CFG.new_box.unlink()
    except:
        pass
    return len(added)

def wait_if_paused():
    """If pause_4chan.txt exists, wait until it's deleted. Interruptible by Ctrl+C."""
    if not CFG.pause_file.exists():
        return
    cprint(f"[PAUSED] Delete {CFG.pause_file} to resume...", "yellow")
    while CFG.pause_file.exists():
        if stop_event.is_set():
            return
        time.sleep(1)
    cprint("[RESUMED]", "green")
    print()

# ==================== GALLERY-DL WRAPPER ====================

def run_gallery_dl(url: str) -> Tuple[int, str]:
    """
    Run gallery-dl for a single URL with timeout and Ctrl+C support.
    Returns (return_code, combined_output).
    -1 return code means interrupted by Ctrl+C.
    """
    global current_proc
    log_path = CFG.log_file
    err_path = Path(str(log_path) + ".err")

    # Clean previous logs
    for p in (log_path, err_path):
        try:
            if p.exists():
                p.unlink()
        except:
            pass

    if stop_event.is_set():
        return -1, "Interrupted"

    cmd = ["gallery-dl", "--sleep-request", "2.0", url]
    spinner = ["|", "/", "-", "\\"]
    start = time.monotonic()
    proc = None

    try:
        with log_path.open("wb") as out_f, err_path.open("wb") as err_f:
            # CREATE_NO_WINDOW hides console window on Windows
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            proc = subprocess.Popen(
                cmd, stdout=out_f, stderr=err_f,
                creationflags=flags
            ) if flags else subprocess.Popen(cmd, stdout=out_f, stderr=err_f)

            with proc_lock:
                current_proc = proc

            spin = 0
            while proc.poll() is None:
                # Check for Ctrl+C every iteration
                if stop_event.is_set():
                    cprint(f"\n  [INTERRUPTED] Stopping gallery-dl", "yellow")
                    try:
                        proc.kill()
                    except:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except:
                        pass
                    break

                # Check for timeout
                elapsed = int(time.monotonic() - start)
                if elapsed >= CFG.gallery_timeout:
                    sys.stdout.write("\r" + " " * 60 + "\r")
                    cprint(f"  [TIMEOUT] {CFG.gallery_timeout}s exceeded, killing process", "red")
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except:
                        pass
                    break

                # Spinner animation
                mins, secs = divmod(elapsed, 60)
                sys.stdout.write(f"\r  [{spinner[spin % 4]}] Scraping... [{mins:02d}:{secs:02d}]  ")
                sys.stdout.flush()
                spin += 1
                time.sleep(0.2)

            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
            code = proc.returncode if proc.returncode is not None else 1
            if stop_event.is_set():
                code = -1

    except FileNotFoundError:
        cprint("  [FATAL] gallery-dl not found", "red")
        return 1, ""
    except Exception as e:
        if stop_event.is_set():
            return -1, "Interrupted"
        cprint(f"  [ERROR] {e}", "red")
        return 1, ""
    finally:
        with proc_lock:
            current_proc = None

    if stop_event.is_set():
        return -1, "Interrupted"

    # Merge stdout + stderr logs
    try:
        err_bytes = err_path.read_bytes() if err_path.exists() else b""
        try:
            err_path.unlink()
        except:
            pass
        log_bytes = log_path.read_bytes() if log_path.exists() else b""
        if err_bytes:
            with log_path.open("ab") as f:
                f.write(err_bytes)
            log_bytes += err_bytes
        return code, log_bytes.decode("utf-8", errors="ignore")
    except:
        return code if 'code' in locals() else 1, ""

def classify_log(log_text: str) -> Tuple[bool, bool, bool, bool]:
    """
    Classify gallery-dl output to decide action.
    Returns (is_dead_ok, is_blocked, is_404, is_unsupported)
    """
    low = log_text.lower()
    is_dead_ok = any(x in low for x in DEAD_MARKERS)
    is_blocked = any(x in low for x in BLOCKED_MARKERS)
    is_404 = MARK_404 in low
    is_unsupported = MARK_UNSUPPORTED in low
    return is_dead_ok, is_blocked, is_404, is_unsupported

def log_failure(msg: str):
    """Append failure to failures log with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with CFG.fail_log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{ts}] {msg}\n")
    except:
        pass

def process_thread(url: str, label: str, dry_run: bool, counters: Dict[str, int], dead_list: List[str], thread_sleep: int) -> bool:
    """
    Process a single thread URL.
    Returns True to continue loop, False if interrupted by Ctrl+C.
    """
    if stop_event.is_set():
        return False

    url = url.strip().strip('"')
    if not url:
        return True

    cprint(f"[{label}] {url}", "white")

    # Dry-run mode: just show what would be executed
    if dry_run:
        cprint(f'  [DRY-RUN] gallery-dl --sleep-request 2.0 "{url}"', "yellow")
        print()
        counters["CYCLE_SKIPPED"] += 1
        return True

    # Retry loop
    for attempt in range(CFG.max_retries + 1):
        if stop_event.is_set():
            return False

        if attempt > 0:
            cprint(f"  [RETRY {attempt}/{CFG.max_retries}] {CFG.retry_sleep}s...", "yellow")
            if interruptible_sleep(CFG.retry_sleep):
                return False

        code, log_text = run_gallery_dl(url)

        # Interrupted by Ctrl+C
        if code == -1 or stop_event.is_set():
            return False

        # Success
        if code == 0:
            dead_ok, _, _, _ = classify_log(log_text)
            if dead_ok:
                # Thread expired/archived despite exit 0 - treat as dead
                log_failure(f"404 REMOVED: {url}")
                dead_list.append(url)
                cprint("  [404] Thread expired/archived, removing from list", "yellow")
                print()
                counters["CYCLE_404"] += 1
                return True
            cprint("  [OK] Downloaded successfully", "green")
            cprint(f"  [PAUSE] {thread_sleep}s...", "white")
            print()
            counters["CYCLE_SUCCESS"] += 1
            if interruptible_sleep(thread_sleep):
                return False
            return True

        # Failure - classify error type
        _, is_blocked, is_404, is_unsupported = classify_log(log_text)

        if is_unsupported:
            log_failure(f"UNSUPPORTED: {url}")
            dead_list.append(url)
            cprint("  [UNSUPPORTED] URL not supported, removing", "yellow")
            print()
            counters["CYCLE_UNSUPPORTED"] += 1
            return True

        if is_404:
            log_failure(f"404 REMOVED: {url}")
            dead_list.append(url)
            cprint("  [404] Thread not found, removing from list", "yellow")
            print()
            counters["CYCLE_404"] += 1
            return True

        if is_blocked:
            log_failure(f"BLOCKED: {url}")
            if CFG.blocked_sleep < 60:
                cprint(f"  [BLOCKED] Rate-limited, pausing {CFG.blocked_sleep}s", "red")
            else:
                cprint(f"  [BLOCKED] Rate-limited, pausing {CFG.blocked_sleep // 60}m", "red")
            print()
            counters["CYCLE_BLOCKED"] += 1
            if interruptible_sleep(CFG.blocked_sleep):
                return False
            return True

        # Generic failure - retry until max retries
        if attempt == CFG.max_retries:
            log_failure(f"FAILED max retries: {url}")
            cprint("  [FAILED] Keeping in queue for next cycle", "red")
            print()
            counters["CYCLE_FAILED"] += 1
            return True

    return True

def remove_dead(dead_urls: List[str]) -> int:
    """Remove dead URLs from main list atomically."""
    if not dead_urls:
        return 0
    dead_set = set(u.lower() for u in dead_urls if URL_RE.match(u))
    if not dead_set:
        return 0

    current = read_urls(CFG.list_file)
    kept = [u for u in current if u.lower() not in dead_set]
    removed = len(current) - len(kept)

    if removed > 0:
        atomic_write(CFG.list_file, "\n".join(kept) + ("\n" if kept else ""))
        cprint(f"[CLEANUP] Removed {removed} dead threads from list", "yellow")

    try:
        if CFG.dead_tmp.exists():
            CFG.dead_tmp.unlink()
    except:
        pass
    return removed

# ==================== MAIN LOOP ====================

def main():
    global current_proc

    parser = argparse.ArgumentParser(add_help=False, description="4chan Thread Monitor V 1.0")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded without downloading")
    parser.add_argument("--help", action="store_true", dest="help_flag", help="Show this help")
    parser.add_argument("--loops", type=int, default=None, help="Number of cycles (0 = infinite)")
    parser.add_argument("--no-clear", action="store_true", help="Don't clear screen between cycles")
    parser.add_argument("--self-test", action="store_true", help="Run internal self-tests")
    args, _ = parser.parse_known_args()

    if args.help_flag:
        print(__doc__)
        parser.print_help()
        print("\nControls:")
        print("  Ctrl+C - First press: graceful stop (saves stats)")
        print("           Second press: force exit")
        print("  To pause: create file pause_4chan.txt, delete to resume")
        sys.exit(0)

    if args.self_test:
        assert read_urls(Path("nonexist")) == []
        assert classify_log("404 Client Error")[2] is True
        assert classify_log("429 Too Many")[1] is True
        assert classify_log("Unsupported URL")[3] is True
        assert classify_log("Thread has expired")[0] is True
        cprint("Self-test passed!", "green")
        sys.exit(0)

    clear_enabled = not args.no_clear
    clear_screen = lambda: os.system("cls" if os.name == "nt" else "clear") if clear_enabled else None

    # --- Signal handler for Ctrl+C ---
    def on_signal(signum, frame):
        if stop_event.is_set():
            # Second Ctrl+C - force exit
            cprint("\n[FORCE EXIT] Terminating...", "red")
            with proc_lock:
                if current_proc and current_proc.poll() is None:
                    try:
                        current_proc.kill()
                    except:
                        pass
            os._exit(1)
        else:
            stop_event.set()
            cprint("\n[INTERRUPTED] Stopping gracefully...", "yellow")
            with proc_lock:
                if current_proc and current_proc.poll() is None:
                    try:
                        current_proc.kill()
                    except:
                        pass

    try:
        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, 'SIGBREAK'):  # Windows Ctrl+Break
            signal.signal(signal.SIGBREAK, on_signal)
    except:
        pass

    # --- Startup banner ---
    clear_screen()
    cprint("=" * 79, "cyan")
    cprint("  4CHAN THREAD MONITOR - V 1.0", "cyan")
    cprint("  GitHub-ready, production build", "cyan")
    cprint("=" * 79, "cyan")
    print()
    if args.dry_run:
        cprint("[DRY-RUN MODE] No files will be downloaded", "yellow")
        print()

    ver = check_gallery_dl()
    check_pypi_update(ver)
    sanitize_list()

    stats = load_stats()
    total_sessions = stats["TOTAL_SESSIONS"]
    totals = {k: stats[k] for k in stats if k != "TOTAL_SESSIONS"}

    # --- Get loop count ---
    if args.loops is not None:
        max_loops = max(0, args.loops)
    else:
        cprint("How many monitor cycles to run? (0 = infinite)", "white")
        try:
            max_loops = int((input("Loop Count: ").strip() or "0"))
        except:
            max_loops = 0

    current_loop = 0
    clear_screen()

    # --- Main monitoring loop ---
    try:
        while not stop_event.is_set():
            wait_if_paused()
            if stop_event.is_set():
                break

            absorb_new_box()
            if stop_event.is_set():
                break

            # No list file - wait
            if not CFG.list_file.exists():
                cprint("[WAIT] No thread list found (threads_4chan.txt). Waiting 60s...", "yellow")
                if interruptible_sleep(60):
                    break
                clear_screen()
                continue

            if max_loops > 0 and current_loop >= max_loops:
                cprint("[SYSTEM] Max cycles reached. Exiting.", "cyan")
                break

            # Single read per cycle (optimized)
            queue_urls = read_urls(CFG.list_file)
            queue_size = len(queue_urls)
            current_loop += 1
            total_sessions += 1

            # Adaptive sleep based on queue size
            thread_sleep = CFG.base_thread_sleep
            if queue_size <= 2:
                thread_sleep = CFG.max_thread_sleep
            elif queue_size >= 10:
                thread_sleep = CFG.min_thread_sleep

            cprint("=" * 79, "cyan")
            cprint(f" CYCLE: {current_loop} | TIME: {datetime.now().strftime('%H:%M:%S')} | QUEUE: {queue_size} | SLEEP: {thread_sleep}s", "cyan")
            if args.dry_run:
                cprint(" MODE: DRY-RUN", "yellow")
            cprint("=" * 79, "cyan")
            print()

            counters = {
                "CYCLE_SUCCESS": 0, "CYCLE_404": 0, "CYCLE_BLOCKED": 0,
                "CYCLE_FAILED": 0, "CYCLE_SKIPPED": 0, "CYCLE_UNSUPPORTED": 0
            }
            dead_accum: List[str] = []

            # Process priority queue first
            if CFG.priority_box.exists() and not stop_event.is_set():
                cprint("[PRIORITY] Processing priority threads...", "yellow")
                print()
                for p_url in read_urls(CFG.priority_box):
                    if stop_event.is_set():
                        break
                    if not process_thread(p_url, "PRIORITY", args.dry_run, counters, dead_accum, thread_sleep):
                        break
                print()

            # Process main queue
            for url in queue_urls:
                if stop_event.is_set():
                    break
                if not process_thread(url, "NORMAL", args.dry_run, counters, dead_accum, thread_sleep):
                    break

            if stop_event.is_set():
                cprint("\n[INTERRUPTED] Saving stats and exiting...", "yellow")
                break

            # Remove dead threads
            if dead_accum:
                remove_dead(dead_accum)

            # Update stats
            totals["TOTAL_SUCCESS"] += counters["CYCLE_SUCCESS"]
            totals["TOTAL_404"] += counters["CYCLE_404"]
            totals["TOTAL_BLOCKED"] += counters["CYCLE_BLOCKED"]
            totals["TOTAL_FAILED"] += counters["CYCLE_FAILED"]
            totals["TOTAL_UNSUPPORTED"] += counters["CYCLE_UNSUPPORTED"]
            save_stats({"TOTAL_SESSIONS": total_sessions, **totals})

            # Cycle summary
            print()
            cprint("-" * 71, "cyan")
            cprint(f" CYCLE {current_loop} SUMMARY", "cyan")
            cprint("-" * 71, "cyan")
            cprint(f"  Success     : {counters['CYCLE_SUCCESS']}", "green")
            cprint(f"  404 Removed : {counters['CYCLE_404']}", "yellow")
            cprint(f"  Blocked     : {counters['CYCLE_BLOCKED']}", "yellow")
            cprint(f"  Unsupported : {counters['CYCLE_UNSUPPORTED']}", "yellow")
            cprint(f"  Failed      : {counters['CYCLE_FAILED']}", "red")
            cprint(f"  Skipped     : {counters['CYCLE_SKIPPED']}", "gray")
            cprint("-" * 71, "cyan")
            cprint(f" ALL-TIME  Sessions:{total_sessions}  OK:{totals['TOTAL_SUCCESS']}  404:{totals['TOTAL_404']}  Blocked:{totals['TOTAL_BLOCKED']}  Failed:{totals['TOTAL_FAILED']}  Unsupported:{totals['TOTAL_UNSUPPORTED']}", "white")
            cprint("-" * 71, "cyan")
            print()

            if max_loops > 0 and current_loop >= max_loops:
                cprint("[SYSTEM] Final cycle complete.", "cyan")
                break
            if stop_event.is_set():
                break

            # Sleep between cycles (adaptive)
            cycle_sleep = CFG.base_cycle_sleep
            if queue_size <= 2:
                cycle_sleep = 60
            elif queue_size >= 10:
                cycle_sleep = 15

            cprint(f"[SYSTEM] Resting {cycle_sleep}s...", "white")
            if interruptible_sleep(cycle_sleep):
                break
            clear_screen()

    except KeyboardInterrupt:
        cprint("\n[INTERRUPTED] Saving...", "yellow")
        stop_event.set()
    finally:
        # Always save stats and cleanup
        try:
            if CFG.dead_tmp.exists():
                CFG.dead_tmp.unlink()
        except:
            pass
        save_stats({"TOTAL_SESSIONS": total_sessions, **totals})
        with proc_lock:
            if current_proc and current_proc.poll() is None:
                try:
                    current_proc.kill()
                except:
                    pass

    # --- Final summary ---
    print()
    cprint("=" * 79, "cyan")
    cprint("  SESSION COMPLETE (interrupted)" if stop_event.is_set() else "  SESSION COMPLETE", "cyan")
    cprint("=" * 79, "cyan")
    cprint(f"  Sessions : {total_sessions}", "white")
    cprint(f"  Success  : {totals['TOTAL_SUCCESS']}", "green")
    cprint(f"  404 Removed : {totals['TOTAL_404']}", "yellow")
    cprint(f"  Blocked  : {totals['TOTAL_BLOCKED']}", "yellow")
    cprint(f"  Failed      : {totals['TOTAL_FAILED']}", "red")
    cprint(f"  Unsupported : {totals['TOTAL_UNSUPPORTED']}", "yellow")
    cprint("=" * 79, "cyan")
    print()
    press_any_key_to_exit()

if __name__ == "__main__":
    main()
