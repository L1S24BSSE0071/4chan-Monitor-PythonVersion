# 4chan Thread Monitor

A robust, cross-platform Python monitor that keeps a list of 4chan threads downloaded using [gallery-dl](https://github.com/mikf/gallery-dl). It loops over your thread list, pulls down new media, drops dead threads automatically, backs off when rate-limited, and survives crashes and Ctrl+C without corrupting its files.

This is a Python rewrite of an earlier Windows batch script (`4chan-threads-monitor.bat`), built for reliability and easier maintenance.

## Features

- **Self-cleaning thread list**: strips BOMs, invisible/non-printable characters and blank lines, keeps only `http(s)://` URLs, and removes duplicates (case-insensitive) while preserving order
- **Priority queue**: threads in `priority_4chan.txt` are processed first in every cycle
- **Drop-box for new URLs**: paste links into `new_4chan.txt` at any time; they are merged into the main list on the next cycle (duplicates are reported and skipped), then the file is deleted
- **Pause / resume**: create `pause_4chan.txt` to pause, delete it to resume, no need to stop the script
- **Smart error handling**: dead threads (404, expired, archived) and unsupported URLs are removed automatically, rate limits trigger a cool-down, and transient failures are retried
- **Adaptive pacing**: sleeps shorter when the queue is large and longer when it is small
- **Atomic file writes**: list and stats files are written via temp file + `os.replace`, so a crash mid-write can't corrupt them
- **Graceful Ctrl+C**: the first press kills the running `gallery-dl` process and saves stats; the second forces an immediate exit
- **Hang protection**: any single `gallery-dl` run is killed after 10 minutes
- **Persistent statistics**: lifetime counters survive between sessions
- **Dry-run mode** and a built-in **self-test**

## Requirements

- Python 3.7+
- [gallery-dl](https://github.com/mikf/gallery-dl) available on your `PATH`

The script itself uses only the Python standard library.

## Installation

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install gallery-dl
```

## Quick start

1. Create `threads_4chan.txt` in the folder you'll run the script from, with one thread URL per line:

   ```text
   https://boards.4chan.org/g/thread/123456789
   https://boards.4chan.org/wg/thread/987654321
   ```

2. Run the monitor:

   ```bash
   python 4chan-Monitor.py
   ```

3. When prompted, enter how many cycles to run (`0` = run forever).

Downloads are saved wherever gallery-dl is configured to put them. By default that is a `gallery-dl/` folder in the current working directory; change it in your [gallery-dl config](https://github.com/mikf/gallery-dl#configuration). gallery-dl skips files it has already downloaded, so re-checking a thread only fetches new media.

## Command-line options

| Option              | Description                                                                        |
| ------------------- | ---------------------------------------------------------------------------------- |
| `--loops N`         | Run `N` cycles, then exit. `0` means infinite. If omitted, you're asked at startup |
| `--dry-run`         | Print the `gallery-dl` command for each thread without downloading anything        |
| `--no-clear`        | Don't clear the terminal between cycles (useful for logs and CI)                   |
| `--no-pause`        | Don't wait for a key press before exiting                                          |
| `--no-update-check` | Skip the PyPI update check at startup                                              |
| `--self-test`       | Run the built-in sanity checks and exit                                            |
| `--help`            | Show usage information                                                             |

Examples:

```bash
python 4chan-Monitor.py                              # interactive
python 4chan-Monitor.py --loops 0                    # run forever, no prompt
python 4chan-Monitor.py --dry-run --loops 1 --no-clear
python 4chan-Monitor.py --self-test
```

> **Tip:** for unattended runs (services, scheduled tasks), pass `--loops` and `--no-pause` so the script doesn't wait for input at startup or exit.
>
> **Note:** `--dry-run` doesn't download anything or modify `stats_4chan.txt`, but it still sanitizes `threads_4chan.txt` and merges `new_4chan.txt`.

## Files

All files live in the directory you run the script from.

| File                 | Purpose                                                                                  |
| -------------------- | ---------------------------------------------------------------------------------------- |
| `threads_4chan.txt`  | Main queue, one URL per line. Cleaned on startup and updated as dead threads are removed |
| `priority_4chan.txt` | High-priority threads, processed first each cycle. Dead threads are removed automatically |
| `new_4chan.txt`      | Drop new URLs here. They are merged into the main list and the file is deleted           |
| `pause_4chan.txt`    | Create this file to pause; delete it to resume                                           |
| `log_4chan.txt`      | Raw output of the most recent `gallery-dl` run (overwritten for each thread)             |
| `failures_4chan.txt` | Append-only, timestamped record of removals, blocks and failures                         |
| `stats_4chan.txt`    | Lifetime counters (`key=value` format, written atomically)                               |

## How it works

Each cycle the monitor:

1. Waits if `pause_4chan.txt` exists
2. Merges any URLs from `new_4chan.txt` into the main list
3. Reads the main and priority lists and picks pacing based on queue size
4. Processes `priority_4chan.txt` first, then the main list, running `gallery-dl --sleep-request 2.0 <url>` for each thread
5. Removes dead threads from the queue files, saves stats, prints a cycle summary
6. Rests before starting the next cycle

### Result handling

| What happened                                                              | Action                                                                      |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Exit code 0                                                                | Counted as success                                                          |
| Deleted individual image (`404`/`410` warning) in an otherwise live thread | Counted as success (partial download)                                       |
| Output contains `Unsupported URL`                                          | Removed from list                                                           |
| Output contains thread `404`/`410` `HttpError` or `NotFoundError`          | Removed from list                                                           |
| Output contains `429`/`403` `HttpError` or `ChallengeError`                | Logged as blocked, pauses 5 minutes, thread stays in the queue              |
| Any other failure                                                          | Retried up to 3 times (10 s apart), then logged and kept for the next cycle |
| `gallery-dl` runs longer than 10 minutes                                   | Killed and treated as a failure                                             |

Removals are logged in `failures_4chan.txt` and automatically cleaned from `threads_4chan.txt` and `priority_4chan.txt`.

### Adaptive pacing

| Queue size | Sleep between threads | Sleep between cycles |
| ---------- | --------------------- | -------------------- |
| 2 or fewer | 15 s                  | 60 s                 |
| 3 to 9     | 8 s                   | 30 s                 |
| 10 or more | 3 s                   | 15 s                 |

## Configuration

Settings are defined in the frozen `Config` dataclass near the top of the script:

| Setting             | Default | Meaning                                                |
| ------------------- | ------- | ------------------------------------------------------ |
| `max_retries`       | `3`     | Retries per thread on a transient failure              |
| `retry_sleep`       | `10`    | Seconds between retries                                |
| `base_thread_sleep` | `8`     | Normal pause between threads (s)                       |
| `min_thread_sleep`  | `3`     | Pause between threads when the queue is 10 or more (s) |
| `max_thread_sleep`  | `15`    | Pause between threads when the queue is 2 or fewer (s) |
| `base_cycle_sleep`  | `30`    | Normal pause between cycles (s)                        |
| `busy_cycle_sleep`  | `15`    | Pause between cycles when the queue is 10 or more (s)  |
| `idle_cycle_sleep`  | `60`    | Pause between cycles when the queue is 2 or fewer (s)  |
| `blocked_sleep`     | `300`   | Cool-down after a rate limit or Cloudflare block (s)   |
| `gallery_timeout`   | `600`   | Maximum runtime for one `gallery-dl` call (s)          |
| `sleep_request`     | `"2.0"` | Seconds passed to `gallery-dl --sleep-request`         |

File names are configured in the same place.

## Controls

| Action                   | Result                                                                   |
| ------------------------ | ------------------------------------------------------------------------ |
| `Ctrl+C` (once)          | Graceful stop: kills the current download, saves stats, prints a summary |
| `Ctrl+C` (twice)         | Force exit                                                               |
| Create `pause_4chan.txt` | Pause after the current thread                                           |
| Delete `pause_4chan.txt` | Resume                                                                   |

## Statistics

`stats_4chan.txt` keeps running totals across sessions:

```text
TOTAL_SESSIONS=0
TOTAL_CYCLES=0
TOTAL_SUCCESS=0
TOTAL_404=0
TOTAL_BLOCKED=0
TOTAL_FAILED=0
TOTAL_UNSUPPORTED=0
```

A per-cycle summary is printed after each cycle, and a full summary when the session ends.

## Troubleshooting

**`gallery-dl not found in PATH`**
Install it with `pip install gallery-dl` and make sure your Python scripts directory is on `PATH`. Check with `gallery-dl --version`.

**Constant `[BLOCKED]` messages**
4chan or Cloudflare is rate-limiting you. Increase `base_thread_sleep` and `blocked_sleep`, or shorten your thread list.

**The script says the list is missing**
Create `threads_4chan.txt` in the folder you're running from, or drop URLs into `new_4chan.txt`.

**A thread was removed but should still be alive**
Check `failures_4chan.txt` for the reason, and `log_4chan.txt` for the last gallery-dl output.

## Responsible use

Keep your request rates polite, respect 4chan's terms of service, and only archive content you are legally allowed to download and store.

## License

MIT
