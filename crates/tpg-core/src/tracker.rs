//! Multitouch protocol B decoder.
//!
//! Evdev reports *changes*, not snapshots: an axis is only sent when its value
//! differs from the slot's previous value, and frames are delimited by
//! `SYN_REPORT`. [`Tracker`] keeps the full per-slot state and emits one
//! atomic [`Frame`] per `SYN_REPORT`, with every contact's lifecycle
//! ([`ContactState`]) and position in millimetres.
//!
//! Fixes compared to the legacy Python daemon:
//! * A new contact inherits the slot's last position, because the kernel does
//!   not resend an axis whose value did not change. (Python reset it to
//!   `None` and ignored the finger.)
//! * `SYN_DROPPED` is honoured: events are discarded up to the next
//!   `SYN_REPORT` and the caller is told to resync from the kernel's state.
//! * Palms reported by firmware (`MT_TOOL_PALM`) are flagged, not counted.
//! * A slot whose tracking ID changes within one frame yields an `Ended`
//!   contact and a `Began` contact instead of a silent identity swap.

use crate::codes::*;
use crate::geometry::Geometry;

/// One raw evdev event, independent of any OS binding.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RawEvent {
    pub time_us: u64,
    pub kind: u16,
    pub code: u16,
    pub value: i32,
}

