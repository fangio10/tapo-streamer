from PIL import Image, ImageTk, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import tkinter.filedialog as filedialog
import re
import json
import os
import shutil
import threading
import time
import logging
import sys
import socket
from datetime import datetime
import shlex
import argparse
import math
from urllib.parse import quote

if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
    _lib_candidates = (
        '/usr/lib64/libvlc.so.5',                       # Fedora/RHEL
        '/usr/lib/x86_64-linux-gnu/libvlc.so.5',        # Debian/Ubuntu amd64
        '/usr/lib/i386-linux-gnu/libvlc.so.5',          # Debian/Ubuntu i386
        '/usr/lib/aarch64-linux-gnu/libvlc.so.5',       # Debian/Ubuntu arm64
        '/usr/local/lib/libvlc.so.5',
    )
    _plugin_candidates = (
        '/usr/lib64/vlc/plugins',
        '/usr/lib/x86_64-linux-gnu/vlc/plugins',
        '/usr/lib/i386-linux-gnu/vlc/plugins',
        '/usr/lib/aarch64-linux-gnu/vlc/plugins',
        '/usr/lib/vlc/plugins',
        '/usr/local/lib/vlc/plugins',
    )
    for _candidate in _lib_candidates:
        if os.path.isfile(_candidate):
            os.environ.setdefault('PYTHON_VLC_LIB_PATH', _candidate)
            break
    for _candidate in _plugin_candidates:
        if os.path.isdir(_candidate):
            os.environ.setdefault('VLC_PLUGIN_PATH', _candidate)
            break

import vlc
import ctypes

