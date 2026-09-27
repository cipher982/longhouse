import { useState } from "react";
import { Link } from "react-router";
import { copyToClipboard } from "../lib/clipboard";
import { connectMachineCommand } from "../lib/connectCommands";
import { Button } from "./ui";
import "./ConnectMachine.css";

/**
 * The one way to connect a machine's Machine Agent to this Runtime Host: a
 * command carrying this host's address that installs Longhouse and opens this
 * site to approve the machine. A machine with no browser takes the token line
 * from Settings → Devices instead.
 */
export default function ConnectMachine() {
  const command = connectMachineCommand();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!(await copyToClipboard(command))) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="connect-machine">
      <p className="connect-machine-label">
        Run this in a terminal on the machine where you use Claude Code, Codex, or Antigravity:
      </p>
      <div className="connect-machine-command">
        <code data-testid="connect-machine-command">{command}</code>
        <Button variant="primary" size="sm" onClick={handleCopy} data-testid="connect-machine-copy">
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <p className="connect-machine-hint">
        It installs Longhouse and opens this site so you can approve the machine. Your past sessions appear here
        a few minutes later.
      </p>
      <p className="connect-machine-hint">
        No browser on that machine? <Link to="/settings/devices">Create a server command</Link> in Settings →
        Devices.
      </p>
    </div>
  );
}
