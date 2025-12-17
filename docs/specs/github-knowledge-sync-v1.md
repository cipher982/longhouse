# GitHub Knowledge Sync - V1 Spec

**Status:** Ready for Implementation
**Author:** David Rose
**Reviewed by:** Senior Dev
**Last Updated:** 2024-12-16

---

## 1. Overview

Enable users to sync private GitHub repositories into Swarmlet's Knowledge Base, allowing agents to search and reference private documentation, runbooks, and AGENTS.md files.

### Key Principles

- **One-time connect, many sources** - User connects GitHub once via OAuth, then adds multiple repo sources
- **Safe by default** - Default patterns exclude secrets (.env, keys, credentials)
- **Incremental sync** - Track SHA to skip unchanged files
- **Reuse existing infrastructure** - GitHub OAuth, CredentialResolver, KnowledgeSource model

---

## 2. Data Flow

```
User connects GitHub (once via OAuth)
         │
         ▼
┌─────────────────────────────┐
│ AccountConnectorCredential  │
│ connector_type: "github"    │
│ encrypted_value: {token}    │
└──────────────┬──────────────┘
               │ Token resolved via
               │ get_account_credential()
               ▼
┌─────────────────────────────┐      ┌─────────────────────────────┐
│ KnowledgeSource             │      │ KnowledgeDocument           │
│ source_type: "github_repo"  │─────▶│ path: https://github.com/...│
│ config: {owner, repo, ...}  │      │ doc_metadata: {sha, ...}    │
└─────────────────────────────┘      └─────────────────────────────┘
```

---

## 3. Config Schema

### KnowledgeSource.config for `source_type="github_repo"`

```python
{
    "owner": "cipher982",                    # Required: Repository owner
    "repo": "mytech",                        # Required: Repository name
    "branch": "main",                        # Optional: Branch (default: repo's default_branch)
    "include_paths": [                       # Optional: Glob patterns to include
        "AGENTS.md",
        "**/*.md",
        "**/*.mdx",
        "**/*.txt",
        "**/*.rst"
    ],
    "exclude_paths": [                       # Optional: Glob patterns to exclude
        ".git/**",
        "node_modules/**",
        "dist/**",
        "build/**",
        ".env*",
        "**/*.pem",
        "**/*.key",
        "**/id_rsa*",
        "**/*secret*"
    ],
    "max_file_size_kb": 500,                 # Optional: Skip files larger than this (default: 500)
}
```

### Pattern Matching Semantics (Important)

Include/exclude patterns are evaluated against **repo-relative POSIX paths** (e.g. `docs/runbook.md`) using **gitignore-style matching** via `pathspec` (`gitwildmatch`).

- Patterns without `/` match at any depth (e.g. `.env*` matches `foo/.env.local`)
- `**` is supported (e.g. `**/*.md`)
- Matching is case-sensitive (Git paths are treated as case-sensitive)

### KnowledgeDocument Storage

| Field          | Value                                                                                           |
| -------------- | ----------------------------------------------------------------------------------------------- |
| `path`         | Clickable GitHub blob URL: `https://github.com/{owner}/{repo}/blob/{branch}/{filepath}`         |
| `title`        | Filename (e.g., `AGENTS.md`)                                                                    |
| `content_text` | Decoded file content (UTF-8)                                                                    |
| `doc_metadata` | `{"github_sha": "abc123", "github_size": 1234, "branch": "main", "repo_path": "docs/guide.md"}` |

---

## 4. API Endpoints

### 4.1 List User's GitHub Repositories

```
GET /api/knowledge/github/repos?page=1&per_page=30
Authorization: Bearer <session>

Response 200:
{
    "repositories": [
        {
            "full_name": "cipher982/mytech",
            "owner": "cipher982",
            "name": "mytech",
            "private": true,
            "default_branch": "main",
            "description": "Infrastructure docs",
            "updated_at": "2024-12-15T10:00:00Z"
        }
    ],
    "page": 1,
    "per_page": 30,
    "has_more": true
}

Response 400 (GitHub not connected):
{
    "detail": "GitHub not connected. Go to Settings > Integrations to connect GitHub."
}
```

