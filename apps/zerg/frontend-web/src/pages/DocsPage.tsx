import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ZapIcon, PuzzleIcon, SettingsIcon, MessageCircleIcon } from "../components/icons";
import "../styles/info-pages.css";

export default function DocsPage() {
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Documentation - Swarmlet";
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription) {
      metaDescription.setAttribute('content', 'Learn how to use Swarmlet. Quick start guides, visual workflow builder tutorials, and integration setup instructions.');
    }
  }, []);

  return (
    <div className="info-page">
      <header className="info-page-header">
        <div className="info-page-header-inner">
          <Link to="/" className="info-page-back">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
            Back to Home
          </Link>
          <Link to="/" className="info-page-brand">
            <SwarmLogo size={28} />
            <span className="info-page-brand-name">Swarmlet</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Documentation</h1>
        <p className="info-page-subtitle">
          Get started with Swarmlet.
        </p>

        <nav className="docs-nav">
          <a href="#quickstart" className="docs-nav-card">
            <ZapIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Quick Start</h3>
            <p>Get up and running</p>
          </a>
          <a href="#canvas" className="docs-nav-card">
            <PuzzleIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Visual Builder</h3>
            <p>Learn the workflow canvas</p>
          </a>
          <a href="#integrations" className="docs-nav-card">
            <SettingsIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Integrations</h3>
            <p>Connect your tools</p>
          </a>
        </nav>

        <section id="quickstart" className="docs-section">
          <h2>Quick Start</h2>

          <h3>1. Sign In</h3>
          <p>
            Click "Start Free" on the homepage and sign in with your Google account.
          </p>

          <h3>2. Explore the Dashboard</h3>
          <p>
            Once signed in, you'll land on the Dashboard where you can see and manage your agents.
          </p>

          <h3>3. Create an Agent</h3>
          <p>
            From the Dashboard, click "New Agent" and configure what you want it to do.
          </p>

          <h3>4. Build a Workflow</h3>
          <p>
            Open the Canvas to create visual workflows by connecting nodes.
          </p>
        </section>

        <section id="canvas" className="docs-section">
          <h2>Visual Builder</h2>
          <p>
            The Canvas lets you design AI workflows visually with a node-based interface.
          </p>

          <h3>Node Types</h3>
          <ul>
            <li><strong>Triggers</strong> - Start your workflow</li>
            <li><strong>Actions</strong> - Perform tasks</li>
            <li><strong>Conditions</strong> - Branch based on data</li>
            <li><strong>AI Nodes</strong> - Use LLMs for decisions</li>
          </ul>

          <h3>Connecting Nodes</h3>
          <p>
            Drag from a node's output to another node's input to create connections.
          </p>
        </section>

        <section id="integrations" className="docs-section">
          <h2>Integrations</h2>
          <p>
            Connect Swarmlet to your existing tools. Go to Settings &gt; Integrations to set up connections.
          </p>

          <h3>Available Now</h3>
          <ul>
            <li><strong>Notifications</strong> - Slack, Discord, Email, SMS</li>
            <li><strong>Project Tools</strong> - GitHub, Jira, Linear, Notion</li>
            <li><strong>Custom</strong> - Webhooks, MCP servers</li>
          </ul>
        </section>

        <section className="docs-section">
          <h2>Need Help?</h2>
          <p>
            <MessageCircleIcon width={16} height={16} style={{ verticalAlign: 'middle', marginRight: '8px' }} />
            Questions? Join our <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a>,
            email <a href="mailto:swarmlet@drose.io">swarmlet@drose.io</a>, or
            open an issue on <a href="https://github.com/cipher982/zerg" target="_blank" rel="noopener noreferrer">GitHub</a>.
          </p>
        </section>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
