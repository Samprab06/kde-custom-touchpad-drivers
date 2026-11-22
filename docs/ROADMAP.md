# Roadmap: from Python script to a cross-desktop gesture daemon

Goal: Windows Precision Touchpad behaviour on Linux, on both KDE Plasma and GNOME (Wayland).

| Gesture | Action |
|---|---|
| 3-finger swipe up/down | Smooth volume, with the desktop's OSD |
| 3-finger tap | Play/Pause |
| 4-finger tap | Middle click |
| 4-finger swipe left/right | Switch desktop, with the desktop's own animation |

See [DISSECTION.md](DISSECTION.md) for why the legacy Python daemon is unreliable.

## Why a daemon, not a GNOME extension or KWin plugin

- One codebase for every libinput desktop. Extensions and KWin plugins are per-desktop.
- No rebuilds on desktop upgrades. KWin C++ plugins track KWin internals; GNOME
  extensions break on most GNOME releases.
- libinput has no 4-finger tap and emits the 3-finger tap as a button, so taps need
  raw contact data whichever route is taken.
- libinput Lua plugins (libinput 1.30+) are sandboxed (no D-Bus or commands) and only
  load if the compositor enables them, so they can't carry the whole feature. They
  are worth a later spike for tap detection without grabbing.

Optional per-desktop add-ons (a GNOME extension or KWin script) may come later for
extras like a smoother OSD. The daemon must work without them.

## Architecture

```
/dev/input/eventN ──► tpgd (Rust)
  (grabbed)            ├─ evdev reader ──► tpg-core::Tracker ──► frames in mm, palms flagged
                       ├─ classifier (pure state machine, timerfd settle, tested on recordings)
                       │    ├─ 1–2 fingers       ──► forward to virtual touchpad
                       │    ├─ 4-finger swipe    ──► forward (KDE: native animated desktop switch;
                       │    │                        GNOME: presented as 3 fingers for its native swipe)
                       │    ├─ 4-finger tap      ──► middle click on a virtual mouse
                       │    ├─ 3-finger tap      ──► KEY_PLAYPAUSE
                       │    └─ 3-finger vertical ──► volume backend (+ OSD)
                       ├─ virtual touchpad: state rebuilt each frame (never raw replay),
                       │    copied device ids, tagged as internal
                       └─ desktop backends: KDE (D-Bus), GNOME (keys / optional extension)
```

## Phases

1. **Restructure (done)**
   - Rust workspace: `tpg-core` (pure, no I/O) and `tpgd` (Linux binary).
   - Python moved to `legacy/`; the systemd D-Bus bug in the legacy unit is fixed.
   - `tpg-core::Tracker`: multitouch protocol-B decoder. Atomic frames in mm, palm
     flagging, `SYN_DROPPED` handling, same-slot ID swaps, and inherited axes for
     new contacts.
   - `tpg-core::classify`: picks the touchpad, never a touchscreen or our own clone.
   - `tpgd list` / `tpgd dump`: diagnostics on real hardware.
   - CI: fmt, clippy, tests.
2. **Recording and replay harness**
   - `tpgd record` writes a trace; parse `libinput record` YAML too.
   - A corpus of real XPS traces (taps, swipes, palms, staggered landings) as tests.
3. **Gesture classifier**
   - Settle window, tap slop vs swipe commit in mm, and direction lock with hysteresis.
   - Palm/thumb heuristics (pressure, size, bottom zone).
4. **Virtual devices**
   - Shadow-state touchpad emitter (diff-based, explicit slot/tool/button resets).
   - Virtual mouse with REL axes for the middle click.
   - udev rules: access scoped to touchpads and uinput (no `input` group), plus
     hotplug and suspend/resume.
5. **Actions and desktop backends**
   - Volume: PipeWire/Pulse with change subscription, plus OSD (KDE osdService, or
     per-step volume keys).
   - KDE: kglobalaccel via D-Bus. GNOME: key injection.
   - 4-finger passthrough, and 4→3 finger remap on GNOME.
6. **Packaging**
   - systemd user unit, TOML config with live reload, `tpgd doctor`.
   - AUR / COPR / .deb.
7. **Later**
   - libinput Lua plugin spike; optional GNOME extension / KWin script; Python removal.
