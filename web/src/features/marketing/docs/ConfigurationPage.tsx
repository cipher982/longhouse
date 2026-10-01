import { Link } from "react-router";
import { usePageMeta } from "@/shared/hooks/usePageMeta";
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
            <td><code>LONGHOUSE_URL</code></td>
            <td>(none)</td>
            <td>Runtime Host address the installer stores for <code>longhouse auth</code></td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_DEVICE_TOKEN</code></td>
            <td>(none)</td>
            <td>Device token read by <code>longhouse auth</code></td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_IMPORT_SCOPE</code></td>
            <td>(asks)</td>
            <td>
              Read by the installer: <code>now</code>, <code>all</code>, or a
              date such as <code>2026-09-01</code> answers what history to
              import without a prompt
            </td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_ALLOW_INSECURE_HTTP</code></td>
            <td>(unset)</td>
            <td>
              Set to <code>1</code> to let native clients use plain http to a
              LAN address. Tailscale and loopback never need it
            </td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_COOKIE_SECURE</code></td>
            <td>(unset)</td>
            <td>
              Set to <code>1</code> to force <code>Secure</code> auth cookies
              when a TLS proxy the server does not trust reports every request
              as https
            </td>
          </tr>
          <tr>
            <td><code>FORWARDED_ALLOW_IPS</code></td>
            <td>loopback</td>
            <td>
              Proxy addresses trusted to report https through{" "}
              <code>X-Forwarded-Proto</code> (uvicorn setting)
            </td>
          </tr>
        </tbody>
      </table>

      <h2>Machine name</h2>
      <p>
        A machine is named by its device token. A token created on the Devices
        page carries the name you gave it, and <code>longhouse auth</code>{" "}
        stores that name; browser approval names the token after this
        machine&apos;s hostname, or after <code>--device</code> when you pass it:
      </p>
      <CodeBlock title="terminal">{`longhouse auth --url https://you.longhouse.ai --device my-vps`}</CodeBlock>
      <p>
        With an existing token, <code>--device</code> must match the
        token&apos;s own name; <code>longhouse auth</code> refuses a different
        one rather than store a pair the Runtime Host would reject. To rename a
        machine, connect it with a token of the new name, then restart the
        Machine Agent with <code>longhouse machine repair --repair-service</code>.
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
      <p>
        Without TLS, native clients accept plain <code>http://</code> to
        loopback and Tailscale addresses, and to a LAN address only after an
        opt-in (see <Link to="/docs/quickstart">Quick Start</Link>). Browser
        login also works over plain http: auth cookies are marked{" "}
        <code>Secure</code> only for requests that arrive over https. A TLS
        proxy on another machine or container must be listed in{" "}
        <code>FORWARDED_ALLOW_IPS</code> for its requests to count as https.
      </p>
    </>
  );
}
