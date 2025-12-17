import React, { useState, useEffect } from "react";
import {
  useGitHubRepos,
  useGitHubBranches,
  useCreateKnowledgeSource,
} from "../hooks/useKnowledgeSources";
import type { GitHubRepo } from "../services/api";

interface AddKnowledgeSourceModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type SourceType = "github_repo" | "url";
type Step = "type" | "github_repo" | "github_config" | "url_config";

export function AddKnowledgeSourceModal({
  isOpen,
  onClose,
}: AddKnowledgeSourceModalProps) {
  const [step, setStep] = useState<Step>("type");
  const [sourceType, setSourceType] = useState<SourceType | null>(null);
  const [selectedRepo, setSelectedRepo] = useState<GitHubRepo | null>(null);
  const [page, setPage] = useState(1);
  const [searchFilter, setSearchFilter] = useState("");
  const [accumulatedRepos, setAccumulatedRepos] = useState<GitHubRepo[]>([]);

  // GitHub config state
  const [branch, setBranch] = useState<string>("");
  const [includePaths, setIncludePaths] = useState("**/*.md, **/*.mdx");
  const [sourceName, setSourceName] = useState("");

  // URL config state
  const [url, setUrl] = useState("");
  const [authHeader, setAuthHeader] = useState("");

  // Only fetch repos when modal is open AND we're on the github_repo step
  const shouldFetchRepos = isOpen && step === "github_repo";

  const createMutation = useCreateKnowledgeSource();
  const { data: reposData, isLoading: isLoadingRepos, error: reposError } = useGitHubRepos(page, 30, shouldFetchRepos);
  const { data: branchesData, isLoading: isLoadingBranches } = useGitHubBranches(
    selectedRepo?.owner || "",
    selectedRepo?.name || ""
  );

  // Accumulate repos across pages
  useEffect(() => {
    if (reposData?.repositories) {
      if (page === 1) {
        // First page replaces
        setAccumulatedRepos(reposData.repositories);
      } else {
        // Subsequent pages accumulate (avoiding duplicates by full_name)
        setAccumulatedRepos((prev) => {
          const existing = new Set(prev.map((r) => r.full_name));
          const newRepos = reposData.repositories.filter((r) => !existing.has(r.full_name));
          return [...prev, ...newRepos];
        });
      }
    }
  }, [reposData, page]);

  const handleClose = () => {
    // Reset state
    setStep("type");
    setSourceType(null);
    setSelectedRepo(null);
    setPage(1);
    setSearchFilter("");
    setAccumulatedRepos([]);
    setBranch("");
    setIncludePaths("**/*.md, **/*.mdx");
    setSourceName("");
    setUrl("");
    setAuthHeader("");
    onClose();
  };

  const handleTypeSelect = (type: SourceType) => {
    setSourceType(type);
    if (type === "github_repo") {
      setStep("github_repo");
    } else {
      setStep("url_config");
    }
  };

  const handleRepoSelect = (repo: GitHubRepo) => {
    setSelectedRepo(repo);
    setSourceName(repo.name);
    setBranch(repo.default_branch);
    setStep("github_config");
  };

  const handleBack = () => {
    if (step === "github_repo" || step === "url_config") {
      setStep("type");
      setSourceType(null);
    } else if (step === "github_config") {
      setStep("github_repo");
      setSelectedRepo(null);
    }
  };

  const handleSubmitGitHub = async () => {
    if (!selectedRepo) return;

    const includePathsArray = includePaths
      .split(",")
      .map((p) => p.trim())
      .filter((p) => p.length > 0);

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await createMutation.mutateAsync({
      name: sourceName || selectedRepo.name,
      source_type: "github_repo",
      config: {
        owner: selectedRepo.owner,
        repo: selectedRepo.name,
        branch: branch || selectedRepo.default_branch,
        include_paths: includePathsArray.length > 0 ? includePathsArray : undefined,
      } as any,
    });

    handleClose();
  };

  const handleSubmitUrl = async () => {
    if (!url) return;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await createMutation.mutateAsync({
      name: sourceName || new URL(url).hostname,
      source_type: "url",
      config: {
        url,
        ...(authHeader ? { auth_header: authHeader } : {}),
      } as any,
    });

    handleClose();
  };

  // Filter accumulated repos by search
  const filteredRepos = accumulatedRepos.filter(
    (repo) =>
      searchFilter === "" ||
      repo.full_name.toLowerCase().includes(searchFilter.toLowerCase()) ||
      (repo.description?.toLowerCase().includes(searchFilter.toLowerCase()) ?? false)
  );

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className="modal-container modal-large" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            {step === "type" && "Add Knowledge Source"}
            {step === "github_repo" && "Select Repository"}
            {step === "github_config" && "Configure Source"}
            {step === "url_config" && "Add URL Source"}
          </h2>
          <button className="modal-close-button" onClick={handleClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="modal-content">
          {/* Step 1: Select Type */}
          {step === "type" && (
            <div className="source-type-grid">
              <button
                className="source-type-card"
                onClick={() => handleTypeSelect("github_repo")}
              >
                <div className="source-type-icon">
                  <svg width="32" height="32" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
                  </svg>
                </div>
                <h3>GitHub Repository</h3>
                <p>Sync documentation and code from a GitHub repo</p>
              </button>

              <button
                className="source-type-card"
                onClick={() => handleTypeSelect("url")}
                data-testid="source-type-url"
              >
                <div className="source-type-icon">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                  </svg>
                </div>
                <h3>URL</h3>
                <p>Fetch content from any URL endpoint</p>
              </button>
            </div>
          )}

          {/* Step 2: Select GitHub Repo */}
          {step === "github_repo" && (
            <div className="repo-picker">
              <div className="repo-search">
                <input
                  type="text"
                  placeholder="Filter repositories..."
                  value={searchFilter}
                  onChange={(e) => setSearchFilter(e.target.value)}
                  className="form-input"
                />
              </div>

              {/* Show loading only on initial load (page 1 with no data yet) */}
              {isLoadingRepos && accumulatedRepos.length === 0 && (
                <p className="loading-text">Loading repositories...</p>
              )}

              {reposError && (
                <p className="error-message">
                  Failed to load repositories. Make sure GitHub is connected in Integrations.
                </p>
              )}

              {/* Show repos once we have any data */}
              {accumulatedRepos.length > 0 && !reposError && (
                <>
                  <div className="repo-list">
                    {filteredRepos.map((repo) => (
                      <button
                        key={repo.full_name}
                        className="repo-item"
                        onClick={() => handleRepoSelect(repo)}
                      >
                        <div className="repo-item-main">
                          <span className="repo-name">{repo.full_name}</span>
                          {repo.private && <span className="repo-private-badge">Private</span>}
                        </div>
                        {repo.description && (
                          <p className="repo-description">{repo.description}</p>
                        )}
                      </button>
                    ))}
                  </div>

                  {reposData?.has_more && (
                    <button
                      className="load-more-button"
                      onClick={() => setPage((p) => p + 1)}
                      disabled={isLoadingRepos}
                    >
                      {isLoadingRepos ? "Loading..." : "Load More"}
                    </button>
                  )}
                </>
              )}
            </div>
          )}

          {/* Step 3: Configure GitHub Source */}
          {step === "github_config" && selectedRepo && (
            <div className="config-form">
              <div className="form-group">
                <label className="form-label">Repository</label>
                <p className="form-static-value">{selectedRepo.full_name}</p>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="source-name">Source Name</label>
                <input
                  type="text"
                  id="source-name"
                  value={sourceName}
                  onChange={(e) => setSourceName(e.target.value)}
                  placeholder={selectedRepo.name}
                  className="form-input"
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="branch">Branch</label>
                {isLoadingBranches ? (
                  <p className="loading-text">Loading branches...</p>
                ) : (
                  <select
                    id="branch"
                    value={branch}
                    onChange={(e) => setBranch(e.target.value)}
                    className="form-input"
                  >
                    {branchesData?.branches.map((b) => (
                      <option key={b.name} value={b.name}>
                        {b.name} {b.is_default ? "(default)" : ""}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="include-paths">Include Paths</label>
                <input
                  type="text"
                  id="include-paths"
                  value={includePaths}
                  onChange={(e) => setIncludePaths(e.target.value)}
                  placeholder="**/*.md, **/*.mdx"
                  className="form-input"
                />
                <small className="form-hint">
                  Comma-separated glob patterns (e.g., **/*.md, docs/**)
                </small>
              </div>
            </div>
          )}

          {/* URL Config */}
          {step === "url_config" && (
            <div className="config-form">
              <div className="form-group">
                <label className="form-label" htmlFor="url-name">Source Name</label>
                <input
                  type="text"
                  id="url-name"
                  value={sourceName}
                  onChange={(e) => setSourceName(e.target.value)}
                  placeholder="My Documentation"
                  className="form-input"
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="url">URL</label>
                <input
                  type="url"
                  id="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://example.com/docs.md"
                  className="form-input"
                  data-testid="url-input"
                  required
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="auth-header">
                  Authorization Header (optional)
                </label>
                <input
                  type="text"
                  id="auth-header"
                  value={authHeader}
                  onChange={(e) => setAuthHeader(e.target.value)}
                  placeholder="Bearer your-token"
                  className="form-input"
                />
                <small className="form-hint">
                  For private URLs that require authentication
                </small>
              </div>
            </div>
          )}
        </div>

        <div className="modal-actions">
          {step !== "type" && (
            <button className="modal-button modal-button-secondary" onClick={handleBack}>
              Back
            </button>
          )}
          <button className="modal-button modal-button-secondary" onClick={handleClose}>
            Cancel
          </button>
          {step === "github_config" && (
            <button
              className="modal-button modal-button-primary"
              onClick={handleSubmitGitHub}
              disabled={createMutation.isPending}
            >
              {createMutation.isPending ? "Adding..." : "Add Source"}
            </button>
          )}
          {step === "url_config" && (
            <button
              className="modal-button modal-button-primary"
              onClick={handleSubmitUrl}
              disabled={createMutation.isPending || !url}
              data-testid="submit-url-source"
            >
              {createMutation.isPending ? "Adding..." : "Add Source"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
