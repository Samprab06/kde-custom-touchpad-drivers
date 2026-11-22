//! Conversion from raw device units to millimetres.
//!
//! Every threshold in the daemon is expressed in mm so behaviour is the same
//! on any touchpad. The legacy Python daemon used raw units, which made its
//! thresholds hardware-specific.

/// Range and resolution of one absolute axis, as reported by `EVIOCGABS`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AxisInfo {
    pub minimum: i32,
    pub maximum: i32,
    /// Units per millimetre. `0` means the kernel/firmware did not report it.
    pub resolution: i32,
}

impl AxisInfo {
    pub fn span(&self) -> i32 {
        self.maximum.saturating_sub(self.minimum)
    }
}

/// Width assumed when a device reports no resolution. Only used as a last
/// resort so thresholds stay in a sane range; `resolution_estimated()` says so.
const FALLBACK_WIDTH_MM: f64 = 100.0;

#[derive(Debug, Clone, PartialEq)]
pub struct Geometry {
    x: AxisInfo,
    y: AxisInfo,
    x_units_per_mm: f64,
    y_units_per_mm: f64,
    estimated: bool,
}

impl Geometry {
    pub fn new(x: AxisInfo, y: AxisInfo) -> Self {
        let mut estimated = false;
        let x_units_per_mm = if x.resolution > 0 {
            f64::from(x.resolution)
        } else {
            estimated = true;
            (f64::from(x.span()) / FALLBACK_WIDTH_MM).max(1.0)
        };
        let y_units_per_mm = if y.resolution > 0 {
            f64::from(y.resolution)
        } else {
            estimated = true;
            // Assume square units when only Y is missing.
            x_units_per_mm
        };
        Self {
            x,
            y,
            x_units_per_mm,
            y_units_per_mm,
            estimated,
        }
    }

    /// Absolute position in mm, measured from the top-left corner of the pad.
    pub fn to_mm(&self, raw_x: i32, raw_y: i32) -> (f64, f64) {
        (
            f64::from(raw_x - self.x.minimum) / self.x_units_per_mm,
            f64::from(raw_y - self.y.minimum) / self.y_units_per_mm,
        )
    }

    pub fn width_mm(&self) -> f64 {
        f64::from(self.x.span()) / self.x_units_per_mm
    }

    pub fn height_mm(&self) -> f64 {
        f64::from(self.y.span()) / self.y_units_per_mm
    }

    /// True if at least one axis had no resolution and a fallback was used.
    pub fn resolution_estimated(&self) -> bool {
        self.estimated
    }

    pub fn x_axis(&self) -> AxisInfo {
        self.x
    }

    pub fn y_axis(&self) -> AxisInfo {
        self.y
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn axis(minimum: i32, maximum: i32, resolution: i32) -> AxisInfo {
        AxisInfo {
            minimum,
            maximum,
            resolution,
        }
    }

    #[test]
    fn converts_using_reported_resolution() {
        let g = Geometry::new(axis(0, 3000, 20), axis(0, 1800, 20));
        assert!(!g.resolution_estimated());
        assert_eq!(g.to_mm(200, 100), (10.0, 5.0));
        assert_eq!(g.width_mm(), 150.0);
        assert_eq!(g.height_mm(), 90.0);
    }

    #[test]
    fn honours_nonzero_minimum() {
        let g = Geometry::new(axis(100, 3100, 20), axis(-50, 1750, 20));
        assert_eq!(g.to_mm(100, -50), (0.0, 0.0));
        assert_eq!(g.to_mm(300, 150), (10.0, 10.0));
    }

    #[test]
    fn falls_back_when_resolution_missing() {
        let g = Geometry::new(axis(0, 2000, 0), axis(0, 1000, 0));
        assert!(g.resolution_estimated());
        assert_eq!(g.width_mm(), FALLBACK_WIDTH_MM);
        assert_eq!(g.height_mm(), 50.0);
    }
}
