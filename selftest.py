"""
selftest.py -- verifies the whole gesture pipeline without a webcam.

Runs the real GestureEngine against synthetic hand landmarks: pinch
hysteresis, double-tap timing, cooldowns, scroll gating, seek-key
composition, config validation, and YouTube title detection.

    python selftest.py          (or: python controller.py --selftest)

Exit code 0 = all green.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gesture_core import (  # noqa: E402
    ConfigError, DoubleTapSeeker, GestureEngine, PinchState,
    ScrollDriver, count_extended_fingers, get_foreground_window_title,
    hand_size, is_youtube_title, load_config, seek_key_sequence, tap_side,
    validate_config, DEFAULT_CONFIG, INDEX_TIP, THUMB_TIP,
)
import copy  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class Point:
    """Stand-in for MediaPipe's NormalizedLandmark (.x / .y)."""

    def __init__(self, x, y):
        self.x = x
        self.y = y


def make_hand(pinched=False, extended=(False, False, False, False), dx=0.0):
    """Build 21 landmarks.

    extended = (index, middle, ring, pinky) extended upward.
    pinched  = thumb tip touching the index fingertip.
    dx       = shift the whole hand sideways (right = positive).
    """
    idx, mid, ring, pk = extended
    lm = [Point(0.0, 0.0) for _ in range(21)]

    lm[0] = Point(0.50 + dx, 0.75)                      # wrist
    lm[9] = Point(0.50 + dx, 0.55)                      # middle MCP -> hand size 0.20
    lm[5] = Point(0.45 + dx, 0.55)
    lm[13] = Point(0.55 + dx, 0.55)
    lm[17] = Point(0.60 + dx, 0.56)
    lm[1] = Point(0.42 + dx, 0.70)
    lm[2] = Point(0.40 + dx, 0.65)
    lm[3] = Point(0.41 + dx, 0.60)

    # index finger (8 = tip, 6 = pip)
    lm[6] = Point(0.45 + dx, 0.45)
    lm[7] = Point(0.445 + dx, 0.385 if idx else 0.47)
    lm[8] = Point(0.44 + dx, 0.32 if idx else 0.49)

    # middle finger
    lm[10] = Point(0.50 + dx, 0.43)
    lm[11] = Point(0.50 + dx, 0.36 if mid else 0.45)
    lm[12] = Point(0.50 + dx, 0.29 if mid else 0.47)

    # ring finger
    lm[14] = Point(0.55 + dx, 0.45)
    lm[15] = Point(0.555 + dx, 0.39 if ring else 0.47)
    lm[16] = Point(0.56 + dx, 0.33 if ring else 0.49)

    # pinky
    lm[18] = Point(0.60 + dx, 0.47)
    lm[19] = Point(0.605 + dx, 0.42 if pk else 0.49)
    lm[20] = Point(0.61 + dx, 0.37 if pk else 0.51)

    # thumb tip: touching index tip when pinched, relaxed otherwise
    if pinched:
        lm[4] = Point(lm[INDEX_TIP].x + 0.012, lm[INDEX_TIP].y + 0.012)
    else:
        lm[4] = Point(0.38 + dx, 0.66)
    return lm


def cfg(**overrides):
    c = copy.deepcopy(DEFAULT_CONFIG)
    c.update(overrides)
    validate_config(c, source="<test>")
    return c


def expect_error(fn, fragment, label):
    try:
        fn()
    except ConfigError as exc:
        assert fragment in str(exc), f"{label}: expected '{fragment}' in error, got: {exc}"
        return
    raise AssertionError(f"{label}: expected ConfigError, none raised")


# ---------------------------------------------------------------------------
# Landmark math
# ---------------------------------------------------------------------------
@test
def test_pinch_ratio_bounds():
    from gesture_core import pinch_ratio
    assert hand_size(make_hand()) > 0
    assert pinch_ratio(make_hand(pinched=True)) < 0.35     # clearly a pinch
    assert pinch_ratio(make_hand()) > 0.55                 # clearly open
    # ratio is scale-invariant: shrink the whole hand toward its wrist
    tiny = make_hand()
    wx, wy = tiny[0].x, tiny[0].y
    for p in tiny:
        p.x = wx + (p.x - wx) * 0.4
        p.y = wy + (p.y - wy) * 0.4
    assert abs(pinch_ratio(tiny) - pinch_ratio(make_hand())) < 1e-9


