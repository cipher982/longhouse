"""The plaintext-http rule at the CLI commands that send a device token to a Runtime Host."""

from __future__ import annotations

from pathlib import Path

import typer

from zerg.services.plaintext_http import OPT_IN_ENV
from zerg.services.plaintext_http import Outcome
from zerg.services.plaintext_http import check_runtime_url
from zerg.services.plaintext_http import insecure_warning
from zerg.services.plaintext_http import refusal_message
from zerg.services.shipper.token import get_allow_insecure_http


def enforce_plaintext_rule(url: str, config_dir: Path | None = None, *, exit_code: int = 1) -> None:
    """Apply the plaintext-http rule to a Runtime Host address before a token is sent to it.

    A LAN address needs the opt-in: LONGHOUSE_ALLOW_INSECURE_HTTP=1, or the one
    `longhouse auth --allow-insecure-http` stored with that address. An opted-in
    address warns on stderr each time it is used. A value the rule cannot parse
    is left to the request itself to fail on, as before.
    """
    allow = get_allow_insecure_http(config_dir, url)
    outcome = check_runtime_url(url, allow_insecure_http=allow)
    if outcome in (Outcome.REFUSED_LAN, Outcome.REFUSED_PUBLIC):
        message = refusal_message(url, outcome)
        if outcome is Outcome.REFUSED_LAN:
            message += f" For this command, set {OPT_IN_ENV}=1 or store the opt-in with `longhouse auth --allow-insecure-http`."
        typer.secho(message, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exit_code)
    if outcome is Outcome.ALLOWED_WARN:
        typer.secho(insecure_warning(url), fg=typer.colors.YELLOW, err=True)
