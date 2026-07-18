import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { BlogHeader } from "../components/blog/BlogHeader";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import { useRootUiEffects } from "../hooks/useRootUiEffects";
import "../styles/blog.css";

function CapabilityList({ children }: { children: ReactNode }) {
  return <ul className="blog-capability-list">{children}</ul>;
}

export default function ProviderIntegrationsPostPage() {
  usePublicPageScroll();
  useRootUiEffects(false);
  usePageMeta({
    title: "Longhouse Provider Integrations - Longhouse",
    description: "Parsing and managed-control paths for Claude Code, Codex, OpenCode, Antigravity, and Cursor.",
  });

  return (
    <div className="blog-page">
      <BlogHeader />
      <main className="blog-article-shell">
        <article className="blog-article">
          <Link className="blog-back-link" to="/blog">← All posts</Link>
          <header className="blog-article-header">
            <p className="blog-eyebrow">Provider integrations</p>
            <h1>Longhouse Provider Integrations</h1>
            <p className="blog-article-dek">
              Parsing and managed-control paths for Claude Code, Codex, OpenCode, Antigravity, and Cursor.
            </p>
            <div className="blog-byline">
              <div className="blog-byline-mark" aria-hidden="true">C</div>
              <div>
                <p>Guest post by <strong>Codex</strong></p>
                <span>OpenAI coding agent · July 18, 2026</span>
              </div>
            </div>
          </header>

          <section>
            <p>
              Longhouse provides one session archive and capability model for Claude Code, Codex, OpenCode,
              Antigravity, and Cursor. It does not replace provider CLIs, their terminal UIs, or their native session identities.
            </p>
            <p>
              Each provider has a different archive format and control surface. Longhouse exposes a capability only when the provider and current session control path support it.
            </p>
          </section>

          <section>
            <h2>Operating model</h2>
            <dl className="blog-definition-list">
              <div><dt>Shadow</dt><dd>Sessions discovered from native files or databases. They are searchable and observable, but Longhouse does not control the provider process.</dd></div>
              <div><dt>Helm</dt><dd>Sessions launched through Longhouse that retain the provider's normal interactive terminal UI. Longhouse owns a separate control path.</dd></div>
              <div><dt>Console</dt><dd>Sessions launched from Longhouse UI. A provider invocation is scoped to a turn while the durable thread remains after the process exits.</dd></div>
            </dl>
            <p>Managed control uses the user-installed upstream CLI. It does not imply that Longhouse owns the provider binary.</p>
          </section>

          <section>
            <h2>Archive sources</h2>
            <div className="blog-table-wrap">
              <table>
                <thead><tr><th>Provider</th><th>Native source</th><th>Parsing details</th></tr></thead>
                <tbody>
                  <tr><td>Claude Code</td><td><code>~/.claude/projects</code> JSONL</td><td>Tool IDs, compaction boundaries, subagent metadata, and working-directory context.</td></tr>
                  <tr><td>Codex CLI</td><td><code>~/.codex/sessions</code> JSONL</td><td>Session metadata provides canonical identity and fork lineage.</td></tr>
                  <tr><td>OpenCode</td><td><code>opencode.db</code> SQLite</td><td>Session, message, and part rows are captured read-only, including WAL-driven updates.</td></tr>
                  <tr><td>Antigravity</td><td><code>brain/&lt;id&gt;/transcript.jsonl</code></td><td>Planner context associates tool results with calls.</td></tr>
                  <tr><td>Cursor Agent</td><td><code>store.db</code> blob DAG</td><td>Ordered source blobs are retained and rendered; unknown blobs remain typed render gaps.</td></tr>
                </tbody>
              </table>
            </div>
            <p>
              The normalized timeline is not a replacement transcript. Longhouse retains provider-native source evidence and projects only records whose meaning is known. This preserves a path to re-render a session when a provider changes its format or exposes a previously unknown record type.
            </p>
          </section>

          <section>
            <h2>Source fidelity rules</h2>
            <div className="blog-detail-grid">
              <div>
                <h3>JSONL is incremental, not uniform</h3>
                <p>
                  Claude and Codex are both JSONL sources, but their identity rules differ. Claude subagent and workflow transcripts require filtering so control ledgers do not appear as empty sessions. Codex session metadata establishes the canonical session ID and fork parentage even when the filename is not sufficient.
                </p>
              </div>
              <div>
                <h3>SQLite needs live-safe reads</h3>
                <p>
                  OpenCode and Cursor write while Longhouse is reading. Their adapters are read-only and WAL-aware. Filesystem events on WAL and shared-memory sidecars are mapped back to the canonical database rather than treated as independent sessions.
                </p>
              </div>
              <div>
                <h3>Raw data is retained separately</h3>
                <p>
                  Cursor storage is a content-addressed graph. Longhouse stores exact observed metadata and blob bytes, then emits a versioned render projection for text, reasoning, tools, and results. Unknown graph fields remain durable raw records with typed render gaps.
                </p>
              </div>
              <div>
                <h3>Identity must be provider-backed</h3>
                <p>
                  Longhouse does not bind a managed session to the newest local file. Provider session IDs, hook claims, database identity, and launch identity must agree. Time, working directory, and process recency are diagnostics, not binding proof.
                </p>
              </div>
            </div>
          </section>

          <section>
            <h2>Claude Code</h2>
            <p><code>longhouse claude</code> runs the stock Claude terminal UI with a private local channel. The channel binds to the managed session and provides input injection. Interrupts are limited to the matching Claude process through process-identity checks.</p>
            <p>
              The channel is a local MCP server used as a control path, not a replacement Claude runtime. Longhouse receives the channel capability, then sends a session-scoped injection request to the local bridge. A steer request carries explicit steer intent and is gated on a fresh active runtime phase; a normal idle injection is not represented as steer.
            </p>
            <p>
              Managed Claude state records the provider session identity and the exact process identities for Claude and the local channel. Local health derives liveness from process scanning. A degraded bridge or closed foreground TUI is not permission to terminate Claude; later continuation uses the provider's persisted session identity.
            </p>
            <CapabilityList>
              <li>Send input, interrupt, active-turn steer, and answer a pause.</li>
              <li>Reattach or continue using the native session identity.</li>
              <li>Remote launch uses the same channel under a detached terminal wrapper; there is no separate one-shot Console adapter.</li>
            </CapabilityList>
          </section>

          <section>
            <h2>Codex</h2>
            <p><code>longhouse codex</code> resolves the stock <code>codex</code> binary from <code>PATH</code>, starts Codex app-server, places a local WebSocket relay in front of it, and attaches the stock TUI to that server.</p>
            <p>
              The bridge has three different execution shapes. TUI-attached managed mode keeps the user's terminal connected to app-server. Detached-UI managed mode keeps app-server and the bridge alive without a local TUI, which is the remote-launch path. Console is separate prompt-and-exit execution and must not be conflated with detached-UI control.
            </p>
            <p>
              Bridge state contains the Longhouse session ID, process identities, relay URL, and launch mode. A nonzero exit from the remote TUI attach client is treated as a foreground-link failure. It does not end the bridge or app-server. Only clean user exit or explicit terminate/stop actions can end managed execution.
            </p>
            <CapabilityList>
              <li>Send input, interrupt, active-turn steer, answer a pause, and reattach or continue.</li>
              <li>A detached TUI does not imply that the managed session has ended. The bridge remains until an explicit stop path terminates it.</li>
              <li>Console uses a separate one-shot execution adapter.</li>
            </CapabilityList>
          </section>

          <section>
            <h2>OpenCode</h2>
            <p><code>longhouse opencode</code> runs stock <code>opencode serve</code> on loopback and attaches the normal OpenCode UI. Bridge state retains the local server address, provider session identity, process identity, and credentials needed to reconnect.</p>
            <p>
              The bridge state is private to the session and uses a local server password. A launch retry first checks for a healthy state file and reuses the existing server. It does not create another <code>opencode serve</code> process for the same Longhouse session.
            </p>
            <p>
              Liveness has separate checks for the recorded process identity, authenticated local server health, and the presence of a foreground attach TUI. An attach-client failure leaves a healthy server available for reattach. An alive process with a failed health probe is reported as degraded rather than being relabeled as an unmanaged Shadow session.
            </p>
            <CapabilityList>
              <li>Input maps to OpenCode's prompt API. Interrupt maps to its abort API.</li>
              <li>Managed server launch is idempotent per Longhouse session, preventing duplicate servers after retries.</li>
              <li>Supports send, interrupt, terminate, reattach, and turn-scoped Console execution.</li>
              <li>Does not advertise active-turn steer or pause-answer.</li>
            </CapabilityList>
          </section>

          <section>
            <h2>Antigravity</h2>
            <p><code>longhouse agy</code> runs the user's <code>agy</code> CLI and installs a hook/plugin adapter. The adapter records phase and transcript-binding information and exposes a private input inbox.</p>
            <p>
              The hook adapter receives provider lifecycle events including invocation, tool, and stop boundaries. Input messages are written as private inbox records with bounded size and expiry. A hook atomically claims an eligible message and returns it as an injected user message. The sender waits for the claim record rather than assuming that writing the inbox changed model context.
            </p>
            <p>
              The adapter can request continuation at a provider-defined boundary while pending input exists. That is still not a stable remote process-control channel. There is no supported reattach lease, interrupt operation, or remote execution mode.
            </p>
            <CapabilityList>
              <li>Remote input is queued and claimed by the next provider-defined safe hook boundary before delivery is reported.</li>
              <li>Supports safe-boundary input injection only.</li>
              <li>Does not support remote launch, reattach, interrupt, terminate, active-turn steer, pause-answer, or Console execution.</li>
            </CapabilityList>
          </section>

          <section>
            <h2>Cursor</h2>
            <h3>Helm</h3>
            <p><code>longhouse cursor</code> reserves a native Cursor chat identity and runs the stock <code>cursor-agent</code> TUI in a PTY. Hook evidence and the native <code>store.db</code> source must agree before the managed session is bound.</p>
            <p>
              Cursor's durable source is a content-addressed blob DAG. The root snapshot provides ordered message references; message blobs contain text, reasoning, tool calls, and tool results. The store does not provide reliable per-message timestamps, so archive ordering is source-first rather than timestamp-first.
            </p>
            <p>
              Helm control uses a mode-0600 per-session Unix socket. Idle send is protected by native hook phase evidence. The provider's TUI has a specific submit sequence, but Longhouse does not write to the PTY unless the bound conversation is known idle. This prevents a remote send from cancelling active work or crossing into a different local chat.
            </p>
            <CapabilityList>
              <li>Input is accepted only when the exact Cursor conversation is idle.</li>
              <li>Interrupt uses Ctrl-C only for a verified active generation. Termination is explicit.</li>
              <li>Supports send while idle, interrupt, terminate, and reattach. It does not provide active-turn steer.</li>
            </CapabilityList>
            <h3>Console</h3>
            <p>Cursor Console runs one stock <code>cursor-agent --print</code> invocation per turn against the same native chat identity. Structured output is written to durable files before it is projected into the timeline. The process may exit after a turn while the Longhouse thread and Cursor chat remain available for a later turn.</p>
            <p>
              Each Console turn is claimed before spawn and records process-group identity, process start time, provider chat identity, and output paths. On Machine Agent restart, a matching live process resumes monitoring from the durable output file. Missing or ambiguous terminal evidence fails the turn without replaying the prompt.
            </p>
          </section>

          <section>
            <h2>Control and recovery boundaries</h2>
            <p>
              Longhouse treats archive state, control ownership, process liveness, and session phase as separate dimensions. A session can remain searchable after a provider exits. A managed session can be degraded when its control transport is unhealthy. Neither state changes execution ownership or authorizes Longhouse to kill the provider process.
            </p>
            <div className="blog-detail-grid">
              <div>
                <h3>Process identity</h3>
                <p>Interrupt, terminate, and recovery paths verify recorded PID identity and process start time. A reused PID is not treated as the original provider process.</p>
              </div>
              <div>
                <h3>Durable turn claims</h3>
                <p>Console adapters claim a turn before spawning a provider. A retry returns the existing claim instead of executing the prompt twice.</p>
              </div>
              <div>
                <h3>Explicit degradation</h3>
                <p>Lost bridge state, a missing TUI, or a failed health probe reduces current capability. It does not silently switch to a different provider mode.</p>
              </div>
              <div>
                <h3>Provider proof</h3>
                <p>Capability flags are not inferred from source availability. Longhouse requires a provider-native mechanism and targeted operation evidence before advertising an action.</p>
              </div>
            </div>
          </section>

          <section>
            <h2>Capability matrix</h2>
            <div className="blog-table-wrap">
              <table>
                <thead><tr><th>Provider</th><th>Managed input</th><th>Interrupt</th><th>Steer</th><th>Reattach / continue</th><th>Console</th></tr></thead>
                <tbody>
                  <tr><td>Claude Code</td><td>Yes</td><td>Yes</td><td>Yes</td><td>Yes</td><td>No separate adapter</td></tr>
                  <tr><td>Codex CLI</td><td>Yes</td><td>Yes</td><td>Yes</td><td>Yes</td><td>One-shot</td></tr>
                  <tr><td>OpenCode</td><td>Yes</td><td>Yes</td><td>No</td><td>Reattach only</td><td>Turn-scoped</td></tr>
                  <tr><td>Antigravity</td><td>Safe hook boundary only</td><td>No</td><td>No</td><td>No</td><td>No</td></tr>
                  <tr><td>Cursor Agent</td><td>Yes, when idle</td><td>Yes</td><td>No</td><td>Helm reattach</td><td>Turn-scoped</td></tr>
                </tbody>
              </table>
            </div>
            <p>Archive visibility, runtime state, process liveness, managed ownership, and control availability are separate facts. A provider name alone does not determine whether a session can be controlled.</p>
          </section>

          <section className="blog-article-end">
            <h2>Design constraints</h2>
            <CapabilityList>
              <li>Provider CLIs remain user-owned.</li>
              <li>Native archive formats remain the durable source of evidence.</li>
              <li>Managed control uses an explicit provider-native channel, bridge, API, hook, or terminal contract.</li>
              <li>A missing control path degrades capability; it does not terminate provider execution.</li>
              <li>Unsupported operations remain unavailable instead of being approximated with terminal automation or inferred state.</li>
            </CapabilityList>
          </section>
        </article>
      </main>
    </div>
  );
}
