//! Native terminal owner for Cursor Helm.
//!
//! This module deliberately owns only the PTY and its on-disk control contract.
//! Machine Agent control remains in `cursor_helm_control`.

use std::ffi::CString;
use std::fs;
use std::io::{IsTerminal, Read, Write};
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;

use anyhow::Context;
use chrono::Utc;
use serde_json::{json, Value};
use uuid::Uuid;

const STATE_DIR: &str = "managed-local/cursor-helm";

pub struct LaunchConfig {
    pub cwd: PathBuf,
    pub project: Option<String>,
    pub name: Option<String>,
    pub loop_mode: String,
    pub url: Option<String>,
    pub token: Option<String>,
    pub resume_session: Option<String>,
    pub cursor_bin: Option<String>,
    pub config_dir: Option<PathBuf>,
    pub permission_mode: String,
    pub verbose: bool,
    pub open: bool,
    pub cursor_args: Vec<String>,
}

fn home(config_dir: Option<&Path>) -> anyhow::Result<PathBuf> {
    Ok(config_dir
        .map(Path::to_path_buf)
        .or_else(|| std::env::var_os("LONGHOUSE_HOME").map(PathBuf::from))
        .unwrap_or(
            PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".longhouse"),
        ))
}
fn state_dir(config_dir: Option<&Path>) -> anyhow::Result<PathBuf> {
    Ok(home(config_dir)?.join(STATE_DIR))
}
fn socket_path(session_id: &str) -> anyhow::Result<PathBuf> {
    // macOS's per-user temporary directory and deeply nested HOME paths can
    // exceed sockaddr_un::sun_path. A private short directory keeps the wire
    // contract absolute while preserving same-user-only socket access.
    let dir =
        PathBuf::from("/tmp").join(format!("longhouse-cursor-{}", unsafe { libc::geteuid() }));
    fs::create_dir_all(&dir)?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))?;
    Ok(dir.join(format!("{session_id}.sock")))
}
fn write_json(path: &Path, value: &Value) -> anyhow::Result<()> {
    fs::create_dir_all(path.parent().context("state path has no parent")?)?;
    let tmp = path.with_extension(format!("json.tmp.{}", std::process::id()));
    fs::write(&tmp, serde_json::to_vec_pretty(value)?)?;
    fs::rename(tmp, path)?;
    Ok(())
}
fn process_start_time(pid: libc::pid_t) -> Option<String> {
    std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "lstart="])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}
