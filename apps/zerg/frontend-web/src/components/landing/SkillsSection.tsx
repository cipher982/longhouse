import { SparklesIcon } from "../icons";

export function SkillsSection() {
  return (
    <section id="skills" className="landing-skills landing-skills-compact">
      <div className="landing-section-inner">
        <div className="landing-skills-compact-row">
          <SparklesIcon width={24} height={24} className="landing-skills-header-icon" />
          <div className="landing-skills-compact-text">
            <h3 className="landing-skills-compact-title">Extend with Skills</h3>
            <p className="landing-skills-compact-desc">
              Add reusable capabilities to your agents with simple <code>SKILL.md</code> files.
              Web search, GitHub, Slack, and more — or create your own.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
