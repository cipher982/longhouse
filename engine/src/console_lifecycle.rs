use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub type InputFuture<'a> = Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>>;

pub const USER_STOP_INPUT_GRACE: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvocationCloseReason {
    UserStop,
    MachineAgentRestart,
}

impl InvocationCloseReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::UserStop => "user_stop",
            Self::MachineAgentRestart => "machine_agent_restart",
        }
    }
}

/// What a close proved about the invocation's processes.
///
/// Closing provider input closes the invocation; whether its process group is
/// gone is a separate fact, and the user is told when it is not.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvocationCleanup {
    /// No process of the invocation's group is left.
    Complete,
    /// The group was proven ours and killed, but a process outlived SIGKILL.
    Survivors,
    /// A live group could not be proven ours, so it was never signalled.
    Unverified,
}

impl InvocationCleanup {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Complete => "complete",
            Self::Survivors => "survivors",
            Self::Unverified => "unverified",
        }
    }
}

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

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
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

pub(crate) const RETAINED_WAKE_TTL: Duration = Duration::from_secs(10 * 60);

#[derive(Clone, Debug)]
pub struct WakeBinding {
    pub buffered_events: Vec<BufferedEvent>,
    pub deferred_idle: Option<IdleOutcome>,
}
struct State {
    phase: InvocationState,
    current_turn: Option<TurnBinding>,
    latest_turn: TurnBinding,
    queued_turn: Option<TurnBinding>,
    pending: BTreeMap<String, PendingItem>,
    recent_items: Vec<PendingItem>,
    wake_seq: u64,
    pending_wake_id: Option<String>,
    buffered_events: Vec<BufferedEvent>,
    deferred_idle: Option<IdleSignal>,
    input_pending: bool,
    closing: bool,
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
                queued_turn: None,
                pending: BTreeMap::new(),
                recent_items: Vec::new(),
                wake_seq: 0,
                pending_wake_id: None,
                buffered_events: Vec::new(),
                deferred_idle: None,
                input_pending: false,
                closing: false,
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

