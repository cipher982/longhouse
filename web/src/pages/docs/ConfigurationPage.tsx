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
        {`LONGHOUSE_PASSWORD=your-password longhouse serve`}
      </CodeBlock>
      <p>
        For production, you can also use a pre-hashed password to avoid passing
        the raw value in the environment:
      </p>
      <CodeBlock title="terminal">
        {`LONGHOUSE_PASSWORD_HASH="$2b$12$..." longhouse serve`}
      </CodeBlock>
      <div className="docs-callout">
        <p>
          <strong>Before binding beyond localhost</strong>, set{" "}
          <code>LONGHOUSE_PASSWORD</code> or{" "}
          <code>LONGHOUSE_PASSWORD_HASH</code>. Longhouse will warn you if
          you bind to 0.0.0.0 without auth.
        </p>
      </div>

      <h2>Port</h2>
      <p>Default port is 8080. Override with:</p>
      <CodeBlock title="terminal">
        {`longhouse serve --port 9090`}
      </CodeBlock>

      <h2>Data location</h2>
      <p>
        The SQLite database is stored at{" "}
        <code>~/.longhouse/longhouse.db</code> by default. Override with the{" "}
        <code>DATABASE_URL</code> environment variable:
      </p>
      <CodeBlock title="terminal">
        {`DATABASE_URL=sqlite:///path/to/your.db longhouse serve`}
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
            <td>Password for browser auth</td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_PASSWORD_HASH</code></td>
            <td>(none)</td>
            <td>Pre-hashed password (bcrypt)</td>
          </tr>
          <tr>
            <td><code>DATABASE_URL</code></td>
            <td><code>~/.longhouse/longhouse.db</code></td>
            <td>SQLite database path</td>
          </tr>
          <tr>
            <td><code>PORT</code></td>
            <td>8080</td>
            <td>Server port (also <code>--port</code>)</td>
          </tr>
          <tr>
            <td><code>AUTH_DISABLED</code></td>
            <td>1 (dev)</td>
            <td>Disable auth entirely</td>
          </tr>
          <tr>
            <td><code>LONGHOUSE_HOST</code></td>
            <td>127.0.0.1</td>
            <td>Bind address</td>
          </tr>
        </tbody>
      </table>

      <h2>Machine name</h2>
      <p>
        Longhouse identifies your machine by the hostname. To override this
        (useful when multiple machines report to the same instance):
      </p>
      <CodeBlock title="terminal">
        {`longhouse machine configure --machine-name my-vps`}
      </CodeBlock>
      <p>
        This is read at engine startup, not live. Re-run the install/repair
        flow after changing it.
      </p>

      <h2>Running on a server</h2>
      <p>
        For an always-on machine (VPS, Mac mini, homelab), the typical setup:
      </p>
      <CodeBlock title="terminal">
        {`# Set auth
export LONGHOUSE_PASSWORD="your-password"

# Bind to all interfaces
export LONGHOUSE_HOST="0.0.0.0"

# Start
longhouse serve`}
      </CodeBlock>
      <p>
        Put a reverse proxy (nginx, Caddy) in front for TLS. The hosted plan
        handles all of this for you if you prefer not to run infrastructure.
      </p>
    </>
  );
}
