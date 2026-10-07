/**
 * The new-session pane: what to work on first, then where. It is the main
 * pane of the app shell (`/timeline/new`), beside the session rail, the way a
 * new chat is in a chat app. Machine, workspace, agent and model are chips
 * under the prompt that start on the last launch's choices.
 */
import { useCallback, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";
import { Link } from "react-router";

import type { MachineDirectoryEntry } from "@/shared/api/index";
import { Spinner } from "@/shared/ui";
import { useReadinessFlag } from "@/shared/lib/readiness-contract";
import { getProviderLabel } from "@/shared/lib/providers";
import ConnectMachine from "@/features/machines/ConnectMachine";
import { onlineForLabel } from "@/features/machines/machineStatus";
import ProviderSignInList from "./ProviderSignInList";
import ModelPicker from "./ModelPicker";
import {
  compactPath,
  launchBlockedLabel,
  launchProvidersForMachine,
  machineStatusClass,
  useLaunchForm,
  workspaceTitle,
} from "./useLaunchForm";
import "./new-session.css";

/** An open chip closes on Escape (focus back on it) or a press outside it. */
function useDismissChips(refs: readonly RefObject<HTMLDetailsElement | null>[]) {
  useEffect(() => {
    const close = (event: Event) => {
      for (const ref of refs) {
        const details = ref.current;
        if (!details?.open) continue;
        if (event instanceof KeyboardEvent) {
          if (event.key !== "Escape") continue;
          details.open = false;
          details.querySelector<HTMLElement>("summary")?.focus();
          continue;
        }
        if (event.target instanceof Node && !details.contains(event.target)) details.open = false;
      }
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, [refs]);
}

function Caret() {
  return (
    <svg className="launch-chip__caret" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <path d="M2 3.5 5 6.5l3-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function NewSessionComposer({
  initialDeviceId,
  onLaunched,
}: {
  initialDeviceId?: string;
  onLaunched: (sessionId: string) => void;
}) {
  const form = useLaunchForm({ initialDeviceId, onLaunched });
  const machinePickerRef = useRef<HTMLDetailsElement | null>(null);
  const providerPickerRef = useRef<HTMLDetailsElement | null>(null);
  const modelPickerRef = useRef<HTMLDetailsElement | null>(null);
  const workspacePickerRef = useRef<HTMLDetailsElement | null>(null);
  const advancedPickerRef = useRef<HTMLDetailsElement | null>(null);
  const chipRefs = useRef([machinePickerRef, providerPickerRef, workspacePickerRef, advancedPickerRef]).current;
  useDismissChips(chipRefs);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);

  const ready = form.launchable.length > 0;
  useReadinessFlag({ ready: !form.machinesQuery.isPending });
  useEffect(() => {
    if (ready) promptRef.current?.focus();
  }, [ready]);

  const handleMachineListKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    const options = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("[role='option']"));
    if (!options.length) return;
    const currentIndex = options.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = currentIndex < 0 ? 0 : Math.min(currentIndex + 1, options.length - 1);
    if (event.key === "ArrowUp") nextIndex = currentIndex < 0 ? options.length - 1 : Math.max(currentIndex - 1, 0);
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = options.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    options[nextIndex]?.focus();
  }, []);

  const close = (ref: RefObject<HTMLDetailsElement | null>) => {
    if (ref.current) ref.current.open = false;
  };

  const { selectedMachine } = form;
  const providers = selectedMachine ? launchProvidersForMachine(selectedMachine) : [];

  return (
    <div className="new-session" data-testid="new-session-pane">
      <div className="new-session__inner">
        <h1 className="new-session__title">What should an agent work on?</h1>
        {form.machinesQuery.isPending ? (
          <div className="new-session__loading">
            <Spinner size="md" />
            <p>Loading machines…</p>
          </div>
        ) : form.machinesQuery.isError ? (
          <p className="text-danger">Failed to load machines.</p>
        ) : !ready ? (
          <LaunchEmptyState machines={form.machines} />
        ) : (
          <form
            className="new-session__box"
            onSubmit={(event) => {
              event.preventDefault();
              void form.submit();
            }}
          >
            <textarea
              ref={promptRef}
              className="new-session__prompt"
              value={form.prompt}
              onChange={(event) => form.setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void form.submit();
                }
              }}
              rows={3}
              placeholder="Describe the task. Enter starts it; Shift+Enter adds a line. Leave it empty to start an idle session."
              aria-label="First message"
              data-testid="launch-prompt"
            />
            <div className="new-session__chips">
              <details ref={machinePickerRef} className="launch-chip" data-testid="launch-machine-select">
                <summary aria-haspopup="listbox" aria-label={`Machine: ${selectedMachine?.machine_name ?? "choose"}`}>
                  <span className="launch-machine-status is-ready" aria-hidden="true" />
                  <span className="launch-chip__key">on</span>
                  <span className="launch-chip__value">{selectedMachine?.machine_name ?? "Choose a machine"}</span>
                  <Caret />
                </summary>
                <div className="launch-chip__panel" data-popover-panel>
                  <span id="launch-available-machines" className="launch-machine-group-label">Available</span>
                  <div className="launch-machine-options" role="listbox" aria-labelledby="launch-available-machines" onKeyDown={handleMachineListKeyDown}>
                    {form.launchable.map((machine) => (
                      <button
                        key={machine.device_id}
                        type="button"
                        role="option"
                        aria-selected={machine.device_id === form.deviceId}
                        className={`launch-machine-row${machine.device_id === form.deviceId ? " is-selected" : ""}`}
                        onClick={() => {
                          form.chooseMachine(machine);
                          close(machinePickerRef);
                        }}
                      >
                        <span className="launch-machine-status is-ready" aria-hidden="true" />
                        <span className="launch-machine-copy"><strong>{machine.machine_name}</strong><small>{onlineForLabel(machine)}</small></span>
                        <span>{machine.device_id === form.deviceId ? "✓" : ""}</span>
                      </button>
                    ))}
                  </div>
                  {form.unavailable.length > 0 && (
                    <>
                      <span id="launch-unavailable-machines" className="launch-machine-group-label">Unavailable</span>
                      <div className="launch-machine-options" role="group" aria-labelledby="launch-unavailable-machines">
                        {form.unavailable.map((machine) => (
                          <UnavailableMachineRow key={machine.device_id} machine={machine} />
                        ))}
                      </div>
                    </>
                  )}
                </div>
              </details>

              <details ref={workspacePickerRef} className="launch-chip">
                <summary aria-label={`Workspace: ${form.cwd || "choose"}`} title={form.cwd || undefined}>
                  <span className="launch-chip__key">in</span>
                  <span className="launch-chip__value">{workspaceTitle(form.cwd, form.workspaces)}</span>
                  <Caret />
                </summary>
                <div className="launch-chip__panel launch-workspace-panel" data-popover-panel>
                  {form.workspaces.length > 0 && (
                    <input
                      type="search"
                      value={form.workspaceSearch}
                      onChange={(event) => form.setWorkspaceSearch(event.target.value)}
                      placeholder="Filter workspaces…"
                      data-testid="launch-workspace-search"
                    />
                  )}
                  {form.filteredWorkspaces.map((w) => (
                    <button
                      key={w.path}
                      type="button"
                      className="launch-option-row launch-workspace-row"
                      onClick={() => {
                        form.chooseWorkspace(w.path);
                        close(workspacePickerRef);
                      }}
                    >
                      <span><strong>{w.label}</strong><small>{compactPath(w.path)}</small></span><span>{w.path === form.cwd ? "✓" : ""}</span>
                    </button>
                  ))}
                  <label className="launch-manual-path">
                    <span>Other path</span>
                    <input
                      type="text"
                      value={form.cwd}
                      onChange={(event) => form.setCwd(event.target.value)}
                      placeholder="/Users/example/git/zerg/longhouse"
                      autoComplete="off"
                      spellCheck={false}
                      data-testid="launch-cwd-input"
                    />
                  </label>
                </div>
              </details>

              {providers.length > 1 ? (
                <details ref={providerPickerRef} className="launch-chip" data-testid="launch-provider-select">
                  <summary aria-label={`Agent: ${getProviderLabel(form.provider)}`}>
                    <span className="launch-chip__value">{getProviderLabel(form.provider)}</span>
                    <Caret />
                  </summary>
                  <div className="launch-chip__panel" data-popover-panel>
                    {providers.map((p) => (
                      <button
                        key={p}
                        type="button"
                        className="launch-option-row"
                        onClick={() => {
                          form.chooseProvider(p);
                          close(providerPickerRef);
                        }}
                      >
                        <span>{getProviderLabel(p)}</span><span>{p === form.provider ? "✓" : ""}</span>
                      </button>
                    ))}
                  </div>
                </details>
              ) : (
                <span className="launch-chip launch-chip--static">
                  <span className="launch-chip__value">{getProviderLabel(form.provider)}</span>
                </span>
              )}

              <ModelPicker
                deviceId={form.deviceId || null}
                provider={form.provider}
                value={form.model}
                onChange={form.setModel}
                pickerRef={modelPickerRef}
                testId="launch-model-select"
                compact
              />

              <details ref={advancedPickerRef} className="launch-chip launch-chip--quiet" data-testid="launch-advanced-runtime">
                <summary aria-label="Session name">
                  <span className="launch-chip__value">{form.displayName.trim() || "Name"}</span>
                  <Caret />
                </summary>
                <div className="launch-chip__panel" data-popover-panel>
                  <label className="launch-manual-path">
                    <span>Session name (optional)</span>
                    <input
                      type="text"
                      value={form.displayName}
                      onChange={(event) => form.setDisplayName(event.target.value)}
                      placeholder="e.g. zerg — refactor launch"
                      data-testid="launch-display-name"
                    />
                  </label>
                </div>
              </details>

              <button
                type="submit"
                className="new-session__start"
                disabled={!form.canSubmit}
                data-testid="launch-submit"
              >
                {form.submitting ? "Starting…" : "Start"}
              </button>
            </div>
          </form>
        )}

        {ready && selectedMachine ? <ProviderSignInList machine={selectedMachine} /> : null}

        {form.error && (
          <p className="text-danger new-session__error" data-testid="launch-error">
            {form.error}
          </p>
        )}
        {form.firstInputFailure && (
          <p className="text-danger new-session__error" data-testid="launch-first-input-error">
            The session started, but your message did not send: {form.firstInputFailure.error}.{" "}
            <Link to={`/timeline/${form.firstInputFailure.sessionId}`}>Open the session</Link> and send it from there;
            the text is still above.
          </p>
        )}
      </div>
    </div>
  );
}

