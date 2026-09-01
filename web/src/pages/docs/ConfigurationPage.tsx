import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function ConfigurationPage() {
  usePageMeta({
    title: "Configuration - Longhouse Docs",
    description: "Auth, ports, data location, and environment variables.",
  });

  return (
    <>
      <h1>Configuration</h1>
      <p className="docs-subtitle">
        Longhouse works with zero configuration for local use. These options
        matter when you bind beyond localhost or run on a shared machine.
      </p>

      <h2>Authentication</h2>
      <p>
        Auth is disabled by default for local-only quickstarts. To add password
        protection:
      </p>
      <CodeBlock title="terminal">
        {`LONGHOUSE_PASSWORD=your-password longhouse-server serve`}
      </CodeBlock>
      <p>
        For production, use a pre-hashed password so the raw value never sits in
        the environment. <code>hash-password</code> prompts for the password on
        stderr and prints only the hash, so it is safe to capture:
      </p>
      <CodeBlock title="terminal">
        {`export LONGHOUSE_PASSWORD_HASH="$(longhouse-server hash-password)"
longhouse-server serve --host 0.0.0.0 --domain longhouse.example.com`}
      </CodeBlock>
      <p>
        What it prints is{" "}
        <code>pbkdf2_sha256$&lt;iterations&gt;$&lt;salt&gt;$&lt;derived&gt;</code>. An
        argon2 or bcrypt hash you produced elsewhere also verifies, provided
        that library is installed in the server environment.
      </p>
      <div className="docs-callout">
        <p>
          <strong>A public bind without auth is refused, not warned about.</strong>{" "}
          If you pass <code>--host 0.0.0.0</code>, <code>--host ::</code>, or{" "}
          <code>--domain</code> with no password configured,{" "}
          <code>longhouse-server serve</code> prints what to set and exits
          non-zero. Pass <code>--allow-public-no-auth</code> only when something
          in front of it — a reverse proxy that authenticates — already does the
          job.
        </p>
      </div>

      <h2>Host and port</h2>
      <p>
        The Runtime Host binds to <code>127.0.0.1:8080</code> unless you say
        otherwise, and the flags are the only thing that changes the bind:
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server serve --port 9090
longhouse-server serve --host 0.0.0.0 --port 80`}
      </CodeBlock>
      <div className="docs-callout">
        <p>
          <strong>The environment does not move the bind.</strong>{" "}
          <code>LONGHOUSE_HOST</code> and <code>LONGHOUSE_PORT</code> feed the
          resolved configuration that <code>longhouse-server config show</code>{" "}
          and local-health report — they do not change where{" "}
          <code>serve</code> listens. Exporting{" "}
          <code>LONGHOUSE_HOST=0.0.0.0</code> and starting the server leaves it
          on loopback. Use <code>--host</code>.
        </p>
      </div>

      <h2>Data location</h2>
      <p>
        The SQLite database is stored at{" "}
        <code>~/.longhouse/longhouse.db</code> by default. Override it with{" "}
        <code>DATABASE_URL</code> or with <code>--db</code>:
      </p>
      <CodeBlock title="terminal">
        {`DATABASE_URL=sqlite:///path/to/your.db longhouse-server serve
longhouse-server serve --db sqlite:///path/to/your.db`}
      </CodeBlock>

      <h2>Environment variables</h2>
      <table>
        <thead>
          <tr>
            <th>Variable</th>
            <th>Default</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>LONGHOUSE_PASSWORD</code></td>
            <td>(none)</td>
            <td>Plaintext password for browser auth</td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_PASSWORD_HASH</code></td>
            <td>(none)</td>
            <td>
              Pre-hashed password. <code>hash-password</code> emits
              pbkdf2_sha256; argon2 and bcrypt hashes also verify
            </td>
          </tr>
          <tr>
            <td><code>DATABASE_URL</code></td>
            <td><code>sqlite:///~/.longhouse/longhouse.db</code></td>
            <td>SQLite database URL</td>
          </tr>
          <tr>
            <td><code>AUTH_DISABLED</code></td>
            <td>(unset)</td>
            <td>
              Turns auth off. <code>serve</code> sets it to <code>1</code> for
              you on a loopback bind, and never on a public one.
            </td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_HOST</code> / <code>LONGHOUSE_PORT</code></td>
            <td><code>127.0.0.1</code> / <code>8080</code></td>
            <td>
              Resolved config reported by diagnostics. Does not change the{" "}
              <code>serve</code> bind — use <code>--host</code> / <code>--port</code>.
            </td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_PUBLIC_URL</code></td>
            <td>(none)</td>
            <td>Public URL shown to clients when no <code>--domain</code> is stored</td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_DEVICE_TOKEN</code></td>
            <td>(none)</td>
            <td>Device token read by <code>longhouse auth</code></td>
          </tr>
        </tbody>
      </table>

      <h2>Machine name</h2>
      <p>
        Longhouse identifies a machine by its hostname. To override that —
        useful when several machines report to one Runtime Host — name it when
        you pair the device:
      </p>
      <CodeBlock title="terminal">
        {`LONGHOUSE_DEVICE_TOKEN="..." longhouse auth --url https://your-runtime.example --device my-vps`}
      </CodeBlock>
      <p>
        That writes the name into the device state file. Restart the Machine
        Agent with <code>longhouse machine repair</code> so it picks the new
        name up.
      </p>

      <h2>Running on a server</h2>
      <p>
        For an always-on machine (VPS, Mac mini, homelab), the typical setup:
      </p>
      <CodeBlock title="terminal">
        {`export LONGHOUSE_PASSWORD_HASH="$(longhouse-server hash-password)"
export JWT_SECRET=$(openssl rand -hex 32)
export INTERNAL_API_SECRET=$(openssl rand -hex 32)

longhouse-server serve --host 0.0.0.0 --domain longhouse.example.com`}
      </CodeBlock>
      <p>
        Put a reverse proxy (nginx, Caddy) in front for TLS —{" "}
        <code>reverse_proxy 127.0.0.1:8080</code> is the whole Caddy config. The
        hosted plan handles all of this if you prefer not to run
        infrastructure.
      </p>
    </>
  );
}
