export function DifferentiationSection() {
  const comparisons = [
    {
      aspect: "Built for",
      swarmlet: "Individuals + nerds",
      enterprise: "Teams, managers, enterprises"
    },
    {
      aspect: "Setup",
      swarmlet: "Connect your own apps in minutes",
      enterprise: "IT tickets, SSO, sales calls"
    },
    {
      aspect: "Pricing",
      swarmlet: "Flat, cheap personal plan",
      enterprise: "Per-seat, \"talk to sales\""
    },
    {
      aspect: "UX",
      swarmlet: "Human stories, not dashboards",
      enterprise: "Admin panels, dashboards, CRMs"
    },
    {
      aspect: "Control",
      swarmlet: "You own your data & agents",
      enterprise: "Shared company workspace"
    }
  ];

  return (
    <section className="landing-differentiation">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Built Different</h2>
        <p className="landing-section-subtitle">
          Not another enterprise tool pretending to be personal.
        </p>

        <div className="landing-diff-table-wrapper">
          <table className="landing-diff-table">
            <thead>
              <tr>
                <th></th>
                <th className="landing-diff-us">
                  <span className="landing-diff-badge">Swarmlet</span>
                </th>
                <th className="landing-diff-them">Enterprise Tools</th>
              </tr>
            </thead>
            <tbody>
              {comparisons.map((row, index) => (
                <tr key={index}>
                  <td className="landing-diff-aspect">{row.aspect}</td>
                  <td className="landing-diff-us">
                    <span className="landing-diff-check">✓</span>
                    {row.swarmlet}
                  </td>
                  <td className="landing-diff-them">{row.enterprise}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

