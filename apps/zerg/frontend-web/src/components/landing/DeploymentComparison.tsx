/**
 * DeploymentComparison
 *
 * Side-by-side comparison table for Self-Hosted vs Hosted Beta.
 * Answers: who runs it, data residency, cost, support, upgrade path.
 *
 * Placed after DeploymentOptions to reinforce the deployment decision.
 */

const rows: { label: string; selfHosted: string; hosted: string }[] = [
  { label: "Who runs it", selfHosted: "You", hosted: "Us" },
  { label: "Data residency", selfHosted: "Your machine", hosted: "Our cloud" },
  { label: "Cost", selfHosted: "Free", hosted: "From $5/mo" },
  { label: "Support", selfHosted: "Community", hosted: "Priority" },
  { label: "Upgrade path", selfHosted: "git pull", hosted: "Automatic" },
];

export function DeploymentComparison() {
  return (
    <section className="landing-deployment-comparison">
      <div className="landing-section-inner">
        <h3 className="deployment-comparison-heading">At a Glance</h3>

        <div className="comparison-table-wrapper">
          <table className="deployment-comparison-table">
            <caption className="sr-only">
              Comparison of Self-Hosted and Hosted Beta deployment options
            </caption>
            <thead>
              <tr>
                <th className="deployment-comparison-feature-header" scope="col">
                  <span className="sr-only">Feature</span>
                </th>
                <th className="deployment-comparison-col-header deployment-comparison-col-header--highlighted" scope="col">
                  Self-Hosted
                </th>
                <th className="deployment-comparison-col-header" scope="col">Hosted Beta</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label}>
                  <th className="deployment-comparison-label" scope="row">{row.label}</th>
                  <td className="deployment-comparison-value deployment-comparison-value--highlighted">
                    {row.selfHosted}
                  </td>
                  <td className="deployment-comparison-value">{row.hosted}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
