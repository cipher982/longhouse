import type { MachineDirectoryEntry } from "@/shared/api/index";
import { connectionLabel, unreadyProviderLabel } from "./machineStatus";
import "./MachineAgents.css";

/**
 * The machines whose Machine Agent has connected to this Runtime Host. This is
 * what a newcomer means by "my machine"; the Runner list below it on the
 * Machines page is a separate, optional executor.
 */
export default function MachineAgents({ machines }: { machines: MachineDirectoryEntry[] }) {
  return (
    <ul className="machine-agents" data-testid="machine-agents">
      {machines.map((machine) => {
        // A signed-out or missing provider CLI is the one thing on this card the
        // person can act on, and only when it leaves nothing to start sessions
        // with; `unknown` readiness is never shown as a problem.
        const hint = machine.online && machine.launch.providers.length === 0 ? unreadyProviderLabel(machine) : null;
        return (
          <li
            key={machine.device_id}
            className="machine-agent"
            data-testid={`machine-agent-${machine.device_id}`}
            data-online={machine.online ? "true" : "false"}
          >
            <span className="machine-agent-dot" aria-hidden="true" />
            <div className="machine-agent-main">
              <strong className="machine-agent-name">{machine.machine_name}</strong>
              <span className="machine-agent-status">{connectionLabel(machine)}</span>
              {hint && <span className="machine-agent-hint">{hint} to start sessions from Longhouse.</span>}
            </div>
            {machine.engine_build && (
              <span className="machine-agent-build" title="Machine Agent build">
                Agent {machine.engine_build}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
