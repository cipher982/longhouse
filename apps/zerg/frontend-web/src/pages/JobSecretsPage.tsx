import { useState } from "react";
import { toast } from "react-hot-toast";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  PageShell,
  SectionHeader,
  Spinner,
  Table,
} from "../components/ui";
import {
  useJobSecrets,
  useUpsertJobSecret,
  useDeleteJobSecret,
  useJobs,
  useJobSecretsStatus,
  useEnableJob,
  useDisableJob,
} from "../hooks/useJobSecrets";
import type { JobInfo } from "../services/api/jobSecrets";
import { ApiError } from "../services/api/base";
import { parseUTC } from "../lib/dateUtils";

// ---------------------------------------------------------------------------
// Secret form (inline add/edit)
// ---------------------------------------------------------------------------

function SecretForm({
  initialKey,
  initialDescription,
  isPrefill,
  fieldInfo,
  onSubmit,
  onCancel,
  isPending,
}: {
  initialKey?: string;
  initialDescription?: string;
  isPrefill?: boolean;
  fieldInfo?: import("../services/api/jobSecrets").SecretFieldInfo | null;
  onSubmit: (key: string, value: string, description: string) => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const isEdit = !!initialKey && !isPrefill;
  const [key, setKey] = useState(initialKey ?? "");
  const [value, setValue] = useState("");
  const [description, setDescription] = useState(initialDescription ?? "");

  const valueInputType = fieldInfo?.type === "text" || fieldInfo?.type === "url" ? fieldInfo.type : "password";
  const valuePlaceholder = (fieldInfo?.placeholder) || "Enter secret value";
  const valueHint = (fieldInfo?.description) || "Encrypted at rest. Never displayed after saving.";

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!key.trim()) {
      toast.error("Key is required");
      return;
    }
    if (!value.trim()) {
      toast.error("Value is required");
      return;
    }
    onSubmit(key.trim(), value, description.trim());
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="secret-form"
      onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}
    >
      <div className="secret-form__fields">
        <div className="form-group">
          <label htmlFor="secret-key">Key</label>
          <Input
            id="secret-key"
            value={key}
            onChange={(e) => setKey(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))}
            placeholder="MY_API_KEY"
            disabled={isEdit}
            autoFocus={!isEdit}
          />
          <small>Environment variable name (e.g. OPENAI_API_KEY)</small>
        </div>
        <div className="form-group">
          <label htmlFor="secret-value">{isEdit ? "New Value" : "Value"}</label>
          <Input
            id="secret-value"
            type={valueInputType}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={valuePlaceholder}
            autoFocus={isEdit}
          />
          <small>{valueHint}</small>
        </div>
        <div className="form-group">
          <label htmlFor="secret-desc">Description <span className="text-muted">(optional)</span></label>
          <Input
            id="secret-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this secret is for"
          />
        </div>
      </div>
      <div className="secret-form__actions">
        <Button variant="ghost" type="button" size="sm" onClick={onCancel} disabled={isPending}>
          Cancel
        </Button>
        <Button variant="primary" type="submit" size="sm" disabled={isPending}>
          {isPending ? "Saving..." : isEdit ? "Update Secret" : "Add Secret"}
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Delete confirmation
// ---------------------------------------------------------------------------

