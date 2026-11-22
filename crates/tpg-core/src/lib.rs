//! Platform-independent core of the touchpad gesture daemon.
//!
//! This crate performs no I/O. It turns a stream of raw evdev events
//! ([`RawEvent`]) into atomic, millimetre-based [`Frame`]s that the gesture
//! classifier (later phases) consumes. Keeping it pure means every piece of
//! gesture logic can be unit-tested and replayed from recorded traces on any OS.

pub mod codes;
pub mod device;
pub mod geometry;
pub mod tracker;

pub use device::{classify, DeviceCaps, DeviceKind};
pub use geometry::{AxisInfo, Geometry};
pub use tracker::{Contact, ContactState, Frame, RawEvent, SlotSnapshot, ToolType, Tracker};
