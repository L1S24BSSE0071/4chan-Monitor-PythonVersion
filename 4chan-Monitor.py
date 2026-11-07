#!/usr/bin/env python3

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

VERSION = "V 1.0"


@dataclass(frozen=True)
class Config:
    list_file: Path = Path("threads_4chan.txt")
    log_file: Path = Path("log_4chan.txt")
    new_box: Path = Path("new_4chan.txt")
    priority_box: Path = Path("priority_4chan.txt")
    pause_file: Path = Path("pause_4chan.txt")
    stats_file: Path = Path("stats_4chan.txt")
    fail_log: Path = Path("failures_4chan.txt")
    max_retries: int = 3
    base_thread_sleep: int = 8
    min_thread_sleep: int = 3
    max_thread_sleep: int = 15
    base_cycle_sleep: int = 30
    busy_cycle_sleep: int = 15
    idle_cycle_sleep: int = 60
    gallery_timeout: int = 600
    blocked_sleep: int = 300
    retry_sleep: int = 10
    sleep_request: str = "2.0"
    use_archive_fallback: bool = True


CFG = Config()

URL_RE = re.compile(r"^https?://[^\s/]+", re.IGNORECASE)
WS_RE = re.compile(r"\s+")
NOT_PRINTABLE_RE = re.compile(r"[^\x20-\x7E]+")
THREAD_RE = re.compile(r"^https?://boards\.4chan(?:nel)?\.org/([a-z0-9]+)/thread/(\d+)(?:[/?#]|$)", re.IGNORECASE)

ARCHIVE_MAP = {
    "desuarchive.org": {
        "a", "aco", "an", "c", "co", "d", "fit", "g", "his", "int",
        "k", "m", "mlp", "mu", "q", "qa", "r9k", "tg", "trash", "vr", "wsg",
    },
    "arch.b4k.co": {"v", "vg", "vm", "vmg", "vp", "vrpg", "vst"},
    "4plebs.org": {"adv", "f", "hr", "mlpol", "mo", "o", "pol", "s4s", "sp", "trv", "tv", "x"},
    "warosu.org": {"3", "biz", "cgl", "ck", "diy", "fa", "ic", "jp", "lit", "sci", "vt"},
    "archive.palanq.win": {"e", "n", "news", "out", "p", "pw", "toy", "vip", "w", "wg", "wsr"},
    "thebarchive.com": {"b", "bant"},
    "archiveofsins.com": {"h", "hc", "hm", "i", "lgbt", "r", "s", "soc", "t", "u", "y"},
}

THREAD_GONE_RE = re.compile(
    r"\[(?!(?:downloader\b|download\]))[^\]]+\]\[error\]\s+(?:HttpError:\s*'?(?:404|410)\b|NotFoundError:)",
    re.I,
)
BLOCKED_RE = re.compile(r"\]\[(?:error|warning)\]\s+(?:(?:HttpError:\s*)?'?(?:429|403)\b|ChallengeError:)", re.I)
UNSUPPORTED_RE = re.compile(r"\]\[error\]\s+Unsupported URL", re.I)
ERROR_LINE_RE = re.compile(r"\]\[error\]", re.I)
FAILED_DL_RE = re.compile(r"\]\[error\]\s+Failed to download", re.I)
DL_WARN_RE = re.compile(r"\[downloader[^\]]*\]\[warning\]", re.I)
DL_404_WARN_RE = re.compile(r"\[downloader[^\]]*\]\[warning\]\s+(?:HttpError:\s*)?'?(?:404|410)\b", re.I)
DL_404_ERR_RE = re.compile(r"\[downloader[^\]]*\]\[error\]\s+(?:HttpError:\s*)?'?(?:404|410)\b", re.I)
FILE_GONE_RE = re.compile(
    r"(?:\]\[warning\]|\[downloader[^\]]*\]\[error\])\s+(?:HttpError:\s*)?'?(?:404|410)\b",
    re.I,
)

COLORS = {
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "white": "\033[97m",
    "gray": "\033[90m",
    "reset": "\033[0m",
}
IS_TTY = sys.stdout.isatty()
SPINNER = "|/-\\"


def _enable_ansi() -> bool:
    if os.environ.get("NO_COLOR") or not IS_TTY:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            return False
    return True


USE_COLOR = _enable_ansi()


def cprint(msg: str = "", color: str = "white") -> None:
    if USE_COLOR:
        msg = f"{COLORS.get(color, '')}{msg}{COLORS['reset']}"
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def press_any_key_to_exit() -> None:
    cprint("Press any key to exit...", "white")
    with contextlib.suppress(EOFError, OSError, KeyboardInterrupt):
        try:
            import msvcrt
            msvcrt.getch()
        except ImportError:
            input()