function DeleteConfirm({
  secretKey,
  onConfirm,
  onCancel,
  isPending,
}: {
  secretKey: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  return (
    <div className="delete-confirm">
      <p>Delete <code>{secretKey}</code>? Jobs using this secret will fail on next run.</p>
      <div className="delete-confirm__actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={isPending}>
          Cancel
        </Button>
        <Button variant="danger" size="sm" onClick={onConfirm} disabled={isPending}>
          {isPending ? "Deleting..." : "Delete"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-job secret status panel
// ---------------------------------------------------------------------------

function JobSecretStatusPanel({
  job,
  onConfigureSecret,
}: {
  job: JobInfo;
  onConfigureSecret: (key: string) => void;
}) {
  const { data: status, isLoading } = useJobSecretsStatus(job.id);
  const enableMutation = useEnableJob();
  const disableMutation = useDisableJob();
  const [showMissingWarning, setShowMissingWarning] = useState<string[] | null>(null);

  const handleToggle = async () => {
    if (job.enabled) {
      disableMutation.mutate(job.id);
    } else {
      enableMutation.mutate(
        { jobId: job.id },
        {
          onError: (error) => {
            if (error instanceof ApiError && error.status === 409) {
              // Pre-flight check failed: extract missing secrets
              // FastAPI wraps in {"detail": {"message": "...", "missing": [...]}}
              const body = error.body as Record<string, unknown> | null;
              const detail = body?.detail as Record<string, unknown> | null;
              const missing = (detail?.missing as string[]) ?? [];
              setShowMissingWarning(missing);
            } else {
              toast.error(`Failed to enable: ${error.message}`);
            }
          },
        },
      );
    }
  };

  const handleForceEnable = () => {
    enableMutation.mutate({ jobId: job.id, force: true });
    setShowMissingWarning(null);
  };

  const configured = status?.secrets.filter((s) => s.configured).length ?? 0;
  const total = status?.secrets.length ?? job.secrets.length;
  const allGood = configured === total;
  const toggling = enableMutation.isPending || disableMutation.isPending;

  return (
    <div className="job-status-row">
      <div className="job-status-row__header">
        <div className="job-status-row__title">
          <span className="job-status-row__name">{job.id}</span>
          <Badge variant={job.enabled ? "success" : "neutral"}>
            {job.enabled ? "enabled" : "disabled"}
          </Badge>
          {job.secrets.length > 0 && status && (
            <Badge variant={allGood ? "success" : "warning"}>
              {configured}/{total} secrets
            </Badge>
          )}
        </div>
        <Button
          variant={job.enabled ? "ghost" : "primary"}
          size="sm"
          onClick={handleToggle}
          disabled={toggling}
        >
          {toggling ? "..." : job.enabled ? "Disable" : "Enable"}
        </Button>
      </div>
      <p className="job-status-row__desc">{job.description}</p>

      {/* Missing secrets warning (409 response) */}
      {showMissingWarning && (
        <div className="job-missing-warning">
          <p>
            This job requires {showMissingWarning.length} unconfigured secret{showMissingWarning.length !== 1 ? "s" : ""}:
            {showMissingWarning.length > 0 && (
              <> <code>{showMissingWarning.join(", ")}</code></>
            )}
          </p>
          <div className="job-missing-warning__actions">
            <Button variant="ghost" size="sm" onClick={() => setShowMissingWarning(null)}>
              Cancel
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setShowMissingWarning(null);
                if (showMissingWarning.length > 0) onConfigureSecret(showMissingWarning[0]);
              }}
            >
              Configure Secrets
            </Button>
            <Button variant="secondary" size="sm" onClick={handleForceEnable} disabled={enableMutation.isPending}>
              Enable Anyway
            </Button>
          </div>
        </div>
      )}

      {/* Secret status details */}
      {isLoading ? (
        <Spinner size="sm" />
      ) : status && status.secrets.length > 0 ? (
        <div className="job-status-row__secrets">
          {status.secrets.map((s) => (
            <div key={s.key} className="job-secret-item">
              <span className={`job-secret-item__indicator ${s.configured ? "job-secret-item__indicator--ok" : "job-secret-item__indicator--missing"}`} />
              <code className="job-secret-item__key">{s.key}</code>
              {s.label && <span className="text-muted">{s.label}</span>}
              {s.required && !s.configured && (
                <Button variant="ghost" size="sm" onClick={() => onConfigureSecret(s.key)}>
                  Configure
                </Button>
              )}
            </div>
          ))}
        </div>
      ) : job.secrets.length === 0 ? (
        <span className="text-muted">No secrets required</span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function JobSecretsPage() {
  const { data: secrets, isLoading: secretsLoading, error: secretsError } = useJobSecrets();
  const { data: jobs, isLoading: jobsLoading, error: jobsError } = useJobs();
  const upsertMutation = useUpsertJobSecret();
  const deleteMutation = useDeleteJobSecret();

  const [showAddForm, setShowAddForm] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [prefillKey, setPrefillKey] = useState<string | null>(null);
  const [prefillField, setPrefillField] = useState<import("../services/api/jobSecrets").SecretFieldInfo | null>(null);

  const handleAdd = (key: string, value: string, description: string) => {
    upsertMutation.mutate(
      { key, data: { value, description: description || undefined } },
      { onSuccess: () => { setShowAddForm(false); setPrefillKey(null); } },
    );
  };

  const handleEdit = (key: string, value: string, description: string) => {
    upsertMutation.mutate(
      { key, data: { value, description: description || undefined } },
      { onSuccess: () => setEditingKey(null) },
    );
  };

  const handleDelete = (key: string) => {
    deleteMutation.mutate(key, { onSuccess: () => setDeletingKey(null) });
  };

  const handleConfigureSecret = (key: string) => {
    // Find matching SecretFieldInfo from any job for richer form UX
    const allFields = (jobs ?? []).flatMap((j) => j.secrets ?? []);
    const field = allFields.find((f) => f.key === key) ?? null;

    // If the secret already exists, edit it. Otherwise, add it with key pre-filled.
    const exists = secrets?.some((s) => s.key === key);
    if (exists) {
      setEditingKey(key);
    } else {
      setPrefillKey(key);
      setPrefillField(field);
      setShowAddForm(true);
    }
    // Scroll to secrets section
    document.getElementById("secrets-section")?.scrollIntoView({ behavior: "smooth" });
  };

  const allJobs = jobs ?? [];

  return (
    <PageShell size="narrow">
      <SectionHeader
        title="Job Secrets"
        description="Manage encrypted secrets for scheduled jobs. Values are encrypted at rest and never displayed."
      />

      <div className="settings-stack settings-stack--lg">
        {/* ---------------------------------------------------------------- */}
        {/* Card 1: Configured Secrets */}
        {/* ---------------------------------------------------------------- */}
        <div id="secrets-section">
        <Card>
          <Card.Header>
            <h3 className="settings-section-title">Configured Secrets</h3>
            {!showAddForm && (
              <Button variant="primary" size="sm" onClick={() => setShowAddForm(true)}>
                + Add Secret
              </Button>
            )}
          </Card.Header>
          <Card.Body>
            {showAddForm && (
              <div className="secret-form-wrapper">
                <SecretForm
                  initialKey={prefillKey ?? undefined}
                  isPrefill={!!prefillKey}
                  fieldInfo={prefillField}
                  onSubmit={handleAdd}
                  onCancel={() => { setShowAddForm(false); setPrefillKey(null); setPrefillField(null); }}
                  isPending={upsertMutation.isPending}
                />
              </div>
            )}

            {secretsLoading ? (
              <div className="secrets-skeleton">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="secrets-skeleton__row" />
                ))}
              </div>
            ) : secretsError ? (
              <EmptyState
                variant="error"
                title="Failed to load secrets"
                description={String(secretsError)}
              />
            ) : !secrets?.length && !showAddForm ? (
              <div className="settings-empty-state">
                <p>No secrets configured yet.</p>
                <p className="text-muted">Add secrets that your scheduled jobs need to run.</p>
              </div>
            ) : secrets?.length ? (
              <Table>
                <Table.Header>
                  <Table.Row>
                    <Table.Cell isHeader>Key</Table.Cell>
                    <Table.Cell isHeader>Description</Table.Cell>
                    <Table.Cell isHeader>Value</Table.Cell>
                    <Table.Cell isHeader>Updated</Table.Cell>
                    <Table.Cell isHeader>Actions</Table.Cell>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {secrets.map((secret) => (
                    <Table.Row key={secret.key}>
                      {editingKey === secret.key ? (
                        <Table.Cell colSpan={5}>
                          <SecretForm
                            initialKey={secret.key}
                            initialDescription={secret.description ?? ""}
                            onSubmit={handleEdit}
                            onCancel={() => setEditingKey(null)}
                            isPending={upsertMutation.isPending}
                          />
                        </Table.Cell>
                      ) : deletingKey === secret.key ? (
                        <Table.Cell colSpan={5}>
                          <DeleteConfirm
                            secretKey={secret.key}
                            onConfirm={() => handleDelete(secret.key)}
                            onCancel={() => setDeletingKey(null)}
                            isPending={deleteMutation.isPending}
                          />
                        </Table.Cell>
                      ) : (
                        <>
                          <Table.Cell>
                            <code>{secret.key}</code>
                          </Table.Cell>
                          <Table.Cell>
                            <span className="text-muted">{secret.description || "—"}</span>
                          </Table.Cell>
                          <Table.Cell>
                            <Badge variant="success">configured</Badge>
                          </Table.Cell>
                          <Table.Cell>
                            <span className="text-muted">
                              {parseUTC(secret.updated_at).toLocaleDateString()}
                            </span>
                          </Table.Cell>
                          <Table.Cell>
                            <div className="secret-actions">
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => setEditingKey(secret.key)}
                              >
                                Edit
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => setDeletingKey(secret.key)}
                              >
                                Delete
                              </Button>
                            </div>
                          </Table.Cell>
                        </>
                      )}
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            ) : null}
          </Card.Body>
        </Card>
        </div>

        {/* ---------------------------------------------------------------- */}
        {/* Card 2: Scheduled Jobs */}
        {/* ---------------------------------------------------------------- */}
        <Card>
          <Card.Header>
            <h3 className="settings-section-title">Scheduled Jobs</h3>
          </Card.Header>
          <Card.Body>
            {jobsLoading ? (
              <div className="secrets-loading">
                <Spinner size="md" />
              </div>
            ) : jobsError ? (
              <EmptyState
                variant="error"
                title="Failed to load jobs"
                description={String(jobsError)}
              />
            ) : !allJobs.length ? (
              <div className="settings-empty-state">
                <p>No scheduled jobs found.</p>
                <p className="text-muted">Jobs registered in the jobs repo will appear here.</p>
              </div>
            ) : (
              <div className="job-status-list">
                {allJobs.map((job) => (
                  <JobSecretStatusPanel
                    key={job.id}
                    job={job}
                    onConfigureSecret={handleConfigureSecret}
                  />
                ))}
              </div>
            )}
          </Card.Body>
        </Card>
      </div>
    </PageShell>
  );
}
