use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

use anyhow::{bail, Result};
use serde_json::Value;

pub type InputFuture<'a> = Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>>;

pub trait ConsoleInput: Send + Sync {
    fn send_input<'a>(&'a self, text: &'a str, images: &'a [PathBuf]) -> InputFuture<'a>;
    fn close_input(&self) -> InputFuture<'_>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvocationState {
    Responding,
    Parked,
    Closed,
}

impl InvocationState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Responding => "responding",
            Self::Parked => "parked",
            Self::Closed => "closed",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TurnOrigin {
    User,
    Wake,
}

impl TurnOrigin {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Wake => "wake",
        }
    }
}

#[derive(Clone, Debug)]
pub struct TurnBinding {
    pub run_id: String,
    pub turn_id: Option<String>,
    pub client_request_id: Option<String>,
    pub origin: TurnOrigin,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PendingItem {
    pub id: String,
    pub kind: String,
    pub status: String,
    pub description: Option<String>,
}

impl PendingItem {
    fn value(&self) -> Value {
        let mut value = serde_json::json!({
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
        });
        if let Some(description) = &self.description {
            value["description"] = Value::String(description.clone());
        }
        value
    }
}

#[derive(Clone, Debug)]
pub struct BufferedEvent {
    pub sequence: u64,
    pub value: Value,
}

#[derive(Clone, Debug)]
pub struct WakeRequest {
    pub invocation_id: String,
    pub wake_id: String,
    pub provider_thread_id: String,
    pub trigger: Value,
}

#[derive(Clone, Debug)]
pub struct IdleSignal {
    pub terminal_state: String,
    pub exit_code: Option<i32>,
    pub stderr: Option<String>,
}

#[derive(Clone, Debug)]
pub struct IdleOutcome {
    pub binding: TurnBinding,
    pub signal: IdleSignal,
    pub invocation_state: InvocationState,
    pub pending_count: usize,
    pub has_active_turn: bool,
}

#[derive(Debug)]
pub struct WakeTargetGone;

impl std::fmt::Display for WakeTargetGone {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("wake target is gone or wake_id is unknown")
    }
}

impl std::error::Error for WakeTargetGone {}
#[derive(Clone, Debug)]
pub struct WakeBinding {
    pub buffered_events: Vec<BufferedEvent>,
    pub deferred_idle: Option<IdleOutcome>,
}

struct State {
    phase: InvocationState,
    current_turn: Option<TurnBinding>,
    latest_turn: TurnBinding,
    pending: BTreeMap<String, PendingItem>,
    recent_items: Vec<PendingItem>,
    wake_seq: u64,
    pending_wake_id: Option<String>,
    buffered_events: Vec<BufferedEvent>,
    deferred_idle: Option<IdleSignal>,
    input_pending: bool,
}

pub struct ConsoleInvocation {
    pub provider: String,
    pub provider_thread_id: String,
    pub launch_id: String,
    process: Mutex<(u32, i32)>,
    input: Mutex<Arc<dyn ConsoleInput>>,
    state: Mutex<State>,
    stopped: AtomicBool,
    stopped_notify: tokio::sync::Notify,
}

impl ConsoleInvocation {
    pub fn new(
        provider: impl Into<String>,
        provider_thread_id: impl Into<String>,
        launch_id: impl Into<String>,
        pid: u32,
        process_group_id: i32,
        binding: TurnBinding,
        input: Arc<dyn ConsoleInput>,
    ) -> Self {
        Self {
            provider: provider.into(),
            provider_thread_id: provider_thread_id.into(),
            launch_id: launch_id.into(),
            process: Mutex::new((pid, process_group_id)),
            input: Mutex::new(input),
            state: Mutex::new(State {
                phase: InvocationState::Responding,
                current_turn: Some(binding.clone()),
                latest_turn: binding,
                pending: BTreeMap::new(),
                recent_items: Vec::new(),
                wake_seq: 0,
                pending_wake_id: None,
                buffered_events: Vec::new(),
                deferred_idle: None,
                input_pending: false,
            }),
            stopped: AtomicBool::new(false),
            stopped_notify: tokio::sync::Notify::new(),
        }
    }

    pub fn state(&self) -> InvocationState {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .phase
    }

    pub fn pending_count(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pending
            .len()
    }

