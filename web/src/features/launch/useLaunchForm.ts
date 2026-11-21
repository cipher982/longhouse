/**
 * Starting a session: the one launch implementation. The new-session pane
 * (`NewSessionComposer`) renders it; the Timeline, the rail and the Machines
 * page all open that pane rather than keeping their own launcher.
 *
 * A Console session is created empty, so a prompt typed here is sent as the
 * session's first input right after the create returns.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  createConsoleSession,
  fetchWorkspaceSuggestions,
  listMachines,
  postSessionInput,
  type MachineDirectoryEntry,
} from "@/shared/api/index";
import { lastSeenLabel, unreadyProviderLabel } from "@/features/machines/machineStatus";

const WORKSPACE_LIMIT = 12;
const LAST_LAUNCH_KEY = "longhouse:launch:last";

/** What the last successful launch chose, so the next one starts there. */
export interface LastLaunch {
  deviceId: string;
  provider: string;
  model: string;
  cwd: string;
  modelByProvider?: Record<string, string>;
}

function normalizeGitRepo(gitRepo: string | null | undefined): string | null {
  let raw = (gitRepo ?? "").trim().toLowerCase();
  if (!raw) return null;
  for (const prefix of ["https://", "http://", "ssh://"]) {
    if (raw.startsWith(prefix)) {
      raw = raw.slice(prefix.length);
      break;
    }
  }
  if (raw.startsWith("git@")) {
    raw = raw.slice(4);
    const colon = raw.indexOf(":");
    if (colon !== -1) raw = `${raw.slice(0, colon)}/${raw.slice(colon + 1)}`;
  }
  raw = raw.replace(/\/+$/, "");
  if (raw.endsWith(".git")) raw = raw.slice(0, -4);
  return raw || null;
}

