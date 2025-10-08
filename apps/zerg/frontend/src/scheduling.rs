//! Scheduling utilities – simple `Frequency` enum that converts to/from
//! cron expressions so the frontend can present a friendly UI without
//! exposing raw cron syntax to the user.  **Phase 1** only requires a small
//! subset that matches the options we plan to surface in the modal:
//!
//! • Every N minutes (1-59)
//! • Hourly at a given minute
//! • Daily at HH:MM
//! • Weekly on weekday at HH:MM (weekday 0 = Sunday)
//! • Monthly on day-of-month at HH:MM
//!
//! The implementation purposefully stays *basic*: we only parse expressions
//! produced by `to_cron()`.  Any other string will return an error so the
//! caller can fall back to a sensible default.

use std::fmt;

/// High-level scheduling options exposed in the UI.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Frequency {
    /// `*/N * * * *` – every *N* minutes (1-59).
    EveryNMinutes(u8),

    /// `M * * * *` – every hour at minute *M* (0-59).
    Hourly { minute: u8 },

    /// `M H * * *` – every day at *HH:MM*.
    Daily { hour: u8, minute: u8 },

    /// `M H * * W` – every week on weekday *W* at *HH:MM* (0 = Sun).
    Weekly {
        weekday: u8, // 0-6
        hour: u8,    // 0-23
        minute: u8,  // 0-59
    },

    /// `M H D * *` – every month on day *D* at *HH:MM* (1-31).
    Monthly {
        day: u8,    // 1-31
        hour: u8,   // 0-23
        minute: u8, // 0-59
    },
}

impl Frequency {
    /// Convert the enum into a 5-field cron string (minute hour day month weekday).
    pub fn to_cron(&self) -> String {
        match self {
            Frequency::EveryNMinutes(n) => format!("*/{} * * * *", n),
            Frequency::Hourly { minute } => format!("{} * * * *", minute),
            Frequency::Daily { hour, minute } => format!("{} {} * * *", minute, hour),
            Frequency::Weekly {
                weekday,
                hour,
                minute,
            } => format!("{} {} * * {}", minute, hour, weekday),
            Frequency::Monthly { day, hour, minute } => format!("{} {} {} * *", minute, hour, day),
        }
    }
}

// ---------------------------------------------------------------------------
// Reverse conversion – very *loose* parse that only recognises patterns we
// generate with `to_cron()`.  This is good enough to pre-fill the modal.
// ---------------------------------------------------------------------------

impl TryFrom<&str> for Frequency {
    type Error = String;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        // Split by whitespace – cron expression should have exactly 5 fields.
        let parts: Vec<&str> = value.split_whitespace().collect();
        if parts.len() != 5 {
            return Err("Cron expression must have exactly 5 whitespace-separated fields".into());
        }

        // Helper to parse u8 from string
        let parse_u8 = |s: &str| -> Result<u8, String> {
            s.parse::<u8>()
                .map_err(|_| format!("Invalid number in cron field: '{}'", s))
        };

        // Pattern 1 – Every N minutes: "*/N * * * *"
        if let Some(stripped) = parts[0].strip_prefix("*/") {
            if parts[1] == "*" && parts[2] == "*" && parts[3] == "*" && parts[4] == "*" {
                let n = parse_u8(stripped)?;
                return Ok(Frequency::EveryNMinutes(n));
            }
        }

        // The remaining patterns all have numeric minute + hour fields.
        let minute = parse_u8(parts[0])?;

        // Pattern 2 – Hourly: "M * * * *"
        if parts[1] == "*" && parts[2] == "*" && parts[3] == "*" && parts[4] == "*" {
            return Ok(Frequency::Hourly { minute });
        }

        // From here on we need an hour value.
        let hour = parse_u8(parts[1])?;

        // Pattern 3 – Daily: "M H * * *"
        if parts[2] == "*" && parts[3] == "*" && parts[4] == "*" {
            return Ok(Frequency::Daily { hour, minute });
        }

        // Pattern 4 – Weekly: "M H * * W"
        if parts[2] == "*" && parts[3] == "*" {
            // Only weekday differs
            let weekday = parse_u8(parts[4])?;
            return Ok(Frequency::Weekly {
                weekday,
                hour,
                minute,
            });
        }

        // Pattern 5 – Monthly: "M H D * *"
        if parts[3] == "*" && parts[4] == "*" {
            let day = parse_u8(parts[2])?;
            return Ok(Frequency::Monthly { day, hour, minute });
        }

        Err("Unrecognised cron expression pattern".into())
    }
}

// ---------------------------------------------------------------------------
// Display implementation – will be used by the UI summary line in Phase 2.
// ---------------------------------------------------------------------------

impl fmt::Display for Frequency {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Frequency::EveryNMinutes(n) => {
                write!(f, "Every {} minute{}", n, if *n == 1 { "" } else { "s" })
            }
            Frequency::Hourly { minute } => write!(f, "Hourly at minute {:02}", minute),
            Frequency::Daily { hour, minute } => write!(f, "Daily at {:02}:{:02}", hour, minute),
            Frequency::Weekly {
                weekday,
                hour,
                minute,
            } => {
                // Map 0-6 to weekday names for readability (Sun=0)
                const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
                let day_name = DAYS.get(*weekday as usize).unwrap_or(&"???");
                write!(f, "Weekly on {} at {:02}:{:02}", day_name, hour, minute)
            }
            Frequency::Monthly { day, hour, minute } => {
                write!(f, "Monthly on {:02} at {:02}:{:02}", day, hour, minute)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tests – wasm-bindgen so they run in the same harness as the rest of the
// frontend suite.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_bindgen_test::*;

    wasm_bindgen_test_configure!(run_in_browser);

    #[wasm_bindgen_test]
    fn test_to_cron_mappings() {
        assert_eq!(Frequency::EveryNMinutes(15).to_cron(), "*/15 * * * *");
        assert_eq!(Frequency::Hourly { minute: 30 }.to_cron(), "30 * * * *");
        assert_eq!(
            Frequency::Daily { hour: 9, minute: 0 }.to_cron(),
            "0 9 * * *"
        );
        assert_eq!(
            Frequency::Weekly {
                weekday: 1,
                hour: 10,
                minute: 20
            }
            .to_cron(),
            "20 10 * * 1"
        );
        assert_eq!(
            Frequency::Monthly {
                day: 5,
                hour: 8,
                minute: 0
            }
            .to_cron(),
            "0 8 5 * *"
        );
    }

    #[wasm_bindgen_test]
    fn test_try_from_cron() {
        assert_eq!(
            Frequency::try_from("*/10 * * * *").unwrap(),
            Frequency::EveryNMinutes(10)
        );
        assert_eq!(
            Frequency::try_from("45 * * * *").unwrap(),
            Frequency::Hourly { minute: 45 }
        );
        assert_eq!(
            Frequency::try_from("0 6 * * *").unwrap(),
            Frequency::Daily { hour: 6, minute: 0 }
        );
        assert_eq!(
            Frequency::try_from("15 14 * * 2").unwrap(),
            Frequency::Weekly {
                weekday: 2,
                hour: 14,
                minute: 15
            }
        );
        assert_eq!(
            Frequency::try_from("5 3 12 * *").unwrap(),
            Frequency::Monthly {
                day: 12,
                hour: 3,
                minute: 5
            }
        );
    }
}