    pub fn latest_turn(&self) -> TurnBinding {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .latest_turn
            .clone()
    }
    pub fn process_identity(&self) -> (u32, i32) {
        *self
            .process
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    pub fn replace_process(&self, pid: u32, process_group_id: i32) {
        *self
            .process
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = (pid, process_group_id);
    }

    pub async fn send_user_input(
        &self,
        binding: TurnBinding,
        text: &str,
        images: &[PathBuf],
    ) -> Result<()> {
        {
            let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            if state.phase != InvocationState::Parked || state.pending.is_empty() {
                bail!("Console invocation is not parked with pending work");
            }
            if binding.origin != TurnOrigin::User {
                bail!("only a user turn can write input into a parked invocation");
            }
            state.phase = InvocationState::Responding;
            state.current_turn = Some(binding.clone());
            state.latest_turn = binding;
            state.pending_wake_id = None;
            state.buffered_events.clear();
            state.deferred_idle = None;
            state.input_pending = false;
        }
        self.write_input(text, images).await
    }
    pub async fn write_input(&self, text: &str, images: &[PathBuf]) -> Result<()> {
        let input = self
            .input
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        input.send_input(text, images).await
    }

    pub fn replace_input(&self, input: Arc<dyn ConsoleInput>) {
        *self.input.lock().unwrap_or_else(|error| error.into_inner()) = input;
    }
    pub fn pending_wake_id(&self) -> Option<String> {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        (state.phase == InvocationState::Responding && state.current_turn.is_none())
            .then(|| state.pending_wake_id.clone())
            .flatten()
    }

    pub fn response_started(&self, trigger: Value) -> Option<WakeRequest> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.phase != InvocationState::Parked || state.pending.is_empty() {
            return None;
        }
        state.phase = InvocationState::Responding;
        state.current_turn = None;
        state.wake_seq += 1;
        let wake_id = format!("{}:{}", self.launch_id, state.wake_seq);
        state.pending_wake_id = Some(wake_id.clone());
        state.buffered_events.clear();
        Some(WakeRequest {
            invocation_id: self.launch_id.clone(),
            wake_id,
            provider_thread_id: self.provider_thread_id.clone(),
            trigger,
        })
    }

    pub fn bind_wake<F>(
        &self,
        invocation_id: &str,
        wake_id: &str,
        binding: TurnBinding,
        persist: F,
    ) -> Result<WakeBinding>
    where
        F: FnOnce() -> Result<()>,
    {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if invocation_id != self.launch_id
            || state.phase != InvocationState::Responding
            || state.current_turn.is_some()
            || state.pending_wake_id.as_deref() != Some(wake_id)
        {
            return Err(WakeTargetGone.into());
        }
        persist()?;
        state.current_turn = Some(binding.clone());
        state.latest_turn = binding.clone();
        state.pending_wake_id = None;
        state.input_pending = binding.origin == TurnOrigin::User;
        let buffered_events = std::mem::take(&mut state.buffered_events);
        let deferred_idle = if state.input_pending {
            None
        } else {
            state
                .deferred_idle
                .take()
                .map(|signal| complete_idle(&mut state, signal))
        };
        Ok(WakeBinding {
            buffered_events,
            deferred_idle,
        })
    }