class tapoStreamer:
    DEFAULT_VLC_PARAMS = [
        "--avcodec-hw=any",
        "--network-caching=3000",
    ]

    @classmethod
    def parse_vlcparams(cls, raw, default=None):
        default = default if default is not None else cls.DEFAULT_VLC_PARAMS
        try:
            if isinstance(raw, (list, tuple)):
                params = list(raw)
            else:
                raw_str = (raw or "").strip()
                params = shlex.split(raw_str) if raw_str else []
            valid_params = [p for p in params if p.startswith('--')]
            return valid_params or default
        except Exception as e:
            logging.error(f"Failed to parse VLC parameters '{raw}': {e}", exc_info=True)
            return default

    MIN_WIDTH = 1340
    MIN_HEIGHT = 720
    FONT_CANDIDATES = ["Verdana", "Tahoma", "Arial", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Noto Sans"]
    FONT_FALLBACK = "Helvetica"  # Built-in Tk alias, always resolvable

    # Raw filename tokens (case-insensitive) -> canonical id.
    DETECTION_TYPE_ALIASES = {
        "motion": "motion",
        "person": "person",
        "pet": "pet",
        "vehicle": "vehicle",
        "baby_cry": "baby_cry",
        "bark": "bark",
        "meow": "meow",
        "line_crossing": "line_crossing",
        "area_intrusion": "area_intrusion",
        "tamper": "tamper",
        "glass_break": "glass_break",
        "smoke": "smoke",
        "face": "face",
        "loitering": "loitering",
        "package_delivered": "package_delivered",
        "package_picked_up": "package_picked_up",
    }

    # Canonical id -> display label, in default order.
    DETECTION_TYPE_LABELS = {
        "motion": "Motion",
        "person": "Person",
        "pet": "Pet",
        "vehicle": "Vehicle",
        "face": "Face",
        "line_crossing": "Line Crossing",
        "area_intrusion": "Area Intrusion",
        "loitering": "Loitering",
        "package_delivered": "Package Delivered",
        "package_picked_up": "Package Picked Up",
        "tamper": "Tamper",
        "baby_cry": "Baby Crying",
        "bark": "Bark",
        "meow": "Meow",
        "glass_break": "Glass Break",
        "smoke": "Smoke",
    }

    ALL_TYPES_LABEL = "All Types"

    # False: lock Archive/Events buttons only during mode transitions.
    # True: also lock them while any stream is initializing.
    LOCK_MODE_BUTTONS_DURING_INIT = False

    # Trust a cached day only if its scan ran this long after the newest folder mtime
    # (coarse mtime resolution, clock skew on network shares).
    EVENTS_CACHE_TRUST_MARGIN_NS = 5_000_000_000

    # Downgrade rule (see _net_sample): per-second starvation score feeds a decaying pressure.
    NET_STARVE_FULL_KB = 6.0      # at or below: fully starved
    NET_STARVE_NONE_KB = 14.0     # at or above: not starved
    NET_PRESSURE_TAU_S = 20.0     # pressure decay time constant
    # Trigger pressure per sensitivity level (1-5, 0 = off).
    NET_TRIGGER_BY_LEVEL = {1: 4.5, 2: 3.0, 3: 2.0, 4: 1.5, 5: 1.0}
    DOWNGRADE_LEVEL_LABELS = ("Off", "Very low", "Low", "Medium", "High", "Very high")
    # Failed HQ inits (camera reachable, no frames) before falling back to LQ.
    HQ_INIT_FAILS_BEFORE_LQ = 2

    @classmethod
    def normalize_detection_type(cls, raw):
        """Map a raw filename token to a canonical id; unknown tokens become their own id."""
        if not raw:
            return None
        key = raw.strip().lower()
        if key in cls.DETECTION_TYPE_ALIASES:
            return cls.DETECTION_TYPE_ALIASES[key]
        if key:
            return key
        return None

    @classmethod
    def detection_type_label(cls, canonical_id):
        if canonical_id in cls.DETECTION_TYPE_LABELS:
            return cls.DETECTION_TYPE_LABELS[canonical_id]
        # Unknown id: prettify (foo_bar -> Foo Bar)
        return canonical_id.replace("_", " ").title()

    def check_decoder_availability(self):
        # Best-effort check for a hardware-capable ffmpeg h264 decoder.
        if not sys.platform.startswith("linux"):
            return
        try:
            import subprocess
            result = subprocess.run(
                ['ffmpeg', '-decoders'],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout
            h264_lines = [l for l in output.splitlines() if 'h264' in l.lower()]
            has_hw_capable = any(
                ' h264 ' in l or l.strip().split()[-1] == 'h264'
                for l in h264_lines if 'libopenh264' not in l and 'v4l2m2m' not in l
            )
            only_openh264 = h264_lines and all('libopenh264' in l or 'v4l2m2m' in l for l in h264_lines)
            if only_openh264 or not has_hw_capable:
                logging.warning(
                    "ffmpeg appears to only provide software h264 decoding "
                    "(libopenh264) - hardware acceleration will not be available. "
                    "On Fedora/RHEL, install ffmpeg from RPM Fusion for a "
                    "hardware-capable build (h264 with vaapi/cuda/qsv support)."
                )
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _setup_logging(self, debug_mode):
        logging.getLogger().handlers = []

        if not debug_mode:
            null_handler = logging.NullHandler()
            logging.getLogger().addHandler(null_handler)
            logging.getLogger().setLevel(logging.CRITICAL + 1)
            return

        # INFO, not DEBUG, to keep third-party chatter (onvif, urllib3, PIL) out of the log.
        log_level = logging.INFO
        log_dir = os.path.dirname(self.config_file)
        log_file = os.path.join(log_dir, "tapo-streamer.log")

        # Rotating log (5 MB x 3); thread name in the format helps trace Tk vs worker threads.
        from logging.handlers import RotatingFileHandler
        _handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
        _handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(threadName)s] %(levelname)s - %(message)s'))
        logging.basicConfig(level=log_level, handlers=[_handler])

        zeep_logger = logging.getLogger('zeep')
        zeep_logger.propagate = False 

        logging.getLogger('zeep.xsd.types.simple').setLevel(logging.WARNING)

        logging.info("Logging initialized with level INFO")

    def __init__(self, root):
        parser = argparse.ArgumentParser(description="Tapo Streamer Application")
        parser.add_argument('--debug', action='store_true', help="Enable debug logging")
        args = parser.parse_args()

        self.root = root
        self.root.title("Tapo Streamer")
        self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)

        if sys.platform.startswith("linux"):
            config_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        else:
            config_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "config.json")
        self.watch_progress_file = os.path.join(config_dir, "watch_progress.json")

        self.debug_mode = args.debug
        self.speed_cycle = [1.0, 2.0, 4.0, 8.0]
        self._init_font_choices()
        self.load_config()
        if not args.debug and hasattr(self, 'config_debug'):
            self.debug_mode = self.config_debug
        self._setup_logging(self.debug_mode)
        threading.Thread(target=self.check_decoder_availability, daemon=True).start()

        if getattr(sys, 'frozen', False):
            self.base_path = sys._MEIPASS
            if sys.platform.startswith('win'):
                os.environ['VLC_PLUGIN_PATH'] = os.path.join(self.base_path, 'vlc', 'plugins')
            # On Linux, VLC_PLUGIN_PATH is set at module load (see top).
        else:
            self.base_path = os.path.dirname(os.path.abspath(__file__))

        icon_path = os.path.join(self.base_path, "cam.png")
        try:
            img = Image.open(icon_path)
            img_titlebar = img.resize((64, 64), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img_titlebar)
            self.root.iconphoto(True, icon)
        except Exception as e:
            logging.error(f"Error loading icon: {e}", exc_info=True)

        self.root.configure(bg="#222222")
        self.apply_theme()
        self.root.minsize(1340, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.running = True
        self.is_fullscreen = False
        self.fullscreen_index = None
        self.help_overlay = None
        self.vlc_instances = [None] * 4
        # Serialises stream starts (instance -> player -> play()); concurrent starts
        # segfaulted inside libvlc.
        self._vlc_start_lock = threading.Lock()
        # Serialises runtime audio (mute) calls into libvlc.
        self._vlc_audio_lock = threading.Lock()
        self.stream_initializing = [False] * 4
        self.stream_init_lock = threading.Lock()
        self.stream_cleanup_events = [threading.Event() for _ in range(4)]
        # Abort signal for an in-flight init. Only the init's own finally block clears it,
        # so cleanup_stream() can't swallow an abort.
        self.stream_abort_events = [threading.Event() for _ in range(4)]
        # Bumped when a clip start is superseded or cancelled, so pending worker plays bail out.
        self._play_tokens = [0] * 4
        # monitor_stream thread per index; cleanup_stream waits for it before releasing the player.
        self.monitor_threads = [None] * 4
        self.archive_entry_locks = [threading.Lock() for _ in range(4)]
        # Runtime-only LQ downgrade flag; hq_enabled stays the configured value.
        self.session_lq = [False] * 4
        # Per-stream state for _net_sample; reset by cleanup_stream.
        self.net_state = [None] * 4
        # Consecutive HQ inits that reached the camera but got no frames.
        self.hq_init_failures = [0] * 4
        # Indices whose VLC teardown is still running on a background thread. update_layout()
        # must not rebind these as live streams while a stale clip player is releasing.
        self.pending_vlc_teardown = set()
        self.archive_transitioning = [False] * 4
        # In-flight mode transitions (Tk thread only). While > 0, Archive/Events buttons are
        # locked and mode handlers refuse to start another transition.
        self._mode_busy = 0
        self._mode_busy_since = 0.0
        self._exit_event_pending = False
        self.media_players = [None] * 4
        self.streams = [""] * 4
        self.panels = [None] * 4
        self.labels = [None] * 4
        self.fullscreen_buttons = [None] * 4
        self.exit_fullscreen_button = None
        self.exit_fullscreen_image = None

        self.onvif_cams = {}
        self.onvif_fail_until = {}
        self.ptz_busy = False
        self.ptz_buttons_disabled = False
        self.ptz_lock = threading.Lock()

        self.is_archive_mode = [False] * 4 
        self.archive_mode_button = None
        self.archive_mode_image = None
        self.current_archive_path = [None] * 4
        # Playback speed is global, seeded from the configured default.
        self.global_playback_speed = self.default_playback_speed
        self.is_paused = [False] * 4
        self.video_ended = [False] * 4
        self.pagination_state = [{} for _ in range(4)]

        self.config_button = None
        self.ptz_buttons = []
        self.ptz_images = []
        # Global speed-cycle button; lives in config_panel, not per-quadrant.
        self.speed_toggle_button = None
        self.speed_toggle_image = None
        self.archive_buttons = [None] * 4
        self.archive_canvas = [None] * 4
        self.back_buttons = [None] * 4
        self.exit_buttons = [None] * 4
        self.pause_buttons = [None] * 4
        self.ff_buttons = [None] * 4
        self.replay_buttons = [None] * 4
        self.rewind_buttons = [None] * 4
        self.audio_buttons = [None] * 4
        self.archive_audio_muted = [True] * 4
        self.event_mode = False

        # Hover polling for clip controls: on Windows the VLC HWND swallows Tk <Enter>/<Leave>,
        # so the pointer is polled. Display-only; actions use real Tk buttons.
        self._hover_poll_ids = [None] * 4
        self._clip_controls_visible = [False] * 4

        self.event_clip_queues = [[] for _ in range(4)]
        self.event_active_cams = set()
        self.event_done_cams = set()
        self.event_overlay = None
        self.current_playing_event = None
        # True if any clip was skipped during this event playback (so not a full watch).
        self._event_clips_skipped = False
        # Wall-clock time the first clip of the current event started (None until then).
        self._event_first_clip_started_at = None
        # Dicts: {kind, after_id, index, path, unscaled_ms, scheduled_at, scheduled_speed}; kind is
        # "clip_launch" or "ramp_step". Lets cycle_speed() reschedule pending delays.
        self._pending_event_afters = []
        self.events_button = None
        self.events_button_image = None
        self.event_back_button = None
        self.event_back_button_image = None
        self.watch_progress = {index: {} for index in range(4)}
        self.watch_progress_dirty = False
        self.visited_folders = {index: set() for index in range(4)}

        # Sleep mode: stops live streams after sleep_mode_minutes unfocused/minimized and
        # restarts them on focus. 0 = disabled.
        self._sleep_timer_id = None
        self._is_asleep = False
        self._sleep_stopped_indices = []
        # Hidden-behind-fullscreen sleep: per-stream timer handle and stopped flag.
        self._bg_sleep_timers = [None] * 4
        self._bg_asleep = [False] * 4
        self._bg_reconcile_pending = False
        self._app_focused = True
        self._app_minimized = False

        self.panel_sizes = [(0, 0)] * 4
        self.target_dims = [(0, 0)] * 4
        self.frame_shapes = [(0, 0)] * 4
  
        self.last_dropped_frames = {}  # Last cumulative dropped frames per stream
        self.last_displayed_frames = {}  # Last cumulative displayed frames per stream
        for i in range(len(self.ips)):
            self.last_dropped_frames[i] = 0
            self.last_displayed_frames[i] = 0

        self.icon_cache = {
            "up": self.create_icon("up"),
            "down": self.create_icon("down"),
            "left": self.create_icon("left"),
            "right": self.create_icon("right"),
            "minimize": self.create_icon("minimize"),
            "config": self.create_icon("config"),
            "disk": self.create_icon("disk"),
            "fullscreen": self.create_icon("fullscreen"),
            "pause": self.create_icon("pause"),
            "play": self.create_icon("play"),
            "speed": self.create_icon("speed"),
            "replay": self.create_icon("replay"),
            "rewind": self.create_icon("rewind"),
            "exit": self.create_icon("exit"),
            "back": self.create_icon("back"),
            "folder": self.create_icon("folder", opacity=1.0),
            "folder_clicked": self.create_icon("folder", opacity=0.6),
            "archive": self.create_icon("archive", opacity=1.0),
            "events": self.create_icon("events"),
            "list": self.create_icon("list"),
            "delete": self.create_icon("delete"),
            "download": self.create_icon("download"),
            "audio_on": self.create_icon("audio_on"),
            "audio_off": self.create_icon("audio_off"),
            "eye": self.create_icon("eye"),
        }
        # Red variants shown while Archive/Events mode is active.
        self.icon_cache["disk_active"] = self.recolor_icon_active(self.icon_cache["disk"])
        self.icon_cache["events_active"] = self.recolor_icon_active(self.icon_cache["events"])
        # Dimmed variants shown while the button is disabled (Tk's native rendering is inconsistent).
        self.icon_cache["disk_disabled"] = self.dim_icon(self.icon_cache["disk"])
        self.icon_cache["events_disabled"] = self.dim_icon(self.icon_cache["events"])
        # Half-opacity eye: event exited partway (session-only). Full eye: complete watch (persisted).
        self.icon_cache["eye_partial"] = self.dim_icon(self.icon_cache["eye"], opacity=0.5)

        self.day_folder_icon_cache = {}
        self.speed_icon_cache = {}
        self.thumbnail_cache = {}
        self.thumbnail_cache_order = []
        self.thumbnail_cache_max = 200

        self.load_watch_progress()
        self.init_ui()
        self.update_streams()
        self.root.after(0, lambda: threading.Thread(target=self.start_streams, daemon=True).start())

    # Fixed UI colors (theme switching was removed).
    UI_COLORS = {
        "bg": "#f0f0f0", "bg_alt": "#e2e2e2", "fg": "#1a1a1a",
        "fg_dim": "#666666", "accent": "#c8261a",
        "field_bg": "#ffffff", "border": "#bbbbbb",
    }

    def apply_theme(self):
        """Apply the fixed UI colors to ttk and plain Tk widgets. Video panels stay black."""
        palette = self.UI_COLORS
        self.active_theme_colors = palette

        try:
            bg = palette["bg"]
            bg_alt = palette["bg_alt"]
            fg = palette["fg"]
            fg_dim = palette["fg_dim"]
            accent = palette["accent"]
            field_bg = palette["field_bg"]
            border = palette["border"]

            style = ttk.Style(self.root)
            style.theme_use("clam")

            style.configure(".", background=bg, foreground=fg,
                             fieldbackground=field_bg, bordercolor=border,
                             darkcolor=bg, lightcolor=bg_alt,
                             troughcolor=bg_alt, focuscolor=accent)

            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.configure("TSeparator", background=border)

            style.configure("TButton", background=bg_alt, foreground=fg,
                             bordercolor=border, focusthickness=1)
            style.map("TButton",
                      background=[("active", border), ("disabled", bg)],
                      foreground=[("disabled", fg_dim)])

            style.configure("TCheckbutton", background=bg, foreground=fg)
            style.map("TCheckbutton",
                      background=[("active", bg)],
                      foreground=[("disabled", fg_dim)])

            style.configure("TEntry", fieldbackground=field_bg, foreground=fg,
                             insertcolor=fg, bordercolor=border)

            style.configure("TCombobox", fieldbackground=field_bg, background=bg_alt,
                             foreground=fg, arrowcolor=fg, bordercolor=border)
            style.map("TCombobox",
                      fieldbackground=[("readonly", field_bg)],
                      foreground=[("disabled", fg_dim)])
            self.root.option_add("*TCombobox*Listbox.background", field_bg)
            self.root.option_add("*TCombobox*Listbox.foreground", fg)
            self.root.option_add("*TCombobox*Listbox.selectBackground", accent)

            style.configure("TNotebook", background=bg, bordercolor=border)
            style.configure("TNotebook.Tab", background=bg_alt, foreground=fg_dim,
                             padding=(12, 6))
            # clam overrides tab padding for the selected tab; pin it so all tabs match.
            style.map("TNotebook.Tab",
                      background=[("selected", bg)],
                      foreground=[("selected", fg)],
                      padding=[("selected", (12, 6))],
                      expand=[("selected", (0, 0, 0, 0))])

            style.configure("Vertical.TScrollbar", background=bg_alt,
                             troughcolor=bg, arrowcolor=fg, bordercolor=border)
            style.configure("Horizontal.TScrollbar", background=bg_alt,
                             troughcolor=bg, arrowcolor=fg, bordercolor=border)

            # Toplevel dialogs don't inherit root's bg; set defaults for new widgets.
            for widget_class in ("Toplevel", "Frame", "Label", "Text"):
                self.root.option_add(f"*{widget_class}.background", bg)
                self.root.option_add(f"*{widget_class}.foreground", fg)
            self.root.option_add("*Entry.background", field_bg)
            self.root.option_add("*Entry.foreground", fg)
            self.root.option_add("*Entry.insertBackground", fg)
            self.root.option_add("*Text.background", field_bg)
            self.root.option_add("*Text.foreground", fg)
            self.root.option_add("*Text.insertBackground", fg)
            self.root.option_add("*Button.background", bg_alt)
            self.root.option_add("*Button.foreground", fg)
            self.root.option_add("*Button.activeBackground", border)

            self.root.configure(bg=bg)
        except Exception as e:
            logging.warning(f"Failed to apply theme: {e}")

    def _init_font_choices(self):
        try:
            installed = {name.lower() for name in tkfont.families(self.root)}
        except Exception as e:
            logging.warning(f"Could not query installed fonts, using fallback: {e}")
            installed = set()

        available = [name for name in self.FONT_CANDIDATES if name.lower() in installed]
        if not available:
            available = [self.FONT_FALLBACK]
        available = available[:5]  # keep dropdown short

        self.font_choices = [{"label": name, "family": name, "weight": "normal"} for name in available]

        bold_base = available[0]
        self.font_choices.append({"label": f"{bold_base} Bold", "family": bold_base, "weight": "bold"})

        self.font_choice_labels = [choice["label"] for choice in self.font_choices]

    def app_font(self, size, style=None):
        choice = next(
            (c for c in self.font_choices if c["label"] == self.ui_font),
            self.font_choices[0]
        )
        weight = style if style is not None else choice["weight"]
        return (choice["family"], size, weight)

    @staticmethod
    def _fit_list(value, default, n=4):
        """Return exactly n items from value, padded from default; non-lists give default."""
        if not isinstance(value, (list, tuple)):
            return list(default)
        out = list(value)[:n]
        out += list(default)[len(out):n]
        return out

    def _backup_config_file(self, reason):
        """Copy the current config aside before anything overwrites it."""
        try:
            backup = f"{self.config_file}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(self.config_file, backup)
            logging.warning(f"Config backed up to {backup} ({reason})")
        except Exception as e:
            logging.error(f"Could not back up config file: {e}")

    def load_config(self):
        self.username = ""
        self.password = ""
        self.archive_dir = ""
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True] * 4
        self.audio_enabled = [True] * 4
        self.ptz_supported = [False] * 4
        self.config_debug = False
        self.vlcparams = self.DEFAULT_VLC_PARAMS
        self.ptz_resolution = 3
        self.saved_window_size = "1340x720"
        self.enable_fullscreen_buttons = False
        self.default_playback_speed = 1.0
        self.enable_retries = True
        self.max_retry_attempts = 5
        self.initial_backoff_delay = 2.0
        self.downgrade_sensitivity = 3   # 0 = off, 1 (least) .. 5 (most sensitive)
        self.no_frame_timeout = 15.0
        self.ui_font = self.font_choice_labels[0]
        self.resume_playback = True
        self.motion_triggered_events = False
        self.event_overlap_window_mins = 1
        self.exclusive_archive_audio = True
        self.controls_position = "top-left"
        self.default_event_filter = []
        self.sleep_mode_minutes = 0
        self.sleep_hidden_streams = False
        self.events_show_download = False
        self.events_show_delete = False
        self.event_partial_min_watch_s = 5.0

        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                self.username = config.get("username", self.username)
                self.password = config.get("password", self.password)
                self.archive_dir = config.get("archive_dir", self.archive_dir)
                self.ips = [str(x) if x else "" for x in self._fit_list(config.get("ips"), self.ips)]
                self.hq_enabled = [bool(x) for x in self._fit_list(config.get("hq_enabled"), self.hq_enabled)]
                self.audio_enabled = [bool(x) for x in self._fit_list(config.get("audio_enabled"), self.audio_enabled)]
                self.ptz_supported = [bool(x) for x in self._fit_list(config.get("ptz_supported"), self.ptz_supported)]
                self.config_debug = config.get("debug", self.config_debug)
                self.vlcparams = self.parse_vlcparams(config.get("vlcparams", self.vlcparams), default=self.vlcparams)
                self.ptz_resolution = config.get("ptz_resolution", self.ptz_resolution)
                self.saved_window_size = config.get("saved_window_size", self.saved_window_size)
                self.enable_fullscreen_buttons = config.get("enable_fullscreen_buttons", self.enable_fullscreen_buttons)
                self.default_playback_speed = config.get("default_playback_speed", self.default_playback_speed)
                self.enable_retries = config.get("enable_retries", self.enable_retries)
                self.max_retry_attempts = config.get("max_retry_attempts", self.max_retry_attempts)
                self.initial_backoff_delay = config.get("initial_backoff_delay", self.initial_backoff_delay)
                if "downgrade_sensitivity" in config:
                    try:
                        self.downgrade_sensitivity = max(0, min(5, int(config["downgrade_sensitivity"])))
                    except (ValueError, TypeError):
                        logging.warning("Invalid downgrade_sensitivity, using default 3")
                raw_no_frame = config.get("no_frame_timeout", self.no_frame_timeout)
                self.no_frame_timeout = float(raw_no_frame) if raw_no_frame > 5 else 15.0
                raw_font = config.get("ui_font", self.ui_font)
                matched_font = next(
                    (label for label in self.font_choice_labels if label.lower() == str(raw_font).lower()),
                    None
                )
                self.ui_font = matched_font if matched_font else self.font_choice_labels[0]
                self.resume_playback = bool(config.get("resume_playback", self.resume_playback))
                self.motion_triggered_events = bool(config.get("motion_triggered_events", self.motion_triggered_events))
                self.event_overlap_window_mins = int(config.get("event_overlap_window_mins", self.event_overlap_window_mins))
                if self.event_overlap_window_mins not in (1, 2, 3, 5):
                    self.event_overlap_window_mins = 1
                self.exclusive_archive_audio = bool(config.get("exclusive_archive_audio", self.exclusive_archive_audio))
                raw_cp = config.get("controls_position", self.controls_position)
                self.controls_position = raw_cp if raw_cp in self.CONTROL_POSITIONS else "top-left"
                raw_default_filter = config.get("default_event_filter", self.default_event_filter)
                self.default_event_filter = (
                    [str(t).lower() for t in raw_default_filter]
                    if isinstance(raw_default_filter, list) else []
                )
                self.sleep_hidden_streams = bool(config.get("sleep_hidden_streams", self.sleep_hidden_streams))
                self.events_show_download = bool(config.get("events_show_download", self.events_show_download))
                self.events_show_delete = bool(config.get("events_show_delete", self.events_show_delete))
                try:
                    self.event_partial_min_watch_s = float(config.get("event_partial_min_watch_s", self.event_partial_min_watch_s))
                    if self.event_partial_min_watch_s < 0:
                        logging.warning(f"Invalid event_partial_min_watch_s: {self.event_partial_min_watch_s}, using default 5.0")
                        self.event_partial_min_watch_s = 5.0
                except (ValueError, TypeError):
                    logging.warning("Invalid event_partial_min_watch_s input, using default 5.0")
                    self.event_partial_min_watch_s = 5.0
                try:
                    self.sleep_mode_minutes = int(config.get("sleep_mode_minutes", self.sleep_mode_minutes))
                    if self.sleep_mode_minutes < 0:
                        logging.warning(f"Invalid sleep_mode_minutes: {self.sleep_mode_minutes}, using default 0")
                        self.sleep_mode_minutes = 0
                except (ValueError, TypeError):
                    logging.warning(f"Invalid sleep_mode_minutes input, using default 0")
                    self.sleep_mode_minutes = 0

                try:
                    if self.saved_window_size != "fullscreen":
                        width, height = map(int, self.saved_window_size.split("x"))
                        if width < self.MIN_WIDTH or height < self.MIN_HEIGHT:
                            logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                            self.saved_window_size = "1340x720"
                except (ValueError, TypeError):
                    logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                    self.saved_window_size = "1340x720"

                if not isinstance(self.ptz_resolution, int) or self.ptz_resolution < 1 or self.ptz_resolution > 5:
                    self.ptz_resolution = 3

                try:
                    self.default_playback_speed = float(self.default_playback_speed)
                    if self.default_playback_speed not in self.speed_cycle:
                        logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                        self.default_playback_speed = 1.0
                except (ValueError, TypeError):
                    logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                    self.default_playback_speed = 1.0

                if self.max_retry_attempts < 1:
                    logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 3")
                    self.max_retry_attempts = 5
                if self.initial_backoff_delay <= 0:
                    logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 1.0")
                    self.initial_backoff_delay = 2.0

            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse config file {self.config_file}: {e}. Using default settings.")
                self._backup_config_file("invalid JSON")
                self.save_config()
            except PermissionError as e:
                logging.error(f"Permission denied accessing config file {self.config_file}: {e}. Using default settings.")
            except Exception as e:
                logging.error(f"Unexpected error loading config file {self.config_file}: {e}", exc_info=True)
                self._backup_config_file("unexpected load error")
                self.save_config()
        else:
            logging.info(f"Config file {self.config_file} does not exist. Creating with default settings.")
            self.save_config()

        # Windows: the embedded VLC HWND swallows Tk mouse events, so the real Tk buttons are
        # always required. Overrides any saved value.
        if sys.platform.startswith('win'):
            self.enable_fullscreen_buttons = True

    def save_config(self):
        config = {
            "username": self.username,
            "password": self.password,
            "archive_dir": self.archive_dir,
            "vlcparams": self.vlcparams,
            "ips": self.ips,
            "hq_enabled": self.hq_enabled,
            "audio_enabled": self.audio_enabled,
            "ptz_supported": self.ptz_supported,
            "debug": self.config_debug,
            "ptz_resolution": self.ptz_resolution,
            "saved_window_size": self.saved_window_size,
            "enable_fullscreen_buttons": self.enable_fullscreen_buttons,
            "default_playback_speed": self.default_playback_speed,
            "enable_retries": self.enable_retries,
            "max_retry_attempts": self.max_retry_attempts,
            "initial_backoff_delay": self.initial_backoff_delay,
            "downgrade_sensitivity": self.downgrade_sensitivity,
            "no_frame_timeout": self.no_frame_timeout,
            "ui_font": self.ui_font,
            "resume_playback": self.resume_playback,
            "motion_triggered_events": self.motion_triggered_events,
            "event_overlap_window_mins": self.event_overlap_window_mins,
            "exclusive_archive_audio": self.exclusive_archive_audio,
            "controls_position": self.controls_position,
            "default_event_filter": self.default_event_filter,
            "sleep_mode_minutes": self.sleep_mode_minutes,
            "sleep_hidden_streams": self.sleep_hidden_streams,
            "events_show_download": self.events_show_download,
            "events_show_delete": self.events_show_delete,
            "event_partial_min_watch_s": self.event_partial_min_watch_s,
        }
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            # Write to a temp file (owner-only, holds the camera password), then atomically swap it in.
            tmp_path = self.config_file + ".tmp"
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_file)
        except PermissionError as e:
            logging.error(f"Permission denied saving config to {self.config_file}: {e}")
            messagebox.showerror("Error", f"Failed to save configuration due to permission issues: {e}")
        except Exception as e:
            logging.error(f"Failed to save config to {self.config_file}: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to save configuration: {e}")

    def load_watch_progress(self):
        if not os.path.exists(self.watch_progress_file):
            return
        try:
            with open(self.watch_progress_file, "r") as f:
                data = json.load(f)
            for index in range(4):
                entries = data.get(str(index), {})
                if isinstance(entries, dict):
                    cleaned = {}
                    for path, info in entries.items():
                        try:
                            position = float(info.get("position", 0))
                            duration = float(info.get("duration", 0))
                            if duration > 0 and position >= 0:
                                cleaned[path] = {"position": position, "duration": duration}
                        except (TypeError, ValueError, AttributeError):
                            continue
                    # Drop entries for clips that no longer exist, but only if the archive folder is
                    # reachable (an offline share must not wipe everything).
                    if self.archive_dir and os.path.isdir(self.archive_dir):
                        before = len(cleaned)
                        cleaned = {p: v for p, v in cleaned.items() if os.path.exists(p)}
                        if len(cleaned) != before:
                            self.watch_progress_dirty = True
                    self.watch_progress[index] = cleaned
        except Exception as e:
            logging.warning(f"Failed to load watch progress from {self.watch_progress_file}: {e}")

    def save_watch_progress(self):
        try:
            # Clear the flag first: anything recorded during the write re-marks it dirty.
            self.watch_progress_dirty = False
            try:
                # Snapshot via list(...items()) so a monitor thread can't break iteration mid-dump.
                data = {
                    str(index): {p: dict(v) for p, v in list(self.watch_progress[index].items())}
                    for index in range(4)
                }
                os.makedirs(os.path.dirname(self.watch_progress_file), exist_ok=True)
                tmp_path = self.watch_progress_file + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.watch_progress_file)
            except Exception:
                self.watch_progress_dirty = True
                raise
        except Exception as e:
            logging.warning(f"Failed to save watch progress to {self.watch_progress_file}: {e}")

    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuration")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        notebook = ttk.Notebook(dialog)
        notebook.pack(pady=10, padx=10, fill="both", expand=True)

        connection_frame = ttk.Frame(notebook)
        notebook.add(connection_frame, text="Connection")
        connection_frame.columnconfigure(1, weight=1)

        core_frame = ttk.Frame(notebook)
        notebook.add(core_frame, text="General")
        core_frame.columnconfigure(1, weight=1)

        events_frame = ttk.Frame(notebook)
        notebook.add(events_frame, text="Events")
        events_frame.columnconfigure(1, weight=1)

        advanced_frame = ttk.Frame(notebook)
        notebook.add(advanced_frame, text="Advanced")
        advanced_frame.columnconfigure(1, weight=1)

        LBL  = dict(sticky="w",  padx=(12, 6), pady=4)
        WIDE = dict(sticky="we", padx=(0,  12), pady=4)
        SPAN = dict(sticky="w",  padx=(12, 12), pady=4, columnspan=2)

        def add_section_header(frame, text, row):
            """Draw a bold header and separator at `row`; return the next free row."""
            tk.Label(frame, text=text, font=self.app_font(10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
            )
            row += 1
            ttk.Separator(frame, orient="horizontal").grid(
                row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
            )
            return row + 1

        # --- Connection Tab ---
        conn_row = 0

        conn_row = add_section_header(connection_frame, "Connection", conn_row)

        tk.Label(connection_frame, text="Username:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        username_entry = tk.Entry(connection_frame, width=32)
        username_entry.insert(0, self.username)
        username_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        tk.Label(connection_frame, text="Password:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        password_entry = tk.Entry(connection_frame, width=32, show="*")
        password_entry.insert(0, self.password)
        password_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        tk.Label(connection_frame, text="Video Path:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        archive_entry = tk.Entry(connection_frame, width=32)
        archive_entry.insert(0, self.archive_dir)
        archive_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        conn_row = add_section_header(connection_frame, "Cameras", conn_row)

        ip_entries = []
        hq_checkboxes = []
        audio_checkboxes = []
        ptz_checkboxes = []
        for i in range(4):
            tk.Label(connection_frame, text=f"Cam {i+1} IP:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)

            cam_frame = ttk.Frame(connection_frame)
            cam_frame.grid(row=conn_row, column=1, sticky="we", padx=(0, 12), pady=4)

            ip_entry = tk.Entry(cam_frame, width=16)
            ip_entry.insert(0, self.ips[i])
            ip_entry.pack(side="left")
            ip_entries.append(ip_entry)

            hq_var = tk.BooleanVar(value=self.hq_enabled[i])
            ttk.Checkbutton(cam_frame, text="HQ", variable=hq_var).pack(side="left", padx=(8, 0))
            hq_checkboxes.append(hq_var)

            audio_var = tk.BooleanVar(value=self.audio_enabled[i])
            ttk.Checkbutton(cam_frame, text="Audio", variable=audio_var).pack(side="left", padx=(8, 0))
            audio_checkboxes.append(audio_var)

            ptz_var = tk.BooleanVar(value=self.ptz_supported[i])
            ttk.Checkbutton(cam_frame, text="PTZ", variable=ptz_var).pack(side="left", padx=(8, 0))
            ptz_checkboxes.append(ptz_var)

            conn_row += 1

        tk.Label(connection_frame, text="PTZ Sensitivity:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        ptz_resolution_var = tk.IntVar(value=self.ptz_resolution)
        ttk.Combobox(
            connection_frame, textvariable=ptz_resolution_var, values=[1, 2, 3, 4, 5], state="readonly", width=6
        ).grid(row=conn_row, column=1, sticky="w", padx=(0, 12), pady=4)
        conn_row += 1

        # --- General Tab ---
        row = 0

        row = add_section_header(core_frame, "Appearance", row)

        tk.Label(core_frame, text="Font:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        font_var = tk.StringVar(value=self.ui_font)
        ttk.Combobox(
            core_frame, textvariable=font_var, values=self.font_choice_labels, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(core_frame, text="Player Controls:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        controls_position_var = tk.StringVar(value=self.controls_position)
        ttk.Combobox(
            core_frame, textvariable=controls_position_var,
            values=self.CONTROL_POSITIONS, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        fullscreen_buttons_var = tk.BooleanVar(value=self.enable_fullscreen_buttons)
        fullscreen_buttons_cb = ttk.Checkbutton(
            core_frame, text="Show Stream Buttons", variable=fullscreen_buttons_var
        )
        fullscreen_buttons_cb.grid(row=row, column=0, **SPAN)
        if sys.platform.startswith('win'):
            fullscreen_buttons_var.set(True)
            fullscreen_buttons_cb.configure(state="disabled")
            tk.Label(
                core_frame, text="Required on Windows",
                font=self.app_font(9), fg="#888888"
            ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        save_window_size_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(core_frame, text="Save Window Size", variable=save_window_size_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        row = add_section_header(core_frame, "Playback & Display", row)

        tk.Label(core_frame, text="Playback Speed:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        playback_speed_var = tk.DoubleVar(value=self.default_playback_speed)
        ttk.Combobox(
            core_frame, textvariable=playback_speed_var, values=self.speed_cycle, state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        resume_playback_var = tk.BooleanVar(value=self.resume_playback)
        ttk.Checkbutton(
            core_frame, text="Resume Archive Clips From Last Position", variable=resume_playback_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        exclusive_audio_var = tk.BooleanVar(value=self.exclusive_archive_audio)
        ttk.Checkbutton(
            core_frame, text="Exclusive Archive Audio (unmuting one clip mutes others)",
            variable=exclusive_audio_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        tk.Label(core_frame, text="Sleep Mode (min):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        sleep_mode_entry = tk.Entry(core_frame, width=10)
        sleep_mode_entry.insert(0, str(self.sleep_mode_minutes))
        sleep_mode_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        sleep_hidden_var = tk.BooleanVar(value=self.sleep_hidden_streams)
        ttk.Checkbutton(
            core_frame, text="Also sleep background cams while one is maximised",
            variable=sleep_hidden_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        tk.Label(
            core_frame,
            text="Stops live streams after this long unfocused or minimized. 0 = disabled\n"
                 "Ticked: also stops cams in the background while one is maximised.",
            font=self.app_font(9), fg="#888888", justify="left"
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(0, 4))
        row += 1

        # --- Events Tab ---
        row = 0

        row = add_section_header(events_frame, "Events", row)

        motion_events_var = tk.BooleanVar(value=self.motion_triggered_events)
        ttk.Checkbutton(events_frame, text="Motion Triggered Events", variable=motion_events_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(events_frame, text="Event Overlap Window:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        event_overlap_var = tk.IntVar(value=self.event_overlap_window_mins)
        overlap_combo = ttk.Combobox(
            events_frame, textvariable=event_overlap_var, values=[1, 2, 3, 5], state="readonly", width=6
        )
        overlap_combo.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        tk.Label(events_frame, text="min", font=self.app_font(10)).grid(
            row=row, column=1, sticky="w", padx=(62, 0), pady=4
        )
        row += 1

        def _update_overlap_state(*_):
            overlap_combo.config(state="readonly" if motion_events_var.get() else "disabled")
        motion_events_var.trace_add("write", _update_overlap_state)
        _update_overlap_state()

        show_download_var = tk.BooleanVar(value=self.events_show_download)
        ttk.Checkbutton(
            events_frame, text="Show Download Button in Event List", variable=show_download_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        show_delete_var = tk.BooleanVar(value=self.events_show_delete)
        ttk.Checkbutton(
            events_frame, text="Show Delete Button in Event List", variable=show_delete_var
        ).grid(row=row, column=0, **SPAN)
        row += 1
        # Read back by save_streams via the dialog object.
        dialog.events_show_download_var = show_download_var
        dialog.events_show_delete_var = show_delete_var

        tk.Label(events_frame, text="Partial Watch Minimum:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        partial_row = tk.Frame(events_frame)
        partial_row.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        partial_min_entry = tk.Entry(partial_row, width=10)
        partial_min_entry.insert(0, f"{self.event_partial_min_watch_s:g}")
        partial_min_entry.pack(side="left")
        tk.Label(partial_row, text="sec", font=self.app_font(10)).pack(side="left", padx=(6, 0))
        tk.Label(
            events_frame,
            text="Watch an event at least this long before exiting or skipping a clip\n"
                 "marks it as partially watched (dimmed eye). 0 = any exit counts",
            font=self.app_font(9), fg="#888888", justify="left"
        ).grid(row=row + 1, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(0, 4))
        row += 2
        dialog.event_partial_min_entry = partial_min_entry

        tk.Label(events_frame, text="Default Event Filter:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        default_filter_selected = list(self.default_event_filter)  # selected canonical ids

        def _default_filter_summary():
            if not default_filter_selected:
                return self.ALL_TYPES_LABEL
            return ", ".join(self.detection_type_label(t) for t in default_filter_selected)

        default_filter_button = tk.Button(
            events_frame, text=_default_filter_summary(), font=self.app_font(10),
            anchor="w", relief="flat", bd=1, padx=8,
            bg=self.active_theme_colors.get("field_bg", "#333333"),
            fg=self.active_theme_colors.get("fg", "#ffffff"),
            activebackground=self.active_theme_colors.get("field_bg", "#333333"),
        )
        default_filter_button.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)

        def _open_default_filter_popup():
            popup = tk.Toplevel(dialog)
            popup.transient(dialog)
            popup.resizable(False, False)
            popup.title("Default Event Filter")

            check_vars = {
                cid: tk.BooleanVar(value=cid in default_filter_selected)
                for cid in self.DETECTION_TYPE_LABELS
            }

            tk.Label(popup, text="Show these types by default:", font=self.app_font(9)).pack(
                anchor="w", padx=10, pady=(10, 4)
            )
            for cid in self.DETECTION_TYPE_LABELS:
                ttk.Checkbutton(
                    popup, text=self.detection_type_label(cid), variable=check_vars[cid]
                ).pack(anchor="w", padx=16, pady=1)

            def _apply():
                default_filter_selected[:] = [cid for cid, v in check_vars.items() if v.get()]
                default_filter_button.configure(text=_default_filter_summary())
                popup.destroy()

            btn_row = tk.Frame(popup)
            btn_row.pack(pady=(8, 10))
            tk.Button(btn_row, text="OK", width=8, font=self.app_font(9), command=_apply).pack(
                side="left", padx=4
            )
            tk.Button(btn_row, text="Cancel", width=8, font=self.app_font(9), command=popup.destroy).pack(
                side="left", padx=4
            )

            popup.update_idletasks()
            popup.grab_set()

        default_filter_button.configure(command=_open_default_filter_popup)
        row += 1


        def _clear_events_cache():
            events_dir = self._events_dir()
            removed = 0
            errors = 0
            if os.path.isdir(events_dir):
                for root_dir, dirs, files in os.walk(events_dir):
                    for fname in files:
                        if fname.endswith(".json"):
                            try:
                                os.remove(os.path.join(root_dir, fname))
                                removed += 1
                            except Exception as e:
                                logging.warning(f"Could not remove events cache file: {e}")
                                errors += 1
            if errors:
                messagebox.showwarning(
                    "Events Cache",
                    f"Removed {removed} file(s), {errors} could not be deleted.",
                    parent=dialog
                )
            else:
                messagebox.showinfo(
                    "Events Cache",
                    f"Cleared {removed} cached event file(s)." if removed else "No cached event files found.",
                    parent=dialog
                )

        cache_row = tk.Frame(events_frame)
        cache_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=4)
        tk.Button(
            cache_row, text="Clear Events Cache", font=self.app_font(10),
            command=_clear_events_cache
        ).pack(side="left")
        tk.Label(
            cache_row, text="Forces a fresh scan next time Events are opened",
            font=self.app_font(9), fg="#888888"
        ).pack(side="left", padx=(10, 0))
        row += 1

        # --- Advanced Tab ---
        row = 0

        row = add_section_header(advanced_frame, "Stream Reliability", row)

        tk.Label(advanced_frame, text="Quality Downgrade:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        downgrade_frame = ttk.Frame(advanced_frame)
        downgrade_frame.grid(row=row, column=1, sticky="we", padx=(0, 12), pady=4)
        downgrade_var = tk.IntVar(value=self.downgrade_sensitivity)
        downgrade_text = tk.Label(downgrade_frame, width=9, anchor="w", font=self.app_font(10))

        def _downgrade_changed(value):
            downgrade_text.configure(text=self.DOWNGRADE_LEVEL_LABELS[int(float(value))])

        _theme = self.active_theme_colors
        downgrade_scale = tk.Scale(
            downgrade_frame, from_=0, to=5, resolution=1, orient="horizontal",
            showvalue=False, length=150, variable=downgrade_var, command=_downgrade_changed,
            bd=0, highlightthickness=0, sliderrelief="raised",
            bg=_theme.get("bg", "#f0f0f0"), troughcolor=_theme.get("bg_alt", "#e2e2e2"),
            activebackground=_theme.get("border", "#bbbbbb")
        )
        downgrade_scale.pack(side="left")
        downgrade_text.pack(side="left", padx=(8, 0))
        _downgrade_changed(self.downgrade_sensitivity)
        row += 1

        tk.Label(
            advanced_frame,
            text="Moves a struggling stream to low quality for the session (never saved).\n"
                 "Left = off, right = most sensitive. A manual config save restores HQ.",
            font=self.app_font(9), fg="#888888", justify="left"
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(0, 4))
        row += 1

        tk.Label(advanced_frame, text="Stream Timeout (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        no_frame_timeout_entry = tk.Entry(advanced_frame, width=10)
        no_frame_timeout_entry.insert(0, str(self.no_frame_timeout))
        no_frame_timeout_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        enable_retries_var = tk.BooleanVar(value=self.enable_retries)
        ttk.Checkbutton(advanced_frame, text="Enable Automatic Retries", variable=enable_retries_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Max Retry Attempts:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        max_retry_attempts_entry = tk.Entry(advanced_frame, width=10)
        max_retry_attempts_entry.insert(0, str(self.max_retry_attempts))
        max_retry_attempts_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Initial Backoff Delay (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        initial_backoff_delay_entry = tk.Entry(advanced_frame, width=10)
        initial_backoff_delay_entry.insert(0, str(self.initial_backoff_delay))
        initial_backoff_delay_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(advanced_frame, "VLC Options", row)

        vlc_params = tk.Text(advanced_frame, width=45, height=3)
        vlc_params.insert("1.0", ' '.join(self.vlcparams or self.DEFAULT_VLC_PARAMS))
        vlc_params.grid(row=row, column=0, columnspan=2, sticky="we", padx=(12, 12), pady=4)
        row += 1

        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=6
        )
        row += 1

        debug_var = tk.BooleanVar(value=self.config_debug)
        ttk.Checkbutton(advanced_frame, text="Enable Debug Logging", variable=debug_var).grid(
            row=row, column=0, **SPAN
        )

        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=10)

        tk.Button(
            button_frame, text="Save", width=10, font=self.app_font(10),
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, ptz_checkboxes,
                fullscreen_buttons_var, debug_var, archive_entry, vlc_params,
                ptz_resolution_var, save_window_size_var, dialog,
                enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry,
                downgrade_var,
                playback_speed_var, font_var, no_frame_timeout_entry, resume_playback_var,
                motion_events_var, event_overlap_var, exclusive_audio_var, default_filter_selected,
                controls_position_var, sleep_mode_entry, sleep_hidden_var
            )
        ).pack(side="left", padx=5)

        tk.Button(
            button_frame, text="Cancel", width=10, font=self.app_font(10),
            command=dialog.destroy
        ).pack(side="left", padx=5)

        dialog.update_idletasks()

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, ptz_checkboxes, fullscreen_buttons_var, debug_var, archive_entry, vlc_params, ptz_resolution_var, save_window_size_var, dialog, enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry, downgrade_var, playback_speed_var, font_var=None, no_frame_timeout_entry=None, resume_playback_var=None, motion_events_var=None, event_overlap_var=None, exclusive_audio_var=None, default_filter_selected=None, controls_position_var=None, sleep_mode_entry=None, sleep_hidden_var=None):
        # Snapshot which streams were live so saving only restarts those.
        was_playing = [
            bool(self.media_players[i]) and not self.is_archive_mode[i]
            for i in range(4)
        ]
        self.username = username_entry.get().strip()
        self.password = password_entry.get().strip()
        self.archive_dir = archive_entry.get().strip()
        self.ips = [e.get().strip() for e in ip_entries]
        self.hq_enabled = [v.get() for v in hq_checkboxes]
        self.session_lq = [False] * 4
        self.hq_init_failures = [0] * 4
        self.audio_enabled = [v.get() for v in audio_checkboxes]
        self.ptz_supported = [v.get() for v in ptz_checkboxes]
        self.enable_fullscreen_buttons = fullscreen_buttons_var.get()
        self.config_debug = debug_var.get()
        if font_var is not None:
            chosen = font_var.get()
            self.ui_font = chosen if chosen in self.font_choice_labels else self.font_choice_labels[0]
        if resume_playback_var is not None:
            self.resume_playback = resume_playback_var.get()
        if motion_events_var is not None:
            self.motion_triggered_events = motion_events_var.get()
        if event_overlap_var is not None:
            v = int(event_overlap_var.get())
            self.event_overlap_window_mins = v if v in (1, 2, 3, 5) else 1
        if exclusive_audio_var is not None:
            self.exclusive_archive_audio = exclusive_audio_var.get()
        if controls_position_var is not None:
            v = controls_position_var.get()
            self.controls_position = v if v in self.CONTROL_POSITIONS else "top-left"
        if sleep_mode_entry is not None:
            try:
                self.sleep_mode_minutes = int(sleep_mode_entry.get().strip())
                if self.sleep_mode_minutes < 0:
                    logging.warning(f"Invalid sleep_mode_minutes: {self.sleep_mode_minutes}, using default 0")
                    self.sleep_mode_minutes = 0
            except ValueError:
                logging.warning(f"Invalid sleep_mode_minutes input, using default 0")
                self.sleep_mode_minutes = 0
        _dl_var = getattr(dialog, "events_show_download_var", None)
        if _dl_var is not None:
            self.events_show_download = bool(_dl_var.get())
        _del_var = getattr(dialog, "events_show_delete_var", None)
        if _del_var is not None:
            self.events_show_delete = bool(_del_var.get())
        _pm_entry = getattr(dialog, "event_partial_min_entry", None)
        if _pm_entry is not None:
            try:
                self.event_partial_min_watch_s = float(_pm_entry.get().strip())
                if self.event_partial_min_watch_s < 0:
                    logging.warning(f"Invalid event_partial_min_watch_s: {self.event_partial_min_watch_s}, using default 5.0")
                    self.event_partial_min_watch_s = 5.0
            except ValueError:
                logging.warning("Invalid event_partial_min_watch_s input, using default 5.0")
                self.event_partial_min_watch_s = 5.0
        if default_filter_selected is not None:
            self.default_event_filter = list(default_filter_selected)
        if sleep_hidden_var is not None:
            self.sleep_hidden_streams = sleep_hidden_var.get()
    
        try:
            self.default_playback_speed = float(playback_speed_var.get())
            if self.default_playback_speed not in self.speed_cycle:
                logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                self.default_playback_speed = 1.0
        except (ValueError, TypeError):
            logging.warning(f"Invalid default_playback_speed input, using default 1.0")
            self.default_playback_speed = 1.0
        # Config dialog is modal, so no clips are cycling; safe to reset the live speed.
        self.global_playback_speed = self.default_playback_speed

        self.enable_retries = enable_retries_var.get()
        try:
            self.max_retry_attempts = int(max_retry_attempts_entry.get().strip())
            if self.max_retry_attempts < 1:
                logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 5")
                self.max_retry_attempts = 5
        except ValueError:
            logging.warning(f"Invalid max_retry_attempts input, using default 5")
            self.max_retry_attempts = 5
        try:
            self.initial_backoff_delay = float(initial_backoff_delay_entry.get().strip())
            if self.initial_backoff_delay <= 0:
                logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 2.0")
                self.initial_backoff_delay = 2.0
        except ValueError:
            logging.warning(f"Invalid initial_backoff_delay input, using default 2.0")
            self.initial_backoff_delay = 2.0
        try:
            self.downgrade_sensitivity = max(0, min(5, int(downgrade_var.get())))
        except (ValueError, TypeError, tk.TclError):
            self.downgrade_sensitivity = 3
        try:
            self.no_frame_timeout = float(no_frame_timeout_entry.get().strip())
            if self.no_frame_timeout < 5:
                logging.warning(f"Invalid no_frame_timeout: {self.no_frame_timeout}, using default 15.0")
                self.no_frame_timeout = 15.0
        except ValueError:
            logging.warning(f"Invalid no_frame_timeout input, using default 15.0")
            self.no_frame_timeout = 15.0

        try:
            ptz_resolution = ptz_resolution_var.get()
            if not isinstance(ptz_resolution, int) or ptz_resolution < 1 or ptz_resolution > 5:
                logging.warning(f"Invalid PTZ resolution: {ptz_resolution}, using default 3")
                ptz_resolution = 3
            self.ptz_resolution = ptz_resolution
        except Exception as e:
            logging.error(f"Failed to parse PTZ resolution: {e}, using default 3")
            self.ptz_resolution = 3

        raw_params = vlc_params.get("1.0", "end-1c").strip()
        self.vlcparams = self.parse_vlcparams(raw_params)

        if save_window_size_var.get():
            if self.root.attributes("-fullscreen"):
                self.saved_window_size = "fullscreen"
            else:
                width = self.root.winfo_width()
                height = self.root.winfo_height()
                if width >= self.MIN_WIDTH and height >= self.MIN_HEIGHT:
                    self.saved_window_size = f"{width}x{height}"
                else:
                    logging.warning(f"Current window size {width}x{height} is below minsize {self.MIN_WIDTH}x{self.MIN_HEIGHT}, saving default")
                    self.saved_window_size = "1340x720"
         
        if self.debug_mode != self.config_debug:
            self.debug_mode = self.config_debug
            self._setup_logging(self.debug_mode)

        self.onvif_cams = {}
        self.onvif_fail_until = {}
        self.update_streams()
        self.save_config()
        self.apply_theme()

        self.update_label_bindings()
        self.build_config_panel()
        self._rearm_sleep_mode_timer()
        self._rearm_bg_sleep()

        dialog.destroy()
        threading.Thread(target=self.restart_previously_playing_streams, args=(was_playing,), daemon=True).start()

    def check_network_connectivity(self, ip_input):
        """Return True if the camera (IP, IP:port or IP:port/path) accepts a TCP connection on its RTSP port."""
        try:
            if ':' in ip_input:
                parts = ip_input.split(':', 1)
                host = parts[0].strip()
                port_str = parts[1].strip()

                if '/' in port_str:
                    port_str = port_str.split('/', 1)[0].strip()

                try:
                    port = int(port_str)
                    if not (1 <= port <= 65535):
                        port = 554  # invalid port -> fallback
                        logging.warning(f"Invalid port value '{port_str}' for {host} → using default 554")
                except ValueError:
                    port = 554
                    logging.warning(f"Non-numeric port '{port_str}' for {host} → using default 554")
            else:
                host = ip_input.strip()
                port = 554

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex((host, port))
            sock.close()

            if result == 0:
                return True
            else:
                logging.warning(f"Network check: Camera at {host}:{port} is not reachable (error code: {result})")
                return False

        except Exception as e:
            logging.warning(f"Network check failed for '{ip_input}': {e}")
            return False

    def debounce_layout_update(self):
        """Debounce layout updates."""
        if hasattr(self, '_layout_debounce_id'):
            self.root.after_cancel(self._layout_debounce_id)
        self._layout_debounce_id = self.root.after(100, self.update_layout)

    def _effective_hq(self, index):
        """HQ as streamed: configured HQ unless downgraded this session."""
        return bool(self.hq_enabled[index]) and not self.session_lq[index]

    def _build_stream_url(self, ip, hq):
        # Percent-encode credentials so characters like @ : / # % can't corrupt the URL.
        user = quote(self.username, safe="")
        pw = quote(self.password, safe="")
        return f"rtsp://{user}:{pw}@{ip}/stream{'2' if not hq else '1'}"

    @staticmethod
    def _redact_url(url):
        return re.sub(r"(//)[^@/]*@", r"\1***@", url or "")

    def _apply_live_audio(self, index):
        """Set mute for a freshly initialised live stream (unmuted only if fullscreen).
        Bypasses set_audio_state(), which refuses while a stream is initializing."""
        if not self.audio_enabled[index]:
            return
        player = self.media_players[index]
        if not player:
            return
        want_audio = (self.is_fullscreen and self.fullscreen_index == index
                      and not self.is_archive_mode[index])
        try:
            with self._vlc_audio_lock:
                player.audio_set_mute(not want_audio)
        except Exception as e:
            logging.error(f"Stream {index}: Failed to apply live audio state: {e}")

    def _stop_removed_stream(self, index):
        """Stop a stream that lost its IP/URL in a config save."""
        self._request_init_abort(index)
        with self.archive_entry_locks[index]:
            self._wait_init_idle(index)
            self.cleanup_stream(index)
            self.update_stream_label(index, "Disabled")
        if self.fullscreen_buttons[index]:
            self.root.after(0, lambda idx=index: self.fullscreen_buttons[idx].place_forget())

    def update_stream(self, index: int) -> None:
        if not 0 <= index <= 3:
            return
        
        if len(self.streams) < 4:
            self.streams.extend([""] * (4 - len(self.streams)))
        
        ip = self.ips[index]
        hq = self._effective_hq(index)
        
        if ip and self.username and self.password:
            stream = self._build_stream_url(ip, hq)
            # Drop URLs already used by another stream
            seen_urls = {s for i, s in enumerate(self.streams) if s and i != index}
            if stream in seen_urls:
                stream = ""
        else:
            stream = ""
        
        self.streams[index] = stream
        logging.info(f"Updated stream at index {index}: {self._redact_url(stream)}")

    def update_streams(self):
        self.streams = []
        seen_urls = set()
        for i, ip in enumerate(self.ips):
            hq = self._effective_hq(i)
            if ip and self.username and self.password:
                stream = self._build_stream_url(ip, hq)
                if stream in seen_urls:
                    stream = ""
                else:
                    seen_urls.add(stream)
            else:
                stream = ""
            self.streams.append(stream)
        logging.info(f"Updated streams: {[self._redact_url(s) for s in self.streams]}")

    def create_icon(self, icon_type, opacity=1.0):
        size = (40, 40) if icon_type in ["config", "back", "left", "right", "up", "down", "fullscreen", "minimize", "play"] else (100, 100) if icon_type in ["folder", "archive", "back"] else (40, 40)
        if icon_type == "eye":
          img = Image.new("RGBA", size, (0, 0, 0, 0))
        else:
          img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        
        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        if icon_type == "config":
            import math
            cx, cy = 20, 20
            for k in range(8):
                a = math.radians(k * 45)
                dx, dy = math.cos(a), math.sin(a)
                px, py = -dy, dx
                r_in, r_out, hw_in, hw_out = 9, 15, 3.0, 2.0
                draw.polygon([
                    (cx + dx * r_in + px * hw_in,   cy + dy * r_in + py * hw_in),
                    (cx + dx * r_out + px * hw_out, cy + dy * r_out + py * hw_out),
                    (cx + dx * r_out - px * hw_out, cy + dy * r_out - py * hw_out),
                    (cx + dx * r_in - px * hw_in,   cy + dy * r_in - py * hw_in),
                ], fill="white")
            draw.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill="white")
            draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=(0, 0, 0, 255))
        elif icon_type == "fullscreen":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((10, 10, 13, 10), fill="white", width=2)
            draw.line((10, 10, 10, 13), fill="white", width=2)
            draw.line((30, 30, 27, 30), fill="white", width=2)
            draw.line((30, 30, 30, 27), fill="white", width=2)
        elif icon_type == "minimize":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((8, 20, 32, 20), fill="white", width=2)
            draw.line((20, 8, 20, 32), fill="white", width=2)
        elif icon_type == "pause":
            draw.rectangle((12, 8, 18, 32), fill="white")
            draw.rectangle((22, 8, 28, 32), fill="white")
        elif icon_type == "speed":
            # Double-chevron: fast-forward, mirrored for rewind.
            draw.polygon([(10, 8), (20, 20), (10, 32)], fill="white")
            draw.polygon([(20, 8), (30, 20), (20, 32)], fill="white")
        elif icon_type == "replay":
            draw.arc((10, 10, 30, 30), start=45, end=315, fill="white", width=3)
            draw.polygon([(28, 12), (32, 16), (28, 20)], fill="white")
        elif icon_type == "rewind":
            draw.polygon([(30, 8), (20, 20), (30, 32)], fill="white")
            draw.polygon([(20, 8), (10, 20), (20, 32)], fill="white")
        elif icon_type == "exit":
            draw.line((12, 12, 28, 28), fill="white", width=3)
            draw.line((12, 28, 28, 12), fill="white", width=3)
        elif icon_type == "folder":
            draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.line((20, 30, 40, 50), fill=adjust_color("black", opacity), width=2)
        elif icon_type == "archive":
            draw.rectangle((20, 20, 80, 80), outline=adjust_color("white", opacity), width=2)
            draw.polygon([(35, 25), (35, 75), (75, 50)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
        elif icon_type == "play":
            draw.polygon([(12, 8), (32, 20), (12, 32)], outline="white", width=2, fill="white")
        elif icon_type == "disk":
            draw.rectangle([(8, 8), (32, 32)], outline="white", width=2, fill="black")
            draw.rectangle([(12, 10), (28, 18)], outline="white", width=1, fill="white")
            draw.rectangle([(16, 24), (24, 30)], outline="white", width=1, fill="white")      
        elif icon_type == "back":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "events":
            draw.rectangle([(9, 11), (31, 31)], outline="white", width=2)
            draw.line([(9, 17), (31, 17)], fill="white", width=2)
            draw.line([(14, 9), (14, 14)], fill="white", width=2)
            draw.line([(26, 9), (26, 14)], fill="white", width=2)
            draw.polygon([(22, 19), (18, 25), (21, 25), (18, 31), (24, 23), (21, 23)], fill="white")
        elif icon_type == "list":
            for row_y in (11, 20, 29):
                draw.ellipse([(8, row_y - 2), (12, row_y + 2)], fill="white")
                draw.line([(17, row_y), (32, row_y)], fill="white", width=3)
        elif icon_type == "delete":
            draw.rectangle([(13, 14), (27, 31)], outline="white", width=2)
            draw.line([(10, 14), (30, 14)], fill="white", width=2)
            draw.line([(17, 11), (23, 11)], fill="white", width=2)
            draw.line([(17, 18), (17, 28)], fill="white", width=1)
            draw.line([(20, 18), (20, 28)], fill="white", width=1)
            draw.line([(23, 18), (23, 28)], fill="white", width=1)
        elif icon_type == "download":
            draw.line([(20, 8), (20, 24)], fill="white", width=3)
            draw.polygon([(12, 18), (28, 18), (20, 28)], fill="white")
            draw.line([(9, 32), (31, 32)], fill="white", width=3)
        elif icon_type == "audio_on":
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.arc([(23, 13), (31, 27)], start=300, end=60, fill="white", width=2)
            draw.arc([(25, 10), (35, 30)], start=300, end=60, fill="white", width=2)
        elif icon_type == "audio_off":
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.line([(25, 14), (33, 26)], fill="white", width=2)
            draw.line([(33, 14), (25, 26)], fill="white", width=2)
        elif icon_type == "eye":
            # Open eye: the "watched" marker in the event listing.
            draw.ellipse((6, 15, 34, 25), outline="white", width=2)
            draw.ellipse((16, 16, 24, 24), fill="white")
        elif icon_type == "left":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "right":
            draw.polygon([(10, 10), (25, 20), (10, 30)], fill="white")
        elif icon_type == "up":
            draw.polygon([(10, 30), (20, 15), (30, 30)], fill="white")
        elif icon_type == "down":
            draw.polygon([(10, 10), (20, 25), (30, 10)], fill="white")
        return ImageTk.PhotoImage(img)

    ACTIVE_MODE_COLOR = "#e62117"

    def recolor_icon_active(self, photo_image):
        """Return a red-tinted copy of a white-on-black icon (marks an active mode button)."""
        img = ImageTk.getimage(photo_image).convert("RGBA")
        r, g, b = tuple(int(self.ACTIVE_MODE_COLOR[i:i + 2], 16) for i in (1, 3, 5))
        pixels = img.load()
        for y in range(img.height):
            for x in range(img.width):
                pr, pg, pb, pa = pixels[x, y]
                if pr > 200 and pg > 200 and pb > 200:
                    pixels[x, y] = (r, g, b, pa)
        return ImageTk.PhotoImage(img)

    def dim_icon(self, photo_image, opacity=0.55):
        """Return a faded copy of an icon (alpha only), for disabled mode buttons."""
        img = ImageTk.getimage(photo_image).convert("RGBA")
        pixels = img.load()
        for y in range(img.height):
            for x in range(img.width):
                pr, pg, pb, pa = pixels[x, y]
                pixels[x, y] = (pr, pg, pb, int(pa * opacity))
        return ImageTk.PhotoImage(img)

    def get_day_folder_icon(self, day_abbrev, is_clicked):
        cache_key = (day_abbrev, is_clicked)
        cached = self.day_folder_icon_cache.get(cache_key)
        if cached is not None:
            return cached

        opacity = 0.6 if is_clicked else 1.0
        size = (100, 100)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity),
                        outline=adjust_color("white", opacity), width=3)
        draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)],
                      fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)

        font = None
        for font_path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "arialbd.ttf",
        ):
            try:
                font = ImageFont.truetype(font_path, 24)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        text = day_abbrev
        text_color = adjust_color("black", opacity)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except Exception:
            text_w, text_h = draw.textsize(text, font=font)

        text_x = 20 + (60 - text_w) // 2
        text_y = 30 + (50 - text_h) // 2 - 2
        draw.text((text_x, text_y), text, fill=text_color, font=font)

        photo = ImageTk.PhotoImage(img)
        self.day_folder_icon_cache[cache_key] = photo
        return photo

    def get_speed_icon(self, multiplier):
        """Return a cached play icon with an 'Nx' speed badge (shown even at 1x)."""
        cached = self.speed_icon_cache.get(multiplier)
        if cached is not None:
            return cached

        size = (40, 40)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        draw.polygon([(12, 8), (32, 20), (12, 32)], outline="white", width=2, fill="white")

        if multiplier:
            label = f"{int(multiplier)}x" if float(multiplier).is_integer() else f"{multiplier}x"

            font = None
            for font_path in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "DejaVuSans-Bold.ttf",
                "arialbd.ttf",
            ):
                try:
                    font = ImageFont.truetype(font_path, 11)
                    break
                except Exception:
                    continue
            if font is None:
                font = ImageFont.load_default()

            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except Exception:
                text_w, text_h = draw.textsize(label, font=font)

            # Badge at bottom-right with a dark backing for legibility.
            pad_x, pad_y = 2, 1
            badge_x1 = 40 - text_w - pad_x * 2 - 1
            badge_y1 = 40 - text_h - pad_y * 2 - 3
            badge_x2 = 39
            badge_y2 = 39
            draw.rectangle((badge_x1, badge_y1, badge_x2, badge_y2), fill=(0, 0, 0, 255))
            draw.text((badge_x1 + pad_x, badge_y1 + pad_y - bbox[1]), label, fill="#e62117", font=font)

        photo = ImageTk.PhotoImage(img)
        self.speed_icon_cache[multiplier] = photo
        return photo

    def bind_stream_label(self, index):
        try:
            self.labels[index].unbind("<Button-1>")

            if self.labels[index].cget("text") == "Stream Failed, click to reconnect":
                self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
            else:
                if self.event_mode:
                    return
                if not self.enable_fullscreen_buttons and self.streams[index]:
                    self.labels[index].bind("<Button-1>", lambda event, idx=index: self.handle_stream_click(idx))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind stream label: {e}")

    def update_label_bindings(self):
        for i in range(4):
            self.bind_stream_label(i)

    def _apply_fullscreen_audio(self):
        """Unmute the fullscreen live stream (if audio-enabled) and mute the other live streams."""
        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if self.is_archive_mode[i]:
                continue
            self.set_audio_state(i, mute=(i != self.fullscreen_index))

    def enter_fullscreen(self):
        if self.is_fullscreen:
            return
        for i in range(4):
            if self.streams[i]:
                self.is_fullscreen = True
                self.fullscreen_index = i
                self._apply_fullscreen_audio()
                self.build_config_panel()
                logging.info(f"Entered fullscreen mode for stream {i}")
                break

    def _return_to_event_listing(self):
        """Force-finish playing event clips and return to the listing (or exit to live if none are playing).
        Shared by right-click/Down and the back-to-list button."""
        playing = self.event_active_cams - self.event_done_cams
        if playing:
            # Cancel pending delayed clip launches so nothing starts after cleanup.
            for _pending in self._pending_event_afters:
                try:
                    self.root.after_cancel(_pending["after_id"])
                except Exception:
                    pass
            self._pending_event_afters.clear()
            # Teardown + widget destruction run in one background thread under archive_entry_locks[i]:
            # destroying vlc_frame before libvlc releases it raises BadWindow, and concurrent teardowns double-release.
            for i in list(playing):
                self.event_clip_queues[i] = []  # no next clip
                self._play_tokens[i] += 1
                self.event_done_cams.add(i)
                self.is_archive_mode[i] = False
                # Mark teardown pending so update_layout() doesn't rebind the still-live clip player as live.
                self.pending_vlc_teardown.add(i)
                # Safe to blank now: only touches the label, not the vlc_frame child VLC renders into.
                self._set_event_blank_label(i)

            def _teardown_players(idxs):
                for i in idxs:
                    with self.archive_entry_locks[i]:
                        try:
                            self.cleanup_stream(i)
                        finally:
                            self._clear_pending_teardown(i)
                        def _finish_ui(idx=i):
                            for widget in self.labels[idx].winfo_children():
                                widget.destroy()
                            self._reset_clip_buttons(idx)
                        self._tk_wait(_finish_ui)
            threading.Thread(target=_teardown_players, args=(list(playing),), daemon=True).start()

            # Exited early: persist a "partially watched" mark (no-op if fully watched).
            self._mark_event_partially_watched(self.current_playing_event)

            # A single-cam event entered fullscreen: drop back to grid so the overlay centres over all panels.
            if getattr(self, '_event_entered_fullscreen', False):
                self.is_fullscreen = False
                self.fullscreen_index = -1
                self._event_entered_fullscreen = False
                self.update_layout()
                self.build_config_panel()
            else:
                # Refresh so the back-to-listing button (shown only during clip playback) is hidden.
                self.build_config_panel()

            if self.event_overlay and self.event_overlay.winfo_exists():
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()
        else:
            # Overlay already showing, no clips running: exit to live
            self._exit_event_mode()

    def exit_fullscreen(self, event=None):

        if self.event_mode:
            self._return_to_event_listing()
            return
        if self.is_fullscreen:
            idx = self.fullscreen_index

            if idx is not None and idx >= 0 and self.is_archive_mode[idx]:
                self.go_back(idx)
                return

            self.is_fullscreen = False
            self.fullscreen_index = -1

            for i in range(4):
                if self.audio_enabled[i]:
                    self.set_audio_state(i, mute=True)

            self.build_config_panel()
        else:
            any_archive_mode = False
            for i in range(4):
                if self.is_archive_mode[i]:
                    any_archive_mode = True
                    self.toggle_archive_mode(i)

            if any_archive_mode:
                self.build_config_panel()

    def build_config_panel(self):
        # All fullscreen/mode/selection changes land here, so hidden-stream sleep re-evaluates here.
        self._schedule_bg_sleep_reconcile()
        try:
            if not self.config_panel:
                self.config_panel = tk.Frame(self.root, bg="#222222", width=60)

            if not self.ptz_buttons:
                self.ptz_buttons = []
                self.ptz_images = []
                for direction in ["up", "down", "left", "right"]:
                    img = self.icon_cache[direction]
                    button = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda d=direction: self.start_ptz_move(d)
                    )
                    self.ptz_buttons.append(button)
                    self.ptz_images.append(img)

            if not self.exit_fullscreen_button:
                self.exit_fullscreen_image = self.icon_cache["minimize"]
                self.exit_fullscreen_button = tk.Button(
                    self.config_panel, image=self.exit_fullscreen_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.exit_fullscreen, cursor="hand2"
                )

            if not self.config_button:
                self.config_img = self.icon_cache["config"]
                self.config_button = tk.Button(
                    self.config_panel, image=self.config_img, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.show_config_dialog, cursor="hand2"
                )

            if not self.archive_mode_button:
                self.archive_mode_image = self.icon_cache["disk"]
                self.archive_mode_button = tk.Button(
                    self.config_panel, image=self.archive_mode_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_all_archive_mode, cursor="hand2"
                )

            if not self.events_button:
                self.events_button_image = self.icon_cache["events"]
                self.events_button = tk.Button(
                    self.config_panel, image=self.events_button_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_event_mode, cursor="hand2"
                )

            if not self.event_back_button:
                self.event_back_button_image = self.icon_cache["list"]
                self.event_back_button = tk.Button(
                    self.config_panel, image=self.event_back_button_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self._return_to_event_listing, cursor="hand2"
                )

            if not self.speed_toggle_button:
                self.speed_toggle_image = self.get_speed_icon(self.global_playback_speed)
                self.speed_toggle_button = tk.Button(
                    self.config_panel, image=self.speed_toggle_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.cycle_speed, cursor="hand2"
                )

            for i in range(4):
                if not self.fullscreen_buttons[i]:
                    img = self.icon_cache["fullscreen"]
                    self.fullscreen_buttons[i] = tk.Button(
                        self.panels[i], image=img, bg="black", bd=0, cursor="hand2",
                        command=lambda idx=i: self.handle_stream_click(idx),
                        state="disabled" if not self.enable_fullscreen_buttons else "normal"
                    )

            if self.is_fullscreen and self.fullscreen_index is not None:
                if not self.archive_buttons[self.fullscreen_index]:
                    img = self.icon_cache["disk"]
                    self.archive_buttons[self.fullscreen_index] = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda idx=self.fullscreen_index: self.toggle_archive_mode(idx)
                    )

            for button in self.ptz_buttons + [self.exit_fullscreen_button, self.config_button, self.archive_mode_button] + \
                          ([self.events_button] if self.events_button else []) + \
                          ([self.event_back_button] if self.event_back_button else []) + \
                          ([self.speed_toggle_button] if self.speed_toggle_button else []) + \
                          [b for b in self.archive_buttons if b] + [b for b in self.fullscreen_buttons if b]:
                button.pack_forget()
                if button in self.fullscreen_buttons:
                    button.place_forget()

            ptz_enabled = (self.is_fullscreen and self.fullscreen_index is not None and
                           self.ptz_supported[self.fullscreen_index] and not self.is_archive_mode[self.fullscreen_index])
            for button in self.ptz_buttons:
                button.config(state="normal" if ptz_enabled else "disabled")

            if self.is_fullscreen and self.fullscreen_index is not None and not self.event_mode:
                if (self.archive_dir and self.streams[self.fullscreen_index]):
                    _fi = self.fullscreen_index
                    _locked = self._index_locked(_fi)
                    self.archive_buttons[_fi].configure(
                        image=(self.icon_cache["disk_active"] if self.is_archive_mode[_fi]
                               else self.icon_cache["disk_disabled"] if _locked
                               else self.icon_cache["disk"]),
                        state="disabled" if _locked else "normal"
                    )
                    self.archive_buttons[self.fullscreen_index].pack(pady=5, padx=10)
                # Global speed control (archive mode).
                if self.is_archive_mode[self.fullscreen_index]:
                    self.speed_toggle_button.configure(image=self.get_speed_icon(self.global_playback_speed))
                    self.speed_toggle_button.pack(pady=5, padx=10)
                if ptz_enabled:
                    for button in self.ptz_buttons:
                        button.pack(pady=5, padx=10)
                # Redundant in fullscreen archive mode (the archive button already exits to grid).
                if not self.is_archive_mode[self.fullscreen_index]:
                    self.exit_fullscreen_button.pack(pady=5, padx=10)
                # Events button in fullscreen too, so users can jump to Events without backing out.
                if self.motion_triggered_events and self.archive_dir:
                    any_initializing = self._modes_busy()
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["events_active"] if self.event_mode
                               else self.icon_cache["events_disabled"] if any_initializing
                               else self.icon_cache["events"])
                    )
                    self.events_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=5, padx=10)
            elif self.event_mode:
                # Event mode nav: Archive, Events, back-to-listing, Config. No PTZ or exit-fullscreen button.
                any_initializing = self._modes_busy()
                # is_archive_mode[i] is also True while a cam plays an event clip (the pipeline is reused),
                # so show the archive button inactive here; Event and Archive modes are mutually exclusive.
                if self.archive_dir:
                    self.archive_mode_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["disk_disabled"] if any_initializing
                               else self.icon_cache["disk"])
                    )
                    self.archive_mode_button.pack(pady=5, padx=10)
                if self.motion_triggered_events and self.archive_dir:
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=self.icon_cache["events_active"]
                    )
                    self.events_button.pack(pady=5, padx=10)
                # Only while a clip is playing; otherwise the listing overlay is already visible.
                if self.event_active_cams - self.event_done_cams:
                    self.event_back_button.pack(pady=5, padx=10)
                # Always available in event mode so speed can be set before pressing play.
                self.speed_toggle_button.configure(image=self.get_speed_icon(self.global_playback_speed))
                self.speed_toggle_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=10, padx=10)
            else:
                any_initializing = self._modes_busy()
                any_archive_mode = any(self.is_archive_mode[i] for i in range(4))
                if self.archive_dir:
                    self.archive_mode_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["disk_active"] if any_archive_mode
                               else self.icon_cache["disk_disabled"] if any_initializing
                               else self.icon_cache["disk"])
                    )
                    self.archive_mode_button.pack(pady=5, padx=10)
                # Speed control while archive mode is active (browser or playback), so it can be set
                # before opening a clip. Not gated on media_players: the browser has no player.
                if any_archive_mode:
                    self.speed_toggle_button.configure(image=self.get_speed_icon(self.global_playback_speed))
                    self.speed_toggle_button.pack(pady=5, padx=10)
                if self.motion_triggered_events and self.archive_dir:
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["events_active"] if self.event_mode
                               else self.icon_cache["events_disabled"] if any_initializing
                               else self.icon_cache["events"])
                    )
                    self.events_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=10, padx=10)
                # Never shown in event mode: single-cam events auto-enter fullscreen, multi-cam events don't support it.
                for i in range(4):
                    if (self.enable_fullscreen_buttons and self.ips[i] and not self.is_archive_mode[i]
                            and not self.event_mode):
                        self.fullscreen_buttons[i].configure(state="normal")
                        self.fullscreen_buttons[i].place(relx=1.0, rely=1.0, x=-35, y=-35, anchor="se")
                        self.fullscreen_buttons[i].lift()
                    elif self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()

        except Exception:
            pass

    
    def iterate_streams(self, direction):
        if not self.is_fullscreen:
            return
        if self.fullscreen_index is None:
            return

        enabled_streams = [i for i in range(4) if self.streams[i]]
        if not enabled_streams:
            return

        try:
            current_pos = enabled_streams.index(self.fullscreen_index)
        except ValueError:
            return

        new_pos = (current_pos + direction) % len(enabled_streams)
        new_index = enabled_streams[new_pos]
        self.fullscreen_index = new_index

        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if i == self.fullscreen_index and not self.is_archive_mode[i]:
                self.set_audio_state(i, mute=False)
            else:
                self.set_audio_state(i, mute=True)

        self.build_config_panel()
        self.debounce_layout_update()


    def init_ui(self):

        self.config_panel = tk.Frame(self.root, bg="#222222", width=60)
        self.config_panel.pack(side="right", fill="y")
        
        self.grid_frame = tk.Frame(self.root, bg="#222222")
        self.grid_frame.pack(fill="both", expand=True)

        initial_width, initial_height = 960, 540
        for i in range(4):
            panel = tk.Frame(self.grid_frame, bg="black")
            self.panels[i] = panel
            label = tk.Label(panel, bg="black", text="Disabled", fg="white")
            self.labels[i] = label
            label.pack(fill="both", expand=True)
            x = 0 if i in (0, 2) else initial_width + 5
            y = 0 if i in (0, 1) else initial_height + 5
            panel.place(x=x, y=y, width=initial_width, height=initial_height)
            self.panel_sizes[i] = (initial_width, initial_height)
          
            self.archive_buttons[i] = None  # Created in build_config_panel
            self.archive_canvas[i] = tk.Canvas(panel, bg="#222222", highlightthickness=0)
          
            self.fullscreen_buttons[i] = None

        self.update_label_bindings()

        self.apply_window_size(self.saved_window_size)

        def handle_fullscreen_toggle(event):
            is_fullscreen = not self.root.attributes("-fullscreen")
            self.root.attributes("-fullscreen", is_fullscreen)

        self.root.bind("<Alt-Return>", handle_fullscreen_toggle)
        self.root.bind("<Shift_L>", handle_fullscreen_toggle)
        self.root.bind("<Up>", lambda e: self.enter_fullscreen())
        self.root.bind("<Down>", lambda e: self.exit_fullscreen())
        self.root.bind("<Button-3>", lambda e: self.exit_fullscreen())
        self.root.bind("<Left>", lambda e: self.iterate_streams(-1))
        self.root.bind("<Right>", lambda e: self.iterate_streams(1))
        self.root.bind("<Configure>", lambda e: self.debounce_layout_update())

        # Fullscreen archive: Page Up/Down change page, Backspace goes up a folder.
        self.root.bind("<Prior>", lambda e: self.archive_change_page_shortcut(-1))   # Page Up
        self.root.bind("<Next>", lambda e: self.archive_change_page_shortcut(1))     # Page Down
        self.root.bind("<BackSpace>", lambda e: self.archive_go_back_shortcut())

        # Sleep mode: bound on root so it fires regardless of which child has focus.
        self.root.bind("<FocusIn>", self._on_app_focus_in)
        self.root.bind("<FocusOut>", self._on_app_focus_out)
        self.root.bind("<Unmap>", self._on_app_unmap)
        self.root.bind("<Map>", self._on_app_map)

        self.build_config_panel()

    # --- Sleep mode ---
    # Unfocused or minimized starts the countdown; both must clear to cancel it.

    def _on_app_focus_out(self, event=None):
        # Only the root's own focus counts, not child widgets losing focus to each other.
        if event is not None and event.widget is not self.root:
            return
        self._app_focused = False
        self._maybe_start_sleep_timer()

    def _on_app_focus_in(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_focused = True
        self._maybe_cancel_sleep_timer_and_wake()

    def _on_app_unmap(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_minimized = True
        self._maybe_start_sleep_timer()

    def _on_app_map(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_minimized = False
        self._maybe_cancel_sleep_timer_and_wake()

    def _maybe_start_sleep_timer(self):
        """Arm the sleep countdown if unfocused/minimized, enabled, and not already running."""
        if self.sleep_mode_minutes <= 0:
            return
        if self._is_asleep or self._sleep_timer_id is not None:
            return
        if self._app_focused and not self._app_minimized:
            return
        delay_ms = int(self.sleep_mode_minutes * 60 * 1000)
        logging.info(f"Sleep mode: app unfocused/minimized, sleeping in {self.sleep_mode_minutes} min if it stays that way")
        self._sleep_timer_id = self.root.after(delay_ms, self._enter_sleep_mode)

    def _maybe_cancel_sleep_timer_and_wake(self):
        """Cancel any pending countdown and wake streams if asleep (on refocus/restore)."""
        # Still unfocused or minimized; don't wake yet.
        if not self._app_focused or self._app_minimized:
            return
        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None
            logging.info("Sleep mode: app refocused before timeout, countdown cancelled")
        if self._is_asleep:
            self._wake_from_sleep_mode()

    # --- Hidden-behind-fullscreen sleep ---

    def _hidden_live_indices(self):
        """Live streams hidden behind a fullscreen stream (empty unless the feature and timeout are set)."""
        if not self.sleep_hidden_streams or self.sleep_mode_minutes <= 0:
            return set()
        if self.event_mode or not self.is_fullscreen:
            return set()
        if self.fullscreen_index is None or self.fullscreen_index < 0:
            return set()
        return {
            i for i in range(4)
            if i != self.fullscreen_index
            and not self.is_archive_mode[i]
            and self.ips[i] and self.streams[i]
        }

    def _schedule_bg_sleep_reconcile(self):
        # Coalesce many state changes in one UI turn into one reconcile.
        if self._bg_reconcile_pending:
            return
        self._bg_reconcile_pending = True
        try:
            self.root.after_idle(self._reconcile_background_sleep)
        except Exception:
            self._bg_reconcile_pending = False

    def _reconcile_background_sleep(self):
        """Sync timers/sleep state with who is hidden now. Idempotent; Tk thread."""
        self._bg_reconcile_pending = False
        if not self.running:
            return
        hidden = self._hidden_live_indices()
        for i in range(4):
            if i in hidden:
                if not self._bg_asleep[i] and self._bg_sleep_timers[i] is None:
                    self._start_bg_sleep_timer(i)
            else:
                self._cancel_bg_sleep_timer(i)
                if self._bg_asleep[i]:
                    self._wake_bg_stream(i)

    def _start_bg_sleep_timer(self, index):
        delay_ms = int(self.sleep_mode_minutes * 60 * 1000)
        self._bg_sleep_timers[index] = self.root.after(
            delay_ms, lambda idx=index: self._enter_bg_sleep(idx)
        )
        logging.info(f"Stream {index}: hidden behind fullscreen, sleeping in {self.sleep_mode_minutes} min if it stays hidden")

    def _cancel_bg_sleep_timer(self, index):
        handle = self._bg_sleep_timers[index]
        if handle is not None:
            try:
                self.root.after_cancel(handle)
            except Exception:
                pass
            self._bg_sleep_timers[index] = None

    def _enter_bg_sleep(self, index):
        self._bg_sleep_timers[index] = None
        if not self.running or self._bg_asleep[index]:
            return
        if index not in self._hidden_live_indices():
            return  # no longer hidden (timer raced a state change)
        if not self.media_players[index] and not self.stream_initializing[index]:
            return  # already stopped; nothing to do

        logging.info(f"Stream {index}: hidden for {self.sleep_mode_minutes} min, putting to sleep")
        self._bg_asleep[index] = True
        self._request_init_abort(index)

        def _teardown():
            with self.archive_entry_locks[index]:
                self._wait_init_idle(index)
                self.cleanup_stream(index)
                # Label only once the player has stopped painting.
                self.update_stream_label(index, "Sleeping")

        threading.Thread(target=_teardown, daemon=True).start()

    def _wake_bg_stream(self, index):
        """Restart a stream that hidden-stream sleep stopped."""
        self._bg_asleep[index] = False
        if self.is_archive_mode[index] or self.event_mode:
            return  # archive/events own this stream now

        logging.info(f"Stream {index}: no longer hidden, waking")
        self._disable_stream_action_buttons(indices=[index])

        def _restart():
            # Same lock as the sleep teardown, so the old player is released first.
            with self.archive_entry_locks[index]:
                if not (self.is_archive_mode[index] or self.event_mode or self._bg_asleep[index]):
                    self.try_init_stream_with_retries(index)
            self.root.after(0, self.update_layout)
            self.root.after(0, self._reenable_stream_action_buttons)

        threading.Thread(target=_restart, daemon=True).start()

    def _rearm_bg_sleep(self):
        """Config changed (timeout/checkbox): restart countdowns from now."""
        for i in range(4):
            self._cancel_bg_sleep_timer(i)
        self._schedule_bg_sleep_reconcile()

    def _rearm_sleep_mode_timer(self):
        """Re-evaluate after a config save: restart the countdown from the current focus state; 0 disables."""
        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None
        if self.sleep_mode_minutes <= 0:
            # Disabled: wake immediately if asleep.
            if self._is_asleep:
                self._wake_from_sleep_mode()
            return
        self._maybe_start_sleep_timer()

    def _enter_sleep_mode(self):
        """Stop live (non-archive, non-event) streams and remember them for wake."""
        self._sleep_timer_id = None

        # Re-check focus: it may have returned just as the timer fired.
        if self._app_focused and not self._app_minimized:
            return
        if self._is_asleep:
            return

        if self.event_mode:
            logging.info("Sleep mode: skipping, event mode is active")
            return

        stopped = []
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            if not self.ips[i] or not self.streams[i]:
                continue
            if not self.media_players[i] and not self.stream_initializing[i]:
                continue
            stopped.append(i)

        if not stopped:
            logging.info("Sleep mode: no active live streams to stop")
            self._is_asleep = True
            self._sleep_stopped_indices = []
            return

        logging.info(f"Sleep mode: stopping live streams {stopped} after {self.sleep_mode_minutes} min unfocused/minimized")
        self._is_asleep = True
        self._sleep_stopped_indices = stopped

        # Abort mid-init streams rather than releasing them under init_stream.
        for i in stopped:
            self._request_init_abort(i)

        def _sleep_teardown(idxs):
            for i in idxs:
                try:
                    with self.archive_entry_locks[i]:
                        self._wait_init_idle(i)
                        self.cleanup_stream(i)
                        # Label only after the player stops painting, inside the lock so a fast wake
                        # can't be overwritten.
                        self.update_stream_label(i, "Sleeping")
                except Exception as e:
                    logging.error(f"Sleep mode: error stopping stream {i}: {e}")
                if self.fullscreen_buttons[i]:
                    self.root.after(0, lambda idx=i: self.fullscreen_buttons[idx].place_forget())

        threading.Thread(target=_sleep_teardown, args=(list(stopped),), daemon=True).start()

    def _wake_from_sleep_mode(self):
        """Restart streams that sleep mode stopped."""
        if not self._is_asleep:
            return
        self._is_asleep = False
        to_restart = self._sleep_stopped_indices
        self._sleep_stopped_indices = []

        if not to_restart:
            return

        logging.info(f"Sleep mode: waking, restarting streams {to_restart}")

        self.root.after(0, self._disable_stream_action_buttons)

        def _restart_one(i):
            # Same lock as the sleep teardown, so the old player is released first.
            with self.archive_entry_locks[i]:
                if self.is_archive_mode[i] or self.event_mode:
                    return  # user moved on while asleep
                self.try_init_stream_with_retries(i)

        def _restart():
            threads = [
                threading.Thread(target=_restart_one, args=(i,), daemon=True)
                for i in to_restart
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.root.after(0, self.update_layout)
            self.root.after(0, self._reenable_stream_action_buttons)

        threading.Thread(target=_restart, daemon=True).start()

    def set_audio_state(self, index, mute=True):
        if not self.audio_enabled[index]:
            return

        with self.stream_init_lock:
            if self.stream_initializing[index]:
                return

        if self.media_players[index]:
            try:
                state = self.media_players[index].get_state()
                if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
                    return
                with self._vlc_audio_lock:
                    self.media_players[index].audio_set_mute(mute)
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set python-vlc audio state: {e}")

    def update_stream_label(self, index, text, fg="white", image=""):
        """Update the stream label from any thread."""
        try:
            self.root.after(0, lambda: self.labels[index].configure(image=image, text=text, fg=fg))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to update label: {e}")

    def _set_event_blank_label(self, index):
        """Blank a cam's label in event mode: 'Disabled' for unconfigured cams, else a dimmed 'Cam N'."""
        if not self.ips[index] or not self.streams[index]:
            self.labels[index].configure(image="", text="Disabled", fg="white", bg="black")
        else:
            self.labels[index].configure(image="", text=f"Cam {index + 1}", fg="#888888", bg="black")

    def try_init_stream_with_retries(self, index):
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                logging.warning(f"Stream {index}: Already initializing, skipping retry")
                return False
            self.stream_initializing[index] = True
        self._post_refresh_buttons()

        try:
            if not self.ips[index] or not self.streams[index]:
                self.update_stream_label(index, "Disabled")
                if self.fullscreen_buttons[index]:
                    self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                logging.info(f"Stream {index}: Disabled (no IP or URL)")
                return False

            max_attempts = self.max_retry_attempts if self.enable_retries else 1
            backoff_delay = self.initial_backoff_delay
            max_backoff = 30.0

            for attempt in range(max_attempts):
                # Abort requested (e.g. switched to archive mode during retries/backoff).
                if self.stream_abort_events[index].is_set() or not self.running:
                    logging.info(f"Stream {index}: Abort signal received, stopping init")
                    return False

                # Init failures aren't a quality signal; only the runtime monitor downgrades.
                if attempt > 0:
                    self.update_stream_label(index, f"Retrying... (Attempt {attempt+1}/{max_attempts})")
                else:
                    self.update_stream_label(index, "Loading...")

                if max_attempts > 1:
                    logging.info(f"Stream {index}: Attempt {attempt+1}/{max_attempts}")

                if not self.check_network_connectivity(self.ips[index]):
                    logging.warning(f"Stream {index}: Network check failed")
                    if attempt == max_attempts - 1:
                        self.cleanup_stream(index)
                        self.update_stream_label(index, "Stream Failed, click to reconnect")
                        self.bind_retry_connection(index)
                        if self.fullscreen_buttons[index]:
                            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                        logging.error(f"Stream {index}: Network unreachable after all attempts")
                        return False
                    time.sleep(backoff_delay)
                    backoff_delay = min(backoff_delay * 2, max_backoff)
                    continue

                # Camera is reachable but HQ keeps failing: fall back to LQ for this session.
                if (self.downgrade_sensitivity > 0 and self._effective_hq(index)
                        and self.hq_init_failures[index] >= self.HQ_INIT_FAILS_BEFORE_LQ):
                    logging.warning(f"Stream {index}: HQ failed to start {self.hq_init_failures[index]}x "
                                    f"although the camera is reachable, switching to low quality for this session")
                    self.session_lq[index] = True
                    self.update_stream(index)
                    self.update_stream_label(index, "HQ not starting, trying Low Quality...")

                self.cleanup_stream(index)
                if self.init_stream(index):
                    logging.info(f"Stream {index}: Initialized successfully")
                    self.hq_init_failures[index] = 0
                    self._apply_live_audio(index)
                    self.root.after(0, lambda idx=index: self.bind_stream_label(idx))
                    return True

                if self.stream_abort_events[index].is_set() or not self.running:
                    logging.info(f"Stream {index}: Init failed due to abort signal, yielding cleanup to signaller")
                    return False

                if self._effective_hq(index):
                    self.hq_init_failures[index] += 1

                if attempt == max_attempts - 1:
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    if self.fullscreen_buttons[index]:
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                    logging.error(f"Stream {index}: All attempts failed")
                    return False

                logging.info(f"Stream {index}: Attempt {attempt+1} failed, retrying in {backoff_delay:.2f}s")
                self.stream_abort_events[index].wait(timeout=backoff_delay)
                backoff_delay = min(backoff_delay * 2, max_backoff)

            return False
        except Exception as e:
            logging.error(f"Stream {index}: Unexpected error during retry: {e}")
            return False
        finally:
            with self.stream_init_lock:
                self.stream_initializing[index] = False
                # The init this abort targeted is over.
                self.stream_abort_events[index].clear()
            self._post_refresh_buttons()

    def build_vlc_instance_args(self, extra_args=None, allow_frame_drop=False):
        """Build the libvlc instance args. allow_frame_drop=True (clip playback) omits --no-skip-frames
        so 4x/8x playback doesn't stutter; live streams never drop frames."""
        args = [
            '--no-video-title-show',
            '--rtsp-tcp',
        ]
        if not allow_frame_drop:
            args.append('--no-skip-frames')
        if extra_args:
            args.extend(extra_args)
        args.extend(self.vlcparams)
        if sys.platform.startswith('win'):
            args.append('--aout=directsound')
        else:
            args.extend(['--aout=pulse', '--vout=gl'])
        if self.debug_mode:
            # verbose=1 (warnings/errors): level 2 floods the log with per-frame libvlc chatter.
            args.append('--verbose=1')
        return args

    def _vlc_log_handler(self, data, level, ctx, fmt, args):
        # libvlc levels: 0=DEBUG, 1=NOTICE, 2=WARNING, 3=ERROR. Forward only WARNING+, whatever --verbose says.
        if level < 2:
            return
        try:
            buf = ctypes.create_string_buffer(2048)
            libc = ctypes.CDLL(None)
            libc.vsnprintf(buf, ctypes.c_size_t(len(buf)), fmt, args)
            message = buf.value.decode(errors="replace")
        except Exception:
            message = "<unformattable libvlc log message>"

        level_map = {
            2: logging.WARNING,
            3: logging.ERROR,
        }
        logging.log(level_map.get(level, logging.WARNING), f"libvlc: {message}")

    def attach_vlc_logging(self, instance):
        """Attach the libvlc log callback when debug logging is on (safe if unavailable)."""
        if not self.debug_mode:
            return
        try:
            # One callback for the app's lifetime: instances keep a raw pointer to it.
            if getattr(self, "_vlc_log_cb", None) is None:
                self._vlc_log_cb = vlc.LogCb(self._vlc_log_handler)
            instance.log_set(self._vlc_log_cb, None)
        except Exception:
            pass

    def init_stream(self, index):
        """Initialize a stream using Python-VLC (libvlc auto-selects hardware decode if available)."""
        logging.info(f"Stream {index}: Initializing stream")
        try:
            # Resolved on the Tk thread (creating the X window is an Xlib call).
            xid = self._tk_call(lambda: self.labels[index].winfo_id())
        except Exception as e:
            logging.error(f"Stream {index}: Failed to get window ID: {e}")
            return False

        timeout = 8
        start_wait = time.time()
        check_interval = 0.5
        required_frames = 5
        frame_times = []
        start_lock_held = False

        try:
            # Start streams one at a time; the frame wait below still runs in parallel.
            self._vlc_start_lock.acquire()
            start_lock_held = True
            instance = vlc.Instance(self.build_vlc_instance_args())
            if not instance:
                raise RuntimeError("Failed to create VLC instance")
            self.attach_vlc_logging(instance)
            self.vlc_instances[index] = instance
            player = instance.media_player_new()
            if not player:
                raise RuntimeError("Failed to create VLC media player")
            self.media_players[index] = player
            media = instance.media_new(self.streams[index])
            if not self.audio_enabled[index]:
                # Audio off: create no audio output at all, so nothing to mute or race.
                media.add_option(":no-audio")
            player.set_media(media)
            player.set_xwindow(xid) if sys.platform.startswith("linux") else player.set_hwnd(xid)

            if player.play() == -1:
                raise RuntimeError("Failed to start VLC player")

            # No early audio_set_mute(True): it raced audio-output creation and segfaulted in VLC's
            # PipeWire plugin. _apply_live_audio() mutes once the stream is up.
            self._vlc_start_lock.release()
            start_lock_held = False

            while time.time() - start_wait < timeout:
                if self.stream_abort_events[index].is_set() or not self.running:
                    logging.info(f"Stream {index}: Abort signal during frame wait, stopping init")
                    try:
                        if player.get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                            player.stop()
                        player.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing player during abort: {e}")
                    self.media_players[index] = None
                    # Each stream owns its instance, so release it here too.
                    try:
                        instance.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing VLC instance during abort: {e}")
                    if self.vlc_instances[index] is instance:
                        self.vlc_instances[index] = None
                    return False
                stats = vlc.MediaStats()
                if player.get_media().get_stats(stats):
                    current_displayed = stats.displayed_pictures
                    new_frames = current_displayed - self.last_displayed_frames[index]
                    self.last_displayed_frames[index] = current_displayed
                    if new_frames > 0:
                        frame_times.append((time.time(), new_frames))
                        frame_times = [(t, f) for t, f in frame_times if time.time() - t < timeout]
                        recent_frames = sum(f for _, f in frame_times)
                        if recent_frames >= required_frames:
                            for _ in range(5):
                                width, height = player.video_get_size(0) or (0, 0)
                                if width > 0 and height > 0:
                                    self.frame_shapes[index] = (width, height)
                                    break
                                time.sleep(0.5)
                            player.video_set_scale(0)
                            self.last_dropped_frames[index] = stats.lost_pictures
                            mt = threading.Thread(target=self.monitor_stream, args=(index, player), daemon=True)
                            self.monitor_threads[index] = mt
                            mt.start()
                            return True
                if player.get_state() in (vlc.State.Error, vlc.State.Ended):
                    raise RuntimeError("Stream encountered error or ended")
                time.sleep(check_interval)
            logging.warning(f"Stream {index}: No frames detected within {timeout}s")
            return False
        except Exception as e:
            if start_lock_held:
                self._vlc_start_lock.release()
            logging.error(f"Stream {index}: Python-VLC initialization failed: {e}")
            return False

    def cleanup_stream(self, index):
        # Only log cleanups that actually release something (most calls are no-ops).
        _had_resources = bool(self.media_players[index] or self.vlc_instances[index])
        _cleanup_t0 = time.time()
        if _had_resources:
            logging.info(f"Stream {index}: Cleaning up")
        self.stream_cleanup_events[index].set()
        self._stop_hover_poll(index)

        # Let the monitor thread exit before releasing its player (unless it's the caller).
        # Bounded so a stuck monitor can't wedge the caller.
        mt = self.monitor_threads[index]
        if mt is not None and mt is not threading.current_thread() and mt.is_alive():
            mt.join(timeout=3.0)

        try:
            if self.media_players[index]:
                try:
                    if self.media_players[index].get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                        self.media_players[index].stop()
                    self.media_players[index].release()
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing media player: {e}")
                self.media_players[index] = None

            # Each stream owns its instance exclusively, so releasing it can't affect other streams.
            if self.vlc_instances[index]:
                try:
                    self.vlc_instances[index].release()
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing VLC instance: {e}")
                self.vlc_instances[index] = None

            self.frame_shapes[index] = (0, 0)
            self.net_state[index] = None
            self.last_dropped_frames[index] = 0
            self.last_displayed_frames[index] = 0

            if _had_resources:
                logging.info(f"Stream {index}: Cleanup completed in {int((time.time() - _cleanup_t0) * 1000)}ms")
        except Exception as e:
            logging.error(f"Stream {index}: Cleanup failed: {e}")
        finally:
            self.stream_cleanup_events[index].clear()

    def _retry_stream_connection_thread(self, index):
        """Thread function to retry stream connection and restore bindings."""
        try:
            success = self.try_init_stream_with_retries(index)
            if success:
                logging.info(f"Stream {index}: Retry successful, restoring bindings")
                self.root.after(0, lambda: self.bind_stream_label(index))
                self.root.after(0, self.update_layout)
            else:
                logging.warning(f"Stream {index}: Retry failed, keeping retry binding")
                self.root.after(0, lambda: self.bind_retry_connection(index))
        except Exception as e:
            logging.error(f"Stream {index}: Error during retry: {e}")
            self.root.after(0, lambda: self.bind_retry_connection(index))

    def retry_stream_connection(self, index):
        """Retry the stream connection and restore bindings if successful."""
        logging.info(f"Stream {index}: Retrying connection due to label click")
        threading.Thread(target=self._retry_stream_connection_thread, args=(index,), daemon=True).start()

    def bind_retry_connection(self, index):
        """Bind the label to retry the stream connection."""
        try:
            self.labels[index].unbind("<Button-1>")
            self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind retry connection: {e}")

    @classmethod
    def _starvation(cls, kb_per_s):
        """Starvation score 0..1 for a rate in kB/s (linear between FULL and NONE)."""
        full, none = cls.NET_STARVE_FULL_KB, cls.NET_STARVE_NONE_KB
        if kb_per_s <= full:
            return 1.0
        if kb_per_s >= none:
            return 0.0
        return (none - kb_per_s) / (none - full)

    def _net_sample(self, index, stats, now):
        """Update network pressure from the last second; returns it, or None if no usable sample."""
        total = max(int(stats.demux_read_bytes), int(stats.read_bytes))

        st = self.net_state[index]
        if st is None:
            self.net_state[index] = {"t": now, "bytes": total, "pressure": 0.0}
            return None

        dt = now - st["t"]
        if dt < 0.5:
            return None

        rate = max(0, total - st["bytes"]) / dt
        starve = self._starvation(rate / 1024.0)
        pressure = st["pressure"] * math.exp(-dt / self.NET_PRESSURE_TAU_S) + starve
        st.update(t=now, bytes=total, pressure=pressure)
        return pressure

    def _downgrade_stream_quality(self, index, player):
        """Switch one live stream to LQ on a worker thread, under its entry lock."""
        with self.archive_entry_locks[index]:
            # Bail out if the stream changed since the monitor asked.
            if (not self.running
                    or self.media_players[index] is not player
                    or self.is_archive_mode[index]
                    or self.event_mode
                    or self._is_asleep
                    or self._bg_asleep[index]):
                logging.info(f"Stream {index}: Quality downgrade skipped, stream state changed")
                return
            logging.warning(f"Stream {index}: Switching to low quality for this session")
            self.session_lq[index] = True
            self.update_stream(index)
            self.update_stream_label(index, "Switching to Low Quality...")
            self.try_init_stream_with_retries(index)

    def monitor_stream(self, index, player):
        # Fails a stalled stream; requests an LQ downgrade on network pressure (session-only, no auto-upgrade).
        logging.info(
            f"Monitoring stream {index} (quality={'HQ' if self._effective_hq(index) else 'LQ'}, "
            f"downgrade sensitivity={self.downgrade_sensitivity}, "
            f"trigger pressure={self.NET_TRIGGER_BY_LEVEL.get(self.downgrade_sensitivity, 'off')})"
        )

        last_check = time.time()
        last_frame_time = time.time()
        no_frame_timeout = self.no_frame_timeout
        pressure = 0.0

        while self.running and self.media_players[index] is player:
            if self.stream_cleanup_events[index].wait(timeout=1.0):
                break

            try:
                current_time = time.time()

                if player is None:
                    logging.error(f"Stream {index}: No player, exiting monitor")
                    break

                state = player.get_state()
                if state in (vlc.State.Ended, vlc.State.Error):
                    logging.error(f"Stream {index} stopped: {state}")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                if current_time - last_check >= 1.0:
                    try:
                        stats = vlc.MediaStats()
                        media_obj = player.get_media()
                        if media_obj.get_stats(stats):
                            current_displayed = stats.displayed_pictures
                            displayed_frames = max(0, current_displayed - self.last_displayed_frames[index])
                            self.last_displayed_frames[index] = current_displayed

                            if displayed_frames > 0:
                                last_frame_time = current_time

                            sample = self._net_sample(index, stats, current_time)
                            if sample is not None:
                                pressure = sample
                        # No stats this tick: the no-frame timeout below covers real stalls.
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error fetching VLC stats: {e}")

                    last_check = current_time

                if current_time - last_frame_time > no_frame_timeout:
                    logging.error(f"Stream {index}: No frames for {no_frame_timeout}s, marking failed")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                # Quality downgrade (session-only)
                trigger = self.NET_TRIGGER_BY_LEVEL.get(self.downgrade_sensitivity)
                if trigger and self._effective_hq(index) and pressure >= trigger:
                    logging.warning(
                        f"Stream {index}: network pressure {pressure:.1f} >= "
                        f"{trigger:g}, requesting downgrade to LQ"
                    )
                    threading.Thread(
                        target=self._downgrade_stream_quality, args=(index, player), daemon=True
                    ).start()
                    break

            except Exception as e:
                logging.error(f"Stream {index}: Monitoring error: {e}")
                self.update_stream_label(index, "Stream Failed")
                break

        logging.info(f"Stream {index} monitoring stopped")

    # --- Mode-transition guard: single source of truth for whether a mode change may start ---
    # Drives the buttons and is checked in every handler, so queued clicks are rejected.

    def _modes_busy(self):
        if self._mode_busy > 0 and time.time() - self._mode_busy_since > 60:
            logging.warning("Mode-transition busy flag held for >60s, forcing it clear")
            self._mode_busy = 0
        if self._mode_busy > 0 or any(self.archive_transitioning) or self.pending_vlc_teardown:
            return True
        return bool(self.LOCK_MODE_BUTTONS_DURING_INIT and any(self.stream_initializing))

    def _index_locked(self, index):
        return bool(
            self._mode_busy > 0
            or self.archive_transitioning[index]
            or index in self.pending_vlc_teardown
            or (self.LOCK_MODE_BUTTONS_DURING_INIT and self.stream_initializing[index])
        )

    def _begin_mode_switch(self):
        """Tk thread only. Pair with _end_mode_switch (posted from the worker)."""
        self._mode_busy += 1
        self._mode_busy_since = time.time()
        self._refresh_mode_buttons()

    def _end_mode_switch(self):
        self._mode_busy = max(0, self._mode_busy - 1)
        self._reenable_stream_action_buttons()

    def _post_to_tk(self, fn):
        try:
            self.root.after(0, fn)
        except Exception:
            pass   # root already destroyed (shutdown)

    def _post_refresh_buttons(self):
        self._post_to_tk(self._refresh_mode_buttons)

    def _refresh_mode_buttons(self, indices=None):
        """Recompute Archive/Events/per-panel archive button state from the guard. Tk thread only."""
        try:
            busy = self._modes_busy()
            any_archive_mode = (not self.event_mode) and any(self.is_archive_mode[i] for i in range(4))
            if self.archive_mode_button:
                if busy:
                    self.archive_mode_button.configure(
                        state="disabled",
                        image=self.icon_cache["disk_active"] if any_archive_mode else self.icon_cache["disk_disabled"]
                    )
                elif self.archive_dir:
                    self.archive_mode_button.configure(
                        state="normal",
                        image=self.icon_cache["disk_active"] if any_archive_mode else self.icon_cache["disk"]
                    )
            if self.events_button:
                if busy:
                    self.events_button.configure(
                        state="disabled",
                        image=self.icon_cache["events_active"] if self.event_mode else self.icon_cache["events_disabled"]
                    )
                elif self.motion_triggered_events and self.archive_dir:
                    self.events_button.configure(
                        state="normal",
                        image=self.icon_cache["events_active"] if self.event_mode else self.icon_cache["events"]
                    )
            for i in (range(4) if indices is None else indices):
                btn = self.archive_buttons[i]
                if btn:
                    locked = self._index_locked(i)
                    btn.configure(
                        state="disabled" if locked else "normal",
                        image=(self.icon_cache["disk_active"] if self.is_archive_mode[i]
                               else self.icon_cache["disk_disabled"] if locked
                               else self.icon_cache["disk"])
                    )
        except Exception as e:
            logging.warning(f"Failed to refresh mode buttons: {e}")

    def _disable_stream_action_buttons(self, indices=None):
        # Kept for existing callers; re-evaluates the guard.
        self._refresh_mode_buttons(indices)

    def _reenable_stream_action_buttons(self, indices=None):
        # A stream that restarted while hidden needs its countdown.
        self._schedule_bg_sleep_reconcile()
        self._refresh_mode_buttons(indices)

    def _defer_exit_event_mode(self):
        """Leave Events once an in-flight transition clears, instead of dropping the click."""
        if self._exit_event_pending:
            return
        self._exit_event_pending = True
        logging.info("Events: exit requested mid-transition, deferring until idle")

        def _poll():
            if not self.running:
                self._exit_event_pending = False
                return
            if self._modes_busy():
                self.root.after(100, _poll)
                return
            self._exit_event_pending = False
            if self.event_mode:
                self._exit_event_mode()
        self.root.after(100, _poll)

    # --- Cross-thread helpers ---

    def _tk_call(self, fn, timeout=30.0):
        """Run fn on the Tk thread and wait for its result (inline if already on it)."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        box = {}

        def _w():
            try:
                box["value"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                done.set()
        self.root.after(0, _w)
        if not done.wait(timeout):
            raise TimeoutError("Tk thread did not run the callback in time")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _tk_wait(self, fn):
        """_tk_call that only logs on failure (for cleanup steps)."""
        try:
            self._tk_call(fn)
        except Exception as e:
            logging.warning(f"Tk-side step did not complete: {e}")

    def _wait_init_idle(self, index, timeout=10.0):
        """Block (worker thread) until no init is in flight, so a player isn't released mid-init."""
        deadline = time.time() + timeout
        while self.stream_initializing[index] and time.time() < deadline:
            time.sleep(0.05)
        if self.stream_initializing[index]:
            logging.warning(f"Stream {index}: init still running after {timeout}s, proceeding anyway")

    def _clear_pending_teardown(self, index):
        self.pending_vlc_teardown.discard(index)
        self._post_refresh_buttons()

    def start_streams(self):

        self.root.after(0, self._disable_stream_action_buttons)

        threads = []
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            if self.ips[i]:
                thread = threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

        self.root.after(0, self._reenable_stream_action_buttons)

    def restart_previously_playing_streams(self, was_playing):
        """Like start_streams, but only restarts streams that were live before the config save."""

        self.root.after(0, self._disable_stream_action_buttons)

        threads = []
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            # Restart streams that were live and still have a valid IP/URL.
            if was_playing[i] and self.ips[i] and self.streams[i]:
                thread = threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
            elif was_playing[i]:
                # Was live but lost its IP/URL (removed or now a duplicate): stop it.
                thread = threading.Thread(target=self._stop_removed_stream, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

        self.root.after(0, self._reenable_stream_action_buttons)

    def update_layout(self):
        """Update the layout of panels based on current state."""
        current_state = {
            'is_fullscreen': self.is_fullscreen,
            'is_fullscreen_index': self.fullscreen_index,
            'window_size': (self.grid_frame.winfo_width(), self.grid_frame.winfo_height())
        }

        if hasattr(self, 'last_layout_state') and self.last_layout_state == current_state:
            return

        size_changed = not hasattr(self, 'last_layout_state') or \
                       self.last_layout_state['window_size'] != current_state['window_size']

        self.last_layout_state = current_state

        x_offset = 60
        self.grid_frame.place(x=0, y=0, width=-x_offset, relwidth=1.0, relheight=1.0)
        w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
        if w <= 10 or h <= 10:
            w, h = 1920, 1080

        if size_changed:
            self._debounced_render_archive_views()

        if self.is_fullscreen and self.fullscreen_index is not None:
            for i in range(4):
                if i == self.fullscreen_index:
                    self.panels[i].place(x=0, y=0, width=w, height=h)
                    self.panel_sizes[i] = (w, h)
                    self.update_target_dims(i)
                    if (not self.is_archive_mode[i] and self.media_players[i]
                            and i not in self.pending_vlc_teardown):
                        # Don't rebind a stale clip player still releasing on a background thread
                        # (is_archive_mode can flip to False before teardown finishes).
                        hwnd = self.labels[i].winfo_id()
                        self.media_players[i].set_hwnd(hwnd) if sys.platform.startswith("win") else self.media_players[i].set_xwindow(hwnd)
                else:
                    self.panels[i].place_forget()
                    if self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()
        else:
            ww, hh = w // 2, h // 2
            self.panel_sizes = [(ww, hh)] * 4
            self.panels[0].place(x=0, y=0, width=ww, height=hh)
            self.panels[1].place(x=ww, y=0, width=ww, height=hh)
            self.panels[2].place(x=0, y=hh, width=ww, height=hh)
            self.panels[3].place(x=ww, y=hh, width=ww, height=hh)
            for i in range(4):
                self.update_target_dims(i)

    def _debounced_render_archive_views(self):
        """Debounced render of archive views (root.after keeps widget work on the Tk thread)."""
        prev = getattr(self, "_archive_render_after_id", None)
        if prev is not None:
            try:
                self.root.after_cancel(prev)
            except Exception:
                pass
        self._archive_render_after_id = self.root.after(200, self._render_archive_views_now)

    def _render_archive_views_now(self):
        self._archive_render_after_id = None
        if not self.running:
            return
        for i in range(4):
            if self.is_archive_mode[i] and self.current_archive_path[i]:
                self.render_archive_view(i)

    def update_target_dims(self, index):
        w, h = self.panel_sizes[index]
        if w <= 10 or h <= 10:
            self.target_dims[index] = (0, 0)
            return
        frame_w, frame_h = self.frame_shapes[index]
        if frame_w <= 0 or frame_h <= 0:
            self.target_dims[index] = (0, 0)
            return
        self.target_dims[index] = (w, h)

    def handle_stream_click(self, index):
        if not self.streams[index] and not self.is_archive_mode[index]:
            return

        if self.is_fullscreen and self.fullscreen_index == index:
            self.exit_fullscreen()
        else:
            self.is_fullscreen = True
            self.fullscreen_index = index
            self._apply_fullscreen_audio()

            self.build_config_panel()
        
        logging.info(f"{'Entered' if self.is_fullscreen else 'Exited'} fullscreen mode for stream {index} via click")


    def toggle_all_archive_mode(self):
        """Toggle archive mode for all streams (mirrors the Events button): closes Events mode first,
        exits archive mode if active, otherwise enters it."""
        if not self.archive_dir:
            return

        if self._modes_busy():
            logging.info("Archive: ignored, a mode transition is still in progress")
            return

        if self.event_mode:
            # Events -> Archive: close the overlay without restarting live streams (eligible cams go
            # to archive, the rest are stopped below).
            self._exit_event_mode(restart_streams=False)

        any_archive_mode = any(self.is_archive_mode[i] for i in range(4))

        if any_archive_mode:
            for i in range(4):
                if self.is_archive_mode[i] and not self.archive_transitioning[i]:
                    # rebuild_ui=True: each stream's exit flow rebuilds the panel once its lock lifts, so the
                    # archive icon stays red until the restart finishes.
                    self.toggle_archive_mode(i, rebuild_ui=True)
                    logging.info(f"Stream {i}: Exited archive mode via global toggle")
            return

        eligible = [i for i in range(4) if self.streams[i] and not self.is_archive_mode[i]]
        if not eligible:
            # Nothing to archive. If we just left event mode without restarting, restart cams now.
            cams_to_restart = [i for i in range(4) if self.ips[i] and self.streams[i] and not self.media_players[i]]
            if cams_to_restart:
                threading.Thread(
                    target=lambda: [self.try_init_stream_with_retries(i) for i in cams_to_restart],
                    daemon=True
                ).start()
            return

        # Stop every other live stream so no feed keeps decoding behind the archive browser.
        for i in range(4):
            if (i not in eligible and not self.is_archive_mode[i]
                    and (self.media_players[i] or self.stream_initializing[i])):
                self._request_init_abort(i)
                threading.Thread(target=self._locked_cleanup_stream, args=(i,), daemon=True).start()

        for i in eligible:
            if not self.archive_transitioning[i]:
                self.toggle_archive_mode(i, rebuild_ui=False)
                logging.info(f"Stream {i}: Entered archive mode via global toggle")
        self.build_config_panel()

    def toggle_archive_mode(self, index, rebuild_ui=True, restart_stream=True):
        if not self.archive_dir:
            return

        if self.archive_transitioning[index]:
            return
        if self._mode_busy > 0:
            logging.info(f"Stream {index}: archive toggle ignored, a mode transition is in progress")
            return
        self.archive_transitioning[index] = True

        self._request_init_abort(index)

        self._play_tokens[index] += 1
        self.is_archive_mode[index] = not self.is_archive_mode[index]
        logging.info(f"Stream {index}: Archive mode {'enabled' if self.is_archive_mode[index] else 'disabled'}")

        if not self.is_archive_mode[index]:
            # Teardown for this index is about to start on a background thread; mark it pending so
            # update_layout() doesn't rebind the old clip player as live. Cleared in _exit_archive_locked.
            self.pending_vlc_teardown.add(index)

        # Lock the mode buttons now, not after a worker thread gets scheduled.
        self._refresh_mode_buttons()

        if self.is_archive_mode[index]:
            self.labels[index].pack_forget()
            self.archive_canvas[index].pack(fill="both", expand=True)
            self.archive_canvas[index].delete("all")

            if self.archive_buttons[index]:
                self.archive_buttons[index].config(state="normal")
            if rebuild_ui:
                self.build_config_panel()

            if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
                for btn in self.nav_buttons[index].values():
                    btn.place_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()
            panel_width, panel_height = self.panel_sizes[index]
            self.archive_canvas[index].create_text(
                panel_width // 2, panel_height // 2,
                text="Loading...", fill="white", font=self.app_font(-16)
            )
            threading.Thread(target=self._enter_archive_mode_thread, args=(index,), daemon=True).start()
        else:
            # Teardown runs in a background thread under archive_entry_locks, VLC before widgets (destroying
            # vlc_frame first raises BadWindow on Linux; Tk-thread teardown deadlocks on Windows).
            def _exit_archive_locked():
                with self.archive_entry_locks[index]:
                    self._cleanup_archive_mode_vlc(index)
                    self._clear_pending_teardown(index)
                    # Wait for the Tk-side repack before reattaching the live stream.
                    self._tk_wait(lambda: self._cleanup_archive_mode_ui(index))

                    if restart_stream:
                        self.try_init_stream_with_retries(index)
                    else:
                        logging.info(f"Stream {index}: Archive mode exited without restarting live stream (mode switch)")

                # Teardown/restart for this index is resolved: accept the next toggle, then refresh buttons.
                self.archive_transitioning[index] = False

                def _on_done():
                    self._reenable_stream_action_buttons(indices=[index])
                    if rebuild_ui:
                        self.build_config_panel()
                self._post_to_tk(_on_done)

            threading.Thread(target=_exit_archive_locked, daemon=True).start()

    def _enter_archive_mode_thread(self, index):
        with self.archive_entry_locks[index]:
            self._enter_archive_mode_thread_locked(index)

    def _enter_archive_mode_thread_locked(self, index):
        """Body of _enter_archive_mode_thread; archive_entry_locks[index] must be held."""
      
        deadline = time.time() + 10.0
        while self.stream_initializing[index] and time.time() < deadline:
            time.sleep(0.05)
        if self.stream_initializing[index]:
            logging.warning(f"Stream {index}: Init did not stop within timeout, proceeding anyway")
        self.cleanup_stream(index)
        # Clears a lingering event-clip teardown left pending by _exit_event_mode(restart_streams=False).
        self._clear_pending_teardown(index)

        root_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        try:
            exists = os.path.isdir(root_path)
        except Exception as e:
            logging.warning(f"Stream {index}: Error accessing archive directory {root_path}: {e}")
            exists = False

        def finish():
            try:
                if not self.is_archive_mode[index]:
                    # User toggled back out meanwhile
                    return
                if not exists:
                    self.archive_canvas[index].delete("all")
                    panel_width, panel_height = self.panel_sizes[index]
                    self.archive_canvas[index].create_text(
                        panel_width // 2, panel_height // 2,
                        text="Archive directory not found", fill="white", font=self.app_font(-16)
                    )
                    return
                self.pagination_state[index] = {root_path: 0}
                self.current_archive_path[index] = root_path
                self.render_archive_view(index)
            finally:
                # Entry resolved (browser or error shown): accept new toggles.
                self.archive_transitioning[index] = False
                self._refresh_mode_buttons()

        self.root.after(0, finish)


    def get_cached_thumbnail(self, thumbnail_path, width, height):
        try:
            mtime = os.path.getmtime(thumbnail_path)
        except OSError:
            mtime = None
        key = (thumbnail_path, width, height, mtime)

        cached = self.thumbnail_cache.get(key)
        if cached is not None:
            return cached

        with Image.open(thumbnail_path) as img:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)

        self.thumbnail_cache[key] = photo
        self.thumbnail_cache_order.append(key)
        if len(self.thumbnail_cache_order) > self.thumbnail_cache_max:
            oldest = self.thumbnail_cache_order.pop(0)
            self.thumbnail_cache.pop(oldest, None)

        return photo

    def draw_progress_bar(self, index, x, y, width, height, progress):
        BORDER_INSET = 1
        bar_height = 4
        bar_x = x + BORDER_INSET
        bar_width = width - (BORDER_INSET * 2)
        bar_bottom = y + height - BORDER_INSET
        bar_y = bar_bottom - bar_height

        duration = progress.get("duration", 0)
        if not duration or duration <= 0:
            return
        fraction = max(0.0, min(1.0, progress.get("position", 0) / duration))

        self.archive_canvas[index].create_rectangle(
            bar_x, bar_y, bar_x + bar_width, bar_bottom,
            fill="#3d3d3d", outline=""
        )
        if fraction > 0:
            self.archive_canvas[index].create_rectangle(
                bar_x, bar_y, bar_x + (bar_width * fraction), bar_bottom,
                fill="#e62117", outline=""
            )

    def render_archive_view(self, index):
        # Hide nav buttons so they never show on a loading canvas.
        if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
            if "prev" in self.nav_buttons[index]:
                self.nav_buttons[index]["prev"].place_forget()
            if "next" in self.nav_buttons[index]:
                self.nav_buttons[index]["next"].place_forget()

        self.archive_canvas[index].delete("all")

        path = os.path.normpath(self.current_archive_path[index])
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0

        panel_width, panel_height = self.panel_sizes[index]
        icon_size = 100
        text_height = 30
        item_width = icon_size + 20
        item_height = icon_size + text_height + 20
        margin_x, margin_y = 20, 60  # Left/top margins (top leaves room for buttons and location text)
        
        # Day folders (YYYY-MM-DD) use thumbnail-sized items when thumbnails exist
        is_day_folder = re.match(r".*\d{4}-\d{2}-\d{2}$", path)
        thumbnail_width = None
        thumbnail_height = None
        use_thumbnails = False
        if is_day_folder:
            try:
                thumbnails_dir = os.path.join(path, "thumbnails")
                if os.path.isdir(thumbnails_dir):
                    items = os.listdir(path)
                    mp4_files = [item for item in items if item.endswith(".mp4")]
                    if mp4_files:
                        # Use the first thumbnail's size for all (assumed equal)
                        for mp4_file in mp4_files:
                            base_name = os.path.splitext(mp4_file)[0]
                            thumbnail_path = os.path.join(thumbnails_dir, f"{base_name}.jpg")
                            if os.path.exists(thumbnail_path):
                                with Image.open(thumbnail_path) as img:
                                    original_width, original_height = img.size
                                break
                        else:
                            original_width, original_height = None, None
                        if original_width and original_height:
                            if not self.is_fullscreen:
                                thumbnail_height = 120
                                aspect_ratio = original_width / original_height
                                thumbnail_width = int(thumbnail_height * aspect_ratio)
                            else:
                                # Fullscreen: original size
                                thumbnail_width, thumbnail_height = original_width, original_height
                            icon_size = thumbnail_width
                            item_width = icon_size + 20
                            item_height = thumbnail_height + text_height + 20
                            use_thumbnails = True
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to check thumbnails in {path}: {e}")

        max_columns = (panel_width - 2 * margin_x) // item_width
        max_rows = (panel_height - margin_y - 20) // item_height
        items_per_page = max_columns * max_rows
        if items_per_page < 1:
            items_per_page = 1

        if not self.back_buttons[index]:
            back_img = self.icon_cache["back"]
            self.back_buttons[index] = tk.Button(
                self.archive_canvas[index],
                image=back_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.back_buttons[index].image = back_img
        self.back_buttons[index].place(x=31, y=5)

        if not os.path.isdir(path):
            logging.error(f"Stream {index}: Path {path} is not a directory")
            return

        try:
            items = os.listdir(path)
            if is_day_folder:
                items = [item for item in items if item != "thumbnails"]
        except Exception as e:
            logging.error(f"Stream {index}: Failed to list directory {path}: {e}")
            return
            
        def get_sort_key(item):
            try:
                full_path = os.path.join(path, item)
                if os.path.isdir(full_path):
                    # Folders sort by date
                    return datetime.strptime(item, "%Y-%m-%d")
                elif item.endswith(".mp4"):
                    # Files sort by start time.
                    match = re.match(
                        r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_(\d+m-\d+s)"
                        r"(?:_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)?\.mp4$",
                        item
                    )
                    if not match:
                        logging.warning(f"Stream {index}: Invalid video format for {item}")
                        return datetime.min
                    date_str, time_str, _ = match.groups()
                    date_time = f"{date_str} {time_str.replace('-', ':')}"
                    return datetime.strptime(date_time, "%Y-%m-%d %H:%M:%S")
                return datetime.min
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to parse item {item}: {e}")
                return datetime.min

        # Folders descending at the root, files ascending
        folder_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        is_folder_path = os.path.normpath(path) == folder_path
        sorted_items = sorted(items, key=lambda it: (get_sort_key(it), it), reverse=is_folder_path)

        total_items = len(sorted_items)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        current_page = max(0, min(self.pagination_state[index][path], total_pages - 1))
        self.pagination_state[index][path] = current_page
        start_idx = current_page * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        page_items = sorted_items[start_idx:end_idx]

        start_x = margin_x
        x, y = start_x, margin_y
        column = 0

        norm_path = path.replace("\\", "/")
        cam_index = norm_path.rfind(f"/cam{index + 1}")
        if cam_index != -1:
            location = norm_path[cam_index:].replace("/", " / ")
            location = re.sub(r'(?i)\bcam(\d+)\b', lambda m: 'CAM ' + m.group(1), location).upper()
            self.archive_canvas[index].create_text(
                80, 25, anchor="w", text=f"{location}", fill="white", font=self.app_font(-17)
            )

        page_images = []
        for item in page_items:
            full_path = os.path.join(path, item)
            is_visited = os.path.normpath(full_path) in self.visited_folders[index]
            progress = self.watch_progress[index].get(full_path)

            if os.path.isdir(full_path):
                day_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", item)
                if day_match:
                    try:
                        folder_date = datetime.strptime(item, "%Y-%m-%d")
                        day_abbrev = folder_date.strftime("%a")
                        folder_img = self.get_day_folder_icon(day_abbrev, is_visited)
                    except Exception:
                        folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]
                else:
                    folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]

                folder_id = self.archive_canvas[index].create_image(x + item_width // 2, y + icon_size // 2, image=folder_img)
                if day_match:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item, fill="white", font=self.app_font(-17), anchor="n"
                    )
                else:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item[:10], fill="white", font=self.app_font(-17), anchor="n"
                    )

                for id_ in (folder_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.open_folder(index, p)
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )

                page_images.append(folder_img)

            elif item.endswith(".mp4"):
                if use_thumbnails:
                    base_name = os.path.splitext(item)[0]
                    thumbnail_path = os.path.join(path, "thumbnails", f"{base_name}.jpg")
                    if os.path.exists(thumbnail_path):
                        try:
                            video_img = self.get_cached_thumbnail(thumbnail_path, thumbnail_width, thumbnail_height)
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + thumbnail_height // 2, image=video_img
                            )
                            page_images.append(video_img)
                            thumb_x = x + (item_width - thumbnail_width) // 2
                            self.archive_canvas[index].create_rectangle(
                                thumb_x, y, thumb_x + thumbnail_width, y + thumbnail_height,
                                outline='#ffffff', width=2
                            )
                            if progress:
                                self.draw_progress_bar(
                                    index, thumb_x, y, thumbnail_width, thumbnail_height, progress
                                )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Button-1>",
                                lambda e, p=full_path: self.play_archive_video(index, p)
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Enter>",
                                lambda e: self.archive_canvas[index].config(cursor="hand2")
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Leave>",
                                lambda e: self.archive_canvas[index].config(cursor="")
                            )
                        except Exception as e:
                            logging.warning(f"Stream {index}: Failed to load thumbnail {thumbnail_path}: {e}")
                            video_img = self.icon_cache["archive"]
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + icon_size // 2, image=video_img
                            )
                            page_images.append(video_img)
                            if progress:
                                icon_x = x + (item_width - icon_size) // 2
                                self.draw_progress_bar(
                                    index, icon_x, y, icon_size, icon_size, progress
                                )
                    else:
                        video_img = self.icon_cache["archive"]
                        video_id = self.archive_canvas[index].create_image(
                            x + item_width // 2, y + icon_size // 2, image=video_img
                        )
                        page_images.append(video_img)
                        if progress:
                            icon_x = x + (item_width - icon_size) // 2
                            self.draw_progress_bar(
                                index, icon_x, y, icon_size, icon_size, progress
                            )
                else:
                    video_img = self.icon_cache["archive"]
                    video_id = self.archive_canvas[index].create_image(
                        x + item_width // 2, y + icon_size // 2, image=video_img
                    )
                    page_images.append(video_img)
                    if progress:
                        icon_x = x + (item_width - icon_size) // 2
                        self.draw_progress_bar(
                            index, icon_x, y, icon_size, icon_size, progress
                        )

                # Label: HH:MM plus clip duration
                time_parts = item.split('_')[1].split('-')[:2]
                time_label = ':'.join(time_parts)
                dur_label  = item.split('_')[2].split('.')[0].replace('-', '')
                label = f"{time_label} {dur_label}"
                text_id = self.archive_canvas[index].create_text(
                    x + item_width // 2, y + (thumbnail_height if use_thumbnails else icon_size) + 10, text=label, fill="white", font=self.app_font(-17), anchor="n"
                )

                for id_ in (video_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.play_archive_video(index, p)
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )
            
            column += 1
            x += item_width
            if column >= max_columns:
                x = start_x
                y += item_height
                column = 0

        # Keep PhotoImage references so they aren't garbage-collected.
        self.archive_canvas[index].images = page_images

        if not hasattr(self, 'nav_buttons'):
            self.nav_buttons = [{} for _ in range(len(self.panel_sizes))]

        if total_pages > 1:
            last_column_x = 5 + margin_x + (max_columns - 1) * item_width + item_width // 2

            if "prev" not in self.nav_buttons[index]:
                prev_img = self.icon_cache["left"]
                self.nav_buttons[index]["prev"] = tk.Button(
                    self.archive_canvas[index],
                    image=prev_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, -1)
                )
                self.nav_buttons[index]["prev"].image = prev_img
            self.nav_buttons[index]["prev"].place(x=last_column_x - 40, y=5)
            self.nav_buttons[index]["prev"].config(state="normal" if current_page > 0 else "disabled")

            if "next" not in self.nav_buttons[index]:
                next_img = self.icon_cache["right"]
                self.nav_buttons[index]["next"] = tk.Button(
                    self.archive_canvas[index],
                    image=next_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, 1)
                )
                self.nav_buttons[index]["next"].image = next_img
            self.nav_buttons[index]["next"].place(x=last_column_x, y=5)
            self.nav_buttons[index]["next"].config(state="normal" if current_page < total_pages - 1 else "disabled")

            pagination_x = last_column_x - 100 
            self.archive_canvas[index].create_text(
                pagination_x,
                25,
                text=f"PAGE {current_page + 1}/{total_pages}",
                fill="white",
                font=self.app_font(-17),
                anchor="center"
            )
    
    def change_page(self, index, delta):
        """Update the current page for the current path and re-render the view."""
        path = os.path.normpath(self.current_archive_path[index])
        self.pagination_state[index][path] = self.pagination_state[index].get(path, 0) + delta
        self.render_archive_view(index)

    def _fullscreen_archive_index(self):
        """Return the stream index if in fullscreen archive mode, else None (for keyboard shortcuts)."""
        if self.is_fullscreen and self.fullscreen_index is not None:
            idx = self.fullscreen_index
            if self.is_archive_mode[idx]:
                return idx
        return None

    def archive_change_page_shortcut(self, delta):
        """Page Up/Down: change page in the fullscreen archive browser (not while a clip plays)."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        if self.current_archive_path[idx] and self.media_players[idx] is None:
            self.change_page(idx, delta)

    def archive_go_back_shortcut(self):
        """Backspace: go back one level in the archive browser (or stop playback) for the fullscreen stream."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        self.go_back(idx)

    def open_folder(self, index, path):
        self.visited_folders[index].add(os.path.normpath(path))
        self.current_archive_path[index] = path
        path = os.path.normpath(path)
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0
        self.render_archive_view(index)

    def _reset_clip_buttons(self, index):
        """Null out per-stream clip-playback button refs and reset playback flags."""
        self._stop_hover_poll(index)
        self.exit_buttons[index]   = None
        self.pause_buttons[index]  = None
        self.ff_buttons[index]     = None
        self.replay_buttons[index] = None
        self.rewind_buttons[index] = None
        self.audio_buttons[index]  = None
        self.video_ended[index]    = False

    def _clip_control_widgets(self, index):
        """Clip-control buttons for the current mode. Event mode shows only exit (skips to the next queued
        clip) and audio; seek/pause/replay would desync a cam from the event's shared timeline."""
        if self.event_mode:
            return [
                self.exit_buttons[index],
                self.audio_buttons[index],
            ]
        return [
            self.exit_buttons[index],
            self.rewind_buttons[index],
            self.pause_buttons[index],
            self.ff_buttons[index],
            self.replay_buttons[index],
            self.audio_buttons[index],
        ]

    CONTROL_POSITIONS = [
        "top-left", "top-center", "top-right",
        "center",
        "bottom-left", "bottom-center", "bottom-right",
    ]

    def _clip_control_positions(self, n_buttons=6):
        """place() kwargs for n_buttons clip controls, per controls_position. n_buttons matches the shown
        set (event mode shows 2) so the strip has no empty gap."""
        btn_w, btn_h = 40, 40
        strip_w = n_buttons * btn_w
        margin = 10
        pos = getattr(self, 'controls_position', 'top-left')

        result = []
        for bi in range(n_buttons):
            dx = bi * btn_w

            if pos == "top-left":
                kw = dict(relx=0.0, rely=0.0,
                          x=margin + dx, y=margin, anchor="nw")
            elif pos == "top-center":
                kw = dict(relx=0.5, rely=0.0,
                          x=-strip_w // 2 + dx, y=margin, anchor="nw")
            elif pos == "top-right":
                kw = dict(relx=1.0, rely=0.0,
                          x=-(margin + strip_w) + dx, y=margin, anchor="nw")
            elif pos == "center":
                kw = dict(relx=0.5, rely=0.5,
                          x=-strip_w // 2 + dx, y=-btn_h // 2, anchor="nw")
            elif pos == "bottom-left":
                kw = dict(relx=0.0, rely=1.0,
                          x=margin + dx, y=-(margin + btn_h), anchor="nw")
            elif pos == "bottom-center":
                kw = dict(relx=0.5, rely=1.0,
                          x=-strip_w // 2 + dx, y=-(margin + btn_h), anchor="nw")
            elif pos == "bottom-right":
                kw = dict(relx=1.0, rely=1.0,
                          x=-(margin + strip_w) + dx, y=-(margin + btn_h), anchor="nw")
            else:
                kw = dict(relx=0.0, rely=0.0,
                          x=margin + dx, y=margin, anchor="nw")
            result.append(kw)
        return result

    def _all_clip_control_widgets(self, index):
        """All 6 clip-control buttons regardless of mode, so hiding can't strand a button placed under another mode."""
        return [
            self.exit_buttons[index],
            self.rewind_buttons[index],
            self.pause_buttons[index],
            self.ff_buttons[index],
            self.replay_buttons[index],
            self.audio_buttons[index],
        ]

    def _set_clip_controls_visible(self, index, visible):
        """Show/hide a stream's clip-control buttons (no-op if unchanged)."""
        if self._clip_controls_visible[index] == visible:
            return
        self._clip_controls_visible[index] = visible
        if visible:
            widgets = self._clip_control_widgets(index)
            for widget, kw in zip(widgets, self._clip_control_positions(len(widgets))):
                if widget:
                    widget.place(**kw)
        else:
            for widget in self._all_clip_control_widgets(index):
                if widget:
                    widget.place_forget()

    def _stop_hover_poll(self, index):
        """Cancel the hover poll loop for a stream, if running."""
        poll_id = self._hover_poll_ids[index]
        if poll_id:
            try:
                self.root.after_cancel(poll_id)
            except Exception:
                pass
            self._hover_poll_ids[index] = None
        self._clip_controls_visible[index] = False

    def _start_hover_poll(self, index):
        """Poll the pointer to show/hide the clip-control strip (the Windows VLC HWND swallows Tk hover events).
        Display-only; all actions are real Tk buttons."""
        self._stop_hover_poll(index)

        def poll():
            if not self.running or not self.media_players[index]:
                self._set_clip_controls_visible(index, False)
                self._hover_poll_ids[index] = None
                return

            try:
                px, py = self.root.winfo_pointerxy()
                label = self.labels[index]
                x0 = label.winfo_rootx()
                y0 = label.winfo_rooty()
                inside = (x0 <= px <= x0 + label.winfo_width() and
                          y0 <= py <= y0 + label.winfo_height())
            except Exception:
                inside = False

            self._set_clip_controls_visible(index, inside)

            interval = 60 if sys.platform.startswith('win') else 150
            self._hover_poll_ids[index] = self.root.after(interval, poll)

        poll()

    @staticmethod
    def _path_within(child, root):
        """True if child is root or inside it (component-wise: /data/archive2 is not inside /data/archive)."""
        try:
            child = os.path.normpath(child)
            root = os.path.normpath(root)
            common = os.path.commonpath([child, root])
        except ValueError:
            return False
        return os.path.normcase(common) == os.path.normcase(root)

    def go_back(self, index):
        # In event mode the exit button skips to the cam's next queued clip.
        if self.event_mode:
            self._event_clips_skipped = True
            self._on_event_clip_ended(index)
            return

        if self.watch_progress_dirty:
            self.save_watch_progress()

        # Reset playback state only (no widget destruction yet). Speed is global, so it isn't reset.
        self.is_paused[index] = False
        self._play_tokens[index] += 1  # cancel pending clip play

        # Teardown, widget destruction and navigation run in one background thread under
        # archive_entry_locks[index], VLC first (BadWindow on Linux, deadlock on Windows otherwise).
        def _go_back_locked():
            with self.archive_entry_locks[index]:
                self.cleanup_stream(index)

                def _navigate():
                    # VLC has released the player, so destroying vlc_frame and the controls is now safe.
                    for widget in self.labels[index].winfo_children():
                        widget.destroy()
                    self._reset_clip_buttons(index)

                    if not self.current_archive_path[index]:
                        logging.warning(f"Stream {index}: No current archive path, exiting archive mode")
                        self.toggle_archive_mode(index)
                        return

                    current_path = os.path.normpath(self.current_archive_path[index])
                    archive_dir = os.path.normpath(self.archive_dir)
                    archive_root = os.path.normpath(os.path.join(archive_dir, f"cam{index+1}"))

                    if current_path == archive_root and not current_path.endswith(".mp4"):
                        logging.info(f"Stream {index}: At archive root {current_path}, exiting archive mode")
                        self.toggle_archive_mode(index)
                        return

                    if current_path.endswith(".mp4"):
                        parent_path = os.path.dirname(current_path)
                    else:
                        parent_path = os.path.dirname(current_path)

                    # Reset pagination when leaving a video listing
                    if current_path != archive_root and not current_path.endswith(".mp4"):
                        self.pagination_state[index][current_path] = 0
                        logging.info(f"Stream {index}: Reset pagination for video listing view {current_path} to page 1")

                    if not self._path_within(parent_path, archive_dir) or parent_path == archive_dir:
                        logging.info(f"Stream {index}: Reached archive_dir boundary, exiting archive mode")
                        self.toggle_archive_mode(index)
                        return
                    else:
                        self.current_archive_path[index] = parent_path
                        norm_parent_path = os.path.normpath(parent_path)
                        if norm_parent_path not in self.pagination_state[index]:
                            self.pagination_state[index][norm_parent_path] = 0
                        logging.info(f"Stream {index}: Navigated back to {self.current_archive_path[index]}")

                    self.is_archive_mode[index] = True
                    self.labels[index].pack_forget()
                    self.archive_canvas[index].pack(fill="both", expand=True)
                    self.render_archive_view(index)

                self.root.after(0, _navigate)

        threading.Thread(target=_go_back_locked, daemon=True).start()

    def _events_dir(self):
        config_dir = os.path.dirname(self.config_file)
        return os.path.join(config_dir, "events")

    def _events_path(self, date):

        return os.path.join(self._events_dir(), str(date.year), date.strftime("%Y%m%d") + ".json")

    def _scan_events_for_date(self, date, exclude_paths=None):
        # Scan archive clips for date and cluster them into events.
        from datetime import timedelta

        date_str = date.strftime("%Y-%m-%d")
        min_duration_s = 10          # Ignore clips shorter than this
        gap_limit = timedelta(minutes=self.event_overlap_window_mins)
        exclude_paths = exclude_paths or set()

        # Trailing _<detectiontype> is optional.
        clip_re = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_(\d+)m-(\d+)s(?:_([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*))?\.mp4$")
        all_clips = []  # (start_dt, end_dt, cam_index, abs_path, detection_type)

        for cam_idx in range(4):
            if not self.ips[cam_idx]:
                continue
            day_folder = os.path.join(self.archive_dir, f"cam{cam_idx + 1}", date_str)
            if not os.path.isdir(day_folder):
                continue
            try:
                entries = os.listdir(day_folder)
            except OSError as e:
                logging.warning(f"Events scan: cannot list {day_folder}: {e}")
                continue
            for fname in entries:
                m = clip_re.match(fname)
                if not m:
                    continue
                clip_path = os.path.join(day_folder, fname)
                if clip_path in exclude_paths:
                    continue
                d_str, t_str, mins_str, secs_str, raw_type = m.groups()
                try:
                    start_dt = datetime.strptime(f"{d_str} {t_str.replace('-', ':')}", "%Y-%m-%d %H:%M:%S")
                    duration_s = int(mins_str) * 60 + int(secs_str)
                except (ValueError, TypeError):
                    continue
                if duration_s < min_duration_s:
                    continue
                end_dt = start_dt + timedelta(seconds=duration_s)
                detection_type = self.normalize_detection_type(raw_type)
                all_clips.append((start_dt, end_dt, cam_idx, clip_path, detection_type))

        if not all_clips:
            return []

        all_clips.sort(key=lambda c: c[0])

        # Cluster clips into events by gap
        events = []
        current_cluster = []     # list of clip tuples in this event
        window_end = None        # furthest end time seen so far in this cluster

        def _finalise_cluster(cluster):
            """Convert a raw clip cluster into the event dict schema."""
            cams_data = {}
            for cam_i in range(4):
                cams_data[str(cam_i + 1)] = {"enabled": False, "clips": []}

            e_start = cluster[0][0]
            e_end   = cluster[0][1]
            detection_types = set()
            for s_dt, e_dt, ci, path, detection_type in cluster:
                if s_dt < e_start:
                    e_start = s_dt
                if e_dt > e_end:
                    e_end = e_dt
                cams_data[str(ci + 1)]["clips"].append({
                    "path":           path,
                    "clip_start":     s_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "clip_end":       e_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "detection_type": detection_type,
                })
                cams_data[str(ci + 1)]["enabled"] = True
                if detection_type:
                    detection_types.add(detection_type)

            for cd in cams_data.values():
                cd["clips"].sort(key=lambda c: c["clip_start"])

            return {
                "id":               e_start.strftime("%Y%m%d_%H%M%S"),
                "start":            e_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end":              e_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "played":           False,
                "cams":             cams_data,
                "detection_types":  sorted(detection_types),
            }

        for clip in all_clips:
            s_dt, e_dt, ci, path, detection_type = clip
            if window_end is None or s_dt > window_end + gap_limit:
                if current_cluster:
                    events.append(_finalise_cluster(current_cluster))
                current_cluster = [clip]
                window_end = e_dt
            else:
                current_cluster.append(clip)
                if e_dt > window_end:
                    window_end = e_dt

        if current_cluster:
            events.append(_finalise_cluster(current_cluster))

        return events

    def _events_fingerprint(self, date):
        """Cheap change detector: per-cam day-folder mtimes plus the clustering setting."""
        date_str = date.strftime("%Y-%m-%d")
        mtimes = {}
        for cam_idx in range(4):
            if not self.ips[cam_idx]:
                continue
            folder = os.path.join(self.archive_dir, f"cam{cam_idx + 1}", date_str)
            try:
                mtimes[str(cam_idx + 1)] = os.stat(folder).st_mtime_ns
            except OSError:
                mtimes[str(cam_idx + 1)] = None
        return {"mtimes": mtimes, "overlap": self.event_overlap_window_mins}

    def _read_events_payload(self, date):
        try:
            with open(self._events_path(date)) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _events_cache_trusted(self, payload, fingerprint):
        """Trust the cache only if the scan ran well after the newest folder mtime (coarse-mtime filesystems
        like Samba can miss same-tick additions); otherwise rescan."""
        scanned_ns = payload.get("scanned_at_ns")
        if not isinstance(scanned_ns, int):
            return False
        newest = max(
            (v for v in fingerprint["mtimes"].values() if isinstance(v, int)),
            default=0
        )
        return scanned_ns - newest > self.EVENTS_CACHE_TRUST_MARGIN_NS

    def _load_or_scan_events(self, date):
        # Taken before the fingerprint and scan, so mid-scan arrivals can only force a rescan, never go stale.
        scan_ns = time.time_ns()
        fingerprint = self._events_fingerprint(date)
        payload = self._read_events_payload(date)

        if (payload
                and payload.get("fingerprint") == fingerprint
                and self._events_cache_trusted(payload, fingerprint)):
            return payload.get("events", [])

        deleted = set((payload or {}).get("deleted_clips", []))
        events = self._scan_events_for_date(date, exclude_paths=deleted)

        # Carry over user state (played flag, camera checkboxes) by event id.
        if payload:
            cached = {e.get("id"): e for e in payload.get("events", []) if isinstance(e, dict)}
            for ev in events:
                old = cached.get(ev["id"])
                if not old:
                    continue
                if old.get("played"):
                    ev["played"] = True
                elif old.get("partial"):
                    ev["partial"] = True
                for cam_key, cam_data in ev["cams"].items():
                    old_cam = old.get("cams", {}).get(cam_key)
                    # Keep the old checkbox only if that cam already had clips; newly added cams default to enabled.
                    if old_cam and old_cam.get("clips") and cam_data.get("clips"):
                        cam_data["enabled"] = bool(old_cam.get("enabled", cam_data["enabled"]))

        self._save_events_json(date, events, fingerprint=fingerprint, scan_ns=scan_ns)
        return events

    def _save_events_json(self, date, events, fingerprint=None, scan_ns=None, deleted_add=None):
        """Persist events. Fingerprint, scan time and deleted_clips carry over unless supplied."""
        json_path = self._events_path(date)
        try:
            existing = self._read_events_payload(date) or {}
            deleted = set(existing.get("deleted_clips", []))
            if deleted_add:
                deleted.update(deleted_add)
            payload = {
                "date":          date.strftime("%Y-%m-%d"),
                "scanned_at":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "scanned_at_ns": scan_ns if scan_ns is not None else existing.get("scanned_at_ns"),
                "fingerprint":   fingerprint if fingerprint is not None else existing.get("fingerprint"),
                "deleted_clips": sorted(deleted),
                "events":        events,
            }
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
            tmp = json_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, json_path)
        except Exception as e:
            logging.warning(f"Events: failed to write cache {json_path}: {e}")

    def _event_detection_types(self, event):
        """Sorted detection-type ids in an event."""
        return event.get("detection_types", [])

    def toggle_event_mode(self):
        """Toggle event mode (overlay + coordinated playback) vs live. Mirrors the Archive button:
        closes Events if active, closes Archive first if active, otherwise opens Events."""
        if self.event_mode:
            self._exit_event_mode()   # defers itself if a transition is in flight
            return

        if self._modes_busy():
            logging.info("Events: ignored, a mode transition is still in progress")
            return

        if any(self.is_archive_mode[i] for i in range(4)):
            for i in range(4):
                if self.is_archive_mode[i] and not self.archive_transitioning[i]:
                    self.toggle_archive_mode(i, rebuild_ui=False, restart_stream=False)
                    logging.info(f"Stream {i}: Exited archive mode to switch to Events")

        # Drop back to grid first: the listing centres over the grid and multi-cam events need all four panels.
        if self.is_fullscreen:
            self.is_fullscreen = False
            self.fullscreen_index = -1
            self.update_layout()

        self._open_event_overlay()

    def _exit_event_mode(self, restart_streams=True):
        if restart_streams and self._modes_busy():
            # A teardown or archive exit is still running; defer so the restart can't overlap it.
            self._defer_exit_event_mode()
            return
        self.event_mode = False

        for _pending in self._pending_event_afters:
            try:
                self.root.after_cancel(_pending["after_id"])
            except Exception:
                pass
        self._pending_event_afters.clear()

        if getattr(self, '_event_entered_fullscreen', False):
            self.is_fullscreen = False
            self.fullscreen_index = -1
            self._event_entered_fullscreen = False

        # VLC teardown runs before widget destruction (BadWindow on Linux, deadlock on Windows otherwise),
        # in the background under archive_entry_locks (see go_back).
        archive_cams_to_teardown = [i for i in list(self.event_active_cams) if self.is_archive_mode[i]]

        # Flip is_archive_mode now so the synchronous build_config_panel() below doesn't show the archive
        # button as active. Teardown is folded into _restart_one so it always precedes the restart.
        for i in archive_cams_to_teardown:
            self._play_tokens[i] += 1
            self.is_archive_mode[i] = False
            self.pending_vlc_teardown.add(i)

        self.event_active_cams  = set()
        self.event_done_cams    = set()
        self.event_clip_queues  = [[] for _ in range(4)]
        self.current_playing_event = None

        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()
        self.event_overlay = None

        # Reflect the state flips now so the UI doesn't sit on a fullscreen cam during the restart.
        self.update_layout()
        self.build_config_panel()

        if not restart_streams:
            # Caller handles its own stream state; don't restart live streams just to tear them down.
            logging.info("Event mode exited without restarting live streams (mode switch)")
            self.update_label_bindings()
            return

        cams_to_restart = [i for i in range(4) if self.ips[i] and self.streams[i]]
        if cams_to_restart:
            # Hold the mode buttons and handlers for the whole teardown + restart.
            self._begin_mode_switch()

            archive_teardown_set = set(archive_cams_to_teardown)

            def _restart_all():
                def _restart_one(idx):
                    # Teardown runs inline right before the restart: with separate threads the lock gave exclusion
                    # but no ordering, leaving a stale clip player alive over the new live stream.
                    with self.archive_entry_locks[idx]:
                        if idx in archive_teardown_set:
                            self._cleanup_archive_mode_vlc(idx)
                            self._clear_pending_teardown(idx)
                            # Wait for the Tk-side repack before reattaching the live stream.
                            self._tk_wait(lambda i=idx: self._cleanup_archive_mode_ui(i))
                        self.try_init_stream_with_retries(idx)

                threads = [
                    threading.Thread(
                        target=_restart_one,
                        args=(i,),
                        daemon=True
                    )
                    for i in cams_to_restart
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                def _on_done():
                    self._end_mode_switch()
                    self.build_config_panel()
                self.root.after(0, _on_done)

            threading.Thread(target=_restart_all, daemon=True).start()
        else:
            self.build_config_panel()

        self.update_label_bindings()

    def _open_event_overlay(self, date=None):
        """Build and show the event listing overlay for date (default: today)."""
        from datetime import date as _date

        if date is None:
            date = _date.today()

        self.event_mode = True
        self.build_config_panel()

        # Teardown runs on a background thread under archive_entry_locks: VLC before widget reset, and the
        # "Cam N" label only after the player stops painting (it would otherwise overwrite the text).
        teardown_indices = list(range(4))
        was_archive_mode = {i: self.is_archive_mode[i] for i in teardown_indices}

        # Stop any in-flight init (downgrade, reconnect, wake, startup) before its player is released.
        for i in teardown_indices:
            self._request_init_abort(i)

        def _teardown_all(idxs):
            try:
                for i in idxs:
                    with self.archive_entry_locks[i]:
                        self._wait_init_idle(i)
                        if self.media_players[i]:
                            self.cleanup_stream(i)
                        # Wait on each Tk-side step so the busy flag is honest when released.
                        if was_archive_mode[i]:
                            self._tk_wait(lambda idx=i: self._cleanup_archive_mode_ui(idx))
                        self._tk_wait(lambda idx=i: self._set_event_blank_label(idx))
            finally:
                self._post_to_tk(self._end_mode_switch)
        self._begin_mode_switch()
        threading.Thread(target=_teardown_all, args=(teardown_indices,), daemon=True).start()

        # Clear label click bindings so clicking a blank quadrant can't trigger fullscreen.
        self.update_label_bindings()

        events = self._load_or_scan_events(date)

        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()

        overlay = tk.Frame(
            self.grid_frame, bg="#1a1a1a",
            highlightbackground="#444444", highlightthickness=1
        )
        self.event_overlay = overlay

        self.root.update_idletasks()
        gw = self.grid_frame.winfo_width()
        gh = self.grid_frame.winfo_height()
        ow = min(820, max(500, gw - 40))
        oh = min(500, max(300, gh - 40))

        # Saved so _start_event_playback and _on_event_clip_ended can re-show the panel.
        self._event_overlay_size = (ow, oh)

        def _place_overlay():
            overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
            overlay.lift()

        # ---- Header bar ----
        hdr = tk.Frame(overlay, bg="#111111")
        hdr.pack(fill="x")

        tk.Label(hdr, text="Events", bg="#111111", fg="white",
                 font=self.app_font(13, "bold")).pack(side="left", padx=12, pady=8)

        nav_frame = tk.Frame(hdr, bg="#111111")
        nav_frame.pack(side="left", expand=True)

        prev_btn = tk.Button(nav_frame, text="◀", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        prev_btn.pack(side="left", padx=4)

        day_label_var = tk.StringVar(value=date.strftime("%A, %d %b %Y"))
        tk.Label(nav_frame, textvariable=day_label_var, bg="#111111", fg="#cccccc",
                 font=self.app_font(11), width=22).pack(side="left")

        next_btn = tk.Button(nav_frame, text="▶", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        next_btn.pack(side="left", padx=4)

        close_btn = tk.Button(hdr, text="✕", bg="#111111", fg="#aaaaaa", bd=0,
                              activebackground="#333333", cursor="hand2",
                              font=self.app_font(11),
                              command=self._exit_event_mode)
        close_btn.pack(side="right", padx=10, pady=6)

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Filter bar ----
        filter_bar = tk.Frame(overlay, bg="#1a1a1a")
        filter_bar.pack(fill="x")

        tk.Label(filter_bar, text="Filter:", bg="#1a1a1a", fg="#888888",
                 font=self.app_font(9)).pack(side="left", padx=(12, 6), pady=4)

        # Selected canonical ids; empty means All Types (populated from the default by _refresh_filter_options).
        filter_selected = []

        def _label_for_type(canonical_id):
            return self.detection_type_label(canonical_id)

        def _filter_summary():
            if not filter_selected:
                return self.ALL_TYPES_LABEL
            return ", ".join(_label_for_type(t) for t in filter_selected)

        filter_button = tk.Button(
            filter_bar, text=self.ALL_TYPES_LABEL, font=self.app_font(9),
            anchor="w", relief="flat", bd=1, padx=8, width=18,
            bg="#2a2a2a", fg="white", activebackground="#2a2a2a",
        )
        filter_button.pack(side="left", pady=4)

        def _open_filter_popup():
            present_ids = filter_button._present_ids if hasattr(filter_button, "_present_ids") else []
            popup = tk.Toplevel(self.root)
            popup.transient(self.root)
            popup.resizable(False, False)
            popup.title("Filter Events")

            check_vars = {
                cid: tk.BooleanVar(value=cid in filter_selected) for cid in present_ids
            }

            tk.Label(popup, text="Show these types:", font=self.app_font(9)).pack(
                anchor="w", padx=10, pady=(10, 4)
            )
            if not present_ids:
                tk.Label(popup, text="No detection types for this day.", font=self.app_font(9), fg="#888888").pack(
                    anchor="w", padx=16, pady=1
                )
            for cid in present_ids:
                ttk.Checkbutton(
                    popup, text=_label_for_type(cid), variable=check_vars[cid]
                ).pack(anchor="w", padx=16, pady=1)

            def _apply():
                filter_selected[:] = [cid for cid, v in check_vars.items() if v.get()]
                filter_button.configure(text=_filter_summary())
                popup.destroy()
                _render_rows(_filtered_events())

            btn_row = tk.Frame(popup)
            btn_row.pack(pady=(8, 10))
            tk.Button(btn_row, text="OK", width=8, font=self.app_font(9), command=_apply).pack(side="left", padx=4)
            tk.Button(btn_row, text="Cancel", width=8, font=self.app_font(9), command=popup.destroy).pack(
                side="left", padx=4
            )

            popup.update_idletasks()
            popup.grab_set()

        filter_button.configure(command=_open_filter_popup)

        def _refresh_filter_options(evs, preserve_selection=False):
            """Recompute the types present in evs for the filter popup. Resets to the default selection, or
            with preserve_selection keeps the current one minus ids no longer present."""
            present_ids = sorted({t for ev in evs for t in self._event_detection_types(ev)})
            filter_button._present_ids = present_ids

            if preserve_selection:
                filter_selected[:] = [cid for cid in filter_selected if cid in present_ids]
            else:
                filter_selected[:] = [cid for cid in self.default_event_filter if cid in present_ids]

            filter_button.configure(text=_filter_summary())
            return present_ids

        def _filtered_events():
            if not filter_selected:
                return state["events"]
            return [
                ev for ev in state["events"]
                if any(t in filter_selected for t in self._event_detection_types(ev))
            ]

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Scrollable event rows ----
        list_outer = tk.Frame(overlay, bg="#1a1a1a")
        list_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_outer, bg="#1a1a1a", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_scroll_linux(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        def _on_scroll_win(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def _bind_scroll(widget):
            widget.bind("<Button-4>",    _on_scroll_linux)
            widget.bind("<Button-5>",    _on_scroll_linux)
            widget.bind("<MouseWheel>",  _on_scroll_win)
            for child in widget.winfo_children():
                _bind_scroll(child)

        _bind_scroll(canvas)
        _bind_scroll(list_outer)

        state = {"date": date, "events": events}

        # ---- Rows are plain canvas items, with one canvas-level click binding (per-row widgets were
        # slow under XWayland and leaked a trace variable per checkbox) ----
        ROW_H = 48
        ICON = 40
        CB_CELL = 48
        CB_HALF = 9      # half-size of a camera checkbox square
        state["actions"] = {}
        state["drawn_w"] = 0

        class _PlayedIcon:
            """Stand-in for the old eye Label: playback code only calls winfo_exists() and configure(image=...)."""
            def __init__(self, item):
                self.item = item

            def winfo_exists(self):
                try:
                    return bool(canvas.winfo_exists()) and canvas.type(self.item) is not None
                except tk.TclError:
                    return False

            def configure(self, **kw):
                if "image" in kw:
                    canvas.itemconfigure(self.item, image=kw["image"])

        def _paint_checkbox(box, tick, on, bg):
            canvas.itemconfigure(box, fill="#ffffff" if on else "#f0f0f0", outline="#000000")
            canvas.itemconfigure(tick, state="normal" if on else "hidden")

        def _render_rows(evs):
            canvas.delete("evrow")
            actions = {}
            state["actions"] = actions

            W = canvas.winfo_width()
            if W <= 1:
                # Not mapped yet: use the overlay size; <Configure> redraws at the real width.
                W = max(300, getattr(self, "_event_overlay_size", (820, 500))[0] - 22)
            state["drawn_w"] = W

            font_spec = self.app_font(10)
            font_obj = tkfont.Font(root=self.root, font=font_spec)

            if not evs:
                canvas.create_text(
                    W // 2, 40, text="No events found for this day.", fill="#666666",
                    font=self.app_font(10, "italic"), tags=("evrow",)
                )
                canvas.configure(scrollregion=(0, 0, W, 80))
                return

            def _fit_text(text, max_px):
                if max_px <= 0:
                    return ""
                if font_obj.measure(text) <= max_px:
                    return text
                ell = "\u2026"
                lo, hi = 0, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if font_obj.measure(text[:mid] + ell) <= max_px:
                        lo = mid
                    else:
                        hi = mid - 1
                return text[:lo] + ell if lo > 0 else ""

            show_dl = bool(getattr(self, "events_show_download", False))
            show_del = bool(getattr(self, "events_show_delete", False))

            # Left: play [download] [delete]; then time and detection types.
            x = 6
            play_x = x
            x += ICON + 2
            dl_x = None
            if show_dl:
                dl_x = x
                x += ICON + 2
            del_x = None
            if show_del:
                del_x = x
                x += ICON + 2
            time_x = x + 8
            type_x = time_x + font_obj.measure("00:00\u201300:00") + 24

            # Right: four camera checkboxes, then the watched eye.
            eye_cx = W - 8 - ICON // 2
            cb_right = W - 8 - ICON - 6
            cb_cx = {ci: cb_right - CB_CELL * (4 - ci) - CB_CELL // 2 for ci in range(1, 5)}
            type_max = (cb_right - CB_CELL * 4) - type_x - 8

            for ev_idx, ev in enumerate(evs):
                y0 = ev_idx * ROW_H
                cy = y0 + ROW_H // 2
                row_bg = "#1e1e1e" if ev_idx % 2 == 0 else "#232323"
                canvas.create_rectangle(0, y0, W, y0 + ROW_H, fill=row_bg, outline="", tags=("evrow",))
                canvas.create_line(0, y0 + ROW_H - 1, W, y0 + ROW_H - 1, fill="#2f2f2f", tags=("evrow",))

                # Always create the watched item (blank if unplayed) so playback can switch its image.
                if ev.get("played"):
                    eye_img = self.icon_cache["eye"]
                elif ev.get("partial"):
                    eye_img = self.icon_cache["eye_partial"]
                else:
                    eye_img = ""
                eye_item = canvas.create_image(
                    eye_cx, cy, image=eye_img, tags=("evrow",)
                )
                played_icon = _PlayedIcon(eye_item)

                def _play(event_ref=ev, p_icon=played_icon):
                    enabled_cams = [
                        int(ck) - 1
                        for ck, cd in event_ref["cams"].items()
                        if cd.get("enabled") and cd.get("clips")
                    ]
                    if not enabled_cams:
                        messagebox.showwarning(
                            "No Cameras Selected",
                            "Enable at least one camera checkbox before playing.",
                            parent=self.root
                        )
                        return
                    overlay.place_forget()
                    self._start_event_playback(event_ref, p_icon, state["date"], state["events"])

                def _download(event_ref=ev):
                    self._download_event_clips(event_ref)

                def _delete(ev_ref=ev):
                    if messagebox.askyesno(
                        "Delete Event",
                        "Remove this event?",
                        parent=self.root
                    ):
                        paths = [c["path"] for cd in ev_ref["cams"].values()
                                 for c in cd.get("clips", []) if c.get("path")]
                        state["events"][:] = [e for e in state["events"] if e is not ev_ref]
                        self._save_events_json(state["date"], state["events"], deleted_add=paths)
                        _refresh_filter_options(state["events"], preserve_selection=True)
                        _render_rows(_filtered_events())

                canvas.create_image(
                    play_x, cy, image=self.icon_cache["play"], anchor="w",
                    tags=("evrow", f"play:{ev_idx}")
                )
                actions[f"play:{ev_idx}"] = _play
                if show_dl:
                    canvas.create_image(
                        dl_x, cy, image=self.icon_cache["download"], anchor="w",
                        tags=("evrow", f"dl:{ev_idx}")
                    )
                    actions[f"dl:{ev_idx}"] = _download
                if show_del:
                    canvas.create_image(
                        del_x, cy, image=self.icon_cache["delete"], anchor="w",
                        tags=("evrow", f"del:{ev_idx}")
                    )
                    actions[f"del:{ev_idx}"] = _delete

                try:
                    s = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%S")
                    e = datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M:%S")
                    time_txt = f"{s.strftime('%H:%M')}\u2013{e.strftime('%H:%M')}"
                except Exception:
                    time_txt = ev.get("start", "?")[:16]
                canvas.create_text(
                    time_x, cy, text=time_txt, fill="white", font=font_spec,
                    anchor="w", tags=("evrow",)
                )

                types_present = self._event_detection_types(ev)
                label_txt = ", ".join(self.detection_type_label(t) for t in types_present) if types_present else ""
                canvas.create_text(
                    type_x, cy, text=_fit_text(label_txt, type_max), fill="white",
                    font=font_spec, anchor="w", tags=("evrow",)
                )

                for ci in range(1, 5):
                    cam_key = str(ci)
                    
                    if cam_key not in ev["cams"]:
                        ev["cams"][cam_key] = {"enabled": False, "clips": []}
                    cam_data = ev["cams"][cam_key]
                    
                    on = bool(cam_data.get("enabled", False))
                    cx = cb_cx[ci]
                    tag = f"cb:{ev_idx}:{ci}"
                    
                    # Clickable if it has clips or is currently enabled.
                    has_clips = bool(cam_data.get("clips"))
                    is_clickable = has_clips or on

                    # Clickable boxes stay white even when unchecked
                    initial_bg = "#ffffff" if is_clickable else row_bg

                    box = canvas.create_rectangle(
                        cx - CB_HALF, cy - CB_HALF, cx + CB_HALF, cy + CB_HALF,
                        outline="#888888", fill=initial_bg,
                        tags=("evrow", tag)
                    )
                    
                    tick = canvas.create_line(
                        cx - 5, cy, cx - 1, cy + 4, cx + 5, cy - 4,
                        fill="#111111", width=2, state=("normal" if on else "hidden"),
                        tags=("evrow", tag)
                    )

                    # Pass "#ffffff" so an unticked clickable box stays white instead of reverting to row_bg.
                    def _toggle(ck=cam_key, event_ref=ev, b=box, t=tick, bg="#ffffff", allowed=is_clickable):
                        if not allowed:
                            return
                            
                        cd = event_ref["cams"][ck]
                        cd["enabled"] = not cd.get("enabled", False)
                        _paint_checkbox(b, t, cd["enabled"], bg)
                        self._save_events_json(state["date"], state["events"])

                    actions[tag] = _toggle

            canvas.configure(scrollregion=(0, 0, W, len(evs) * ROW_H))

        def _current_action():
            for tag in canvas.gettags("current"):
                fn = state["actions"].get(tag)
                if fn is not None:
                    return fn
            return None

        def _on_canvas_click(event):
            fn = _current_action()
            if fn is not None:
                fn()

        def _on_canvas_motion(event):
            canvas.configure(cursor="hand2" if _current_action() is not None else "")

        def _on_canvas_resize(event):
            if event.width > 1 and event.width != state["drawn_w"]:
                _render_rows(_filtered_events())

        canvas.bind("<Button-1>", _on_canvas_click, add="+")
        canvas.bind("<Motion>", _on_canvas_motion, add="+")
        canvas.bind("<Leave>", lambda e: canvas.configure(cursor=""), add="+")
        canvas.bind("<Configure>", _on_canvas_resize, add="+")

        _refresh_filter_options(events)
        _render_rows(_filtered_events())

        def _navigate(delta):
            from datetime import timedelta as _td2
            new_date = state["date"] + _td2(days=delta)
            if new_date > _date.today():
                return  # Can't navigate to the future
            state["date"]   = new_date
            state["events"] = self._load_or_scan_events(new_date)
            day_label_var.set(new_date.strftime("%A, %d %b %Y"))
            next_btn.configure(state="normal" if new_date < _date.today() else "disabled")
            _refresh_filter_options(state["events"])
            _render_rows(_filtered_events())

        prev_btn.configure(command=lambda: _navigate(-1))
        next_btn.configure(command=lambda: _navigate(+1))
        next_btn.configure(state="disabled" if date >= _date.today() else "normal")

        _place_overlay()

    def _download_event_clips(self, event):
        """Copy clips from the event's enabled cameras to a chosen folder, prefixed "camN_" so
        overlapping events from different cams can't overwrite each other."""
        clip_sources = []  # list of (cam_index, source_path)
        for cam_key, cam_data in event.get("cams", {}).items():
            if not cam_data.get("enabled"):
                continue
            for clip in cam_data.get("clips", []):
                path = clip.get("path")
                if path:
                    clip_sources.append((int(cam_key), path))

        if not clip_sources:
            messagebox.showwarning(
                "No Cameras Selected",
                "Enable at least one camera checkbox before downloading.",
                parent=self.root
            )
            return

        dest_dir = filedialog.askdirectory(
            title="Choose folder to save event clips",
            parent=self.root
        )
        if not dest_dir:
            return  # User cancelled

        threading.Thread(
            target=self._copy_event_clips_thread,
            args=(clip_sources, dest_dir),
            daemon=True
        ).start()

    def _copy_event_clips_thread(self, clip_sources, dest_dir):
        """Worker: copy each (cam_index, source_path) to dest_dir as "camN_<name>", tolerating individual
        failures, then report a summary on the Tk thread."""
        copied = 0
        failed = []

        for cam_index, source_path in clip_sources:
            try:
                original_name = os.path.basename(source_path)
                dest_name = f"cam{cam_index}_{original_name}"
                dest_path = os.path.join(dest_dir, dest_name)
                shutil.copy2(source_path, dest_path)
                copied += 1
                logging.info(f"Event download: copied {source_path} -> {dest_path}")
            except Exception as e:
                logging.error(f"Event download: failed to copy {source_path}: {e}")
                failed.append(os.path.basename(source_path))

        def _report():
            if failed:
                failed_list = "\n".join(failed[:10])
                more = f"\n...and {len(failed) - 10} more" if len(failed) > 10 else ""
                messagebox.showwarning(
                    "Download Completed with Errors",
                    f"Copied {copied} of {len(clip_sources)} clip(s) to:\n{dest_dir}\n\n"
                    f"Failed to copy:\n{failed_list}{more}",
                    parent=self.root
                )
            else:
                messagebox.showinfo(
                    "Download Complete",
                    f"Copied {copied} clip(s) to:\n{dest_dir}",
                    parent=self.root
                )
        self.root.after(0, _report)

    def _transfer_archive_audio(self, index):
        # Give audio exclusively to index, muting every other unmuted archive/event stream.

        if not self.exclusive_archive_audio:
            return
        for i in range(4):
            if i == index:
                continue
            if not self.archive_audio_muted[i]:
                self.archive_audio_muted[i] = True
                if self.media_players[i]:
                    try:
                        self.media_players[i].audio_set_mute(True)
                    except Exception:
                        pass
                if self.audio_buttons[i]:
                    try:
                        self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                    except Exception:
                        pass
        self.archive_audio_muted[index] = False

    def _start_event_playback(self, event, played_label_widget, date, events_list):

        self.current_playing_event = event
        self._event_clips_skipped = False
        self._event_first_clip_started_at = None
        self.event_clip_queues  = [[] for _ in range(4)]
        self.event_active_cams  = set()
        self.event_done_cams    = set()

        event_start_dt = None
        try:
            enabled_starts = [
                datetime.strptime(cd["clips"][0]["clip_start"], "%Y-%m-%dT%H:%M:%S")
                for cd in event["cams"].values()
                if cd.get("enabled") and cd.get("clips")
            ]
            if enabled_starts:
                event_start_dt = min(enabled_starts)
        except Exception:
            event_start_dt = None

        cam_launches = []  # (ci, first_path, unscaled_delay_ms)

        for cam_key, cam_data in event["cams"].items():
            if not cam_data.get("enabled"):
                continue
            clips = cam_data.get("clips", [])
            if not clips:
                continue
            ci = int(cam_key) - 1

            queue = []
            for clip_idx, clip in enumerate(clips):
                if clip_idx == 0:
                    gap_ms = 0
                else:
                    try:
                        prev_end_dt  = datetime.strptime(clips[clip_idx - 1]["clip_end"],   "%Y-%m-%dT%H:%M:%S")
                        this_start_dt = datetime.strptime(clip["clip_start"],               "%Y-%m-%dT%H:%M:%S")
                        gap_s  = max(0.0, (this_start_dt - prev_end_dt).total_seconds())
                        gap_ms = int(gap_s * 1000)
                    except Exception:
                        gap_ms = 0
                queue.append((clip["path"], gap_ms))
            self.event_clip_queues[ci] = queue
            self.event_active_cams.add(ci)

            # Store the UNSCALED delay; it's divided by the current speed only when scheduling, so
            # cycle_speed() can reschedule a wait that's already ticking down.
            if event_start_dt is not None:
                try:
                    clip_start_dt = datetime.strptime(clips[0]["clip_start"], "%Y-%m-%dT%H:%M:%S")
                    unscaled_delay_ms = int(max(0.0, (clip_start_dt - event_start_dt).total_seconds()) * 1000)
                except Exception:
                    unscaled_delay_ms = 0
            else:
                unscaled_delay_ms = 0

            first_path, _ = self.event_clip_queues[ci].pop(0)
            cam_launches.append((ci, first_path, unscaled_delay_ms))

        if not self.event_active_cams:
            # Nothing to play: re-show the overlay
            if self.event_overlay and self.event_overlay.winfo_exists():
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()
            return

        # Refs for the completion callback
        self._event_played_label   = played_label_widget
        self._event_date_for_save  = date
        self._event_list_for_save  = events_list

        self._event_entered_fullscreen = False
        if len(self.event_active_cams) == 1:
            ci_solo = next(iter(self.event_active_cams))
            if not self.is_fullscreen:
                self.is_fullscreen = True
                self.fullscreen_index = ci_solo
                self._event_entered_fullscreen = True
                self.update_layout()
                self.build_config_panel()
        else:
            # Refresh so the back-to-listing button appears as soon as playback starts.
            self.build_config_panel()

        for ci, _, _ in cam_launches:
            if not self.is_archive_mode[ci]:
                self.is_archive_mode[ci] = True
                self.archive_canvas[ci].pack_forget()
                self.labels[ci].pack(fill="both", expand=True)

        for ci, _, _ in cam_launches:
            self.archive_audio_muted[ci] = True

        for ci, first_path, unscaled_delay_ms in cam_launches:
            if unscaled_delay_ms == 0:
                self.play_archive_video(ci, first_path)
            else:
                self._schedule_event_clip_launch(ci, first_path, unscaled_delay_ms)
              
    def _request_init_abort(self, index):
        """Ask the in-flight init (if any) to stop. Under stream_init_lock so it can't race the init's
        finally block and leave a stale abort for the next init."""
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                logging.info(f"Stream {index}: Init in progress, signalling abort")
                self.stream_abort_events[index].set()

    def _locked_cleanup_stream(self, index):
        """cleanup_stream under the per-index lock (worker threads)."""
        with self.archive_entry_locks[index]:
            self._wait_init_idle(index)
            self.cleanup_stream(index)

    def _teardown_clip_async(self, index, then=None):
        """Release a cam's clip player on a worker thread, THEN destroy its widgets and run `then` on the Tk thread
        (destroying vlc_frame first raises BadWindow on Linux; Tk-thread teardown can deadlock on Windows)."""
        self._play_tokens[index] += 1
        token = self._play_tokens[index]

        def _work():
            with self.archive_entry_locks[index]:
                self.cleanup_stream(index)

            def _ui():
                if token != self._play_tokens[index]:
                    return  # superseded by exit/back/new play
                try:
                    for widget in self.labels[index].winfo_children():
                        widget.destroy()
                except Exception as e:
                    logging.warning(f"Stream {index}: error destroying clip widgets: {e}")
                self._reset_clip_buttons(index)
                if then:
                    then()
            self.root.after(0, _ui)

        threading.Thread(target=_work, daemon=True).start()

    def _on_event_clip_ended(self, index):
        # Called on the Tk thread when an event-mode clip finishes.
        if not self.event_mode:
            return  # User exited event mode early

        if self.event_clip_queues[index]:
            next_path, unscaled_gap_ms = self.event_clip_queues[index].pop(0)

            def _next(i=index, p=next_path, gap=unscaled_gap_ms):
                if not self.event_mode:
                    return
                if gap > 0:
                    self._set_event_blank_label(i)
                    self._schedule_event_clip_launch(i, p, gap)
                    # If this was the last cam playing, remaining waits are dead air: collapse them.
                    self._collapse_dead_air()
                else:
                    self.play_archive_video(i, p)

            self._teardown_clip_async(index, _next)
        else:
            self._teardown_clip_async(index, lambda i=index: self._finish_event_cam(i))

    def _mark_event_partially_watched(self, event):
        """Mark an event partially watched (persisted as ev["partial"], dimmed eye); never overrides 'played'."""
        if not event or event.get("played"):
            return
        # Ignore brief peeks: require a minimum playback time, measured from when the first clip really
        # started (an exit during the initial stagger wait never counts).
        started = self._event_first_clip_started_at
        if started is None or time.time() - started < self.event_partial_min_watch_s:
            return
        newly_marked = not event.get("partial")
        event["partial"] = True
        if newly_marked:
            self._save_events_json(self._event_date_for_save, self._event_list_for_save)
        try:
            if self._event_played_label and self._event_played_label.winfo_exists():
                self._event_played_label.configure(image=self.icon_cache["eye_partial"])
        except Exception:
            pass

    def _finish_event_cam(self, index):
        """This cam's clips are all done (its player is already released)."""
        if not self.event_mode:
            return
        self.is_archive_mode[index] = False
        self._set_event_blank_label(index)
        self.event_done_cams.add(index)

        # If this was the last cam still playing, others waiting on future clips are staring at dead air: collapse it.
        self._collapse_dead_air()

        if self.event_done_cams >= self.event_active_cams:
            # All cams finished: mark played and restore the overlay
            if self.current_playing_event:
                if not self._event_clips_skipped:
                    # Genuine full playthrough: persist, clear any partial mark.
                    self.current_playing_event["played"] = True
                    self.current_playing_event.pop("partial", None)
                    self._save_events_json(
                        self._event_date_for_save,
                        self._event_list_for_save
                    )
                    try:
                        if self._event_played_label and self._event_played_label.winfo_exists():
                            self._event_played_label.configure(image=self.icon_cache["eye"])
                    except Exception:
                        pass
                else:
                    # A clip was skipped: partial mark (no-op if already fully watched).
                    self._mark_event_partially_watched(self.current_playing_event)

            if self.event_overlay and self.event_overlay.winfo_exists():
                # A single-cam event entered fullscreen: drop back to grid before re-showing the overlay.
                if getattr(self, '_event_entered_fullscreen', False):
                    self.is_fullscreen = False
                    self.fullscreen_index = -1
                    self._event_entered_fullscreen = False
                    self.update_layout()
                    self.build_config_panel()
                else:
                    # Refresh so the back-to-listing button is hidden again.
                    self.build_config_panel()
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()

    def _any_event_clip_playing(self):
        """True if any cam is actively playing a clip in event mode (not waiting between clips or finished).
        Used to detect dead air so gaps can be collapsed."""
        for i in (self.event_active_cams - self.event_done_cams):
            if self.media_players[i] and self.is_archive_mode[i]:
                return True
        return False

    def _collapse_dead_air(self):
        """If nothing is playing, jump to the soonest pending clip and shift every other pending clip_launch
        back by the same amount, keeping relative offsets. ramp_step entries are left alone."""
        if self._any_event_clip_playing():
            return

        launches = [p for p in self._pending_event_afters if p["kind"] == "clip_launch"]
        if not launches:
            return

        now = time.time()
        # Remaining unscaled wait per pending launch (same math as _reschedule_pending_event_afters).
        remaining = {}
        for entry in launches:
            elapsed_real_ms = (now - entry["scheduled_at"]) * 1000 * entry["scheduled_speed"]
            remaining[id(entry)] = max(0.0, entry["unscaled_ms"] - elapsed_real_ms)

        shortest_ms = min(remaining.values())
        if shortest_ms <= 0:
            # Something is already due: let it fire rather than racing a reschedule against its after().
            return

        others = [p for p in self._pending_event_afters if p["kind"] != "clip_launch"]
        self._pending_event_afters = others

        for entry in launches:
            try:
                self.root.after_cancel(entry["after_id"])
            except Exception:
                pass
            new_unscaled_ms = max(0.0, remaining[id(entry)] - shortest_ms)
            if new_unscaled_ms <= 0:
                self._fire_event_clip_launch(entry["index"], entry["path"])
            else:
                self._schedule_event_clip_launch(entry["index"], entry["path"], new_unscaled_ms)

        logging.info(
            f"Event playback: collapsed {int(shortest_ms)}ms of dead air "
            f"(no clip playing) across {len(launches)} pending clip(s)"
        )

    def _schedule_event_clip_launch(self, index, video_path, unscaled_delay_ms):
        """Schedule an event clip after unscaled_delay_ms of real time divided by the current speed. The unscaled
        value is kept in _pending_event_afters so cycle_speed() can reschedule the remaining wait on a speed change."""
        speed = max(1.0, self.global_playback_speed)
        actual_delay_ms = max(0, int(unscaled_delay_ms / speed))

        after_id = self.root.after(
            actual_delay_ms,
            lambda i=index, p=video_path: self._fire_event_clip_launch(i, p)
        )
        self._pending_event_afters.append({
            "kind": "clip_launch",
            "after_id": after_id,
            "index": index,
            "path": video_path,
            "unscaled_ms": unscaled_delay_ms,
            "scheduled_at": time.time(),
            "scheduled_speed": speed,
        })
        logging.info(
            f"Cam {index + 1}: delaying next event clip by {actual_delay_ms}ms "
            f"({unscaled_delay_ms}ms unscaled at x{speed})"
        )

    def _fire_event_clip_launch(self, index, video_path):
        """after() callback: drop the pending entry (it's firing) and start the clip."""
        self._pending_event_afters = [
            p for p in self._pending_event_afters
            if not (p["kind"] == "clip_launch" and p["index"] == index and p["path"] == video_path)
        ]
        self.play_archive_video(index, video_path)

    def _ramp_to_speed(self, index, target_speed):
        """after() callback for the brief low-rate warm-up of a fresh clip at high speed (see play_archive_video):
        steps the player up to the target speed and drops its pending entry."""
        self._pending_event_afters = [
            p for p in self._pending_event_afters
            if not (p["kind"] == "ramp_step" and p["index"] == index)
        ]
        if self.media_players[index] and self.is_archive_mode[index] and not self.is_paused[index]:
            try:
                self.media_players[index].set_rate(target_speed)
                logging.info(f"Stream {index}: Ramped up to x{target_speed} after decoder warm-up")
            except Exception as e:
                logging.error(f"Stream {index}: Error ramping up playback speed: {e}")

    def _reschedule_pending_event_afters(self):
        """Called by cycle_speed(): cancel pending clip launches and reschedule the REMAINING wait at the new speed.
        ramp_step entries are fired early instead (not proportional to clip-timeline gaps)."""
        if not self._pending_event_afters:
            return

        pending = self._pending_event_afters
        self._pending_event_afters = []

        for entry in pending:
            try:
                self.root.after_cancel(entry["after_id"])
            except Exception:
                pass

            if entry["kind"] == "ramp_step":
                # Speed changed during warm-up: apply the current global speed now.
                self._ramp_to_speed(entry["index"], self.global_playback_speed)
                continue

            # Unscaled wait already used up, in real time at the speed it was scheduled under.
            elapsed_real_ms = (time.time() - entry["scheduled_at"]) * 1000 * entry["scheduled_speed"]
            remaining_unscaled_ms = max(0, entry["unscaled_ms"] - elapsed_real_ms)

            if remaining_unscaled_ms <= 0:
                # Wait already elapsed: fire now rather than dropping the launch.
                self._fire_event_clip_launch(entry["index"], entry["path"])
            else:
                self._schedule_event_clip_launch(entry["index"], entry["path"], remaining_unscaled_ms)

    def play_archive_video(self, index, video_path):
        """Start clip playback on this quadrant. Tk-side setup happens here; anything blocking (releasing the
        previous player, creating the instance, waiting for Playing) runs in _start_archive_playback_thread."""
        self.is_archive_mode[index] = True
        self.archive_canvas[index].pack_forget()
        self.labels[index].pack(fill="both", expand=True)

        self.update_stream_label(index, "Loading...")

        # The previous clip's frame and controls are destroyed only after its player is released
        # (see _start_archive_playback_thread).
        old_children = list(self.labels[index].winfo_children())

        try:
            vlc_frame = tk.Frame(self.labels[index], bg="")
            vlc_frame.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0, anchor="nw")
            vlc_frame.configure(highlightthickness=0)
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create VLC frame: {e}")
            self.labels[index].configure(image="", text="Frame Creation Failed", fg="white")
            return

        try:
            exit_img = self.icon_cache["exit"]
            self.exit_buttons[index] = tk.Button(
                self.labels[index],
                image=exit_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.exit_buttons[index].image = exit_img

            pause_img = self.icon_cache["pause"]
            self.pause_buttons[index] = tk.Button(
                self.labels[index],
                image=pause_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.toggle_pause(index)
            )
            self.pause_buttons[index].image = pause_img

            # Fast-forward (+10s). Speed cycling is a single global control (speed_toggle_button), not per-clip.
            ff_img = self.icon_cache["speed"]
            self.ff_buttons[index] = tk.Button(
                self.labels[index],
                image=ff_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.forward_video(index)
            )
            self.ff_buttons[index].image = ff_img

            replay_img = self.icon_cache["replay"]
            self.replay_buttons[index] = tk.Button(
                self.labels[index],
                image=replay_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.replay_video(index)
            )
            self.replay_buttons[index].image = replay_img

            rewind_img = self.icon_cache["rewind"]
            self.rewind_buttons[index] = tk.Button(
                self.labels[index],
                image=rewind_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.rewind_video(index)
            )
            self.rewind_buttons[index].image = rewind_img

            # Audio toggle. Starting a clip gives it audio exclusively (most recently started clip wins),
            # in both archive browsing and event playback.
            self._transfer_archive_audio(index)
            audio_img = self.icon_cache["audio_on"]
            self.audio_buttons[index] = tk.Button(
                self.labels[index],
                image=audio_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda idx=index: self.toggle_archive_audio(idx)
            )
            self.audio_buttons[index].image = audio_img

            self.current_archive_path[index] = video_path
            # Buttons start hidden; the hover-poll loop places/forgets them.
            self.labels[index].update_idletasks()
            logging.debug(f"Stream {index}: Buttons created for video {video_path}")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create or place buttons: {e}")
            self.labels[index].configure(image="", text="Button Creation Failed", fg="white")
            vlc_frame.destroy()
            return

        # Reset playback state (speed is global, so not reset).
        self.is_paused[index] = False
        self.video_ended[index] = False

        self._play_tokens[index] += 1
        token = self._play_tokens[index]

        try:
            xid = vlc_frame.winfo_id()
        except Exception as e:
            logging.error(f"Stream {index}: Failed to get VLC frame window id: {e}")
            self.labels[index].configure(image="", text="Frame Creation Failed", fg="white")
            return

        threading.Thread(
            target=self._start_archive_playback_thread,
            args=(index, video_path, vlc_frame, xid, old_children, token),
            daemon=True
        ).start()

    def _start_archive_playback_thread(self, index, video_path, vlc_frame, xid, old_children, token):
        """Worker-thread half of play_archive_video."""

        def _fail(text):
            def _apply():
                if token != self._play_tokens[index]:
                    return
                try:
                    self.labels[index].configure(image="", text=text, fg="white")
                    vlc_frame.destroy()
                except Exception:
                    pass
            self.root.after(0, _apply)

        def _destroy_old():
            for widget in old_children:
                try:
                    widget.destroy()
                except Exception:
                    pass

        with self.archive_entry_locks[index]:
            if token != self._play_tokens[index] or not self.running:
                # Superseded before starting; the newer call owns cleaning up this call's widgets.
                return

            # Release the previous player while its window (the old frame) is still alive, only then destroy it.
            self.cleanup_stream(index)
            self.root.after(0, _destroy_old)

            instance = None
            player = None
            try:
                instance = vlc.Instance(self.build_vlc_instance_args(
                    allow_frame_drop=True
                ))
                if instance is None:
                    logging.error(f"Stream {index}: Failed to create VLC instance for archive video")
                    _fail("VLC Initialization Failed")
                    return
                self.attach_vlc_logging(instance)
                self.vlc_instances[index] = instance
                player = instance.media_player_new()
                if player is None:
                    logging.error(f"Stream {index}: Failed to create VLC media player for archive video")
                    _fail("VLC Player Creation Failed")
                    instance.release()
                    self.vlc_instances[index] = None
                    return
                self.media_players[index] = player
                media = instance.media_new(video_path)
                player.set_media(media)
                player.set_hwnd(xid) if sys.platform.startswith("win") else player.set_xwindow(xid)
                event_manager = player.event_manager()
                playing_event = threading.Event()
                event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: playing_event.set())

                if player.play() == -1:
                    logging.error(f"Stream {index}: Failed to start VLC player for archive video")
                    _fail("VLC Playback Failed")
                    player.release()
                    instance.release()
                    self.media_players[index] = None
                    self.vlc_instances[index] = None
                    return

                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if token != self._play_tokens[index] or not self.running:
                        # Superseded/cancelled while starting; whoever took over releases the player (they take this lock first).
                        return
                    if playing_event.is_set():
                        break
                    time.sleep(0.1)
                else:
                    logging.error(f"Stream {index}: Archive video failed to start within 5s")
                    _fail("Playback Timeout")
                    player.release()
                    instance.release()
                    self.media_players[index] = None
                    self.vlc_instances[index] = None
                    return

                self.root.after(
                    0, lambda: self._finish_archive_playback_start(index, player, video_path, token)
                )
            except Exception as e:
                logging.error(f"Stream {index}: Failed to start archive video playback: {e}")
                _fail("Playback Failed")
                self.cleanup_stream(index)

    def _finish_archive_playback_start(self, index, player, video_path, token):
        """Tk-thread half of a clip start, run once the player reports Playing: applies audio/rate/resume, starts monitors."""
        if (token != self._play_tokens[index] or not self.running
                or self.media_players[index] is not player):
            return   # cancelled or replaced while starting

        if self.event_mode and self._event_first_clip_started_at is None:
            self._event_first_clip_started_at = time.time()

        self.set_audio_state(index, mute=self.archive_audio_muted[index])

        # Ramp into 4x/8x: starting a fresh decoder at high speed while other cams' decoders are mid-flight
        # tripped libvlc's "late video" bailout. Start lower only when target is 4x/8x and several cams are active.
        target_speed = self.global_playback_speed
        ramp_needed = (
            target_speed >= 4.0
            and self.event_mode
            and len(self.event_active_cams - self.event_done_cams) > 1
        )
        try:
            if ramp_needed:
                player.set_rate(2.0)
                _ramp_after_id = self.root.after(
                    800,
                    lambda i=index, s=target_speed: self._ramp_to_speed(i, s)
                )
                self._pending_event_afters.append({
                    "kind": "ramp_step",
                    "after_id": _ramp_after_id,
                    "index": index,
                    "path": None,  # not a clip launch
                    "unscaled_ms": 0,
                    "scheduled_at": time.time(),
                    "scheduled_speed": target_speed,
                })
            else:
                player.set_rate(target_speed)
        except Exception as e:
            logging.error(f"Stream {index}: Error setting initial playback rate: {e}")

        # Resume from the saved position only when browsing the archive; event clips always start from the beginning.
        if self.resume_playback and not self.event_mode:
            saved = self.watch_progress[index].get(video_path)
            if saved and saved.get("duration", 0) > 0:
                resume_at = saved.get("position", 0)
                if 0 < resume_at < saved["duration"] - 3:
                    try:
                        player.set_time(int(resume_at * 1000))
                        logging.info(f"Stream {index}: Resumed playback at {resume_at:.1f}s")
                    except Exception as e:
                        logging.warning(f"Stream {index}: Failed to seek to saved position: {e}")

        threading.Thread(target=self.monitor_vlc_playback, args=(index, player), daemon=True).start()
        self._start_hover_poll(index)
        logging.info(f"Stream {index}: Started python-vlc playback for archive video")

    def toggle_pause(self, index):
        self.is_paused[index] = not self.is_paused[index]

        new_icon = self.icon_cache["play" if self.is_paused[index] else "pause"]
        self.pause_buttons[index].configure(image=new_icon)
        self.pause_buttons[index].image = new_icon

        if self.media_players[index]:
            try:
                self.media_players[index].pause()
                # Resume at the current global rate (pausing doesn't change speed).
                if not self.is_paused[index]:
                    self.media_players[index].set_rate(self.global_playback_speed)
                logging.info(f"Stream {index} {'paused' if self.is_paused[index] else 'resumed'} at {self.global_playback_speed}x speed")
            except Exception as e:
                logging.error(f"Error toggling pause for stream {index}: {e}")

    def toggle_archive_audio(self, index):
        currently_muted = self.archive_audio_muted[index]
        new_muted = not currently_muted

        if not new_muted and self.exclusive_archive_audio:
            # Unmuting: mute every other archive stream first
            for i in range(4):
                if i != index and not self.archive_audio_muted[i]:
                    self.archive_audio_muted[i] = True
                    if self.media_players[i]:
                        try:
                            self.media_players[i].audio_set_mute(True)
                        except Exception:
                            pass
                    if self.audio_buttons[i]:
                        try:
                            self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                        except Exception:
                            pass

        self.archive_audio_muted[index] = new_muted
        if self.media_players[index]:
            try:
                self.media_players[index].audio_set_mute(new_muted)
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set archive audio mute: {e}")

        if self.audio_buttons[index]:
            icon = self.icon_cache["audio_off" if new_muted else "audio_on"]
            self.audio_buttons[index].configure(image=icon)

        logging.info(f"Stream {index}: Archive audio {'muted' if new_muted else 'unmuted'}")

    def _resume_after_seek(self, index):
        """Post-seek bookkeeping for rewind/forward: keep the global rate and, if paused, resume and restore the pause icon."""
        if self.media_players[index]:
            self.media_players[index].set_rate(self.global_playback_speed)
        if self.is_paused[index]:
            self.media_players[index].play()
            self.is_paused[index] = False
            new_icon = self.icon_cache["pause"]
            self.pause_buttons[index].configure(image=new_icon)
            self.pause_buttons[index].image = new_icon

    def rewind_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for rewind")
            return

        if self.media_players[index] and not self.video_ended[index]:
            try:
                current_time = self.media_players[index].get_time()
                new_time = max(0, current_time - 10000)
                self.media_players[index].set_time(new_time)
                self._resume_after_seek(index)
                logging.info(f"Stream {index}: Rewound video by 10 seconds to {new_time/1000:.1f}s")
            except Exception as e:
                logging.error(f"Error rewinding video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Video ended, restarted for rewind")

    def forward_video(self, index):
        """Skip the current clip forward 10 seconds (partner to rewind_video)."""
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for fast-forward")
            return

        if self.media_players[index] and not self.video_ended[index]:
            try:
                current_time = self.media_players[index].get_time()
                duration = self.media_players[index].get_length()
                new_time = current_time + 10000
                if duration and duration > 0:
                    new_time = min(new_time, max(0, duration - 500))
                self.media_players[index].set_time(new_time)
                self._resume_after_seek(index)
                logging.info(f"Stream {index}: Skipped video forward by 10 seconds to {new_time/1000:.1f}s")
            except Exception as e:
                logging.error(f"Error fast-forwarding video for stream {index}: {e}")
        # Clip already ended: nothing to skip to (unlike rewind, forward doesn't restart it).

    def replay_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for replay")
            return

        if self.media_players[index] and not self.video_ended[index]:
            try:
                self.media_players[index].set_time(0)
                self._resume_after_seek(index)
                logging.info(f"Stream {index}: Replayed video")
            except Exception as e:
                logging.error(f"Error replaying video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Restarted video playback")

    def cycle_speed(self):
        """Cycle the global playback speed and apply it to every playing archive/event clip; new clips pick it up too."""
        current_speed = self.global_playback_speed
        if current_speed not in self.speed_cycle:
            # Fall back to the first cycle entry on a stale/unexpected value.
            current_speed = self.speed_cycle[0]
        next_speed = self.speed_cycle[(self.speed_cycle.index(current_speed) + 1) % len(self.speed_cycle)]
        self.global_playback_speed = next_speed

        for i in range(4):
            if self.media_players[i] and self.is_archive_mode[i] and not self.is_paused[i]:
                try:
                    self.media_players[i].set_rate(next_speed)
                except Exception as e:
                    logging.error(f"Stream {i}: Error applying global playback speed: {e}")

        # Event waits already ticking down (inter-clip gap or cross-cam stagger) need their remaining time
        # recalculated at the new speed, or higher speeds look stuck.
        if self.event_mode:
            self._reschedule_pending_event_afters()

        logging.info(f"Global playback speed set to x{next_speed}")

        if self.speed_toggle_button:
            icon = self.get_speed_icon(next_speed)
            self.speed_toggle_button.configure(image=icon)
            self.speed_toggle_image = icon

    def monitor_vlc_playback(self, index, player):
        """Monitor one clip session. `player` is captured at start, not re-read from media_players[index]
        (the slot can be reused by a live stream); the loop exits once the slot no longer holds this player."""
        video_path = self.current_archive_path[index]
        while self.running and not self.video_ended[index]:
            if self.media_players[index] is not player:
                # Slot already replaced/cleared: this session is over regardless of video_ended.
                logging.debug(f"Stream {index}: Monitored player no longer active, stopping this monitor thread")
                break
            try:
                state = player.get_state()
                if state == vlc.State.Ended:
                    logging.info(f"Stream {index}: python-vlc playback ended")
                    self.video_ended[index] = True
                    # In event mode, hand off to the event coordinator instead of archive navigation.
                    if self.event_mode:
                        self.root.after(0, self._on_event_clip_ended, index)
                        break

                    existing = self.watch_progress[index].get(video_path, {})
                    duration = existing.get("duration", 0)
                    if duration > 0:
                        self.watch_progress[index][video_path] = {
                            "position": duration,
                            "duration": duration,
                        }
                        self.watch_progress_dirty = True
                    break

                position_ms = player.get_time()
                duration_ms = player.get_length()
                if (position_ms is not None and position_ms > 0 and duration_ms and duration_ms > 0
                        and not self.event_mode):
                    self.watch_progress[index][video_path] = {
                        "position": position_ms / 1000.0,
                        "duration": duration_ms / 1000.0,
                    }
                    self.watch_progress_dirty = True
                time.sleep(1.0)
            except Exception as e:
                logging.error(f"Error monitoring playback for stream {index}: {e}")
                self.video_ended[index] = True  # end the loop
                break

    @staticmethod
    def _onvif_host(ip):
        # Drop any :port or /path suffix, ONVIF has its own port.
        return ip.split("/", 1)[0].split(":", 1)[0].strip()

    @staticmethod
    def _onvif_transport(operation_timeout=10.0):
        from zeep.transports import Transport
        return Transport(timeout=10, operation_timeout=operation_timeout)

    def get_onvif_camera(self, ip):
        cached = self.onvif_cams.get(ip)
        if cached:
            return cached
        # A recent failure is not retried straight away, so a dead cam can't stall every click.
        if time.time() < self.onvif_fail_until.get(ip, 0):
            return None
        try:
            from onvif import ONVIFCamera
            cam = ONVIFCamera(self._onvif_host(ip), 2020, self.username, self.password,
                              adjust_time=True, transport=self._onvif_transport())
            media = cam.create_media_service()
            ptz = cam.create_ptz_service()
            profiles = media.GetProfiles()
            if not profiles:
                raise RuntimeError("camera returned no media profiles")
            profile = next((p for p in profiles if getattr(p, "PTZConfiguration", None)), profiles[0])
            info = {"cam": cam, "ptz": ptz, "media": media, "token": profile.token}
            self.onvif_cams[ip] = info
            self.onvif_fail_until.pop(ip, None)
            return info
        except Exception as e:
            self.onvif_fail_until[ip] = time.time() + 10.0
            logging.warning(f"ONVIF: could not connect to {ip}: {e}")
            return None

    def start_ptz_move(self, direction):
        idx = self.fullscreen_index
        if not self.is_fullscreen or idx is None or idx < 0 or not self.streams[idx]:
            return
        ip = self.ips[idx]
        if not ip or self.ptz_busy:
            return

        with self.ptz_lock:
            if self.ptz_buttons_disabled:
                return
            self.ptz_busy = True
            self.disable_ptz_buttons()

        logging.info(f"Starting PTZ move: direction={direction}, ip={ip}")
        threading.Thread(target=self.ptz_move_loop, args=(direction, ip), daemon=True).start()

    def disable_ptz_buttons(self):
        """Disable all PTZ buttons on the main thread."""
        if not self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = True
            self.root.after(0, lambda: [
                button.config(state="disabled") for button in self.ptz_buttons
            ])

    def _ptz_allowed_now(self):
        idx = self.fullscreen_index
        return bool(self.is_fullscreen and idx is not None and idx >= 0
                    and self.ptz_supported[idx] and not self.is_archive_mode[idx])

    def enable_ptz_buttons(self):
        """Re-enable the PTZ buttons on the main thread, if PTZ is usable right now."""
        if self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = False
            self.root.after(0, lambda: [
                button.config(state="normal" if self._ptz_allowed_now() else "disabled")
                for button in self.ptz_buttons
            ])

    def ptz_move_loop(self, direction, ip):
        try:
            with self.ptz_lock:
                if not self.get_onvif_camera(ip):
                    return

                start_time = time.time()
                self.send_ptz_command(ip, direction)
                time.sleep(0.5)  # fixed nudge length per click
                self.send_ptz_command(ip, "stop")

                cam_info = self.get_onvif_camera(ip)
                if not cam_info:
                    return
                ptz = cam_info["ptz"]
                token = cam_info["token"]

                # Wait for the camera to report idle, resending stop once if it doesn't.
                polls = 0
                resent = False
                poll_start = time.time()
                while time.time() - poll_start < 2.0:
                    try:
                        status = ptz.GetStatus({"ProfileToken": token})
                    except Exception as e:
                        logging.error(f"Error checking PTZ status for ip={ip}: {e}")
                        break
                    move_status = getattr(status, "MoveStatus", None)
                    pan_tilt = getattr(move_status, "PanTilt", None) if move_status is not None else None
                    if pan_tilt is None or str(pan_tilt).upper() == "UNKNOWN":
                        logging.info(f"PTZ status for ip={ip} is not reported by the camera, cannot confirm idle")
                        break
                    if str(pan_tilt).upper() == "IDLE":
                        logging.info(f"Confirmed PTZ idle for ip={ip} after {time.time() - start_time:.2f} seconds")
                        break
                    polls += 1
                    if polls == 3 and not resent:
                        resent = True
                        logging.warning(f"PTZ still {pan_tilt} after stop for ip={ip}, resending stop")
                        self.send_ptz_command(ip, "stop")
                    time.sleep(0.2)

                logging.info(f"PTZ movement completed for ip={ip}, direction={direction}, total duration={time.time() - start_time:.2f} seconds")

        except Exception as e:
            logging.error(f"Error in PTZ move loop for ip={ip}, direction={direction}: {e}", exc_info=True)
        finally:
            self.ptz_busy = False
            self.enable_ptz_buttons()

    def send_ptz_command(self, ip, command):
        cam_info = self.get_onvif_camera(ip)
        if not cam_info:
            logging.error(f"Cannot send PTZ command: No ONVIF camera for ip={ip}")
            return
        try:
            ptz = cam_info["ptz"]
            token = cam_info["token"]
            if command == "stop":
                # Explicit flags: some firmware ignores a bare Stop for pan moves.
                ptz.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
            else:
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                velocity = {"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 0}}
                # Speed scaling: speed = 0.025 * (4 ^ (resolution - 1)), capped at 1.0
                base_speed = 0.025  # Anchors resolution 1 at 0.025
                speed = min(1.0, base_speed * (4 ** (self.ptz_resolution - 1)))
                if command == "left":
                    velocity["PanTilt"]["x"] = -speed
                elif command == "right":
                    velocity["PanTilt"]["x"] = speed
                elif command == "up":
                    velocity["PanTilt"]["y"] = speed
                elif command == "down":
                    velocity["PanTilt"]["y"] = -speed
                else:
                    logging.warning(f"Invalid PTZ command: {command}")
                    return
                request.Velocity = velocity
                ptz.ContinuousMove(request)
                logging.info(f"Sent PTZ {command} command to ip={ip}, velocity={velocity}, resolution={self.ptz_resolution}, speed={speed:.4f}")
        except Exception as e:
            logging.error(f"Failed to send PTZ command {command} to ip={ip}: {e}", exc_info=True)

    def apply_window_size(self, size):
        logging.info(f"Applying window size: {size}")
        try:
            if size.lower() == "fullscreen":
                self.root.attributes("-fullscreen", True)
                self.root.update_idletasks()
            else:
                try:
                    width, height = map(int, size.split("x"))
                except (ValueError, AttributeError):
                    logging.warning(f"Invalid saved_window_size: {size}, using default {self.MIN_WIDTH}x{self.MIN_HEIGHT}")
                    width, height = self.MIN_WIDTH, self.MIN_HEIGHT

                width, height = self.clamp_size(width, height)

                self.force_unmaximize(width, height)

                self.root.attributes("-fullscreen", False)
                self.root.geometry(f"{width}x{height}")
                self.center_window(width, height)
        except Exception as e:
            logging.error(f"Failed to apply window size {size}: {e}", exc_info=True)
            width, height = self.MIN_WIDTH, self.MIN_HEIGHT
            self.root.attributes("-fullscreen", False)
            self.force_unmaximize(width, height)
            self.root.geometry(f"{width}x{height}")
            self.center_window(width, height)

    def center_window(self, width, height):
        try:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            taskbar_height = self.get_taskbar_height()

            if taskbar_height > 0:
                # Center in available space excluding taskbar (Windows)
                available_height = screen_height - taskbar_height
                x = (screen_width - width) // 2
                y = (available_height - height) // 2
                if y < 0:
                    y = 0
            else:
                # Linux, or Windows detection failure: use full screen height
                x = (screen_width - width) // 2
                y = (screen_height - height) // 2

            x = max(0, x)
            y = max(0, y)

            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.update_idletasks()
        except Exception as e:
            logging.error(f"Failed to center window: {e}", exc_info=True)
            self.root.geometry(f"{width}x{height}+0+0")
            self.root.update_idletasks()

    def clamp_size(self, width, height):
        screen_width = self.root.winfo_screenwidth() - 50  # Margin for panels/docks
        screen_height = self.root.winfo_screenheight() - 100
        return (max(self.MIN_WIDTH, min(width, screen_width)),
                max(self.MIN_HEIGHT, min(height, screen_height)))

    def force_unmaximize(self, width, height):
        try:
            self.root.wm_state("normal")
            self.root.update_idletasks()
            for attempt in range(3):
                if self.root.wm_state() != "zoomed":
                    return True
                self.root.wm_state("normal")
                self.root.update_idletasks()
                time.sleep(0.1)
            return False
        except Exception as e:
            logging.error(f"Error forcing unmaximize: {e}", exc_info=True)
            return False
    
    def get_taskbar_height(self):
        """Detect taskbar height for Windows; return 0 for Linux."""
        if sys.platform.startswith("win"):
            try:
                class RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long),
                                ("top", ctypes.c_long),
                                ("right", ctypes.c_long),
                                ("bottom", ctypes.c_long)]
                
                rect = RECT()
                SPI_GETWORKAREA = 0x0030
                ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
                screen_height = self.root.winfo_screenheight()
                work_area_height = rect.bottom - rect.top
                taskbar_height = screen_height - work_area_height
                if taskbar_height > 0 and taskbar_height < screen_height // 2:
                    return taskbar_height
            except Exception:
                pass
        return 0

    def _cleanup_archive_mode_ui(self, index):
        # Tk-only half of archive cleanup (no VLC work); safe on the main thread.
        try:
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            logging.info(f"Stream {index}: Destroyed all child widgets in label")

            if self.archive_canvas[index]:
                self.archive_canvas[index].pack_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()

            self._reset_clip_buttons(index)

            self.is_archive_mode[index] = False
            self.current_archive_path[index] = None
            self.is_paused[index] = False
            self.video_ended[index] = False
            self.pagination_state[index] = {}

            self.labels[index].pack(fill="both", expand=True)

        except Exception as e:
            logging.error(f"Stream {index}: Failed to clean up archive mode UI: {e}")

    def _cleanup_archive_mode_vlc(self, index):
        # VLC-only half of archive cleanup. Call from a background thread holding archive_entry_locks[index]
        # or from shutdown's teardown thread.
        try:
            if self.media_players[index]:
                self.cleanup_stream(index)
        except Exception as e:
            logging.error(f"Stream {index}: Failed to clean up archive mode VLC resources: {e}")

    def cleanup_config_panel(self):
        try:
            if self.config_panel:
                self.config_panel.destroy()
                self.config_panel = None
                # config_panel children are destroyed with it; just drop the stale references.
                self.speed_toggle_button = None
                self.speed_toggle_image = None
            if self.ptz_buttons:
                for button in self.ptz_buttons:
                    button.destroy()
                self.ptz_buttons = []
                self.ptz_images = []
            if self.exit_fullscreen_button:
                self.exit_fullscreen_button.destroy()
                self.exit_fullscreen_button = None
                self.exit_fullscreen_image = None
            if self.config_button:
                self.config_button.destroy()
                self.config_button = None
                self.config_img = None
            if self.archive_mode_button:
                self.archive_mode_button.destroy()
                self.archive_mode_button = None
                self.archive_mode_image = None
            for i in range(4):
                if self.archive_buttons[i]:
                    self.archive_buttons[i].destroy()
                    self.archive_buttons[i] = None
            for i in range(4):
                if self.fullscreen_buttons[i]:
                    self.fullscreen_buttons[i].destroy()
                    self.fullscreen_buttons[i] = None
        except Exception:
            pass



    def cleanup(self):
        self.enable_ptz_buttons()

        for _i in range(4):
            self._cancel_bg_sleep_timer(_i)

        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None

        # Signal background threads to stop before touching any VLC object.
        self.running = False
        for i in range(4):
            self.stream_cleanup_events[i].set()
            self.stream_abort_events[i].set()

        # Safety-net flush in case the app is closed mid-video.
        if self.watch_progress_dirty:
            self.save_watch_progress()

        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None

        for _pending in getattr(self, "_pending_event_afters", []):
            try:
                self.root.after_cancel(_pending["after_id"])
            except Exception:
                pass
        self._pending_event_afters = []

        try:
            self.root.withdraw()
        except Exception:
            pass

        def _vlc_teardown():
            for i in range(4):
                try:
                    self._wait_init_idle(i, timeout=4.0)
                    self.cleanup_stream(i)
                except Exception as e:
                    logging.error(f"Error during shutdown of stream {i}: {e}")

            self.onvif_cams.clear()

            # Hand Tk widget destruction back to the main thread.
            self.root.after(0, _tk_teardown)

        def _tk_teardown():
            for i in range(4):
                try:
                    if self.archive_canvas[i]:
                        self.archive_canvas[i].destroy()
                    if self.back_buttons[i]:
                        self.back_buttons[i].destroy()
                    if self.exit_buttons[i]:
                        self.exit_buttons[i].destroy()
                    if self.pause_buttons[i]:
                        self.pause_buttons[i].destroy()
                    if self.ff_buttons[i]:
                        self.ff_buttons[i].destroy()
                    if self.replay_buttons[i]:
                        self.replay_buttons[i].destroy()
                    if self.rewind_buttons[i]:
                        self.rewind_buttons[i].destroy()
                    if self.audio_buttons[i]:
                        self.audio_buttons[i].destroy()
                    self.archive_canvas[i] = None
                    self.back_buttons[i] = None
                    self._reset_clip_buttons(i)
                except Exception as e:
                    logging.error(f"Error cleaning up UI for stream {i}: {e}")

            self.cleanup_config_panel()

            try:
                self.root.destroy()
                logging.info("Shutdown completed")
            except Exception as e:
                logging.error(f"Error destroying Tkinter root: {e}")

        import threading as _threading
        _threading.Thread(target=_vlc_teardown, daemon=True).start()

if __name__ == "__main__":
    # Xlib must be told about multi-threaded use before Tk opens the display
    # (Tk, libvlc's GL vout and our workers all hit X).
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libX11.so.6").XInitThreads()
        except OSError:
            pass
    # A native crash (SIGSEGV/SIGABRT) leaves no Python traceback; faulthandler dumps all thread stacks to this file.
    try:
        import faulthandler
        if sys.platform.startswith("linux"):
            _crash_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        else:
            _crash_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        os.makedirs(_crash_dir, exist_ok=True)
        _crash_fh = open(os.path.join(_crash_dir, "crash.log"), "a")
        faulthandler.enable(file=_crash_fh, all_threads=True)
    except Exception:
        pass
    root = tk.Tk()
    app = tapoStreamer(root)
    root.mainloop()