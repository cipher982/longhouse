import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, Card, EmptyState, PageShell, SectionHeader, Spinner, Table } from "../components/ui";
import config from "../lib/config";
import { parseUTC } from "../lib/dateUtils";
import { toTitleCaseWords } from "../lib/sessionUtils";

// docs/specs/provider-factory-coherence.md, Phase 5: capability projection
// from the contract, proof status attached separately, both rendered. This
// page is that rendering -- the human-facing mirror of
// GET /agents/provider-capabilities (device-token machine surface), served
// here via GET /admin/provider-capabilities (cookie-authenticated, same
// underlying projection).

interface ProviderCapabilityRow {
  provider: string;
  capability: string;
  assertion_id: string;
  variant: string | null;
  scenario_id: string;
  declared: boolean;
  proof_status: string;
  generated_at: string | null;
  evidence_class: string | null;
}

interface ProviderCapabilityProjectionResponse {
  schema_version: number;
  artifact_kind: string;
  capabilities: ProviderCapabilityRow[];
}

async function fetchProviderCapabilities(): Promise<ProviderCapabilityProjectionResponse> {
  const response = await fetch(`${config.apiBaseUrl}/admin/provider-capabilities`, {
    credentials: "include",
  });

  if (!response.ok) {
    if (response.status === 403) {
      throw new Error("Admin access required");
    }
    throw new Error("Failed to fetch provider capabilities");
  }

  return response.json();
}

function proofStatusBadgeVariant(status: string): "success" | "warning" | "error" | "neutral" {
  switch (status) {
    case "pass":
      return "success";
    case "semantic_fail":
    case "infrastructure_error":
      return "error";
    case "stale":
    case "blocked":
    case "skipped":
      return "warning";
    case "never_proven":
    default:
      return "neutral";
  }
}

function formatGeneratedAt(value: string | null): string {
  if (!value) return "Never";
  return parseUTC(value).toLocaleString();
}

export default function ProviderCapabilitiesPage() {
  const [providerFilter, setProviderFilter] = useState<string>("all");
  const { data, isLoading, error } = useQuery({
    queryKey: ["provider-capabilities"],
    queryFn: fetchProviderCapabilities,
    retry: false,
  });

  const providers = useMemo(() => {
    if (!data) return [];
    return Array.from(new Set(data.capabilities.map((row) => row.provider))).sort();
  }, [data]);

  const rows = useMemo(() => {
    if (!data) return [];
    const filtered =
      providerFilter === "all" ? data.capabilities : data.capabilities.filter((row) => row.provider === providerFilter);
    return [...filtered].sort((a, b) => a.provider.localeCompare(b.provider) || a.capability.localeCompare(b.capability));
  }, [data, providerFilter]);

  if (isLoading) {
    return (
      <PageShell size="wide">
        <EmptyState icon={<Spinner size="sm" />} title="Loading provider capabilities" description="" />
      </PageShell>
    );
  }

  if (error || !data) {
    return (
      <PageShell size="wide">
        <EmptyState
          title="Unable to load provider capabilities"
          description={error instanceof Error ? error.message : "Unknown error"}
        />
      </PageShell>
    );
  }

  const provenCount = data.capabilities.filter((row) => row.proof_status === "pass").length;

  return (
    <PageShell size="wide">
      <SectionHeader
        title="Provider capabilities"
        description="Every declared capability assertion from schemas/managed_providers.yml, joined with its real proof status. A row exists whether or not it has ever been proven -- the schema is the source of truth for what should exist."
        actions={
          <select
            aria-label="Filter by provider"
            className="modal-select"
            style={{ width: "auto", minWidth: "180px", marginBottom: 0 }}
            value={providerFilter}
            onChange={(event) => setProviderFilter(event.target.value)}
          >
            <option value="all">All providers</option>
            {providers.map((provider) => (
              <option key={provider} value={provider}>
                {toTitleCaseWords(provider)}
              </option>
            ))}
          </select>
        }
      />

      <Card>
        <p style={{ marginBottom: "0.75rem" }}>
          {provenCount} of {data.capabilities.length} declared assertions have a passing proof on record.
        </p>

        {rows.length === 0 ? (
          <EmptyState title="No declared capabilities" description="schemas/managed_providers.yml has no capability assertions for this filter." />
        ) : (
          <Table>
            <Table.Header>
              <Table.Cell isHeader>Provider</Table.Cell>
              <Table.Cell isHeader>Capability</Table.Cell>
              <Table.Cell isHeader>Assertion</Table.Cell>
              <Table.Cell isHeader>Proof status</Table.Cell>
              <Table.Cell isHeader>Evidence class</Table.Cell>
              <Table.Cell isHeader>Last proven</Table.Cell>
            </Table.Header>
            <Table.Body>
              {rows.map((row) => (
                <Table.Row key={`${row.provider}:${row.assertion_id}:${row.variant ?? "default"}`}>
                  <Table.Cell>{toTitleCaseWords(row.provider)}</Table.Cell>
                  <Table.Cell>{row.capability}</Table.Cell>
                  <Table.Cell>
                    <code>{row.assertion_id}{row.variant ? `:${row.variant}` : ""}</code>
                  </Table.Cell>
                  <Table.Cell>
                    <Badge variant={proofStatusBadgeVariant(row.proof_status)}>{row.proof_status}</Badge>
                  </Table.Cell>
                  <Table.Cell>{row.evidence_class ?? "—"}</Table.Cell>
                  <Table.Cell>{formatGeneratedAt(row.generated_at)}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        )}
      </Card>
    </PageShell>
  );
}
