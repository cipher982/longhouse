//! Process resource measurements included in engine heartbeats.
//!
//! Samples are cumulative platform counters. Each process keeps a bounded
//! ten-minute history, so published rates use the same monotonic interval on
//! every platform and never infer zero from an unavailable counter.

use std::collections::VecDeque;
#[cfg(target_os = "linux")]
use std::io::Read;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use parking_lot::Mutex;
use serde::Serialize;

const SAMPLE_INTERVAL: Duration = Duration::from_secs(30);
const HISTORY_WINDOW: Duration = Duration::from_secs(10 * 60);
const MAX_SAMPLES: usize =
    1 + HISTORY_WINDOW.as_secs() as usize / SAMPLE_INTERVAL.as_secs() as usize;

static SAMPLER: LazyLock<Mutex<ResourceUsageSampler>> =
    LazyLock::new(|| Mutex::new(ResourceUsageSampler::default()));

/// The sampled resource usage for this machine.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ResourceUsage {
    pub window_seconds: u64,
    pub sampled_at: DateTime<Utc>,
    pub engine: ProcessResourceUsage,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub desktop_app: Option<ProcessResourceUsage>,
}

/// Resource usage for one process; unsupported measurements remain JSON null.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProcessResourceUsage {
    pub pid: u32,
    pub cpu_percent: Option<f64>,
    pub disk_write_bytes_per_sec: Option<f64>,
    pub wakeups_per_sec: Option<f64>,
    pub phys_footprint_bytes: Option<u64>,
}

#[derive(Clone, Copy)]
struct CumulativeSample {
    sampled_at: Instant,
    cpu_user_ns: Option<u64>,
    cpu_system_ns: Option<u64>,
    disk_write_bytes: Option<u64>,
    wakeups: Option<u64>,
    phys_footprint_bytes: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct DesktopIdentity {
    pid: u32,
    start_time_micros: i64,
}

struct ResourceUsageSampler {
    sample_interval: Duration,
    last_sample_at: Option<Instant>,
    last_usage: Option<ResourceUsage>,
    engine_samples: VecDeque<CumulativeSample>,
    desktop_identity: Option<DesktopIdentity>,
    desktop_samples: VecDeque<CumulativeSample>,
}

impl Default for ResourceUsageSampler {
    fn default() -> Self {
        Self::new(SAMPLE_INTERVAL)
    }
}

impl ResourceUsageSampler {
    fn new(sample_interval: Duration) -> Self {
        Self {
            sample_interval,
            last_sample_at: None,
            last_usage: None,
            engine_samples: VecDeque::with_capacity(MAX_SAMPLES),
            desktop_identity: None,
            desktop_samples: VecDeque::with_capacity(MAX_SAMPLES),
        }
    }

    fn sample(&mut self, now: Instant, sampled_at: DateTime<Utc>) -> ResourceUsage {
        if self
            .last_sample_at
            .is_some_and(|last| now.saturating_duration_since(last) < self.sample_interval)
        {
            if let Some(usage) = &self.last_usage {
                return usage.clone();
            }
        }

        let engine_pid = std::process::id();
        push_sample(&mut self.engine_samples, measure_process(engine_pid), now);

        #[cfg(target_os = "macos")]
        let desktop_app = self.sample_desktop_app(now);
        #[cfg(not(target_os = "macos"))]
        let desktop_app = {
            self.desktop_identity = None;
            self.desktop_samples.clear();
            None
        };

        let window_seconds = self
            .engine_samples
            .front()
            .zip(self.engine_samples.back())
            .map(|(oldest, newest)| {
                newest
                    .sampled_at
                    .saturating_duration_since(oldest.sampled_at)
                    .as_secs()
            })
            .unwrap_or(0)
            .min(HISTORY_WINDOW.as_secs());
        let usage = ResourceUsage {
            window_seconds,
            sampled_at,
            engine: process_usage(engine_pid, &self.engine_samples),
            desktop_app,
        };
        self.last_sample_at = Some(now);
        self.last_usage = Some(usage.clone());
        usage
    }