### 4.2 List Repository Branches

```
GET /api/knowledge/github/repos/{owner}/{repo}/branches
Authorization: Bearer <session>

Response 200:
{
    "branches": [
        {"name": "main", "protected": true, "is_default": true},
        {"name": "develop", "protected": false, "is_default": false}
    ]
}
```

### 4.3 Create GitHub Repo Source

```
POST /api/knowledge/sources
Authorization: Bearer <session>
Content-Type: application/json

{
    "name": "MyTech Infrastructure",
    "source_type": "github_repo",
    "config": {
        "owner": "cipher982",
        "repo": "mytech",
        "branch": "main",
        "include_paths": ["AGENTS.md", "**/*.md"],
        "exclude_paths": [".git/**", ".env*"]
    },
    "sync_schedule": "0 * * * *"
}

Response 201:
{
    "id": 42,
    "name": "MyTech Infrastructure",
    "source_type": "github_repo",
    "config": {...},
    "sync_status": "pending",
    "sync_schedule": "0 * * * *",
    "created_at": "2024-12-16T12:00:00Z"
}

Response 400 (GitHub not connected):
{
    "detail": "GitHub must be connected before adding a GitHub repository source. Go to Settings > Integrations."
}

Response 400 (Validation error):
{
    "detail": "GitHub repo source missing required fields: ['repo']"
}
```

### 4.4 Trigger Sync (existing, unchanged)

```
POST /api/knowledge/sources/{source_id}/sync
Authorization: Bearer <session>

Response 202:
{
    "status": "syncing",
    "source_id": 42
}
```

**Important:** This endpoint must return quickly. For GitHub repos, run sync in a background task and use a **new DB session** (do not reuse the request session after returning).

---

## 5. Backend Implementation

### 5.1 New Module: `zerg/connectors/github_api.py`

Shared GitHub API utilities (extracted from github_tools.py):

```python
"""Shared GitHub API utilities for tools and knowledge sync."""

import asyncio
from typing import Any, Optional
import httpx

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0


def github_headers(token: str) -> dict[str, str]:
    """Standard GitHub API headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Swarmlet/1.0",
    }


def github_async_client(
    token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """Create an AsyncClient with standard GitHub headers and base_url."""
    return httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers=github_headers(token),
        timeout=timeout,
        follow_redirects=True,
    )


def github_sync_client(
    token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Client:
    """Create a sync Client with standard GitHub headers and base_url."""
    return httpx.Client(
        base_url=GITHUB_API_BASE,
        headers=github_headers(token),
        timeout=timeout,
        follow_redirects=True,
    )
```

### 5.2 New Module: `zerg/connectors/credentials.py`

Account-level credential helper (mirrors CredentialResolver pattern):

```python
"""Account-level credential helpers for services (not tool context)."""

import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from zerg.connectors.registry import ConnectorType
from zerg.utils.crypto import decrypt

logger = logging.getLogger(__name__)


def get_account_credential(
    db: Session,
    owner_id: int,
    connector_type: ConnectorType | str,
) -> Optional[dict[str, Any]]:
    """Get decrypted account-level credential for a user.

    Mirrors the decrypt+JSON pattern from CredentialResolver._resolve_account_credential().

    Args:
        db: Database session
        owner_id: User ID
        connector_type: Connector type (enum or string)

    Returns:
        Decrypted credential dict, or None if not configured
    """
    from zerg.models.models import AccountConnectorCredential

    type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

    cred = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == type_str,
        )
        .first()
    )

    if not cred:
        return None

    try:
        decrypted = decrypt(cred.encrypted_value)
        return json.loads(decrypted)
    except Exception as e:
        logger.warning(
            "Failed to decrypt account credential owner_id=%d connector=%s: %s",
            owner_id,
            type_str,
            str(e),
        )
        return None


def has_account_credential(
    db: Session,
    owner_id: int,
    connector_type: ConnectorType | str,
) -> bool:
    """Check if account-level credential exists (without decrypting)."""
    from zerg.models.models import AccountConnectorCredential

    type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

    return (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == type_str,
        )
        .count()
        > 0
    )
```

