/**
 * Starting a session: the one launch implementation. The new-session pane
 * (`NewSessionComposer`) renders it; the Timeline, the rail and the Machines
 * page all open that pane rather than keeping their own launcher.
 *
 * A Console session is created empty, so a prompt typed here is sent as the
 * session's first input right after the create returns.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
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
}

export function readLastLaunch(): LastLaunch | null {
  try {
    const raw = window.localStorage.getItem(LAST_LAUNCH_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<LastLaunch>;
    if (typeof value.deviceId !== "string" || !value.deviceId) return null;
    return {
      deviceId: value.deviceId,
      provider: typeof value.provider === "string" ? value.provider : "",
      model: typeof value.model === "string" ? value.model : "",
      cwd: typeof value.cwd === "string" ? value.cwd : "",
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
  const [model, setModel] = useState<string>("");
  const [cwd, setCwd] = useState<string>("");
  const [prompt, setPrompt] = useState<string>("");
  const [workspaceSearch, setWorkspaceSearch] = useState<string>("");
  const [displayName, setDisplayName] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [firstInputFailure, setFirstInputFailure] = useState<FirstInputFailure | null>(null);

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
  // first launchable machine. On the last-used machine, its last provider,
  // model and folder come back too.
  useEffect(() => {
    if (!launchable.length || deviceId) return;
    const find = (id: string | undefined) => (id ? launchable.find((m) => m.device_id === id) : undefined);
    const machine = find(initialDeviceId) ?? find(last?.deviceId) ?? launchable[0];
    setDeviceId(machine.device_id);
    if (last && last.deviceId === machine.device_id) {
      if (launchProvidersForMachine(machine).includes(last.provider)) {
        setProvider(last.provider);
        setModel(last.model);
      }
      if (last.cwd) setCwd(last.cwd);
    }
  }, [launchable, deviceId, initialDeviceId, last]);

  // Keep the provider valid for the selected machine.
  useEffect(() => {
    if (!selectedMachine) return;
    const providers = launchProvidersForMachine(selectedMachine);
    if (!provider || !providers.includes(provider)) {
      setProvider(defaultProvider(selectedMachine));
      setModel("");
    }
  }, [selectedMachine, provider]);

  // Start with the top-ranked workspace the user has actually used on this machine.
  useEffect(() => {
    if (cwd.trim() || workspaces.length === 0) return;
    setCwd(workspaces[0].path);
  }, [cwd, workspaces]);

  // After a first message failed, changing anything the launch sends means a
  // new launch: the started session stays reachable from the error's link.
  useEffect(() => {
    setFirstInputFailure(null);
  }, [prompt, deviceId, provider, model, cwd, displayName]);

  const chooseMachine = useCallback((machine: MachineDirectoryEntry) => {
    setDeviceId(machine.device_id);
    setProvider(defaultProvider(machine));
    setModel("");
    setCwd("");
    setWorkspaceSearch("");
    setError(null);
  }, []);

  const chooseProvider = useCallback((next: string) => {
    setProvider(next);
    setModel("");
    setError(null);
  }, []);

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
    writeLastLaunch({ deviceId, provider, model: model.trim(), cwd: cwd.trim() });
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
  }, [canSubmit, deviceId, provider, model, cwd, displayName, prompt, onLaunched]);

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
  return path.replace(/^\/Users\/[^/]+/, "~");
}

export function workspaceTitle(cwd: string, workspaces: Array<{ path: string; label: string }>): string {
  if (!cwd) return "Choose a workspace";
  return workspaces.find((workspace) => workspace.path === cwd)?.label ?? cwd.split("/").filter(Boolean).at(-1) ?? cwd;
}
