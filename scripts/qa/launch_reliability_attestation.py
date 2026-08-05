"""Importable facade for the hyphenated launch-reliability attestation CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_CLI_PATH = Path(__file__).with_name("launch-reliability-attestation.py")
_SPEC = importlib.util.spec_from_file_location("launch_reliability_attestation_cli", _CLI_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load {_CLI_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

ARTIFACT_KIND = _MODULE.ARTIFACT_KIND
SCHEMA_VERSION = _MODULE.SCHEMA_VERSION
TRUSTED_KEYS_PATH = _MODULE.TRUSTED_KEYS_PATH
MEASURE_NAMES = _MODULE.MEASURE_NAMES
MAX_RECEIPT_LIFETIME = _MODULE.MAX_RECEIPT_LIFETIME
canonical_json = _MODULE.canonical_json
build_subject = _MODULE.build_subject
create_receipt = _MODULE.create_receipt
create_receipt_from_report = _MODULE.create_receipt_from_report
sha256_json = _MODULE.sha256_json


def verify_receipt(*args, **kwargs):
    """Verify using this facade's current trusted-key configuration."""

    kwargs.setdefault("keys_path", TRUSTED_KEYS_PATH)
    return _MODULE.verify_receipt(*args, **kwargs)


def module() -> ModuleType:
    """Expose the CLI module for callers that need its parser constants."""

    return _MODULE