### 5.3 Sync Function: `sync_github_repo_source()`

Add to `zerg/services/knowledge_sync_service.py`:

```python
import asyncio
import base64
from typing import Optional

import httpx
import pathspec

from zerg.connectors.credentials import get_account_credential
from zerg.connectors.github_api import github_async_client
from zerg.connectors.registry import ConnectorType
from zerg.models.models import KnowledgeDocument


# Safe defaults - docs only, exclude secrets
DEFAULT_INCLUDE_PATHS = ["AGENTS.md", "**/*.md", "**/*.mdx", "**/*.txt", "**/*.rst"]
DEFAULT_EXCLUDE_PATHS = [
    ".git/**",
    "node_modules/**",
    "dist/**",
    "build/**",
    ".env*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/*secret*",
    "**/*.p12",
    "**/*.pfx",
    "**/credentials*",
]


async def sync_github_repo_source(db: Session, source: KnowledgeSource) -> None:
    """Fetch files from GitHub repo and store as KnowledgeDocuments.

    Uses incremental sync: skips files where SHA matches existing doc.
    Fetches blobs with bounded concurrency.
    """
    config = source.config
    owner, repo = config["owner"], config["repo"]
    branch = config.get("branch")  # May be None - we'll resolve default
    include_paths = config.get("include_paths", DEFAULT_INCLUDE_PATHS)
    exclude_paths = config.get("exclude_paths", DEFAULT_EXCLUDE_PATHS)
    max_size = config.get("max_file_size_kb", 500) * 1024

    # 1. Resolve GitHub token
    creds = get_account_credential(db, source.owner_id, ConnectorType.GITHUB)
    if not creds or "token" not in creds:
        raise ValueError("GitHub not connected. Please connect GitHub in Integrations.")
    token = creds["token"]

    logger.info(f"Syncing GitHub repo {owner}/{repo} for source {source.id}")

    include_spec = pathspec.PathSpec.from_lines("gitwildmatch", include_paths)
    exclude_spec = pathspec.PathSpec.from_lines("gitwildmatch", exclude_paths)

    try:
        async with github_async_client(token) as gh:
            # 2. If branch not specified, get repo's default branch
            if not branch:
                repo_resp = await gh.get(f"/repos/{owner}/{repo}")
                repo_resp.raise_for_status()
                branch = repo_resp.json()["default_branch"]
                logger.info(f"Using default branch: {branch}")

            # 3. Get branch ref -> commit SHA
            ref_resp = await gh.get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
            if ref_resp.status_code == 404:
                raise ValueError(f"Branch '{branch}' not found in {owner}/{repo}")
            ref_resp.raise_for_status()
            commit_sha = ref_resp.json()["object"]["sha"]

            # 4. Get commit -> tree SHA (correct chain)
            commit_resp = await gh.get(f"/repos/{owner}/{repo}/git/commits/{commit_sha}")
            commit_resp.raise_for_status()
            tree_sha = commit_resp.json()["tree"]["sha"]

            # 5. Get recursive tree
            tree_resp = await gh.get(
                f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
                params={"recursive": "1"},
            )
            tree_resp.raise_for_status()
            tree_data = tree_resp.json()

            truncated = bool(tree_data.get("truncated"))
            if truncated:
                logger.warning(f"Repository tree truncated for {owner}/{repo} - large repo (skipping cleanup)")

            # 6. Filter files by patterns and size
            files_to_sync = []
            for item in tree_data.get("tree", []):
                if item["type"] != "blob":
                    continue
                if item.get("size", 0) > max_size:
                    continue

                path = item["path"]

                # Include + exclude (gitignore semantics)
                if not include_spec.match_file(path):
                    continue
                if exclude_spec.match_file(path):
                    continue

                files_to_sync.append(item)

            logger.info(f"Found {len(files_to_sync)} files to sync from {owner}/{repo}")

            # Precompute expected doc paths (used for cleanup)
            expected_paths = {
                f"https://github.com/{owner}/{repo}/blob/{branch}/{f['path']}"
                for f in files_to_sync
            }

            # 7. Build map of existing docs for incremental sync (by path -> sha)
            existing_sha_by_path: dict[str, str] = {}
            offset = 0
            while True:
                batch = knowledge_crud.get_knowledge_documents(
                    db, owner_id=source.owner_id, source_id=source.id, skip=offset, limit=500
                )
                if not batch:
                    break
                for doc in batch:
                    sha = (doc.doc_metadata or {}).get("github_sha")
                    if sha:
                        existing_sha_by_path[doc.path] = sha
                offset += len(batch)

            # 8. Fetch blobs with bounded concurrency
            semaphore = asyncio.Semaphore(10)  # Max 10 concurrent requests

            async def fetch_and_store(file_info: dict) -> None:
                async with semaphore:
                    file_sha = file_info["sha"]
                    file_path = file_info["path"]
                    doc_path = f"https://github.com/{owner}/{repo}/blob/{branch}/{file_path}"

                    # Skip fetch if unchanged (incremental sync)
                    if existing_sha_by_path.get(doc_path) == file_sha:
                        return

                    try:
                        blob_resp = await gh.get(f"/repos/{owner}/{repo}/git/blobs/{file_sha}")
                        if blob_resp.status_code != 200:
                            logger.warning(f"Failed to fetch {file_path}: {blob_resp.status_code}")
                            return

                        blob_data = blob_resp.json()
                        if blob_data.get("encoding") == "base64":
                            content = base64.b64decode(blob_data["content"]).decode("utf-8", errors="replace")
                        else:
                            content = blob_data.get("content", "")

                        knowledge_crud.upsert_knowledge_document(
                            db,
                            source_id=source.id,
                            owner_id=source.owner_id,
                            path=doc_path,
                            content_text=content,
                            title=file_path.split("/")[-1],
                            doc_metadata={
                                "github_sha": file_sha,
                                "github_size": file_info.get("size", 0),
                                "branch": branch,
                                "repo_path": file_path,
                            },
                        )

                    except Exception as e:
                        logger.error(f"Error syncing file {file_path}: {e}")

            # Execute all fetches with concurrency control
            await asyncio.gather(*[fetch_and_store(f) for f in files_to_sync])

            # 9. Cleanup: delete docs that are no longer expected.
            # IMPORTANT:
            # - Compare against expected_paths (tree-derived), not "synced successfully" paths.
            # - Skip cleanup if GitHub returned a truncated tree.
            # - Do NOT use offset pagination while deleting; it can skip rows.
            if not truncated:
                last_id = 0
                while True:
                    batch = (
                        db.query(KnowledgeDocument)
                        .filter(
                            KnowledgeDocument.owner_id == source.owner_id,
                            KnowledgeDocument.source_id == source.id,
                            KnowledgeDocument.id > last_id,
                        )
                        .order_by(KnowledgeDocument.id)
                        .limit(500)
                        .all()
                    )
                    if not batch:
                        break
                    for doc in batch:
                        last_id = doc.id
                        if doc.path not in expected_paths:
                            knowledge_crud.delete_knowledge_document(db, doc.id)
                            logger.info(f"Removed deleted file: {doc.path}")

            # 10. Update sync status
            knowledge_crud.update_source_sync_status(db, source.id, status="success")
            logger.info(f"Successfully synced GitHub repo {owner}/{repo} (branch={branch})")

    except httpx.HTTPStatusError as e:
        error_msg = _handle_github_error(e, owner, repo)
        knowledge_crud.update_source_sync_status(db, source.id, status="failed", error=error_msg)
        raise ValueError(error_msg)

    except Exception as e:
        error_msg = f"Sync failed: {str(e)}"
        knowledge_crud.update_source_sync_status(db, source.id, status="failed", error=error_msg)
        raise


def _handle_github_error(e: httpx.HTTPStatusError, owner: str, repo: str) -> str:
    """Convert GitHub HTTP errors to user-friendly messages."""
    status = e.response.status_code

    if status == 401:
        return "GitHub token expired or revoked. Please reconnect GitHub in Integrations."
    elif status == 403:
        remaining = e.response.headers.get("x-ratelimit-remaining", "?")
        reset = e.response.headers.get("x-ratelimit-reset", "?")
        return f"GitHub API rate limited. Remaining: {remaining}. Resets at: {reset}"
    elif status == 404:
        return f"Repository {owner}/{repo} not found or not accessible with current token."
    else:
        return f"GitHub API error: {status} - {e.response.text[:200]}"
```

