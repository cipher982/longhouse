from __future__ import annotations

from zerg.generated.provider_brands import provider_display_name

AUTH_FAILURE_CODES = frozenset(
    {
        "provider_auth_required",
        "provider_auth_missing",
        "provider_auth_invalid",
        "provider_auth_helper_failed",
    }
)


def console_terminal_failure(
    terminal_state: object,
    terminal_reason: object,
    detail: object,
    provider: object,
) -> tuple[str | None, str | None]:
    """Return a durable receipt code and client-safe text for one terminal turn."""
    state = str(terminal_state or "run_failed")
    if state == "run_completed":
        return None, None
    if state != "run_failed":
        return None, state

    reason = str(terminal_reason or "")
    provider_id = str(provider or "").strip().lower()
    if reason in AUTH_FAILURE_CODES:
        label = provider_display_name(provider_id, fallback=provider_id.title() or "Provider")
        if reason == "provider_auth_missing" and provider_id == "omp":
            message = "OMP has no OpenAI credential; run `omp /login` on the selected machine."
        elif reason == "provider_auth_invalid" and provider_id == "codex":
            message = "Codex rejected its OpenAI credential; run `codex login` on the selected machine."
        elif reason == "provider_auth_helper_failed":
            message = "OMP's OpenAI credential helper failed on the selected machine; inspect its error there."
            helper_detail = " ".join(str(detail or "").split())
            if helper_detail:
                message = f"{message} Helper reported: {helper_detail[:300]}"
        elif reason == "provider_auth_required" and provider_id == "opencode":
            message = "OpenCode could not authenticate its configured model provider; run `opencode auth login` on the selected machine."
        else:
            message = f"{label} requires authentication; sign in to {label} on the selected machine."
        return reason, message

    rendered_detail = str(detail or "").strip()
    if rendered_detail:
        return None, f"{state}: {rendered_detail[-1000:]}"
    return None, state
