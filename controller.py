"""
controller.py -- PinchFlick (gesture scroll + YouTube seek, one app).

    python controller.py            # start with the camera
    python controller.py --debug    # start with the full debug overlay
    python controller.py --selftest # verify logic without a camera
    python controller.py --smoke 8  # run 8 seconds unattended, print a summary

Gestures (both live at the same time):
    * 1 finger up ........ scroll up      -- works everywhere (any app/page)
    * 2 fingers up ......  scroll down    -- works everywhere
    * double-pinch left .. seek -10s      -- ONLY while a YouTube tab is focused
    * double-pinch right . seek +10s      -- ONLY while a YouTube tab is focused

Seek safety: the seek keystrokes (YouTube's own j/l/arrow shortcuts) are only
sent when the FOREGROUND window's title ends with a YouTube marker (e.g.
"... - YouTube"), so a pinch on a blog, game, or editor does nothing. The
same signal decides everything, so a seek can never land on the wrong window.

Camera window keys:  q / ESC = quit,  d = toggle debug overlay
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

import gesture_core as core
from gesture_core import ConfigError, GestureEngine, YouTubeDetector, load_config, seek_key_sequence

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)


# ---------------------------------------------------------------------------
# Model resolution: config path -> bundled -> the sibling gesture_scroll copy -> download
# ---------------------------------------------------------------------------
def resolve_model(cfg):
    if cfg["model_path"]:
        if not os.path.exists(cfg["model_path"]):
            raise SystemExit(f"Config error: model_path does not exist: {cfg['model_path']}")
        return cfg["model_path"]
    candidates = [
        os.path.join(HERE, "hand_landmarker.task"),
        os.path.abspath(os.path.join(HERE, "..", "gesture_scroll", "hand_landmarker.task")),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    dest = os.path.join(HERE, "hand_landmarker.task")
    print(f"Downloading hand-tracking model to {dest} (one-time, ~7 MB)...")
    try:
        urllib.request.urlretrieve(MODEL_URL, dest)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Couldn't download the model ({exc}).\n"
            f"Download it manually from:\n  {MODEL_URL}\n"
            f"and save it as:\n  {dest}"
        ) from exc
    return dest


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _put(cv2, frame, text, org, color=(240, 240, 240), scale=0.62, thick=1):
    """Text with a black outline so it stays readable on any background."""
    shadow = (0, 0, 0)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, shadow, thick + 2, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def draw_hand(cv2, frame, landmarks, pinched):
    h, w = frame.shape[:2]
    pts = [(int(p.x * w), int(p.y * h)) for p in landmarks]
    color = (60, 60, 255) if pinched else (0, 200, 0)  # BGR: red while pinched
    for a, b in core.HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], color, 2)
    for pt in pts:
        cv2.circle(frame, pt, 3, color, -1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(cfg, args):
    try:
        import cv2
        import mediapipe as mp
        import pyautogui
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision as mp_vision
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency: {exc}\n"
            f"Install with:  pip install -r requirements.txt"
        ) from exc

    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = cfg["pyautogui_failsafe"]

    model_path = resolve_model(cfg)
    print(f"Hand-tracking model: {model_path}")

    cap = cv2.VideoCapture(cfg["camera_index"])
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["frame_width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["frame_height"])
    if not cap.isOpened():
        raise SystemExit(
            f"Could not open camera index {cfg['camera_index']}.\n"
            "Close other apps using the webcam (or set camera_index in config.json)."
        )

    landmarker = mp_vision.HandLandmarker.create_from_options(
        mp_vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=cfg["num_hands"],
            min_hand_detection_confidence=cfg["min_detection_confidence"],
            min_hand_presence_confidence=cfg["min_presence_confidence"],
            min_tracking_confidence=cfg["min_tracking_confidence"],
        )
    )

    engine = GestureEngine(cfg, input_mirrored=cfg["mirror_display"])
    detector = YouTubeDetector(cfg["youtube_title_markers"], cfg["youtube_check_interval_ms"])
    show_debug = cfg["show_debug_window"]
    window = "PinchFlick (q=quit, d=debug)"

    # Small, resizable, floating preview -- so the HUD (divider, tap side,
    # status) stays visible even while a fullscreen video covers the screen.
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    try:
        cv2.resizeWindow(window, 480, 360)
        if cfg["always_on_top"]:
            cv2.setWindowProperty(window, cv2.WND_PROP_TOPMOST, 1)
        import ctypes as _ct
        _sw = _ct.windll.user32.GetSystemMetrics(0)
        _sh = _ct.windll.user32.GetSystemMetrics(1)
        cv2.setWindowProperty(window, cv2.WND_PROP_X, max(8, _sw - 500))
        cv2.setWindowProperty(window, cv2.WND_PROP_Y, 16)   # top-right corner
    except Exception:  # noqa: BLE001 -- cosmetic only, never fatal
        pass

    print()
    print("PinchFlick running.")
    print("  scroll : 1 finger = up, 2 fingers = down (works everywhere)")
    print(f"  seek   : double-pinch left/right = -/+{cfg['seek_seconds']}s "
          f"({'YouTube tab must be focused' if cfg['seek_requires_youtube'] else 'anywhere'})")
    print("  keys   : q / ESC = quit, d = toggle debug overlay")
    print("  note   : click your video window first -- seeks only fire while it's focused")
    print()

    stats = {"frames": 0, "hand_frames": 0, "seeks": 0, "scroll_events": 0, "wheel": 0}
    t0 = time.monotonic()
    last_ts = -1
    last_frame_time = time.monotonic()
    fps = 0.0
    block_text, block_until = None, 0.0
    flash_side, flash_until, flash_text, flash_color = None, -1, "", (0, 0, 0)
    smoke_deadline = (time.monotonic() + args.smoke) if args.smoke else None
    stop_reason = "quit key"

    try:
        while True:
            if smoke_deadline is not None and time.monotonic() >= smoke_deadline:
                stop_reason = "smoke test finished"
                break
            ok, frame = cap.read()
            if not ok:
                stop_reason = "camera stopped delivering frames"
                break

            if cfg["mirror_display"]:
                frame = cv2.flip(frame, 1)

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            ts = int((time.monotonic() - t0) * 1000)
            if ts <= last_ts:  # VIDEO mode demands strictly increasing stamps
                ts = last_ts + 1
            last_ts = ts

            result = landmarker.detect_for_video(mp_image, ts)
            landmarks = result.hand_landmarks[0] if result.hand_landmarks else None
            on_youtube, title = detector.check(ts)

            out = engine.frame(landmarks, ts, on_youtube)
            stats["frames"] += 1
            if landmarks:
                stats["hand_frames"] += 1

            # ---- act: scroll (anywhere) --------------------------------
            if out.scroll_clicks:
                try:
                    pyautogui.scroll(out.scroll_clicks)
                except pyautogui.FailSafeException:
                    stop_reason = "pyautogui failsafe (mouse hit a screen corner)"
                    break
                stats["scroll_events"] += 1
                stats["wheel"] += out.scroll_clicks

            # ---- act: seek (YouTube only -- already gated by the engine) -
            if out.seek:
                seq = seek_key_sequence(cfg["seek_seconds"], out.seek, cfg["seek_keys"])
                arrow = "-" if out.seek == "backward" else "+"
                try:
                    for i, key in enumerate(seq):
                        if i:
                            time.sleep(0.05)  # let YouTube register each press
                        pyautogui.press(key)
                except pyautogui.FailSafeException:
                    stop_reason = "pyautogui failsafe (mouse hit a screen corner)"
                    break
                stats["seeks"] += 1
                print(f"[seek] {arrow}{cfg['seek_seconds']}s ({' + '.join(seq)})")

            if out.seek_blocked:
                block_text = f"Seek ignored -- {out.seek_blocked}"
                block_until = ts + 2500
                print(f"[seek blocked] {out.seek_blocked}")

            # ---- visual echo: which side just registered -----------------
            if out.tap and not out.seek:
                flash_side = out.tap
                flash_until = ts + 700
                flash_color = (255, 200, 80)
                flash_text = ("◀ tap again!" if out.tap == "left"
                              else "tap again! ▶")
                print(f"[tap] {out.tap} side -- double-pinch again within "
                      f"{cfg['double_tap_window_ms']}ms")
            if out.seek:
                if out.seek == "backward":
                    flash_side, flash_text = "left", f"◀ {cfg['seek_seconds']}s"
                else:
                    flash_side, flash_text = "right", f"{cfg['seek_seconds']}s ▶"
                flash_until = ts + 700
                flash_color = (90, 230, 90)

            # ---- draw ---------------------------------------------------
            now = time.monotonic()
            fps = 0.9 * fps + 0.1 * (1.0 / max(now - last_frame_time, 1e-6))
            last_frame_time = now

            if landmarks:
                draw_hand(cv2, frame, landmarks, out.pinched)

            h, w = frame.shape[:2]
            cv2.line(frame, (w // 2, 0), (w // 2, h), (130, 130, 130), 1)

            ratio_text = "no hand" if out.ratio is None else f"{out.ratio:.2f}"
            _put(cv2, frame,
                 f"YouTube: {'SEEK ON' if on_youtube else 'off'}    pinch: {ratio_text}"
                 f"{'  <PINCHED>' if out.pinched else ''}    fingers up: {out.fingers}",
                 (10, 26))
            _put(cv2, frame, out.tap_status, (10, 52), color=(255, 220, 120))
            if block_text and ts < block_until:
                _put(cv2, frame, block_text, (10, 78), color=(80, 80, 255))
            if show_debug:
                _put(cv2, frame, f"fps: {fps:.1f}  scroll: {out.scroll_clicks:+d}  "
                                 f"seeks: {stats['seeks']}  window: {title[:44] or '(none)'}",
                     (10, 104), color=(170, 220, 170))
            if flash_text and ts < flash_until:
                scale, thick = 1.1, 3
                (tw, th), _ = cv2.getTextSize(flash_text, cv2.FONT_HERSHEY_SIMPLEX,
                                              scale, thick)
                ty = h // 2
                tx = 14 if flash_side == "left" else w - tw - 14
                cv2.putText(frame, flash_text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                            scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
                cv2.putText(frame, flash_text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
                            scale, flash_color, thick, cv2.LINE_AA)

            short = (f"[{'YT' if on_youtube else '--'}] pinch {ratio_text} | "
                     f"scroll {out.scroll_clicks:+d} | seek x{stats['seeks']}")
            try:
                cv2.setWindowTitle(window, short)
            except Exception:  # noqa: BLE001 -- older OpenCV lacks this
                pass

            cv2.imshow(window, frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("d"):
                show_debug = not show_debug

    except KeyboardInterrupt:
        stop_reason = "Ctrl+C"
    finally:
        cap.release()
        landmarker.close()
        cv2.destroyAllWindows()
        elapsed = max(time.monotonic() - t0, 1e-6)
        print()
        print(f"Stopped: {stop_reason}")
        print(f"Summary: {stats['frames']} frames in {elapsed:.1f}s "
              f"({stats['frames'] / elapsed:.1f} fps), "
              f"hand visible {stats['hand_frames']}/{stats['frames']}, "
              f"seeks fired {stats['seeks']}, "
              f"scroll events {stats['scroll_events']} ({stats['wheel']:+d} wheel clicks)")
        print(f"Last focused window: {detector.title or '(none)'} -> "
              f"YouTube: {'yes' if detector.is_youtube else 'no'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Air-gesture scroller + YouTube-only video seeker.")
    parser.add_argument("--config", metavar="PATH",
                        help="config file (default: config.json next to this script)")
    parser.add_argument("--camera", type=int, metavar="N",
                        help="override camera_index from the config")
    parser.add_argument("--debug", action="store_true",
                        help="start with the debug overlay on")
    parser.add_argument("--smoke", type=float, metavar="SECONDS",
                        help="run unattended for SECONDS then exit (for testing)")
    parser.add_argument("--selftest", action="store_true",
                        help="run logic self-tests (no camera needed) and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        import selftest
        return selftest.main()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.camera is not None:
        cfg["camera_index"] = args.camera
    if args.debug:
        cfg["show_debug_window"] = True
    try:
        return run(cfg, args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- die clean, never a traceback wall
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
