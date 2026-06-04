#!/usr/bin/env python3
"""
KDE Custom Touchpad Daemon
==========================
A low-latency, custom touchpad gesture daemon for KDE Plasma 6 on Wayland.
Provides velocity-based volume control, discrete media/desktop shortcuts,
and multi-finger tap detection — superseding KDE's built-in gesture engine.

Gesture Bindings (default):
  3-finger swipe up/down    → Volume control (velocity-scaled)
  3-finger swipe left/right → Previous / Next track
  3-finger tap              → Middle mouse button click
  3-finger pinch in/out     → Play / Pause
  4-finger swipe up         → Toggle Overview
  4-finger swipe down       → Show Desktop
  4-finger swipe left/right → Switch to Previous / Next Desktop
  4-finger tap              → (unbound by default)

Architecture: Dedicated Settle-Timer Thread
-------------------------------------------
Physical touchpad hardware reports multi-finger contacts staggered across
several hardware scan frames.  The 2nd and 3rd fingers of a 3-finger tap
typically arrive 10-40 ms after the 1st.  Without mitigation the daemon
forwards the initial 1-finger contact frame to the compositor as a pointer
press before the 3rd finger is known, causing false left-clicks.

This version fixes that using a dedicated background timer thread and a
lock-free command queue, avoiding the perceptible lag of the earlier
synchronous settling-window approach:

  1. First contact → SettleTimer.arm(25 ms).  Events are buffered.
  2. Timer fires on background thread → posts 'SETTLE_RESOLVED' to queue.
  3. _process_frame() drains the queue at the top of each frame.
  4. Decision: peak ≥ 3 → GESTURE mode (buffer discarded).
                peak ≤ 2 → POINTER mode (buffer flushed to compositor).
  5. If 3+ fingers arrive BEFORE the timer fires → cancel timer, switch to
     GESTURE mode immediately (zero additional delay in that path).
  6. If all fingers lift BEFORE the timer fires → cancel timer, decide from
     peak count immediately (handles very-fast taps).

25 ms is below the ~33 ms inter-frame period at 30 fps and well below the
~50 ms human perception threshold for cursor latency, so pointer interactions
feel instantaneous while multi-finger taps never produce false clicks.
"""

import os
import sys
import time
import json
import queue
import argparse
import subprocess
import threading
import signal
from typing import Callable, Dict, Any, List, Optional, Tuple

try:
    import evdev
    from evdev import ecodes as e
except ImportError:
    print("Error: 'evdev' module is not installed. Please run ./install.sh", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    # ---- Gesture thresholds ----

    # Distance (raw touchpad units) the centroid must travel before a swipe
    # is recognised.
    "swipe_trigger_threshold": 60.0,

    # Distance the centroid must travel to fire a discrete (one-shot) action
    # such as a desktop switch or track skip.
    "discrete_trigger_threshold": 120.0,

    # Change in inter-finger span (raw units) required to trigger a pinch.
    "pinch_trigger_threshold": 80.0,

    # Maximum per-finger drift (raw units) allowed while still counting as a tap.
    # Dell XPS touchpads report in a finer coordinate space (~3000 units wide),
    # so a value of 100 was cancelling even the lightest gentle taps. 200 is the
    # right ballpark: still tight enough to reject genuine movement.
    "tap_threshold": 200.0,

    # Maximum contact duration (seconds) for a 3-finger tap.
    "tap_duration_limit": 0.30,

    # Maximum contact duration (seconds) for a 4-finger tap.
    # Slightly generous because landing four contacts simultaneously is harder.
    "four_finger_tap_duration_limit": 0.40,

    # ---- Contact settling window ----
    #
    # How long (ms) the SettleTimer waits before committing to pointer or
    # gesture mode.  25 ms is imperceptible (<1 video frame at 30 fps) but
    # long enough to capture staggered multi-finger arrivals (10-40 ms range).
    # Raise to 40 ms if you still see occasional false clicks; lower to 15 ms
    # if you notice cursor lag on single-finger touches.
    "settle_window_ms": 25,

    # ---- Volume control ----

    # Finger velocity (raw units/s) at the inflection point of the volume curve.
    "velocity_threshold": 300.0,

    # Exponent applied to the velocity ratio for the acceleration curve.
    "velocity_power": 1.2,

    # Base volume step per raw unit of displacement (fraction of full scale).
    "base_volume_step": 0.005,

    # Throttle window (ms) for the background volume-worker thread.
    "volume_coalesce_ms": 30,

    # ---- Gesture → action bindings ----
    #
    # Action formats:
    #   key:<ECODE>           – inject a virtual key press/release
    #   shortcut:<KWIN_NAME>  – invoke a KWin global shortcut via D-Bus
    #   dbus:<SVC PATH IFACE.METHOD [ARGS]> – raw qdbus6 call
    #   command:<SHELL_CMD>   – spawn an arbitrary shell command
    "shortcuts": {
        # 3-finger gestures
        "three_finger_tap":   "key:BTN_MIDDLE",
        "swipe3_left":        "key:KEY_PREVIOUSSONG",
        "swipe3_right":       "key:KEY_NEXTSONG",
        "pinch3_in":          "key:KEY_PLAYPAUSE",
        "pinch3_out":         "key:KEY_PLAYPAUSE",

        # 4-finger gestures
        "four_finger_tap":    "command:true",
        "swipe4_up":          "shortcut:Overview",
        "swipe4_down":        "shortcut:Show Desktop",
        "swipe4_left":        "shortcut:Switch to Previous Desktop",
        "swipe4_right":       "shortcut:Switch to Next Desktop",
    },
}

