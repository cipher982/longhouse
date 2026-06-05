//! Coarse local machine presence for notification timing.
//!
//! This intentionally reports only a privacy-scoped idle bucket. It does not
//! observe input events, app/window titles, commands, or raw activity history.

use std::time::Duration;

use anyhow::Result;
use chrono::Utc;
use serde::Serialize;

use crate::shipping::client::ShipperClient;

const MACHINE_PRESENCE_POST_TIMEOUT: Duration = Duration::from_secs(6);
const IDLE_5M_SECONDS: u64 = 5 * 60;
const IDLE_10M_SECONDS: u64 = 10 * 60;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub enum MachinePresenceState {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "idle_5m")]
    Idle5m,
    #[serde(rename = "idle_10m")]
    Idle10m,
    // Reserved for reliable screen-lock detection once the agent launch context
    // can prove it without requiring input-monitoring permissions.
    #[allow(dead_code)]
    #[serde(rename = "locked")]
    Locked,
    #[serde(rename = "unknown")]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MachinePresencePayload {
    pub state: MachinePresenceState,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub idle_seconds: Option<u64>,
    pub measured_at: String,
}

pub fn collect_machine_presence() -> MachinePresencePayload {
    let measured_at = Utc::now().to_rfc3339();
    match platform_idle_seconds() {
        Some(idle_seconds) => MachinePresencePayload {
            state: bucket_idle_seconds(idle_seconds),
            source: platform_presence_source().to_string(),
            idle_seconds: Some(idle_seconds),
            measured_at,
        },
        None => MachinePresencePayload {
            state: MachinePresenceState::Unknown,
            source: platform_presence_source().to_string(),
            idle_seconds: None,
            measured_at,
        },
    }
}

pub fn bucket_idle_seconds(idle_seconds: u64) -> MachinePresenceState {
    if idle_seconds >= IDLE_10M_SECONDS {
        MachinePresenceState::Idle10m
    } else if idle_seconds >= IDLE_5M_SECONDS {
        MachinePresenceState::Idle5m
    } else {
        MachinePresenceState::Active
    }
}

pub async fn send_machine_presence(
    client: &ShipperClient,
    payload: &MachinePresencePayload,
) -> Result<()> {
    let body = serde_json::to_vec(payload)?;
    client
        .post_json_with_timeout(
            "/api/agents/machine-presence",
            body,
            Some(MACHINE_PRESENCE_POST_TIMEOUT),
        )
        .await
}

#[cfg(target_os = "macos")]
fn platform_presence_source() -> &'static str {
    "macos_hid_idle"
}

#[cfg(not(target_os = "macos"))]
fn platform_presence_source() -> &'static str {
    "unsupported"
}

#[cfg(not(target_os = "macos"))]
fn platform_idle_seconds() -> Option<u64> {
    None
}

#[cfg(target_os = "macos")]
fn platform_idle_seconds() -> Option<u64> {
    macos_hid_idle_seconds()
}

#[cfg(target_os = "macos")]
fn macos_hid_idle_seconds() -> Option<u64> {
    use std::ffi::CString;
    use std::os::raw::{c_char, c_void};
    use std::ptr;

    type KernReturn = i32;
    type IoObject = u32;
    type IoService = u32;
    type CfAllocatorRef = *const c_void;
    type CfDictionaryRef = *const c_void;
    type CfMutableDictionaryRef = *mut c_void;
    type CfStringRef = *const c_void;
    type CfTypeRef = *const c_void;
    type CfTypeId = usize;
    type CfNumberType = i32;

    const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
    const K_CF_NUMBER_SINT64_TYPE: CfNumberType = 4;
    const K_IO_MASTER_PORT_DEFAULT: u32 = 0;

    #[link(name = "IOKit", kind = "framework")]
    extern "C" {
        fn IOServiceMatching(name: *const c_char) -> CfMutableDictionaryRef;
        fn IOServiceGetMatchingService(master_port: u32, matching: CfDictionaryRef) -> IoService;
        fn IORegistryEntryCreateCFProperty(
            entry: IoService,
            key: CfStringRef,
            allocator: CfAllocatorRef,
            options: u32,
        ) -> CfTypeRef;
        fn IOObjectRelease(object: IoObject) -> KernReturn;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(
            alloc: CfAllocatorRef,
            c_str: *const c_char,
            encoding: u32,
        ) -> CfStringRef;
        fn CFGetTypeID(cf: CfTypeRef) -> CfTypeId;
        fn CFNumberGetTypeID() -> CfTypeId;
        fn CFNumberGetValue(
            number: CfTypeRef,
            the_type: CfNumberType,
            value_ptr: *mut c_void,
        ) -> bool;
        fn CFRelease(cf: CfTypeRef);
    }

    unsafe {
        let class_name = CString::new("IOHIDSystem").ok()?;
        let matching = IOServiceMatching(class_name.as_ptr());
        if matching.is_null() {
            return None;
        }

        let service = IOServiceGetMatchingService(K_IO_MASTER_PORT_DEFAULT, matching);
        if service == 0 {
            return None;
        }

        let key_name = CString::new("HIDIdleTime").ok()?;
        let key =
            CFStringCreateWithCString(ptr::null(), key_name.as_ptr(), K_CF_STRING_ENCODING_UTF8);
        if key.is_null() {
            let _ = IOObjectRelease(service);
            return None;
        }

        let value = IORegistryEntryCreateCFProperty(service, key, ptr::null(), 0);
        CFRelease(key);
        let _ = IOObjectRelease(service);

        if value.is_null() {
            return None;
        }

        let is_number = CFGetTypeID(value) == CFNumberGetTypeID();
        if !is_number {
            CFRelease(value);
            return None;
        }

        let mut idle_nanos: i64 = 0;
        let ok = CFNumberGetValue(
            value,
            K_CF_NUMBER_SINT64_TYPE,
            (&mut idle_nanos as *mut i64).cast::<c_void>(),
        );
        CFRelease(value);
        if !ok || idle_nanos < 0 {
            return None;
        }

        Some((idle_nanos as u64) / 1_000_000_000)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_bucket_thresholds_are_coarse() {
        assert_eq!(bucket_idle_seconds(0), MachinePresenceState::Active);
        assert_eq!(bucket_idle_seconds(299), MachinePresenceState::Active);
        assert_eq!(bucket_idle_seconds(300), MachinePresenceState::Idle5m);
        assert_eq!(bucket_idle_seconds(599), MachinePresenceState::Idle5m);
        assert_eq!(bucket_idle_seconds(600), MachinePresenceState::Idle10m);
    }

    #[test]
    fn unsupported_platform_reports_unknown_payload() {
        #[cfg(not(target_os = "macos"))]
        {
            let payload = collect_machine_presence();
            assert_eq!(payload.state, MachinePresenceState::Unknown);
            assert_eq!(payload.source, "unsupported");
            assert_eq!(payload.idle_seconds, None);
        }
    }

    #[test]
    fn payload_uses_server_wire_state_names() {
        let payload = MachinePresencePayload {
            state: MachinePresenceState::Idle10m,
            source: "macos_hid_idle".to_string(),
            idle_seconds: Some(601),
            measured_at: "2026-06-04T20:15:00Z".to_string(),
        };
        let value = serde_json::to_value(payload).unwrap();
        assert_eq!(value["state"], "idle_10m");
        assert_eq!(value["idle_seconds"], 601);
    }
}