stop_event = threading.Event()
current_proc: Optional[subprocess.Popen] = None


def interruptible_sleep(seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    return stop_event.is_set()


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):
            proc.kill()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def safe_write(path: Path, content: str) -> bool:
    try:
        atomic_write(path, content)
        return True
    except OSError as e:
        cprint(f"[WARN] Could not write {path}: {e}", "yellow")
        return False


def cleanup_stale_temp() -> None:
    err_file = CFG.log_file.with_name(CFG.log_file.name + ".err")
    dirs = {
        p.parent
        for p in (CFG.list_file, CFG.log_file, CFG.new_box, CFG.priority_box, CFG.pause_file, CFG.stats_file, CFG.fail_log)
    }
    temp_files = [p for d in dirs if d.exists() for p in d.glob(".*_4chan.txt.*.tmp")]
    for p in (*temp_files, err_file):
        with contextlib.suppress(OSError):
            p.unlink()


def dedupe_key(url: str) -> str:
    m = THREAD_RE.match(url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}"
    return url.split("#", 1)[0].rstrip("/").lower()


def archive_urls_for(url: str) -> List[str]:
    m = THREAD_RE.match(url)
    if not m:
        return []
    board, tid = m.group(1).lower(), m.group(2)
    urls: List[str] = []
    for host, boards in ARCHIVE_MAP.items():
        if board in boards:
            urls.append(f"https://{host}/{board}/thread/{tid}")
    urls.append(f"https://archived.moe/{board}/thread/{tid}")
    return urls


def _extract_url(line: str) -> Optional[str]:
    tokens = NOT_PRINTABLE_RE.sub("", WS_RE.sub(" ", line)).split("#", 1)[0].replace('"', " ").replace("'", " ").split()
    if not tokens:
        return None
    u = tokens[0]
    m_url = URL_RE.match(u)
    if not m_url:
        return None
    u = m_url.group(0).lower() + u[m_url.end():]
    m_th = THREAD_RE.match(u)
    if m_th:
        u = u[:m_th.start(2)].lower() + u[m_th.start(2):]
    return u


def parse_urls(text: str) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for line in text.splitlines():
        cleaned = _extract_url(line)
        if not cleaned:
            continue
        key = dedupe_key(cleaned)
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _decode_bytes(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="ignore")
    return raw.decode("utf-8-sig", errors="ignore")


def read_urls(path: Path) -> Optional[List[str]]:
    try:
        return parse_urls(_decode_bytes(path.read_bytes()))
    except FileNotFoundError:
        return []
    except OSError:
        return None


