import { SparklesIcon, GlobeIcon, GithubIcon, SlackIcon, ZapIcon } from "../icons";

const builtInSkills = [
  { name: "Web Search", icon: GlobeIcon, description: "Search the web for current information" },
  { name: "GitHub", icon: GithubIcon, description: "Interact with repos, issues, and PRs" },
  { name: "Slack", icon: SlackIcon, description: "Send messages to Slack channels" },
  { name: "Quick Search", icon: ZapIcon, description: "Fast web lookup shortcut" },
];

export function SkillsSection() {
  return (
    <section id="skills" className="landing-skills">
      <div className="landing-section-inner">
        <div className="landing-skills-header">
          <SparklesIcon width={32} height={32} className="landing-skills-header-icon" />
          <h2 className="landing-section-title">Extend with Skills</h2>
        </div>
        <p className="landing-section-subtitle">
          Reusable capabilities that give your agents superpowers.
        </p>

        <div className="landing-skills-grid">
          {builtInSkills.map((skill, index) => (
            <div key={index} className="landing-skill-card">
              <skill.icon width={24} height={24} className="landing-skill-icon" />
              <div className="landing-skill-info">
                <strong>{skill.name}</strong>
                <span>{skill.description}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="landing-skills-custom">
          <h3>Create Your Own</h3>
          <p>
            Add a <code>SKILL.md</code> file to define custom skills for your agents.
          </p>
          <pre className="landing-skills-code"><code>{`---
name: my-skill
description: "What this skill does"
tool_dispatch: web_search
---

Instructions for the agent.`}</code></pre>
        </div>
      </div>
    </section>
  );
}
