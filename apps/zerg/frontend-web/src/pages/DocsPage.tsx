import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ZapIcon, SettingsIcon, MessageCircleIcon, SparklesIcon } from "../components/icons";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function DocsPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();

  useEffect(() => {
    document.title = "Documentation - Longhouse";
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription) {
      metaDescription.setAttribute('content', 'Learn how to use Longhouse. Quick start guides, timeline search tips, and integration setup instructions.');
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
            <span className="info-page-brand-name">Longhouse</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Documentation</h1>
        <p className="info-page-subtitle">
          Get started with Longhouse.
        </p>

        <nav className="docs-nav">
          <a href="#quickstart" className="docs-nav-card">
            <ZapIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Quick Start</h3>
            <p>Get up and running</p>
          </a>
          <a href="#skills" className="docs-nav-card">
            <SparklesIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Skills</h3>
            <p>Extend agent capabilities</p>
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
            Click "Start Free" on the homepage to create an account or sign in.
          </p>

          <h3>2. Start a Session</h3>
          <p>
            Once signed in, start a new agent session from the main view. Tell Oikos what you want to do.
          </p>

          <h3>3. Search the Timeline</h3>
          <p>
            Use the Timeline to search across your sessions, review results, and resume where you left off.
          </p>
        </section>

        <section id="skills" className="docs-section">
          <h2>Skills</h2>
          <p>
            Skills are reusable capabilities that extend what your agents can do.
            They provide specialized knowledge, tools, and behaviors that agents can leverage.
          </p>

          <h3>Built-in Skills</h3>
          <ul>
            <li><strong>Web Search</strong> - Search the web for current information</li>
            <li><strong>GitHub</strong> - Interact with repositories, issues, and PRs</li>
            <li><strong>Slack</strong> - Send messages and manage channels</li>
            <li><strong>Quick Search</strong> - Fast web lookup shortcut</li>
          </ul>

          <h3>How Skills Work</h3>
          <p>
            Skills are automatically loaded and made available to your agents. When an agent
            needs a capability, it can discover and use the appropriate skill. Skills can:
          </p>
          <ul>
            <li>Provide specialized tools (e.g., <code>web_search</code>)</li>
            <li>Add context to the agent's system prompt</li>
            <li>Define custom behaviors and automations</li>
          </ul>

          <h3>Creating Custom Skills</h3>
          <p>
            You can create custom skills by adding a <code>SKILL.md</code> file to
            <code>~/.longhouse/skills/</code>. Each skill has a YAML frontmatter with metadata and
            markdown content describing the skill's purpose and usage.
          </p>
          <pre><code>{`---
name: my-skill
description: "What this skill does"
emoji: "🔧"
tool_dispatch: tool_name  # Optional: wrap a tool
---

# My Skill

Instructions and context for the agent.`}</code></pre>
        </section>

        <section id="integrations" className="docs-section">
          <h2>Integrations</h2>
          <p>
            Connect Longhouse to your existing tools. Go to Settings &gt; Integrations to set up connections.
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
            <MessageCircleIcon width={16} height={16} className="docs-inline-icon" />
            Questions? Join our <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a>,
            email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>, or
            open an issue on <a href="https://github.com/cipher982/longhouse" target="_blank" rel="noopener noreferrer">GitHub</a>.
          </p>
        </section>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
