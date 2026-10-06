use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

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
#[derive(Clone)]
struct RetainedWake {
    invocation: Arc<ConsoleInvocation>,
    wake_id: String,
    buffered_events: Vec<BufferedEvent>,
    signal: IdleSignal,
    expires_at: Instant,
}

const RETAINED_WAKE_TTL: Duration = Duration::from_secs(10 * 60);

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
            discard_retained_wakes(&self.provider, &self.provider_thread_id);
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
    pub fn bind_retained_wake<F>(
        &self,
        invocation_id: &str,
        wake_id: &str,
        binding: TurnBinding,
        persist: F,
    ) -> Result<WakeBinding>
    where
        F: FnOnce(InvocationState) -> Result<()>,
    {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if invocation_id != self.launch_id
            || binding.origin != TurnOrigin::Wake
            || state.current_turn.is_some()
            || state.pending_wake_id.is_some()
            || !matches!(
                state.phase,
                InvocationState::Parked | InvocationState::Closed
            )
        {
            return Err(WakeTargetGone.into());
        }
        let mut retained = retained_wakes()
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        prune_retained_wakes(&mut retained);
        let Some(response) = retained.get(wake_id) else {
            return Err(WakeTargetGone.into());
        };
        if response.invocation.launch_id != self.launch_id
            || response.invocation.provider != self.provider
            || response.invocation.provider_thread_id != self.provider_thread_id
        {
            return Err(WakeTargetGone.into());
        }
        persist(state.phase)?;
        let response = retained.remove(wake_id).unwrap();
        state.latest_turn = binding.clone();
        state.input_pending = false;
        state.deferred_idle = None;
        state.buffered_events.clear();
        // A process that already exited stays closed: a late bind only
        // delivers the retained response, it never revives the invocation.
        let invocation_state = if state.phase == InvocationState::Closed
            || self.stopped.load(Ordering::Acquire)
            || state.pending.is_empty()
        {
            InvocationState::Closed
        } else {
            InvocationState::Parked
        };
        state.phase = invocation_state;
        Ok(WakeBinding {
            buffered_events: response.buffered_events,
            deferred_idle: Some(IdleOutcome {
                binding,
                signal: response.signal,
                invocation_state,
                pending_count: state.pending.len(),
                has_active_turn: true,
            }),
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

    pub fn idle(self: &Arc<Self>, signal: IdleSignal) -> Option<IdleOutcome> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.input_pending {
            state.deferred_idle = Some(signal);
            return None;
        }
        let retained = if state.current_turn.is_none() {
            state.pending_wake_id.take().map(|wake_id| RetainedWake {
                invocation: self.clone(),
                wake_id,
                buffered_events: std::mem::take(&mut state.buffered_events),
                signal: signal.clone(),
                expires_at: Instant::now() + RETAINED_WAKE_TTL,
            })
        } else {
            None
        };
        let outcome = complete_idle(&mut state, signal);
        if let Some(response) = retained {
            retain_unbound_wake(response);
        }
        Some(outcome)
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

type RetainedWakeRegistry = HashMap<String, RetainedWake>;
static RETAINED_WAKES: LazyLock<Mutex<RetainedWakeRegistry>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn retained_wakes() -> &'static Mutex<RetainedWakeRegistry> {
    &RETAINED_WAKES
}

fn prune_retained_wakes(retained: &mut RetainedWakeRegistry) {
    let now = Instant::now();
    retained.retain(|_, wake| wake.expires_at > now);
}

fn retain_unbound_wake(response: RetainedWake) {
    let wake_id = response.wake_id.clone();
    let expires_at = response.expires_at;
    let mut retained = retained_wakes()
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    prune_retained_wakes(&mut retained);
    retained.retain(|_, wake| {
        wake.invocation.provider != response.invocation.provider
            || wake.invocation.provider_thread_id != response.invocation.provider_thread_id
    });
    retained.insert(wake_id.clone(), response);
    drop(retained);

    if let Ok(runtime) = tokio::runtime::Handle::try_current() {
        runtime.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(expires_at)).await;
            let mut retained = retained_wakes()
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if retained
                .get(&wake_id)
                .is_some_and(|wake| wake.expires_at <= Instant::now())
            {
                retained.remove(&wake_id);
            }
        });
    }
}

