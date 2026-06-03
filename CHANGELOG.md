# Changelog

All notable changes to this project will be documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Fixed
- **3/4-finger taps misidentified as 2-finger left-clicks** — hardware touchpad contacts do not arrive simultaneously; the 2nd and 3rd fingers of a 3-finger tap typically register 10-40 ms after the 1st. Without mitigation, the daemon forwarded the partial 1-finger and 2-finger contact state to the compositor before the full count was known, causing false left-clicks. Implemented a **contact settling window** (default 60 ms, matches Windows PTP reference spec): all raw events from the first contact frame are buffered and held. When the window expires, the peak finger count decides: ≥3 fingers → GESTURE mode (buffer discarded, compositor never sees partial state); ≤2 fingers → POINTER mode (buffer flushed to compositor intact). This completely eliminates false left-click leakage from multi-finger taps.

### Added
- `settle_window_ms` configuration key (default: 60 ms) — tune if your hardware reports fingers unusually slowly or quickly.
- `PassthroughState` state machine enum (`SETTLING` / `POINTER` / `GESTURE`) replacing the previous boolean `forwarding` flag, making state transitions explicit and auditable in verbose logs.


- **4-finger tap (BTN_MIDDLE) was silently dropped** — the physical touchpad does not advertise `BTN_MIDDLE` in its evdev capability set, so routing the event to the virtual touchpad clone (which mirrors those capabilities exactly) caused the kernel to discard it. `BTN_MIDDLE` and all other button/key injections now always go to the dedicated virtual keyboard uinput device, which explicitly declares these capabilities.
- **4-finger tap detection was overly strict** — a separate, more generous `four_finger_tap_duration_limit` (default 400 ms) is now applied to 4-finger taps, accounting for the physical difficulty of landing four contacts simultaneously within the same tight window as a 3-finger tap.

### Changed
- **Gesture mapping updated** to the preferred layout:
  - 3-finger tap → Play / Pause (`KEY_PLAYPAUSE`)
  - 3-finger swipe left/right → Previous / Next track
  - 3-finger swipe up/down → Volume control (velocity-scaled, unchanged)
  - 4-finger tap → Middle mouse button (`BTN_MIDDLE`)
  - 4-finger swipe up → Toggle Overview
  - 4-finger swipe down → Show Desktop
  - 4-finger swipe left/right → Switch to Previous / Next Desktop
- `trigger_action()` refactored to always route `key:` actions through `self.uinput`. Removed the separate `is_mouse_btn` branch that incorrectly directed `BTN_*` events to the virtual touchpad.
- `_process_frame()` split into focused helpers (`_on_gesture_end`, `_track_fingers`, `_check_gesture_trigger`) for readability and testability.
- `_release_all_slots()` renamed with underscore convention; `_handle_passthrough()` extracted from inline logic.
- Systemd service unit no longer hard-codes `/home/sam`; uses the `%h` specifier for portability.
- `DBUS_SESSION_BUS_ADDRESS` added to the service environment so `qdbus6` calls reach KWin correctly when run as a user service.

### Added
- `four_finger_tap_duration_limit` configuration key (default: 0.40 s).
- Hardware compatibility fallback layout documented in README for touchpads that cannot reliably register 4 simultaneous contacts.
- `CHANGELOG.md` (this file).
- `config.example.json` — annotated example configuration file.
- `LICENSE` (MIT).

---

## [0.1.0] — Initial release

- Velocity-scaled 3-finger vertical swipe for volume control with exponential acceleration curve.
- Asynchronous `VolumeWorker` thread that coalesces high-frequency deltas onto a 30 ms cadence.
- Discrete 4-finger swipe for desktop switching and overview via KWin D-Bus shortcuts.
- Discrete 3-finger horizontal swipe for audio track navigation.
- 3-finger and 4-finger tap detection with configurable drift threshold and duration limit.
- Exclusive grab mode with transparent virtual touchpad clone for 1/2-finger pass-through.
- Automatic KWin native gesture suppression via kwinrc patching on daemon startup.
- Auto-detection of multitouch touchpad via evdev capability scanning.
- JSON configuration file support with deep-merge for `shortcuts` overrides.
