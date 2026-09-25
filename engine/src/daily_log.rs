//! Daily log file for the `connect` daemon.
//!
//! Writes to `<dir>/<prefix>.YYYY-MM-DD`, dated in UTC and opened for append,
//! the names tracing-appender's `rolling::daily` used. Log readers
//! (`engine.log.*` globs, `prune_old_logs`) depend on that naming. Writes are
//! synchronous: one `write_all` per formatted event under a mutex.

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::Mutex;

use chrono::{NaiveDate, Utc};
use tracing_subscriber::fmt::MakeWriter;

pub struct DailyLogFile {
    dir: PathBuf,
    prefix: &'static str,
    current: Mutex<Option<(NaiveDate, File)>>,
}

impl DailyLogFile {
    pub fn new(dir: PathBuf, prefix: &'static str) -> Self {
        Self {
            dir,
            prefix,
            current: Mutex::new(None),
        }
    }

    fn path_for(&self, date: NaiveDate) -> PathBuf {
        self.dir
            .join(format!("{}.{}", self.prefix, date.format("%Y-%m-%d")))
    }

    fn write_at(&self, date: NaiveDate, buf: &[u8]) -> io::Result<()> {
        let mut current = self.current.lock().unwrap_or_else(|err| err.into_inner());
        if current.as_ref().map(|(open, _)| *open) != Some(date) {
            let file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.path_for(date))?;
            *current = Some((date, file));
        }
        let (_, file) = current.as_mut().expect("log file opened above");
        file.write_all(buf)
    }
}

pub struct DailyLogWriter<'a>(&'a DailyLogFile);

impl Write for DailyLogWriter<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write_at(Utc::now().date_naive(), buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for DailyLogFile {
    type Writer = DailyLogWriter<'a>;

    fn make_writer(&'a self) -> Self::Writer {
        DailyLogWriter(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rolls_to_a_new_dated_file_and_appends() {
        let dir = tempfile::tempdir().unwrap();
        let log = DailyLogFile::new(dir.path().to_path_buf(), "engine.log");
        let day1 = NaiveDate::from_ymd_opt(2026, 9, 24).unwrap();
        let day2 = NaiveDate::from_ymd_opt(2026, 9, 25).unwrap();
        std::fs::write(dir.path().join("engine.log.2026-09-24"), "earlier\n").unwrap();

        log.write_at(day1, b"one\n").unwrap();
        log.write_at(day2, b"two\n").unwrap();
        log.write_at(day2, b"three\n").unwrap();

        let read = |name: &str| std::fs::read_to_string(dir.path().join(name)).unwrap();
        assert_eq!(read("engine.log.2026-09-24"), "earlier\none\n");
        assert_eq!(read("engine.log.2026-09-25"), "two\nthree\n");
    }
}
