"""
gesture_core.py -- every decision PinchFlick makes.

Pure Python: no camera, no MediaPipe, no pyautogui, no OpenCV. This module
takes hand landmarks + timestamps and returns decisions (scroll clicks,
seek direction, status text). That is what lets selftest.py verify the
whole gesture pipeline without a webcam.

Landmark input: any sequence of 21 objects exposing `.x` / `.y`
(MediaPipe's NormalizedLandmark qualifies; the tests use simple Points).
"""

from __future__ import annotations

import copy
import ctypes
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# MediaPipe hand landmark indices (standard 21-point skeleton)
# ---------------------------------------------------------------------------
WRIST = 0
THUMB_TIP = 4
INDEX_TIP = 8
MIDDLE_MCP = 9
# (tip, pip) pairs for index, middle, ring, pinky -- tip above pip = extended
FINGER_LANDMARKS = ((8, 6), (12, 10), (16, 14), (20, 18))

HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),          # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),          # index
    (0, 9), (9, 10), (10, 11), (11, 12),     # middle
    (0, 13), (13, 14), (14, 15), (15, 16),   # ring
    (0, 17), (17, 18), (18, 19), (19, 20),   # pinky
    (5, 9), (9, 13), (13, 17),               # palm
)

# ---------------------------------------------------------------------------
# Default configuration -- everything the app does is tunable from config.json
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # --- camera / tracking ---
    "camera_index": 0,
    "frame_width": 640,
    "frame_height": 480,
    "mirror_display": True,          # flip camera view so it feels like a mirror
    "num_hands": 1,
    "min_detection_confidence": 0.6,
    "min_presence_confidence": 0.6,
    "min_tracking_confidence": 0.6,

    # --- scroll gesture (works everywhere) ---
    "scroll_enabled": True,
    "scroll_up_fingers": 1,          # this many fingers up = scroll up
    "scroll_down_fingers": 2,        # this many fingers up = scroll down
    "scroll_speed": 5,               # wheel clicks per trigger frame
    "scroll_interval_ms": 0,         # 0 = every frame while held (original feel)
    "scroll_confirm_frames": 2,      # gesture must be stable this many frames
    "scroll_invert": False,
    "scroll_open_hand_ratio": 0.55,  # scroll only counts when hand is open
                                     # this far (thumb/index ratio) -- stops a
                                     # half-closed pinch from faking a scroll

    # --- seek gesture (YouTube only) ---
    "seek_enabled": True,
    "seek_requires_youtube": True,   # double-pinch seeks ONLY when a YouTube
                                     # tab is focused; ignored everywhere else
    "seek_seconds": 10,              # any multiple of 5 (YouTube shortcut granularity)
    "double_tap_window_ms": 550,     # max gap between the two pinches
    "seek_cooldown_ms": 0,           # min gap between seeks; 0 = no cooldown
    "tap_side_dead_zone": 0.04,      # pinches within this fraction of the
                                     # center line are ignored (with a message)
                                     # so a pinch near the middle can't flip
                                     # sides randomly
    "scroll_suppress_after_seek_ms": 500,  # scroll rests briefly after a seek
    "pinch_on_ratio": 0.35,          # thumb/index distance below this = pinched
    "pinch_off_ratio": 0.55,         # must open past this before next pinch counts
    "seek_keys": {                   # YouTube's own shortcuts, remap if you like
        "forward_5": "right",
        "backward_5": "left",
        "forward_10": "l",
        "backward_10": "j",
    },

    # --- YouTube detection (foreground window title) ---
    "youtube_title_markers": ["- YouTube", "\u2013 YouTube"],
    "youtube_check_interval_ms": 200,

    # --- misc ---
    "show_debug_window": False,      # start with the extra debug overlay on
    "always_on_top": True,           # preview floats above fullscreen video
    "pyautogui_failsafe": True,      # slam mouse into a corner = panic exit
    "model_path": None,              # null = auto-locate/download hand_landmarker.task
}


class ConfigError(Exception):
    """Raised for a missing, malformed, or invalid config file."""


