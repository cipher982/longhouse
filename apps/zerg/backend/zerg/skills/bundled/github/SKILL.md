---
name: github
description: "Interact with GitHub repositories, issues, and pull requests using the GitHub API."
emoji: "🐙"
homepage: "https://docs.github.com/en/rest"
primary_env: "GITHUB_TOKEN"
requires:
  env:
    - GITHUB_TOKEN
---

# GitHub Skill

Work with GitHub repositories using the Zerg GitHub tools.

## Available Tools

Use the `github_*` tools for GitHub operations:

- `github_list_repositories` - List your repositories
- `github_create_issue` - Create a new issue
- `github_list_issues` - List issues with filters
- `github_get_issue` - Get issue details
- `github_add_comment` - Comment on issues/PRs
- `github_list_pull_requests` - List PRs
- `github_get_pull_request` - Get PR details

## Examples

### List Open Issues

```python
github_list_issues(owner="myorg", repo="myrepo", state="open")
```

### Create an Issue

```python
github_create_issue(
    owner="myorg",
    repo="myrepo",
    title="Bug: Login fails",
    body="Users report login button not working",
    labels=["bug", "priority-high"]
)
```

## Authentication

Configure GitHub token in Fiche Settings > Connectors, or ensure
`GITHUB_TOKEN` is set in the environment.
