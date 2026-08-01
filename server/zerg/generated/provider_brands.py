# @generated from config/provider-brands.json — do not edit by hand.
# Run: python3 scripts/generate/provider_brands.py

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "antigravity": "Antigravity",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Antigravity",
    "openai": "OpenAI",
    "opencode": "OpenCode",
    "z.ai": "Z.ai",
    "zai": "Z.ai"
}


def provider_display_name(provider: object, *, fallback: str = "Session") -> str:
    """Return the canonical label, preserving a readable unknown-provider fallback."""
    cleaned = str(provider or "").strip()
    if not cleaned:
        return fallback
    return PROVIDER_DISPLAY_NAMES.get(cleaned.lower(), cleaned.replace("_", " ").title())