    pub fn finish_pending_user_input(&self) -> Option<IdleOutcome> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if !state.input_pending {
            return None;
        }
        state.input_pending = false;
        state
            .deferred_idle
            .take()
            .map(|signal| complete_idle(&mut state, signal))
    }

    pub fn route_stream_event(&self, sequence: u64, value: Value) -> Option<(TurnBinding, Value)> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if let Some(binding) = state.current_turn.clone() {
            Some((binding, value))
        } else if state.pending_wake_id.is_some() {
            state
                .buffered_events
                .push(BufferedEvent { sequence, value });
            None
        } else {
            Some((state.latest_turn.clone(), value))
        }
    }

    pub fn replace_pending(
        &self,
        items: Vec<PendingItem>,
        recent_items: Vec<PendingItem>,
    ) -> (bool, bool) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let mut pending = BTreeMap::new();
        for item in items {
            pending.insert(item.id.clone(), item);
        }
        let changed = pending != state.pending || recent_items != state.recent_items;
        state.pending = pending;
        state.recent_items = recent_items;
        let close = state.phase == InvocationState::Parked && state.pending.is_empty();
        if close {
            state.phase = InvocationState::Closed;
        }
        (changed, close)
    }
    pub fn update_pending_item(&self, item: PendingItem, is_pending: bool) -> (bool, bool) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let changed = if is_pending {
            state.recent_items.retain(|recent| recent.id != item.id);
            let id = item.id.clone();
            state
                .pending
                .insert(id, item.clone())
                .is_none_or(|previous| previous != item)
        } else {
            let was_pending = state.pending.remove(&item.id).is_some();
            let previous = state
                .recent_items
                .iter()
                .find(|recent| recent.id == item.id);
            let changed = was_pending || previous != Some(&item);
            state.recent_items.retain(|recent| recent.id != item.id);
            state.recent_items.push(item);
            if state.recent_items.len() > 32 {
                state.recent_items.remove(0);
            }
            changed
        };
        let close = state.phase == InvocationState::Parked && state.pending.is_empty();
        if close {
            state.phase = InvocationState::Closed;
        }
        (changed, close)
    }

    pub fn delegation_snapshot(&self) -> Option<(TurnBinding, Value)> {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let binding = state
            .current_turn
            .as_ref()
            .unwrap_or(&state.latest_turn)
            .clone();
        if state.pending_wake_id.is_some() {
            return None;
        }
        let mut kinds = serde_json::Map::new();
        for item in state.pending.values() {
            let count = kinds
                .get(&item.kind)
                .and_then(Value::as_u64)
                .unwrap_or_default()
                + 1;
            kinds.insert(item.kind.clone(), Value::from(count));
        }
        Some((
            binding,
            serde_json::json!({
                "count": state.pending.len(),
                "kinds": kinds,
                "items": state.pending.values().map(PendingItem::value).collect::<Vec<_>>(),
                "recent_items": state.recent_items.iter().map(PendingItem::value).collect::<Vec<_>>(),
                "observed_at": chrono::Utc::now().to_rfc3339(),
            }),
        ))
    }

    pub fn idle(&self, signal: IdleSignal) -> Option<IdleOutcome> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.input_pending {
            state.deferred_idle = Some(signal);
            return None;
        }
        if state.current_turn.is_none() && state.pending_wake_id.take().is_some() {
            state.buffered_events.clear();
            state.deferred_idle = None;
        }
        Some(complete_idle(&mut state, signal))
    }

    pub async fn close_input(&self) -> Result<()> {
        let input = self
            .input
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        input.close_input().await
    }

    pub fn take_active_turn(&self) -> Option<TurnBinding> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.phase = InvocationState::Closed;
        let active = state.current_turn.take();
        state.pending_wake_id = None;
        state.buffered_events.clear();
        state.deferred_idle = None;
        state.input_pending = false;
        active
    }

    pub fn process_exited(&self) {
        self.take_active_turn();
        self.mark_stopped();
    }

    pub fn mark_stopped(&self) {
        self.stopped.store(true, Ordering::Release);
        self.stopped_notify.notify_waiters();
    }

    pub async fn wait_stopped(&self) {
        loop {
            if self.stopped.load(Ordering::Acquire) {
                return;
            }
            let notified = self.stopped_notify.notified();
            if self.stopped.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }
}

fn complete_idle(state: &mut State, signal: IdleSignal) -> IdleOutcome {
    let active_turn = state.current_turn.take();
    let has_active_turn = active_turn.is_some();
    let binding = active_turn.unwrap_or_else(|| state.latest_turn.clone());
    state.latest_turn = binding.clone();
    state.pending_wake_id = None;
    state.input_pending = false;
    state.deferred_idle = None;
    let invocation_state = if state.pending.is_empty() {
        InvocationState::Closed
    } else {
        InvocationState::Parked
    };
    state.phase = invocation_state;
    IdleOutcome {
        binding,
        signal,
        invocation_state,
        pending_count: state.pending.len(),
        has_active_turn,
    }
}
type InvocationKey = (String, String);
static INVOCATIONS: LazyLock<Mutex<HashMap<InvocationKey, Arc<ConsoleInvocation>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn invocations() -> &'static Mutex<HashMap<InvocationKey, Arc<ConsoleInvocation>>> {
    &INVOCATIONS
}

pub fn register(invocation: Arc<ConsoleInvocation>) -> Result<()> {
    let key = (
        invocation.provider.clone(),
        invocation.provider_thread_id.clone(),
    );
    let mut registered = invocations()
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if registered.contains_key(&key) {
        bail!("a Console invocation already owns this provider thread");
    }
    registered.insert(key, invocation);
    Ok(())
}

pub fn lookup(provider: &str, provider_thread_id: &str) -> Option<Arc<ConsoleInvocation>> {
    invocations()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .get(&(provider.to_string(), provider_thread_id.to_string()))
        .cloned()
}

pub fn lookup_launch(launch_id: &str) -> Option<Arc<ConsoleInvocation>> {
    invocations()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .values()
        .find(|invocation| invocation.launch_id == launch_id)
        .cloned()
}

