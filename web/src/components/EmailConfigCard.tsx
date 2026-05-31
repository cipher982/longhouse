import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  fetchEmailStatus,
  saveEmailConfig,
  testEmail,
  deleteEmailConfig,
} from "../services/api/emailConfig";
import { Badge, Button, Card, Input, Spinner } from "./ui";

export default function EmailConfigCard() {
  const queryClient = useQueryClient();
  const [isConfiguring, setIsConfiguring] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [testResult, setTestResult] = useState<{
    success: boolean;
    message: string;
  } | null>(null);
  const [isTesting, setIsTesting] = useState(false);

  // Form state
  const [form, setForm] = useState({
    aws_ses_access_key_id: "",
    aws_ses_secret_access_key: "",
    aws_ses_region: "",
    from_email: "",
    notify_email: "",
  });

  const {
    data: status,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["email-status"],
    queryFn: fetchEmailStatus,
  });

  const keyStatusMap = useMemo(
    () => new Map((status?.keys ?? []).map((keyStatus) => [keyStatus.key, keyStatus])),
    [status?.keys],
  );

  const saveMutation = useMutation({
    mutationFn: saveEmailConfig,
    onSuccess: () => {
      toast.success("Email config saved");
      queryClient.invalidateQueries({ queryKey: ["email-status"] });
      setIsConfiguring(false);
      resetForm();
    },
    onError: (err: Error) => {
      toast.error(`Failed to save: ${err.message}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteEmailConfig,
    onSuccess: (data) => {
      toast.success(`Removed ${data.keys_deleted} override(s)`);
      queryClient.invalidateQueries({ queryKey: ["email-status"] });
    },
    onError: (err: Error) => {
      toast.error(`Failed to remove: ${err.message}`);
    },
  });

  function resetForm() {
    setForm({
      aws_ses_access_key_id: "",
      aws_ses_secret_access_key: "",
      aws_ses_region: "",
      from_email: "",
      notify_email: "",
    });
    setTestResult(null);
    setIsEditing(false);
  }

  function handleToggleConfigure() {
    if (isConfiguring) {
      setIsConfiguring(false);
      resetForm();
    } else {
      setIsConfiguring(true);
      setForm({
        aws_ses_access_key_id: "",
        aws_ses_secret_access_key: "",
        aws_ses_region: status?.aws_ses_region ?? "",
        from_email: status?.from_email ?? "",
        notify_email: status?.notify_email ?? "",
      });
      setIsEditing(!(status?.configured ?? false));
      setTestResult(null);
    }
  }

  async function handleTest() {
    setIsTesting(true);
    setTestResult(null);
    try {
      const result = await testEmail();
      setTestResult(result);
    } catch {
      setTestResult({ success: false, message: "Request failed" });
    } finally {
      setIsTesting(false);
    }
  }

  function handleSave() {
    const baseline = {
      aws_ses_region: status?.aws_ses_region ?? "",
      from_email: status?.from_email ?? "",
      notify_email: status?.notify_email ?? "",
    };

    // Only send fields that actually changed. Secret fields are replace-only.
    const payload: Record<string, string> = {};
    for (const [key, rawValue] of Object.entries(form)) {
      const value = rawValue.trim();
      if (!value) {
        continue;
      }
      if (key in baseline && value === baseline[key as keyof typeof baseline]) {
        continue;
      }
      if (
        (key === "aws_ses_access_key_id" || key === "aws_ses_secret_access_key") &&
        !value
      ) {
        continue;
      }
      if (value) {
        payload[key] = value.trim();
      }
    }
    if (Object.keys(payload).length === 0) {
      toast.error("No changes to save");
      return;
    }
    saveMutation.mutate(payload);
  }

  function handleRemoveOverrides() {
    if (
      confirm(
        "Remove custom email config? Will revert to platform defaults (env vars)."
      )
    ) {
      deleteMutation.mutate();
    }
  }

  const hasChanges = useMemo(() => {
    if (!status) return false;
    return (
      form.aws_ses_access_key_id.trim() !== "" ||
      form.aws_ses_secret_access_key.trim() !== "" ||
      form.aws_ses_region.trim() !== (status.aws_ses_region ?? "") ||
      form.from_email.trim() !== (status.from_email ?? "") ||
      form.notify_email.trim() !== (status.notify_email ?? "")
    );
  }, [form, status]);

  // Derive display state
  const hasDbOverrides = status?.keys.some((k) => k.source === "db") ?? false;
  const sourceLabel = status?.configured
    ? status.source === "db"
      ? "Custom"
      : "Platform"
    : null;

  const currentAccessKey = status?.aws_ses_access_key_preview ?? "Not configured";
  const currentSecretKey = status?.aws_ses_secret_access_key_preview ?? "Not configured";
  const currentRegion = status?.aws_ses_region ?? "Not configured";
  const currentFromEmail = status?.from_email ?? "Not configured";
  const currentNotifyEmail = status?.notify_email ?? "Not configured";

  return (
    <Card>
      <Card.Header>
        <h3 className="settings-section-title ui-section-title">
          Email (Notifications)
        </h3>
      </Card.Header>
      <Card.Body>
        <p className="section-description">
          Email delivery via AWS SES. One instance email receives alerts and test mail.
        </p>

        {error ? (
          <div className="llm-test-result llm-test-result--error">
            Failed to load email status. {String(error)}
          </div>
        ) : isLoading ? (
          <div className="llm-loading">
            <Spinner size="sm" />
          </div>
        ) : (
          <div className="llm-capabilities-list">
            {/* Status row */}
            <div className="llm-capability-row">
              <div className="llm-capability-info">
                <div className="llm-capability-header">
                  <span className="llm-capability-label">Email Delivery</span>
                  <Badge
                    variant={status?.configured ? "success" : "warning"}
                  >
                    {status?.configured ? "Active" : "Not configured"}
                  </Badge>
                  {sourceLabel && (
                    <span className="llm-capability-source">
                      {sourceLabel}
                    </span>
                  )}
                </div>
                <span className="llm-capability-features">
                  Enables: job failure alerts and system notification emails
                </span>
              </div>
              <div className="llm-capability-actions">
                {hasDbOverrides && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleRemoveOverrides}
                    disabled={deleteMutation.isPending}
                  >
                    Remove Override
                  </Button>
                )}
                {status?.configured && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={handleTest}
                    disabled={isTesting}
                  >
                    {isTesting ? "Sending..." : "Test Email"}
                  </Button>
                )}
                <Button
                  variant={isConfiguring ? "secondary" : "primary"}
                  size="sm"
                  onClick={handleToggleConfigure}
                >
                  {isConfiguring ? "Cancel" : "Configure"}
                </Button>
              </div>
            </div>

            {/* Test result */}
            {testResult && (
              <div
                className={`llm-test-result ${testResult.success ? "llm-test-result--success" : "llm-test-result--error"}`}
              >
                {testResult.message}
              </div>
            )}

            {/* Config form */}
            {isConfiguring && (
              <div className="llm-config-form">
                <div className="llm-config-fields">
                  <div className="form-group">
                    <label className="form-label">Current SES Access Key ID</label>
                    <Input
                      value={currentAccessKey}
                      readOnly
                    />
                    <small>
                      {keyStatusMap.get("AWS_SES_ACCESS_KEY_ID")?.configured
                        ? "Masked preview of the configured key."
                        : "No access key configured."}
                    </small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current SES Secret Access Key</label>
                    <Input
                      value={currentSecretKey}
                      readOnly
                    />
                    <small>
                      {keyStatusMap.get("AWS_SES_SECRET_ACCESS_KEY")?.configured
                        ? "Masked preview of the configured secret."
                        : "No secret key configured."}
                    </small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current SES Region</label>
                    <Input
                      value={currentRegion}
                      readOnly
                    />
                    <small>Effective value currently in use</small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current From Email</label>
                    <Input
                      value={currentFromEmail}
                      readOnly
                    />
                    <small>Must be verified in SES</small>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Current Instance Email</label>
                    <Input
                      value={currentNotifyEmail}
                      readOnly
                    />
                    <small>Used for alerts and test emails</small>
                  </div>
                </div>

                {!isEditing && (
                  <div className="llm-config-actions">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setIsEditing(true)}
                    >
                      {status?.configured ? "Edit Settings" : "Add Settings"}
                    </Button>
                  </div>
                )}

                {isEditing && (
                  <>
                    <div className="llm-test-result">
                      Enter replacement secret values only for the fields you want to change.
                    </div>

                    <div className="llm-config-fields">
                      <div className="form-group">
                        <label className="form-label">New SES Access Key ID</label>
                        <Input
                          value={form.aws_ses_access_key_id}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              aws_ses_access_key_id: e.target.value,
                            })
                          }
                          placeholder={
                            keyStatusMap.get("AWS_SES_ACCESS_KEY_ID")?.configured
                              ? "Replace current access key"
                              : "AKIA..."
                          }
                        />
                        <small>
                          {keyStatusMap.get("AWS_SES_ACCESS_KEY_ID")?.configured
                            ? "Leave empty to keep the current access key."
                            : "Required when adding SES credentials."}
                        </small>
                      </div>

                      <div className="form-group">
                        <label className="form-label">New SES Secret Access Key</label>
                        <Input
                          type="password"
                          value={form.aws_ses_secret_access_key}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              aws_ses_secret_access_key: e.target.value,
                            })
                          }
                          placeholder={
                            keyStatusMap.get("AWS_SES_SECRET_ACCESS_KEY")?.configured
                              ? "Replace current secret key"
                              : "Secret key..."
                          }
                        />
                        <small>
                          {keyStatusMap.get("AWS_SES_SECRET_ACCESS_KEY")?.configured
                            ? "Leave empty to keep the current secret key."
                            : "Required when adding SES credentials."}
                        </small>
                      </div>

                      <div className="form-group">
                        <label className="form-label">SES Region</label>
                        <Input
                          value={form.aws_ses_region}
                          onChange={(e) =>
                            setForm({ ...form, aws_ses_region: e.target.value })
                          }
                          placeholder="us-east-1 (default)"
                        />
                        <small>Leave empty for us-east-1</small>
                      </div>

                      <div className="form-group">
                        <label className="form-label">From Email</label>
                        <Input
                          value={form.from_email}
                          onChange={(e) =>
                            setForm({ ...form, from_email: e.target.value })
                          }
                          placeholder="notifications@yourdomain.com"
                        />
                        <small>Must be verified in SES</small>
                      </div>

                      <div className="form-group">
                        <label className="form-label">
                          Instance Email
                        </label>
                        <Input
                          value={form.notify_email}
                          onChange={(e) =>
                            setForm({ ...form, notify_email: e.target.value })
                          }
                          placeholder="you@example.com"
                        />
                        <small>Used for alerts and test emails</small>
                      </div>
                    </div>

                    <div className="llm-config-actions">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setIsEditing(false);
                          setForm({
                            aws_ses_access_key_id: "",
                            aws_ses_secret_access_key: "",
                            aws_ses_region: status?.aws_ses_region ?? "",
                            from_email: status?.from_email ?? "",
                            notify_email: status?.notify_email ?? "",
                          });
                        }}
                      >
                        Stop Editing
                      </Button>
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={handleSave}
                        disabled={saveMutation.isPending || !hasChanges}
                      >
                        {saveMutation.isPending ? "Saving..." : "Save"}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </Card.Body>
    </Card>
  );
}
