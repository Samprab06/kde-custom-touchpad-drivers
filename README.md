# KDE Custom Touchpad Drivers

A low-latency, custom touchpad gesture daemon for **KDE Plasma 6 on Wayland**.  
It supersedes KDE's built-in gesture engine and provides velocity-scaled volume control, discrete media and desktop shortcuts, and robust multi-finger tap detection matching the feel of Windows Precision Touchpad.

---

##  Default Gesture Bindings

| Gesture | Action |
|---|---|
| 3-finger swipe **up / down** | Volume **+/−** (velocity-scaled) |
| 3-finger swipe **left / right** | Previous / Next track |
| 3-finger **tap** | Middle mouse button click |
| 3-finger **pinch** in or out | Play / Pause |
| 4-finger swipe **up** | Toggle Overview |
| 4-finger swipe **down** | Show Desktop |
| 4-finger swipe **left / right** | Switch to Previous / Next Desktop |

> [!NOTE]
> Some touchpad panels (particularly elan / ELAN32AA) have hardware limits on 4-finger simultaneous contact registration. If the 4-finger tap is unreliable on your device, see the **Hardware Compatibility** section below for a fallback layout.

---

##  Key Technical Features

- **Velocity-scaled volume control** — tracks finger centroid speed and applies a power-based acceleration curve.  
  Slow deliberate swipes → ±0.5% adjustments. Fast flicks → up to ±8% per frame.
- **Asynchronous volume coalescing** — high-frequency swipe deltas are buffered and committed on a 30 ms background thread, keeping the main event loop at full hardware frequency (~1000 Hz).
- **Discrete swipe lock** — desktop switch and song skip actions fire exactly once per touch session after crossing a spatial threshold, preventing double-triggers and jitter.
- **Virtual uinput keyboard** — emits `BTN_MIDDLE` and media keys via a dedicated virtual device that explicitly advertises these capabilities, ensuring events always reach the compositor regardless of grab state.
- **Virtual touchpad clone** — in grab mode, 1- and 2-finger interactions (cursor, scrolling, physical clicks) are transparently forwarded through a virtual touchpad mirror, so the daemon is invisible to normal use.
- **Automatic KWin suppression** — on startup, patches `~/.config/kwinrc` to disable KWin's own gesture recogniser, then hot-reloads the affected effects, preventing double-firing.

---

##  Configuration

The daemon loads configuration from, in order:

1. `--config <file>` CLI argument  
2. `~/.config/kde-custom-touchpad.json` (per-user)  
3. `/etc/kde-custom-touchpad.json` (system-wide)

A full example with all available keys:

```json
{
  "swipe_trigger_threshold": 60.0,
  "discrete_trigger_threshold": 120.0,
  "pinch_trigger_threshold": 80.0,
  "tap_threshold": 100.0,
  "tap_duration_limit": 0.30,
  "four_finger_tap_duration_limit": 0.40,
  "velocity_threshold": 300.0,
  "velocity_power": 1.2,
  "base_volume_step": 0.005,
  "volume_coalesce_ms": 30,
  "shortcuts": {
    "three_finger_tap":  "key:BTN_MIDDLE",
    "swipe3_left":       "key:KEY_PREVIOUSSONG",
    "swipe3_right":      "key:KEY_NEXTSONG",
    "pinch3_in":         "key:KEY_PLAYPAUSE",
    "pinch3_out":        "key:KEY_PLAYPAUSE",
    "four_finger_tap":   "command:true",
    "swipe4_up":         "shortcut:Overview",
    "swipe4_down":       "shortcut:Show Desktop",
    "swipe4_left":       "shortcut:Switch to Previous Desktop",
    "swipe4_right":      "shortcut:Switch to Next Desktop"
  }
}
```

### Action formats

| Prefix | Example | Description |
|---|---|---|
| `key:<ECODE>` | `key:BTN_MIDDLE` | Inject a virtual key press/release |
| `shortcut:<NAME>` | `shortcut:Overview` | Invoke a KWin global shortcut via D-Bus |
| `dbus:<SVC PATH IFACE.METHOD>` | `dbus:org.kde.KWin /KWin reconfigure` | Raw `qdbus6` call |
| `command:<CMD>` | `command:playerctl play-pause` | Run an arbitrary shell command |

---

##  Hardware Compatibility

### 4-Finger Tap Fallback Layout

If your touchpad cannot reliably register 4 simultaneous contacts (common on some ELAN/Synaptics panels), use the following config to swap middle-mouse-button to the 3-finger tap and move play/pause to a 3-finger pinch:

```json
{
  "shortcuts": {
    "three_finger_tap": "key:BTN_MIDDLE",
    "pinch3_in":        "key:KEY_PLAYPAUSE",
    "pinch3_out":       "command:true",
    "four_finger_tap":  "command:true"
  }
}
```

Save this as `~/.config/kde-custom-touchpad.json` and restart the service.

---

##  Installation

### Requirements

- Python 3.9+
- `python-evdev` >= 1.7.0 (installed automatically)
- `wpctl` (part of PipeWire, for volume control)
- `qdbus6` (part of KDE Frameworks, for KWin shortcuts)

### 1. Run the installer

```bash
./install.sh
```

This will:
- Create a Python virtual environment in `.venv/`
- Install `python-evdev`
- Copy the systemd user service to `~/.config/systemd/user/`
- Enable the service to auto-start with your KDE session

### 2. Configure permissions

The daemon needs access to `/dev/input/event*` (read) and `/dev/uinput` (write).

**Add your user to the `input` group:**
```bash
sudo usermod -aG input $USER
```

**Create a udev rule for `/dev/uinput`:**
```bash
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Log out and back in (or reboot) for group membership to take effect.

### 3. Start the daemon

```bash
systemctl --user daemon-reload
systemctl --user enable --now kde-custom-touchpad.service
```

### Logs and debugging

```bash
# Live log stream
journalctl --user -u kde-custom-touchpad.service -f

# One-shot verbose run for testing (stop with Ctrl+C)
.venv/bin/python precision_daemon.py --verbose
```

---

##  Architecture

```
Physical Touchpad (/dev/input/eventN)
         │
         │ (exclusive grab in grab mode)
         ▼
  PrecisionDaemon.process_event()
         │
         ├─ 1/2 fingers → Virtual Touchpad Clone (uinput)
         │                  ↳ Compositor sees normal pointer/scroll
         │
         └─ 3/4 fingers → Gesture Classifier
                              │
                              ├─ Swipe vertical 3f  → VolumeWorker (background thread)
                              ├─ Swipe horizontal 3f → KEY_PREVIOUSSONG / KEY_NEXTSONG
                              ├─ Tap 3f             → KEY_PLAYPAUSE
                              ├─ Swipe 4f           → KWin shortcut via qdbus6
                              └─ Tap 4f             → BTN_MIDDLE (Virtual Keyboard uinput)
```

### Why a separate virtual keyboard for BTN_MIDDLE?

The physical touchpad advertises `BTN_LEFT` but **not** `BTN_MIDDLE` in its evdev capabilities.  The virtual touchpad clone mirrors these capabilities exactly, so sending `BTN_MIDDLE` to it silently drops the event — the kernel rejects events for codes not declared in the device's capability set.

By registering `BTN_MIDDLE` on a dedicated virtual keyboard device (`KDE Custom Touchpad Virtual Keyboard`), the compositor always receives middle-click events regardless of whether the virtual touchpad is blocked for gesture interception.

---

##  License

MIT — see [LICENSE](LICENSE).
