"""
integration_test.py -- end-to-end check of the SEEK path against a REAL window.

selftest.py proves the gesture logic with synthetic hands. This script proves
the three things that only real system integration can:

  1. A real foreground window's title is read correctly by Windows.
  2. The YouTube gate opens for a title ending in "- YouTube" and stays shut
     for anything else.
  3. The seek keystrokes actually reach the focused window (the exact
     mechanism controller.py uses -- YouTube's own j/l/arrow shortcuts).

A tiny test window titled "... - YouTube" flashes up for a couple of seconds;
keys are sent to it and counted. No webcam needed.

    python integration_test.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pyautogui

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False  # a stray corner-slam must not kill the test

import tkinter as tk  # noqa: E402

from gesture_core import (  # noqa: E402
    GestureEngine, YouTubeDetector, get_foreground_window_title,
    is_youtube_title, load_config, seek_key_sequence,
)
from selftest import make_hand  # noqa: E402


def pump(root, seconds):
    """Process window events (and deliver synthetic keypresses) for a bit."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


def focus_window(root):
    """Give the test window real foreground status.

    Windows blocks programmatic focus-stealing (SetForegroundWindow), so do
    what a user does: click on the window. Falls back to the ALT-key trick
    if the click path is somehow unavailable. Returns True on success.
    """
    import ctypes

    expected = root.title()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    try:
        pyautogui.click(x + w // 2, y + h // 2)
        pump(root, 0.3)
        if get_foreground_window_title() == expected:
            return True
    except Exception:  # noqa: BLE001
        pass
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)            # ALT down
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)       # ALT up
    pump(root, 0.3)
    return get_foreground_window_title() == expected


def run_double_pinch(engine, on_youtube):
    """The real engine, fed the real double-pinch-right gesture."""
    open_h = make_hand(extended=(1, 0, 0, 0))
    pinch = make_hand(pinched=True, dx=0.15)  # right half of the view
    out = None
    for landmarks, t in ((open_h, 0), (pinch, 100), (open_h, 150), (pinch, 450)):
        out = engine.frame(landmarks, t, on_youtube=on_youtube)
    return out


def main():
    cfg = load_config(None)
    original_mouse_pos = pyautogui.position()  # put the cursor back when done

    # --- real window with a real title ---------------------------------
    root = tk.Tk()
    root.title("Integration Test - YouTube")
    root.geometry("440x120+80+80")
    tk.Label(root, text="PinchFlick integration test",
             font=("Segoe UI", 11)).pack(pady=30)
    received = []
    root.bind("<Key>", lambda e: received.append(e.keysym))
    root.attributes("-topmost", True)
    root.update()
    if not focus_window(root):
        root.destroy()
        print("INTEGRATION FAILED: could not bring the test window to the front")
        return 1

    detector = YouTubeDetector(cfg["youtube_title_markers"], cfg["youtube_check_interval_ms"])

    # --- 1. real titles -> real detection --------------------------------
    # Covers both formats seen in the wild: plain Chrome/Edge style and
    # Brave style (browser name appended after the YouTube marker).
    for candidate in (
        "Integration Test - YouTube - Brave",
        "Integration Test - YouTube",
        "Integration Test \u2013 YouTube",
    ):
        root.title(candidate)
        root.update()
        pump(root, 0.1)
        on_yt, title = detector.check(0, force=True)
        assert on_yt, f"window titled {candidate!r} was not detected: {title!r}"
    assert get_foreground_window_title() == "Integration Test \u2013 YouTube", (
        f"test window is not actually foreground: {get_foreground_window_title()!r}")
    print(f"[1] PASS  real foreground titles detected (incl. Brave-style): {title!r}")

    # --- 2. detection opens the gate -> engine emits a seek --------------
    out = run_double_pinch(GestureEngine(cfg, input_mirrored=True), on_youtube=on_yt)
    assert out.seek == "forward", f"engine did not produce a seek: {out!r}"
    print(f"[2] PASS  engine produced seek on YouTube: {out.seek!r}")

    # --- 3. keystrokes reach the focused window -------------------------
    # Re-assert focus immediately before sending, exactly as the gate
    # requires: keystrokes go wherever the foreground window is.
    if not focus_window(root):
        raise AssertionError(
            f"lost foreground to: {get_foreground_window_title()!r}")
    fg = get_foreground_window_title()
    assert is_youtube_title(fg, cfg["youtube_title_markers"]), f"focus lost: {fg!r}"
    seq = seek_key_sequence(cfg["seek_seconds"], out.seek, cfg["seek_keys"])
    for key in seq:
        pyautogui.press(key)
    pump(root, 0.6)
    assert seq and seq[0] in received, (
        f"keystrokes never reached the window (sent {seq}, received {received})")
    print(f"[3] PASS  keystrokes delivered to the focused window: sent {seq}, got {received}")

    # --- 4. same gesture, non-YouTube title -> blocked, zero keys --------
    root.title("Integration Test - Blog Post")
    root.update()
    pump(root, 0.3)
    on_yt2, title2 = detector.check(999_999, force=True)
    assert not on_yt2, f"non-YouTube title wrongly detected: {title2!r}"
    out2 = run_double_pinch(GestureEngine(cfg, input_mirrored=True), on_youtube=on_yt2)
    assert out2.seek is None and out2.seek_blocked == "not on YouTube", (
        f"seek must be blocked off-YouTube: {out2!r}")
    sent_before = len(received)
    if out2.seek:  # pragma: no cover -- guarded by the assert above
        for key in seek_key_sequence(cfg["seek_seconds"], out2.seek, cfg["seek_keys"]):
            pyautogui.press(key)
    pump(root, 0.4)
    assert len(received) == sent_before, "keys were sent despite the YouTube gate!"
    print(f"[4] PASS  non-YouTube title blocks the seek, no keys sent: {title2!r}")

    root.destroy()
    pyautogui.moveTo(original_mouse_pos.x, original_mouse_pos.y)
    print("\nINTEGRATION: ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nINTEGRATION FAILED: {exc}")
        sys.exit(1)
