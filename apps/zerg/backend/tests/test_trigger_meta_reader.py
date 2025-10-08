"""Unit tests for backend trigger meta resolver and legacy upgrade path."""

import pytest

from zerg.schemas.workflow import Position
from zerg.schemas.workflow import WorkflowNode
from zerg.schemas.workflow import resolve_trigger_meta


def _node(cfg: dict) -> WorkflowNode:
    return WorkflowNode(id="n1", type="trigger", position=Position(x=0.0, y=0.0), config=cfg)


def test_resolve_trigger_meta_typed_only():
    cfg = {"trigger": {"type": "email", "config": {"enabled": True, "params": {"foo": "bar"}, "filters": []}}}
    meta = resolve_trigger_meta(_node(cfg))
    assert meta["type"] == "email"
    assert meta["config"]["enabled"] is True
    assert meta["config"]["params"]["foo"] == "bar"


def test_workflow_node_upgrades_legacy_trigger_config():
    raw = {
        "id": "legacy",
        "type": "trigger",
        "position": {"x": 0.0, "y": 0.0},
        "config": {
            "trigger_type": "schedule",
            "enabled": False,
            "params": {"cron": "0 12 * * *"},
            "text": "Scheduled Trigger",
        },
    }

    node = WorkflowNode.model_validate(raw)
    assert node.config["trigger"]["type"] == "schedule"
    meta = resolve_trigger_meta(node)
    assert meta["type"] == "schedule"
    assert meta["config"]["enabled"] is False
    assert meta["config"]["params"]["cron"] == "0 12 * * *"
    assert "trigger_type" not in node.config


def test_workflow_node_infers_manual_trigger_when_missing_semantics():
    raw = {
        "id": "visual-only",
        "type": "trigger",
        "position": {"x": 10.0, "y": 20.0},
        "config": {"text": "Manual Trigger", "color": "#f59e0b"},
    }

    node = WorkflowNode.model_validate(raw)
    assert node.config["trigger"]["type"] == "manual"
    meta = resolve_trigger_meta(node)
    assert meta["type"] == "manual"


def test_resolve_trigger_meta_rejects_unvalidated_legacy_payload():
    node = WorkflowNode.model_construct(
        id="raw",
        type="trigger",
        position=Position(x=0.0, y=0.0),
        config={"trigger_type": "schedule", "enabled": False},
    )

    with pytest.raises(Exception):
        resolve_trigger_meta(node)
