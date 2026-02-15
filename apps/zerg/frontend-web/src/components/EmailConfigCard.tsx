import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  fetchEmailStatus,
  saveEmailConfig,
  testEmail,
  deleteEmailConfig,
  type EmailStatus,
} from "../services/api/emailConfig";
import { Badge, Button, Card, Input, Spinner } from "./ui";

export default function EmailConfigCard() {
  const queryClient = useQueryClient();
  const [isConfiguring, setIsConfiguring] = useState(false);
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
  }

  function handleToggleConfigure() {
    if (isConfiguring) {
      setIsConfiguring(false);
      resetForm();
    } else {
      setIsConfiguring(true);
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
    // Only send non-empty fields
    const payload: Record<string, string> = {};
    for (const [key, value] of Object.entries(form)) {
      if (value.trim()) {
        payload[key] = value.trim();
      }
    }
    if (!payload.aws_ses_access_key_id && !payload.from_email) {
      toast.error("Provide at least SES Access Key ID or From Email");
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

  // Derive display state
  const hasDbOverrides = status?.keys.some((k) => k.source === "db") ?? false;
  const sourceLabel = status?.configured
    ? status.source === "db"
      ? "Custom"
      : "Platform"
    : null;

  return (
    <Card>
      <Card.Header>
        <h3 className="settings-section-title ui-section-title">
          Email (Notifications)
        </h3>
      </Card.Header>
      <Card.Body>
        <p className="section-description">
          Email delivery for job notifications, digests, and alerts via AWS SES.
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
                  Enables: job failure alerts, daily digests, notification emails
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
                    <label className="form-label">
                      SES Access Key ID
                    </label>
                    <Input
                      type="password"
                      value={form.aws_ses_access_key_id}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          aws_ses_access_key_id: e.target.value,
                        })
                      }
                      placeholder="AKIA..."
                    />
                  </div>

                  <div className="form-group">
                    <label className="form-label">
                      SES Secret Access Key
                    </label>
                    <Input
                      type="password"
                      value={form.aws_ses_secret_access_key}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          aws_ses_secret_access_key: e.target.value,
                        })
                      }
                      placeholder="Secret key..."
                    />
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
                      Notification Email
                    </label>
                    <Input
                      value={form.notify_email}
                      onChange={(e) =>
                        setForm({ ...form, notify_email: e.target.value })
                      }
                      placeholder="you@example.com (default recipient)"
                    />
                    <small>Default recipient for alerts and notifications</small>
                  </div>
                </div>

                <div className="llm-config-actions">
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={handleSave}
                    disabled={saveMutation.isPending}
                  >
                    {saveMutation.isPending ? "Saving..." : "Save"}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </Card.Body>
    </Card>
  );
}
