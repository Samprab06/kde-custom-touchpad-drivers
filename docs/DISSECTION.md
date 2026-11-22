# kde-custom-touchpad-drivers: a full dissection

Target behaviour (matching Windows on the Dell XPS 15):

| Gesture | Windows behaviour you want |
|---|---|
| 3-finger swipe up/down | Smooth, continuous volume with the OSD |
| 3-finger tap | Play/Pause |
| 4-finger tap | Middle click |
| 4-finger swipe left/right | Switch virtual desktop |

Short version: the project's design forwards a **delta-encoded** event stream (evdev) after dropping parts of it. That alone explains most of the "works half the time" behaviour. Several smaller bugs stack on top: a broken systemd environment line, a tap-vs-swipe threshold inversion, a stale volume baseline, palms counted as fingers, and timing loss in the replay. Smoothness is capped by design too. Desktop switching can't animate with your fingers from outside KWin, and the daemon turns off the one component (KWin's native gesture) that *can* do that.

---

## Part 1: Why it doesn't work (ranked by impact)

### 1. Fatal design flaw: replaying a partial evdev stream onto the virtual clone

`precision_daemon.py:922-958`, `:651-679`

Evdev is not a stream of snapshots. It is a stream of **changes**, and the kernel drops any event whose value equals the device's current state. That happens on the physical device *and* on your uinput clone. The daemon forwards some frames and discards others (SETTLING buffer dropped, GESTURE sessions discarded, mid-session upgrades). After that, the clone's internal state no longer matches the physical pad, and the physical pad never resends what it thinks it already sent.

Concrete ways this goes wrong:

- **Stale `BTN_TOOL_*` / `BTN_TOUCH` bits.** Two fingers are forwarded (`BTN_TOOL_DOUBLETAP=1`), then a third lands and the session upgrades to GESTURE (`:946-951`). `_release_all_slots()` (`:662`) only sends `TRACKING_ID=-1`. `BTN_TOOL_DOUBLETAP`, `BTN_TOUCH` and possibly `BTN_LEFT` stay at 1 on the clone. The physical device already sent `DOUBLETAP 0` inside the discarded frame, so it will never send it again. libinput counts fingers partly from `BTN_TOOL_*`, so later one-finger touches can be misread (pointer turns into scroll, a tap becomes a right-click, or the cursor freezes) until some later forwarded frame happens to clear the bit. **This is intermittent by nature, which matches "works half the time".**
- **Slot-pointer desync.** `_release_all_slots()` writes `ABS_MT_SLOT n` for every slot and leaves the clone's current slot at the last one (say 2). If the physical pad's current slot is still 0, its next touch sends *no* `ABS_MT_SLOT` event (unchanged from its point of view). The new contact's `TRACKING_ID`/position then land in **slot 2 on the clone**. The release later goes to slot 0, so slot 2 is a **phantom finger stuck down**.
- **Stuck physical click.** If you click the pad with 2 fingers and a palm or third finger touches, `BTN_LEFT=1` was forwarded and the release is swallowed. Result: a stuck drag.

**Fix (structural):** stop replaying raw events. Keep two models, the physical contact state and *what the clone currently believes*. Each frame, **compute the diff and emit it**, always with an explicit `ABS_MT_SLOT` before each slot's events. When the daemon claims a session, explicitly lift every clone contact **and** zero `BTN_TOUCH`, every `BTN_TOOL_*` and `BTN_LEFT`. When it hands a session back, start fresh contacts with new tracking IDs. libevdev (`libevdev_uinput_*`, `libevdev_get_slot_value`) makes this straightforward in C or Rust.

### 2. Buffered frames are collapsed into one frame, and fast taps are discarded

- `_flush_settle_buf()` (`:656-659`) writes every buffered non-SYN event and then sends **one** `SYN_REPORT`. N hardware frames become a single frame. libinput sees a touch-down plus 25-60 ms of motion arriving in one instant, all stamped with the replay time. That causes pointer-acceleration spikes and possible "touch jump" discards. **Fix:** replay each buffered frame with its own `SYN_REPORT` (or, better, re-synthesize as in #1).
- **Fast 1- and 2-finger taps are lost entirely.** When all fingers lift during SETTLING, line `:552` calls `fire_now()`, which queues a decision. A few lines later, `:571-585` cancels the timer, **drains that same queue** and `_settle_buf.clear()`s, so nothing is flushed. With `settle_window_ms: 60` (the value in `config.example.json` and the CHANGELOG), any tap shorter than 60 ms never reaches libinput, so tap-to-click is silently dropped. Quick taps are often 40-90 ms. **Fix:** when peak ≤ 2 in that branch, flush the buffer (frame by frame) instead of clearing it.

### 3. The systemd unit breaks every D-Bus action

`systemd/kde-custom-touchpad.service`:

```ini
Environment=DBUS_SESSION_BUS_ADDRESS=%E{DBUS_SESSION_BUS_ADDRESS}
```

`%E` is not "environment variable" in systemd. It is the **configuration directory specifier** (`$XDG_CONFIG_HOME` for user units). The value becomes something like `/home/you/.config{DBUS_SESSION_BUS_ADDRESS}`, a garbage bus address that **overrides** the correct one the user manager already provides. Every `qdbus6` call then fails as a service: the 4-finger desktop switch, Overview, Show Desktop, and the `reconfigure`/effect reload in `disable_kwin_gestures()`. Run by hand from a terminal, everything works. That fits "half the time". **Fix:** delete the line. User services already inherit the bus address.

The unit also runs with `--verbose`. Every volume frame prints a log line (about 100/s) into journald from the input thread. That adds jitter, and if the loop falls behind, the kernel emits `SYN_DROPPED`, which the daemon ignores (#9).

### 4. Tap and swipe thresholds are inverted, so taps turn into micro-swipes

`DEFAULT_CONFIG`: `swipe_trigger_threshold = 60`, `tap_threshold = 200` (raw units).

A swipe is recognised after the *centroid* moves 60 units, but a tap is only cancelled after a *finger* drifts 200. The swipe check runs every frame, and `_check_gesture_trigger` (`:807-810`) sets `tap_eligible = False` when it fires. So a 3-finger tap with about 2-3 mm of roll (normal, especially while fingers land and lift unevenly) becomes a **vertical swipe**. The volume nudges slightly and **play/pause doesn't fire**. This is likely the main reason 3-finger taps "don't work half the time".

Also, all thresholds are in **raw device units**, not millimetres. Your XPS pad reports roughly 3000 units across about 150 mm, so roughly 20 units/mm. 60 units is about 3 mm and 120 units (desktop switch / track skip) is about 6 mm, which is very easy to trigger by accident. libinput normalises everything with `absinfo.resolution`; the daemon should too.

**Fix:** work in mm. Use a tap slop of about 3-4 mm and a swipe commit of about 8-10 mm or more. Decide the tap-vs-swipe question by "did anything exceed slop before lift", not by racing two thresholds.

### 5. Volume: stale baseline, no OSD, unmute ignored, 150 % cap

`VolumeWorker` (`:220-287`):

- `_get_system_volume()` runs **once, at thread start** (`:246`). If you change the volume afterwards with the keyboard keys, Plasma or an app, `current_volume` is stale. The next swipe starts from the old value and **jumps**.
- `wpctl set-volume` doesn't show Plasma's volume OSD, so you get no visual feedback. A lot of what makes the Windows gesture *feel* smooth is the flyout tracking your finger.
- Muted sinks stay muted (Windows unmutes when you raise the volume).
- Clamped to 1.5 (150 %), so you can swipe into distortion.
- One `fork+exec` of `wpctl` per update, at most about 30 updates/s with 30 ms coalescing plus process start-up. Audible and visible stepping.
- The velocity curve: `velocity_threshold=300 units/s` is about 15 mm/s, so nearly every real swipe saturates `v_ratio` at 4. The "acceleration" is effectively a constant ×6.3 multiplier with noise from the per-frame `dt` EMA.
- The centroid jumps when the finger count changes mid-swipe. `prev_avg_x/y` aren't reset when one finger briefly drops out during a triggered swipe, so the next frame's `dy` is the jump between two different finger sets (`:761-762`), which causes a volume spike.

**Fix options (best first on KDE):**
1. Emit `KEY_VOLUMEUP`/`KEY_VOLUMEDOWN` from the uinput keyboard **once per N mm of travel** (for example 1 step per 3 mm), with Plasma's volume step set to 1-2 %. You get Plasma's OSD, unmute-on-raise, the correct default sink and the 100 % cap for free. This is close to what Windows actually does (discrete steps plus a flyout).
2. Talk to PipeWire/Pulse natively (libpulse via pipewire-pulse, or libwireplumber) with a **change subscription**, so the baseline is always current. Then call `org.kde.osdService.volumeChanged(int)` on `org.kde.plasmashell /org/kde/osdService` for the OSD.

### 6. Palms and thumbs count as fingers

The daemon ignores `ABS_MT_TOOL_TYPE` (hid-multitouch maps the PTP *Confidence* bit to `MT_TOOL_PALM`), pressure and touch size. On an XPS 15-size pad:
- 2-finger scroll + resting palm = 3 "fingers", so the scroll is hijacked into a gesture (mid-session upgrade) and dies.
- A 3-finger swipe + thumb = 4, so you get the wrong gesture.

libinput does palm, thumb and edge rejection for you. Once you grab the device you take that job on, and none of it is implemented.

### 7. The virtual clone loses libinput's device identity

`_initialize_virtual_touchpad()` (`:403-428`) copies capabilities and props but **not** vendor/product/bustype (python-evdev defaults to `BUS_USB`, vid/pid 0x0001):
- libinput treats it as an **external USB touchpad**. **Disable-while-typing only pairs internal keyboards with internal touchpads**, so palm-while-typing protection is gone.
- Dell/ELAN-specific libinput **quirks** (matched on name/vid/pid/DMI) don't apply.
- KWin stores touchpad settings per device. The clone is a new device with its own settings (tap-to-click, natural scrolling, speed). Whatever you configured for the real "DELL…Touchpad" isn't used.

**Fix:** clone ids (`UInput.from_device` / `libevdev_uinput_create_from_device`), and add a udev rule that sets `ID_INPUT_TOUCHPAD_INTEGRATION=internal` on the clone. Don't create a clone with a name identical to the real pad, or KWin's config keys collide.

### 8. Middle click may not arrive at all

`BTN_MIDDLE` is sent from "KDE Custom Touchpad Virtual Keyboard", which has `EV_KEY` only and no `REL_X/REL_Y`. udev's `input_id` builtin only tags a device `ID_INPUT_MOUSE` if it has relative axes. libinput then likely gives it keyboard capability only, and pointer buttons on a non-pointer device may be dropped. I haven't run this on your machine; check with `sudo libinput debug-events` (does a `POINTER_BUTTON` appear for that device?). **Fix:** put the buttons on a separate virtual *mouse* that declares `REL_X`, `REL_Y` and `REL_WHEEL` (this is what ydotool does).

Also, press and release are written in the same `SYN_REPORT` (`:897-899`). Most consumers cope, but a ~5-10 ms gap with two frames is safer.

### 9. Robustness gaps

- `SYN_DROPPED` is ignored. After an overflow (likely with `--verbose`), slot state is wrong until every finger lifts. You must resync with `EVIOCGMTSLOTS`/`EVIOCGKEY`; libevdev does this automatically.
- `find_touchpad()` (`:980-992`) returns the **first** device with MT axes. On OLED/touchscreen XPS models that can be the **touchscreen**. Filter on `INPUT_PROP_POINTER`/`BUTTONPAD` and `!INPUT_PROP_DIRECT`, and skip your own clone by name.
- The settle decision only applies when the *next hardware frame* arrives (the queue is drained at the top of `_process_frame`), and every touch spawns a new OS thread (`threading.Timer`). Use a `timerfd` in the same `poll()` loop instead.
- If a new contact lands at the exact X (or Y) of the slot's previous contact, the kernel doesn't resend that axis. `start_x` stays `None` (`:469-474`) and that finger is ignored in centroid/tap maths. Carry the old position over instead of resetting it to `None`.
- After suspend/resume the i2c-hid device may re-enumerate. The daemon crashes and restarts after 3 s, holding a grab on a dead fd in the meantime. You need hotplug via udev monitor.
- `_release_all_slots()` sends `SYN_REPORT` twice (harmless but sloppy).
- `disable_kwin_gestures()` rewrites `~/.config/kwinrc` in place, non-atomically, while KWin may be writing it, and never restores it on uninstall. I couldn't confirm that `VirtualDesktopSwipeGestures`, `TouchpadShortcutsPluginEnabled`, etc. are real KWin 6 keys. In grab mode it doesn't matter, because KWin never receives 3/4-finger frames anyway.

### 10. Docs and config disagree with each other (and with what you want)

| Item | `DEFAULT_CONFIG` | `config.example.json` | README |
|---|---|---|---|
| 3-finger tap | `BTN_MIDDLE` | `KEY_PLAYPAUSE` | table: middle; diagram: play/pause |
| 4-finger tap | `command:true` (nothing) | `BTN_MIDDLE` | diagram: middle |
| settle window | 25 ms | 60 ms | (CHANGELOG: 60 ms "Windows PTP spec") |
| tap_threshold | 200 | 100 | 100 |

So what you get depends on whether you copied the example file. Without it, **3-finger tap = middle click and 4-finger tap does nothing**, the opposite of the Windows layout you want. (I also couldn't find any "60 ms settle" figure in Microsoft's PTP spec.)

---

## Part 2: Why Windows feels smooth and Linux doesn't (the real blockers)

| | Windows Precision Touchpad | Linux / Wayland |
|---|---|---|
| Who recognises gestures | The OS input stack plus Explorer shell: one owner, first-party | libinput, **inside the compositor process** |
| Who can bind gestures | OS setting (Settings → Touchpad → Three-finger gestures → "Change audio and volume") | Only the compositor. Plasma 6 has no UI to rebind 3-finger swipes, and KDE's upstream gesture-customisation work is still in progress |
| Third-party access to global gestures | Supported by design | **Deliberately impossible** for normal clients (Wayland security model). Only workarounds: run inside the compositor, or read/grab raw evdev |
| Palm / confidence | Firmware confidence bit honoured by OS | Exposed as `MT_TOOL_PALM`, but **you** must handle it once you grab |
| Volume feedback | Native flyout that tracks the finger | No API for "show the OSD at value X" outside Plasma's own D-Bus OSD service |
| Desktop switching | Animation follows your fingers 1:1 | KWin does this natively for its **own** 4-finger swipe, but there's **no external API to drive the animation progress**. From outside you can only fire "switch now" (a discrete jump) |
| 4-finger tap | Supported | **libinput has no 4-finger tap.** Its tap engine does 1/2/3 fingers only, and 3-finger tap is emitted as a middle *button*, not a bindable gesture |

What this means:
1. **Smooth desktop switching is something you are currently disabling.** If you let KWin see 4-finger swipes, you get the Windows-like animated switch for free.
2. **4-finger tap and "3-finger tap → play/pause" can't be done through libinput.** They need raw evdev access or a KWin plugin. That's the only legitimate reason to touch evdev at all.
3. **Smooth volume needs continuous deltas.** You can get them either from evdev or from inside KWin, where KWin's QML `SwipeGestureHandler` exposes a `progress` 0.0-1.0 property. Bounded progress is awkward for an unbounded volume knob, and cancel semantics were recently reworked (KWin MR 5989).

---

## Part 3: Is it portable to GNOME?

**The evdev layer (grab, clone, recognise, uinput) is compositor-agnostic.** Mutter uses libinput too, so the core works unchanged once the bugs above are fixed. KDE-specific pieces that need a backend abstraction:

| Piece | KDE | GNOME |
|---|---|---|
| Desktop switch / overview | `kglobalaccel invokeShortcut` over D-Bus | No public "invoke shortcut" D-Bus (`org.gnome.Shell.Eval` is disabled since GNOME 41). Inject the keyboard shortcut via uinput (`Super+PageUp/PageDown`, `Super`), or ship a small Shell extension exposing a D-Bus method |
| Native gestures to suppress | kwinrc (questionable) | Built-in 3-finger swipes are hard-coded in gnome-shell. In grab mode they never see 3+ fingers anyway |
| Volume OSD | `org.kde.osdService` / `KEY_VOLUME*` | `KEY_VOLUME*` (step via `org.gnome.settings-daemon.plugins.media-keys volume-step`) or an extension calling `Main.osdWindowManager` |
| Volume backend | PipeWire: same | PipeWire: same |

GNOME is arguably **more** hackable for the smooth parts. A Shell extension runs *inside* the compositor and receives `Clutter.TouchpadSwipeEvent` with raw dx/dy and finger count (this is how "Gesture Improvements"-style extensions work). It can drive the real workspace animation and the real volume OSD. The cost is that the extension API breaks most GNOME releases. 4-finger tap still requires evdev on both desktops.

---

## Part 4: Turning it into a real daemon

### Recommended architecture: "claim only what the compositor can't do"

```
                 ┌────────────────── touchpad-gestured (Rust, system or user service) ─────────────────┐
/dev/input/evN ─►│ libevdev reader ─► contact model (mm, palm/thumb filter, SYN_DROPPED resync)        │
  (grabbed)      │        │                                                                             │
                 │        ▼                                                                             │
                 │  session classifier (pure state machine, timerfd settle, unit-tested on recordings)  │
                 │     ├─ 1-2 fingers ──────────────► forward ──┐                                       │
                 │     ├─ 4-finger swipe ───────────► forward ──┼─► state-diff emitter ─► uinput clone ─┼─► libinput/KWin
                 │     │   (KWin animates desktops 1:1)         │   (correct ids, internal-tagged)      │   (native smooth
                 │     ├─ 4-finger tap (observed) ──► middle click on virtual mouse (REL axes)          │    desktop switch)
                 │     ├─ 3-finger tap ─────────────► KEY_PLAYPAUSE (or MPRIS D-Bus)                    │
                 │     └─ 3-finger vertical ────────► volume backend (PipeWire native + OSD backend)    │
                 │  DE backends: kde.rs (kglobalaccel/osdService via zbus) | gnome.rs (keys/extension) │
                 │  config: TOML, hot-reload via inotify; D-Bus control iface (pause/resume/status)    │
                 └─────────────────────────────────────────────────────────────────────────────────────┘
```

Key decisions:

1. **Forward 4-finger sessions to the compositor instead of claiming them.** KWin then does native, finger-tracking desktop switching (exactly the Windows feel). libinput has no 4-finger tap, so you can *observe* the same session for a tap and emit middle click without double-firing. Staggered arrival (3 fingers, then a 4th) works because the state-diff emitter can start fresh contacts on the clone at any time.
2. **Language: Rust** (`evdev`/`input-linux` or libevdev bindings, `zbus`, `pipewire-rs`/libpulse) or C with libevdev + sd-bus. You get a single static binary, no GIL or GC pauses, and an `epoll` loop with `timerfd` + `signalfd` + udev monitor (hotplug, suspend/resume).
3. **Never replay raw events.** Use the shadow-state diff emitter (#1).
4. **Units in mm, real palm/thumb rejection** (`MT_TOOL_PALM`, `ABS_MT_PRESSURE`/`TOUCH_MAJOR` thresholds, bottom thumb zone).
5. **Test harness:** record failing gestures with `libinput record` or `evemu-record` and replay them in unit tests against the classifier. This is the single biggest quality lever. The tap/swipe/stagger bugs above would all have been caught.
6. **Permissions: stop using the `input` group.** It lets *every* process you run keylog every keyboard. Use udev `TAG+="uaccess"` scoped to touchpads (`ENV{ID_INPUT_TOUCHPAD}=="1"`) and `/dev/uinput`, the same approach Steam uses for controllers. Alternatively run a hardened system service (`DeviceAllow=`, `ProtectSystem=strict`, `PrivateNetwork=yes`, `NoNewPrivileges=yes`) that only opens the touchpad and talks to a user-side agent over D-Bus for DE actions.
7. **Packaging:** binary + `systemd --user` unit (no `%E` bug) + udev rules + libinput quirk/udev rule for the clone, shipped as AUR/COPR/.deb. Plus an optional KWin QML script or GNOME extension for the OSD/animation niceties.

### Alternative with no grab at all (KDE only)

Write a **KWin C++ plugin**. Prior art: the third-party **InputActions** (formerly kwin-gestures) KWin plugin for custom touchpad gestures. Inside KWin you get libinput's gesture recognition (palm rejection, mm units, stagger handling) for free, plus progress callbacks. The downside is that you rebuild for every Plasma release, and you still can't get a 4-finger *tap* without patching libinput or watching hold-gesture begin/end events. Before you build your own, check whether InputActions already covers 3-finger volume + taps.

---

## Part 5: Quick wins if you keep the Python version for now

1. Delete the `DBUS_SESSION_BUS_ADDRESS=%E{…}` line and `--verbose` from the unit.
2. In `_release_all_slots` / upgrade paths: also write `BTN_TOUCH 0`, every `BTN_TOOL_* 0` and `BTN_LEFT 0`, then restore `ABS_MT_SLOT` to `self.current_slot`.
3. Flush (not clear) the buffer when all fingers lift during SETTLING with peak ≤ 2. Emit one `SYN_REPORT` per buffered frame.
4. Make `swipe_trigger_threshold` larger than the tap slop (or decide taps first). Convert to mm via `device.absinfo(ABS_MT_POSITION_X).resolution`.
5. Re-read the volume at the start of each 3-finger session, or better, use `KEY_VOLUMEUP/DOWN` per N mm with a 1-2 % Plasma step.
6. Ignore slots whose `ABS_MT_TOOL_TYPE == MT_TOOL_PALM`.
7. Put `BTN_MIDDLE` on a virtual device with `REL_X/REL_Y`.
8. Set `DEFAULT_CONFIG` to your actual layout: 3-tap → `KEY_PLAYPAUSE`, 4-tap → `BTN_MIDDLE`.
9. Stop grabbing 4-finger sessions and re-enable KWin's native 4-finger desktop swipe for the smooth animation.

### Sources
- [KWin SwipeGestureHandler QML type](https://api.kde.org/qml-org-kde-kwin-swipegesturehandler.html), [PinchGestureHandler](https://api.kde.org/qml-org-kde-kwin-pinchgesturehandler.html), [KWin MR 5989 (gesture handler port)](https://invent.kde.org/plasma/kwin/-/merge_requests/5989)
- [InputActions on AUR](https://aur.archlinux.org/packages/input-actions), [KDE "Gestures in Graz, and beyond"](https://discuss.kde.org/t/gestures-in-graz-and-beyond/46717), [KDE bug 402857: configurable touchpad gestures](https://bugs.kde.org/show_bug.cgi?id=402857)
