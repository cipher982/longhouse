import { useMemo, useState } from "react";
import {
  useGitHubRepos,
  useGitHubBranches,
  useCreateKnowledgeSource,
} from "../hooks/useKnowledgeSources";
import type { GitHubRepo } from "../services/api";
import { Button } from "./ui";

interface AddKnowledgeSourceModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type Step = "type" | "github_repo" | "github_config" | "url_config";

function AddKnowledgeSourceDialog({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<Step>("type");
  const [selectedRepo, setSelectedRepo] = useState<GitHubRepo | null>(null);
  const [searchFilter, setSearchFilter] = useState("");

  const [branch, setBranch] = useState("");
  const [includePaths, setIncludePaths] = useState("**/*.md, **/*.mdx");
  const [sourceName, setSourceName] = useState("");

  const [url, setUrl] = useState("");
  const [authHeader, setAuthHeader] = useState("");

  const shouldFetchRepos = step === "github_repo";

  const createMutation = useCreateKnowledgeSource();
  const {
    repositories,
    isLoading: isLoadingRepos,
    error: reposError,
    hasMore,
    fetchNextPage,
    isFetchingNextPage,
  } = useGitHubRepos(30, shouldFetchRepos);
  const { data: branchesData, isLoading: isLoadingBranches } = useGitHubBranches(
    selectedRepo?.owner || "",
    selectedRepo?.name || "",
  );

  const filteredRepos = useMemo(
    () =>
      repositories.filter(
        (repo) =>
          searchFilter === "" ||
          repo.full_name.toLowerCase().includes(searchFilter.toLowerCase()) ||
          (repo.description?.toLowerCase().includes(searchFilter.toLowerCase()) ?? false),
      ),
    [repositories, searchFilter],
  );

  const handleTypeSelect = (type: "github_repo" | "url") => {
    setStep(type === "github_repo" ? "github_repo" : "url_config");
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
      return;
    }

    if (step === "github_config") {
      setStep("github_repo");
      setSelectedRepo(null);
    }
  };

  const handleSubmitGitHub = async () => {
    if (!selectedRepo) return;

    const includePathsArray = includePaths
      .split(",")
      .map((path) => path.trim())
      .filter((path) => path.length > 0);

    await createMutation.mutateAsync({
      name: sourceName || selectedRepo.name,
      source_type: "github_repo",
      config: {
        owner: selectedRepo.owner,
        repo: selectedRepo.name,
        branch: branch || selectedRepo.default_branch,
        include_paths: includePathsArray.length > 0 ? includePathsArray : undefined,
      } as Record<string, unknown>,
    });

    onClose();
  };

  const handleSubmitUrl = async () => {
    const normalizedUrl = url.trim();
    if (!normalizedUrl) return;

    await createMutation.mutateAsync({
      name: sourceName || new URL(normalizedUrl).hostname,
      source_type: "url",
      config: {
        url: normalizedUrl,
        ...(authHeader ? { auth_header: authHeader } : {}),
      } as Record<string, unknown>,
    });

    onClose();
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-container modal-large" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            {step === "type" && "Add Knowledge Source"}
            {step === "github_repo" && "Select Repository"}
            {step === "github_config" && "Configure Source"}
            {step === "url_config" && "Add URL Source"}
          </h2>
          <button className="modal-close-button" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="modal-content">
          {step === "type" && (
            <div className="source-type-grid">
              <button className="source-type-card" onClick={() => handleTypeSelect("github_repo")}>
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
                  <svg
                    width="32"
                    height="32"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                  </svg>
                </div>
                <h3>URL</h3>
                <p>Fetch content from any URL endpoint</p>
              </button>
            </div>
          )}

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

              {isLoadingRepos && repositories.length === 0 && (
                <p className="loading-text">Loading repositories...</p>
              )}

              {reposError && (
                <p className="error-message">
                  Failed to load repositories. Make sure GitHub is connected in Integrations.
                </p>
              )}

              {repositories.length > 0 && !reposError && (
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

                  {hasMore && (
                    <button
                      className="load-more-button"
                      onClick={() => void fetchNextPage()}
                      disabled={isFetchingNextPage}
                    >
                      {isFetchingNextPage ? "Loading..." : "Load More"}
                    </button>
                  )}
                </>
              )}
            </div>
          )}

          {step === "github_config" && selectedRepo && (
            <div className="config-form">
              <div className="form-group">
                <label className="form-label">Repository</label>
                <p className="form-static-value">{selectedRepo.full_name}</p>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="source-name">
                  Source Name
                </label>
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
                <label className="form-label" htmlFor="branch">
                  Branch
                </label>
                {isLoadingBranches ? (
                  <p className="loading-text">Loading branches...</p>
                ) : (
                  <select
                    id="branch"
                    value={branch}
                    onChange={(e) => setBranch(e.target.value)}
                    className="form-input"
                  >
                    {branchesData?.branches.map((repoBranch) => (
                      <option key={repoBranch.name} value={repoBranch.name}>
                        {repoBranch.name} {repoBranch.is_default ? "(default)" : ""}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="include-paths">
                  Include Paths
                </label>
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

          {step === "url_config" && (
            <div className="config-form">
              <div className="form-group">
                <label className="form-label" htmlFor="url-name">
                  Source Name
                </label>
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
                <label className="form-label" htmlFor="url">
                  URL
                </label>
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
            <Button variant="secondary" onClick={handleBack}>
              Back
            </Button>
          )}
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          {step === "github_config" && (
            <Button
              variant="primary"
              onClick={handleSubmitGitHub}
              disabled={createMutation.isPending}
            >
              {createMutation.isPending ? "Adding..." : "Add Source"}
            </Button>
          )}
          {step === "url_config" && (
            <Button
              variant="primary"
              onClick={handleSubmitUrl}
              disabled={createMutation.isPending || !url.trim()}
              data-testid="submit-url-source"
            >
              {createMutation.isPending ? "Adding..." : "Add Source"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export function AddKnowledgeSourceModal({
  isOpen,
  onClose,
}: AddKnowledgeSourceModalProps) {
  if (!isOpen) return null;
  return <AddKnowledgeSourceDialog onClose={onClose} />;
}
