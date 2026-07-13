/**
 * Provider truth. A capability matrix that says plainly which controls work
 * for each provider — sync is universal, control depth varies.
 */

import { getLaunchProviderSupportList, type LaunchProviderSupport } from "../../lib/providers";
import { ProviderGlyph } from "../ProviderGlyph";

const CAPABILITY_COLUMNS: { key: keyof LaunchProviderSupport | "sync"; label: string }[] = [
  { key: "sync", label: "Sync & search" },
  { key: "launchAndSend", label: "Launch & send" },
  { key: "interrupt", label: "Interrupt" },
  { key: "steerMidTurn", label: "Steer mid-turn" },
  { key: "resume", label: "Resume" },
];

function CapabilityCell({ supported }: { supported: boolean }) {
  return (
    <td className={`landing-providers-cell ${supported ? "yes" : "no"}`}>
      <span aria-hidden="true">{supported ? "✓" : "—"}</span>
      <span className="landing-visually-hidden">{supported ? "Supported" : "Not supported"}</span>
    </td>
  );
}

export function IntegrationsSection() {
  const providers = getLaunchProviderSupportList();

  return (
    <section id="providers" className="landing-providers">
      <div className="landing-section-inner">
        <h2 className="landing-providers-title">
          What works with Longhouse
        </h2>
        <p className="landing-providers-lead">
          Every provider syncs into the same searchable timeline the moment you
          install Longhouse. Launch through Longhouse and you can also drive the
          session remotely — from the browser or your phone.
        </p>

        <div className="landing-providers-tablewrap">
          <table className="landing-providers-table">
            <thead>
              <tr>
                <th scope="col" className="landing-providers-th-provider">Provider</th>
                {CAPABILITY_COLUMNS.map((col) => (
                  <th scope="col" key={col.key}>{col.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.id}>
                  <th scope="row" className="landing-providers-cell-provider">
                    <span className="landing-providers-provider-label">
                      <span className="landing-provider-row-glyph">
                        <ProviderGlyph provider={p.id} size={24} />
                      </span>
                      <span className="landing-provider-row-name">{p.marketingName}</span>
                    </span>
                  </th>
                  <CapabilityCell supported={true} />
                  <CapabilityCell supported={p.launchAndSend} />
                  <CapabilityCell supported={p.interrupt} />
                  <CapabilityCell supported={p.steerMidTurn} />
                  <CapabilityCell supported={p.resume} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
