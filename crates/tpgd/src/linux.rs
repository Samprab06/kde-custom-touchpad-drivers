use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use evdev::{AbsoluteAxisCode, Device, KeyCode, PropType};
use tpg_core::{classify, AxisInfo, DeviceCaps, DeviceKind, Frame, Geometry, RawEvent, Tracker};

#[derive(Parser)]
#[command(name = "tpgd", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List input devices and show which one is the touchpad.
    List,
    /// Print decoded touchpad frames (finger count, positions in mm, palms).
    /// Read-only: the device is not grabbed.
    Dump {
        /// Touchpad device, e.g. /dev/input/event7. Auto-detected if omitted.
        #[arg(long)]
        device: Option<PathBuf>,
    },
    /// Run the gesture daemon (not implemented yet).
    Run,
}

pub fn main() -> Result<()> {
    match Cli::parse().command {
        Command::List => list(),
        Command::Dump { device } => dump(device),
        Command::Run => bail!(
            "the Rust daemon is not implemented yet (phase 1 ships diagnostics only); \
             use legacy/precision_daemon.py for now"
        ),
    }
}

fn caps_of(dev: &Device) -> DeviceCaps {
    let props = dev.properties();
    let abs = dev.supported_absolute_axes();
    let has_abs = |code| abs.is_some_and(|a| a.contains(code));
    DeviceCaps {
        name: dev.name().unwrap_or_default().to_owned(),
        prop_pointer: props.contains(PropType::POINTER),
        prop_direct: props.contains(PropType::DIRECT),
        prop_buttonpad: props.contains(PropType::BUTTONPAD),
        has_mt_position: has_abs(AbsoluteAxisCode::ABS_MT_POSITION_X)
            && has_abs(AbsoluteAxisCode::ABS_MT_POSITION_Y),
        has_mt_slot: has_abs(AbsoluteAxisCode::ABS_MT_SLOT),
        has_btn_tool_finger: dev
            .supported_keys()
            .is_some_and(|k| k.contains(KeyCode::BTN_TOOL_FINGER)),
    }
}

fn sorted_devices() -> Vec<(PathBuf, Device)> {
    let mut devices: Vec<_> = evdev::enumerate().collect();
    devices.sort_by(|a, b| a.0.cmp(&b.0));
    devices
}

/// Geometry and slot count of a touchpad.
fn touchpad_geometry(dev: &Device) -> Result<(Geometry, usize)> {
    let mut x = None;
    let mut y = None;
    let mut slots = 1;
    for (code, info) in dev.get_absinfo().context("reading axis info")? {
        let axis = AxisInfo {
            minimum: info.minimum(),
            maximum: info.maximum(),
            resolution: info.resolution(),
        };
        match code {
            AbsoluteAxisCode::ABS_MT_POSITION_X => x = Some(axis),
            AbsoluteAxisCode::ABS_MT_POSITION_Y => y = Some(axis),
            AbsoluteAxisCode::ABS_MT_SLOT => {
                slots = usize::try_from(info.maximum()).unwrap_or(0) + 1;
            }
            _ => {}
        }
    }
    match (x, y) {
        (Some(x), Some(y)) => Ok((Geometry::new(x, y), slots)),
        _ => bail!("device has no multitouch position axes"),
    }
}

fn list() -> Result<()> {
    let devices = sorted_devices();
    if devices.is_empty() {
        eprintln!("{}", permission_hint());
        return Ok(());
    }
    for (path, dev) in &devices {
        let caps = caps_of(dev);
        let kind = classify(&caps);
        let label = match kind {
            DeviceKind::Touchpad => "touchpad",
            DeviceKind::Touchscreen => "touchscreen",
            DeviceKind::OwnVirtual => "tpgd virtual",
            DeviceKind::Other => "-",
        };
        println!("{:<20} {:<13} {}", path.display(), label, caps.name);
        if kind == DeviceKind::Touchpad {
            match touchpad_geometry(dev) {
                Ok((g, slots)) => println!(
                    "{:<20} {:<13} {:.0} x {:.0} mm, {} slots{}",
                    "",
                    "",
                    g.width_mm(),
                    g.height_mm(),
                    slots,
                    if g.resolution_estimated() {
                        " (resolution missing, size estimated)"
                    } else {
                        ""
                    }
                ),
                Err(err) => println!("{:<34} could not read geometry: {err:#}", ""),
            }
        }
    }
    Ok(())
}

fn find_touchpad() -> Result<(PathBuf, Device)> {
    let devices = sorted_devices();
    if devices.is_empty() {
        bail!("{}", permission_hint());
    }
    devices
        .into_iter()
        .find(|(_, dev)| classify(&caps_of(dev)) == DeviceKind::Touchpad)
        .context("no touchpad found; run `tpgd list` and pass --device")
}

fn open(path: &Path) -> Result<Device> {
    Device::open(path).with_context(|| format!("opening {}\n{}", path.display(), permission_hint()))
}

fn dump(device: Option<PathBuf>) -> Result<()> {
    let (path, mut dev) = match device {
        Some(path) => {
            let dev = open(&path)?;
            (path, dev)
        }
        None => find_touchpad()?,
    };
    let (geometry, slots) = touchpad_geometry(&dev)?;
    eprintln!(
        "Reading {} ({}), {:.0} x {:.0} mm, {} slots. Ctrl+C to stop.",
        path.display(),
        dev.name().unwrap_or("?"),
        geometry.width_mm(),
        geometry.height_mm(),
        slots
    );
    let mut tracker = Tracker::new(geometry, slots);
    let mut t0 = None;
    loop {
        // evdev::Device handles SYN_DROPPED internally by synthesizing the
        // events needed to reach the kernel's current state.
        for ev in dev.fetch_events().context("reading events")? {
            let time_us = ev
                .timestamp()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| u64::try_from(d.as_micros()).unwrap_or(u64::MAX));
            let raw = RawEvent::new(time_us, ev.event_type().0, ev.code(), ev.value());
            if let Some(frame) = tracker.feed(raw) {
                let start = *t0.get_or_insert(frame.time_us);
                print_frame(&frame, start);
            }
        }
    }
}

fn print_frame(frame: &Frame, start_us: u64) {
    use std::fmt::Write;
    let secs = frame.time_us.saturating_sub(start_us) as f64 / 1e6;
    let mut line = format!(
        "{secs:9.3}s fingers={} tool={}{}",
        frame.finger_count(),
        frame.tool_finger_count,
        if frame.button_left { " CLICK" } else { "" }
    );
    for c in &frame.contacts {
        let mark = match c.state {
            tpg_core::ContactState::Began => "+",
            tpg_core::ContactState::Active => "",
            tpg_core::ContactState::Ended => "-",
        };
        let _ = write!(
            line,
            "  [{mark}s{} #{} {:6.1},{:5.1}mm{}]",
            c.slot,
            c.tracking_id,
            c.x_mm,
            c.y_mm,
            if c.is_palm() { " PALM" } else { "" }
        );
    }
    println!("{line}");
}

fn permission_hint() -> &'static str {
    "No readable input devices. tpgd needs read access to /dev/input/event*: \
     run it with sudo for now, or add yourself to the `input` group \
     (a narrower udev rule ships in a later phase)."
}
