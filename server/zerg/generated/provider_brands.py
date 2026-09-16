# @generated from schemas/managed_providers.yml and config/provider-brands.json — do not edit by hand.
# Run: python3 scripts/generate/provider_brands.py

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "agy": "Antigravity",
    "antigravity": "Antigravity",
    "claude": "Claude",
    "claude-code": "Claude",
    "codex": "Codex",
    "codex-cli": "Codex",
    "cursor": "Cursor",
    "cursor-agent": "Cursor",
    "gemini": "Antigravity",
    "google-antigravity": "Antigravity",
    "oh my pi": "OMP",
    "oh-my-pi": "OMP",
    "ohmypi": "OMP",
    "omp": "OMP",
    "open-code": "OpenCode",
    "openai": "OpenAI",
    "openai-codex": "Codex",
    "opencode": "OpenCode",
    "pi": "Pi",
    "pi-agent": "Pi",
    "z.ai": "Z.ai",
    "zai": "Z.ai"
}


def provider_display_name(provider: object, *, fallback: str = "Session") -> str:
    """Return the canonical label, preserving a readable unknown-provider fallback."""
    cleaned = str(provider or "").strip()
    if not cleaned:
        return fallback
    return PROVIDER_DISPLAY_NAMES.get(cleaned.lower(), cleaned.replace("_", " ").title())