pub fn lookup_retained_wake(invocation_id: &str, wake_id: &str) -> Option<Arc<ConsoleInvocation>> {
    let mut retained = retained_wakes()
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    prune_retained_wakes(&mut retained);
    retained.get(wake_id).and_then(|wake| {
        (wake.invocation.launch_id == invocation_id).then(|| wake.invocation.clone())
    })
}

pub fn discard_retained_wakes(provider: &str, provider_thread_id: &str) {
    let mut retained = retained_wakes()
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    prune_retained_wakes(&mut retained);
    retained.retain(|_, wake| {
        wake.invocation.provider != provider
            || wake.invocation.provider_thread_id != provider_thread_id
    });
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
        let identity = uuid::Uuid::new_v4().to_string();
        Arc::new(ConsoleInvocation::new(
            "claude",
            format!("provider-thread-{identity}"),
            format!("launch-{identity}"),
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
        assert_eq!(wake.wake_id, format!("{}:1", invocation.launch_id));
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
    async fn unbound_wake_idle_retains_events_and_terminal_after_close() {
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
        invocation.replace_pending(vec![], vec![]);

        let outcome = invocation
            .idle(IdleSignal {
                terminal_state: "run_completed".to_string(),
                exit_code: Some(0),
                stderr: None,
            })
            .unwrap();
        assert!(!outcome.has_active_turn);
        assert_eq!(outcome.invocation_state, InvocationState::Closed);
        assert_eq!(invocation.state(), InvocationState::Closed);
        assert_eq!(invocation.pending_wake_id(), None);
        assert!(invocation
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .buffered_events
            .is_empty());
        assert!(lookup_retained_wake(&wake.invocation_id, &wake.wake_id).is_some());

        let binding = invocation
            .bind_retained_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "late-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                |_| Ok(()),
            )
            .unwrap();
        assert_eq!(binding.buffered_events.len(), 1);
        assert_eq!(
            binding.buffered_events[0].value,
            serde_json::json!({"type": "assistant"})
        );
        let terminal = binding.deferred_idle.unwrap();
        assert_eq!(terminal.binding.run_id, "late-wake");
        assert_eq!(terminal.signal.terminal_state, "run_completed");
        assert_eq!(terminal.invocation_state, InvocationState::Closed);
        assert!(terminal.has_active_turn);
        assert!(lookup_retained_wake(&wake.invocation_id, &wake.wake_id).is_none());
        assert_eq!(invocation.latest_turn().run_id, "late-wake");
        let error = invocation
            .bind_retained_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "duplicate-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                |_| Ok(()),
            )
            .unwrap_err();
        assert!(error.is::<WakeTargetGone>());
    }

    #[tokio::test]
    async fn a_late_wake_bind_never_revives_an_exited_invocation() {
        let invocation = invocation();
        let pending = vec![PendingItem {
            id: "task-1".to_string(),
            kind: "monitor".to_string(),
            status: "running".to_string(),
            description: None,
        }];
        invocation.replace_pending(pending, vec![]);
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        let wake = invocation
            .response_started(serde_json::json!({"kind": "monitor_event"}))
            .unwrap();
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        // The provider exits while its registry still lists the monitor.
        invocation.mark_stopped();
        let binding = invocation
            .bind_retained_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "late-wake-after-exit".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                |_| Ok(()),
            )
            .unwrap();
        assert_eq!(
            binding.deferred_idle.unwrap().invocation_state,
            InvocationState::Closed
        );
        assert_eq!(invocation.state(), InvocationState::Closed);
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
    #[tokio::test]
    async fn a_later_user_turn_discards_a_retained_wake_response() {
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
        invocation.route_stream_event(1, serde_json::json!({"type": "assistant"}));
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        assert!(lookup_retained_wake(&wake.invocation_id, &wake.wake_id).is_some());

        invocation
            .send_user_input(
                TurnBinding {
                    run_id: "user-after-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::User,
                },
                "continue",
                &[],
            )
            .await
            .unwrap();
        assert!(lookup_retained_wake(&wake.invocation_id, &wake.wake_id).is_none());
    }

    #[tokio::test]
    async fn a_newer_retained_wake_replaces_the_older_response() {
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
        let first = invocation
            .response_started(serde_json::json!({"kind": "first"}))
            .unwrap();
        invocation.route_stream_event(1, serde_json::json!({"type": "assistant"}));
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        let second = invocation
            .response_started(serde_json::json!({"kind": "second"}))
            .unwrap();
        invocation.route_stream_event(2, serde_json::json!({"type": "assistant"}));
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });

        assert!(lookup_retained_wake(&first.invocation_id, &first.wake_id).is_none());
        assert!(lookup_retained_wake(&second.invocation_id, &second.wake_id).is_some());
    }

    #[tokio::test]
    async fn retained_wake_responses_expire_after_ten_minutes() {
        assert_eq!(RETAINED_WAKE_TTL, Duration::from_secs(600));
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
        invocation.idle(IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        });
        retained_wakes()
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .get_mut(&wake.wake_id)
            .unwrap()
            .expires_at = Instant::now() - Duration::from_secs(1);

        assert!(lookup_retained_wake(&wake.invocation_id, &wake.wake_id).is_none());
        let error = invocation
            .bind_retained_wake(
                &wake.invocation_id,
                &wake.wake_id,
                TurnBinding {
                    run_id: "expired-wake".to_string(),
                    turn_id: None,
                    client_request_id: None,
                    origin: TurnOrigin::Wake,
                },
                |_| Ok(()),
            )
            .unwrap_err();
        assert!(error.is::<WakeTargetGone>());
    }
}
#[cfg(test)]
pub(crate) mod conformance {
    use std::future::Future;
    use std::pin::Pin;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub(crate) enum LifecycleScenario {
        Plain,
        Background,
        WakePending,
        UserSend,
        WakeDrained,
        Restart,
        WakeUserSend,
        WakeUnboundDrained,
        WakeImmediateUnbound,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub(crate) enum ScenarioOutcome {
        Passed,
        Unsupported(&'static str),
    }

    pub(crate) type ScenarioFuture = Pin<Box<dyn Future<Output = ScenarioOutcome> + 'static>>;
    pub(crate) type ScenarioRunner = fn(LifecycleScenario) -> ScenarioFuture;

    const PHASE_ONE_SCENARIOS: [(u8, LifecycleScenario); 6] = [
        (1, LifecycleScenario::Plain),
        (2, LifecycleScenario::Background),
        (3, LifecycleScenario::WakePending),
        (4, LifecycleScenario::UserSend),
        (5, LifecycleScenario::WakeDrained),
        (8, LifecycleScenario::Restart),
    ];

    pub(crate) async fn run_phase_one(adapters: &[(&'static str, ScenarioRunner)]) {
        for (provider, run) in adapters {
            for (scenario_id, scenario) in PHASE_ONE_SCENARIOS {
                let case = format!("{provider} console lifecycle scenario {scenario_id}");
                match run(scenario).await {
                    ScenarioOutcome::Passed => {}
                    ScenarioOutcome::Unsupported(disposition) => {
                        assert!(
                            !disposition.trim().is_empty(),
                            "{case} has no unsupported disposition"
                        );
                        eprintln!("{case}: disposition={disposition}");
                    }
                }
            }
        }
    }
}