pub fn unregister(launch_id: &str) {
    let mut registered = invocations()
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    registered.retain(|_, invocation| invocation.launch_id != launch_id);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    struct FakeInput;

    impl ConsoleInput for FakeInput {
        fn send_input<'a>(&'a self, _text: &'a str, _images: &'a [PathBuf]) -> InputFuture<'a> {
            Box::pin(async { Ok(()) })
        }

        fn close_input(&self) -> InputFuture<'_> {
            Box::pin(async { Ok(()) })
        }
    }

    fn invocation() -> Arc<ConsoleInvocation> {
        Arc::new(ConsoleInvocation::new(
            "claude",
            "provider-thread",
            "launch-1",
            1,
            1,
            TurnBinding {
                run_id: "run-1".to_string(),
                turn_id: None,
                client_request_id: None,
                origin: TurnOrigin::User,
            },
            Arc::new(FakeInput),
        ))
    }

    #[tokio::test]
    async fn idle_parks_only_while_work_remains_and_registry_drain_closes() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![PendingItem {
                id: "task-1".to_string(),
                kind: "monitor".to_string(),
                status: "running".to_string(),
                description: Some("watch files".to_string()),
            }],
            vec![],
        );
        let outcome = invocation
            .idle(IdleSignal {
                terminal_state: "run_completed".to_string(),
                exit_code: Some(0),
                stderr: None,
            })
            .unwrap();
        assert_eq!(outcome.invocation_state, InvocationState::Parked);
        assert_eq!(outcome.pending_count, 1);
        let (changed, close) = invocation.replace_pending(vec![], vec![]);
        assert!(changed);
        assert!(close);
        assert_eq!(invocation.state(), InvocationState::Closed);
    }

    #[tokio::test]
    async fn wake_buffers_events_until_the_matching_turn_binds() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![PendingItem {
                id: "task-1".to_string(),
                kind: "subagent".to_string(),
                status: "running".to_string(),
                description: None,
            }],
            vec![],
        );
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        let wake = invocation
            .response_started(serde_json::json!({"kind": "task_completed"}))
            .unwrap();
        assert_eq!(wake.wake_id, "launch-1:1");
        assert!(invocation
            .route_stream_event(1, serde_json::json!({"type": "assistant"}))
            .is_none());
        let binding = invocation
            .bind_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "wake-run".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                || Ok(()),
            )
            .unwrap();
        assert_eq!(binding.buffered_events.len(), 1);
        assert!(binding.deferred_idle.is_none());
        assert_eq!(invocation.latest_turn().run_id, "wake-run");
    }

    #[tokio::test]
    async fn unbound_wake_idle_drops_events_and_makes_wake_target_gone() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![PendingItem {
                id: "task-1".to_string(),
                kind: "monitor".to_string(),
                status: "running".to_string(),
                description: None,
            }],
            vec![],
        );
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        let wake = invocation
            .response_started(serde_json::json!({"kind": "task_completed"}))
            .unwrap();
        assert!(invocation
            .route_stream_event(1, serde_json::json!({"type": "assistant"}))
            .is_none());

        let outcome = invocation
            .idle(IdleSignal {
                terminal_state: "run_completed".to_string(),
                exit_code: Some(0),
                stderr: None,
            })
            .unwrap();
        assert!(!outcome.has_active_turn);
        assert_eq!(invocation.state(), InvocationState::Parked);
        assert_eq!(invocation.pending_wake_id(), None);
        assert!(invocation.state.lock().unwrap().buffered_events.is_empty());
        let error = invocation
            .bind_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "late-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                || Ok(()),
            )
            .unwrap_err();
        assert!(error.is::<WakeTargetGone>());
    }

    #[tokio::test]
    async fn wake_persistence_failure_leaves_unbound_state_unchanged() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![PendingItem {
                id: "task-1".to_string(),
                kind: "monitor".to_string(),
                status: "running".to_string(),
                description: None,
            }],
            vec![],
        );
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        let wake = invocation
            .response_started(serde_json::json!({"kind": "task_completed"}))
            .unwrap();
        assert!(invocation
            .route_stream_event(1, serde_json::json!({"type": "assistant"}))
            .is_none());

        assert!(invocation
            .bind_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "failed-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                || Err(anyhow::anyhow!("claim persistence failed")),
            )
            .is_err());
        assert_eq!(invocation.latest_turn().run_id, "run-1");
        assert_eq!(invocation.pending_wake_id(), Some(wake.wake_id));
        assert_eq!(invocation.state(), InvocationState::Responding);
        assert_eq!(invocation.state.lock().unwrap().buffered_events.len(), 1);
    }

    #[tokio::test]
    async fn parked_user_turn_reuses_invocation() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![PendingItem {
                id: "task-1".to_string(),
                kind: "shell".to_string(),
                status: "running".to_string(),
                description: None,
            }],
            vec![],
        );
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        invocation
            .send_user_input(
                TurnBinding {
                    run_id: "run-2".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::User,
                },
                "continue",
                &[],
            )
            .await
            .unwrap();
        assert_eq!(invocation.process_identity().0, 1);
        assert_eq!(invocation.latest_turn().run_id, "run-2");
        assert_eq!(invocation.state(), InvocationState::Responding);
    }
}
