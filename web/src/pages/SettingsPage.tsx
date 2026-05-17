import { useReadinessFlag } from "../lib/readiness-contract";
import { PageShell, SectionHeader } from "../components/ui";
import EmailConfigCard from "../components/EmailConfigCard";

export default function SettingsPage() {
  useReadinessFlag({ ready: true });

  return (
    <PageShell size="narrow" className="settings-page-container">
      <SectionHeader
        title="Settings"
        description="Manage the active Longhouse instance settings exposed in the product today."
      />

      <div className="settings-stack settings-stack--lg">
        <EmailConfigCard />
      </div>
    </PageShell>
  );
}