### 5.4 Router Updates: `zerg/routers/knowledge.py`

```python
# Add to imports
from zerg.connectors.credentials import get_account_credential, has_account_credential
from zerg.connectors.github_api import github_async_client
from zerg.connectors.registry import ConnectorType

# Update ALLOWED_SOURCE_TYPES
ALLOWED_SOURCE_TYPES = {"url", "github_repo"}

# Add validation in create_source()
if source_in.source_type == "github_repo":
    required_fields = ["owner", "repo"]
    missing = [f for f in required_fields if f not in source_in.config]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"GitHub repo source missing required fields: {missing}",
        )

    if not has_account_credential(db, current_user.id, ConnectorType.GITHUB):
        raise HTTPException(
            status_code=400,
            detail="GitHub must be connected before adding a GitHub repository source. "
                   "Go to Settings > Integrations to connect GitHub.",
        )


# New endpoints
@router.get("/github/repos")
async def list_github_repos(
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List GitHub repositories available to the user (paginated).

    V1: This endpoint mirrors GitHub's /user/repos listing (no global search).
    The UI search box filters locally across loaded pages and uses "Load more".
    """
    creds = get_account_credential(db, current_user.id, ConnectorType.GITHUB)
    if not creds or "token" not in creds:
        raise HTTPException(
            status_code=400,
            detail="GitHub not connected. Go to Settings > Integrations to connect GitHub.",
        )

    token = creds["token"]

    # Build params
    params = {"per_page": per_page, "page": page, "sort": "updated"}

    async with github_async_client(token) as gh:
        response = await gh.get("/user/repos", params=params)
        response.raise_for_status()
        repos_data = response.json()

    repos = []
    for repo in repos_data:
        repos.append({
            "full_name": repo["full_name"],
            "owner": repo["owner"]["login"],
            "name": repo["name"],
            "private": repo["private"],
            "default_branch": repo["default_branch"],
            "description": repo["description"],
            "updated_at": repo["updated_at"],
        })

    # Check if there are more pages via Link header
    link_header = response.headers.get("Link", "")
    has_more = 'rel="next"' in link_header

    return {
        "repositories": repos,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
    }


@router.get("/github/repos/{owner}/{repo}/branches")
async def list_repo_branches(
    owner: str = Path(...),
    repo: str = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List branches for a GitHub repository."""
    creds = get_account_credential(db, current_user.id, ConnectorType.GITHUB)
    if not creds or "token" not in creds:
        raise HTTPException(status_code=400, detail="GitHub not connected")

    token = creds["token"]

    async with github_async_client(token) as gh:
        # Get repo info for default branch
        repo_resp = await gh.get(f"/repos/{owner}/{repo}")
        if repo_resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Repository not found or not accessible")
        repo_resp.raise_for_status()
        default_branch = repo_resp.json()["default_branch"]

        # Get branches
        branches_resp = await gh.get(f"/repos/{owner}/{repo}/branches", params={"per_page": 100})
        branches_resp.raise_for_status()

    branches = []
    for b in branches_resp.json():
        branches.append({
            "name": b["name"],
            "protected": b.get("protected", False),
            "is_default": b["name"] == default_branch,
        })

    # Sort so default branch is first
    branches.sort(key=lambda x: (not x["is_default"], x["name"]))

    return {"branches": branches}
```