@test
def test_count_extended_fingers():
    assert count_extended_fingers(make_hand(extended=(1, 0, 0, 0))) == 1
    assert count_extended_fingers(make_hand(extended=(1, 1, 0, 0))) == 2
    assert count_extended_fingers(make_hand(extended=(1, 1, 1, 1))) == 4
    assert count_extended_fingers(make_hand(extended=(0, 0, 0, 0))) == 0


@test
def test_tap_side_mirrored_and_raw():
    left_hand = make_hand(pinched=True, dx=-0.15)   # mid x ~0.30
    right_hand = make_hand(pinched=True, dx=+0.15)  # mid x ~0.60
    assert tap_side(left_hand, input_mirrored=True) == "left"
    assert tap_side(right_hand, input_mirrored=True) == "right"
    # raw (unflipped) camera space: left/right swap
    assert tap_side(left_hand, input_mirrored=False) == "right"
    assert tap_side(right_hand, input_mirrored=False) == "left"


# ---------------------------------------------------------------------------
# Pinch hysteresis
# ---------------------------------------------------------------------------
@test
def test_pinch_hysteresis_single_edge():
    latch = PinchState(0.35, 0.55)
    assert latch.update(0.60) is False and not latch.active   # open
    assert latch.update(0.34) is True and latch.active        # pinch -> edge
    assert latch.update(0.45) is False and latch.active       # in the gap: stays
    assert latch.update(0.50) is False and latch.active       # still stays
    assert latch.update(0.56) is False and not latch.active   # released
    assert latch.update(0.34) is True and latch.active        # second pinch -> new edge
    assert latch.update(None) is False and not latch.active   # hand lost


# ---------------------------------------------------------------------------
# Double-tap seeker
# ---------------------------------------------------------------------------
@test
def test_double_tap_fires_once():
    seeker = DoubleTapSeeker(window_ms=550, cooldown_ms=600)
    assert seeker.register("left", 0) is None      # first tap
    assert seeker.register("left", 400) == "backward"
    # state reset after firing
    assert seeker.register("left", 500) is None    # new pair starts


@test
def test_double_tap_side_must_match_and_window_must_hold():
    seeker = DoubleTapSeeker(window_ms=550, cooldown_ms=600)
    assert seeker.register("left", 0) is None
    assert seeker.register("right", 300) is None   # side switch: pair restarts on right
    assert seeker.register("right", 400) == "forward"  # 100ms later: right pair fires

    slow = DoubleTapSeeker(window_ms=550, cooldown_ms=600)
    assert slow.register("left", 0) is None
    assert slow.register("left", 700) is None      # 700ms > 550ms window: too slow,
    assert slow.register("left", 1000) == "backward"  # so this pairs with the 700 tap


@test
def test_seek_cooldown_blocks_second_seek():
    seeker = DoubleTapSeeker(window_ms=550, cooldown_ms=600)
    assert seeker.register("left", 0) is None
    assert seeker.register("left", 400) == "backward"   # seek #1 at t=400
    assert seeker.register("right", 700) is None
    assert seeker.register("right", 950) is None        # 950-400=550 < 600 -> blocked
    assert seeker.register("right", 1400) is None       # starts a fresh pair
    assert seeker.register("right", 1750) == "forward"  # 1750-400 > 600 -> allowed


# ---------------------------------------------------------------------------
# Scroll driver
# ---------------------------------------------------------------------------
@test
def test_scroll_driver_direction_and_confirm():
    d = ScrollDriver(speed=5, confirm_frames=2, interval_ms=0,
                     invert=False, up_fingers=1, down_fingers=2)
    assert d.update(1, 0, True) == 0        # frame 1: confirming
    assert d.update(1, 33, True) == 5       # frame 2: stable 1 finger -> up
    assert d.update(1, 66, True) == 5       # held: keeps scrolling
    assert d.update(2, 99, True) == 0       # changed: reconfirming
    assert d.update(2, 132, True) == -5     # stable 2 fingers -> down
    assert d.update(0, 165, True) == 0      # fist: no scroll
    assert d.update(3, 198, True) == 0      # 3 fingers: no scroll


@test
def test_scroll_driver_gate_clears_pending_frames():
    d = ScrollDriver(speed=5, confirm_frames=2, interval_ms=0,
                     invert=False, up_fingers=1, down_fingers=2)
    assert d.update(1, 0, True) == 0        # half-confirmed
    assert d.update(1, 33, False) == 0      # gate closed (pinch!) -> dropped
    assert d.update(1, 66, False) == 0      # still closed
    assert d.update(1, 99, True) == 0       # gate open: starts fresh
    assert d.update(1, 132, True) == 5      # confirmed now