fn phase(dir: &Path, session_id: &str, conversation: &str, launch_id: &str) -> Option<Value> {
    let value: Value =
        serde_json::from_slice(&fs::read(dir.join(format!("{session_id}.phase.json"))).ok()?)
            .ok()?;
    (value.get("session_id")?.as_str()? == session_id
        && value.get("conversation_id")?.as_str()? == conversation
        && value.get("launch_id")?.as_str()? == launch_id)
        .then_some(value)
}
fn read_claim(dir: &Path, session_id: &str) -> anyhow::Result<String> {
    Uuid::parse_str(session_id).context("--resume-session must be a Longhouse session UUID")?;
    let value: Value = serde_json::from_slice(&fs::read(
        dir.join("binding-probes")
            .join(format!("{session_id}.json")),
    )?)?;
    if value.get("schema_version").and_then(Value::as_i64) != Some(2)
        || value.get("provider").and_then(Value::as_str) != Some("cursor")
        || value.get("session_id").and_then(Value::as_str) != Some(session_id)
        || value.get("status").and_then(Value::as_str) != Some("observed")
    {
        anyhow::bail!("no observed native Cursor identity claim exists for this Longhouse session");
    }
    value
        .get("conversation_uuid")
        .and_then(Value::as_str)
        .filter(|s| Uuid::parse_str(s).is_ok())
        .map(str::to_owned)
        .context("no valid Cursor identity claim exists for this Longhouse session")
}
fn resolve_bin(explicit: Option<String>) -> anyhow::Result<String> {
    let value = explicit
        .or_else(|| std::env::var("LONGHOUSE_CURSOR_BIN").ok())
        .unwrap_or_else(|| "cursor-agent".into());
    if value.contains('/') {
        return Path::new(&value)
            .is_file()
            .then_some(value)
            .context("--cursor-bin is not an executable file");
    }
    for path in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let candidate = path.join(&value);
        if candidate.is_file() {
            return Ok(candidate.display().to_string());
        }
    }
    anyhow::bail!("cursor-agent executable not found. Install Cursor's CLI or set --cursor-bin.")
}
fn cursor_chat(bin: &str, cwd: &Path) -> anyhow::Result<String> {
    let output = std::process::Command::new(bin)
        .arg("create-chat")
        .current_dir(cwd)
        .output()?;
    if !output.status.success() {
        anyhow::bail!(
            "cursor-agent create-chat failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let id = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    Uuid::parse_str(&id).context("cursor-agent create-chat returned invalid id")?;
    Ok(id)
}
fn raw(fd: RawFd) -> anyhow::Result<libc::termios> {
    unsafe {
        let mut saved = std::mem::zeroed();
        if libc::tcgetattr(fd, &mut saved) != 0 {
            anyhow::bail!("read terminal state failed")
        };
        let mut next = saved;
        libc::cfmakeraw(&mut next);
        if libc::tcsetattr(fd, libc::TCSADRAIN, &next) != 0 {
            anyhow::bail!("set terminal raw mode failed")
        };
        Ok(saved)
    }
}
struct Terminal(RawFd, libc::termios);
impl Drop for Terminal {
    fn drop(&mut self) {
        unsafe {
            libc::tcsetattr(self.0, libc::TCSADRAIN, &self.1);
        }
    }
}
fn sync_winsize(master: RawFd) {
    unsafe {
        let mut size: libc::winsize = std::mem::zeroed();
        if libc::ioctl(1, libc::TIOCGWINSZ, &mut size) == 0 {
            let _ = libc::ioctl(master, libc::TIOCSWINSZ, &size);
        }
    }
}
fn write_all(fd: RawFd, mut bytes: &[u8]) -> std::io::Result<()> {
    while !bytes.is_empty() {
        let written = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if written > 0 {
            bytes = &bytes[written as usize..];
        } else if written < 0
            && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
        {
            continue;
        } else {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}
#[derive(serde::Deserialize)]
struct Registration {
    session_id: Option<String>,
    run_id: Option<String>,
    hook_token: Option<String>,
}
fn register(config: &LaunchConfig, cwd: &Path, session_id: &str) -> Option<Registration> {
    let machine = home(config.config_dir.as_deref()).ok()?.join("machine");
    let state: Value = fs::read(machine.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    let url = config.url.clone().or_else(|| {
        state
            .get("runtime_url")
            .and_then(Value::as_str)
            .map(str::to_owned)
    });
    let token = config.token.clone().or_else(|| {
        fs::read_to_string(machine.join("device-token"))
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
    });
    let (Some(url), Some(token)) = (url, token) else {
        return None;
    };
    let machine_name = state
        .get("machine_name")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into()));
    let payload = json!({"cwd":cwd,"provider":"cursor","project":config.project,"display_name":config.name,"loop_mode":config.loop_mode,"permission_mode":config.permission_mode,"session_id":session_id,"machine_name":machine_name});
    tokio::runtime::Runtime::new()
        .ok()?
        .block_on(async {
            reqwest::Client::new()
                .post(format!(
                    "{}/api/sessions/managed-local/this-device",
                    url.trim_end_matches('/')
                ))
                .header("X-Agents-Token", token)
                .json(&payload)
                .timeout(std::time::Duration::from_secs(5))
                .send()
                .await
                .ok()?
                .error_for_status()
                .ok()?
                .json::<Registration>()
                .await
                .ok()
        })
        .filter(|value| value.session_id.as_deref().unwrap_or(session_id) == session_id)
}
fn enqueue_terminal_event(config: &LaunchConfig, session_id: &str, exit_code: i32) {
    let Ok(root) = home(config.config_dir.as_deref()) else {
        return;
    };
    let now = Utc::now().to_rfc3339();
    let event = json!({"runtime_key":format!("cursor:{session_id}"),"session_id":session_id,"provider":"cursor","device_id":std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into()),"source":"cursor_helm","kind":"terminal_signal","phase":"finished","occurred_at":now,"dedupe_key":format!("cursor-helm-terminal:{session_id}:{now}"),"payload":{"terminal_state":"session_ended","terminal_reason":"provider_exit","terminal_source":"cursor_helm","exit_code":exit_code}});
    let _ = write_json(
        &root
            .join("agent/runtime-events-outbox")
            .join(format!("{}.json", Uuid::new_v4())),
        &event,
    );
}
fn response(stream: &mut UnixStream, value: Value) {
    let _ = stream.write_all(format!("{}\n", value).as_bytes());
}
fn serve(
    mut stream: UnixStream,
    master: RawFd,
    child: libc::pid_t,
    stop: &AtomicBool,
    pty_lock: &Mutex<()>,
    dir: &Path,
    session_id: &str,
    conversation: &str,
    launch_id: &str,
) {
    // Accepted sockets inherit nonblocking mode on macOS. Read one bounded
    // newline-framed message with a deadline; partial reads are never commands.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(8)));
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(8)));
    let mut bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                bytes.extend_from_slice(&chunk[..count]);
                if bytes.len() > 65_536 {
                    return response(
                        &mut stream,
                        json!({"ok":false,"error":{"code":"bad_request","message":"request too large"}}),
                    );
                }
                if bytes.contains(&b'\n') {
                    break;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"bad_request","message":"incomplete request"}}),
                )
            }
        }
    }
    let line = bytes.split(|b| *b == b'\n').next().unwrap_or_default();
    let Ok(request) = serde_json::from_slice::<Value>(line) else {
        return response(
            &mut stream,
            json!({"ok":false,"error":{"code":"bad_request","message":"invalid JSON"}}),
        );
    };
    match request.get("kind").and_then(Value::as_str) {
        Some("send")
            if request
                .get("text")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty()) =>
        {
            if phase(dir, session_id, conversation, launch_id)
                .and_then(|value| {
                    value
                        .get("phase")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .as_deref()
                != Some("idle")
            {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"provider_not_idle","message":"Cursor provider is not idle; send was not injected"}}),
                );
            }
            let _hold = pty_lock.lock().unwrap();
            let text = request["text"].as_str().unwrap().as_bytes();
            if let Err(error) = write_all(master, text) {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            thread::sleep(std::time::Duration::from_millis(
                std::env::var("LH_CURSOR_HELM_TEXT_SETTLE_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(300),
            ));
            if let Err(error) = write_all(master, b"\x1b") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            thread::sleep(std::time::Duration::from_millis(
                std::env::var("LH_CURSOR_HELM_ESCAPE_SETTLE_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(100),
            ));
            if let Err(error) = write_all(master, b"\r") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("interrupt") => {
            let expected = request.get("generation_id").and_then(Value::as_str);
            let generation = phase(dir, session_id, conversation, launch_id)
                .filter(|value| value.get("phase").and_then(Value::as_str) == Some("active"))
                .and_then(|value| {
                    value
                        .get("generation_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                });
            if expected.is_none() || generation.as_deref() != expected {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"provider_generation_mismatch","message":"Cursor active generation changed; cancel was not injected"}}),
                );
            }
            let _hold = pty_lock.lock().unwrap();
            if let Err(error) = write_all(master, b"\x03") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("terminate") => {
            unsafe {
                libc::kill(child, libc::SIGTERM);
            }
            stop.store(true, Ordering::Relaxed);
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("ping") => response(
            &mut stream,
            json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
        ),
        _ => response(
            &mut stream,
            json!({"ok":false,"error":{"code":"bad_request","message":"unknown or malformed command"}}),
        ),
    }
}

pub fn launch(config: LaunchConfig) -> anyhow::Result<()> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        anyhow::bail!("longhouse cursor Helm needs an interactive terminal");
    }
    if !matches!(
        config.permission_mode.as_str(),
        "auto_approve" | "provider_local" | "remote_approve" | "remote_human"
    ) {
        anyhow::bail!("invalid --permission-mode");
    }
    let cwd = fs::canonicalize(&config.cwd)
        .with_context(|| format!("resolve {}", config.cwd.display()))?;
    let bin = resolve_bin(config.cursor_bin.clone())?;
    let dir = state_dir(config.config_dir.as_deref())?;
    let session_id = config
        .resume_session
        .clone()
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let conversation = if config.resume_session.is_some() {
        read_claim(&dir, &session_id)?
    } else {
        cursor_chat(&bin, &cwd)?
    };
    fs::create_dir_all(&dir)?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(dir.join(format!("{session_id}.lock")))?;
    if fs2_lock(&lock).is_err() {
        anyhow::bail!("Cursor Helm session {session_id} is already attached");
    }
    let launch_id = Uuid::new_v4().to_string();
    // Runtime/control failure is deliberately soft. Permission authority is not:
    // a remote approval session never starts without a hook token to fail closed.
    let registered = register(&config, &cwd, &session_id);
    let hook_url = config.url.clone().or_else(|| {
        home(config.config_dir.as_deref()).ok().and_then(|root| {
            fs::read(root.join("machine/state.json"))
                .ok()
                .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
                .and_then(|state| {
                    state
                        .get("runtime_url")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
        })
    });
    if config.verbose {
        eprintln!("Longhouse Cursor session: {session_id}");
        if let Some(url) = hook_url.as_deref() {
            eprintln!(
                "Timeline: {}/sessions/{session_id}",
                url.trim_end_matches('/')
            );
        }
    }
    if matches!(
        config.permission_mode.as_str(),
        "remote_approve" | "remote_human"
    ) && registered
        .as_ref()
        .and_then(|value| value.hook_token.as_deref())
        .filter(|value| !value.is_empty())
        .is_none()
    {
        anyhow::bail!("Cursor remote approval could not be enforced; Cursor was not launched");
    }
    write_json(
        &dir.join("binding-probes")
            .join(format!("{session_id}.json")),
        &json!({"schema_version":2,"provider":"cursor","status":"pending","session_id":session_id,"conversation_uuid":conversation,"launch_id":launch_id,"permission_policy":config.permission_mode,"expires_at":(Utc::now()+chrono::Duration::minutes(10)).to_rfc3339()}),
    )?;
    let socket = socket_path(&session_id)?;
    let _ = fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, std::os::unix::fs::PermissionsExt::from_mode(0o600))?;
    // Build all exec data before forkpty. The parent is multi-threaded by this
    // point, so the child may only call async-signal-safe libc functions.
    let mut argv = vec![bin.clone(), "--resume".into(), conversation.clone()];
    if config.permission_mode == "auto_approve" {
        argv.extend(["--force".into(), "--approve-mcps".into()]);
    }
    argv.extend(config.cursor_args.clone());
    let argv: Vec<CString> = argv
        .iter()
        .map(|value| CString::new(value.as_str()))
        .collect::<Result<_, _>>()
        .context("Cursor arguments cannot contain NUL")?;
    let mut env_pairs: Vec<(Vec<u8>, Vec<u8>)> = std::env::vars_os()
        .filter_map(|(key, value)| {
            let key = key.as_os_str().as_bytes().to_vec();
            (!matches!(
                key.as_slice(),
                b"LONGHOUSE_SESSION_ID"
                    | b"LONGHOUSE_CURSOR_LAUNCH_ID"
                    | b"LONGHOUSE_PERMISSION_HOOK_ENABLED"
                    | b"LONGHOUSE_HOOK_URL"
                    | b"LONGHOUSE_HOOK_TOKEN"
            ))
            .then(|| (key, value.as_os_str().as_bytes().to_vec()))
        })
        .collect();
    env_pairs.extend([
        (
            b"LONGHOUSE_SESSION_ID".to_vec(),
            session_id.as_bytes().to_vec(),
        ),
        (
            b"LONGHOUSE_CURSOR_LAUNCH_ID".to_vec(),
            launch_id.as_bytes().to_vec(),
        ),
        (
            b"LONGHOUSE_PERMISSION_HOOK_ENABLED".to_vec(),
            if matches!(
                config.permission_mode.as_str(),
                "remote_approve" | "remote_human"
            ) {
                b"1".to_vec()
            } else {
                b"0".to_vec()
            },
        ),
    ]);
    if let Some(url) = hook_url.as_deref() {
        env_pairs.push((b"LONGHOUSE_HOOK_URL".to_vec(), url.as_bytes().to_vec()));
    }
    if let Some(token) = registered
        .as_ref()
        .and_then(|value| value.hook_token.as_deref())
    {
        env_pairs.push((b"LONGHOUSE_HOOK_TOKEN".to_vec(), token.as_bytes().to_vec()));
    }
    let env: Vec<CString> = env_pairs
        .into_iter()
        .map(|(key, value)| {
            let mut pair = key;
            pair.push(b'=');
            pair.extend(value);
            CString::new(pair)
        })
        .collect::<Result<_, _>>()
        .context("Cursor environment cannot contain NUL")?;
    let mut argv_ptrs: Vec<*const libc::c_char> = argv.iter().map(|value| value.as_ptr()).collect();
    argv_ptrs.push(std::ptr::null());
    let mut env_ptrs: Vec<*const libc::c_char> = env.iter().map(|value| value.as_ptr()).collect();
    env_ptrs.push(std::ptr::null());
    let cwd_c = CString::new(cwd.as_os_str().as_bytes())
        .context("Cursor working directory cannot contain NUL")?;
    let mut master = -1;
    let pid = unsafe {
        libc::forkpty(
            &mut master,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if pid < 0 {
        anyhow::bail!("forkpty failed: {}", std::io::Error::last_os_error());
    }
    if pid == 0 {
        unsafe {
            libc::chdir(cwd_c.as_ptr());
            libc::execve(argv[0].as_ptr(), argv_ptrs.as_ptr(), env_ptrs.as_ptr());
            libc::_exit(127)
        }
    }
    let launcher_pid = std::process::id();
    let launcher_start = process_start_time(launcher_pid as libc::pid_t)
        .context("could not capture Cursor Helm launcher process identity")?;
    let cursor_start =
        process_start_time(pid).context("could not capture cursor-agent process identity")?;
    let now = Utc::now().to_rfc3339();
    write_json(
        &dir.join(format!("{session_id}.json")),
        &json!({"schema_version":1,"session_id":session_id,"run_id":registered.as_ref().and_then(|value| value.run_id.as_deref()),"connection_id":Uuid::new_v4().to_string(),"lease_generation":Uuid::new_v4().to_string(),"provider":"cursor","control_plane":"cursor_helm","socket_path":socket,"launcher_pid":launcher_pid,"launcher_process_start_time":launcher_start,"cursor_pid":pid,"cursor_process_start_time":cursor_start,"cwd":cwd,"ready":true,"registration":if registered.is_some() {"registered"} else {"degraded"},"started_at":now,"updated_at":now}),
    )?;
    let terminal = Terminal(0, raw(0)?);
    let stop = Arc::new(AtomicBool::new(false));
    let resized = Arc::new(AtomicBool::new(true));
    signal_hook::flag::register(libc::SIGWINCH, resized.clone())?;
    signal_hook::flag::register(libc::SIGTERM, stop.clone())?;
    signal_hook::flag::register(libc::SIGHUP, stop.clone())?;
    sync_winsize(master);
    let socket_stop = stop.clone();
    let guard = Arc::new(Mutex::new(()));
    let socket_guard = guard.clone();
    let server_dir = dir.clone();
    let server_session = session_id.clone();
    let server_conversation = conversation.clone();
    let server_launch = launch_id.clone();
    listener.set_nonblocking(true)?;
    let server = thread::spawn(move || {
        while !socket_stop.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((stream, _)) => serve(
                    stream,
                    master,
                    pid,
                    &socket_stop,
                    &socket_guard,
                    &server_dir,
                    &server_session,
                    &server_conversation,
                    &server_launch,
                ),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(std::time::Duration::from_millis(25))
                }
                Err(_) => break,
            }
        }
    });
    let mut input = [0u8; 8192];
    let mut output = [0u8; 65536];
    loop {
        if stop.load(Ordering::Relaxed) {
            break;
        }
        if resized.swap(false, Ordering::Relaxed) {
            sync_winsize(master);
        }
        let mut fds = [
            libc::pollfd {
                fd: 0,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: master,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        if unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, 250) } < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            break;
        }
        if fds[0].revents & libc::POLLIN != 0 {
            let count = unsafe { libc::read(0, input.as_mut_ptr().cast(), input.len()) };
            if count > 0 {
                let _hold = guard.lock().unwrap();
                if write_all(master, &input[..count as usize]).is_err() {
                    break;
                }
            }
        }
        if fds[1].revents & libc::POLLIN != 0 {
            let count = unsafe { libc::read(master, output.as_mut_ptr().cast(), output.len()) };
            if count > 0 {
                if write_all(1, &output[..count as usize]).is_err() {
                    break;
                }
            } else {
                break;
            }
        }
    }
    drop(terminal);
    stop.store(true, Ordering::Relaxed);
    let _ = server.join();
    let exit_code = unsafe {
        let mut status = 0;
        libc::waitpid(pid, &mut status, 0);
        if libc::WIFEXITED(status) {
            libc::WEXITSTATUS(status)
        } else {
            128 + libc::WTERMSIG(status)
        }
    };
    enqueue_terminal_event(&config, &session_id, exit_code);
    let _ = fs::remove_file(&socket);
    let _ = fs::remove_file(dir.join(format!("{session_id}.json")));
    let _ = fs::remove_file(dir.join(format!("{session_id}.phase.json")));
    if config.open {
        if let Some(url) = hook_url.as_deref() {
            println!(
                "Timeline: {}/sessions/{session_id}",
                url.trim_end_matches('/')
            );
        }
    }
    Ok(())
}

fn fs2_lock(file: &fs::File) -> std::io::Result<()> {
    unsafe {
        if libc::flock(
            std::os::fd::AsRawFd::as_raw_fd(file),
            libc::LOCK_EX | libc::LOCK_NB,
        ) == 0
        {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    }
}
