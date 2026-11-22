//! Deciding which input device is the touchpad.
//!
//! The legacy daemon picked the first device with multitouch axes, which on
//! touchscreen laptops (e.g. OLED XPS models) can be the touchscreen.

/// Name prefix of every virtual device this project creates, so we never
/// pick up our own clone as the "physical" touchpad.
pub const VIRTUAL_DEVICE_NAME_PREFIX: &str = "tpgd virtual";

/// The capability bits needed to classify a device.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeviceCaps {
    pub name: String,
    pub prop_pointer: bool,
    pub prop_direct: bool,
    pub prop_buttonpad: bool,
    pub has_mt_position: bool,
    pub has_mt_slot: bool,
    pub has_btn_tool_finger: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeviceKind {
    Touchpad,
    Touchscreen,
    /// One of our own uinput devices.
    OwnVirtual,
    Other,
}

pub fn classify(caps: &DeviceCaps) -> DeviceKind {
    if caps.name.starts_with(VIRTUAL_DEVICE_NAME_PREFIX) {
        return DeviceKind::OwnVirtual;
    }
    if !caps.has_mt_position {
        return DeviceKind::Other;
    }
    if caps.prop_direct {
        return DeviceKind::Touchscreen;
    }
    if caps.prop_pointer || caps.prop_buttonpad || caps.has_btn_tool_finger {
        return DeviceKind::Touchpad;
    }
    DeviceKind::Other
}

#[cfg(test)]
mod tests {
    use super::*;

    fn xps_touchpad() -> DeviceCaps {
        DeviceCaps {
            name: "DELL0A1F:00 04F3:3147 Touchpad".into(),
            prop_pointer: true,
            prop_buttonpad: true,
            has_mt_position: true,
            has_mt_slot: true,
            has_btn_tool_finger: true,
            ..Default::default()
        }
    }

    #[test]
    fn detects_clickpad() {
        assert_eq!(classify(&xps_touchpad()), DeviceKind::Touchpad);
    }

    #[test]
    fn rejects_touchscreen_even_with_mt_axes() {
        let caps = DeviceCaps {
            name: "ELAN2097:00 04F3:2A2B".into(),
            prop_direct: true,
            has_mt_position: true,
            has_mt_slot: true,
            ..Default::default()
        };
        assert_eq!(classify(&caps), DeviceKind::Touchscreen);
    }

    #[test]
    fn rejects_hid_mouse_node_of_touchpad() {
        let caps = DeviceCaps {
            name: "DELL0A1F:00 04F3:3147 Mouse".into(),
            ..Default::default()
        };
        assert_eq!(classify(&caps), DeviceKind::Other);
    }

    #[test]
    fn recognises_own_virtual_clone() {
        let caps = DeviceCaps {
            name: format!("{VIRTUAL_DEVICE_NAME_PREFIX} touchpad"),
            ..xps_touchpad()
        };
        assert_eq!(classify(&caps), DeviceKind::OwnVirtual);
    }
}
