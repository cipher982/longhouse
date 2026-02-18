import { useState } from "react";
import {
  Badge,
  Button,
  Card,
  Input,
  Spinner,
} from "../ui";
import {
  useRepoConfig,
  useSaveRepoConfig,
  useVerifyRepoConfig,
  useDeleteRepoConfig,
} from "../../hooks/useJobSecrets";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffMs = now - then;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

// ---------------------------------------------------------------------------
// Connect Form (not configured state)
// ---------------------------------------------------------------------------

function RepoConnectForm() {
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [token, setToken] = useState("");

  const saveMutation = useSaveRepoConfig();
  const verifyMutation = useVerifyRepoConfig();

  const formValues = { repo_url: repoUrl, branch, token: token || undefined };

  const handleVerify = () => {
    verifyMutation.mutate(formValues);
  };

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    saveMutation.mutate(formValues);
  };

  const canSubmit = repoUrl.trim().length > 0;

  return (
    <form onSubmit={handleSave} className="settings-stack settings-stack--md">
      <div className="form-group">
        <label htmlFor="repo-url">Repository URL</label>
        <Input
          id="repo-url"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder="https://github.com/user/sauron-jobs.git"
          autoFocus
        />
        <small>HTTPS clone URL of your jobs repository</small>
      </div>
      <div className="form-group">
        <label htmlFor="repo-branch">Branch</label>
        <Input
          id="repo-branch"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder="main"
        />
      </div>
      <div className="form-group">
        <label htmlFor="repo-token">
          Access Token <span className="text-muted">(optional for public repos)</span>
        </label>
        <Input
          id="repo-token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="ghp_... or gitlab PAT"
        />
        <small>Encrypted at rest. Required for private repositories.</small>
      </div>

      {/* Verify result */}
      {verifyMutation.data && (
        <div className="settings-stack settings-stack--sm">
          {verifyMutation.data.success ? (
            <Badge variant="success">
              Verified - commit {verifyMutation.data.commit_sha?.slice(0, 7)}
            </Badge>
          ) : (
            <Badge variant="error">
              Failed: {verifyMutation.data.error ?? "Unknown error"}
            </Badge>
          )}
        </div>
      )}
      {verifyMutation.error && (
        <Badge variant="error">
          Verify error: {verifyMutation.error.message}
        </Badge>
      )}

      <div className="secret-form__actions">
        <Button
          variant="secondary"
          size="sm"
          onClick={handleVerify}
          disabled={!canSubmit || verifyMutation.isPending}
        >
          {verifyMutation.isPending ? (
            <><Spinner size="sm" /> Verifying...</>
          ) : (
            "Verify"
          )}
        </Button>
        <Button
          variant="primary"
          size="sm"
          type="submit"
          disabled={!canSubmit || saveMutation.isPending}
        >
          {saveMutation.isPending ? "Saving..." : "Save"}
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Status Card (configured state)
// ---------------------------------------------------------------------------

function RepoStatusCard({
  config,
}: {
  config: {
    repo_url: string;
    branch: string;
    has_token: boolean;
    last_sync_sha: string | null;
    last_sync_at: string | null;
    last_sync_error: string | null;
    source: string;
  };
}) {
  const deleteMutation = useDeleteRepoConfig();
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [showUpdateToken, setShowUpdateToken] = useState(false);
  const [newToken, setNewToken] = useState("");
  const updateMutation = useSaveRepoConfig();

  const handleUpdateToken = () => {
    updateMutation.mutate(
      { repo_url: config.repo_url, branch: config.branch, token: newToken },
      { onSuccess: () => { setShowUpdateToken(false); setNewToken(""); } },
    );
  };

  const handleDisconnect = () => {
    deleteMutation.mutate(undefined, {
      onSuccess: () => setConfirmDisconnect(false),
    });
  };

  return (
    <div className="settings-stack settings-stack--md">
      <div>
        <strong>Repository</strong>{" "}
        <span className="text-muted">{config.repo_url}</span>
      </div>
      <div>
        <strong>Branch</strong>{" "}
        <span className="text-muted">{config.branch}</span>
      </div>
      <div>
        <strong>Token</strong>{" "}
        <Badge variant={config.has_token ? "success" : "neutral"}>
          {config.has_token ? "configured" : "none"}
        </Badge>
      </div>
      <div>
        <strong>Last Sync</strong>{" "}
        {config.last_sync_sha ? (
          <span className="text-muted">
            {config.last_sync_sha.slice(0, 7)}
            {config.last_sync_at && <> ({relativeTime(config.last_sync_at)})</>}
          </span>
        ) : (
          <span className="text-muted">never</span>
        )}
      </div>
      {!showUpdateToken ? (
        <Button variant="secondary" size="sm" onClick={() => setShowUpdateToken(true)}>
          Update Token
        </Button>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); handleUpdateToken(); }} className="settings-stack settings-stack--sm">
          <Input
            type="password"
            value={newToken}
            onChange={(e) => setNewToken(e.target.value)}
            placeholder="New token (ghp_...)"
          />
          <div className="secret-form__actions">
            <Button variant="ghost" size="sm" onClick={() => setShowUpdateToken(false)}>Cancel</Button>
            <Button variant="primary" size="sm" type="submit" disabled={!newToken.trim() || updateMutation.isPending}>
              {updateMutation.isPending ? "Saving..." : "Save Token"}
            </Button>
          </div>
        </form>
      )}
      {config.last_sync_error && (
        <div>
          <strong>Last Error</strong>{" "}
          <Badge variant="error">{config.last_sync_error}</Badge>
        </div>
      )}

      {confirmDisconnect ? (
        <div className="delete-confirm">
          <p>Disconnect this repository? Jobs will remain but won't receive updates.</p>
          <div className="delete-confirm__actions">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirmDisconnect(false)}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={handleDisconnect}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? "Disconnecting..." : "Disconnect"}
            </Button>
          </div>
        </div>
      ) : (
        <div>
          <Button
            variant="danger"
            size="sm"
            onClick={() => setConfirmDisconnect(true)}
          >
            Disconnect Repo
          </Button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Panel
// ---------------------------------------------------------------------------

export default function RepoConnectPanel() {
  const { data: config, isLoading, error } = useRepoConfig();

  return (
    <Card>
      <Card.Header>
        <h3 className="settings-section-title">Connect Git Repo</h3>
        {config && (
          <Badge variant="success">connected</Badge>
        )}
      </Card.Header>
      <Card.Body>
        {isLoading ? (
          <div className="settings-stack settings-stack--md">
            <Spinner size="sm" />
            <span className="text-muted">Loading repo config...</span>
          </div>
        ) : error ? (
          <div className="settings-stack settings-stack--md">
            <Badge variant="error">Error loading config</Badge>
            <span className="text-muted">{String(error)}</span>
          </div>
        ) : config ? (
          <RepoStatusCard config={config} />
        ) : (
          <RepoConnectForm />
        )}
      </Card.Body>
    </Card>
  );
}