# ---------------------------------------------------------------------------
# Settle-timer states
# ---------------------------------------------------------------------------

class SettleState:
    """
    Three-state machine for the contact settling window.

    SETTLING – First contact arrived; SettleTimer is armed; events are buffered.
    POINTER  – Settled with ≤2 peak fingers; forwarding to compositor.
    GESTURE  – Settled with ≥3 peak fingers; daemon owns the touch session.
    """
    SETTLING = "settling"
    POINTER  = "pointer"
    GESTURE  = "gesture"

# Command tokens posted from the timer thread to the event loop
_CMD_SETTLE = "SETTLE_RESOLVED"

# ---------------------------------------------------------------------------
# Settle timer
# ---------------------------------------------------------------------------

class SettleTimer:
    """
    Fires a callback on a daemon thread after ``delay_s`` seconds.

    The callback posts a token into a ``queue.SimpleQueue`` rather than
    touching any shared mutable state directly, so no locking is needed and
    the actual settle decision is always applied on the event-loop thread.
    """

    def __init__(self, cmd_queue: "queue.SimpleQueue[Tuple[str, int]]"):
        self._queue = cmd_queue
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def arm(self, delay_s: float, peak_at_arm: int) -> None:
        """Start (or restart) the timer.  Safe to call from the event thread."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                delay_s,
                self._fire,
                args=(peak_at_arm,),
            )
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        """Cancel a pending timer without posting to the queue."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def fire_now(self, peak: int) -> None:
        """
        Cancel any pending timer and immediately post the settle decision.
        Used when the event loop already knows the outcome (e.g. all fingers
        lifted, or ≥3 fingers arrived) so we don't need to wait for the clock.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._queue.put((_CMD_SETTLE, peak))

    def _fire(self, peak: int) -> None:
        """Called on the timer thread after the delay expires."""
        self._queue.put((_CMD_SETTLE, peak))


# ---------------------------------------------------------------------------
# Volume worker
# ---------------------------------------------------------------------------

class VolumeWorker(threading.Thread):
    """
    Background thread that throttles and coalesces high-frequency volume
    updates onto a configurable cadence, preventing wpctl subprocess storms
    from blocking the main event loop.
    """

    def __init__(self, coalesce_ms: int):
        super().__init__(daemon=True, name="VolumeWorker")
        self.coalesce_s = coalesce_ms / 1000.0
        self.current_volume: Optional[float] = None
        self.target_volume: float = 0.50
        self.running = True
        self._cv = threading.Condition()

    def set_target(self, volume: float) -> None:
        with self._cv:
            self.target_volume = max(0.0, min(1.5, volume))
            self._cv.notify()

    def stop(self) -> None:
        self.running = False
        with self._cv:
            self._cv.notify()

    def run(self) -> None:
        self.current_volume = self._get_system_volume()
        self.target_volume = self.current_volume

        while self.running:
            with self._cv:
                while self.running and abs(self.target_volume - self.current_volume) < 0.001:
                    self._cv.wait()
                if not self.running:
                    break
                local_target = self.target_volume

            time.sleep(self.coalesce_s)

            with self._cv:
                local_target = self.target_volume

            self._set_system_volume(local_target)
            self.current_volume = local_target

    def _get_system_volume(self) -> float:
        try:
            out = subprocess.check_output(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=1.0
            ).decode()
            parts = out.split()
            if len(parts) >= 2:
                return float(parts[1])
        except Exception as ex:
            print(f"[VolumeWorker] Warning: could not query volume: {ex}", file=sys.stderr)
        return 0.50

    def _set_system_volume(self, val: float) -> None:
        try:
            subprocess.run(
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{val:.3f}"],
                check=True, timeout=1.0,
            )
        except Exception as ex:
            print(
                f"[VolumeWorker] Error: could not set volume to {val:.3f}: {ex}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

class PrecisionDaemon:
    """
    Core event-processing daemon.

    In grab mode the physical device is exclusively owned.  A virtual clone
    forwards 1/2-finger interactions transparently to the compositor.
    3+ finger interactions are intercepted for gesture recognition.

    Pass-through decisions are gated by the SettleTimer: all raw events from
    the first contact frame are buffered until the timer fires (25 ms by
    default).  The timer posts a command to a SimpleQueue; _process_frame
    drains that queue at the start of every frame to apply the decision on
    the event-loop thread, keeping all device I/O single-threaded.

    All key/button injections go to a separate uinput virtual keyboard that
    explicitly declares BTN_MIDDLE, KEY_PLAYPAUSE, etc.  The virtual touchpad
    clone mirrors physical capabilities only and silently drops codes it
    wasn't declared with — so BTN_MIDDLE must never be sent there.
    """

    def __init__(
        self,
        device_path: str,
        grab_mode: bool,
        config: Dict[str, Any],
        verbose: bool,
    ):
        self.device_path = device_path
        self.grab_mode = grab_mode
        self.config = config
        self.verbose = verbose

        self.device = evdev.InputDevice(self.device_path)
        self.uinput: Optional[evdev.UInput] = None
        self.virtual_touchpad: Optional[evdev.UInput] = None

        self.volume_worker = VolumeWorker(self.config["volume_coalesce_ms"])

        # ---- Multi-touch slot tracking ----
        self.slots: Dict[int, Dict[str, Any]] = {}
        self.current_slot: int = 0
        self.prev_active_count: int = 0

        # ---- Per-gesture-session state ----
        self.gesture_start_time: float = 0.0
        self.gesture_fingers: int = 0
        self.tap_eligible: bool = True
        self.swipe_triggered: bool = False
        self.pinch_triggered: bool = False
        self.swipe_fingers: int = 0
        self.pinch_fingers: int = 0
        self.gesture_direction: Optional[str] = None
        self.discrete_action_triggered: bool = False

        self.swipe_start_avg_x: Optional[float] = None
        self.swipe_start_avg_y: Optional[float] = None
        self.swipe_start_span: Optional[float] = None
        self.prev_avg_x: Optional[float] = None
        self.prev_avg_y: Optional[float] = None
        self.last_event_time: float = 0.0

        # ---- Volume accumulation ----
        self.volume_accumulator: float = 0.0
        self.active_volume_baseline: float = 0.50
        self.smooth_dy: float = 0.0
        self.max_avg_dist: float = 0.0

        # ---- Settle-timer infrastructure ----
        self._settle_window_s: float = self.config.get("settle_window_ms", 25) / 1000.0
        self._settle_state: str = SettleState.POINTER
        self._settle_peak: int = 0            # highest finger count seen this session
        self._settle_buf: List[evdev.InputEvent] = []  # raw event buffer
        self._settle_queue: "queue.SimpleQueue[Tuple[str, int]]" = queue.SimpleQueue()
        self._settle_timer = SettleTimer(self._settle_queue)

        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _initialize_uinput(self) -> None:
        """
        Virtual keyboard / button injection device.

        BTN_MIDDLE lives HERE (not on the virtual touchpad) because the
        physical touchpad does not advertise it in its evdev capability set.
        Events for undeclared codes are silently dropped by the kernel.
        """
        cap = {
            e.EV_KEY: [
                e.KEY_LEFTMETA, e.KEY_LEFTALT, e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT,
                e.KEY_TAB, e.KEY_ENTER, e.KEY_ESC, e.KEY_SPACE,
                e.KEY_PLAYPAUSE, e.KEY_NEXTSONG, e.KEY_PREVIOUSSONG,
                e.BTN_MIDDLE, e.BTN_LEFT, e.BTN_RIGHT,
            ],
        }
        self.uinput = evdev.UInput(cap, name="KDE Custom Touchpad Virtual Keyboard")
        self.log("Virtual keyboard/button uinput device created.")

    def _initialize_virtual_touchpad(self) -> None:
        """Virtual touchpad clone for 1/2-finger pass-through.

        CRITICAL: input_props must be forwarded from the physical device.
        Without INPUT_PROP_POINTER the kernel/libinput classifies the UInput
        device as a touchscreen (INPUT_PROP_DIRECT / direct absolute mapping)
        rather than a touchpad (relative-delta pointer).  The consequence is
        that every touch snaps the cursor to the raw absolute coordinate on the
        pad surface — the 1:1 mapping issue — and each new contact restarts
        from scratch instead of continuing from the last known cursor position.
        """
        phys_caps = self.device.capabilities()
        phys_caps.pop(e.EV_SYN, None)

        # Carry the physical device's input properties across to the UInput
        # clone (typically INPUT_PROP_POINTER=0 and INPUT_PROP_BUTTONPAD=2).
        phys_props = self.device.input_props()

        self.virtual_touchpad = evdev.UInput(
            phys_caps,
            name="KDE Custom Touchpad Virtual Touchpad",
            input_props=phys_props if phys_props else None,
        )
        self.log(
            f"Virtual touchpad clone created (input_props={phys_props})."
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._initialize_uinput()
        if self.grab_mode:
            self._initialize_virtual_touchpad()

        self.volume_worker.start()

        if self.grab_mode:
            self.device.grab()
            self.log(f"Exclusively grabbed '{self.device.name}' at {self.device_path}")
        else:
            self.log(f"Monitoring '{self.device.name}' in parallel mode at {self.device_path}")

        for event in self.device.read_loop():
            self._process_event(event)

    # ------------------------------------------------------------------
    # Raw event ingestion
    # ------------------------------------------------------------------

    def _process_event(self, event: evdev.InputEvent) -> None:
        if self.grab_mode:
            self._settle_buf.append(event)

        if event.type == e.EV_ABS:
            code, val = event.code, event.value

            if code == e.ABS_MT_SLOT:
                self.current_slot = val

            elif code == e.ABS_MT_TRACKING_ID:
                if val == -1:
                    if self.current_slot in self.slots:
                        self.slots[self.current_slot]["active"] = False
                else:
                    self.slots[self.current_slot] = {
                        "start_x": None, "start_y": None,
                        "x": None,       "y": None,
                        "tracking_id": val,
                        "active": True,
                    }

            elif code == e.ABS_MT_POSITION_X:
                slot = self.slots.get(self.current_slot)
                if slot:
                    if slot["start_x"] is None:
                        slot["start_x"] = float(val)
                    slot["x"] = float(val)

            elif code == e.ABS_MT_POSITION_Y:
                slot = self.slots.get(self.current_slot)
                if slot:
                    if slot["start_y"] is None:
                        slot["start_y"] = float(val)
                    slot["y"] = float(val)

        elif event.type == e.EV_SYN and event.code == e.SYN_REPORT:
            self._process_frame(event.timestamp())

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    def _process_frame(self, timestamp: float) -> None:
        # ---- 1. Drain the settle-timer command queue (lock-free, main thread only) ----
        self._drain_settle_queue()

        active_slots = {sid: s for sid, s in self.slots.items() if s["active"]}
        num_active = len(active_slots)

        # ---- 2. Finger-count transitions ----
        if num_active != self.prev_active_count:
            self.log(f"Fingers: {self.prev_active_count} → {num_active}")

            if self.prev_active_count == 0 and num_active > 0:
                # ---- New touch session ----
                self.gesture_start_time = timestamp
                self.gesture_fingers = num_active
                self.tap_eligible = True
                self.swipe_triggered = False
                self.pinch_triggered = False
                self.gesture_direction = None
                self.discrete_action_triggered = False
                self.active_volume_baseline = self.volume_worker.current_volume or 0.50
                self.volume_accumulator = 0.0
                self.last_event_time = timestamp
                self.swipe_fingers = 0
                self.pinch_fingers = 0
                self.smooth_dy = 0.0
                self.max_avg_dist = 0.0

                # Arm the settle timer
                self._settle_state = SettleState.SETTLING
                self._settle_peak = num_active
                self._settle_timer.arm(self._settle_window_s, num_active)
                self.log(
                    f"Settle timer armed ({self._settle_window_s*1000:.0f} ms, "
                    f"initial_fingers={num_active})"
                )

            elif num_active > self.gesture_fingers:
                self.gesture_fingers = num_active
                if num_active > 4:
                    self.tap_eligible = False

            # Track peak during settling
            if self._settle_state == SettleState.SETTLING and num_active > self._settle_peak:
                self._settle_peak = num_active
                self.log(f"Settle peak updated to {self._settle_peak}")

            # If ≥3 fingers arrive during settling: skip waiting, decide NOW
            if self._settle_state == SettleState.SETTLING and self._settle_peak >= 3:
                self.log("≥3 fingers detected during settle — resolving immediately to GESTURE.")
                self._settle_timer.fire_now(self._settle_peak)

            # If all fingers lift during settling: decide immediately from peak
            if self._settle_state == SettleState.SETTLING and num_active == 0:
                self.log("All fingers lifted during settle — resolving immediately.")
                self._settle_timer.fire_now(self._settle_peak)

            # Reset centroid/span baseline on finger-count change
            if not self.swipe_triggered and not self.pinch_triggered:
                self.swipe_start_avg_x = None
                self.swipe_start_avg_y = None
                self.swipe_start_span = None

            if num_active == 0:
                duration = timestamp - self.gesture_start_time
                self.log(
                    f"Gesture ended — duration={duration:.3f}s "
                    f"peak={self.gesture_fingers} drift={self.max_avg_dist:.1f}"
                )

                # Cancel any pending settle timer and resolve immediately.
                # This prevents a race where the timer fires AFTER slots.clear()
                # and tries to release already-gone slots, leaving compositor
                # contacts stuck — causing the "3-finger tap freeze".
                if self._settle_state == SettleState.SETTLING:
                    self._settle_timer.cancel()
                    # Drain anything the timer may have already posted
                    while True:
                        try:
                            self._settle_queue.get_nowait()
                        except queue.Empty:
                            break
                    # If it was a gesture session (≥3 fingers), make sure the
                    # compositor has clean releases before we clear the slot map.
                    if self._settle_peak >= 3:
                        self._release_all_slots()
                    self._settle_state = SettleState.POINTER
                    self._settle_peak = 0
                    self._settle_buf.clear()

                # Always send clean releases for any GESTURE-mode session so the
                # compositor does not see dangling MT contacts.
                elif self._settle_state == SettleState.GESTURE:
                    self._release_all_slots()

                self._on_gesture_end(duration)

                self.slots.clear()
                self.swipe_start_avg_x = None
                self.swipe_start_avg_y = None
                self.swipe_start_span = None
                self.prev_avg_x = None
                self.prev_avg_y = None

            self.prev_active_count = num_active

        # ---- 3. Per-frame finger tracking ----
        if num_active > 0:
            self._track_fingers(active_slots, num_active, timestamp)

        # ---- 4. Grab-mode pass-through ----
        if self.grab_mode and self.virtual_touchpad:
            self._handle_passthrough(num_active)

    # ------------------------------------------------------------------
    # Settle queue drain (called at top of every frame, event-loop thread only)
    # ------------------------------------------------------------------

    def _drain_settle_queue(self) -> None:
        """
        Consume all pending commands from the settle timer without blocking.
        This is the ONLY place settle state transitions happen, ensuring all
        device writes remain on the event-loop thread.
        """
        while True:
            try:
                cmd, peak = self._settle_queue.get_nowait()
            except queue.Empty:
                break

            if cmd != _CMD_SETTLE or self._settle_state != SettleState.SETTLING:
                continue

            if peak >= 3:
                self._settle_state = SettleState.GESTURE
                self.log(
                    f"[Settle] Resolved → GESTURE (peak={peak}). "
                    f"Discarding {len(self._settle_buf)} buffered events."
                )
                # Clean release so compositor doesn't get stuck contacts
                self._release_all_slots()
                self._settle_buf.clear()
            else:
                self._settle_state = SettleState.POINTER
                self.log(
                    f"[Settle] Resolved → POINTER (peak={peak}). "
                    f"Flushing {len(self._settle_buf)} buffered events."
                )
                self._flush_settle_buf()

    # ------------------------------------------------------------------
    # Settle buffer management
    # ------------------------------------------------------------------

    def _flush_settle_buf(self) -> None:
        """Replay buffered events to the virtual touchpad (POINTER mode resolution)."""
        if not self.virtual_touchpad:
            self._settle_buf.clear()
            return
        for ev in self._settle_buf:
            if ev.type != e.EV_SYN:
                self.virtual_touchpad.write(ev.type, ev.code, ev.value)
        self.virtual_touchpad.syn()
        self._settle_buf.clear()

    def _release_all_slots(self) -> None:
        """Send tracking-id=-1 for all active slots to prevent stuck contacts.

        Snapshots the slot IDs at call time so it is safe to call both before
        and after self.slots.clear().
        """
        if not self.virtual_touchpad:
            return
        # Snapshot so we're safe even if called near a slots.clear().
        slot_ids = list(self.slots.keys())
        if not slot_ids:
            return
        self.log(f"Releasing {len(slot_ids)} virtual touchpad slot(s).")
        for slot_id in slot_ids:
            self.virtual_touchpad.write(e.EV_ABS, e.ABS_MT_SLOT, slot_id)
            self.virtual_touchpad.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.virtual_touchpad.write(e.EV_SYN, e.SYN_REPORT, 0)
        self.virtual_touchpad.syn()

    # ------------------------------------------------------------------
    # Tap detection
    # ------------------------------------------------------------------

    def _on_gesture_end(self, duration: float) -> None:
        """Called when all fingers lift; fire tap actions if eligible."""
        fingers = self.gesture_fingers

        if fingers == 3 and self.tap_eligible:
            if duration < self.config["tap_duration_limit"]:
                self.log(f"3-finger tap → middle click (duration={duration:.3f}s)")
                self._trigger_action("three_finger_tap")

        elif fingers == 4 and self.tap_eligible:
            limit = self.config.get(
                "four_finger_tap_duration_limit",
                self.config["tap_duration_limit"] + 0.10,
            )
            if duration < limit:
                self.log(f"4-finger tap (duration={duration:.3f}s)")
                self._trigger_action("four_finger_tap")

        # Reset settle state for the next touch session
        self._settle_state = SettleState.POINTER
        self._settle_peak = 0
        self._settle_buf.clear()

    # ------------------------------------------------------------------
    # Finger tracking (swipe / pinch classification)
    # ------------------------------------------------------------------

    def _track_fingers(
        self,
        active_slots: Dict[int, Dict[str, Any]],
        num_active: int,
        timestamp: float,
    ) -> None:
        # Cancel tap eligibility if any finger drifted too far
        for slot in active_slots.values():
            sx, sy = slot.get("start_x"), slot.get("start_y")
            cx, cy = slot.get("x"), slot.get("y")
            if None not in (sx, sy, cx, cy):
                drift = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5
                if drift > self.config["tap_threshold"]:
                    if self.tap_eligible:
                        self.log(f"Tap cancelled: finger drifted {drift:.1f} units")
                    self.tap_eligible = False

        if num_active not in (3, 4):
            return

        valid = [s for s in active_slots.values() if s["x"] is not None and s["y"] is not None]
        if len(valid) != num_active:
            return

        avg_x = sum(s["x"] for s in valid) / num_active
        avg_y = sum(s["y"] for s in valid) / num_active
        current_span = (
            sum(((s["x"] - avg_x) ** 2 + (s["y"] - avg_y) ** 2) ** 0.5 for s in valid)
            / num_active
        )

        if self.swipe_start_avg_x is None:
            self.swipe_start_avg_x = avg_x
            self.swipe_start_avg_y = avg_y
            self.swipe_start_span = current_span

        total_dx = avg_x - self.swipe_start_avg_x
        total_dy = avg_y - self.swipe_start_avg_y
        total_dist = (total_dx ** 2 + total_dy ** 2) ** 0.5
        total_d_span = current_span - self.swipe_start_span

        self.max_avg_dist = max(self.max_avg_dist, total_dist)

        if not self.swipe_triggered and not self.pinch_triggered:
            self._check_gesture_trigger(
                num_active, timestamp, total_dx, total_dy,
                total_dist, total_d_span, current_span,
            )
        elif self.swipe_triggered:
            dx = avg_x - (self.prev_avg_x or avg_x)
            dy = avg_y - (self.prev_avg_y or avg_y)
            dt = timestamp - self.last_event_time
            if dt > 0:
                self._handle_swipe_update(self.swipe_fingers, dx, dy, dt, total_dx, total_dy)
        elif self.pinch_triggered:
            if not self.discrete_action_triggered:
                pt = self.config.get("pinch_trigger_threshold", 80.0)
                if total_d_span < -pt:
                    self._trigger_action(f"pinch{self.pinch_fingers}_in")
                    self.discrete_action_triggered = True
                elif total_d_span > pt:
                    self._trigger_action(f"pinch{self.pinch_fingers}_out")
                    self.discrete_action_triggered = True

        self.prev_avg_x = avg_x
        self.prev_avg_y = avg_y
        self.last_event_time = timestamp

    def _check_gesture_trigger(
        self,
        num_active: int,
        timestamp: float,
        total_dx: float,
        total_dy: float,
        total_dist: float,
        total_d_span: float,
        current_span: float,
    ) -> None:
        duration = timestamp - self.gesture_start_time
        swipe_threshold = self.config["swipe_trigger_threshold"]
        pinch_threshold = self.config.get("pinch_trigger_threshold", 80.0)
        large = 200.0

        # Trigger swipe as soon as the distance threshold is crossed.
        # The old 0.15 s time gate was blocking fast-start scrolls from firing
        # until the user had already moved a noticeable amount — making 2-finger
        # scroll feel sluggish and unresponsive on first contact.
        should_swipe = total_dist > swipe_threshold
        should_pinch = abs(total_d_span) > pinch_threshold

        if should_pinch and abs(total_d_span) > total_dist:
            self.pinch_triggered = True
            self.tap_eligible = False
            self.pinch_fingers = num_active
            self.log(f"Pinch triggered ({num_active}f): d_span={total_d_span:.1f}")
        elif should_swipe:
            self.swipe_triggered = True
            self.tap_eligible = False
            self.swipe_fingers = num_active
            if abs(total_dx) >= abs(total_dy):
                self.gesture_direction = "horizontal"
                self.log(f"Swipe triggered ({num_active}f): horizontal")
            else:
                self.gesture_direction = "vertical"
                self.log(f"Swipe triggered ({num_active}f): vertical")

    # ------------------------------------------------------------------
    # Swipe handling
    # ------------------------------------------------------------------

    def _handle_swipe_update(
        self,
        fingers: int,
        dx: float, dy: float, dt: float,
        total_dx: float, total_dy: float,
    ) -> None:
        if self.gesture_direction == "vertical" and fingers == 3:
            # ---- Velocity-scaled volume control ----
            self.smooth_dy = self.smooth_dy * 0.6 + dy * 0.4
            raw_velocity = abs(self.smooth_dy) / dt
            v_ratio = min(4.0, raw_velocity / self.config.get("velocity_threshold", 300.0))
            multiplier = 1.0 + (v_ratio ** self.config.get("velocity_power", 1.2))
            sensitivity = self.config.get("base_volume_step", 0.005) * 0.02
            delta = -self.smooth_dy * sensitivity * multiplier
            self.volume_accumulator += delta
            target = max(0.0, min(1.5, self.active_volume_baseline + self.volume_accumulator))
            self.volume_worker.set_target(target)
            self.log(
                f"Volume: dy={dy:.1f} vel={raw_velocity:.0f} ×{multiplier:.2f} → {target:.1%}"
            )

        elif self.gesture_direction == "vertical" and fingers == 4:
            if not self.discrete_action_triggered:
                thresh = self.config["discrete_trigger_threshold"]
                if total_dy < -thresh:
                    self._trigger_action("swipe4_up")
                    self.discrete_action_triggered = True
                elif total_dy > thresh:
                    self._trigger_action("swipe4_down")
                    self.discrete_action_triggered = True

        elif self.gesture_direction == "horizontal":
            thresh = self.config["discrete_trigger_threshold"]
            if fingers == 4 and not self.discrete_action_triggered:
                if total_dx < -thresh:
                    self._trigger_action("swipe4_left")
                    self.discrete_action_triggered = True
                elif total_dx > thresh:
                    self._trigger_action("swipe4_right")
                    self.discrete_action_triggered = True
            elif fingers == 3 and not self.discrete_action_triggered:
                if total_dx < -thresh:
                    self._trigger_action("swipe3_left")
                    self.discrete_action_triggered = True
                elif total_dx > thresh:
                    self._trigger_action("swipe3_right")
                    self.discrete_action_triggered = True

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _trigger_action(self, action_key: str) -> None:
        """
        Dispatch the action bound to action_key.

        All key/button events go to self.uinput (the virtual keyboard), never
        to self.virtual_touchpad.  The touchpad clone mirrors raw MT data only
        and was not declared with BTN_MIDDLE or media keys in its capabilities.
        """
        action = self.config["shortcuts"].get(action_key)
        if not action:
            self.log(f"No action bound to '{action_key}'.")
            return

        self.log(f"Action '{action_key}' → '{action}'")

        try:
            if action.startswith("key:"):
                key_name = action.split(":", 1)[1]
                key_code = getattr(e, key_name, None)
                if key_code is None:
                    print(f"[Daemon] Error: unknown evdev code '{key_name}'", file=sys.stderr)
                    return
                if self.uinput:
                    self.uinput.write(e.EV_KEY, key_code, 1)
                    self.uinput.write(e.EV_KEY, key_code, 0)
                    self.uinput.syn()

            elif action.startswith("shortcut:"):
                shortcut_name = action.split(":", 1)[1]
                subprocess.Popen(
                    f'qdbus6 org.kde.kglobalaccel /component/kwin '
                    f'invokeShortcut "{shortcut_name}"',
                    shell=True,
                )

            elif action.startswith("dbus:"):
                subprocess.Popen(f"qdbus6 {action.split(':', 1)[1]}", shell=True)

            elif action.startswith("command:"):
                subprocess.Popen(action.split(":", 1)[1], shell=True)

        except Exception as ex:
            print(f"[Daemon] Failed to execute '{action_key}': {ex}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Grab-mode pass-through
    # ------------------------------------------------------------------

    def _handle_passthrough(self, num_active: int) -> None:
        """
        Route the current frame's buffered events to the compositor or discard.

        SETTLING → nothing forwarded; buffer accumulates.
        GESTURE  → events discarded; daemon owns the session.
        POINTER  → events forwarded; 1/2-finger interactions pass through.

        Mid-session upgrade: if 3+ fingers arrive after settling resolved to
        POINTER (e.g. user placed 2 fingers then added a 3rd), transition to
        GESTURE and release compositor slots to prevent stuck contacts.
        """
        state = self._settle_state

        if state == SettleState.SETTLING:
            # Buffer is already accumulating in _settle_buf via _process_event.
            # Nothing to forward yet.
            return

        if state == SettleState.GESTURE:
            self._settle_buf.clear()
            return

        # POINTER mode — check for mid-session upgrade
        if num_active >= 3 or self.swipe_triggered or self.pinch_triggered:
            self._settle_state = SettleState.GESTURE
            self._release_all_slots()
            self._settle_buf.clear()
            self.log("Mid-session upgrade: POINTER → GESTURE")
            return

        # Forward the frame
        for ev in self._settle_buf:
            if ev.type != e.EV_SYN:
                self.virtual_touchpad.write(ev.type, ev.code, ev.value)
        self.virtual_touchpad.syn()
        self._settle_buf.clear()

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_exit(self, signum, frame) -> None:
        self.log(f"Signal {signum} received — shutting down.")
        self._settle_timer.cancel()
        self.volume_worker.stop()
        if self.grab_mode:
            try:
                self.device.ungrab()
            except Exception:
                pass
        sys.exit(0)


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def find_touchpad() -> Optional[str]:
    """Auto-detect the first multitouch touchpad."""
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            if e.EV_ABS in caps:
                abs_codes = [code for code, _ in caps[e.EV_ABS]]
                if e.ABS_MT_POSITION_X in abs_codes and e.ABS_MT_POSITION_Y in abs_codes:
                    return path
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# KWin native gesture suppression
# ---------------------------------------------------------------------------

def disable_kwin_gestures() -> None:
    """Patch ~/.config/kwinrc to disable KWin's built-in touchpad gesture engine."""
    kwinrc_path = os.path.expanduser("~/.config/kwinrc")
    if not os.path.exists(kwinrc_path):
        print("kwinrc not found — skipping KWin gesture suppression.")
        return

    try:
        with open(kwinrc_path, "r") as f:
            lines = f.readlines()

        sections: Dict[str, Dict[str, int]] = {}
        current_section: Optional[str] = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1]
                sections.setdefault(current_section, {})
            elif current_section and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                sections[current_section][key] = idx

        required: Dict[str, Dict[str, str]] = {
            "Wayland": {
                "VirtualDesktopSwipeGestures": "false",
                "VirtualDesktopPinchGestures": "false",
                "VirtualDesktopSwipeGestureFingerCount": "4",
            },
            "Plugins": {
                "TouchpadShortcutsPluginEnabled": "false",
            },
            "Effect-Slide": {
                "HorizontalSwipeGesture": "false",
                "VerticalSwipeGesture": "false",
            },
        }

        modified = False
        for section, options in required.items():
            if section not in sections:
                lines.append(f"\n[{section}]\n")
                for k, v in options.items():
                    lines.append(f"{k}={v}\n")
                modified = True
            else:
                for k, v in options.items():
                    if k in sections[section]:
                        idx = sections[section][k]
                        if lines[idx].split("=", 1)[1].strip() != v:
                            lines[idx] = f"{k}={v}\n"
                            modified = True
                    else:
                        for idx, line in enumerate(lines):
                            if line.strip() == f"[{section}]":
                                lines.insert(idx + 1, f"{k}={v}\n")
                                modified = True
                                break

        if modified:
            print("Patching ~/.config/kwinrc to suppress native KWin gestures…")
            with open(kwinrc_path, "w") as f:
                f.writelines(lines)
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/KWin", "reconfigure"],
                capture_output=True,
            )
            print("kwinrc patched and KWin reconfigured.")
        else:
            print("KWin native gestures already suppressed in kwinrc.")

        for effect in ("slide", "overview"):
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Effects",
                 "org.kde.kwin.Effects.unloadEffect", effect],
                capture_output=True,
            )
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Effects",
                 "org.kde.kwin.Effects.loadEffect", effect],
                capture_output=True,
            )
        print("KWin slide/overview effects reloaded.")

    except Exception as ex:
        print(f"Warning: failed to patch kwinrc: {ex}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="KDE Custom Precision Touchpad Gesture Daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--device", type=str, metavar="PATH",
                        help="Touchpad device path (auto-detected if omitted).")
    parser.add_argument("--no-grab", action="store_true",
                        help="Disable exclusive device grab (monitoring mode).")
    parser.add_argument("--config", type=str, metavar="FILE",
                        help="Path to JSON configuration file.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose per-frame logging.")
    args = parser.parse_args()

    device_path = args.device or find_touchpad()
    if not device_path:
        print(
            "Error: could not auto-detect a multitouch touchpad.\n"
            "Specify one with --device /dev/input/eventN",
            file=sys.stderr,
        )
        sys.exit(1)

    config: Dict[str, Any] = {
        k: (v.copy() if isinstance(v, dict) else v)
        for k, v in DEFAULT_CONFIG.items()
    }
    config_file = args.config
    if not config_file:
        for candidate in (
            os.path.expanduser("~/.config/kde-custom-touchpad.json"),
            "/etc/kde-custom-touchpad.json",
        ):
            if os.path.exists(candidate):
                config_file = candidate
                break

    if config_file and os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                loaded = json.load(f)
            if "shortcuts" in loaded and isinstance(loaded["shortcuts"], dict):
                config["shortcuts"].update(loaded["shortcuts"])
                loaded.pop("shortcuts")
            config.update(loaded)
            print(f"Loaded config: {config_file}")
        except Exception as ex:
            print(f"Warning: could not load config '{config_file}': {ex}", file=sys.stderr)

    disable_kwin_gestures()

    grab_mode = not args.no_grab
    settle_ms = config.get("settle_window_ms", 25)
    daemon = PrecisionDaemon(device_path, grab_mode, config, args.verbose)
    print(
        f"Starting KDE Custom Touchpad Daemon on {device_path} "
        f"(grab={grab_mode}, settle_timer={settle_ms} ms)"
    )
    daemon.run()


if __name__ == "__main__":
    main()
