interface Scenario {
  image: string;
  title: string;
  description: string;
  steps: string[];
}

const scenarios: Scenario[] = [
  {
    image: "/images/landing/scenario-health.png",
    title: "Daily Health & Focus Check",
    description: "Connect your watch/health app + calendar",
    steps: [
      "Each morning, your AI checks sleep & schedule",
      "If recovery is low, it suggests rescheduling heavy meetings",
      "You start each day knowing exactly what you can handle"
    ]
  },
  {
    image: "/images/landing/scenario-inbox.png",
    title: "Inbox + Chat Guardian",
    description: "Connect email, Slack, Discord",
    steps: [
      "AI watches for \"urgent\", \"manager\", or \"family\" tags",
      "Filters noise, surfaces what matters",
      "You get a single digest with the 5 things that actually matter"
    ]
  },
  {
    image: "/images/landing/scenario-home.png",
    title: "Smart Home That Knows You",
    description: "Use location + time + calendar",
    steps: [
      "\"Leaving work?\" → preheat/cool home, turn on lights",
      "One trigger, multiple actions, zero config",
      "Your home anticipates you, not the other way around"
    ]
  }
];

export function ScenariosSection() {
  const handleStartFree = () => {
    // Scroll to top and trigger login
    window.scrollTo({ top: 0, behavior: 'smooth' });
    setTimeout(() => {
      document.querySelector<HTMLButtonElement>('.landing-cta-main')?.click();
    }, 500);
  };

  return (
    <section id="scenarios" className="landing-scenarios">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">How It Works</h2>
        <p className="landing-section-subtitle">
          Three real scenarios. Zero buzzwords.
        </p>

        <div className="landing-scenarios-grid">
          {scenarios.map((scenario, index) => (
            <div key={index} className="landing-scenario-card" style={{ animationDelay: `${index * 100}ms` }}>
              <div className="landing-scenario-image">
                <img src={scenario.image} alt={scenario.title} />
              </div>
              <h3 className="landing-scenario-title">{scenario.title}</h3>
              <p className="landing-scenario-desc">{scenario.description}</p>
              <ul className="landing-scenario-steps">
                {scenario.steps.map((step, stepIndex) => (
                  <li key={stepIndex}>{step}</li>
                ))}
              </ul>
              <button className="btn-primary landing-scenario-cta" onClick={handleStartFree}>
                Start Free
              </button>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