### 5.5 Update Dispatcher

In `sync_knowledge_source()`:

```python
if source.source_type == "url":
    await sync_url_source(db, source)
elif source.source_type == "github_repo":
    await sync_github_repo_source(db, source)
else:
    raise ValueError(f"Unsupported source_type: {source.source_type}")
```

---

## 6. Frontend Implementation

### 6.1 New Page: `/settings/knowledge`

**Route:** Add to router as `/settings/knowledge`

**Components:**

- `KnowledgeSourcesPage.tsx` - Main page
- `KnowledgeSourceCard.tsx` - Individual source with status, actions
- `AddSourceModal.tsx` - Type picker + configuration

**Features:**

- List sources with sync status badge (success/failed/pending)
- Last synced time, document count
- Actions: Sync Now, Edit, Delete
- "Add Source" button opens modal

### 6.2 Add Source Modal

**Step 1: Type Selection**

```
┌─────────────────────────────────────┐
│ Add Knowledge Source                │
├─────────────────────────────────────┤
│                                     │
│  ○ URL                              │
│    Sync content from a public URL   │
│                                     │
│  ● GitHub Repository                │
│    Sync files from a private repo   │
│                                     │
│  ○ Upload File (coming soon)        │
│    Upload local files               │
│                                     │
├─────────────────────────────────────┤
│ [Cancel]              [Next →]      │
└─────────────────────────────────────┘
```

