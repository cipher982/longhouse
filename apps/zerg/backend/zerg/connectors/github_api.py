"""Shared GitHub API utilities for tools and knowledge sync.

Provides reusable HTTP client factories and headers for GitHub API requests.
Used by both github_tools.py (agent tools) and knowledge_sync_service.py.
"""

import httpx

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0


def github_headers(token: str) -> dict[str, str]:
    """Standard GitHub API headers.

    Args:
        token: GitHub Personal Access Token

    Returns:
        Dictionary of headers for GitHub API requests
    """
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
    """Create an AsyncClient with standard GitHub headers and base_url.

    Args:
        token: GitHub Personal Access Token
        timeout: Request timeout in seconds (default: 30.0)

    Returns:
        httpx.AsyncClient configured for GitHub API

    Example:
        async with github_async_client(token) as gh:
            response = await gh.get("/user/repos")
    """
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
    """Create a sync Client with standard GitHub headers and base_url.

    Args:
        token: GitHub Personal Access Token
        timeout: Request timeout in seconds (default: 30.0)

    Returns:
        httpx.Client configured for GitHub API

    Example:
        with github_sync_client(token) as gh:
            response = gh.get("/user/repos")
    """
    return httpx.Client(
        base_url=GITHUB_API_BASE,
        headers=github_headers(token),
        timeout=timeout,
        follow_redirects=True,
    )