# ---------------------------------------------------------------------------
# Config loading + validation
# ---------------------------------------------------------------------------
def default_config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path=None):
    """Merge a JSON config over DEFAULT_CONFIG and validate the result.

    path=None -> use config.json next to this file if it exists, else defaults.
    An explicitly given path that doesn't exist is an error.
    """
    explicit = path is not None
    if path is None:
        path = default_config_path()

    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(user, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        unknown = sorted(set(user) - set(DEFAULT_CONFIG))
        if unknown:
            raise ConfigError(
                f"unknown key(s) in {path}: {', '.join(unknown)} "
                f"(valid keys: {', '.join(sorted(DEFAULT_CONFIG))})"
            )
        # partial override of the seek_keys table is allowed
        if isinstance(user.get("seek_keys"), dict):
            user["seek_keys"] = {**DEFAULT_CONFIG["seek_keys"], **user["seek_keys"]}
        cfg.update(user)
    elif explicit:
        raise ConfigError(f"config file not found: {path}")

    validate_config(cfg, source=path)
    return cfg


def _type_name(v):
    return type(v).__name__


def validate_config(cfg, source="<config>"):
    """Raise ConfigError naming the exact key if anything is off."""

    def err(key, why, got):
        return ConfigError(f"{source}: '{key}' {why} (got {got!r})")

    def want_bool(key):
        if not isinstance(cfg[key], bool):
            raise err(key, "must be true or false", cfg[key])

    def want_int(key, lo=None, hi=None):
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise err(key, "must be a whole number", v)
        if lo is not None and v < lo:
            raise err(key, f"must be >= {lo}", v)
        if hi is not None and v > hi:
            raise err(key, f"must be <= {hi}", v)

    def want_number(key, lo=None, hi=None):
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise err(key, "must be a number", v)
        if lo is not None and v < lo:
            raise err(key, f"must be >= {lo}", v)
        if hi is not None and v > hi:
            raise err(key, f"must be <= {hi}", v)

    for key in (
        "mirror_display", "scroll_enabled", "scroll_invert",
        "seek_enabled", "seek_requires_youtube",
        "show_debug_window", "always_on_top", "pyautogui_failsafe",
    ):
        want_bool(key)

    for key in ("camera_index", "frame_width", "frame_height", "num_hands",
                "scroll_up_fingers", "scroll_down_fingers", "scroll_speed",
                "scroll_interval_ms", "scroll_confirm_frames",
                "seek_seconds", "double_tap_window_ms", "seek_cooldown_ms",
                "scroll_suppress_after_seek_ms", "youtube_check_interval_ms"):
        want_int(key, lo=0)

    if cfg["camera_index"] < 0:
        raise err("camera_index", "must be >= 0", cfg["camera_index"])
    if cfg["frame_width"] < 64 or cfg["frame_height"] < 64:
        raise err("frame_width/frame_height", "must be at least 64", (cfg["frame_width"], cfg["frame_height"]))
    if cfg["num_hands"] not in (1, 2):
        raise err("num_hands", "must be 1 or 2", cfg["num_hands"])
    if cfg["scroll_up_fingers"] not in range(5) or cfg["scroll_down_fingers"] not in range(5):
        raise err("scroll_up_fingers/scroll_down_fingers", "must be 0-4", (cfg["scroll_up_fingers"], cfg["scroll_down_fingers"]))
    if cfg["scroll_up_fingers"] == cfg["scroll_down_fingers"]:
        raise err("scroll_up_fingers/scroll_down_fingers", "must differ", cfg["scroll_up_fingers"])
    if cfg["scroll_speed"] < 1:
        raise err("scroll_speed", "must be >= 1", cfg["scroll_speed"])
    if cfg["scroll_confirm_frames"] < 1:
        raise err("scroll_confirm_frames", "must be >= 1", cfg["scroll_confirm_frames"])
    if cfg["double_tap_window_ms"] < 100:
        raise err("double_tap_window_ms", "must be >= 100", cfg["double_tap_window_ms"])

    if isinstance(cfg["seek_seconds"], bool) or not isinstance(cfg["seek_seconds"], int) \
            or cfg["seek_seconds"] < 5 or cfg["seek_seconds"] > 60 \
            or cfg["seek_seconds"] % 5 != 0:
        raise err("seek_seconds", "must be a multiple of 5 between 5 and 60 "
                 "(YouTube's shortcuts only move in 5s steps)", cfg["seek_seconds"])

    for key in ("pinch_on_ratio", "pinch_off_ratio", "scroll_open_hand_ratio",
                "tap_side_dead_zone",
                "min_detection_confidence", "min_presence_confidence",
                "min_tracking_confidence"):
        want_number(key, lo=0.0, hi=1.0)
    if cfg["tap_side_dead_zone"] > 0.49:
        raise err("tap_side_dead_zone", "must be <= 0.49", cfg["tap_side_dead_zone"])
    if cfg["pinch_on_ratio"] >= cfg["pinch_off_ratio"]:
        raise err("pinch_on_ratio", "must be smaller than pinch_off_ratio", cfg["pinch_on_ratio"])
    if cfg["pinch_on_ratio"] >= cfg["scroll_open_hand_ratio"]:
        raise err("scroll_open_hand_ratio", "must be larger than pinch_on_ratio",
                  cfg["scroll_open_hand_ratio"])

    keys = cfg["seek_keys"]
    if not isinstance(keys, dict):
        raise err("seek_keys", "must be an object", keys)
    missing = [k for k in DEFAULT_CONFIG["seek_keys"] if not isinstance(keys.get(k), str) or not keys.get(k)]
    if missing:
        raise err("seek_keys", f"missing/empty key binding(s): {', '.join(missing)}", keys)

    markers = cfg["youtube_title_markers"]
    if not isinstance(markers, list) or not all(isinstance(m, str) and m for m in markers):
        raise err("youtube_title_markers", "must be a non-empty list of strings", markers)
    if cfg["seek_requires_youtube"] and not markers:
        raise err("youtube_title_markers", "cannot be empty while seek_requires_youtube is true", markers)

    if cfg["model_path"] is not None and not isinstance(cfg["model_path"], str):
        raise err("model_path", "must be a string path or null", cfg["model_path"])


# ---------------------------------------------------------------------------
# Landmark math (the shared "pinch ratio" primitive)
# ---------------------------------------------------------------------------
def hand_size(landmarks):
    """Wrist -> middle-knuckle distance: a stable hand-size reference so
    ratios don't change just because the hand is near/far from the camera."""
    w = landmarks[WRIST]
    m = landmarks[MIDDLE_MCP]
    return max(((w.x - m.x) ** 2 + (w.y - m.y) ** 2) ** 0.5, 1e-6)


def pinch_ratio(landmarks):
    """Thumb-tip to index-tip distance, normalized by hand size."""
    t = landmarks[THUMB_TIP]
    i = landmarks[INDEX_TIP]
    return ((t.x - i.x) ** 2 + (t.y - i.y) ** 2) ** 0.5 / hand_size(landmarks)


def count_extended_fingers(landmarks):
    """How many of index/middle/ring/pinky point up (tip above pip)."""
    return sum(
        1 for tip, pip in FINGER_LANDMARKS
        if landmarks[tip].y < landmarks[pip].y
    )


def tap_x(landmarks, input_mirrored=True):
    """Horizontal position of the pinch in VIEW space (0=left, 1=right).

    input_mirrored=True means landmarks are already in mirror/display space
    (frame was flipped before detection) -- use x as-is. False means raw
    camera space -- flip x so left/right match what the user sees.
    """
    raw_x = (landmarks[THUMB_TIP].x + landmarks[INDEX_TIP].x) / 2.0
    return raw_x if input_mirrored else 1.0 - raw_x


def tap_side(landmarks, input_mirrored=True):
    """'left' or 'right' half of the view the user SEES (split at x=0.5)."""
    return "left" if tap_x(landmarks, input_mirrored) < 0.5 else "right"


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------
class PinchState:
    """Hysteresis latch: enters below `on_ratio`, only leaves past `off_ratio`.

    Frame-to-frame jitter inside the gap can't re-fire or drop the state, so
    one physical pinch produces exactly one rising edge.
    """

    def __init__(self, on_ratio, off_ratio):
        self.on_ratio = on_ratio
        self.off_ratio = off_ratio
        self.active = False

    def update(self, ratio):
        """Feed this frame's ratio; returns True on a fresh pinch (rising edge)."""
        if ratio is None:
            self.active = False
            return False
        if not self.active and ratio < self.on_ratio:
            self.active = True
            return True
        if self.active and ratio > self.off_ratio:
            self.active = False
        return False

    def release(self):
        self.active = False


class DoubleTapSeeker:
    """Two pinches on the same side of the frame inside the window = a seek."""

    def __init__(self, window_ms, cooldown_ms):
        self.window_ms = window_ms
        self.cooldown_ms = cooldown_ms
        self.last_tap_time = None
        self.last_tap_side = None
        self.last_seek_time = None
        self.status = "Ready -- pinch twice quickly on one side to seek"

    def register(self, side, now_ms):
        """Feed a pinch event; returns 'backward'/'forward' or None."""
        paired = (
            self.last_tap_side == side
            and self.last_tap_time is not None
            and (now_ms - self.last_tap_time) <= self.window_ms
        )
        if paired:
            self.last_tap_time = None
            self.last_tap_side = None
            if self.last_seek_time is None or (now_ms - self.last_seek_time) > self.cooldown_ms:
                self.last_seek_time = now_ms
                direction = "backward" if side == "left" else "forward"
                self.status = f"Double-tap {side} -> {'-' if side == 'left' else '+'}seek"
                return direction
            self.status = "Too soon after the last seek -- wait a moment"
            return None

        self.last_tap_time = now_ms
        self.last_tap_side = side
        self.status = f"Tap ({side}) -- tap again quickly to confirm"
        return None


class ScrollDriver:
    """Turns per-frame finger counts into wheel clicks.

    A gesture must hold steady for `confirm_frames` consecutive frames before
    it fires (kills single-frame flicker), and the gate (`allowed=False`, set
    by pinch/seek suppression in GestureEngine) drops any pending frames so a
    pinch can never leak a phantom scroll.
    """

    def __init__(self, speed, confirm_frames, interval_ms, invert,
                 up_fingers, down_fingers):
        self.speed = speed
        self.confirm = confirm_frames
        self.interval_ms = interval_ms
        self.invert = invert
        self.up_fingers = up_fingers
        self.down_fingers = down_fingers
        self._recent = []
        self._last_fire = None

    def reset(self):
        self._recent = []

    def update(self, fingers, now_ms, allowed):
        """Returns wheel clicks to send this frame (0 = send nothing)."""
        if not allowed:
            self._recent = []
            return 0
        self._recent.append(fingers)
        if len(self._recent) > self.confirm:
            self._recent.pop(0)
        if len(self._recent) < self.confirm or len(set(self._recent)) > 1:
            return 0
        target = self._recent[-1]
        if target not in (self.up_fingers, self.down_fingers):
            return 0
        if (self.interval_ms > 0 and self._last_fire is not None
                and now_ms - self._last_fire < self.interval_ms):
            return 0
        self._last_fire = now_ms
        clicks = self.speed if target == self.up_fingers else -self.speed
        return -clicks if self.invert else clicks


# ---------------------------------------------------------------------------
# Seek key composition (YouTube shortcuts move in 5s/10s steps)
# ---------------------------------------------------------------------------
def seek_key_sequence(seek_seconds, direction, keys):
    """List of key presses for e.g. 15s forward -> ['l', 'right']."""
    if direction not in ("forward", "backward"):
        raise ValueError(f"direction must be 'forward' or 'backward', got {direction!r}")
    if isinstance(seek_seconds, bool) or not isinstance(seek_seconds, int) \
            or seek_seconds < 5 or seek_seconds % 5 != 0:
        raise ValueError(f"seek_seconds must be a multiple of 5 (>=5), got {seek_seconds!r}")
    tens, remainder = divmod(seek_seconds, 10)
    seq = [keys[f"{direction}_10"]] * tens
    if remainder:
        seq.append(keys[f"{direction}_5"])
    return seq


# ---------------------------------------------------------------------------
# YouTube detection via the foreground window's title
# ---------------------------------------------------------------------------
def is_youtube_title(title, markers):
    """True when the title carries the marker as a complete tail segment.

    Real window titles look like:

        "Never Gonna Give You Up - YouTube"          (Chrome, Edge)
        "Never Gonna Give You Up - YouTube - Brave"  (Brave appends its name)
        "Never Gonna Give You Up - YouTube - Mozilla Firefox"

    so the marker must sit at the very end OR be followed only by
    " - <browser name>". The marker has to start a segment and nothing but
    a browser-name segment may follow it -- that keeps
    "My blog post about YouTube" or "GitHub - YouTube.js" from matching
    while still accepting every real browser's title format.
    """
    if not title:
        return False
    folded = title.rstrip().casefold()
    for marker in markers:
        m = marker.rstrip().casefold()
        if not m:
            continue
        if re.search(re.escape(m) + r"(?: [-\u2013\u2014] .*)?$", folded):
            return True
    return False


def get_foreground_window_title():
    """Best-effort title of the focused window ('' if unknown)."""
    try:
        if sys.platform == "win32":
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value
        try:
            import pygetwindow as gw  # type: ignore
            win = gw.getActiveWindow()
            return win.title if win else ""
        except Exception:
            return ""
    except Exception:
        return ""


class YouTubeDetector:
    """Caches 'is the focused window a YouTube tab?' every N ms."""

    def __init__(self, markers, check_interval_ms):
        self.markers = markers
        self.interval = check_interval_ms
        self._last_check = None
        self.title = ""
        self.is_youtube = False

    def check(self, now_ms, force=False):
        if force or self._last_check is None or (now_ms - self._last_check) >= self.interval:
            self._last_check = now_ms
            self.title = get_foreground_window_title()
            self.is_youtube = is_youtube_title(self.title, self.markers)
        return self.is_youtube, self.title


# ---------------------------------------------------------------------------
# The engine: one frame in -> decisions out
# ---------------------------------------------------------------------------
class EngineResult:
    def __init__(self, ratio, fingers, pinched, scroll_clicks,
                 seek, seek_blocked, tap_status, side, tap):
        self.ratio = ratio
        self.fingers = fingers
        self.pinched = pinched
        self.scroll_clicks = scroll_clicks
        self.seek = seek
        self.seek_blocked = seek_blocked
        self.tap_status = tap_status
        self.side = side
        self.tap = tap  # side of a freshly registered tap this frame, else None

    def __repr__(self):
        return (f"EngineResult(ratio={self.ratio}, fingers={self.fingers}, "
                f"pinched={self.pinched}, scroll={self.scroll_clicks:+d}, "
                f"seek={self.seek}, tap={self.tap}, blocked={self.seek_blocked})")


class GestureEngine:
    """Combines pinch latch + double-tap seeker + scroll driver into one
    per-frame decision, including all the suppression rules:

    - while pinched (or hand not clearly open) -> no scroll
    - for `scroll_suppress_after_seek_ms` after a seek -> no scroll
    - seek fires only if enabled and (optionally) a YouTube tab is focused
    """

    def __init__(self, cfg, input_mirrored=True):
        self.cfg = cfg
        self.input_mirrored = input_mirrored
        self.pinch = PinchState(cfg["pinch_on_ratio"], cfg["pinch_off_ratio"])
        self.seeker = DoubleTapSeeker(cfg["double_tap_window_ms"], cfg["seek_cooldown_ms"])
        self.scroller = ScrollDriver(
            speed=cfg["scroll_speed"],
            confirm_frames=cfg["scroll_confirm_frames"],
            interval_ms=cfg["scroll_interval_ms"],
            invert=cfg["scroll_invert"],
            up_fingers=cfg["scroll_up_fingers"],
            down_fingers=cfg["scroll_down_fingers"],
        )
        self.last_seek_ms = None
        self.last_side = None

    def frame(self, landmarks, now_ms, on_youtube=False):
        cfg = self.cfg

        if not landmarks:  # no hand in view
            self.pinch.release()
            self.scroller.reset()
            return EngineResult(
                ratio=None, fingers=0, pinched=False, scroll_clicks=0,
                seek=None, seek_blocked=None,
                tap_status=self.seeker.status, side=self.last_side, tap=None,
            )

        ratio = pinch_ratio(landmarks)
        fresh_pinch = self.pinch.update(ratio)

        seek = None
        seek_blocked = None
        tap = None
        if fresh_pinch:
            pos_x = tap_x(landmarks, self.input_mirrored)
            dead = cfg["tap_side_dead_zone"]
            if abs(pos_x - 0.5) < dead:
                # ambiguous middle: better to refuse than to assign a random
                # side and silently break the double-tap pairing
                self.seeker.status = (
                    "Too close to the middle -- pinch clearly left or right "
                    "of the line")
            else:
                side = "left" if pos_x < 0.5 else "right"
                self.last_side = side
                tap = side
                direction = self.seeker.register(side, now_ms)
                if direction:
                    if not cfg["seek_enabled"]:
                        seek_blocked = "seek is disabled in config"
                    elif cfg["seek_requires_youtube"] and not on_youtube:
                        seek_blocked = "not on YouTube"
                    else:
                        seek = direction
                        self.last_seek_ms = now_ms

        fingers = count_extended_fingers(landmarks)

        scroll_ok = (
            cfg["scroll_enabled"]
            and not self.pinch.active
            and ratio >= cfg["scroll_open_hand_ratio"]
        )
        if scroll_ok and self.last_seek_ms is not None:
            if now_ms - self.last_seek_ms < cfg["scroll_suppress_after_seek_ms"]:
                scroll_ok = False
        clicks = self.scroller.update(fingers, now_ms, scroll_ok)

        return EngineResult(
            ratio=ratio, fingers=fingers, pinched=self.pinch.active,
            scroll_clicks=clicks, seek=seek, seek_blocked=seek_blocked,
            tap_status=self.seeker.status, side=self.last_side, tap=tap,
        )