@test
def test_scroll_driver_interval_and_invert():
    d = ScrollDriver(speed=3, confirm_frames=1, interval_ms=100,
                     invert=True, up_fingers=1, down_fingers=2)
    assert d.update(1, 0, True) == -3       # inverted: "up fingers" scrolls down
    assert d.update(1, 50, True) == 0       # throttled by interval
    assert d.update(1, 101, True) == -3     # interval elapsed


# ---------------------------------------------------------------------------
# Seek keys
# ---------------------------------------------------------------------------
@test
def test_seek_key_sequences():
    keys = DEFAULT_CONFIG["seek_keys"]
    assert seek_key_sequence(10, "forward", keys) == ["l"]
    assert seek_key_sequence(10, "backward", keys) == ["j"]
    assert seek_key_sequence(5, "backward", keys) == ["left"]
    assert seek_key_sequence(15, "forward", keys) == ["l", "right"]
    assert seek_key_sequence(20, "backward", keys) == ["j", "j"]
    assert seek_key_sequence(25, "forward", keys) == ["l", "l", "right"]
    for bad in (7, 0, -10, 12, True):
        try:
            seek_key_sequence(bad, "forward", keys)
        except ValueError:
            continue
        raise AssertionError(f"seek_seconds={bad!r} should be rejected")
    try:
        seek_key_sequence(10, "sideways", keys)
    except ValueError:
        return
    raise AssertionError("bad direction should be rejected")


# ---------------------------------------------------------------------------
# YouTube detection
# ---------------------------------------------------------------------------
@test
def test_youtube_title_markers():
    m = DEFAULT_CONFIG["youtube_title_markers"]
    assert is_youtube_title("Funny Cat Video - YouTube", m)
    assert is_youtube_title("Funny Cat Video \u2013 YouTube", m)
    assert is_youtube_title("FUNNY CAT - YOUTUBE", m)      # case-insensitive
    # regression: real-world titles from this user's machine (Brave appends
    # its own name AFTER the YouTube marker)
    assert is_youtube_title(
        "React Performance Optimizations: How to Fix a Slow App - YouTube - Brave", m)
    assert is_youtube_title("Some Video - YouTube - Google Chrome", m)
    assert is_youtube_title("Some Video - YouTube \u2014 Mozilla Firefox", m)
    assert not is_youtube_title("", m)
    assert not is_youtube_title("My blog post about YouTube", m)  # no marker
    assert not is_youtube_title("My rant - YouTube is great", m)  # marker not at tail
    assert not is_youtube_title("GitHub - YouTube.js", m)
    assert not is_youtube_title(None, m)


@test
def test_foreground_title_returns_string():
    title = get_foreground_window_title()
    assert isinstance(title, str)


# ---------------------------------------------------------------------------
# Config loading + validation
# ---------------------------------------------------------------------------
@test
def test_config_defaults_load():
    c = load_config(path=None)  # default path: config.json if present, else defaults
    assert set(c) == set(DEFAULT_CONFIG)
    assert isinstance(c["seek_keys"], dict)


@test
def test_config_partial_merge():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write('{"seek_seconds": 15, "scroll_speed": 8, "seek_keys": {"forward_10": "k"}}')
        path = fh.name
    try:
        c = load_config(path)
        assert c["seek_seconds"] == 15
        assert c["scroll_speed"] == 8
        assert c["seek_keys"]["forward_10"] == "k"          # overridden
        assert c["seek_keys"]["backward_10"] == "j"         # rest kept
        assert c["pinch_on_ratio"] == DEFAULT_CONFIG["pinch_on_ratio"]
    finally:
        os.unlink(path)


@test
def test_config_errors_are_specific():
    expect_error(lambda: load_config(path=os.path.join(tempfile.gettempdir(), "__nope__.json")),
                 "not found", "missing explicit file")

    def with_user_json(text):
        def run():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                fh.write(text)
                p = fh.name
            try:
                load_config(p)
            finally:
                os.unlink(p)
        return run

    expect_error(with_user_json("{not json"), "not valid JSON", "bad JSON")
    expect_error(with_user_json('{"scroll_spd": 5}'), "unknown key", "unknown key")
    expect_error(with_user_json('{"pinch_on_ratio": 0.6}'), "smaller than pinch_off_ratio",
                 "on > off")
    expect_error(with_user_json('{"seek_seconds": 12}'), "multiple of 5", "seek 12")
    expect_error(with_user_json('{"scroll_confirm_frames": 0}'), ">= 1", "confirm 0")
    expect_error(with_user_json('{"scroll_speed": "5"}'), "whole number", "speed str")
    expect_error(with_user_json('{"scroll_up_fingers": 2}'), "must differ", "up == down")
    expect_error(with_user_json('{"mirror_display": "yes"}'), "true or false", "bool str")
    expect_error(with_user_json('{"seek_keys": null}'), "object", "null keys")
    expect_error(with_user_json('{"tap_side_dead_zone": 0.6}'), "must be <= 0.49",
                 "dead zone too wide")


