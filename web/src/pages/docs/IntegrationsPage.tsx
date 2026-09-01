import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { getLaunchProviderSupportList } from "../../lib/providers";
import { CodeBlock } from "./CodeBlock";

const PROVIDERS = getLaunchProviderSupportList();

/**
 * Where the Machine Agent looks for each provider's own history, from
 * engine/src/discovery.rs. Capability claims are NOT here — those come from the
 * contract via getLaunchProviderSupportList().
 */
const IMPORT_SOURCES: Record<string, { binary: string; paths: string[] }> = {
  claude: { binary: "claude", paths: ["~/.claude/projects/"] },
  codex: { binary: "codex", paths: ["~/.codex/sessions/"] },
  cursor: {
    binary: "cursor-agent",
    paths: ["~/.cursor/chats/", "~/.cursor/projects/", "$XDG_CONFIG_HOME/cursor/chats/"],
  },
  opencode: { binary: "opencode", paths: ["~/.local/share/opencode/"] },
  pi: { binary: "pi", paths: [] },
  antigravity: {
    binary: "agy",
    paths: ["~/.gemini/antigravity-cli/brain/", "~/.gemini/antigravity/brain/", "~/.gemini/tmp/"],
  },
};

function yesNo(value: boolean) {
  return value ? "Yes" : "No";
}

export default function IntegrationsPage() {
  usePageMeta({
    title: "Integrations - Longhouse Docs",
    description:
      "The six CLI agents Longhouse launches and controls: Claude Code, Codex CLI, Cursor Agent, OpenCode, Pi Agent, and Antigravity CLI.",
  });

  return (
    <>
      <h1>Integrations</h1>
      <p className="docs-subtitle">
        Longhouse reads the session files your CLI tools already produce. No
        plugins, no provider-side configuration. Bare CLI runs import as
        unmanaged history; launching through Longhouse creates managed sessions
        and keeps the control path explicit. Import exists so Longhouse is
        useful immediately, but starting through Longhouse is the path we want
        users on.
      </p>

      <h2>Six providers ship today</h2>
      <p>
        All six launch through the native <code>longhouse</code> CLI and land in
        the same timeline. What they can do after launch differs, and the
        difference is not cosmetic — it is what each provider&apos;s own CLI
        exposes. The table is generated from{" "}
        <code>schemas/managed_providers.yml</code>, the same contract the
        runtime reads, so it cannot drift from what the product will let you do.
      </p>
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Launch</th>
            <th>Send</th>
            <th>Interrupt</th>
            <th>Mid-turn steer</th>
            <th>Resume</th>
          </tr>
        </thead>
        <tbody>
          {PROVIDERS.map((provider) => (
            <tr key={provider.id}>
              <td>{provider.marketingName}</td>
              <td>
                {provider.nativeLaunchCommand ? (
                  <code>{provider.nativeLaunchCommand}</code>
                ) : (
                  "—"
                )}
              </td>
              <td>{yesNo(provider.launchAndSend)}</td>
              <td>{yesNo(provider.interrupt)}</td>
              <td>{yesNo(provider.steerMidTurn)}</td>
              <td>{yesNo(provider.resume)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        Search, session detail, and the raw event stream work the same for every
        row. Those are properties of the archive, not of the control path, so
        they do not vary by provider.
      </p>

      <h2>What gets imported, and from where</h2>
      <p>
        The Machine Agent watches each provider&apos;s own history directory and
        imports what it finds there, whether or not Longhouse launched it.
      </p>
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Bare CLI</th>
            <th>Watched history</th>
          </tr>
        </thead>
        <tbody>
          {PROVIDERS.map((provider) => {
            const source = IMPORT_SOURCES[provider.id];
            return (
              <tr key={provider.id}>
                <td>{provider.marketingName}</td>
                <td><code>{source.binary}</code></td>
                <td>
                  {source.paths.length === 0 ? (
                    "Managed launches only — no stock history directory"
                  ) : (
                    source.paths.map((path, index) => (
                      <span key={path}>
                        {index > 0 && ", "}
                        <code>{path}</code>
                      </span>
                    ))
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p>
        Sessions are deduplicated by provider session ID, so re-importing is
        safe and idempotent. Anything imported this way is a Shadow session:
        searchable and observable, with no control path, because Longhouse never
        owned one.
      </p>

      <h2>Notes per provider</h2>
      <p>
        <strong>Claude Code and Codex CLI</strong> are the deepest paths. Both
        support mid-turn steer, so you can redirect a turn that is already
        running rather than waiting for it to finish.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude
longhouse codex`}
      </CodeBlock>
      <p>
        <strong>OpenCode</strong> takes send, interrupt, terminate, and
        pause-answer through its permission reply endpoint. Mid-turn steer is
        not advertised because OpenCode exposes no stable mid-turn injection
        semantic. Pass <code>--model</code> when a session must stay on a
        specific model; Longhouse carries that choice through the initial launch
        and a later cold reattach.
      </p>
      <CodeBlock title="terminal">
        {`longhouse opencode
longhouse opencode --model <provider/model>`}
      </CodeBlock>
      <p>
        <strong>Cursor Agent</strong> runs as a managed Helm session whose
        native runtime owns the PTY, control, permission, and transcript
        lifecycle. Send, interrupt, terminate, and reattach work; mid-turn steer
        and pause-answer do not.
      </p>
      <p>
        <strong>Pi Agent</strong> is a one-shot console adapter. Send, interrupt,
        and terminate work within a turn; there is no reattach and no resume,
        and nothing persists between turns.
      </p>
      <p>
        <strong>Antigravity CLI</strong> is the narrowest of the six. It launches
        under Longhouse&apos;s hook-inbox control path and accepts send;
        interrupt, terminate, and reattach are not supported. It refuses to
        start if its Longhouse hook is not installed rather than opening an
        unmanaged session wearing a managed session id, and send stays gated per
        session on observed hook readiness.
      </p>

      <h2>Import an existing machine</h2>
      <p>
        The native Machine Agent service is installed with{" "}
        <code>longhouse machine repair --repair-service</code> after{" "}
        <code>longhouse auth</code>. To backfill Claude Code history that
        predates it, the Runtime Host can run a one-shot import:
      </p>
      <CodeBlock title="terminal">{`longhouse-server ship`}</CodeBlock>

      <h2>MCP Server</h2>
      <p>
        Longhouse includes a built-in MCP server that exposes session search,
        recall, and coordination to any MCP-compatible host:
      </p>
      <CodeBlock title="terminal">{`longhouse-server mcp-server`}</CodeBlock>
      <p>
        This is the same <Link to="/docs/api">Machine API</Link> surface
        exposed over the MCP protocol. Add it to your Claude Code or Codex
        MCP configuration to give your agent access to session history.
      </p>
    </>
  );
}