    #[cfg(target_os = "macos")]
    fn sample_desktop_app(&mut self, now: Instant) -> Option<ProcessResourceUsage> {
        let cached = self
            .desktop_identity
            .map(|identity| (identity.pid, identity.start_time_micros));
        let resolved = crate::process_identity::try_resolve_longhouse_app(cached).map(
            |(pid, start_time_micros)| DesktopIdentity {
                pid,
                start_time_micros,
            },
        );
        if resolved != self.desktop_identity {
            self.desktop_samples.clear();
            self.desktop_identity = resolved;
        }
        let identity = self.desktop_identity?;
        push_sample(
            &mut self.desktop_samples,
            measure_process(identity.pid),
            now,
        );
        Some(process_usage(identity.pid, &self.desktop_samples))
    }
}

/// Return the latest process-wide resource snapshot, sampling no more often
/// than once every 30 seconds.
pub fn sample() -> ResourceUsage {
    SAMPLER.lock().sample(Instant::now(), Utc::now())
}

fn push_sample(
    samples: &mut VecDeque<CumulativeSample>,
    mut sample: CumulativeSample,
    now: Instant,
) {
    sample.sampled_at = now;
    samples.push_back(sample);
    while samples.len() > 1
        && now.saturating_duration_since(samples.front().unwrap().sampled_at) > HISTORY_WINDOW
    {
        samples.pop_front();
    }
    while samples.len() > MAX_SAMPLES {
        samples.pop_front();
    }
}

fn process_usage(pid: u32, samples: &VecDeque<CumulativeSample>) -> ProcessResourceUsage {
    let Some(newest) = samples.back() else {
        return ProcessResourceUsage {
            pid,
            cpu_percent: None,
            disk_write_bytes_per_sec: None,
            wakeups_per_sec: None,
            phys_footprint_bytes: None,
        };
    };
    let rates = samples
        .front()
        .filter(|_| samples.len() >= 2)
        .and_then(|oldest| {
            let elapsed = newest
                .sampled_at
                .saturating_duration_since(oldest.sampled_at);
            (elapsed > Duration::ZERO).then_some((oldest, elapsed))
        });
    let (cpu_percent, disk_write_bytes_per_sec, wakeups_per_sec) = rates
        .map(|(oldest, elapsed)| {
            let cpu_start = oldest
                .cpu_user_ns
                .zip(oldest.cpu_system_ns)
                .and_then(|(user, system)| user.checked_add(system));
            let cpu_end = newest
                .cpu_user_ns
                .zip(newest.cpu_system_ns)
                .and_then(|(user, system)| user.checked_add(system));
            (
                rate(cpu_start, cpu_end, elapsed)
                    .map(|cpu_ns_per_sec| cpu_ns_per_sec / 10_000_000.0),
                rate(oldest.disk_write_bytes, newest.disk_write_bytes, elapsed),
                rate(oldest.wakeups, newest.wakeups, elapsed),
            )
        })
        .unwrap_or((None, None, None));
    ProcessResourceUsage {
        pid,
        cpu_percent,
        disk_write_bytes_per_sec,
        wakeups_per_sec,
        phys_footprint_bytes: newest.phys_footprint_bytes,
    }
}

fn rate(start: Option<u64>, end: Option<u64>, elapsed: Duration) -> Option<f64> {
    let delta = end?.checked_sub(start?)?;
    (elapsed > Duration::ZERO).then(|| delta as f64 / elapsed.as_secs_f64())
}

#[cfg(target_os = "macos")]
fn measure_process(pid: u32) -> CumulativeSample {
    let mut sample = CumulativeSample {
        sampled_at: Instant::now(),
        cpu_user_ns: None,
        cpu_system_ns: None,
        disk_write_bytes: None,
        wakeups: None,
        phys_footprint_bytes: None,
    };
    let Ok(pid) = libc::pid_t::try_from(pid) else {
        return sample;
    };
    let mut info = std::mem::MaybeUninit::<libc::rusage_info_v4>::zeroed();
    // proc_pid_rusage's pointer type is opaque in libc; Apple expects the
    // versioned structure at this address for the requested flavor.
    let result = unsafe {
        libc::proc_pid_rusage(
            pid,
            libc::RUSAGE_INFO_V4,
            info.as_mut_ptr().cast::<libc::rusage_info_t>(),
        )
    };
    if result != 0 {
        return sample;
    }
    let info = unsafe { info.assume_init() };
    sample.cpu_user_ns = mach_time_to_nanos(info.ri_user_time);
    sample.cpu_system_ns = mach_time_to_nanos(info.ri_system_time);
    sample.disk_write_bytes = Some(info.ri_diskio_byteswritten);
    sample.wakeups = info.ri_pkg_idle_wkups.checked_add(info.ri_interrupt_wkups);
    sample.phys_footprint_bytes = Some(info.ri_phys_footprint);
    sample
}

#[cfg(target_os = "macos")]
#[allow(deprecated)]
fn mach_time_to_nanos(value: u64) -> Option<u64> {
    static TIMEBASE: LazyLock<Option<(u32, u32)>> = LazyLock::new(|| {
        let mut info = libc::mach_timebase_info { numer: 0, denom: 0 };
        let result = unsafe { libc::mach_timebase_info(&mut info) };
        (result == 0 && info.denom != 0).then_some((info.numer, info.denom))
    });
    let (numer, denom) = (*TIMEBASE)?;
    let nanos = u128::from(value).checked_mul(u128::from(numer))? / u128::from(denom);
    u64::try_from(nanos).ok()
}

#[cfg(target_os = "linux")]
fn measure_process(_pid: u32) -> CumulativeSample {
    let mut sample = CumulativeSample {
        sampled_at: Instant::now(),
        cpu_user_ns: None,
        cpu_system_ns: None,
        disk_write_bytes: read_proc_write_bytes(),
        wakeups: None,
        phys_footprint_bytes: read_proc_vm_rss(),
    };
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::zeroed();
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } == 0 {
        let usage = unsafe { usage.assume_init() };
        sample.cpu_user_ns = timeval_to_nanos(usage.ru_utime);
        sample.cpu_system_ns = timeval_to_nanos(usage.ru_stime);
    }
    sample
}