# ---------------------------------------------------------------------------
# Full engine pipeline (the part that used to be unreliable)
# ---------------------------------------------------------------------------
@test
def test_engine_scroll_works_without_youtube():
    """Scrolling must fire on any page -- no YouTube involved."""
    e = GestureEngine(cfg(), input_mirrored=True)
    open_one = make_hand(extended=(1, 0, 0, 0))
    r1 = e.frame(open_one, 0, on_youtube=False)
    r2 = e.frame(open_one, 33, on_youtube=False)
    assert r1.scroll_clicks == 0 and r2.scroll_clicks == 5, (r1, r2)

    open_two = make_hand(extended=(1, 1, 0, 0))
    assert e.frame(open_two, 66, on_youtube=False).scroll_clicks == 0  # changing
    assert e.frame(open_two, 99, on_youtube=False).scroll_clicks == -5


@test
def test_engine_seek_fires_only_on_youtube():
    open_one = make_hand(extended=(1, 0, 0, 0))
    pinch = make_hand(pinched=True, extended=(0, 0, 0, 0))

    # -- not YouTube: double pinch must NOT seek
    e = GestureEngine(cfg(), input_mirrored=True)
    e.frame(open_one, 0, on_youtube=False)
    e.frame(pinch, 100, on_youtube=False)          # tap 1
    e.frame(open_one, 150, on_youtube=False)
    r = e.frame(pinch, 450, on_youtube=False)      # tap 2 within window
    assert r.seek is None, f"seek must not fire off YouTube: {r}"
    assert r.seek_blocked == "not on YouTube"
    assert r.scroll_clicks == 0                    # and no phantom scroll from the pinch

    # -- IS YouTube: same sequence must seek exactly once
    e2 = GestureEngine(cfg(), input_mirrored=True)
    e2.frame(open_one, 0, on_youtube=True)
    e2.frame(pinch, 100, on_youtube=True)
    e2.frame(open_one, 150, on_youtube=True)
    r = e2.frame(pinch, 450, on_youtube=True)
    assert r.seek == "backward", f"expected backward seek, got {r}"


@test
def test_engine_direction_by_frame_side():
    keys_left = make_hand(pinched=True, dx=-0.15)
    keys_right = make_hand(pinched=True, dx=+0.15)
    open_h = make_hand(extended=(1, 0, 0, 0))

    e = GestureEngine(cfg(), input_mirrored=True)
    e.frame(open_h, 0, on_youtube=True)
    e.frame(keys_right, 100, on_youtube=True)
    e.frame(open_h, 150, on_youtube=True)
    r = e.frame(keys_right, 450, on_youtube=True)
    assert r.seek == "forward", r


@test
def test_no_seek_cooldown_by_default_back_to_back_seeks():
    """seek_cooldown_ms defaults to 0: two double-taps in a row both fire."""
    assert DEFAULT_CONFIG["seek_cooldown_ms"] == 0
    e = GestureEngine(cfg(), input_mirrored=True)
    open_h = make_hand(extended=(1, 0, 0, 0))
    right = make_hand(pinched=True, dx=+0.15)

    def pair(t0):
        e.frame(open_h, t0, True)
        e.frame(right, t0 + 100, True)      # tap 1
        e.frame(open_h, t0 + 150, True)
        return e.frame(right, t0 + 450, True)   # tap 2

    r1 = pair(0)
    assert r1.seek == "forward", r1
    # second pair starts the moment the first seek fired -> must seek again
    r2 = pair(460)
    assert r2.seek == "forward", f"cooldown still blocking: {r2}"