    pub fn pending_items(&self) -> Vec<PendingItem> {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pending
            .values()
            .cloned()
            .collect()
    }
    pub fn persist_pending_claim(&self) -> Result<()> {
        let binding = self.latest_turn();
        crate::turn_claims::default_registry()?
            .record_invocation_pending_items(&binding.run_id, self.pending_items())?;
        Ok(())
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
            if state.phase != InvocationState::Parked || state.pending.is_empty() || state.closing {
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
    pub async fn queue_user_input(
        &self,
        binding: TurnBinding,
        text: &str,
        images: &[PathBuf],
    ) -> Result<()> {
        {
            let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
            if state.phase != InvocationState::Responding
                || state.current_turn.is_none()
                || state.pending_wake_id.is_some()
                || state.queued_turn.is_some()
                || state.closing
            {
                bail!("Console invocation cannot queue another user turn");
            }
            if binding.origin != TurnOrigin::User {
                bail!("only a user turn can be queued during a response");
            }
            state.queued_turn = Some(binding.clone());
        }
        self.write_input(text, images).await
    }
    pub fn has_queued_user_turn(&self, run_id: &str) -> bool {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .queued_turn
            .as_ref()
            .is_some_and(|binding| binding.run_id == run_id)
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
    pub fn expire_pending_wake(&self, wake_id: &str, signal: IdleSignal) -> Option<IdleOutcome> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.phase != InvocationState::Responding
            || state.current_turn.is_some()
            || state.pending_wake_id.as_deref() != Some(wake_id)
        {
            return None;
        }
        state.buffered_events.clear();
        Some(complete_idle(&mut state, signal))
    }

    pub fn response_started(&self, trigger: Value) -> Option<WakeRequest> {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.phase == InvocationState::Responding && state.current_turn.is_none() {
            if let Some(binding) = state.queued_turn.take() {
                state.latest_turn = binding.clone();
                state.current_turn = Some(binding);
                state.pending_wake_id = None;
                state.buffered_events.clear();
                state.deferred_idle = None;
                return None;
            }
        }
        if state.phase != InvocationState::Parked || state.pending.is_empty() || state.closing {
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
            || state.closing
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
            || state.closing
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
        } else if state.queued_turn.is_some() {
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
        if state.closing {
            return (false, false);
        }
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
    pub fn remove_pending_item(&self, id: &str) -> (bool, bool) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.closing {
            return (false, false);
        }
        let changed = state.pending.remove(id).is_some();
        if changed {
            state.recent_items.retain(|item| item.id != id);
        }
        let close = state.phase == InvocationState::Parked && state.pending.is_empty();
        if close {
            state.phase = InvocationState::Closed;
        }
        (changed, close)
    }
    pub fn replace_pending_placeholder(
        &self,
        placeholder_id: &str,
        updates: Vec<(PendingItem, bool)>,
    ) -> (bool, bool) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.closing {
            return (false, false);
        }
        let mut changed = state.pending.remove(placeholder_id).is_some();
        let recent_len = state.recent_items.len();
        state.recent_items.retain(|item| item.id != placeholder_id);
        changed |= state.recent_items.len() != recent_len;
        for (item, is_pending) in updates {
            changed |= update_pending_item(&mut state, item, is_pending);
        }
        let close = state.phase == InvocationState::Parked && state.pending.is_empty();
        if close {
            state.phase = InvocationState::Closed;
        }
        (changed, close)
    }
    pub fn update_pending_item(&self, item: PendingItem, is_pending: bool) -> (bool, bool) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.closing {
            return (false, false);
        }
        let changed = update_pending_item(&mut state, item, is_pending);
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
        if state.closing {
            return None;
        }
        if state.input_pending {
            state.deferred_idle = Some(signal);
            return None;
        }
        if state.queued_turn.is_some() && state.current_turn.is_some() {
            let binding = state.current_turn.take().unwrap();
            state.latest_turn = binding.clone();
            state.phase = InvocationState::Responding;
            state.pending_wake_id = None;
            state.buffered_events.clear();
            state.deferred_idle = None;
            return Some(IdleOutcome {
                binding,
                signal,
                invocation_state: InvocationState::Responding,
                pending_count: state.pending.len(),
                has_active_turn: true,
            });
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

    fn begin_user_close(&self) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.phase != InvocationState::Parked
            || state.pending.is_empty()
            || state.current_turn.is_some()
            || state.queued_turn.is_some()
            || state.closing
        {
            return false;
        }
        state.closing = true;
        true
    }

    fn cancel_user_close(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .closing = false;
    }

    fn finish_user_close(&self) -> InvocationCloseOutcome {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.phase = InvocationState::Closed;
        state.closing = false;
        state.pending_wake_id = None;
        state.buffered_events.clear();
        state.deferred_idle = None;
        state.input_pending = false;
        state.recent_items.clear();
        let outcome = InvocationCloseOutcome {
            invocation_id: self.launch_id.clone(),
            stopped: std::mem::take(&mut state.pending).into_values().collect(),
            cleanup: InvocationCleanup::Complete,
            error_note: None,
        };
        discard_retained_wakes(&self.provider, &self.provider_thread_id);
        outcome
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
        state.pending_wake_id = None;
        state.buffered_events.clear();
        state.deferred_idle = None;
        state.input_pending = false;
        state.current_turn.take()
    }
    pub fn take_queued_turn(&self) -> Option<TurnBinding> {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .queued_turn
            .take()
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
fn update_pending_item(state: &mut State, item: PendingItem, is_pending: bool) -> bool {
    if is_pending {
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

#[derive(Clone, Debug)]
pub struct InvocationCloseOutcome {
    pub invocation_id: String,
    pub stopped: Vec<PendingItem>,
    pub cleanup: InvocationCleanup,
    /// Everything that went wrong, cleanup and bookkeeping alike.
    pub error_note: Option<String>,
}

pub async fn close_parked_invocation(
    invocation: &Arc<ConsoleInvocation>,
    claim: &crate::turn_claims::TurnClaim,
    machine_name: &str,
    source: &str,
    outbox_dir: Result<PathBuf>,
) -> Result<Option<InvocationCloseOutcome>> {
    let latest_turn = invocation.latest_turn();
    if claim.launch_id.as_deref() != Some(invocation.launch_id.as_str())
        || claim.provider != invocation.provider
        || claim.provider_thread_id.as_deref() != Some(invocation.provider_thread_id.as_str())
        || claim.run_id != latest_turn.run_id
        || claim.state != "terminal"
        || claim.invocation_state.as_deref() != Some("parked")
        || claim.pending_count == 0
        || !invocation.begin_user_close()
    {
        return Ok(None);
    }

    if let Err(error) = invocation.close_input().await {
        invocation.cancel_user_close();
        return Err(error).context("closing Console provider input");
    }
    tokio::time::sleep(USER_STOP_INPUT_GRACE).await;

    // Stop the group before anything is recorded or announced. Provider input
    // is already closed, so the invocation can never take another turn: it is
    // closed from here on even if a process survives SIGKILL (an
    // uninterruptible kernel wait) or the group cannot be proven ours. Either
    // way the error note says what was left, and the managed-process janitor
    // owns it.
    let (_, process_group_id) = invocation.process_identity();
    let stopped = invocation.pending_items();
    let (cleanup, mut error_note) = stop_invocation_group(claim, process_group_id).await;
    if let Some(message) = &error_note {
        tracing::error!(run_id = %claim.run_id, "{message}");
    }

    // Provider input is closed, so the invocation is closed whatever happens
    // below: every failure only adds to the note.
    match crate::turn_claims::default_registry() {
        Ok(claims) => {
            if let Err(error) =
                claims.record_invocation_pending_items(&claim.run_id, stopped.clone())
            {
                tracing::warn!(
                    %error,
                    run_id = %claim.run_id,
                    "Failed to persist stopped Console pending items"
                );
                append_close_error(
                    &mut error_note,
                    format!("failed to persist stopped Console pending items: {error}"),
                );
            }
            let outbox_dir = outbox_dir.context("resolving Console close event outbox");
            let published = publish_invocation_closed(
                &claims,
                outbox_dir
                    .as_deref()
                    .map_err(|error| anyhow::anyhow!("{error:#}")),
                claim,
                machine_name,
                source,
                InvocationCloseReason::UserStop,
                cleanup,
                &stopped,
            );
            if let Ok(false) = published {
                append_close_error(
                    &mut error_note,
                    "Console invocation close event is retained for replay".to_string(),
                );
            }
            if let Err(error) = published {
                tracing::warn!(
                    %error,
                    run_id = %claim.run_id,
                    "Failed to publish Console invocation close"
                );
                append_close_error(
                    &mut error_note,
                    format!("failed to publish Console invocation close: {error}"),
                );
            }
            if let Err(error) =
                claims.record_invocation_state(&claim.run_id, "closed", stopped.len())
            {
                tracing::warn!(
                    %error,
                    run_id = %claim.run_id,
                    "Failed to mark Console invocation claim closed"
                );
                append_close_error(
                    &mut error_note,
                    format!("failed to mark Console invocation claim closed: {error}"),
                );
            }
        }
        Err(error) => {
            tracing::warn!(
                %error,
                run_id = %claim.run_id,
                "Failed to open Console turn claim registry while closing invocation"
            );
            append_close_error(
                &mut error_note,
                format!("failed to open Console turn claim registry: {error}"),
            );
        }
    }

    unregister(&invocation.launch_id);

    let mut outcome = invocation.finish_user_close();
    invocation.process_exited();
    outcome.cleanup = cleanup;
    outcome.error_note = error_note;
    Ok(Some(outcome))
}

/// How long the provider's own monitor gets to finish with the group before a
/// close calls anything left a survivor.
///
/// The leader is our child, held by the monitor task, and `killpg(pgid, 0)`
/// counts it until that task reaps it. Claude and OMP monitors poll
/// `try_wait` every 100 ms; Codex tears its worker down with
/// `shutdown_owned_child` as soon as input closes, which the 2 s
/// `USER_STOP_INPUT_GRACE` already covers. One second is ten monitor polls,
/// enough to absorb a slow process-inventory scan in a monitor iteration.
const OWNER_REAP_SETTLE: Duration = Duration::from_secs(1);

/// Stop the invocation's process group if it is still provably ours, and
/// return what that proved plus a note for anything left alive.
///
/// The group is signalled only after a recorded process (pid, birth time and
/// current pgid) shows it is still ours, checked immediately before signalling.
/// A pgid is never reallocated while any member lives, so a live group that
/// fails that check is either ours with no recorded member left to prove it,
/// or a reused id and ours is gone. Neither is signalled.
async fn stop_invocation_group(
    claim: &crate::turn_claims::TurnClaim,
    process_group_id: i32,
) -> (InvocationCleanup, Option<String>) {
    if !crate::process_group::group_is_alive(process_group_id) {
        return (InvocationCleanup::Complete, None);
    }
    let verified = claim.process_group_id == Some(process_group_id)
        && claim.process_group_is_from_this_boot()
        && crate::process_identity::try_collect_process_facts_by_pid()
            .is_some_and(|inventory| claim.has_live_group_identity(&inventory));
    if !verified {
        if crate::process_group::wait_for_group_exit(process_group_id, OWNER_REAP_SETTLE).await {
            return (InvocationCleanup::Complete, None);
        }
        return (
            InvocationCleanup::Unverified,
            Some(format!(
                "Console process group {process_group_id} is still alive but could not be \
                 verified as this invocation's, so it was not signalled"
            )),
        );
    }
    let shutdown = crate::process_group::shutdown_group(process_group_id, Duration::ZERO).await;
    if shutdown.is_gone()
        || crate::process_group::wait_for_group_exit(process_group_id, OWNER_REAP_SETTLE).await
    {
        return (InvocationCleanup::Complete, None);
    }
    (
        InvocationCleanup::Survivors,
        Some(format!(
            "Console process group {process_group_id} survived close: {}",
            shutdown.as_str()
        )),
    )
}

fn append_close_error(error_note: &mut Option<String>, message: String) {
    if let Some(existing) = error_note {
        existing.push_str("; ");
        existing.push_str(&message);
    } else {
        *error_note = Some(message);
    }
}

pub fn stopped_items_for_claim(claim: &crate::turn_claims::TurnClaim) -> Vec<PendingItem> {
    if !claim.pending_items.is_empty() {
        return claim.pending_items.clone();
    }
    if claim.pending_count == 0 {
        return Vec::new();
    }
    vec![PendingItem {
        id: "unknown".to_string(),
        kind: "background".to_string(),
        status: "stopped".to_string(),
        description: Some(format!(
            "{} background task(s); task details were not recorded",
            claim.pending_count
        )),
    }]
}

/// Publish an invocation's closure through its claim.
///
/// The exact `invocation_closed` event is retained in the claim before any
/// handoff, so the daemon replays it (with the empty delegation snapshot that
/// clears the pending work) until it reaches the durable outbox; that handoff
/// is what makes the claim's `closed` final. Returns whether it was handed off
/// now. Without an outbox the event is still retained, and this reports why it
/// could not be handed off.
pub fn publish_invocation_closed(
    registry: &crate::turn_claims::TurnClaimRegistry,
    outbox_dir: Result<&std::path::Path>,
    claim: &crate::turn_claims::TurnClaim,
    machine_name: &str,
    source: &str,
    reason: InvocationCloseReason,
    cleanup: InvocationCleanup,
    stopped: &[PendingItem],
) -> Result<bool> {
    anyhow::ensure!(
        !stopped.is_empty(),
        "an invocation close must name what it stopped"
    );
    let stopped = stopped
        .iter()
        .map(|item| {
            serde_json::json!({
                "id": item.id,
                "kind": item.kind,
                "description": item.description,
            })
        })
        .collect::<Vec<_>>();
    let invocation_id = claim.launch_id.as_deref().unwrap_or(&claim.run_id);
    registry.retain_invocation_close_event(
        &claim.run_id,
        serde_json::json!({
            "runtime_key": format!("{}:{}", claim.provider, claim.session_id),
            "session_id": claim.session_id,
            "thread_id": claim.thread_id,
            "run_id": claim.run_id,
            "provider": claim.provider,
            "device_id": machine_name,
            "source": source,
            "kind": "invocation_closed",
            "occurred_at": chrono::Utc::now().to_rfc3339(),
            "dedupe_key": format!("close:{invocation_id}"),
            "payload": {
                "invocation_id": invocation_id,
                "reason": reason.as_str(),
                "cleanup": cleanup.as_str(),
                "stopped": stopped,
            }
        }),
    )?;
    crate::outbox::retry_retained_invocation_close_event(registry, outbox_dir?, &claim.run_id)
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
    #[test]
    fn expiring_unbound_wake_returns_to_normal_pending_state() {
        let invocation = invocation();
        invocation.replace_pending(
            vec![
                PendingItem {
                    id: "task-1".to_string(),
                    kind: "monitor".to_string(),
                    status: "running".to_string(),
                    description: None,
                },
                PendingItem {
                    id: "task-2".to_string(),
                    kind: "monitor".to_string(),
                    status: "running".to_string(),
                    description: None,
                },
            ],
            vec![],
        );
        invocation
            .idle(IdleSignal {
                terminal_state: "run_completed".to_string(),
                exit_code: Some(0),
                stderr: None,
            })
            .unwrap();
        let first = invocation
            .response_started(serde_json::json!({"kind": "task_completed"}))
            .unwrap();
        invocation.update_pending_item(
            PendingItem {
                id: "task-1".to_string(),
                kind: "monitor".to_string(),
                status: "completed".to_string(),
                description: None,
            },
            false,
        );
        let parked = invocation
            .expire_pending_wake(
                &first.wake_id,
                IdleSignal {
                    terminal_state: "run_completed".to_string(),
                    exit_code: Some(0),
                    stderr: None,
                },
            )
            .unwrap();
        assert_eq!(parked.invocation_state, InvocationState::Parked);
        assert_eq!(parked.pending_count, 1);
        assert!(!parked.has_active_turn);
        assert_eq!(invocation.pending_wake_id(), None);

        let second = invocation
            .response_started(serde_json::json!({"kind": "task_completed"}))
            .unwrap();
        invocation.update_pending_item(
            PendingItem {
                id: "task-2".to_string(),
                kind: "monitor".to_string(),
                status: "completed".to_string(),
                description: None,
            },
            false,
        );
        let closed = invocation
            .expire_pending_wake(
                &second.wake_id,
                IdleSignal {
                    terminal_state: "run_completed".to_string(),
                    exit_code: Some(0),
                    stderr: None,
                },
            )
            .unwrap();
        assert_eq!(closed.invocation_state, InvocationState::Closed);
        assert_eq!(closed.pending_count, 0);
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

    /// A sleeper leading its own process group, with a second member so the
    /// group outlives nothing but a group-wide signal.
    fn spawn_owned_group() -> tokio::process::Child {
        let mut command = tokio::process::Command::new("/bin/sh");
        command
            .arg("-c")
            .arg("sleep 300 & sleep 300")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().expect("spawn test process group")
    }

    /// The claim a parking provider writes at spawn, for a leader `pid`.
    fn spawned_claim(pid: u32, start: Option<String>) -> crate::turn_claims::TurnClaim {
        let dir = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(dir.path().to_path_buf());
        let run_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "claude")
            .unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                pid,
                pid as i32,
                start,
                "claude-print",
                "launch-1",
                Some("provider-thread"),
                "",
                "",
                serde_json::json!({}),
            )
            .unwrap()
    }

    #[test]
    fn a_close_is_retained_for_replay_even_without_an_outbox() {
        let dir = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(dir.path().join("claims"));
        let run_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "claude")
            .unwrap();
        let claim = registry.read(&run_id).unwrap();
        let stopped = [PendingItem {
            id: "task-1".to_string(),
            kind: "monitor".to_string(),
            status: "running".to_string(),
            description: Some("watch files".to_string()),
        }];

        let error = publish_invocation_closed(
            &registry,
            Err(anyhow::anyhow!("no runtime outbox")),
            &claim,
            "box",
            "claude_console",
            InvocationCloseReason::UserStop,
            InvocationCleanup::Unverified,
            &stopped,
        )
        .unwrap_err();
        assert!(error.to_string().contains("no runtime outbox"));
        let retained = registry.read(&run_id).unwrap();
        let close = retained.invocation_close_event.expect("close retained");
        assert_eq!(close["payload"]["stopped"][0]["id"], "task-1");
        assert_eq!(close["payload"]["cleanup"], "unverified");
        assert!(!retained.invocation_close_event_handed_off);

        // The daemon's replay hands off the close with its cleared delegation.
        let outbox = dir.path().join("outbox");
        assert!(
            crate::outbox::retry_retained_invocation_close_event(&registry, &outbox, &run_id)
                .unwrap()
        );
        let events = std::fs::read_dir(&outbox)
            .unwrap()
            .flatten()
            .map(|entry| serde_json::from_slice::<Value>(&std::fs::read(entry.path()).unwrap()))
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert!(events.iter().any(|event| event == &close));
        let cleared = events
            .iter()
            .find(|event| event["kind"] == "delegation_signal")
            .expect("cleared delegation replays with the close");
        assert_eq!(cleared["payload"]["delegation"]["count"], 0);
        assert_eq!(cleared["run_id"], run_id);
        assert_eq!(cleared["dedupe_key"], format!("close:{run_id}:delegation"));
        let closed = registry.read(&run_id).unwrap();
        assert!(closed.invocation_close_event_handed_off);
        assert_eq!(closed.invocation_state.as_deref(), Some("closed"));
    }

    #[tokio::test]
    async fn close_never_signals_a_live_group_it_cannot_prove_is_ours() {
        // The recorded leader's birth time does not match the live process
        // holding that pid: exactly what a reused pgid looks like.
        let mut child = spawn_owned_group();
        let pid = child.id().unwrap();
        let claim = spawned_claim(pid, Some("Thu Jan  1 00:00:00 1970".to_string()));
        assert!(claim.process_group_is_from_this_boot());

        let (cleanup, note) = stop_invocation_group(&claim, pid as i32).await;
        assert_eq!(cleanup, InvocationCleanup::Unverified);
        let note = note.expect("an unverified live group is named in the close note");

        assert!(note.contains("could not be verified"), "{note}");
        assert!(note.contains("not signalled"), "{note}");
        assert!(
            matches!(child.try_wait(), Ok(None)),
            "an unverified group was signalled"
        );
        assert!(crate::process_group::group_is_alive(pid as i32));
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(pid as i32),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
    }

    #[tokio::test]
    async fn close_stops_a_group_whose_recorded_leader_is_still_ours() {
        let mut child = spawn_owned_group();
        let pid = child.id().unwrap();
        let start = crate::turn_claims::process_start_time_for_pid(Some(pid));
        assert!(start.is_some(), "spawn-time identity probe failed");
        let claim = spawned_claim(pid, start);
        // The provider monitor owns the Child and reaps it.
        let monitor = tokio::spawn(async move { child.wait().await });

        assert_eq!(
            stop_invocation_group(&claim, pid as i32).await,
            (InvocationCleanup::Complete, None)
        );

        assert!(!crate::process_group::group_is_alive(pid as i32));
        assert!(!monitor.await.unwrap().unwrap().success());
    }

    #[tokio::test]
    async fn close_waits_for_the_monitor_to_reap_before_calling_the_group_a_survivor() {
        // A monitor that reaps less often than the kill-confirm budget leaves
        // the dead leader a zombie past `shutdown_group`'s own check, and
        // `killpg(pgid, 0)` still counts it. That is macOS behaviour; the
        // Linux test container does not count the zombie, so this only bites
        // when run natively on a Mac.
        const MONITOR_POLL: Duration = Duration::from_millis(700);
        assert!(MONITOR_POLL > crate::process_group::KILL_CONFIRM_BUDGET);
        assert!(MONITOR_POLL * 2 < crate::process_group::KILL_CONFIRM_BUDGET + OWNER_REAP_SETTLE);
        let mut child = spawn_owned_group();
        let pid = child.id().unwrap();
        let claim = spawned_claim(
            pid,
            crate::turn_claims::process_start_time_for_pid(Some(pid)),
        );
        let monitor = tokio::spawn(async move {
            loop {
                if let Ok(Some(status)) = child.try_wait() {
                    return status;
                }
                tokio::time::sleep(MONITOR_POLL).await;
            }
        });

        assert_eq!(
            stop_invocation_group(&claim, pid as i32).await,
            (InvocationCleanup::Complete, None)
        );

        assert!(!crate::process_group::group_is_alive(pid as i32));
        assert!(!monitor.await.unwrap().success());
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
        StopWhileParked,
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

    const PHASE_ONE_SCENARIOS: [(u8, LifecycleScenario); 7] = [
        (1, LifecycleScenario::Plain),
        (2, LifecycleScenario::Background),
        (3, LifecycleScenario::WakePending),
        (4, LifecycleScenario::UserSend),
        (5, LifecycleScenario::WakeDrained),
        (6, LifecycleScenario::StopWhileParked),
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