def check_gallery_dl() -> str:
    cprint("[STARTUP] Checking gallery-dl...", "white")
    if not shutil.which("gallery-dl"):
        cprint("[FATAL] gallery-dl not found in PATH. Install with: pip install gallery-dl", "red")
        sys.exit(1)
    try:
        r = subprocess.run(
            ["gallery-dl", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        ver = (r.stdout or r.stderr or "").strip()
        if r.returncode != 0 or not ver:
            raise RuntimeError(ver or f"exit code {r.returncode}")
        ver = ver.splitlines()[0].strip()
        cprint(f"[STARTUP] Found: {ver}", "green")
        return ver
    except Exception as e:
        cprint(f"[FATAL] gallery-dl check failed: {e}", "red")
        sys.exit(1)


def _version_tuple(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v.split("-")[0])[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def check_pypi_update(ver_raw: str) -> None:
    ver = re.sub(r"^gallery-dl\s*", "", ver_raw.strip(), flags=re.I)
    try:
        import json
        import urllib.request
        req = urllib.request.Request("https://pypi.org/pypi/gallery-dl/json", headers={"User-Agent": "4chan-monitor/1.1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            latest = json.loads(resp.read().decode())["info"]["version"]
        if _version_tuple(latest) > _version_tuple(ver):
            cprint(f"[UPDATE] gallery-dl {latest} available (you have {ver}) -> pip install -U gallery-dl", "yellow")
        else:
            cprint(f"[STARTUP] gallery-dl is up to date ({ver})", "green")
    except Exception:
        cprint("[STARTUP] Update check skipped (offline)", "gray")
    print()


def sanitize_list() -> None:
    cleanup_stale_temp()
    if CFG.priority_box.exists():
        prio_urls = read_urls(CFG.priority_box)
        if prio_urls is not None:
            safe_write(CFG.priority_box, "\n".join(prio_urls) + ("\n" if prio_urls else ""))
    if not CFG.list_file.exists():
        return
    urls = read_urls(CFG.list_file)
    if urls is None:
        cprint(f"[WARN] {CFG.list_file} is unreadable right now; skipping sanitize", "yellow")
        return
    if safe_write(CFG.list_file, "\n".join(urls) + ("\n" if urls else "")):
        cprint(f"[STARTUP] List sanitized: {len(urls)} URLs", "green")
        print()


DEFAULT_STATS = {
    "TOTAL_SESSIONS": 0,
    "TOTAL_CYCLES": 0,
    "TOTAL_SUCCESS": 0,
    "TOTAL_404": 0,
    "TOTAL_ARCHIVED": 0,
    "TOTAL_BLOCKED": 0,
    "TOTAL_FAILED": 0,
    "TOTAL_UNSUPPORTED": 0,
}
COUNTER_MAP = {
    "TOTAL_SUCCESS": "CYCLE_SUCCESS",
    "TOTAL_404": "CYCLE_404",
    "TOTAL_ARCHIVED": "CYCLE_ARCHIVED",
    "TOTAL_BLOCKED": "CYCLE_BLOCKED",
    "TOTAL_FAILED": "CYCLE_FAILED",
    "TOTAL_UNSUPPORTED": "CYCLE_UNSUPPORTED",
}


def load_stats() -> Dict[str, int]:
    stats = DEFAULT_STATS.copy()
    try:
        lines = _decode_bytes(CFG.stats_file.read_bytes()).splitlines()
    except OSError:
        return stats
    for line in lines:
        k, sep, v = line.partition("=")
        k = k.strip()
        if sep and k in stats:
            with contextlib.suppress(ValueError):
                stats[k] = max(0, int(v.strip()))
    return stats


def save_stats(stats: Dict[str, int]) -> None:
    safe_write(CFG.stats_file, "\n".join(f"{k}={v}" for k, v in stats.items()) + "\n")


def absorb_new_box() -> int:
    if not CFG.new_box.exists():
        return 0
    try:
        raw = CFG.new_box.read_bytes()
    except OSError:
        return 0
    existing = read_urls(CFG.list_file)
    if existing is None:
        return 0
    known = {dedupe_key(u) for u in existing}
    added: List[str] = []
    events: List[tuple] = []
    dupes = 0
    for line in _decode_bytes(raw).splitlines():
        url = _extract_url(line)
        if not url:
            continue
        key = dedupe_key(url)
        if key in known:
            events.append((f"  [DUPLICATE] {url}", "yellow"))
            dupes += 1
        else:
            known.add(key)
            added.append(url)
            events.append((f"  [ADDED] {url}", "green"))
    if added and not safe_write(CFG.list_file, "\n".join(existing + added) + "\n"):
        return 0
    for msg, color in events:
        cprint(msg, color)
    if added or dupes:
        cprint(f"[INFO] Merge complete: {len(added)} added, {dupes} duplicates", "cyan")
        print()
    with contextlib.suppress(OSError):
        if CFG.new_box.read_bytes() == raw:
            CFG.new_box.unlink()
    return len(added)


def wait_if_paused() -> None:
    if not CFG.pause_file.exists():
        return
    cprint(f"[PAUSED] Delete {CFG.pause_file} to resume...", "yellow")
    while CFG.pause_file.exists():
        if interruptible_sleep(0.5):
            return
    cprint("[RESUMED]", "green")
    print()


def remove_dead(dead_urls: List[str]) -> int:
    dead_keys = {dedupe_key(u) for u in dead_urls if URL_RE.match(u)}
    if not dead_keys:
        return 0
    removed_keys: Set[str] = set()
    for path in (CFG.list_file, CFG.priority_box):
        current = read_urls(path)
        if not current:
            continue
        kept = [u for u in current if dedupe_key(u) not in dead_keys]
        if len(kept) < len(current) and safe_write(path, "\n".join(kept) + ("\n" if kept else "")):
            removed_keys.update(dedupe_key(u) for u in current if dedupe_key(u) in dead_keys)
    if removed_keys:
        cprint(f"[CLEANUP] Removed {len(removed_keys)} dead threads from list", "yellow")
    return len(removed_keys)


@dataclass
class RunResult:
    code: int
    out: str = ""
    err: str = ""
    interrupted: bool = False
    timed_out: bool = False


def _clear_spinner() -> None:
    if IS_TTY:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()


def run_gallery_dl(url: str) -> RunResult:
    global current_proc
    if stop_event.is_set():
        return RunResult(-1, interrupted=True)
    err_path = CFG.log_file.with_name(CFG.log_file.name + ".err")
    cmd = ["gallery-dl", "--sleep-request", CFG.sleep_request, url]
    popen_kw = (
        {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    start = time.monotonic()
    timed_out = False
    code = 1
    try:
        with CFG.log_file.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL, **popen_kw)
            current_proc = proc
            spin = 0
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if stop_event.is_set():
                    _clear_spinner()
                    cprint("  [INTERRUPTED] Stopping gallery-dl", "yellow")
                    kill_tree(proc)
                    break
                elapsed = int(time.monotonic() - start)
                if elapsed >= CFG.gallery_timeout:
                    _clear_spinner()
                    cprint(f"  [TIMEOUT] {CFG.gallery_timeout}s exceeded, killing process", "red")
                    timed_out = True
                    kill_tree(proc)
                    break
                if IS_TTY:
                    mins, secs = divmod(elapsed, 60)
                    sys.stdout.write(f"\r  [{SPINNER[spin % 4]}] Scraping... [{mins:02d}:{secs:02d}]  ")
                    sys.stdout.flush()
                    spin += 1
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            _clear_spinner()
            code = proc.returncode if proc.returncode is not None else 1
    except FileNotFoundError:
        cprint("  [FATAL] gallery-dl not found", "red")
        return RunResult(1, err="[gallery-dl][error] gallery-dl executable not found\n")
    except OSError as e:
        if stop_event.is_set():
            return RunResult(-1, interrupted=True)
        cprint(f"  [ERROR] {e}", "red")
        return RunResult(1, err=f"[os][error] {e}\n")
    finally:
        current_proc = None
        out_bytes = b""
        err_bytes = b""
        with contextlib.suppress(OSError):
            out_bytes = CFG.log_file.read_bytes()
        with contextlib.suppress(OSError):
            err_bytes = err_path.read_bytes()
            if err_bytes:
                with CFG.log_file.open("ab") as f:
                    f.write(err_bytes)
        with contextlib.suppress(OSError):
            err_path.unlink()
    if stop_event.is_set():
        return RunResult(-1, interrupted=True)
    return RunResult(code, out_bytes.decode("utf-8", "ignore"), err_bytes.decode("utf-8", "ignore"), timed_out=timed_out)


def combined_output(res: RunResult) -> str:
    chunks = []
    if res.out:
        chunks.append(res.out)
    if res.err:
        chunks.append(res.err)
    return "\n".join(chunks)


def classify(res: RunResult) -> str:
    log_text = combined_output(res)
    if UNSUPPORTED_RE.search(log_text):
        return "unsupported"
    if THREAD_GONE_RE.search(log_text):
        return "dead"
    if res.code == 0:
        return "partial" if FILE_GONE_RE.search(log_text) else "ok"
    if BLOCKED_RE.search(log_text):
        return "blocked"
    failed = len(FAILED_DL_RE.findall(log_text))
    dl_404_errs = len(DL_404_ERR_RE.findall(log_text))
    other_errors = len(ERROR_LINE_RE.findall(log_text)) - failed - dl_404_errs
    non_404_dl_warns = len(DL_WARN_RE.findall(log_text)) - len(DL_404_WARN_RE.findall(log_text))
    if (failed or dl_404_errs) and other_errors == 0 and non_404_dl_warns == 0 and len(FILE_GONE_RE.findall(log_text)) >= failed:
        return "partial"
    return "error"


def last_error_line(err: str) -> str:
    all_lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    err_lines = [ln for ln in all_lines if ERROR_LINE_RE.search(ln)]
    if err_lines:
        last_err = err_lines[-1]
        if FAILED_DL_RE.search(last_err):
            warns = [ln for ln in all_lines if DL_WARN_RE.search(ln)]
            if warns:
                non_404 = [ln for ln in warns if not DL_404_WARN_RE.search(ln)]
                chosen = non_404[-1] if non_404 else warns[-1]
                return f"{chosen} -> {last_err}"[:200]
        return last_err[:200]
    return all_lines[-1][:200] if all_lines else ""


def log_failure(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with contextlib.suppress(OSError):
        with CFG.fail_log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{ts}] {msg}\n")


def try_archive_fallback(url: str, counters: Dict[str, int]) -> Optional[bool]:
    arch_urls = archive_urls_for(url)
    if not arch_urls:
        return False
    cprint("  [404] Thread dead on 4chan, trying archive fallback...", "yellow")
    empty_ok_url: Optional[str] = None
    for arch_url in arch_urls:
        wait_if_paused()
        if stop_event.is_set():
            return None
        cprint(f"  [ARCHIVE] {arch_url}", "cyan")
        res = run_gallery_dl(arch_url)
        if res.interrupted or stop_event.is_set():
            return None
        if res.timed_out:
            continue
        kind = classify(res)
        if kind in ("ok", "partial") and res.out.strip():
            cprint("  [ARCHIVE OK] Recovered from archive, removing from active list", "green")
            log_failure(f"404 ARCHIVED ({arch_url}): {url}")
            counters.setdefault("CYCLE_ARCHIVED", 0)
            counters["CYCLE_ARCHIVED"] += 1
            return True
        if kind == "ok" and empty_ok_url is None:
            empty_ok_url = arch_url
    if empty_ok_url is not None:
        cprint("  [ARCHIVE OK] Recovered from archive, removing from active list", "green")
        log_failure(f"404 ARCHIVED ({empty_ok_url}): {url}")
        counters.setdefault("CYCLE_ARCHIVED", 0)
        counters["CYCLE_ARCHIVED"] += 1
        return True
    cprint("  [ARCHIVE MISS] Not found in archive, removing from list", "yellow")
    return False


def process_thread(
    url: str,
    label: str,
    dry_run: bool,
    counters: Dict[str, int],
    dead_list: List[str],
    thread_sleep: int,
    has_more_work: bool = True,
    use_archive: bool = True,
) -> bool:
    wait_if_paused()
    if stop_event.is_set():
        return False
    cprint(f"[{label}] {url}", "white")
    if dry_run:
        cprint(f'  [DRY-RUN] gallery-dl --sleep-request {CFG.sleep_request} "{url}"', "yellow")
        print()
        counters["CYCLE_SKIPPED"] += 1
        return True
    for attempt in range(CFG.max_retries + 1):
        if attempt > 0:
            cprint(f"  [RETRY {attempt}/{CFG.max_retries}] {CFG.retry_sleep}s...", "yellow")
            if interruptible_sleep(CFG.retry_sleep):
                return False
            wait_if_paused()
            if stop_event.is_set():
                return False
        res = run_gallery_dl(url)
        if res.interrupted or stop_event.is_set():
            return False
        if res.timed_out:
            log_failure(f"TIMEOUT after {CFG.gallery_timeout}s: {url}")
            cprint("  [FAILED] Timed out, keeping in queue for next cycle", "red")
            print()
            counters["CYCLE_FAILED"] += 1
            return True
        kind = classify(res)
        if kind in ("ok", "partial"):
            if kind == "partial":
                cprint("  [OK] Downloaded (some images no longer exist on the server)", "green")
            else:
                cprint("  [OK] Downloaded successfully", "green")
            counters["CYCLE_SUCCESS"] += 1
            if thread_sleep > 0:
                cprint(f"  [PAUSE] {thread_sleep}s...", "white")
                print()
                return not interruptible_sleep(thread_sleep)
            print()
            return True
        if kind == "unsupported":
            log_failure(f"UNSUPPORTED: {url}")
            dead_list.append(url)
            cprint("  [UNSUPPORTED] URL not supported, removing", "yellow")
            print()
            counters["CYCLE_UNSUPPORTED"] += 1
            return True
        if kind == "dead":
            if use_archive and CFG.use_archive_fallback and archive_urls_for(url):
                archived = try_archive_fallback(url, counters)
                if archived is None:
                    return False
                if not archived:
                    log_failure(f"404 REMOVED (archive miss): {url}")
            else:
                log_failure(f"404 REMOVED: {url}")
                cprint("  [404] Thread not found, removing from list", "yellow")
            dead_list.append(url)
            print()
            counters["CYCLE_404"] += 1
            return True
        if kind == "blocked":
            log_failure(f"BLOCKED: {url}")
            counters["CYCLE_BLOCKED"] += 1
            if not has_more_work:
                cprint("  [BLOCKED] Rate-limited", "red")
                print()
                return True
            wait = CFG.blocked_sleep
            wait_str = f"{wait // 60}m" if wait >= 60 and wait % 60 == 0 else f"{wait}s"
            cprint(f"  [BLOCKED] Rate-limited, pausing {wait_str}", "red")
            print()
            return not interruptible_sleep(wait)
        if attempt == CFG.max_retries:
            detail = last_error_line(combined_output(res))
            log_failure(f"FAILED max retries: {url}" + (f" | {detail}" if detail else ""))
            cprint("  [FAILED] Keeping in queue for next cycle", "red")
            print()
            counters["CYCLE_FAILED"] += 1
            return True
    return True


def run_self_test() -> int:
    dead_err = "[4chan][error] HttpError: '404 Not Found' for 'https://a.4cdn.org/g/thread/2.json'\n"
    file_gone = (
        "[downloader.http][warning] '404 Not Found' for 'https://i.4cdn.org/g/9.jpg'\n"
        "[download][error] Failed to download 9.jpg\n"
    )
    dl_err_gone = (
        "[downloader.http][error] HttpError: '404 Not Found' for 'https://i.4cdn.org/g/9.jpg'\n"
        "[download][error] Failed to download 9.jpg\n"
    )
    mixed_dl_err = (
        "[downloader.http][warning] '404 Not Found' for 'https://i.4cdn.org/g/9.jpg' (1/2)\n"
        "[downloader.http][warning] '404 Not Found' for 'https://i.4cdn.org/g/9.jpg' (2/2)\n"
        "[download][error] Failed to download 9.jpg\n"
        "[downloader.http][warning] ReadTimeoutError: timed out (1/2)\n"
        "[download][error] Failed to download 10.jpg\n"
    )
    text = (
        "\ufeffhttps://boards.4chan.org/g/thread/123#p9\n"
        "https://boards.4channel.org/g/thread/123/some-slug\n"
        '\ufeff   " https://boards.4chan.org/a/thread/5 "   # anime\n'
        "HTTPS://BOARDS.4CHAN.ORG/V/THREAD/77\tinline-tab-note\n"
        "https://\n"
        "not a url\n\n"
    )
    cases = [
        ("missing file -> []", read_urls(Path("nonexistent-file")) == []),
        (
            "parse: dedupe + BOM + quotes + tabs + case + fragments + comments",
            parse_urls(text)
            == [
                "https://boards.4chan.org/g/thread/123",
                "https://boards.4chan.org/a/thread/5",
                "https://boards.4chan.org/v/thread/77",
            ],
        ),
        (
            "healthy thread whose filenames contain 404/429/403 stays alive",
            classify(RunResult(0, out="./gallery-dl/4chan/g/1/1700004041404.jpg\n./x/1429403.png\n")) == "ok",
        ),
        ("thread 404", classify(RunResult(4, err=dead_err)) == "dead"),
        ("429", classify(RunResult(4, err="[4chan][error] HttpError: '429 Too Many Requests' for 'u'")) == "blocked"),
        (
            "downloader 429 failure is blocked",
            classify(RunResult(4, err="[downloader.http][warning] '429 Too Many Requests' for 'u'\n[download][error] Failed to download 1.jpg")) == "blocked",
        ),
        (
            "429 warning in stdout is still blocked",
            classify(RunResult(4, out="[downloader.http][warning] '429 Too Many Requests' for 'u'\n[download][error] Failed to download 1.jpg\n")) == "blocked",
        ),
        (
            "recovered 429 warning with exit code 0 is ok",
            classify(RunResult(0, err="[downloader.http][warning] '429 Too Many Requests' for 'u' (1/5)\n")) == "ok",
        ),
        (
            "404 warning in stdout is still treated as partial",
            classify(RunResult(4, out="[downloader.http][warning] '404 Not Found' for 'https://i.4cdn.org/g/9.jpg'\n[download][error] Failed to download 9.jpg\n")) == "partial",
        ),
        (
            "cloudflare",
            classify(RunResult(8, err="[4chan][error] ChallengeError: cloudflare (403 Forbidden) for 'u'")) == "blocked",
        ),
        ("unsupported", classify(RunResult(64, err="[gallery-dl][error] Unsupported URL 'https://x.y/z'")) == "unsupported"),
        ("deleted image in live thread is not a dead thread", classify(RunResult(4, err=file_gone)) == "partial"),
        ("downloader 404 error is partial, not dead thread", classify(RunResult(4, err=dl_err_gone)) == "partial"),
        ("404 image + timeout image is retried as error", classify(RunResult(4, err=mixed_dl_err)) == "error"),
        ("network error is retried", classify(RunResult(4, err="[4chan][error] ConnectionError: boom")) == "error"),
        ("version tuple padding", _version_tuple("1.28") == _version_tuple("1.28.0") and _version_tuple("1.29") > _version_tuple("1.28.9")),
        ("last_error_line includes downloader warning context", "ReadTimeoutError" in last_error_line(mixed_dl_err)),
        (
            "archive fallback URL generation",
            archive_urls_for("https://boards.4chan.org/g/thread/123/slug")
            == ["https://desuarchive.org/g/thread/123", "https://archived.moe/g/thread/123"]
            and archive_urls_for("https://boards.4chan.org/wg/thread/99")
            == ["https://archive.palanq.win/wg/thread/99", "https://archived.moe/wg/thread/99"],
        ),
    ]
    failed = [name for name, ok in cases if not ok]
    for name, ok in cases:
        cprint(f"  [{'PASS' if ok else 'FAIL'}] {name}", "green" if ok else "red")
    if failed:
        cprint(f"Self-test FAILED ({len(failed)}/{len(cases)})", "red")
        return 1
    cprint("Self-test passed!", "green")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor and download 4chan threads using gallery-dl.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded without downloading")
    parser.add_argument("--loops", type=int, default=None, help="Number of cycles (0 = infinite)")
    parser.add_argument("--no-clear", action="store_true", help="Don't clear screen between cycles")
    parser.add_argument("--no-pause", action="store_true", help="Don't wait for a key press before exiting")
    parser.add_argument("--no-update-check", action="store_true", help="Skip the PyPI update check at startup")
    parser.add_argument("--no-archive", action="store_true", help="Skip archive fallback when a thread 404s")
    parser.add_argument("--self-test", action="store_true", help="Run internal self-tests")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    def clear_screen() -> None:
        if args.no_clear or not IS_TTY:
            return
        if USE_COLOR:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        else:
            os.system("cls" if os.name == "nt" else "clear")

    clear_screen()
    cprint("=" * 79, "cyan")
    cprint(f"  4CHAN THREAD MONITOR - {VERSION}", "cyan")
    cprint("=" * 79, "cyan")
    print()
    if args.dry_run:
        cprint("[DRY-RUN MODE] No files will be downloaded", "yellow")
        print()
        cprint("[STARTUP] dry-run (gallery-dl check skipped)", "gray")
        print()
    else:
        ver = check_gallery_dl()
        if not args.no_update_check:
            check_pypi_update(ver)
    sanitize_list()

    if args.loops is not None:
        max_loops = max(0, args.loops)
    else:
        cprint("How many monitor cycles to run? (0 = infinite)", "white")
        try:
            max_loops = max(0, int(input("Loop Count: ").strip() or "0"))
        except ValueError:
            max_loops = 0
        except EOFError:
            max_loops = 1
        except KeyboardInterrupt:
            print()
            cprint("[EXIT] Interrupted at prompt, exiting.", "yellow")
            sys.exit(0)
        print()
        clear_screen()

    totals = load_stats()
    totals["TOTAL_SESSIONS"] += 1

    def on_signal(signum, frame):
        if stop_event.is_set():
            cprint("\n[FORCE EXIT] Terminating...", "red")
            proc = current_proc
            if proc is not None:
                kill_tree(proc)
            os._exit(1)
        stop_event.set()
        cprint("\n[INTERRUPTED] Stopping gracefully...", "yellow")

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, on_signal)
        for sig_name in ("SIGBREAK", "SIGTERM"):
            if hasattr(signal, sig_name):
                signal.signal(getattr(signal, sig_name), on_signal)

    current_loop = 0

    try:
        while not stop_event.is_set():
            wait_if_paused()
            if stop_event.is_set():
                break
            absorb_new_box()
            queue_urls = read_urls(CFG.list_file)
            prio_urls = read_urls(CFG.priority_box)
            if queue_urls is None or prio_urls is None:
                cprint("[WARN] Thread list is locked/unreadable, retrying in 10s", "yellow")
                if args.dry_run or interruptible_sleep(10):
                    break
                continue
            prio_keys = {dedupe_key(u) for u in prio_urls}
            jobs = [("PRIORITY", u) for u in prio_urls] + [
                ("NORMAL", u) for u in queue_urls if dedupe_key(u) not in prio_keys
            ]
            if not jobs:
                if args.dry_run or max_loops > 0:
                    cprint(f"[WAIT] No threads queued ({CFG.list_file}).", "yellow")
                    break
                cprint(f"[WAIT] No threads queued ({CFG.list_file}). Waiting 60s...", "yellow")
                if interruptible_sleep(60):
                    break
                clear_screen()
                continue
            queue_size = len(jobs)
            current_loop += 1
            totals["TOTAL_CYCLES"] += 1
            thread_sleep = CFG.base_thread_sleep
            cycle_sleep = CFG.base_cycle_sleep
            if queue_size <= 2:
                thread_sleep, cycle_sleep = CFG.max_thread_sleep, CFG.idle_cycle_sleep
            elif queue_size >= 10:
                thread_sleep, cycle_sleep = CFG.min_thread_sleep, CFG.busy_cycle_sleep
            cprint("=" * 79, "cyan")
            cprint(
                f" CYCLE: {current_loop} | TIME: {datetime.now().strftime('%H:%M:%S')} | QUEUE: {queue_size} | SLEEP: {thread_sleep}s",
                "cyan",
            )
            if args.dry_run:
                cprint(" MODE: DRY-RUN", "yellow")
            cprint("=" * 79, "cyan")
            print()
            counters = {
                "CYCLE_SUCCESS": 0,
                "CYCLE_404": 0,
                "CYCLE_ARCHIVED": 0,
                "CYCLE_BLOCKED": 0,
                "CYCLE_FAILED": 0,
                "CYCLE_SKIPPED": 0,
                "CYCLE_UNSUPPORTED": 0,
            }
            dead_accum: List[str] = []
            is_last_cycle = max_loops > 0 and current_loop >= max_loops
            for idx, (label, url) in enumerate(jobs):
                not_last_job = idx < queue_size - 1
                pause_s = thread_sleep if not_last_job else 0
                has_more = not_last_job or not is_last_cycle
                if not process_thread(
                    url, label, args.dry_run, counters, dead_accum, pause_s, has_more, not args.no_archive
                ):
                    break
            removed_count = remove_dead(dead_accum) if dead_accum else 0
            for total_key, cycle_key in COUNTER_MAP.items():
                totals[total_key] += counters[cycle_key]
            if not args.dry_run:
                save_stats(totals)
            if stop_event.is_set():
                cprint("\n[INTERRUPTED] Exiting..." if args.dry_run else "\n[INTERRUPTED] Stats saved, exiting...", "yellow")
                break
            print()
            cprint("-" * 71, "cyan")
            cprint(f" CYCLE {current_loop} SUMMARY", "cyan")
            cprint("-" * 71, "cyan")
            cprint(f"  Success     : {counters['CYCLE_SUCCESS']}", "green")
            cprint(f"  404 Removed : {counters['CYCLE_404']}", "yellow")
            cprint(f"  Archived    : {counters['CYCLE_ARCHIVED']}", "cyan")
            cprint(f"  Blocked     : {counters['CYCLE_BLOCKED']}", "yellow")
            cprint(f"  Unsupported : {counters['CYCLE_UNSUPPORTED']}", "yellow")
            cprint(f"  Failed      : {counters['CYCLE_FAILED']}", "red")
            cprint(f"  Skipped     : {counters['CYCLE_SKIPPED']}", "gray")
            cprint("-" * 71, "cyan")
            cprint(
                f" ALL-TIME  Cycles:{totals['TOTAL_CYCLES']}  OK:{totals['TOTAL_SUCCESS']}  404:{totals['TOTAL_404']}  Archived:{totals['TOTAL_ARCHIVED']}  Blocked:{totals['TOTAL_BLOCKED']}  Failed:{totals['TOTAL_FAILED']}  Unsupported:{totals['TOTAL_UNSUPPORTED']}",
                "white",
            )
            cprint("-" * 71, "cyan")
            print()
            if is_last_cycle:
                cprint("[SYSTEM] Max cycles reached. Exiting.", "cyan")
                break
            if max_loops > 0 and removed_count > 0 and not (
                read_urls(CFG.list_file) or read_urls(CFG.priority_box) or CFG.new_box.exists()
            ):
                cprint("[WAIT] No threads remaining in queue. Exiting.", "yellow")
                break
            cprint(f"[SYSTEM] Resting {cycle_sleep}s...", "white")
            if interruptible_sleep(cycle_sleep):
                break
            clear_screen()
    except KeyboardInterrupt:
        cprint("\n[INTERRUPTED] Saving...", "yellow")
        stop_event.set()
    finally:
        if not args.dry_run:
            save_stats(totals)
        proc = current_proc
        if proc is not None:
            kill_tree(proc)
    print()
    cprint("=" * 79, "cyan")
    cprint("  SESSION COMPLETE (interrupted)" if stop_event.is_set() else "  SESSION COMPLETE", "cyan")
    cprint("=" * 79, "cyan")
    cprint(f"  Sessions    : {totals['TOTAL_SESSIONS']}   (cycles: {totals['TOTAL_CYCLES']})", "white")
    cprint(f"  Success     : {totals['TOTAL_SUCCESS']}", "green")
    cprint(f"  404 Removed : {totals['TOTAL_404']}", "yellow")
    cprint(f"  Archived    : {totals['TOTAL_ARCHIVED']}", "cyan")
    cprint(f"  Blocked     : {totals['TOTAL_BLOCKED']}", "yellow")
    cprint(f"  Failed      : {totals['TOTAL_FAILED']}", "red")
    cprint(f"  Unsupported : {totals['TOTAL_UNSUPPORTED']}", "yellow")
    cprint("=" * 79, "cyan")
    print()
    if sys.stdin.isatty() and not args.no_pause and not stop_event.is_set():
        press_any_key_to_exit()


if __name__ == "__main__":
    main()