**Step 2a: GitHub - Connect Check**

If GitHub not connected:

```
┌─────────────────────────────────────┐
│ Connect GitHub                      │
├─────────────────────────────────────┤
│                                     │
│  To add GitHub repositories, you    │
│  need to connect your GitHub        │
│  account first.                     │
│                                     │
│  [Connect GitHub]                   │
│                                     │
│  Or go to Settings > Integrations   │
│                                     │
├─────────────────────────────────────┤
│ [← Back]                            │
└─────────────────────────────────────┘
```

**Step 2b: GitHub - Repo Selection**

```
┌─────────────────────────────────────┐
│ Select Repository                   │
├─────────────────────────────────────┤
│ Connected as @cipher982             │
│                                     │
│ [Search repositories...        🔍]  │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ ○ cipher982/mytech      Private │ │
│ │   Infrastructure docs           │ │
│ ├─────────────────────────────────┤ │
│ │ ○ cipher982/hdr         Private │ │
│ │   HDRPop application            │ │
│ ├─────────────────────────────────┤ │
│ │ ○ cipher982/zerg        Private │ │
│ │   Swarmlet platform             │ │
│ └─────────────────────────────────┘ │
│                                     │
│ [Load more...]                      │
│                                     │
├─────────────────────────────────────┤
│ [← Back]              [Next →]      │
└─────────────────────────────────────┘
```

**Step 3: GitHub - Configuration**

```
┌─────────────────────────────────────┐
│ Configure: cipher982/mytech         │
├─────────────────────────────────────┤
│                                     │
│ Display Name                        │
│ [MyTech Infrastructure         ]    │
│                                     │
│ Branch                              │
│ [main ▼]                            │
│                                     │
│ ▼ Advanced Options                  │
│ ┌─────────────────────────────────┐ │
│ │ Include patterns (one per line) │ │
│ │ [AGENTS.md                    ] │ │
│ │ [**/*.md                      ] │ │
│ │                                 │ │
│ │ Exclude patterns (one per line) │ │
│ │ [.git/**                      ] │ │
│ │ [.env*                        ] │ │
│ │                                 │ │
│ │ ⚠️ Default patterns exclude     │ │
│ │ secrets (.env, .pem, keys)     │ │
│ └─────────────────────────────────┘ │
│                                     │
│ Sync Schedule                       │
│ ○ Manual only                       │
│ ○ Every hour                        │
│ ● Every day                         │
│ ○ Every week                        │
│                                     │
│ ☑ Sync now after creating           │
│                                     │
├─────────────────────────────────────┤
│ [← Back]           [Create Source]  │
└─────────────────────────────────────┘
```

### 6.3 Schedule to Cron Mapping

| UI Option   | `sync_schedule` value        |
| ----------- | ---------------------------- |
| Manual only | `null`                       |
| Every hour  | `0 * * * *`                  |
| Every day   | `0 6 * * *` (6am UTC)        |
| Every week  | `0 6 * * 0` (Sunday 6am UTC) |

