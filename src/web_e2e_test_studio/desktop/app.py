"""Native Tkinter user interface for Web E2E Test Studio."""

from __future__ import annotations

import argparse
import shlex
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from web_e2e_test_studio.desktop.controller import (
    DesktopStudioController,
    StudioEvent,
)
from web_e2e_test_studio.desktop.engine import (
    DesktopRecordingConfig,
    DesktopRecordingSnapshot,
)


class DesktopStudioApp:
    """A compact desktop workbench for assisted browser test recording."""

    POLL_INTERVAL_MS = 120

    def __init__(self, root: tk.Tk, workspace: Path) -> None:
        self.root = root
        self.workspace = workspace.resolve()
        self.controller = DesktopStudioController()
        self._busy = False
        self._running = False
        self._preview: tk.PhotoImage | None = None

        self.url = tk.StringVar(value="https://example.com")
        self.goal = tk.StringVar(value="Record a browser workflow")
        self.name = tk.StringVar(value="recorded_flow")
        self.output_dir = tk.StringVar(value=str(self.workspace / "recordings"))
        self.headless = tk.BooleanVar(value=False)
        self.instruction = tk.StringVar()
        self.tool_path = tk.StringVar()
        self.tool_ref = tk.StringVar()
        self.tool_args = tk.StringVar()
        self.status = tk.StringVar(value="Ready — configure a session and start")
        self.step_summary = tk.StringVar(value="0 verified steps")

        self._configure_window()
        self._build_ui()
        self._set_controls()
        self.root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _configure_window(self) -> None:
        self.root.title("Web E2E Test Studio")
        self.root.geometry("1280x820")
        self.root.minsize(1040, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#52606d")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header, text="Web E2E Test Studio", style="Title.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Playwright recording · VLM assistance · model-free PyTest export",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(header, textvariable=self.step_summary).grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

        self._build_session_panel(shell)

        body = ttk.Panedwindow(shell, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)
        self._build_steps_panel(left)
        self._build_control_notebook(right)

        status_bar = ttk.Frame(shell)
        status_bar.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status).grid(
            row=0, column=0, sticky="w"
        )
        self.progress = ttk.Progressbar(
            status_bar, mode="indeterminate", length=160
        )
        self.progress.grid(row=0, column=1, sticky="e")

    def _build_session_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(
            parent,
            text=" Recording session ",
            padding=10,
            style="Section.TLabelframe",
        )
        panel.grid(row=1, column=0, sticky="ew")
        panel.columnconfigure(1, weight=3)
        panel.columnconfigure(3, weight=2)

        ttk.Label(panel, text="Start URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(panel, textvariable=self.url).grid(
            row=0, column=1, sticky="ew", padx=(8, 16)
        )
        ttk.Label(panel, text="Test name").grid(row=0, column=2, sticky="w")
        ttk.Entry(panel, textvariable=self.name).grid(
            row=0, column=3, sticky="ew", padx=(8, 16)
        )

        ttk.Label(panel, text="Goal").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(panel, textvariable=self.goal).grid(
            row=1, column=1, sticky="ew", padx=(8, 16), pady=(8, 0)
        )
        ttk.Label(panel, text="Output folder").grid(
            row=1, column=2, sticky="w", pady=(8, 0)
        )
        output = ttk.Frame(panel)
        output.grid(
            row=1, column=3, sticky="ew", padx=(8, 16), pady=(8, 0)
        )
        output.columnconfigure(0, weight=1)
        ttk.Entry(output, textvariable=self.output_dir).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(output, text="Browse", command=self._choose_output_dir).grid(
            row=0, column=1, padx=(6, 0)
        )

        actions = ttk.Frame(panel)
        actions.grid(row=0, column=4, rowspan=2, sticky="ns")
        self.start_button = ttk.Button(
            actions,
            text="Start browser",
            command=self._start,
            style="Accent.TButton",
        )
        self.start_button.pack(fill=tk.X)
        self.sync_button = ttk.Button(
            actions, text="Sync manual actions", command=self._sync
        )
        self.sync_button.pack(fill=tk.X, pady=5)
        self.save_button = ttk.Button(
            actions, text="Save & export", command=self._save
        )
        self.save_button.pack(fill=tk.X)
        self.stop_button = ttk.Button(
            actions, text="Stop", command=self._stop
        )
        self.stop_button.pack(fill=tk.X, pady=(5, 0))
        ttk.Checkbutton(
            panel,
            text="Headless browser",
            variable=self.headless,
        ).grid(row=2, column=3, sticky="w", padx=8, pady=(8, 0))

    def _build_steps_panel(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=3)
        parent.rowconfigure(1, weight=2)
        parent.columnconfigure(0, weight=1)

        steps_panel = ttk.LabelFrame(
            parent,
            text=" Verified replay steps ",
            padding=8,
            style="Section.TLabelframe",
        )
        steps_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        steps_panel.rowconfigure(0, weight=1)
        steps_panel.columnconfigure(0, weight=1)
        self.steps = ttk.Treeview(
            steps_panel,
            columns=("source", "action", "intent"),
            show="headings",
            selectmode="browse",
        )
        self.steps.heading("source", text="Source")
        self.steps.heading("action", text="Action")
        self.steps.heading("intent", text="Intent")
        self.steps.column("source", width=72, stretch=False)
        self.steps.column("action", width=105, stretch=False)
        self.steps.column("intent", width=360)
        scrollbar = ttk.Scrollbar(
            steps_panel, orient=tk.VERTICAL, command=self.steps.yview
        )
        self.steps.configure(yscrollcommand=scrollbar.set)
        self.steps.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        preview_panel = ttk.LabelFrame(
            parent,
            text=" Latest observation ",
            padding=8,
            style="Section.TLabelframe",
        )
        preview_panel.grid(
            row=1, column=0, sticky="nsew", padx=(0, 8), pady=(10, 0)
        )
        preview_panel.rowconfigure(1, weight=1)
        preview_panel.columnconfigure(0, weight=1)
        self.page_info = ttk.Label(
            preview_panel,
            text="No browser observation yet",
            style="Subtitle.TLabel",
        )
        self.page_info.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.preview_label = ttk.Label(
            preview_panel,
            text="A screenshot preview appears after recording starts.",
            anchor=tk.CENTER,
        )
        self.preview_label.grid(row=1, column=0, sticky="nsew")

    def _build_control_notebook(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        notebook = ttk.Notebook(parent)
        notebook.grid(row=0, column=0, sticky="nsew")
        ai_tab = ttk.Frame(notebook, padding=12)
        tool_tab = ttk.Frame(notebook, padding=12)
        log_tab = ttk.Frame(notebook, padding=8)
        notebook.add(ai_tab, text="VLM assistant")
        notebook.add(tool_tab, text="External tools")
        notebook.add(log_tab, text="Run log")
        self._build_ai_tab(ai_tab)
        self._build_tool_tab(tool_tab)
        self._build_log_tab(log_tab)

    def _build_ai_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(
            tab,
            text="Describe the next browser outcome in natural language.",
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.instruction).grid(
            row=1, column=0, sticky="ew", pady=(10, 8)
        )
        help_text = (
            "The agent observes the current page, proposes a structured action, "
            "executes it through Playwright, re-observes, and records only verified "
            "steps.\n\nConfigure VLM_API_KEY and optionally VLM_BASE_URL / VLM_MODEL "
            "before launching the Studio."
        )
        ttk.Label(
            tab,
            text=help_text,
            wraplength=390,
            justify=tk.LEFT,
            anchor=tk.NW,
        ).grid(row=2, column=0, sticky="nsew")
        self.ai_button = ttk.Button(
            tab,
            text="Run VLM instruction",
            command=self._run_ai,
            style="Accent.TButton",
        )
        self.ai_button.grid(row=3, column=0, sticky="ew", pady=(12, 0))

    def _build_tool_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        ttk.Label(tab, text="Register a local Python or executable tool").grid(
            row=0, column=0, sticky="w"
        )
        picker = ttk.Frame(tab)
        picker.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        picker.columnconfigure(0, weight=1)
        ttk.Entry(picker, textvariable=self.tool_path).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(picker, text="Browse", command=self._choose_tool).grid(
            row=0, column=1, padx=(6, 0)
        )
        self.register_tool_button = ttk.Button(
            tab, text="Register tool", command=self._register_tool
        )
        self.register_tool_button.grid(row=2, column=0, sticky="ew")
        ttk.Separator(tab).grid(row=3, column=0, sticky="ew", pady=16)
        ttk.Label(tab, text="Registered tool ID or name").grid(
            row=4, column=0, sticky="w"
        )
        ttk.Entry(tab, textvariable=self.tool_ref).grid(
            row=5, column=0, sticky="ew", pady=(6, 10)
        )
        ttk.Label(tab, text="Arguments (shell-style)").grid(
            row=6, column=0, sticky="w"
        )
        ttk.Entry(tab, textvariable=self.tool_args).grid(
            row=7, column=0, sticky="ew", pady=(6, 10)
        )
        self.run_tool_button = ttk.Button(
            tab,
            text="Run & record tool",
            command=self._run_tool,
            style="Accent.TButton",
        )
        self.run_tool_button.grid(row=8, column=0, sticky="ew")

    def _build_log_tab(self, tab: ttk.Frame) -> None:
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        self.log = tk.Text(
            tab,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Cascadia Mono", 9),
            padx=8,
            pady=8,
        )
        scrollbar = ttk.Scrollbar(
            tab, orient=tk.VERTICAL, command=self.log.yview
        )
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _start(self) -> None:
        name = self.name.get().strip()
        url = self.url.get().strip()
        goal = self.goal.get().strip()
        if not name or not url or not goal:
            messagebox.showwarning(
                "Missing session details",
                "Start URL, test name, and goal are required.",
            )
            return
        output_dir = Path(self.output_dir.get()).expanduser().resolve()
        config = DesktopRecordingConfig(
            url=url,
            goal=goal,
            name=name,
            asset_path=output_dir / f"{name}.json",
            output_path=output_dir / f"test_{name}.py",
            workspace=self.workspace,
            headless=self.headless.get(),
        )
        self._submit("Starting browser…", lambda: self.controller.start(config))

    def _sync(self) -> None:
        self._submit(
            "Capturing manual browser actions…",
            self.controller.sync_manual,
        )

    def _save(self) -> None:
        self._submit("Saving asset and PyTest…", self.controller.save)

    def _stop(self) -> None:
        self._submit("Stopping browser…", self.controller.stop)

    def _run_ai(self) -> None:
        instruction = self.instruction.get().strip()
        if not instruction:
            messagebox.showwarning(
                "Missing instruction", "Enter a VLM instruction first."
            )
            return
        self._submit(
            "VLM agent is observing and acting…",
            lambda: self.controller.run_ai(instruction),
        )

    def _register_tool(self) -> None:
        path = self.tool_path.get().strip()
        if not path:
            messagebox.showwarning(
                "Missing tool", "Choose an external tool file first."
            )
            return
        self._submit(
            "Registering external tool…",
            lambda: self.controller.register_tool(path),
        )

    def _run_tool(self) -> None:
        tool_ref = self.tool_ref.get().strip()
        if not tool_ref:
            messagebox.showwarning(
                "Missing tool reference", "Enter a registered tool ID or name."
            )
            return
        arguments = shlex.split(self.tool_args.get(), posix=False)
        self._submit(
            f"Running external tool {tool_ref}…",
            lambda: self.controller.run_tool(tool_ref, arguments),
        )

    def _submit(self, status: str, command: Callable[[], None]) -> None:
        if self._busy:
            return
        self._busy = True
        self.status.set(status)
        self.progress.start(12)
        self._set_controls()
        try:
            command()
        except Exception as exc:
            self._handle_event(
                StudioEvent("error", f"{type(exc).__name__}: {exc}")
            )

    def _poll_events(self) -> None:
        for event in self.controller.poll_events():
            self._handle_event(event)
        self.root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _handle_event(self, event: StudioEvent) -> None:
        self._busy = False
        self.progress.stop()
        self.status.set(event.message)
        self._append_log(event.message)
        for detail in event.details:
            self._append_log(f"  {detail}")
        if event.kind == "error":
            messagebox.showerror("Studio command failed", event.message)
        if event.snapshot is not None:
            self._apply_snapshot(event.snapshot)
        self._set_controls()

    def _apply_snapshot(self, snapshot: DesktopRecordingSnapshot) -> None:
        self._running = snapshot.running
        self.steps.delete(*self.steps.get_children())
        for index, step in enumerate(snapshot.steps, start=1):
            self.steps.insert(
                "",
                tk.END,
                values=(
                    step.source.value,
                    step.action.name,
                    f"{index}. {step.intent}",
                ),
            )
        self.step_summary.set(f"{snapshot.step_count} verified steps")
        observation = snapshot.last_observation
        page_text = observation.title or "(untitled page)"
        self.page_info.configure(text=f"{page_text}  ·  {observation.url}")
        self._show_preview(observation.screenshot_path)
        self._append_log(f"  Asset: {snapshot.asset_path}")
        self._append_log(f"  PyTest: {snapshot.output_path}")

    def _show_preview(self, screenshot_path: str) -> None:
        path = Path(screenshot_path) if screenshot_path else None
        if path is None or not path.is_file():
            self._preview = None
            self.preview_label.configure(
                image="",
                text="No screenshot is available for this observation.",
            )
            return
        try:
            image = tk.PhotoImage(file=str(path))
            max_width, max_height = 660, 280
            scale = max(
                1,
                (image.width() + max_width - 1) // max_width,
                (image.height() + max_height - 1) // max_height,
            )
            self._preview = image.subsample(scale, scale)
            self.preview_label.configure(image=self._preview, text="")
        except tk.TclError:
            self._preview = None
            self.preview_label.configure(
                image="", text=f"Screenshot saved at:\n{path}"
            )

    def _set_controls(self) -> None:
        idle = not self._busy
        self.start_button.configure(
            state=tk.NORMAL if idle and not self._running else tk.DISABLED
        )
        active_state = tk.NORMAL if idle and self._running else tk.DISABLED
        for button in (
            self.sync_button,
            self.save_button,
            self.stop_button,
            self.ai_button,
            self.register_tool_button,
            self.run_tool_button,
        ):
            button.configure(state=active_state)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.output_dir.get(),
            title="Choose recording output folder",
        )
        if selected:
            self.output_dir.set(selected)

    def _choose_tool(self) -> None:
        selected = filedialog.askopenfilename(
            initialdir=str(self.workspace),
            title="Choose an external tool",
            filetypes=(
                ("Supported tools", "*.py *.exe *.bat *.cmd"),
                ("All files", "*.*"),
            ),
        )
        if selected:
            self.tool_path.set(selected)

    def _close(self) -> None:
        self.status.set("Closing Studio…")
        self.controller.close()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-e2e-desktop",
        description="Launch the native Web E2E Test Studio.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace allowed for external tools (default: current directory)",
    )
    args = parser.parse_args(argv)
    root = tk.Tk()
    DesktopStudioApp(root, args.workspace)
    root.mainloop()
    return 0


__all__ = ["DesktopStudioApp", "main"]
