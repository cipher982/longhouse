"""Which (provider, mode) pairs accept image attachments on user input.

One explicit table, read by capability projection and by every request
boundary that accepts attachments, so the composer's paperclip and the
server's 4xx agree. The value names how the Machine Agent delivers the
image; the server only needs the row to exist.

- ``native``: the provider payload carries the image itself (Codex
  ``localImage``, OpenCode ``file`` part, pi/OMP ``ImageContent``,
  ``opencode run -f``).
- ``path``: the engine stages the file where the provider's own file/Read
  tool can open it and names the path in the prompt (Claude, Cursor,
  Antigravity Helm).

Antigravity Helm is ``path``: its remote transport is the hook inbox (see
``antigravity_hook_inbox.py``), whose messages only carry ``text`` and whose
``injectSteps: [{"userMessage": ...}]`` reply has nowhere to put an image
part. The engine stages the file and names its path in the message; agy's
model opens it with ``read_file`` (proven live on 1.5.9). Antigravity Console
sends native ACP image parts.
"""

from __future__ import annotations

ATTACHMENT_DELIVERY: dict[tuple[str, str], str] = {
    ("codex", "helm"): "native",
    ("codex", "console"): "native",
    ("opencode", "helm"): "native",
    ("opencode", "console"): "native",
    ("pi", "helm"): "native",
    ("pi", "console"): "native",
    ("omp", "helm"): "native",
    ("omp", "console"): "native",
    ("claude", "helm"): "path",
    ("claude", "console"): "path",
    ("cursor", "helm"): "path",
    ("cursor", "console"): "path",
    ("antigravity", "helm"): "path",
    ("antigravity", "console"): "native",
}


def attachment_delivery(provider: str | None, mode: str | None) -> str | None:
    """Delivery mechanism for this provider in this session mode, or None."""
    key = (str(provider or "").strip().lower(), str(mode or "").strip().lower())
    return ATTACHMENT_DELIVERY.get(key)


def attachments_supported(provider: str | None, mode: str | None) -> bool:
    return attachment_delivery(provider, mode) is not None