### 6.4 New Hooks

```typescript
// src/hooks/useKnowledgeSources.ts
export function useKnowledgeSources() {
  return useQuery({
    queryKey: ['knowledge-sources'],
    queryFn: () => api.get('/api/knowledge/sources'),
  });
}

export function useCreateKnowledgeSource() {
  return useMutation({
    mutationFn: (data: CreateSourceRequest) =>
      api.post('/api/knowledge/sources', data),
    onSuccess: () => queryClient.invalidateQueries(['knowledge-sources']),
  });
}

export function useSyncKnowledgeSource() {
  return useMutation({
    mutationFn: (sourceId: number) =>
      api.post(`/api/knowledge/sources/${sourceId}/sync`),
  });
}

// src/hooks/useGitHubRepos.ts
export function useGitHubRepos(page = 1) {
  return useQuery({
    queryKey: ['github-repos', page],
    queryFn: () => api.get('/api/knowledge/github/repos', {
      params: { page, per_page: 30 }
    }),
    enabled: /* check if github connected */,
  });
}

export function useGitHubBranches(owner: string, repo: string) {
  return useQuery({
    queryKey: ['github-branches', owner, repo],
    queryFn: () => api.get(`/api/knowledge/github/repos/${owner}/${repo}/branches`),
    enabled: !!owner && !!repo,
  });
}
```

---

## 7. Files to Create/Modify

### New Files

| File                                                                      | Purpose                     |
| ------------------------------------------------------------------------- | --------------------------- |
| `apps/zerg/backend/zerg/connectors/github_api.py`                         | Shared GitHub API utilities |
| `apps/zerg/backend/zerg/connectors/credentials.py`                        | Account credential helpers  |
| `apps/zerg/frontend-web/src/pages/KnowledgeSourcesPage.tsx`               | Knowledge sources list page |
| `apps/zerg/frontend-web/src/components/knowledge/AddSourceModal.tsx`      | Add source wizard           |
| `apps/zerg/frontend-web/src/components/knowledge/GitHubRepoPicker.tsx`    | Repo selection              |
| `apps/zerg/frontend-web/src/components/knowledge/GitHubRepoConfig.tsx`    | Configuration form          |
| `apps/zerg/frontend-web/src/components/knowledge/KnowledgeSourceCard.tsx` | Source card                 |
| `apps/zerg/frontend-web/src/hooks/useKnowledgeSources.ts`                 | Knowledge source hooks      |
| `apps/zerg/frontend-web/src/hooks/useGitHubRepos.ts`                      | GitHub repo hooks           |
| `apps/zerg/backend/tests/test_knowledge_github.py`                        | GitHub sync tests           |

### Modified Files

| File                                                        | Changes                          |
| ----------------------------------------------------------- | -------------------------------- |
| `apps/zerg/backend/pyproject.toml`                          | Add `pathspec` dependency        |
| `apps/zerg/backend/zerg/services/knowledge_sync_service.py` | Add `sync_github_repo_source()`  |
| `apps/zerg/backend/zerg/routers/knowledge.py`               | Add GitHub endpoints, validation |
| `apps/zerg/backend/zerg/tools/builtin/github_tools.py`      | Import from shared module        |
| `apps/zerg/frontend-web/src/routes/App.tsx`                 | Add `/settings/knowledge` route  |

---

## 8. Testing Requirements

### Unit Tests

