from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


EXPECTED_COPY = {
    "README.md": [
        "OpenCode supports managed send, interrupt, launch, and terminate, but not active-turn steer or pause-answer",
    ],
    "web/src/lib/providers.ts": [
        "Archive, launch, send, interrupt, terminate",
        "Lifecycle control",
    ],
    "web/src/components/landing/IntegrationsSection.tsx": [
        "OpenCode supports managed",
        "send, interrupt, launch, and terminate without active-turn steer",
    ],
    "web/src/pages/docs/IntegrationsPage.tsx": [
        "remote send,",
        "interrupt, and lifecycle terminate",
        "Active-turn steer and pause-answer",
    ],
    "web/src/pages/docs/QuickStartPage.tsx": [
        "OpenCode supports managed send, interrupt, launch, and terminate but",
        "not active-turn steer",
    ],
    "web/src/pages/docs/CLIReferencePage.tsx": [
        "OpenCode supports managed send, interrupt, launch, and terminate but",
        "not active-turn steer",
    ],
    ".agents/skills/managed-provider-cli/SKILL.md": [
        "OpenCode server-bridge send, interrupt, launch, and terminate are first-class",
    ],
    "config/session-propagation-sla.toml": [
        "Native OpenCode launch/send/interrupt/terminate exists",
        "Managed OpenCode remote send/interrupt is a product contract",
    ],
    "docs/specs/managed-provider-session-contract.md": [
        "OpenCode server-bridge sessions support managed send, interrupt, launch, and",
        "terminate through the local `opencode serve` bridge",
    ],
}


FORBIDDEN_COPY = {
    "README.md": [
        "managed live-send",
        "OpenCode supports managed live",
    ],
    "web/src/lib/providers.ts": [
        "Archive, launch, and managed live send",
        "statusLabel: \"Live send\"",
    ],
    "web/src/components/landing/IntegrationsSection.tsx": [
        "OpenCode supports managed live",
    ],
    "web/src/pages/docs/IntegrationsPage.tsx": [
        "managed live-send",
        "Live send and interrupt",
    ],
    "web/src/pages/docs/QuickStartPage.tsx": [
        "managed live send",
        "OpenCode supports managed live",
    ],
    "web/src/pages/docs/CLIReferencePage.tsx": [
        "managed live send",
        "OpenCode supports managed live",
    ],
    ".agents/skills/managed-provider-cli/SKILL.md": [
        "send and interrupt are first-class local control",
        "opencode-channel attach/send/interrupt",
    ],
    "config/session-propagation-sla.toml": [
        "OpenCode managed launch/control is not first-class enough",
        "Remote send/interrupt is not a defined OpenCode product contract yet",
    ],
    "docs/specs/managed-provider-session-contract.md": [
        "OpenCode server-bridge sessions support live send and interrupt",
    ],
}


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_opencode_native_control_copy_is_current() -> None:
    for relative_path, expected_fragments in EXPECTED_COPY.items():
        text = _read(relative_path)
        missing = [fragment for fragment in expected_fragments if fragment not in text]
        assert missing == [], f"{relative_path} is missing {missing}"


def test_opencode_native_control_copy_does_not_regress_to_live_send_only() -> None:
    for relative_path, forbidden_fragments in FORBIDDEN_COPY.items():
        text = _read(relative_path)
        present = [fragment for fragment in forbidden_fragments if fragment in text]
        assert present == [], f"{relative_path} has stale OpenCode copy {present}"
