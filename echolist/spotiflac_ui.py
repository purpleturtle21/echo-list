"""SpotiFLAC companion window — download Spotify playlists as FLAC."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from queue import Queue, Empty
from threading import Thread, Event

from .updater import install_package

def _check_spotiflac():
    try:
        from SpotiFLAC import SpotiFLAC
        return True
    except ImportError:
        return False

SPOTIFLAC_AVAILABLE = _check_spotiflac()

# Theme — matches gui.py
BG = "#1a1a1a"
BG_PANEL = "#222222"
BG_INPUT = "#2a2a2a"
FG = "#cccccc"
FG_DIM = "#888888"
FG_BRIGHT = "#eeeeee"
RED = "#cc3333"
RED_DARK = "#991111"
RED_BRIGHT = "#ff4444"
GREEN = "#44aa44"
YELLOW = "#ccaa33"
BORDER = "#333333"
ACCENT = RED

SETTINGS_DIR = Path.home() / ".echolist"
SETTINGS_FILE = SETTINGS_DIR / "spotiflac_settings.json"
SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(playlist|album|track)/[a-zA-Z0-9]+"
)

DEFAULT_SETTINGS = {
    "quality": "LOSSLESS",
    "services": ["qobuz", "tidal"],
    "filename_format": "{title} - {artist}",
    "use_track_numbers": True,
    "default_mode": "library",
    "window_geometry": "",
}


def _load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: dict):
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class SpotiFLACWindow:
    """Floating Toplevel for downloading Spotify playlists via SpotiFLAC."""

    def __init__(self, app):
        self.app = app
        self.settings = _load_settings()
        self._downloading = False
        self._cancel = Event()
        self._callback_queue: Queue = Queue()
        self._downloaded_files: list[Path] = []
        self._failed_tracks: list[str] = []
        self._playlist_name = ""
        self._drag_data = None

        self.win = tk.Toplevel(app.root)
        self.win.title("SpotiFLAC")
        self.win.configure(bg=BG)
        self.win.minsize(420, 480)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.win.transient(app.root)

        geo = self.settings.get("window_geometry")
        if geo:
            self.win.geometry(geo)
        else:
            self.win.geometry("460x560")

        self._apply_theme()
        if _check_spotiflac():
            self._build_ui()
        else:
            self._build_install_ui()
        self._poll_callbacks()

    # ── Theme ──

    def _apply_theme(self):
        style = ttk.Style(self.win)
        style.configure("SF.TButton", background=BG_PANEL, foreground=FG)
        style.configure("SFAccent.TButton", background=RED_DARK, foreground=FG_BRIGHT)
        style.map("SFAccent.TButton",
                  background=[("active", RED), ("disabled", BG_PANEL)])
        style.configure("SF.TRadiobutton", background=BG, foreground=FG,
                        indicatorcolor=BG_INPUT)
        style.map("SF.TRadiobutton",
                  background=[("active", BG_PANEL)],
                  indicatorcolor=[("selected", RED)])
        style.configure("SF.TCheckbutton", background=BG, foreground=FG,
                        indicatorcolor=BG_INPUT)
        style.map("SF.TCheckbutton",
                  background=[("active", BG_PANEL)],
                  indicatorcolor=[("selected", RED)])
        style.configure("SF.TLabelframe", background=BG, foreground=FG_DIM)
        style.configure("SF.TLabelframe.Label", background=BG, foreground=FG_DIM,
                        font=("Consolas", 9))

    # ── Install UI ──

    def _build_install_ui(self):
        for w in self.win.winfo_children():
            w.destroy()

        frame = tk.Frame(self.win, bg=BG)
        frame.place(relx=0.5, rely=0.4, anchor="center")

        tk.Label(frame, text="SPOTIFY DOWNLOAD", font=("Consolas", 12, "bold"),
                 bg=BG, fg=FG_BRIGHT).pack(pady=(0, 12))
        tk.Label(frame, text="SpotiFLAC is not installed",
                 font=("Consolas", 10), bg=BG, fg=FG).pack()
        tk.Label(frame, text="Requires Python 3.9+ installed on your system",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(pady=(2, 12))

        self._install_btn = ttk.Button(frame, text="[ I N S T A L L ]",
                                        style="SFAccent.TButton",
                                        command=self._do_install)
        self._install_btn.pack(ipadx=16, ipady=4)

        self._install_status = tk.Label(frame, text="", font=("Consolas", 9),
                                         bg=BG, fg=FG_DIM, wraplength=350)
        self._install_status.pack(pady=(8, 0))

        self._install_bar = ttk.Progressbar(frame, mode="indeterminate",
                                             length=300, style="Green.Horizontal.TProgressbar")

    def _do_install(self):
        self._install_btn.state(["disabled"])
        self._install_bar.pack(pady=(8, 0))
        self._install_bar.start(15)
        self._install_status.config(text="Installing spotiflac...", fg=FG_DIM)

        def on_progress(msg):
            self._schedule(lambda: self._install_status.config(text=msg))

        def on_done():
            def _finish():
                self._install_bar.stop()
                self._install_bar.pack_forget()
                global SPOTIFLAC_AVAILABLE
                SPOTIFLAC_AVAILABLE = _check_spotiflac()
                if SPOTIFLAC_AVAILABLE:
                    self._install_status.config(text="Installed! Loading...", fg=GREEN)
                    self.win.after(500, self._switch_to_main_ui)
                else:
                    self._install_status.config(
                        text="Package installed but could not import. Try restarting.",
                        fg=YELLOW,
                    )
                    self._install_btn.state(["!disabled"])
            self._schedule(_finish)

        def on_error(msg):
            def _err():
                self._install_bar.stop()
                self._install_bar.pack_forget()
                self._install_status.config(text=msg, fg=RED_BRIGHT)
                self._install_btn.state(["!disabled"])
            self._schedule(_err)

        install_package("spotiflac", on_progress=on_progress,
                        on_done=on_done, on_error=on_error)

    def _switch_to_main_ui(self):
        for w in self.win.winfo_children():
            w.destroy()
        self._apply_theme()
        self._build_ui()

    # ── UI ──

    def _build_ui(self):
        pad = {"padx": 12, "pady": (0, 0)}

        # Title
        tk.Label(self.win, text="SPOTIFY DOWNLOAD", font=("Consolas", 12, "bold"),
                 bg=BG, fg=FG_BRIGHT).pack(pady=(12, 8))

        # A. URL input
        url_frame = tk.Frame(self.win, bg=BG)
        url_frame.pack(fill="x", **pad)
        tk.Label(url_frame, text="URL", font=("Consolas", 9, "bold"),
                 bg=BG, fg=FG_DIM).pack(anchor="w")
        input_row = tk.Frame(url_frame, bg=BG)
        input_row.pack(fill="x", pady=(2, 0))
        self._url_var = tk.StringVar()
        self._url_var.trace_add("write", lambda *_: self._validate_url())
        self._url_entry = tk.Entry(input_row, textvariable=self._url_var,
                                    font=("Consolas", 10), bg=BG_INPUT, fg=FG,
                                    insertbackground=FG, relief="flat", bd=4)
        self._url_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(input_row, text="Paste", style="SF.TButton",
                   command=self._paste_url).pack(side="left", padx=(4, 0))
        self._url_error = tk.Label(url_frame, text="", font=("Consolas", 8),
                                    bg=BG, fg=RED_BRIGHT, anchor="w")
        self._url_error.pack(anchor="w", pady=(2, 0))

        # B. Download mode
        mode_frame = tk.Frame(self.win, bg=BG)
        mode_frame.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(mode_frame, text="MODE", font=("Consolas", 9, "bold"),
                 bg=BG, fg=FG_DIM).pack(anchor="w")
        self._mode_var = tk.StringVar(value=self.settings.get("default_mode", "library"))
        r1 = ttk.Radiobutton(mode_frame, text="Library + Device",
                              variable=self._mode_var, value="library",
                              style="SF.TRadiobutton")
        r1.pack(anchor="w", pady=(2, 0))
        tk.Label(mode_frame, text="Downloads to your music library, then stage to device",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(anchor="w", padx=(20, 0))
        r2 = ttk.Radiobutton(mode_frame, text="Device only",
                              variable=self._mode_var, value="device",
                              style="SF.TRadiobutton")
        r2.pack(anchor="w", pady=(4, 0))
        tk.Label(mode_frame, text="Downloads directly to device, no library backup",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(anchor="w", padx=(20, 0))

        # C. Settings (collapsible)
        self._settings_expanded = False
        settings_header = tk.Frame(self.win, bg=BG, cursor="hand2")
        settings_header.pack(fill="x", padx=12, pady=(10, 0))
        self._settings_arrow = tk.Label(settings_header, text="▸", font=("Consolas", 9),
                                         bg=BG, fg=FG_DIM)
        self._settings_arrow.pack(side="left")
        settings_title = tk.Label(settings_header, text="SETTINGS", font=("Consolas", 9, "bold"),
                                   bg=BG, fg=FG_DIM)
        settings_title.pack(side="left", padx=(4, 0))
        for w in (settings_header, self._settings_arrow, settings_title):
            w.bind("<Button-1>", lambda e: self._toggle_settings())

        self._settings_frame = tk.Frame(self.win, bg=BG)
        # Not packed initially — collapsed

        sf = self._settings_frame
        sf_pad = {"padx": (20, 12), "pady": (2, 0)}

        # Quality
        q_row = tk.Frame(sf, bg=BG)
        q_row.pack(fill="x", **sf_pad)
        tk.Label(q_row, text="Quality:", font=("Consolas", 9), bg=BG, fg=FG).pack(side="left")
        self._quality_var = tk.StringVar(value=self.settings["quality"])
        for q in ("LOSSLESS", "HI_RES_LOSSLESS", "HIGH"):
            ttk.Radiobutton(q_row, text=q, variable=self._quality_var, value=q,
                            style="SF.TRadiobutton").pack(side="left", padx=(8, 0))

        # Services
        svc_row = tk.Frame(sf, bg=BG)
        svc_row.pack(fill="x", **sf_pad)
        tk.Label(svc_row, text="Services:", font=("Consolas", 9), bg=BG, fg=FG).pack(side="left")
        self._svc_vars = {}
        for svc in ("qobuz", "tidal", "deezer"):
            var = tk.BooleanVar(value=svc in self.settings["services"])
            self._svc_vars[svc] = var
            ttk.Checkbutton(svc_row, text=svc, variable=var,
                            style="SF.TCheckbutton").pack(side="left", padx=(8, 0))

        # Filename format
        fmt_row = tk.Frame(sf, bg=BG)
        fmt_row.pack(fill="x", **sf_pad)
        tk.Label(fmt_row, text="Format:", font=("Consolas", 9), bg=BG, fg=FG).pack(side="left")
        self._fmt_var = tk.StringVar(value=self.settings["filename_format"])
        tk.Entry(fmt_row, textvariable=self._fmt_var, font=("Consolas", 9),
                 bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                 bd=2, width=24).pack(side="left", padx=(8, 0))

        # Track numbers
        self._tracknum_var = tk.BooleanVar(value=self.settings["use_track_numbers"])
        ttk.Checkbutton(sf, text="Prefix with track numbers",
                        variable=self._tracknum_var,
                        style="SF.TCheckbutton").pack(anchor="w", padx=(20, 0), pady=(4, 0))

        # D. Download button
        btn_frame = tk.Frame(self.win, bg=BG)
        btn_frame.pack(fill="x", padx=12, pady=(12, 0))
        self._dl_btn = ttk.Button(btn_frame, text="[ D O W N L O A D ]",
                                   style="SFAccent.TButton",
                                   command=self._on_download_click)
        self._dl_btn.pack(anchor="center", ipadx=16, ipady=4)
        self._dl_btn.state(["disabled"])

        # E. Progress area
        prog_frame = tk.Frame(self.win, bg=BG)
        prog_frame.pack(fill="x", padx=12, pady=(10, 0))
        self._prog_label = tk.Label(prog_frame, text="", font=("Consolas", 9),
                                     bg=BG, fg=FG, anchor="w")
        self._prog_label.pack(fill="x")
        self._prog_bar = ttk.Progressbar(prog_frame, mode="determinate",
                                          style="Green.Horizontal.TProgressbar")
        self._prog_bar.pack(fill="x", pady=(2, 0))

        self._log = tk.Text(prog_frame, height=6, font=("Consolas", 8),
                            bg=BG_INPUT, fg=FG_DIM, relief="flat", bd=4,
                            state="disabled", wrap="word")
        self._log.pack(fill="x", pady=(4, 0))

        # F. Results area (hidden initially)
        self._results_frame = tk.Frame(self.win, bg=BG)
        # Not packed until download completes

        results_inner = self._results_frame
        self._results_summary = tk.Label(results_inner, text="", font=("Consolas", 9, "bold"),
                                          bg=BG, fg=GREEN, anchor="w")
        self._results_summary.pack(fill="x", padx=12, pady=(8, 0))

        self._results_tree = ttk.Treeview(results_inner, columns=("file",),
                                           show="headings", height=5,
                                           selectmode="extended")
        self._results_tree.heading("file", text="Downloaded Files")
        self._results_tree.column("file", width=380)
        self._results_tree.pack(fill="x", padx=12, pady=(4, 0))

        # Drag bindings on results tree
        self._results_tree.bind("<ButtonPress-1>", self._results_drag_start, add="+")
        self._results_tree.bind("<B1-Motion>", self._results_drag_motion, add="+")
        self._results_tree.bind("<ButtonRelease-1>", self._results_drag_drop, add="+")

        action_row = tk.Frame(results_inner, bg=BG)
        action_row.pack(fill="x", padx=12, pady=(6, 8))
        ttk.Button(action_row, text="Create EchoList Playlist",
                   style="SFAccent.TButton",
                   command=self._create_echolist_playlist).pack(side="left", ipadx=8, ipady=2)
        ttk.Button(action_row, text="Keep files only",
                   style="SF.TButton",
                   command=self._dismiss_results).pack(side="left", padx=(8, 0), ipady=2)

    # ── Settings toggle ──

    def _toggle_settings(self):
        self._settings_expanded = not self._settings_expanded
        if self._settings_expanded:
            self._settings_frame.pack(fill="x", after=self._settings_arrow.master)
            self._settings_arrow.config(text="▾")
        else:
            self._settings_frame.pack_forget()
            self._settings_arrow.config(text="▸")

    # ── URL validation ──

    def _validate_url(self):
        url = self._url_var.get().strip()
        if not url:
            self._url_error.config(text="")
            self._dl_btn.state(["disabled"])
            return
        if SPOTIFY_URL_RE.match(url):
            self._url_error.config(text="")
            if not self._downloading:
                self._dl_btn.state(["!disabled"])
        else:
            self._url_error.config(text="Not a valid Spotify URL")
            self._dl_btn.state(["disabled"])

    def _paste_url(self):
        try:
            text = self.win.clipboard_get()
        except tk.TclError:
            return
        text = text.strip()
        if SPOTIFY_URL_RE.match(text):
            self._url_var.set(text)
        else:
            lines = text.splitlines()
            for line in lines:
                line = line.strip()
                if SPOTIFY_URL_RE.match(line):
                    self._url_var.set(line)
                    return
            self._url_var.set(text)

    # ── Callback polling ──

    def _poll_callbacks(self):
        try:
            while True:
                fn = self._callback_queue.get_nowait()
                fn()
        except Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(16, self._poll_callbacks)

    def _schedule(self, fn):
        self._callback_queue.put(fn)

    # ── Download ──

    def _on_download_click(self):
        if self._downloading:
            self._cancel.set()
            self._dl_btn.config(text="Cancelling...")
            self._dl_btn.state(["disabled"])
            return
        self._start_download()

    def _start_download(self):
        url = self._url_var.get().strip()
        if not SPOTIFY_URL_RE.match(url):
            return

        self._save_current_settings()
        self._downloading = True
        self._cancel.clear()
        self._downloaded_files.clear()
        self._failed_tracks.clear()
        self._results_frame.pack_forget()

        self._dl_btn.config(text="[ C A N C E L ]")
        self._prog_bar["value"] = 0
        self._prog_label.config(text="Starting download...")
        self._log_clear()

        mode = self._mode_var.get()
        if mode == "library":
            source_root = Path(self.app.mgr.config.source_root).resolve()
            output_dir = source_root / "SpotiFLAC Downloads"
        else:
            output_dir = Path(tempfile.mkdtemp(prefix="echolist_sf_"))

        services = [s for s, v in self._svc_vars.items() if v.get()]
        if not services:
            services = ["qobuz", "tidal"]

        quality = self._quality_var.get()
        fmt = self._fmt_var.get() or "{title} - {artist}"
        use_track_nums = self._tracknum_var.get()

        Thread(
            target=self._download_worker,
            args=(url, str(output_dir), services, quality, fmt, use_track_nums, mode),
            daemon=True,
        ).start()

    def _download_worker(self, url, output_dir, services, quality, fmt, use_track_nums, mode):
        from SpotiFLAC import SpotiFLAC
        self._schedule(lambda: self._log_append("Starting SpotiFLAC download..."))
        try:
            SpotiFLAC(
                url=url,
                output_dir=output_dir,
                services=services,
                quality=quality,
                filename_format=fmt,
                use_track_numbers=use_track_nums,
                use_album_subfolders=True,
                embed_lyrics=True,
                enrich_metadata=True,
                post_download_action="none",
            )
        except Exception as e:
            self._schedule(lambda: self._on_download_error(str(e)))
            return

        if self._cancel.is_set():
            self._schedule(lambda: self._on_download_cancelled())
            return

        output_path = Path(output_dir)
        audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".opus"}
        files = sorted(
            f for f in output_path.rglob("*")
            if f.is_file() and f.suffix.lower() in audio_exts
        )

        playlist_name = output_path.name
        if playlist_name.startswith("echolist_sf_"):
            match = re.search(r"/([^/]+)$", url)
            playlist_name = match.group(1) if match else "Spotify Download"

        # Try to get a better name from the downloaded folder structure
        subdirs = [d for d in output_path.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            playlist_name = subdirs[0].name

        self._schedule(lambda: self._on_download_complete(files, playlist_name, mode, output_dir))

    def _on_download_complete(self, files, playlist_name, mode, output_dir):
        self._downloading = False
        self._downloaded_files = files
        self._playlist_name = playlist_name
        self._dl_btn.config(text="[ D O W N L O A D ]")
        self._validate_url()

        if not files:
            self._prog_label.config(text="No files downloaded")
            self._log_append("Download completed but no audio files found.")
            return

        self._prog_bar["value"] = 100
        self._prog_label.config(text=f"Downloaded {len(files)} track(s)")
        self._log_append(f"Completed: {len(files)} track(s) downloaded")

        for f in files:
            self._log_append(f"  {f.name}")

        # Generate .m3u8
        m3u_path = Path(output_dir) / f"{playlist_name}.m3u8"
        self._write_m3u8(m3u_path, playlist_name, files, Path(output_dir))
        self._m3u_path = m3u_path
        self._log_append(f"Created {m3u_path.name}")

        # Show results
        self._results_tree.delete(*self._results_tree.get_children())
        for f in files:
            self._results_tree.insert("", "end", values=(f.name,), tags=(str(f),))
        self._results_summary.config(
            text=f"Downloaded {len(files)} track(s) as '{playlist_name}'",
            fg=GREEN,
        )
        self._results_frame.pack(fill="x")

        if mode == "device":
            self._device_output_dir = output_dir

    def _on_download_error(self, error_msg):
        self._downloading = False
        self._dl_btn.config(text="[ D O W N L O A D ]")
        self._validate_url()
        self._prog_label.config(text="Download failed")
        self._log_append(f"ERROR: {error_msg}")

    def _on_download_cancelled(self):
        self._downloading = False
        self._dl_btn.config(text="[ D O W N L O A D ]")
        self._validate_url()
        self._prog_label.config(text="Download cancelled")
        self._log_append("Download cancelled by user")

    # ── .m3u8 generation ──

    def _write_m3u8(self, path: Path, name: str, files: list[Path], base_dir: Path):
        lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
        for f in files:
            try:
                rel = f.relative_to(base_dir).as_posix()
            except ValueError:
                rel = f.name
            title = f.stem
            lines.append(f"#EXTINF:-1,{title}")
            lines.append(rel)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── Post-download actions ──

    def _create_echolist_playlist(self):
        if not hasattr(self, "_m3u_path") or not self._m3u_path.exists():
            return

        if hasattr(self, "_device_output_dir"):
            # Device-only mode: move files to source root first
            source_root = Path(self.app.mgr.config.source_root).resolve()
            lib_dir = source_root / "SpotiFLAC Downloads" / self._playlist_name
            lib_dir.mkdir(parents=True, exist_ok=True)
            new_files = []
            for f in self._downloaded_files:
                dest = lib_dir / f.name
                shutil.copy2(f, dest)
                new_files.append(dest)
            # Rewrite .m3u8 with new paths
            m3u_path = lib_dir / f"{self._playlist_name}.m3u8"
            self._write_m3u8(m3u_path, self._playlist_name, new_files, lib_dir)
            self._m3u_path = m3u_path
            # Clean up temp dir
            try:
                shutil.rmtree(self._device_output_dir)
            except Exception:
                pass
            del self._device_output_dir

        self.app._import_m3u_file(self._m3u_path)
        self._dismiss_results()

    def _dismiss_results(self):
        self._results_frame.pack_forget()
        self._downloaded_files.clear()

    # ── Drag from results to main window ──

    def _results_drag_start(self, event):
        iid = self._results_tree.identify_row(event.y)
        if not iid:
            self._drag_data = None
            return
        self._drag_data = {"started": False, "x": event.x, "y": event.y}

    def _results_drag_motion(self, event):
        if not self._drag_data:
            return
        if not self._drag_data.get("started"):
            dx = abs(event.x - self._drag_data["x"])
            dy = abs(event.y - self._drag_data["y"])
            if dx + dy < 8:
                return
            self._drag_data["started"] = True

    def _results_drag_drop(self, event):
        if not self._drag_data or not self._drag_data.get("started"):
            self._drag_data = None
            return
        self._drag_data = None

        rx, ry = event.x_root, event.y_root
        target = self.win.winfo_containing(rx, ry)
        if target is None:
            return

        # Check if dropped over main app's track tree or playlist tree
        main_widgets = []
        try:
            main_widgets = [self.app.track_tree, self.app.playlist_tree]
        except AttributeError:
            return

        dropped_on_main = False
        for w in main_widgets:
            try:
                wx = w.winfo_rootx()
                wy = w.winfo_rooty()
                ww = w.winfo_width()
                wh = w.winfo_height()
                if wx <= rx <= wx + ww and wy <= ry <= wy + wh:
                    dropped_on_main = True
                    break
            except Exception:
                continue

        if dropped_on_main:
            selected = self._results_tree.selection()
            if not selected:
                return
            paths = []
            for iid in selected:
                tags = self._results_tree.item(iid, "tags")
                if tags:
                    p = Path(tags[0])
                    if p.is_file():
                        paths.append(p)
            if paths and self.app.current_pid:
                self.app._stage_add_files(paths)

    # ── Log helpers ──

    def _log_append(self, text):
        self._log.config(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.config(state="disabled")

    def _log_clear(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

    # ── Settings persistence ──

    def _save_current_settings(self):
        self.settings["quality"] = self._quality_var.get()
        self.settings["services"] = [s for s, v in self._svc_vars.items() if v.get()]
        self.settings["filename_format"] = self._fmt_var.get()
        self.settings["use_track_numbers"] = self._tracknum_var.get()
        self.settings["default_mode"] = self._mode_var.get()
        try:
            self.settings["window_geometry"] = self.win.geometry()
        except Exception:
            pass
        _save_settings(self.settings)

    # ── Close ──

    def _on_close(self):
        if hasattr(self, "_quality_var"):
            self._save_current_settings()
        if self._downloading:
            self._cancel.set()
        self.win.destroy()
        self.app._spotify_window = None
