import { useState } from "react";
import {
  cancelProviderSignIn,
  startProviderSignIn,
  submitProviderSignInCode,
  type MachineDirectoryEntry,
  type ProviderSignInStartResponse,
} from "../services/api";
import { getProviderLabel } from "../lib/providers";

// Providers a machine can drive but cannot run yet (signed out, CLI missing).
// When the engine can relay the provider's own login, "Sign in" runs it on the
// machine and shows its URL/code here; the credential stays on that machine.
// The launch modal refetches machines every few seconds, so a completed
// sign-in moves the provider out of this list on its own.
export default function ProviderSignInList({ machine }: { machine: MachineDirectoryEntry }) {
  const items = machine.launch.unavailable_providers ?? [];
  if (items.length === 0) return null;
  return (
    <ul className="launch-unavailable-providers" data-testid="launch-unavailable-providers">
      {items.map((item) => (
        <ProviderSignInRow
          key={item.provider}
          machine={machine}
          provider={item.provider}
          remediation={
            item.remediation ?? (item.reason === "cli_missing" ? "Not installed on this machine" : "Sign in required on this machine")
          }
          canRelay={item.reason === "not_authenticated" && (machine.supports ?? []).includes(`${item.provider}.sign_in`)}
        />
      ))}
    </ul>
  );
}

function ProviderSignInRow({
  machine,
  provider,
  remediation,
  canRelay,
}: {
  machine: MachineDirectoryEntry;
  provider: string;
  remediation: string;
  canRelay: boolean;
}) {
  const [attempt, setAttempt] = useState<ProviderSignInStartResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setAttempt(await startProviderSignIn(machine.device_id, provider));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start sign-in");
    } finally {
      setBusy(false);
    }
  };

  const sendCode = async () => {
    if (!attempt || !code.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await submitProviderSignInCode(machine.device_id, attempt.attempt_id, code.trim());
      setCodeSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send the code");
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (attempt) await cancelProviderSignIn(machine.device_id, attempt.attempt_id).catch(() => undefined);
    setAttempt(null);
    setCode("");
    setCodeSent(false);
  };

  return (
    <li data-testid={`launch-unavailable-provider-${provider}`}>
      <div className="launch-signin-head">
        <span className="launch-signin-copy">
          <strong>{getProviderLabel(provider)}</strong>
          <small>{remediation}</small>
        </span>
        {canRelay && !attempt && (
          <button type="button" className="launch-signin-button" onClick={start} disabled={busy} data-testid={`launch-signin-${provider}`}>
            {busy ? "Starting…" : "Sign in"}
          </button>
        )}
      </div>
      {attempt && (
        <div className="launch-signin-panel" data-testid={`launch-signin-panel-${provider}`}>
          {attempt.prerequisite && <p className="launch-signin-note">{attempt.prerequisite}</p>}
          <a href={attempt.verification_url} target="_blank" rel="noopener noreferrer">
            Open {getProviderLabel(provider)} sign-in ↗
          </a>
          {attempt.flow === "device_code" && attempt.user_code && (
            <p className="launch-signin-code">
              Enter code <code>{attempt.user_code}</code>
            </p>
          )}
          {attempt.flow === "paste_code" && !codeSent && (
            <form
              className="launch-signin-form"
              onSubmit={(event) => {
                event.preventDefault();
                void sendCode();
              }}
            >
              <input
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder="Paste the code shown after signing in"
                aria-label={`${getProviderLabel(provider)} sign-in code`}
              />
              <button type="submit" disabled={busy || !code.trim()}>
                Submit
              </button>
            </form>
          )}
          <p className="launch-signin-note">
            {codeSent || attempt.flow === "device_code"
              ? `Waiting for ${machine.machine_name} to confirm the sign-in…`
              : "The code appears on the page after you approve."}
          </p>
          <button type="button" className="launch-signin-cancel" onClick={() => void cancel()}>
            Cancel
          </button>
        </div>
      )}
      {error && <p className="launch-signin-error">{error}</p>}
    </li>
  );
}
