import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { BookOpenIcon, ZapIcon, PuzzleIcon, CodeIcon, MessageCircleIcon, SettingsIcon } from "../components/icons";
import "../styles/info-pages.css";

export default function DocsPage() {
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Documentation - Swarmlet";
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
          Everything you need to get started with Swarmlet and build powerful AI workflows.
        </p>

        <nav className="docs-nav">
          <a href="#quickstart" className="docs-nav-card">
            <ZapIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Quick Start</h3>
            <p>Get up and running in under 5 minutes</p>
          </a>
          <a href="#canvas" className="docs-nav-card">
            <PuzzleIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Visual Builder</h3>
            <p>Learn the workflow canvas</p>
          </a>
          <a href="#integrations" className="docs-nav-card">
            <SettingsIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Integrations</h3>
            <p>Connect your tools and services</p>
          </a>
          <a href="#api" className="docs-nav-card">
            <CodeIcon width={32} height={32} className="docs-nav-icon" />
            <h3>API Reference</h3>
            <p>Build programmatic integrations</p>
          </a>
        </nav>

        <section id="quickstart" className="docs-section">
          <h2>Quick Start Guide</h2>

          <h3>1. Sign In</h3>
          <p>
            Click "Start Free" on the homepage and sign in with your Google account.
            No credit card required, no trial period. You're in.
          </p>

          <h3>2. Connect Your LLM</h3>
          <p>
            Swarmlet works with your own AI API keys. Go to Settings &gt; Integrations and add your:
          </p>
          <ul>
            <li>OpenAI API key (GPT-4, GPT-3.5)</li>
            <li>Anthropic API key (Claude)</li>
            <li>Or other supported providers</li>
          </ul>

          <h3>3. Create Your First Agent</h3>
          <p>
            Head to the Dashboard and click "New Agent". Give it a name and description
            of what you want it to do. The AI will help you configure it.
          </p>

          <h3>4. Build a Workflow</h3>
          <p>
            Open the Canvas to create visual workflows. Drag and drop nodes to define
            triggers, actions, and conditions. Connect them to create automated pipelines.
          </p>
        </section>

        <section id="canvas" className="docs-section">
          <h2>Visual Builder</h2>

          <p>
            The Canvas is where you design your AI workflows visually. It's built on
            React Flow and gives you a node-based interface for connecting actions.
          </p>

          <h3>Node Types</h3>
          <ul>
            <li><strong>Triggers</strong> - Start your workflow (schedules, webhooks, events)</li>
            <li><strong>Actions</strong> - Do something (send message, call API, update data)</li>
            <li><strong>Conditions</strong> - Branch logic based on data</li>
            <li><strong>AI Nodes</strong> - Use LLM for decisions, text generation, analysis</li>
          </ul>

          <h3>Connecting Nodes</h3>
          <p>
            Click and drag from a node's output handle to another node's input handle
            to create a connection. Data flows along these connections.
          </p>
        </section>

        <section id="integrations" className="docs-section">
          <h2>Integrations</h2>

          <p>
            Swarmlet connects to your existing tools and services. Each integration
            requires you to provide credentials or OAuth authorization.
          </p>

          <h3>Supported Integrations</h3>
          <ul>
            <li><strong>Notifications</strong> - Slack, Discord, Email (Resend), SMS (Twilio)</li>
            <li><strong>Project Management</strong> - GitHub, Jira, Linear, Notion</li>
            <li><strong>Custom</strong> - Webhooks, REST APIs, MCP servers</li>
          </ul>
          <p>
            <em>Coming soon: Google Calendar, Apple Health, Home Assistant</em>
          </p>

          <h3>Adding an Integration</h3>
          <p>
            Go to Settings &gt; Integrations, find the service you want to connect,
            and follow the setup wizard. Most integrations use OAuth for secure access.
          </p>
        </section>

        <section id="api" className="docs-section">
          <h2>API Reference</h2>

          <p>
            Swarmlet exposes a REST API for programmatic access. All endpoints require
            authentication via JWT token.
          </p>

          <h3>Authentication</h3>
          <div className="docs-code">
            <code>
              Authorization: Bearer YOUR_JWT_TOKEN
            </code>
          </div>

          <h3>Base URL</h3>
          <div className="docs-code">
            <code>
              https://api.swarmlet.ai/v1
            </code>
          </div>

          <h3>Key Endpoints</h3>
          <ul>
            <li><code>GET /agents</code> - List your agents</li>
            <li><code>POST /agents</code> - Create a new agent</li>
            <li><code>POST /agents/:id/run</code> - Execute an agent</li>
            <li><code>GET /workflows</code> - List workflows</li>
            <li><code>POST /workflows/:id/trigger</code> - Trigger a workflow</li>
          </ul>

          <p>
            Full API documentation coming soon. In the meantime, check the Network tab
            in your browser's dev tools to see the API in action.
          </p>
        </section>

        <section className="docs-section">
          <h2>Need Help?</h2>
          <p>
            <MessageCircleIcon width={16} height={16} style={{ verticalAlign: 'middle', marginRight: '8px' }} />
            Have questions? Email us at <a href="mailto:hello@swarmlet.ai">hello@swarmlet.ai</a> or
            open an issue on <a href="https://github.com/swarmlet" target="_blank" rel="noopener noreferrer">GitHub</a>.
          </p>
        </section>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