export function readLastLaunch(): LastLaunch | null {
  try {
    const raw = window.localStorage.getItem(LAST_LAUNCH_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<LastLaunch>;
    if (typeof value.deviceId !== "string" || !value.deviceId) return null;
    const provider = typeof value.provider === "string" ? value.provider : "";
    const model = typeof value.model === "string" ? value.model : "";
    const modelByProvider: Record<string, string> = {};
    if (value.modelByProvider && typeof value.modelByProvider === "object") {
      for (const [k, v] of Object.entries(value.modelByProvider)) {
        if (typeof v === "string") modelByProvider[k] = v;
      }
    }
    if (provider && !(provider in modelByProvider)) {
      modelByProvider[provider] = model;
    }
    return {
      deviceId: value.deviceId,
      provider,
      model,
      cwd: typeof value.cwd === "string" ? value.cwd : "",
      modelByProvider,
    };
  } catch {
    return null;
  }
}

export function writeLastLaunch(value: LastLaunch): void {
  try {
    window.localStorage.setItem(LAST_LAUNCH_KEY, JSON.stringify(value));
  } catch {
    // Storage is a convenience: a launch never fails over it.
  }
}

export function machineCanLaunch(m: MachineDirectoryEntry): boolean {
  return m.launch.providers.length > 0;
}

export function launchProvidersForMachine(m: MachineDirectoryEntry): string[] {
  return m.launch.providers.map((option) => option.provider);
}

function defaultProvider(m: MachineDirectoryEntry | undefined): string {
  if (!m) return "";
  return m.launch.default_provider ?? "";
}

function pickProvider(m: MachineDirectoryEntry, ...preferred: Array<string | undefined>): string {
  const available = launchProvidersForMachine(m);
  for (const candidate of preferred) {
    if (candidate && available.includes(candidate)) return candidate;
  }
  return defaultProvider(m);
}

function newClientRequestId(): string {
  const randomUUID = globalThis.crypto?.randomUUID?.bind(globalThis.crypto);
  if (randomUUID) return `web-${randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** The session started but its first message did not go: say so, keep the text. */
export interface FirstInputFailure {
  sessionId: string;
  error: string;
}

export function useLaunchForm({
  initialDeviceId,
  onLaunched,
}: {
  /** Preselect this machine when it can launch (the Machines page's "New session"). */
  initialDeviceId?: string;
  onLaunched: (sessionId: string) => void;
}) {
  const machinesQuery = useQuery({
    queryKey: ["launch-machines"],
    queryFn: listMachines,
    refetchOnMount: "always",
    refetchInterval: 5000,
  });
  const [last] = useState(readLastLaunch);
  const [deviceId, setDeviceId] = useState<string>("");
  const [provider, setProvider] = useState<string>("");
  const [model, setModelState] = useState<string>("");
  const [modelByProvider, setModelByProvider] = useState<Record<string, string>>(
    () => last?.modelByProvider ?? {},
  );
  const [cwd, setCwd] = useState<string>("");
  const [prompt, setPrompt] = useState<string>("");
  const [workspaceSearch, setWorkspaceSearch] = useState<string>("");
  const [displayName, setDisplayName] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [firstInputFailure, setFirstInputFailure] = useState<FirstInputFailure | null>(null);
  const autoSelectedDeviceRef = useRef<string | null>(null);
  const preferredRepoRef = useRef<string | null>(null);

  const setModel = useCallback(
    (next: string) => {
      setModelState(next);
      if (provider) {
        setModelByProvider((prev) => ({ ...prev, [provider]: next }));
      }
    },
    [provider],
  );

  const machines = useMemo(() => machinesQuery.data?.machines ?? [], [machinesQuery.data]);
  const launchable = useMemo(() => machines.filter(machineCanLaunch), [machines]);
  const unavailable = useMemo(() => machines.filter((machine) => !machineCanLaunch(machine)), [machines]);

  const selectedMachine = launchable.find((m) => m.device_id === deviceId);
  const canSubmit =
    !submitting &&
    !firstInputFailure &&
    !!deviceId &&
    !!provider &&
    !!cwd.trim() &&
    !!selectedMachine?.launch.providers.some((option) => option.provider === provider);

  const workspacesQuery = useQuery({
    queryKey: ["launch-workspaces", deviceId],
    queryFn: () => fetchWorkspaceSuggestions(deviceId, { limit: WORKSPACE_LIMIT }),
    enabled: !!deviceId,
    refetchOnMount: "always",
    staleTime: 15_000,
  });
  const workspaces = useMemo(() => workspacesQuery.data?.workspaces ?? [], [workspacesQuery.data]);
  const filteredWorkspaces = useMemo(() => {
    const q = workspaceSearch.trim().toLowerCase();
    if (!q) return workspaces;
    return workspaces.filter((w) => w.path.toLowerCase().includes(q) || w.label.toLowerCase().includes(q));
  }, [workspaces, workspaceSearch]);

  // Start on the machine the caller named, else the last one used, else the
  // first launchable machine. Keep the last-used agent and its remembered model
  // on whichever machine starts; restore the folder only on the same machine.
  useEffect(() => {
    if (!launchable.length || deviceId) return;
    const find = (id: string | undefined) => (id ? launchable.find((m) => m.device_id === id) : undefined);
    const machine = find(initialDeviceId) ?? find(last?.deviceId) ?? launchable[0];
    setDeviceId(machine.device_id);
    const chosenProvider = pickProvider(machine, last?.provider);
    setProvider(chosenProvider);
    setModelState(last?.modelByProvider?.[chosenProvider] ?? "");
    if (last && last.deviceId === machine.device_id && last.cwd) {
      setCwd(last.cwd);
      autoSelectedDeviceRef.current = machine.device_id;
    }
  }, [launchable, deviceId, initialDeviceId, last]);

  // Fill only an empty agent; never overwrite a chosen agent on a background
  // machine-list poll (canSubmit disables Start and ProviderSignInList explains why).
  useEffect(() => {
    if (!selectedMachine || provider) return;
    const nextProvider = defaultProvider(selectedMachine);
    if (nextProvider) {
      setProvider(nextProvider);
      setModelState(modelByProvider[nextProvider] ?? "");
    }
  }, [selectedMachine, provider, modelByProvider]);

  // Auto-select a workspace once per machine when suggestions arrive (preferring
  // the same git_repo across a machine switch). Never fight the user clearing "Other path".
  useEffect(() => {
    if (!deviceId || workspaces.length === 0) return;
    if (autoSelectedDeviceRef.current === deviceId) return;
    autoSelectedDeviceRef.current = deviceId;
    if (cwd.trim()) return;
    const targetRepo = preferredRepoRef.current;
    const matched = targetRepo
      ? workspaces.find((w) => normalizeGitRepo(w.git_repo) === targetRepo)
      : undefined;
    setCwd((matched ?? workspaces[0]).path);
  }, [deviceId, cwd, workspaces]);

  // After a first message failed, changing anything the launch sends means a
  // new launch: the started session stays reachable from the error's link.
  useEffect(() => {
    setFirstInputFailure(null);
  }, [prompt, deviceId, provider, model, cwd, displayName]);

  const chooseMachine = useCallback(
    (machine: MachineDirectoryEntry) => {
      const currentRepo = normalizeGitRepo(workspaces.find((w) => w.path === cwd)?.git_repo);
      preferredRepoRef.current = currentRepo;
      autoSelectedDeviceRef.current = null;
      const nextProvider = pickProvider(machine, provider);
      setDeviceId(machine.device_id);
      setProvider(nextProvider);
      if (nextProvider !== provider) {
        setModelState(modelByProvider[nextProvider] ?? "");
      }
      setCwd("");
      setWorkspaceSearch("");
      setError(null);
    },
    [cwd, modelByProvider, provider, workspaces],
  );

  const chooseProvider = useCallback(
    (next: string) => {
      if (provider) {
        setModelByProvider((prev) => ({ ...prev, [provider]: model }));
      }
      setProvider(next);
      setModelState(modelByProvider[next] ?? "");
      setError(null);
    },
    [model, modelByProvider, provider],
  );

  const chooseWorkspace = useCallback((path: string) => {
    setCwd(path);
    setError(null);
  }, []);

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    let sessionId: string;
    try {
      const result = await createConsoleSession({
        device_id: deviceId,
        provider,
        cwd: cwd.trim(),
        display_name: displayName.trim() || null,
        ...(model.trim() ? { model: model.trim() } : {}),
        launch_surface: "web",
      });
      sessionId = result.session_id;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Launch failed");
      setSubmitting(false);
      return;
    }
    const nextModelByProvider = { ...modelByProvider, [provider]: model.trim() };
    setModelByProvider(nextModelByProvider);
    writeLastLaunch({
      deviceId,
      provider,
      model: model.trim(),
      cwd: cwd.trim(),
      modelByProvider: nextModelByProvider,
    });
    const text = prompt.trim();
    if (text) {
      try {
        await postSessionInput(sessionId, {
          text,
          intent: "auto",
          client_request_id: newClientRequestId(),
          model: model.trim() || null,
        });
      } catch (err) {
        setFirstInputFailure({
          sessionId,
          error: err instanceof ApiError ? err.message : "The message did not send",
        });
        setSubmitting(false);
        return;
      }
    }
    setSubmitting(false);
    onLaunched(sessionId);
  }, [canSubmit, deviceId, provider, model, modelByProvider, cwd, displayName, prompt, onLaunched]);

  return {
    machinesQuery,
    machines,
    launchable,
    unavailable,
    selectedMachine,
    deviceId,
    provider,
    model,
    setModel,
    cwd,
    setCwd,
    prompt,
    setPrompt,
    workspaces,
    filteredWorkspaces,
    workspaceSearch,
    setWorkspaceSearch,
    displayName,
    setDisplayName,
    submitting,
    error,
    firstInputFailure,
    canSubmit,
    chooseMachine,
    chooseProvider,
    chooseWorkspace,
    submit,
  };
}

export function launchBlockedLabel(machine: MachineDirectoryEntry): string {
  switch (machine.launch.blocked_by) {
    case "control_down":
      return lastSeenLabel(machine);
    case "engine_too_old":
      return "Update required";
    case "auth_failed":
      return "Needs repair";
    case "runtime_unreachable":
      return "Needs repair";
    case "no_launch_support":
      return unreadyProviderLabel(machine) ?? "Console launch unavailable";
    default:
      if (!machine.online) return lastSeenLabel(machine);
      return unreadyProviderLabel(machine) ?? "Console launch unavailable";
  }
}

export function machineStatusClass(machine: MachineDirectoryEntry): string {
  if (machine.launch.blocked_by === "control_down") return "is-offline";
  if (machine.launch.blocked_by === "auth_failed" || machine.launch.blocked_by === "runtime_unreachable") return "is-repair";
  return "is-warning";
}

export function compactPath(path: string): string {
  return path.replace(/^\/(?:Users|home)\/[^/]+/, "~");
}

export function workspaceTitle(cwd: string, workspaces: Array<{ path: string; label: string }>): string {
  if (!cwd) return "Choose a workspace";
  return workspaces.find((workspace) => workspace.path === cwd)?.label ?? cwd.split("/").filter(Boolean).at(-1) ?? cwd;
}
