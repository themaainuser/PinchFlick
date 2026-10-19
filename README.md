# PinchFlick

> **2026-10-19 update:** the Rust port - [../pinchflick-rs](../pinchflick-rs)
> ([github.com/themaainuser/PinchFlick-rs](https://github.com/themaainuser/PinchFlick-rs)) -
> is now the **recommended daily driver**: same gestures, same `config.json`,
> verified feature parity (all 25 selftests ported), ~4.6x smaller install,
> steady 30 fps, single exe, no Python needed. This Python build remains the
> verified semantic reference (v1 behavior wins where the two disagree).
> Measured trade-off on the same machine: the Rust build uses ~21% more CPU
> and ~14% more RAM.

One webcam app that merges two earlier projects — **`gesture_scroll`**
(scroll anywhere) and **`air-gesture-youtube-seek`** (seek YouTube videos) —
into a single tool with no browser extension involved.

Both gestures run **at the same time**:

| Gesture | Action | Where it works |
|---|---|---|
| ☝️ One finger up | scroll **up** | **Everywhere** — blogs, docs, games, any window |
| ✌️ Two fingers up | scroll **down** | **Everywhere** |
| 🤏 Double-pinch **left** half of the view | seek **−10s** | **Only while a YouTube tab is focused** |
| 🤏 Double-pinch **right** half of the view | seek **+10s** | **Only while a YouTube tab is focused** |

Double-pinch = thumb + index finger together, apart, together again quickly
(on the same side of the screen).

## Quick start

```bash
cd PinchFlick

# this folder ships with its own .venv (dependencies already installed):
.venv\Scripts\python.exe controller.py

# no environment yet? build one:
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python controller.py
```

First run downloads the `hand_landmarker.task` model into this folder
(one-time, ~8 MB); everything works offline after that.

**Before you start:** `python selftest.py` runs 25 logic tests (no camera
needed) and must say `ALL GREEN`.

### Camera window keys

- `q` / `ESC` — quit
- `d` — toggle the debug overlay (skeleton, live pinch ratio, FPS, window title)

### Left vs right — why a double-pinch sometimes "doesn't register"

- The view is split by a vertical **center line** (always drawn on the
  preview). Each pinch belongs to the half where the thumb+index midpoint
  lands — and **both pinches must land on the same half**, or the pair
  doesn't count.
- Pinches within 4% of the line (`tap_side_dead_zone`) are **refused with a
  message** ("Too close to the middle…") instead of being assigned a random
  side that would silently break the pairing.
- Every registered pinch flashes **◀ / ▶ on that side** of the preview, so
  you always see what the app saw — and the preview floats in the top-right
  corner *above* fullscreen video (`always_on_top`), so you're never
  pinching blind.
- Hand tracking gets weaker at the very edges of the frame — pinch where the
  preview clearly shows your whole hand.

## Why seeks only fire on YouTube (and why they're reliable)

Every ~200 ms the app reads the **foreground window's title**. Browsers label
the active tab `… - YouTube`; if the focused window's title carries that
marker (at the end, or before the browser's own name), seek keystrokes are
allowed — otherwise a double-pinch is silently ignored (the overlay shows
"Seek ignored — not on YouTube").

The seek itself is YouTube's own keyboard shortcuts (`j`/`l` for 10s,
arrow keys for 5s) sent to that same focused window. This is what fixes the
old extension's hit-and-trial behavior:

- the extension poked `document.querySelector("video")`, which often grabbed a
  hidden ad/preview player — the seek went to the wrong element or nowhere;
- here the detection signal and the keystroke target are **the same window**,
  so a seek physically cannot land on the wrong player or fire off YouTube.

Notes:

- Keep the YouTube window focused while seeking. If you click the camera
  window, seek turns off (its title isn't YouTube) — click back to the video.
- Fullscreen (`F`) keeps the same window title, so seeking keeps working.
- Browsers that append their own name — Brave (`… - YouTube - Brave`),
  Firefox (`… - YouTube — Mozilla Firefox`) — are handled automatically:
  the marker must be at the end or followed only by ` - <browser name>`.
- Using something exotic (a site/browser that formats titles very
  differently)? The debug overlay (`d`) shows the exact title being read —
  add its pattern to `youtube_title_markers` in `config.json`.

## No phantom scrolls while pinching

Curling your index finger to pinch could otherwise look like "1 finger up".
Two guards prevent it:

1. Scroll only counts while the hand is clearly **open** (thumb/index ratio
   above `scroll_open_hand_ratio`).
2. Scrolling is suspended while pinched and for
   `scroll_suppress_after_seek_ms` after every seek.

## Configuration (`config.json`)

Every value below is optional in the file — anything you leave out keeps its
default. The app refuses to start with a clear message if a value is invalid.

### Camera / tracking
| Key | Default | Meaning |
|---|---|---|
| `camera_index` | `0` | Which webcam (try `1` if the wrong one opens) |
| `frame_width` / `frame_height` | `640` / `480` | Requested capture size |
| `mirror_display` | `true` | Mirror view so it feels natural; also decides left/right for seeks |
| `num_hands` | `1` | Hands tracked (1 is plenty for these gestures) |
| `min_*_confidence` | `0.6` | Detection/presence/tracking confidence floors |

### Scroll (everywhere)
| Key | Default | Meaning |
|---|---|---|
| `scroll_enabled` | `true` | Master switch |
| `scroll_up_fingers` / `scroll_down_fingers` | `1` / `2` | Finger counts → direction |
| `scroll_speed` | `5` | Wheel clicks per frame while held |
| `scroll_interval_ms` | `0` | `0` = scroll every frame; raise to slow down (e.g. `50`) |
| `scroll_confirm_frames` | `2` | Frames a gesture must hold before firing (anti-flicker) |
| `scroll_invert` | `false` | Flip directions |
| `scroll_open_hand_ratio` | `0.55` | Hand must be at least this "open" to scroll |

### Seek (YouTube only)
| Key | Default | Meaning |
|---|---|---|
| `seek_enabled` | `true` | Master switch |
| `seek_requires_youtube` | `true` | `false` = allow seeking on any focused window |
| `seek_seconds` | `10` | Any multiple of 5 from 5–60 (YouTube moves in 5s steps) |
| `double_tap_window_ms` | `550` | Max gap between the two pinches — raise if your double-pinch is slow |
| `seek_cooldown_ms` | `0` | Min gap between consecutive seeks — `0` = no cooldown |
| `tap_side_dead_zone` | `0.04` | Pinches within this fraction of the center line are refused (with a message) so a middle pinch can't flip sides randomly — `0` = off |
| `scroll_suppress_after_seek_ms` | `500` | Scroll rests this long after a seek |
| `pinch_on_ratio` | `0.35` | Ratio below this = pinched (tighter = fewer accidental pinches) |
| `pinch_off_ratio` | `0.55` | Must open past this before the next pinch counts |
| `seek_keys` | `j`/`l`/`←`/`→` | Remap the YouTube shortcut keys |

### YouTube detection
| Key | Default | Meaning |
|---|---|---|
| `youtube_title_markers` | `["- YouTube", "– YouTube"]` | The title must contain this at the end, or followed only by ` - <browser name>` (e.g. `… - YouTube - Brave`) |
| `youtube_check_interval_ms` | `200` | How often the title is re-read |

### Misc
| Key | Default | Meaning |
|---|---|---|
| `always_on_top` | `true` | Preview floats above fullscreen video so the HUD is always visible |
| `show_debug_window` | `false` | Start with the debug overlay on |
| `pyautogui_failsafe` | `true` | Slam the mouse into a screen corner = instant stop |
| `model_path` | `null` | Custom path to `hand_landmarker.task` (null = auto) |

## Calibrating to your hand

1. Run `python controller.py --debug`.
2. Open your hand flat — the overlay's `pinch:` number should sit well above
   `0.55`. Make a deliberate pinch — it should drop below `0.35`.
3. If pinches fire too easily, lower `pinch_on_ratio` (e.g. `0.30`); if they
   never register, raise it. Keep `pinch_off_ratio` ~`0.15`–`0.20` above it.
4. The status line (`Tap (left) -- tap again quickly to confirm`) tells you
   exactly where you are in the double-tap rhythm.

## Other commands

```bash
python controller.py --selftest        # logic tests, no camera
python controller.py --debug           # start with debug overlay
python controller.py --camera 1        # override camera index
python controller.py --smoke 8         # run 8 s unattended, print a summary
python controller.py --config my.json  # alternate config file
```

## Troubleshooting

- **Camera won't open** — another app (Teams/Zoom/browser) is using it; or
  raise `camera_index`.
- **Scrolling goes the wrong way** — set `scroll_invert: true`.
- **Scroll too fast/slow** — lower/raise `scroll_speed`, or set
  `scroll_interval_ms` to `50` for a steadier crawl.
- **Seek never fires on YouTube** — check the overlay's `YouTube: SEEK ON`
  line (it requires the *video window* to be focused, not the camera window).
  Still off? Press `d` — the overlay prints the exact window title the app
  is reading — and add a matching marker to `youtube_title_markers`.
- **Pinch ratio never drops low enough** — bring your hand closer, improve
  lighting, and read the live ratio in debug to pick thresholds that fit you.
- **Wrong scroll feel in one app** — that app may ignore wheel events while
  unfocused; keep it focused or hover it.