impl RawEvent {
    pub const fn new(time_us: u64, kind: u16, code: u16, value: i32) -> Self {
        Self {
            time_us,
            kind,
            code,
            value,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolType {
    Finger,
    Pen,
    Palm,
    Other(i32),
}

impl From<i32> for ToolType {
    fn from(value: i32) -> Self {
        match value {
            MT_TOOL_FINGER => Self::Finger,
            MT_TOOL_PEN => Self::Pen,
            MT_TOOL_PALM => Self::Palm,
            other => Self::Other(other),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContactState {
    /// First frame of this contact.
    Began,
    /// Still down (may or may not have moved).
    Active,
    /// Lifted in this frame. Position is the last known one.
    Ended,
}

/// One contact in a [`Frame`].
#[derive(Debug, Clone, PartialEq)]
pub struct Contact {
    pub slot: usize,
    pub tracking_id: i32,
    pub state: ContactState,
    pub x_mm: f64,
    pub y_mm: f64,
    pub raw_x: i32,
    pub raw_y: i32,
    pub pressure: Option<i32>,
    pub touch_major: Option<i32>,
    pub tool: ToolType,
}

impl Contact {
    pub fn is_down(&self) -> bool {
        self.state != ContactState::Ended
    }

    pub fn is_palm(&self) -> bool {
        self.tool == ToolType::Palm
    }
}

/// The complete touchpad state after one `SYN_REPORT`.
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub time_us: u64,
    /// Down contacts plus contacts that ended in this frame, ordered by slot.
    pub contacts: Vec<Contact>,
    /// Physical click (clickpads report the whole surface as `BTN_LEFT`).
    pub button_left: bool,
    /// Finger count implied by `BTN_TOOL_*`, for pads that track more fingers
    /// than they have slots. `0` when no tool bit is set.
    pub tool_finger_count: u8,
    /// True if this frame was produced by [`Tracker::resync`] after events
    /// were dropped.
    pub resynced: bool,
}

impl Frame {
    /// Contacts currently touching the pad (including palms).
    pub fn down(&self) -> impl Iterator<Item = &Contact> {
        self.contacts.iter().filter(|c| c.is_down())
    }

    /// Fingers that count for gestures: down and not a palm.
    pub fn finger_count(&self) -> usize {
        self.down().filter(|c| !c.is_palm()).count()
    }

    /// True if any contact began or ended in this frame.
    pub fn has_transitions(&self) -> bool {
        self.contacts
            .iter()
            .any(|c| c.state != ContactState::Active)
    }
}

/// Kernel-side state of one slot, used for [`Tracker::resync`]
/// (e.g. filled from `EVIOCGMTSLOTS`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SlotSnapshot {
    pub tracking_id: i32,
    pub x: i32,
    pub y: i32,
    pub pressure: Option<i32>,
    pub touch_major: Option<i32>,
    pub tool_type: i32,
}

impl Default for SlotSnapshot {
    fn default() -> Self {
        Self {
            tracking_id: -1,
            x: 0,
            y: 0,
            pressure: None,
            touch_major: None,
            tool_type: MT_TOOL_FINGER,
        }
    }
}

#[derive(Debug, Default, Clone, Copy)]
struct ToolBits {
    finger: bool,
    double: bool,
    triple: bool,
    quad: bool,
    quint: bool,
}

impl ToolBits {
    fn count(&self) -> u8 {
        if self.quint {
            5
        } else if self.quad {
            4
        } else if self.triple {
            3
        } else if self.double {
            2
        } else if self.finger {
            1
        } else {
            0
        }
    }
}

pub struct Tracker {
    geometry: Geometry,
    /// State being built from events of the current (uncommitted) frame.
    pending: Vec<SlotSnapshot>,
    /// State as of the last emitted frame.
    committed: Vec<SlotSnapshot>,
    /// `None` when the kernel selected a slot we don't track.
    current_slot: Option<usize>,
    button_left: bool,
    tools: ToolBits,
    dropping: bool,
    needs_resync: bool,
}

impl Tracker {
    pub fn new(geometry: Geometry, num_slots: usize) -> Self {
        let num_slots = num_slots.max(1);
        Self {
            geometry,
            pending: vec![SlotSnapshot::default(); num_slots],
            committed: vec![SlotSnapshot::default(); num_slots],
            current_slot: Some(0),
            button_left: false,
            tools: ToolBits::default(),
            dropping: false,
            needs_resync: false,
        }
    }

    pub fn geometry(&self) -> &Geometry {
        &self.geometry
    }

    pub fn num_slots(&self) -> usize {
        self.pending.len()
    }

    /// True after a `SYN_DROPPED`, until [`Tracker::resync`] is called. While
    /// set, frames are still produced but may be missing contacts or lifts.
    pub fn needs_resync(&self) -> bool {
        self.needs_resync
    }

    /// Feed one raw event. Returns a frame on `SYN_REPORT`.
    pub fn feed(&mut self, ev: RawEvent) -> Option<Frame> {
        if self.dropping {
            if ev.kind == EV_SYN && ev.code == SYN_REPORT {
                self.dropping = false;
                self.needs_resync = true;
            }
            return None;
        }

        match ev.kind {
            EV_SYN => match ev.code {
                SYN_REPORT => return Some(self.commit(ev.time_us, false)),
                SYN_DROPPED => {
                    self.dropping = true;
                    // Throw away the partial frame; it can't be trusted.
                    self.pending.clone_from(&self.committed);
                }
                _ => {}
            },
            EV_ABS => self.handle_abs(ev.code, ev.value),
            EV_KEY => self.handle_key(ev.code, ev.value != 0),
            _ => {}
        }
        None
    }

    /// Replace the tracked state with the kernel's current state (after a
    /// `SYN_DROPPED`) and emit a frame that reconciles the difference.
    pub fn resync(
        &mut self,
        time_us: u64,
        slots: &[SlotSnapshot],
        current_slot: usize,
        button_left: bool,
        tool_finger_count: u8,
    ) -> Frame {
        for (i, pending) in self.pending.iter_mut().enumerate() {
            *pending = slots.get(i).copied().unwrap_or_default();
        }
        self.current_slot = (current_slot < self.pending.len()).then_some(current_slot);
        self.button_left = button_left;
        self.tools = ToolBits {
            finger: tool_finger_count == 1,
            double: tool_finger_count == 2,
            triple: tool_finger_count == 3,
            quad: tool_finger_count == 4,
            quint: tool_finger_count >= 5,
        };
        self.dropping = false;
        self.needs_resync = false;
        self.commit(time_us, true)
    }

    fn handle_abs(&mut self, code: u16, value: i32) {
        if code == ABS_MT_SLOT {
            self.current_slot = usize::try_from(value)
                .ok()
                .filter(|&s| s < self.pending.len());
            return;
        }
        let Some(slot) = self.current_slot.and_then(|s| self.pending.get_mut(s)) else {
            return;
        };
        match code {
            ABS_MT_TRACKING_ID => {
                if value >= 0 && slot.tracking_id < 0 {
                    // New contact: firmware may not resend tool type.
                    slot.tool_type = MT_TOOL_FINGER;
                }
                // Positions are deliberately kept: the kernel only resends an
                // axis when it changes, so the new contact may share them.
                slot.tracking_id = value;
            }
            ABS_MT_POSITION_X => slot.x = value,
            ABS_MT_POSITION_Y => slot.y = value,
            ABS_MT_PRESSURE => slot.pressure = Some(value),
            ABS_MT_TOUCH_MAJOR => slot.touch_major = Some(value),
            ABS_MT_TOOL_TYPE => slot.tool_type = value,
            _ => {}
        }
    }

    fn handle_key(&mut self, code: u16, down: bool) {
        match code {
            BTN_LEFT => self.button_left = down,
            BTN_TOOL_FINGER => self.tools.finger = down,
            BTN_TOOL_DOUBLETAP => self.tools.double = down,
            BTN_TOOL_TRIPLETAP => self.tools.triple = down,
            BTN_TOOL_QUADTAP => self.tools.quad = down,
            BTN_TOOL_QUINTTAP => self.tools.quint = down,
            _ => {}
        }
    }

    fn contact(&self, slot: usize, s: &SlotSnapshot, state: ContactState) -> Contact {
        let (x_mm, y_mm) = self.geometry.to_mm(s.x, s.y);
        Contact {
            slot,
            tracking_id: s.tracking_id,
            state,
            x_mm,
            y_mm,
            raw_x: s.x,
            raw_y: s.y,
            pressure: s.pressure,
            touch_major: s.touch_major,
            tool: ToolType::from(s.tool_type),
        }
    }

    fn commit(&mut self, time_us: u64, resynced: bool) -> Frame {
        let mut contacts = Vec::new();
        for (slot, (old, new)) in self.committed.iter().zip(&self.pending).enumerate() {
            let was_down = old.tracking_id >= 0;
            let is_down = new.tracking_id >= 0;
            match (was_down, is_down) {
                (false, false) => {}
                (false, true) => contacts.push(self.contact(slot, new, ContactState::Began)),
                (true, false) => contacts.push(self.contact(slot, old, ContactState::Ended)),
                (true, true) if old.tracking_id != new.tracking_id => {
                    contacts.push(self.contact(slot, old, ContactState::Ended));
                    contacts.push(self.contact(slot, new, ContactState::Began));
                }
                (true, true) => contacts.push(self.contact(slot, new, ContactState::Active)),
            }
        }
        self.committed.clone_from(&self.pending);
        Frame {
            time_us,
            contacts,
            button_left: self.button_left,
            tool_finger_count: self.tools.count(),
            resynced,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::AxisInfo;

    /// 150 x 90 mm pad at 20 units/mm, 5 slots (roughly an XPS 15).
    fn tracker() -> Tracker {
        let x = AxisInfo {
            minimum: 0,
            maximum: 3000,
            resolution: 20,
        };
        let y = AxisInfo {
            minimum: 0,
            maximum: 1800,
            resolution: 20,
        };
        Tracker::new(Geometry::new(x, y), 5)
    }

    /// Tiny helper to write event sequences tersely.
    struct Script {
        t: u64,
        events: Vec<RawEvent>,
    }

    impl Script {
        fn new() -> Self {
            Self {
                t: 0,
                events: Vec::new(),
            }
        }
        fn ev(mut self, kind: u16, code: u16, value: i32) -> Self {
            self.events.push(RawEvent::new(self.t, kind, code, value));
            self
        }
        fn abs(self, code: u16, value: i32) -> Self {
            self.ev(EV_ABS, code, value)
        }
        fn key(self, code: u16, value: i32) -> Self {
            self.ev(EV_KEY, code, value)
        }
        fn slot(self, s: i32) -> Self {
            self.abs(ABS_MT_SLOT, s)
        }
        fn down(self, id: i32, x: i32, y: i32) -> Self {
            self.abs(ABS_MT_TRACKING_ID, id)
                .abs(ABS_MT_POSITION_X, x)
                .abs(ABS_MT_POSITION_Y, y)
        }
        fn up(self) -> Self {
            self.abs(ABS_MT_TRACKING_ID, -1)
        }
        fn syn(mut self) -> Self {
            self = self.ev(EV_SYN, SYN_REPORT, 0);
            self.t += 8_000; // ~125 Hz
            self
        }
        fn run(self, tracker: &mut Tracker) -> Vec<Frame> {
            self.events
                .into_iter()
                .filter_map(|e| tracker.feed(e))
                .collect()
        }
    }

    #[test]
    fn single_finger_lifecycle_in_mm() {
        let mut t = tracker();
        let frames = Script::new()
            .down(10, 200, 100)
            .key(BTN_TOUCH, 1)
            .key(BTN_TOOL_FINGER, 1)
            .syn()
            .abs(ABS_MT_POSITION_X, 400)
            .syn()
            .up()
            .key(BTN_TOUCH, 0)
            .key(BTN_TOOL_FINGER, 0)
            .syn()
            .run(&mut t);

        assert_eq!(frames.len(), 3);
        let c = &frames[0].contacts[0];
        assert_eq!(c.state, ContactState::Began);
        assert_eq!((c.x_mm, c.y_mm), (10.0, 5.0));
        assert_eq!(frames[0].tool_finger_count, 1);

        let c = &frames[1].contacts[0];
        assert_eq!(c.state, ContactState::Active);
        assert_eq!(c.x_mm, 20.0);

        let c = &frames[2].contacts[0];
        assert_eq!(c.state, ContactState::Ended);
        assert_eq!(c.x_mm, 20.0, "ended contact keeps its last position");
        assert_eq!(frames[2].finger_count(), 0);
        assert_eq!(frames[2].tool_finger_count, 0);
    }

    #[test]
    fn new_contact_inherits_unsent_axes() {
        // The kernel suppresses ABS_MT_POSITION_X for the second touch because
        // it equals the slot's previous value. The legacy daemon lost this finger.
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 500)
            .syn()
            .up()
            .syn()
            .abs(ABS_MT_TRACKING_ID, 2)
            .abs(ABS_MT_POSITION_Y, 900)
            .syn()
            .run(&mut t);

        let c = &frames[2].contacts[0];
        assert_eq!(c.state, ContactState::Began);
        assert_eq!((c.raw_x, c.raw_y), (1000, 900));
        assert_eq!(frames[2].finger_count(), 1);
    }

    #[test]
    fn staggered_three_finger_landing() {
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 900)
            .syn()
            .slot(1)
            .down(2, 1400, 900)
            .syn()
            .slot(2)
            .down(3, 1800, 900)
            .syn()
            .run(&mut t);

        let counts: Vec<_> = frames.iter().map(Frame::finger_count).collect();
        assert_eq!(counts, [1, 2, 3]);
        let states: Vec<_> = frames[2].contacts.iter().map(|c| c.state).collect();
        assert_eq!(
            states,
            [
                ContactState::Active,
                ContactState::Active,
                ContactState::Began
            ]
        );
    }

    #[test]
    fn palm_is_reported_but_not_counted() {
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 900)
            .slot(1)
            .down(2, 1400, 900)
            .slot(2)
            .down(3, 2500, 1700)
            .abs(ABS_MT_TOOL_TYPE, MT_TOOL_PALM)
            .syn()
            .run(&mut t);

        assert_eq!(frames[0].down().count(), 3);
        assert_eq!(frames[0].finger_count(), 2);
        assert!(frames[0].contacts[2].is_palm());
    }

    #[test]
    fn tool_type_resets_for_a_new_contact() {
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 900)
            .abs(ABS_MT_TOOL_TYPE, MT_TOOL_PALM)
            .syn()
            .up()
            .syn()
            .down(2, 1100, 900)
            .syn()
            .run(&mut t);
        assert_eq!(frames[2].finger_count(), 1);
    }

    #[test]
    fn tracking_id_swap_within_one_frame() {
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 900)
            .syn()
            .up()
            .down(2, 2000, 900)
            .syn()
            .run(&mut t);

        let c = &frames[1].contacts;
        assert_eq!(c.len(), 2);
        assert_eq!((c[0].tracking_id, c[0].state), (1, ContactState::Ended));
        assert_eq!(c[0].raw_x, 1000);
        assert_eq!((c[1].tracking_id, c[1].state), (2, ContactState::Began));
        assert_eq!(c[1].raw_x, 2000);
    }

    #[test]
    fn syn_dropped_discards_until_report_then_resyncs() {
        let mut t = tracker();
        let mut frames = Script::new()
            .down(1, 1000, 900)
            .syn()
            .slot(1)
            .down(2, 1400, 900)
            .ev(EV_SYN, SYN_DROPPED, 0)
            // Everything up to and including the next SYN_REPORT is dropped.
            .abs(ABS_MT_POSITION_X, 1500)
            .syn()
            .run(&mut t);
        assert_eq!(frames.len(), 1, "no frame for the dropped segment");
        assert!(t.needs_resync());

        // Kernel says: slot 0 lifted, slot 1 down at 1500.
        let slots = [
            SlotSnapshot::default(),
            SlotSnapshot {
                tracking_id: 2,
                x: 1500,
                y: 900,
                ..Default::default()
            },
        ];
        frames.push(t.resync(50_000, &slots, 1, false, 1));
        assert!(!t.needs_resync());

        let f = &frames[1];
        assert!(f.resynced);
        let c: Vec<_> = f
            .contacts
            .iter()
            .map(|c| (c.tracking_id, c.state))
            .collect();
        assert_eq!(c, [(1, ContactState::Ended), (2, ContactState::Began)]);

        // Subsequent deltas apply to the resynced slot.
        let frames = Script::new().abs(ABS_MT_POSITION_X, 1600).syn().run(&mut t);
        assert_eq!(frames[0].contacts[0].raw_x, 1600);
        assert_eq!(frames[0].contacts[0].slot, 1);
    }

    #[test]
    fn out_of_range_slot_is_ignored_without_panicking() {
        let mut t = tracker();
        let frames = Script::new()
            .slot(42)
            .down(9, 100, 100)
            .syn()
            .slot(0)
            .down(1, 200, 200)
            .syn()
            .run(&mut t);
        assert_eq!(frames[0].finger_count(), 0);
        assert_eq!(frames[1].finger_count(), 1);
    }

    #[test]
    fn physical_click_is_tracked() {
        let mut t = tracker();
        let frames = Script::new()
            .down(1, 1000, 1500)
            .key(BTN_LEFT, 1)
            .syn()
            .key(BTN_LEFT, 0)
            .syn()
            .run(&mut t);
        assert!(frames[0].button_left);
        assert!(!frames[1].button_left);
    }

    #[test]
    fn tool_bits_report_highest_count() {
        let mut t = tracker();
        let frames = Script::new()
            .key(BTN_TOOL_FINGER, 1)
            .syn()
            .key(BTN_TOOL_FINGER, 0)
            .key(BTN_TOOL_QUADTAP, 1)
            .syn()
            .run(&mut t);
        assert_eq!(frames[0].tool_finger_count, 1);
        assert_eq!(frames[1].tool_finger_count, 4);
    }
}
