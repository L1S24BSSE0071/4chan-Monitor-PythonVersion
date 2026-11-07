#!/usr/bin/env pythonw

import contextlib
import importlib.util
import io
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from types import ModuleType
from typing import Optional


MONITOR_SCRIPT = Path(__file__).with_name("4chan-Monitor.py")
MAX_LOG_LINES = 4000


class QueueWriter:
    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self.pending = ""

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.events.put(("line", line.rstrip("\r")))
        return len(text)

    def flush(self) -> None:
        if self.pending:
            self.events.put(("line", self.pending.rstrip("\r")))
            self.pending = ""


def load_monitor_module(script: Path = MONITOR_SCRIPT) -> ModuleType:
    spec = importlib.util.spec_from_file_location("chan_monitor_core", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load monitor module from {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    stdout, stderr = sys.stdout, sys.stderr
    fallback_stdout = None
    fallback_stderr = None
    try:
        if stdout is None:
            fallback_stdout = io.StringIO()
            sys.stdout = fallback_stdout
        if stderr is None:
            fallback_stderr = io.StringIO()
            sys.stderr = fallback_stderr
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    finally:
        if fallback_stdout is not None:
            fallback_stdout.close()
            sys.stdout = stdout
        if fallback_stderr is not None:
            fallback_stderr.close()
            sys.stderr = stderr
    return module


class MonitorGUI:
    def __init__(
        self,
        root: tk.Tk,
        data_dir: Optional[Path] = None,
        monitor_module: Optional[ModuleType] = None,
    ) -> None:
        self.root = root
        self.events = queue.Queue()
        self.monitor = monitor_module or load_monitor_module()
        self.monitor.IS_TTY = False
        self.monitor.USE_COLOR = False
        self.worker: Optional[threading.Thread] = None
        self.active_directory: Optional[Path] = None
        self.download_progress_running = False
        self.stop_requested = False
        self.closing = False
        self.monitor_error: Optional[str] = None
        self.owns_pause = False
        self.owns_stop = False

        self.data_dir = tk.StringVar(
            value=str(data_dir or MONITOR_SCRIPT.parent.resolve())
        )
        self.check_updates = tk.BooleanVar(value=False)
        self.use_archive = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Stopped")
        self.stats_summary = tk.StringVar(value="")
        self.url = tk.StringVar()
        self.cycles = tk.StringVar(value="0")

        root.title("4chan Thread Monitor")
        root.geometry("1080x800")
        root.minsize(820, 620)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._report_callback_exception
        self._configure_styles()
        self._build_ui()
        self._set_running(False)
        self.refresh_queues()
        root.after(100, self._drain_events)
        root.after(1500, self._refresh_while_running)

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.colors = {
            "background": "#f2f5f9",
            "surface": "#ffffff",
            "text": "#172033",
            "muted": "#667085",
            "accent": "#2563eb",
            "accent_active": "#1d4ed8",
            "console": "#111827",
            "console_text": "#e5e7eb",
        }
        self.root.configure(background=self.colors["background"])
        style.configure("TFrame", background=self.colors["background"])
        style.configure("Card.TFrame", background=self.colors["surface"])
        style.configure(
            "TLabel",
            background=self.colors["background"],
            foreground=self.colors["text"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Card.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["text"],
        )
        style.configure(
            "Title.TLabel",
            background=self.colors["background"],
            foreground=self.colors["text"],
            font=("Segoe UI", 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.colors["background"],
            foreground=self.colors["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Muted.Card.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
        )
        style.configure(
            "Primary.TButton",
            background=self.colors["accent"],
            foreground="#ffffff",
            padding=(14, 8),
            borderwidth=0,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#a9bde8"),
                ("active", self.colors["accent_active"]),
            ],
            foreground=[("disabled", "#f4f6fb"), ("!disabled", "#ffffff")],
        )
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 9))
        style.configure(
            "TEntry",
            padding=7,
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
        )
        style.configure(
            "TSpinbox",
            padding=5,
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
        )
        style.configure(
            "TCheckbutton",
            background=self.colors["surface"],
            foreground=self.colors["text"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.colors["surface"])],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=self.colors["accent"],
            troughcolor="#e7edf5",
            bordercolor="#e7edf5",
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
        )

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=(22, 18))
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(3, weight=3)
        container.rowconfigure(6, weight=2)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
        ttk.Label(
            header, text="4chan Thread Monitor", style="Title.TLabel"
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="Manage thread queues and monitor downloads from one window.",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        folder_card = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 12)
        )
        folder_card.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10)
        )
        folder_card.columnconfigure(1, weight=1)
        ttk.Label(
            folder_card, text="Working folder", style="Section.TLabel"
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.directory_entry = ttk.Entry(
            folder_card, textvariable=self.data_dir
        )
        self.directory_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(
            folder_card, text="Browse…", command=self._browse
        )
        self.browse_button.grid(row=0, column=2, padx=(8, 0))

        options = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 10)
        )
        self.options_frame = options
        options.grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10)
        )
        ttk.Checkbutton(
            options,
            text="Check gallery-dl updates at startup",
            variable=self.check_updates,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            options,
            text="Try archive mirrors for dead threads",
            variable=self.use_archive,
        ).pack(side=tk.LEFT, padx=(20, 0))
        ttk.Label(
            options,
            text="Cycles (0 = forever)",
            style="Muted.Card.TLabel",
        ).pack(side=tk.LEFT, padx=(20, 8))
        self.cycles_entry = ttk.Spinbox(
            options, from_=0, to=100000, width=8, textvariable=self.cycles
        )
        self.cycles_entry.pack(side=tk.LEFT)

        self.queue_frame = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 12)
        )
        self.queue_frame.grid(
            row=3, column=0, columnspan=3, sticky="nsew", pady=(0, 10)
        )
        self.queue_frame.columnconfigure(0, weight=1)
        self.queue_frame.columnconfigure(1, weight=1)
        self.queue_frame.rowconfigure(2, weight=1)
        queue_header = ttk.Frame(self.queue_frame, style="Card.TFrame")
        queue_header.grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )
        ttk.Label(
            queue_header, text="Thread queues", style="Section.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Label(
            queue_header,
            text="Priority threads run before the normal queue",
            style="Muted.Card.TLabel",
        ).pack(side=tk.RIGHT)

        self.normal_count = tk.StringVar(value="0")
        self.priority_count = tk.StringVar(value="0")
        normal_header = ttk.Frame(self.queue_frame, style="Card.TFrame")
        normal_header.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 5))
        ttk.Label(
            normal_header, text="Normal", style="Card.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Label(
            normal_header,
            textvariable=self.normal_count,
            style="Muted.Card.TLabel",
        ).pack(side=tk.RIGHT)
        priority_header = ttk.Frame(self.queue_frame, style="Card.TFrame")
        priority_header.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 5))
        ttk.Label(
            priority_header, text="Priority", style="Card.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Label(
            priority_header,
            textvariable=self.priority_count,
            style="Muted.Card.TLabel",
        ).pack(side=tk.RIGHT)

        self.normal_list = self._make_queue_list(self.queue_frame, 0)
        self.priority_list = self._make_queue_list(self.queue_frame, 1)
        ttk.Button(
            self.queue_frame,
            text="Remove selected",
            command=lambda: self.remove_selected("normal"),
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            self.queue_frame,
            text="Remove selected",
            command=lambda: self.remove_selected("priority"),
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        add_frame = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 10)
        )
        add_frame.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(0, 10)
        )
        add_frame.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(add_frame, textvariable=self.url)
        self.url_entry.grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        self.url_entry.bind(
            "<Return>", lambda _event: self.add_url("normal")
        )
        self.add_normal_button = ttk.Button(
            add_frame,
            text="Add to normal",
            command=lambda: self.add_url("normal"),
        )
        self.add_normal_button.grid(row=0, column=1, padx=(0, 6))
        self.add_priority_button = ttk.Button(
            add_frame,
            text="Add to priority",
            command=lambda: self.add_url("priority"),
        )
        self.add_priority_button.grid(row=0, column=2)

        stats_card = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 10)
        )
        stats_card.grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10)
        )
        ttk.Label(
            stats_card, text="Lifetime statistics", style="Section.TLabel"
        ).pack(anchor=tk.W)
        ttk.Label(
            stats_card,
            textvariable=self.stats_summary,
            style="Muted.Card.TLabel",
        ).pack(anchor=tk.W, pady=(3, 0))

        output_card = ttk.Frame(
            container, style="Card.TFrame", padding=(14, 11)
        )
        output_card.grid(
            row=6, column=0, columnspan=3, sticky="nsew", pady=(0, 10)
        )
        output_card.columnconfigure(0, weight=1)
        output_card.rowconfigure(1, weight=1)
        output_header = ttk.Frame(output_card, style="Card.TFrame")
        output_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(
            output_header, text="Activity log", style="Section.TLabel"
        ).pack(side=tk.LEFT)
        self.clear_log_button = ttk.Button(
            output_header, text="Clear", command=self._clear_output
        )
        self.clear_log_button.pack(side=tk.RIGHT)
        self.output = scrolledtext.ScrolledText(
            output_card,
            height=12,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            padx=10,
            pady=9,
            background=self.colors["console"],
            foreground=self.colors["console_text"],
            insertbackground="#ffffff",
            selectbackground=self.colors["accent"],
            font=("Consolas", 9),
        )
        self.output.grid(row=1, column=0, sticky="nsew")

        controls = ttk.Frame(container)
        controls.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.start_button = ttk.Button(
            controls,
            text="Start monitoring",
            style="Primary.TButton",
            command=self.start_monitor,
        )
        self.start_button.pack(side=tk.LEFT)
        self.pause_button = ttk.Button(
            controls, text="Pause", command=self.toggle_pause
        )
        self.pause_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            controls, text="Stop", command=self.stop_monitor
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        self.refresh_button = ttk.Button(
            controls, text="Refresh queues", command=self.refresh_queues
        )
        self.refresh_button.pack(side=tk.LEFT, padx=(8, 0))
        self.download_progress = ttk.Progressbar(
            controls, mode="indeterminate", length=110, maximum=100
        )
        self.download_progress.pack(side=tk.RIGHT, padx=(10, 0))
        self.status_label = ttk.Label(
            controls, textvariable=self.status, width=38, anchor=tk.E
        )
        self.status_label.pack(side=tk.RIGHT, padx=(8, 0))

    @staticmethod
    def _make_queue_list(parent: ttk.Frame, column: int) -> tk.Listbox:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(
            row=2,
            column=column,
            sticky="nsew",
            padx=(0, 8) if column == 0 else (8, 0),
        )
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        listbox = tk.Listbox(
            frame,
            selectmode=tk.EXTENDED,
            height=7,
            activestyle="none",
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#dce3ed",
            highlightcolor="#8eb2f5",
            background="#ffffff",
            foreground="#172033",
            selectbackground="#dbeafe",
            selectforeground="#1e3a8a",
            font=("Segoe UI", 9),
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return listbox

    def _clear_output(self) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.configure(state=tk.DISABLED)

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.data_dir.get() or str(MONITOR_SCRIPT.parent)
        )
        if selected:
            self.data_dir.set(selected)
            self.refresh_queues()

    def _set_running(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        for control in (self.directory_entry, self.browse_button):
            control.configure(state=state)
        for control in self.options_frame.winfo_children():
            if isinstance(control, (ttk.Checkbutton, ttk.Spinbox)):
                control.configure(state=state)
        self.start_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.clear_log_button.configure(state=tk.NORMAL)
        self.pause_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self._set_queue_controls(running)
        self.url_entry.configure(state=tk.NORMAL)
        self.add_normal_button.configure(state=tk.NORMAL)
        self.add_priority_button.configure(state=state)
        if not running:
            self.pause_button.configure(text="Pause")
            self.stop_button.configure(text="Stop")

    def _set_queue_controls(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        for child in self.queue_frame.winfo_children():
            if isinstance(child, (ttk.Button, ttk.Entry)):
                child.configure(state=state)
            elif isinstance(child, ttk.Frame):
                for nested in child.winfo_children():
                    if isinstance(nested, tk.Listbox):
                        nested.configure(state=state)

    def _append_output(self, lines: str) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, lines)
        line_count = int(self.output.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.output.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def _report_callback_exception(self, exception_type, error, tb) -> None:
        detail = "".join(traceback.format_exception(exception_type, error, tb))
        self._append_output(f"[GUI ERROR]\n{detail}\n")
        self.status.set(f"GUI error: {error}")

    def _queue_path(self, queue_name: str) -> Path:
        filename = (
            self.monitor.CFG.list_file
            if queue_name == "normal"
            else self.monitor.CFG.priority_box
        )
        directory = self.active_directory or Path(self.data_dir.get()).expanduser()
        return directory / Path(filename).name

    def _read_queue(self, queue_name: str):
        urls = self.monitor.read_urls(self._queue_path(queue_name))
        if urls is None:
            raise OSError(f"Could not read {self._queue_path(queue_name)}")
        return urls

    def refresh_queues(self) -> None:
        try:
            normal = self._read_queue("normal")
            priority = self._read_queue("priority")
        except OSError as error:
            if self.running:
                self.events.put(("line", f"[GUI] Queue refresh failed: {error}"))
            else:
                self.status.set(f"Queue read error: {error}")
            return
        self._replace_list(self.normal_list, normal)
        self._replace_list(self.priority_list, priority)
        self.normal_count.set(f"{len(normal)} threads")
        self.priority_count.set(f"{len(priority)} threads")
        self._refresh_stats()
        if not self.running:
            self.status.set(f"Stopped | {len(priority)} priority, {len(normal)} normal")

    def _refresh_stats(self) -> None:
        directory = self.active_directory or Path(self.data_dir.get()).expanduser()
        stats = self.monitor.DEFAULT_STATS.copy()
        try:
            lines = (directory / self.monitor.CFG.stats_file.name).read_text(
                encoding="utf-8-sig"
            ).splitlines()
        except FileNotFoundError:
            lines = []
        except OSError:
            self.stats_summary.set("Lifetime statistics unavailable")
            return
        for line in lines:
            key, separator, value = line.partition("=")
            if separator and key.strip() in stats:
                try:
                    stats[key.strip()] = max(0, int(value.strip()))
                except ValueError:
                    continue
        self.stats_summary.set(
            "Lifetime: "
            f"{stats['TOTAL_SESSIONS']} sessions | {stats['TOTAL_CYCLES']} cycles | "
            f"{stats['TOTAL_SUCCESS']} OK | {stats['TOTAL_404']} removed | "
            f"{stats['TOTAL_ARCHIVED']} archived | {stats['TOTAL_BLOCKED']} blocked | "
            f"{stats['TOTAL_FAILED']} failed | {stats['TOTAL_UNSUPPORTED']} unsupported"
        )

    @staticmethod
    def _replace_list(listbox: tk.Listbox, urls) -> None:
        current = listbox.get(0, tk.END)
        if current == tuple(urls):
            return
        selected = {
            current[index]
            for index in listbox.curselection()
            if index < len(current)
        }
        try:
            top_fraction = listbox.yview()[0]
        except tk.TclError:
            top_fraction = 0.0
        listbox.delete(0, tk.END)
        for url in urls:
            listbox.insert(tk.END, url)
        for index, url in enumerate(urls):
            if url in selected:
                listbox.selection_set(index)
        if urls and top_fraction:
            listbox.yview_moveto(top_fraction)

    def _refresh_while_running(self) -> None:
        if self.running:
            self.refresh_queues()
        self.root.after(1500, self._refresh_while_running)

    def add_url(self, queue_name: str) -> None:
        parsed = self.monitor.parse_urls(self.url.get())
        if len(parsed) != 1:
            messagebox.showerror("Invalid URL", "Enter one valid HTTP(S) thread URL.")
            return
        try:
            normal = self._read_queue("normal")
            priority = self._read_queue("priority")
            if queue_name == "normal" and self.running:
                added = self._add_to_drop_box(parsed[0], normal, priority)
                self.url.set("")
                if added:
                    self.status.set("Added to normal queue for the next cycle")
                return
            key = self.monitor.dedupe_key(parsed[0])
            if any(
                self.monitor.dedupe_key(existing) == key
                for existing in normal + priority
            ):
                self.status.set("That thread is already in this queue")
                return
            urls = normal if queue_name == "normal" else priority
            urls.append(parsed[0])
            self._write_queue(queue_name, urls)
        except OSError as error:
            messagebox.showerror("Queue update failed", str(error))
            return
        self.url.set("")
        self.refresh_queues()

    def _add_to_drop_box(self, url: str, normal, priority) -> bool:
        if self.active_directory is None:
            raise OSError("Monitor working folder is unavailable")
        pending_path = self.active_directory / self.monitor.CFG.new_box.name
        pending = self.monitor.read_urls(pending_path)
        if pending is None:
            raise OSError(f"Could not read {pending_path}")
        key = self.monitor.dedupe_key(url)
        if any(
            self.monitor.dedupe_key(existing) == key
            for existing in normal + priority + pending
        ):
            self.status.set("That thread is already queued")
            return False
        content = "\n".join(pending + [url]) + "\n"
        self.monitor.atomic_write(pending_path, content)
        return True

    def remove_selected(self, queue_name: str) -> None:
        listbox = self.normal_list if queue_name == "normal" else self.priority_list
        selected = listbox.curselection()
        if not selected:
            return
        selected_urls = {listbox.get(index) for index in selected}
        try:
            urls = self._read_queue(queue_name)
            remaining = [url for url in urls if url not in selected_urls]
            self._write_queue(queue_name, remaining)
        except OSError as error:
            messagebox.showerror("Queue update failed", str(error))
            return
        self.refresh_queues()

    def _write_queue(self, queue_name: str, urls) -> None:
        content = "\n".join(urls) + ("\n" if urls else "")
        self.monitor.atomic_write(self._queue_path(queue_name), content)

    def start_monitor(self) -> None:
        if self.running:
            return
        directory = Path(self.data_dir.get()).expanduser().resolve()
        if not directory.is_dir():
            messagebox.showerror("Invalid working folder", f"Folder does not exist:\n{directory}")
            return
        stop_file = directory / "stop_4chan.txt"
        if stop_file.exists():
            messagebox.showerror(
                "Stop marker exists",
                f"Remove the stale stop marker before starting:\n{stop_file}",
            )
            return

        try:
            cycles = int(self.cycles.get())
            if cycles < 0:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid cycle count", "Enter a whole number of 0 or more.")
            return
        args = ["--loops", str(cycles), "--no-clear", "--no-pause"]
        if not self.check_updates.get():
            args.append("--no-update-check")
        if not self.use_archive.get():
            args.append("--no-archive")
        args.extend(("--stop-file", str(stop_file)))

        self.stop_requested = False
        self.monitor_error = None
        self.owns_pause = False
        self.owns_stop = False
        self.active_directory = directory
        self.monitor.stop_event.clear()
        self.monitor.progress_callback = self._queue_progress
        self._set_running(True)
        self.pause_button.configure(
            text="Resume" if (directory / "pause_4chan.txt").exists() else "Pause"
        )
        self.status.set("Starting...")
        self._append_output("\n--- Monitor started ---\n")
        self.worker = threading.Thread(
            target=self._run_monitor, args=(args,), daemon=True
        )
        self.worker.start()

    def _run_monitor(self, args) -> None:
        writer = QueueWriter(self.events)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                self.monitor.main(args, working_directory=self.active_directory)
        except SystemExit as error:
            return_code = error.code if isinstance(error.code, int) else 1
            self.events.put(("error", f"Monitor exited with code {return_code}"))
        except Exception:
            self.events.put(("error", traceback.format_exc()))
        finally:
            writer.flush()
            self.events.put(("exit", None))

    def _queue_progress(self, message: str, active: bool) -> None:
        self.events.put(("progress", (message, active)))

    def toggle_pause(self) -> None:
        if not self.running or self.stop_requested or self.active_directory is None:
            return
        pause_file = self.active_directory / "pause_4chan.txt"
        try:
            if pause_file.exists():
                pause_file.unlink()
                self.owns_pause = False
                self.pause_button.configure(text="Pause")
                self.status.set("Resuming...")
            else:
                pause_file.touch(exist_ok=False)
                self.owns_pause = True
                self.pause_button.configure(text="Resume")
                self.status.set("Pause requested...")
        except OSError as error:
            messagebox.showerror("Pause control failed", str(error))

    def stop_monitor(self) -> None:
        if not self.running or self.active_directory is None:
            return
        if self.stop_requested:
            return
        try:
            (self.active_directory / "stop_4chan.txt").touch(exist_ok=False)
        except OSError as error:
            messagebox.showerror("Stop request failed", str(error))
            return
        self.owns_stop = True
        self.stop_requested = True
        self.pause_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)
        for control in (
            self.url_entry,
            self.add_normal_button,
            self.add_priority_button,
        ):
            control.configure(state=tk.DISABLED)
        self.status.set("Stopping gracefully...")

    def _drain_events(self) -> None:
        lines = []
        exited = False
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                lines.append(value + "\n")
                if value.startswith("[PAUSED]"):
                    self.status.set("Paused")
                elif value.startswith("[RESUMED]"):
                    self.status.set("Running")
                elif value.startswith("[INTERRUPTED]"):
                    self.status.set("Stopping gracefully...")
                elif "[OK]" in value:
                    self.status.set("Download complete")
                elif "[FAILED]" in value or "[BLOCKED]" in value:
                    self.status.set(value.strip())
                elif value.startswith("[CYCLE]") or value.startswith(" CYCLE:"):
                    self.status.set("Starting cycle...")
            elif kind == "progress":
                message, active = value
                self.status.set(self._progress_status(message))
                if active:
                    if not self.download_progress_running:
                        self.download_progress.start(12)
                        self.download_progress_running = True
                elif self.download_progress_running:
                    self.download_progress.stop()
                    self.download_progress_running = False
            elif kind == "error":
                self.monitor_error = value
                self.status.set("Monitor error — see activity log")
                lines.append(f"[GUI ERROR] {value}\n")
            elif kind == "exit":
                exited = True
        if lines:
            self._append_output("".join(lines))
        if exited:
            self._monitor_exited()
            if self.closing:
                return
        self.root.after(100, self._drain_events)

    def _monitor_exited(self) -> None:
        self.worker = None
        if self.active_directory is not None:
            for name, owned in (
                ("pause_4chan.txt", self.owns_pause),
                ("stop_4chan.txt", self.owns_stop),
            ):
                if owned:
                    try:
                        (self.active_directory / name).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        self._append_output(f"[GUI] Could not remove {name}: {error}\n")
        self.owns_pause = False
        self.owns_stop = False
        was_stopped_by_user = self.stop_requested
        self.stop_requested = False
        self.active_directory = None
        self.monitor.stop_event.clear()
        self.monitor.progress_callback = None
        if self.download_progress_running:
            self.download_progress.stop()
            self.download_progress_running = False
        self._set_running(False)
        self.refresh_queues()
        if self.closing:
            self.root.destroy()
            return
        if self.monitor_error:
            self.status.set("Stopped after an error")
        elif was_stopped_by_user:
            self.status.set("Stopped by user")
        else:
            self.status.set("Stopped")
        self._append_output("--- Monitor stopped ---\n")

    @staticmethod
    def _progress_status(message: str) -> str:
        if message.startswith("Downloading media: "):
            details = message[len("Downloading media: "):]
            url, separator, elapsed = details.partition(" (")
            route_parts = url.split("://", 1)[-1].partition("/")[2].split("/")
            route = "/".join(route_parts[-3:])
            label = f"Downloading /{route}" if route else "Downloading media"
            if separator:
                label += f" ({elapsed.rstrip(')')})"
        elif message.startswith("Processing"):
            label = "Finalizing download…"
        else:
            label = message
        if len(label) > 38:
            return f"{label[:37]}…"
        return label

    def on_close(self) -> None:
        if self.worker is None:
            self.root.destroy()
            return
        if not self.worker.is_alive():
            self.closing = True
            self._monitor_exited()
            return
        if messagebox.askyesno(
            "Monitor is running",
            "Stop monitoring and close the window?",
            parent=self.root,
        ):
            self.closing = True
            self.stop_monitor()


def main() -> None:
    root = None
    try:
        root = tk.Tk()
        MonitorGUI(root)
        root.mainloop()
    except Exception as error:
        details = traceback.format_exc()
        with contextlib.suppress(OSError):
            Path(__file__).with_suffix(".error.log").write_text(
                details, encoding="utf-8"
            )
        with contextlib.suppress(tk.TclError):
            if root is None:
                root = tk.Tk()
                root.withdraw()
            messagebox.showerror(
                "4chan Thread Monitor could not start",
                f"{error}\n\nDetails were saved beside the GUI as "
                f"{Path(__file__).with_suffix('.error.log').name}.",
                parent=root,
            )
    finally:
        if root is not None:
            with contextlib.suppress(tk.TclError):
                root.destroy()


if __name__ == "__main__":
    main()