@test
def test_center_dead_zone_refuses_ambiguous_pinches():
    """A pinch on the middle line must not be assigned a random side.

    It is refused with a visible message instead, and a decisive left
    pinch right after still registers and seeks normally.
    """
    e = GestureEngine(cfg(), input_mirrored=True)
    open_h = make_hand(extended=(1, 0, 0, 0))
    center = make_hand(pinched=True, dx=0.054)   # pinch midpoint lands on x=0.500

    e.frame(open_h, 0, True)
    r = e.frame(center, 100, True)
    assert r.tap is None and r.seek is None, r
    assert "middle" in r.tap_status, r.tap_status
    e.frame(open_h, 150, True)
    r = e.frame(center, 450, True)
    assert r.tap is None and r.seek is None, f"center pinch paired up: {r}"

    left = make_hand(pinched=True, dx=-0.15)
    e.frame(open_h, 500, True)
    r = e.frame(left, 560, True)
    assert r.tap == "left", r
    e.frame(open_h, 610, True)
    r = e.frame(left, 900, True)
    assert r.seek == "backward" and r.tap == "left", r


@test
def test_engine_no_phantom_scroll_during_or_right_after_pinch():
    """The old failure: curling the index to pinch leaked scroll events."""
    e = GestureEngine(cfg(), input_mirrored=True)
    t = 0

    # steady 1-finger scroll beforehand
    open_one = make_hand(extended=(1, 0, 0, 0))
    e.frame(open_one, t, True); t += 33
    r = e.frame(open_one, t, True); t += 33
    assert r.scroll_clicks == 5

    # pinch #1 -- middle finger happens to be extended (count would be 1!)
    pinch_mid = make_hand(pinched=True, extended=(0, 1, 0, 0))
    r = e.frame(pinch_mid, t, True); t += 33
    assert r.scroll_clicks == 0, f"pinch leaked scroll: {r}"
    assert r.pinched
    r = e.frame(pinch_mid, t, True); t += 33
    assert r.scroll_clicks == 0

    # release, re-pinch within the double-tap window -> seek fires
    e.frame(open_one, t, True); t += 33
    r = e.frame(pinch_mid, t, True)
    assert r.seek == "backward", r
    assert r.scroll_clicks == 0

    # immediately after the seek, an open 1-finger hand must stay quiet
    r = e.frame(open_one, t, True); t += 33
    assert r.scroll_clicks == 0, f"scroll leaked into post-seek window: {r}"
    r = e.frame(open_one, t, True); t += 33
    assert r.scroll_clicks == 0

    # past the suppression window it scrolls again
    t += 600
    e.frame(open_one, t, True); t += 33
    r = e.frame(open_one, t, True)
    assert r.scroll_clicks == 5, f"scroll never recovered: {r}"


@test
def test_engine_no_hand_resets_pinch():
    e = GestureEngine(cfg(), input_mirrored=True)
    pinch = make_hand(pinched=True)
    e.frame(pinch, 0, True)
    assert e.pinch.active
    r = e.frame(None, 33, True)
    assert not e.pinch.active
    assert r.scroll_clicks == 0 and r.seek is None and r.ratio is None
    # a one-frame hand loss doesn't break tap pairing (tap@0 + pinch@100 = seek)
    r = e.frame(pinch, 100, True)
    assert r.seek == "backward", r


@test
def test_engine_scroll_disabled_and_seek_disabled():
    c = cfg(scroll_enabled=False, seek_enabled=True)
    e = GestureEngine(c, input_mirrored=True)
    open_one = make_hand(extended=(1, 0, 0, 0))
    e.frame(open_one, 0, True)
    r = e.frame(open_one, 33, True)
    assert r.scroll_clicks == 0

    c2 = cfg(seek_enabled=False, seek_requires_youtube=True)
    e2 = GestureEngine(c2, input_mirrored=True)
    pinch = make_hand(pinched=True)
    e2.frame(open_one, 0, True)
    e2.frame(pinch, 100, True)
    e2.frame(open_one, 150, True)
    r = e2.frame(pinch, 450, True)
    assert r.seek is None and r.seek_blocked == "seek is disabled in config"


@test
def test_engine_raw_camera_space_flips_side():
    """With an unflipped frame, left/right must be swapped to match the view."""
    e = GestureEngine(cfg(), input_mirrored=False)
    right_screen = make_hand(pinched=True, dx=-0.15)  # raw-left = display-right
    open_h = make_hand(extended=(1, 0, 0, 0))
    e.frame(open_h, 0, True)
    e.frame(right_screen, 100, True)
    e.frame(open_h, 150, True)
    r = e.frame(right_screen, 450, True)
    assert r.seek == "forward", r


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    print(f"Running {len(TESTS)} self-tests...\n")
    failures = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_").replace("_", " ")
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 -- report everything, keep going
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    total = len(TESTS)
    print(f"\n{total - failures}/{total} passed", end="")
    print(" -- ALL GREEN" if failures == 0 else f" -- {failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
