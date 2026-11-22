//! The subset of Linux input event codes (`linux/input-event-codes.h`) the core needs.
//!
//! Duplicated here so the core has no platform dependency.

pub const EV_SYN: u16 = 0x00;
pub const EV_KEY: u16 = 0x01;
pub const EV_ABS: u16 = 0x03;
pub const EV_MSC: u16 = 0x04;

pub const SYN_REPORT: u16 = 0;
pub const SYN_DROPPED: u16 = 3;

pub const MSC_TIMESTAMP: u16 = 0x05;

pub const BTN_LEFT: u16 = 0x110;
pub const BTN_TOOL_FINGER: u16 = 0x145;
pub const BTN_TOOL_QUINTTAP: u16 = 0x148;
pub const BTN_TOUCH: u16 = 0x14a;
pub const BTN_TOOL_DOUBLETAP: u16 = 0x14d;
pub const BTN_TOOL_TRIPLETAP: u16 = 0x14e;
pub const BTN_TOOL_QUADTAP: u16 = 0x14f;

pub const ABS_MT_SLOT: u16 = 0x2f;
pub const ABS_MT_TOUCH_MAJOR: u16 = 0x30;
pub const ABS_MT_POSITION_X: u16 = 0x35;
pub const ABS_MT_POSITION_Y: u16 = 0x36;
pub const ABS_MT_TOOL_TYPE: u16 = 0x37;
pub const ABS_MT_TRACKING_ID: u16 = 0x39;
pub const ABS_MT_PRESSURE: u16 = 0x3a;

pub const MT_TOOL_FINGER: i32 = 0;
pub const MT_TOOL_PEN: i32 = 1;
pub const MT_TOOL_PALM: i32 = 2;