function UnavailableMachineRow({ machine }: { machine: MachineDirectoryEntry }) {
  return (
    <div
      className="launch-machine-row is-unavailable"
      aria-label={`${machine.machine_name}, ${launchBlockedLabel(machine)}, Not available`}
    >
      <span className={`launch-machine-status ${machineStatusClass(machine)}`} aria-hidden="true" />
      <span className="launch-machine-copy"><strong>{machine.machine_name}</strong><small>{launchBlockedLabel(machine)}</small></span>
      <span />
      {machine.launch.blocked_by === "providers_not_ready" && <ProviderSignInList machine={machine} />}
    </div>
  );
}

function LaunchEmptyState({ machines }: { machines: MachineDirectoryEntry[] }) {
  if (machines.length === 0) {
    return (
      <div className="new-session__empty" data-testid="launch-no-machines">
        <p>No enrolled machines yet.</p>
        <ConnectMachine />
      </div>
    );
  }
  return (
    <div className="new-session__empty" data-testid="launch-no-launchable">
      <p><strong>No machines ready to launch</strong></p>
      <p>Your machines remain listed and will become available when their Console connection returns.</p>
      <div className="launch-choice-panel is-static">
        <span className="launch-machine-group-label">Unavailable</span>
        {machines.map((machine) => (
          <UnavailableMachineRow key={machine.device_id} machine={machine} />
        ))}
      </div>
    </div>
  );
}
