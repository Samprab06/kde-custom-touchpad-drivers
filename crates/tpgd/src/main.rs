//! `tpgd`: touchpad gesture daemon.
//!
//! Phase 1 ships diagnostics only (`list`, `dump`). The gesture daemon itself
//! (`run`) lands in later phases; until then use `legacy/precision_daemon.py`.

#[cfg(target_os = "linux")]
mod linux;

#[cfg(target_os = "linux")]
fn main() -> anyhow::Result<()> {
    linux::main()
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("tpgd only runs on Linux (it reads /dev/input via evdev).");
    std::process::exit(1);
}
