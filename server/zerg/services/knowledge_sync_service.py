"""Knowledge Sync Service - fetches and syncs knowledge sources."""

import asyncio
import base64
import logging

import httpx
import pathspec
from sqlalchemy.orm import Session

from zerg.connectors.credentials import get_account_credential
from zerg.connectors.github_api import github_async_client
from zerg.connectors.registry import ConnectorType
from zerg.crud import knowledge_crud
from zerg.models.models import KnowledgeDocument
from zerg.models.models import KnowledgeSource

logger = logging.getLogger(__name__)


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

    Args:
        db: Database session
        source: KnowledgeSource to sync (must be type="github_repo")

    Raises:
        ValueError: If source_type is not "github_repo" or GitHub not connected
    """
    if source.source_type != "github_repo":
        raise ValueError(f"Expected source_type='github_repo', got '{source.source_type}'")

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
            expected_paths = {f"https://github.com/{owner}/{repo}/blob/{branch}/{f['path']}" for f in files_to_sync}

            # 7. Build map of existing docs for incremental sync (by path -> sha)
            existing_sha_by_path: dict[str, str] = {}
            offset = 0
            while True:
                batch = knowledge_crud.get_knowledge_documents(db, owner_id=source.owner_id, source_id=source.id, skip=offset, limit=500)
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
                                # V1.1: Provenance metadata for citations
                                "github_commit_sha": commit_sha,
                                "github_permalink_url": f"https://github.com/{owner}/{repo}/blob/{commit_sha}/{file_path}",
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
            # V1.1: Truncated repos must be visible to users (not silently "success")
            if truncated:
                error_msg = (
                    f"Partial sync: repository too large for full crawl. "
                    f"{len(files_to_sync)} files synced, but some files may be missing. "
                    f"Consider using more specific include_paths patterns."
                )
                knowledge_crud.update_source_sync_status(db, source.id, status="failed", error=error_msg)
                logger.warning(f"Partial sync for {owner}/{repo}: {error_msg}")
            else:
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


async def sync_url_source(db: Session, source: KnowledgeSource) -> None:
    """Fetch URL content and store as KnowledgeDocument.

    Phase 0 implementation: fetches a single URL and stores its content.

    Args:
        db: Database session
        source: KnowledgeSource to sync (must be type="url")

    Raises:
        ValueError: If source_type is not "url"
        httpx.HTTPError: If HTTP request fails
    """
    if source.source_type != "url":
        raise ValueError(f"Expected source_type='url', got '{source.source_type}'")

    url = source.config.get("url")
    if not url:
        raise ValueError("URL source missing 'url' in config")

    logger.info(f"Syncing URL source {source.id}: {url}")

    # Prepare headers with optional auth
    headers = {}
    if auth := source.config.get("auth_header"):
        headers["Authorization"] = auth

    try:
        # Fetch URL content
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content = response.text

        # Extract title from URL (last path segment or hostname)
        from urllib.parse import urlparse

        parsed = urlparse(url)
        title = parsed.path.strip("/").split("/")[-1] if parsed.path.strip("/") else parsed.hostname

        # Upsert document
        doc = knowledge_crud.upsert_knowledge_document(
            db,
            source_id=source.id,
            owner_id=source.owner_id,
            path=url,
            content_text=content,
            title=title,
            doc_metadata={
                "content_type": response.headers.get("content-type"),
                "status_code": response.status_code,
                "content_length": len(content),
            },
        )

        # Update source sync status
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="success",
        )

        logger.info(f"Successfully synced URL source {source.id}, document {doc.id}")

    except httpx.HTTPError as exc:
        error_msg = f"HTTP error fetching {url}: {exc}"
        logger.error(error_msg)

        # Update source with error
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="failed",
            error=error_msg,
        )

        raise

    except Exception as exc:
        error_msg = f"Unexpected error syncing {url}: {exc}"
        logger.exception(error_msg)

        # Update source with error
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="failed",
            error=error_msg,
        )

        raise


async def sync_user_text_source(db: Session, source: KnowledgeSource) -> None:
    """Sync a user_text knowledge source.

    Stores the user-provided content as a single KnowledgeDocument.

    Args:
        db: Database session
        source: KnowledgeSource to sync (must be type="user_text")

    Raises:
        ValueError: If source_type is not "user_text" or content is missing
    """
    if source.source_type != "user_text":
        raise ValueError(f"Expected source_type='user_text', got '{source.source_type}'")

    content = source.config.get("content")
    if not content or not isinstance(content, str):
        error_msg = "User text source missing 'content' in config"
        knowledge_crud.update_source_sync_status(db, source.id, status="failed", error=error_msg)
        raise ValueError(error_msg)

    try:
        knowledge_crud.upsert_knowledge_document(
            db,
            source_id=source.id,
            owner_id=source.owner_id,
            path=f"user_text:{source.id}",
            content_text=content,
            title=source.name,
            doc_metadata={"source_type": "user_text"},
        )

        knowledge_crud.update_source_sync_status(db, source.id, status="success")
    except Exception as exc:
        error_msg = f"Unexpected error syncing user_text source {source.id}: {exc}"
        logger.exception(error_msg)
        knowledge_crud.update_source_sync_status(db, source.id, status="failed", error=error_msg)
        raise


async def sync_knowledge_source(db: Session, source_id: int) -> None:
    """Sync a knowledge source by ID.

    Dispatches to the appropriate sync function based on source_type.

    Args:
        db: Database session
        source_id: KnowledgeSource ID to sync

    Raises:
        ValueError: If source not found or unsupported type
    """
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise ValueError(f"Knowledge source {source_id} not found")

    logger.info(f"Starting sync for source {source_id} ({source.source_type})")

    # Set status to "syncing" at the start
    knowledge_crud.update_source_sync_status(db, source_id, status="syncing")

    if source.source_type == "url":
        await sync_url_source(db, source)
    elif source.source_type == "github_repo":
        await sync_github_repo_source(db, source)
    elif source.source_type == "user_text":
        await sync_user_text_source(db, source)
    else:
        raise ValueError(f"Unsupported source_type: {source.source_type}")


async def sync_all_sources(db: Session, owner_id: int) -> dict:
    """Sync all knowledge sources for a user.

    Args:
        db: Database session
        owner_id: User ID whose sources to sync

    Returns:
        Dictionary with sync statistics
    """
    sources = knowledge_crud.get_knowledge_sources(db, owner_id=owner_id, limit=1000)

    stats = {
        "total": len(sources),
        "success": 0,
        "failed": 0,
        "errors": [],
    }

    for source in sources:
        try:
            await sync_knowledge_source(db, source.id)
            stats["success"] += 1
        except Exception as exc:
            stats["failed"] += 1
            stats["errors"].append(
                {
                    "source_id": source.id,
                    "source_name": source.name,
                    "error": str(exc),
                }
            )
            logger.error(f"Failed to sync source {source.id}: {exc}")

    logger.info(f"Synced {stats['success']}/{stats['total']} sources for user {owner_id}")
    return stats