#[cfg(target_os = "linux")]
fn timeval_to_nanos(value: libc::timeval) -> Option<u64> {
    let seconds = u64::try_from(value.tv_sec).ok()?;
    let microseconds = u64::try_from(value.tv_usec).ok()?;
    seconds
        .checked_mul(1_000_000_000)?
        .checked_add(microseconds.checked_mul(1_000)?)
}

#[cfg(target_os = "linux")]
fn read_proc_write_bytes() -> Option<u64> {
    let mut buffer = [0; 4096];
    let contents = read_proc_text("/proc/self/io", &mut buffer)?;
    contents.lines().find_map(|line| {
        line.strip_prefix("write_bytes:")?
            .trim()
            .parse::<u64>()
            .ok()
    })
}

#[cfg(target_os = "linux")]
fn read_proc_vm_rss() -> Option<u64> {
    let mut buffer = [0; 8192];
    let contents = read_proc_text("/proc/self/status", &mut buffer)?;
    let kilobytes = contents.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        (fields.next()? == "VmRSS:")
            .then(|| fields.next()?.parse::<u64>().ok())
            .flatten()
    })?;
    kilobytes.checked_mul(1024)
}
#[cfg(target_os = "linux")]
fn read_proc_text<'a>(path: &str, buffer: &'a mut [u8]) -> Option<&'a str> {
    let mut file = std::fs::File::open(path).ok()?;
    let length = file.read(buffer).ok()?;
    if length == buffer.len() {
        return None;
    }
    std::str::from_utf8(&buffer[..length]).ok()
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn measure_process(_pid: u32) -> CumulativeSample {
    CumulativeSample {
        sampled_at: Instant::now(),
        cpu_user_ns: None,
        cpu_system_ns: None,
        disk_write_bytes: None,
        wakeups: None,
        phys_footprint_bytes: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_os = "macos")]
    use std::hint::black_box;
    #[cfg(target_os = "macos")]
    use std::io::Write;

    fn sample_at(
        sampled_at: Instant,
        cpu_user_ns: Option<u64>,
        cpu_system_ns: Option<u64>,
        disk_write_bytes: Option<u64>,
        wakeups: Option<u64>,
        phys_footprint_bytes: Option<u64>,
    ) -> CumulativeSample {
        CumulativeSample {
            sampled_at,
            cpu_user_ns,
            cpu_system_ns,
            disk_write_bytes,
            wakeups,
            phys_footprint_bytes,
        }
    }

    #[test]
    fn rates_use_elapsed_time_and_preserve_unknown_values() {
        let start = Instant::now();
        let samples = VecDeque::from([
            sample_at(
                start,
                Some(1_000_000_000),
                Some(2_000_000_000),
                Some(400),
                Some(10),
                Some(4096),
            ),
            sample_at(
                start + Duration::from_secs(10),
                Some(2_500_000_000),
                Some(2_500_000_000),
                Some(900),
                Some(40),
                Some(8192),
            ),
        ]);
        let usage = process_usage(123, &samples);
        assert_eq!(usage.cpu_percent, Some(20.0));
        assert_eq!(usage.disk_write_bytes_per_sec, Some(50.0));
        assert_eq!(usage.wakeups_per_sec, Some(3.0));
        assert_eq!(usage.phys_footprint_bytes, Some(8192));

        let one_sample = VecDeque::from([samples[0]]);
        let usage = process_usage(123, &one_sample);
        assert_eq!(usage.cpu_percent, None);
        assert_eq!(usage.disk_write_bytes_per_sec, None);
        assert_eq!(usage.wakeups_per_sec, None);
        assert_eq!(usage.phys_footprint_bytes, Some(4096));

        let unmeasurable = VecDeque::from([
            sample_at(start, None, None, None, None, None),
            sample_at(start + Duration::from_secs(1), None, None, None, None, None),
        ]);
        let usage = process_usage(123, &unmeasurable);
        assert_eq!(usage.cpu_percent, None);
        assert_eq!(usage.disk_write_bytes_per_sec, None);
        assert_eq!(usage.wakeups_per_sec, None);
        assert_eq!(usage.phys_footprint_bytes, None);
    }

    #[test]
    fn serialized_unmeasurable_resources_are_null_and_absent_apps_are_omitted() {
        let usage = ResourceUsage {
            window_seconds: 0,
            sampled_at: Utc::now(),
            engine: ProcessResourceUsage {
                pid: 123,
                cpu_percent: None,
                disk_write_bytes_per_sec: None,
                wakeups_per_sec: None,
                phys_footprint_bytes: None,
            },
            desktop_app: None,
        };
        let json = serde_json::to_value(usage).unwrap();
        assert_eq!(json["engine"]["cpu_percent"], serde_json::Value::Null);
        assert_eq!(
            json["engine"]["disk_write_bytes_per_sec"],
            serde_json::Value::Null
        );
        assert_eq!(json["engine"]["wakeups_per_sec"], serde_json::Value::Null);
        assert_eq!(
            json["engine"]["phys_footprint_bytes"],
            serde_json::Value::Null
        );
        assert!(json.get("desktop_app").is_none());
    }

    #[test]
    fn sampler_obeys_interval_and_keeps_a_ten_minute_ring() {
        let start = Instant::now();
        let mut sampler = ResourceUsageSampler::new(Duration::from_secs(30));
        let first = sampler.sample(start, Utc::now());
        let cached = sampler.sample(start + Duration::from_secs(1), Utc::now());
        assert_eq!(first.sampled_at, cached.sampled_at);
        assert_eq!(sampler.engine_samples.len(), 1);

        for step in 1..=25 {
            let now = start + Duration::from_secs(step * 30);
            sampler.sample(now, Utc::now());
        }
        assert!(sampler.engine_samples.len() <= MAX_SAMPLES);
        assert!(sampler.engine_samples.len() >= 20);
        assert!(
            sampler
                .engine_samples
                .back()
                .unwrap()
                .sampled_at
                .saturating_duration_since(sampler.engine_samples.front().unwrap().sampled_at)
                <= HISTORY_WINDOW
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn sampling_self_reports_real_resources_after_cpu_and_disk_work() {
        let mut sampler = ResourceUsageSampler::new(Duration::from_secs(1));
        let start = Instant::now();
        sampler.sample(start, Utc::now());

        let until = Instant::now() + Duration::from_millis(150);
        let mut value = 0u64;
        while Instant::now() < until {
            value = black_box(value.wrapping_add(1));
        }
        black_box(value);
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(&vec![0x5a; 1024 * 1024]).unwrap();
        file.sync_all().unwrap();
        std::thread::sleep(Duration::from_millis(1_100));

        let usage = sampler.sample(Instant::now(), Utc::now());
        assert_eq!(usage.engine.pid, std::process::id());
        assert!(usage
            .engine
            .cpu_percent
            .is_some_and(|value| value.is_finite() && value > 0.0));
        assert!(usage
            .engine
            .disk_write_bytes_per_sec
            .is_some_and(|value| value > 0.0));
        assert!(usage.engine.wakeups_per_sec.is_some());
        assert!(usage
            .engine
            .phys_footprint_bytes
            .is_some_and(|value| value > 0));
        println!(
            "RESOURCE_USAGE_SAMPLE_JSON={}",
            serde_json::to_string(&usage).unwrap()
        );
    }
}
