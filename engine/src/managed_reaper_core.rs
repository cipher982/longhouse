//! Shared grace tracking and in-flight guard for managed provider reapers.

use std::collections::HashMap;
use std::collections::HashSet;
use std::marker::PhantomData;
use std::sync::Arc;

use tokio::sync::Mutex;
use tokio::time::Instant;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReaperCoreDecision<Action> {
    Skip,
    Track,
    Act(Action),
}

pub struct ReaperCore<Action> {
    first_seen_at: HashMap<String, Instant>,
    in_flight: Arc<Mutex<HashSet<String>>>,
    _action: PhantomData<Action>,
}

impl<Action: Copy> ReaperCore<Action> {
    pub fn new() -> Self {
        Self {
            first_seen_at: HashMap::new(),
            in_flight: Arc::new(Mutex::new(HashSet::new())),
            _action: PhantomData,
        }
    }

    pub fn retain_seen<'a>(&mut self, session_ids: impl IntoIterator<Item = &'a String>) {
        let seen: HashSet<&str> = session_ids.into_iter().map(String::as_str).collect();
        self.first_seen_at
            .retain(|session_id, _| seen.contains(session_id.as_str()));
    }

    pub fn first_seen(&self, session_id: &str) -> Option<Instant> {
        self.first_seen_at.get(session_id).copied()
    }

    pub fn apply(
        &mut self,
        session_id: &str,
        decision: ReaperCoreDecision<Action>,
        now: Instant,
    ) -> Option<Action> {
        match decision {
            ReaperCoreDecision::Skip => {
                self.first_seen_at.remove(session_id);
                None
            }
            ReaperCoreDecision::Track => {
                self.first_seen_at
                    .entry(session_id.to_string())
                    .or_insert(now);
                None
            }
            ReaperCoreDecision::Act(action) => {
                self.first_seen_at.remove(session_id);
                Some(action)
            }
        }
    }

    pub fn in_flight(&self) -> Arc<Mutex<HashSet<String>>> {
        self.in_flight.clone()
    }

    #[cfg(test)]
    pub fn tracked_count(&self) -> usize {
        self.first_seen_at.len()
    }
}