```python
# tests/test_knowledge_github.py

class TestGitHubRepoSync:
    """Tests for GitHub repo sync."""

    async def test_sync_github_repo_success(self, db_session, _dev_user):
        """Test successful repo sync with mocked GitHub API."""
        # Mock GitHub API responses
        # Create source, trigger sync, verify documents

    async def test_sync_incremental_skip_unchanged(self, db_session, _dev_user):
        """Test that unchanged files (same SHA) are skipped."""

    async def test_sync_removes_deleted_files(self, db_session, _dev_user):
        """Test that files deleted from repo are removed from docs."""

    def test_file_pattern_filtering(self):
        """Test include/exclude pattern matching."""

    def test_default_excludes_secrets(self):
        """Test that default patterns exclude .env, keys, etc."""

    async def test_sync_fails_github_not_connected(self, db_session, _dev_user):
        """Test error when GitHub not connected."""

    async def test_sync_handles_rate_limit(self, db_session, _dev_user):
        """Test graceful handling of GitHub rate limiting."""


class TestGitHubKnowledgeAPI:
    """Tests for GitHub knowledge API endpoints."""

    def test_list_repos_requires_github_connection(self, client):
        """Test 400 when GitHub not connected."""

    def test_create_github_source_validates_config(self, client):
        """Test validation of required fields."""

    def test_create_github_source_requires_connection(self, client):
        """Test 400 when GitHub not connected."""
```

---

## 9. Decisions Made

| Question                 | Decision                                                  |
| ------------------------ | --------------------------------------------------------- |
| Incremental sync?        | **Yes** - Compare blob SHA, skip unchanged                |
| Multi-repo per source?   | **No** - One repo per source (clean model)                |
| Default include patterns | Docs only: `AGENTS.md`, `**/*.md`, `**/*.txt`, `**/*.rst` |
| Default exclude patterns | Secrets: `.env*`, `**/*.pem`, `**/*.key`, `**/secret*`    |
| Sync schedule options    | Manual, Hourly, Daily, Weekly                             |
| Source deletion          | Cascade delete documents (current behavior)               |
| GitHub Enterprise        | Not v1, but design for easy `api_base_url` addition later |

---

## 10. Implementation Order

1. **Backend foundation**
   - Add `pathspec` dependency (gitignore-style matching)
   - Create `zerg/connectors/github_api.py`
   - Create `zerg/connectors/credentials.py`
   - Update `github_tools.py` to use shared module

2. **Sync implementation**
   - Add `sync_github_repo_source()` to knowledge_sync_service.py
   - Add validation to knowledge router
   - Write unit tests

3. **API endpoints**
   - Add `/github/repos` endpoint (paginated)
   - Add `/github/repos/{owner}/{repo}/branches` endpoint
   - Test with curl/Postman

4. **Frontend - Page & List**
   - Create KnowledgeSourcesPage
   - Create KnowledgeSourceCard
   - Wire up hooks

5. **Frontend - Add Modal**
   - Create AddSourceModal with type picker
   - Create GitHubRepoPicker
   - Create GitHubRepoConfig

6. **Polish & Testing**
   - Error handling edge cases
   - Loading states
   - Integration testing
   - Documentation

---

## 11. Future Considerations (Not V1)

- **GitHub Enterprise** - Add `api_base_url` to config schema
- **Webhook-triggered sync** - GitHub webhook on push to auto-sync
- **Multi-branch** - Sync multiple branches as separate doc sets
- **Binary file extraction** - PDF/image content extraction
- **GitLab / Bitbucket** - Same pattern, different API

---

## 12. Implementation Progress

### Stage 1: Backend Foundation ✅

- [x] Add `pathspec>=0.12.0` to pyproject.toml
- [x] Create `zerg/connectors/github_api.py` - shared async/sync HTTP clients
- [x] Create `zerg/connectors/credentials.py` - account credential helpers
- [x] Update `github_tools.py` to use shared module imports

**Commit:** `feat(knowledge): add GitHub API shared modules (Stage 1)`

### Stage 2: Sync Implementation ✅

- [x] Add `sync_github_repo_source()` to knowledge_sync_service.py
- [x] Add github_repo validation to knowledge router
- [x] Write unit tests for GitHub sync (11 tests)

**Commit:** `feat(knowledge): implement GitHub repo sync (Stage 2)`

### Stage 3: API Endpoints ✅

- [x] Add `/github/repos` endpoint (paginated repo list)
- [x] Add `/github/repos/{owner}/{repo}/branches` endpoint
- [x] Test API endpoints (6 new tests, 17 total GitHub tests)

**Commit:** `feat(knowledge): add GitHub API endpoints (Stage 3)`

### Stage 4-6: Frontend & Polish

_Backend complete. Frontend implementation pending._
