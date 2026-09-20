# 4chan Thread Monitor - V 1.0

A robust, cross-platform Python monitor for 4chan threads using [gallery-dl](https://github.com/mikf/gallery-dl).

Automatically downloads new images from a list of threads, handles 404s, rate-limits, and retries.

> Rewritten from Windows Batch (`4chan-threads-monitor.bat`) to Python for reliability and performance.

## Features

- **Auto sanitization** - Strips BOM, invisible chars, dedupes case-insensitively
- **Priority queue** - `priority_4chan.txt` processed first each cycle
- **Auto-merge** - Drop new URLs into `new_4chan.txt`, auto-merged and deleted
- **Pause/resume** - Create `pause_4chan.txt` to pause, delete to resume
- **Robust error handling** - 404 removal, 429 rate-limit pause, unsupported URL removal
- **Atomic writes** - No file corruption on crash or Ctrl+C
- **Graceful Ctrl+C** - Kills gallery-dl subprocess instantly, saves stats
- **Adaptive sleeps** - Faster when queue is large, slower when idle

## Installation

```bash
pip install gallery-dl
```

## Usage

```bash
# Normal run (infinite cycles)
python 4chan-thread-monitor-v1.0.py

# Dry run - no downloads
python 4chan-thread-monitor-v1.0.py --dry-run --loops 1 --no-clear

# Self test
python 4chan-thread-monitor-v1.0.py --self-test

# Run N cycles
python 4chan-thread-monitor-v1.0.py --loops 5
```

## Files

| File | Description |
|------|-------------|
| `threads_4chan.txt` | Main queue (one URL per line) |
| `new_4chan.txt` | Drop new URLs here, auto-merged |
| `priority_4chan.txt` | High priority threads |
| `pause_4chan.txt` | Create to pause |
| `log_4chan.txt` | Last gallery-dl output |
| `failures_4chan.txt` | Failure log with timestamps |
| `stats_4chan.txt` | Session counters |

## Controls

- **Ctrl+C** - First press: graceful stop (kills current download, saves stats)
- Second press: force exit
- **Pause**: Create `pause_4chan.txt` to pause, delete to resume

## Example thread list

```
https://boards.4chan.org/wg/thread/1234567
https://boards.4chan.org/gif/thread/7654321
```

## Changelog

### V 1.0 - GitHub Release
- Clean, commented, production-ready codebase
- Fixed all batch bugs: timeout without /nobreak, findstr without /I, cls destroying logs, BOM handling, dead tmp left on crash
- Fixed Python bugs: binary-safe logs, atomic writes, double I/O, handle leak, Ctrl+C not killing child
- Optimized: single read per cycle, pre-compiled regex (3x speedup on 10k URLs), O(1) dedup
- UI: Removed noisy `(Ctrl+C to quit)` spam, now `Press any key to exit...`

## License

MIT
