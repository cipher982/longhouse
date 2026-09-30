"""The plaintext-http rule for Runtime Host addresses, against the shared case list.

`schemas/plaintext-http-vectors.json` is read by the Python, Rust engine, macOS
Desktop and iOS tests alike, so the four clients cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from zerg.cli import connect
from zerg.services.machine_state import load_machine_state
from zerg.services.machine_state import write_machine_state
from zerg.services.plaintext_http import OPT_IN_ENV
from zerg.services.plaintext_http import OPT_IN_FLAG
from zerg.services.plaintext_http import HostClass
from zerg.services.plaintext_http import Outcome
from zerg.services.plaintext_http import check_runtime_url
from zerg.services.plaintext_http import classify_host
from zerg.services.plaintext_http import insecure_warning
from zerg.services.plaintext_http import refusal_message
from zerg.services.shipper.token import get_allow_insecure_http
from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import normalize_zerg_url
from zerg.services.shipper.token import save_zerg_url

_VECTORS = json.loads((Path(__file__).parents[2] / "schemas" / "plaintext-http-vectors.json").read_text())


@pytest.mark.parametrize("case", _VECTORS["hosts"], ids=lambda case: case["host"] or "<empty>")
def test_host_classification_matches_the_shared_vectors(case):
    assert classify_host(case["host"]).value == case["class"]


@pytest.mark.parametrize(
    "case",
    _VECTORS["urls"],
    ids=lambda case: f"{case['url']!r}-optin={case['allow_insecure_http']}",
)
def test_url_verdicts_match_the_shared_vectors(case):
    outcome = check_runtime_url(case["url"], allow_insecure_http=case["allow_insecure_http"])
    assert outcome.value == case["verdict"]
    # The string-returning guard agrees: usable verdicts come back, the rest are None.
    normalized = normalize_zerg_url(case["url"], allow_insecure_http=case["allow_insecure_http"])
    assert (normalized is not None) == outcome.usable


def test_the_tailscale_v4_edges_are_exact():
    assert classify_host("100.63.255.255") is HostClass.PUBLIC
    assert classify_host("100.64.0.1") is HostClass.TAILSCALE
    assert classify_host("100.127.255.255") is HostClass.TAILSCALE
    assert classify_host("100.128.0.0") is HostClass.PUBLIC


def test_a_lan_refusal_names_the_opt_in_and_a_public_one_does_not():
    lan = refusal_message("http://192.168.1.20:8080", Outcome.REFUSED_LAN)
    assert OPT_IN_FLAG in lan
    assert OPT_IN_ENV in lan
    assert "Tailscale" in lan

    public = refusal_message("http://demo.longhouse.ai", Outcome.REFUSED_PUBLIC)
    assert OPT_IN_FLAG not in public
    assert "https://" in public


def test_the_insecure_warning_is_one_line_naming_the_address_and_the_opt_in():
    warning = insecure_warning(" http://192.168.1.20:8080 ")
    assert "\n" not in warning
    assert "http://192.168.1.20:8080" in warning
    assert OPT_IN_FLAG in warning


def test_the_opt_in_is_read_from_the_environment_or_machine_state(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(OPT_IN_ENV, raising=False)
    assert get_allow_insecure_http(tmp_path) is False

    monkeypatch.setenv(OPT_IN_ENV, "1")
    assert get_allow_insecure_http(tmp_path) is True

    monkeypatch.delenv(OPT_IN_ENV)
    assert get_allow_insecure_http(tmp_path) is False
    write_machine_state(base_dir=tmp_path, written_by="test", runtime_url="http://192.168.1.20:8080", allow_insecure_http=True)
    assert get_allow_insecure_http(tmp_path, "http://192.168.1.20:8080") is True
    assert get_allow_insecure_http(tmp_path, " http://192.168.1.20:8080/ ") is True
    # The stored opt-in covers the address it was stored with, not any other.
    assert get_allow_insecure_http(tmp_path, "http://192.168.1.99:8080") is False
    assert get_allow_insecure_http(tmp_path, "http://192.168.1.20:9090") is False
    assert get_allow_insecure_http(tmp_path) is False


def test_save_zerg_url_accepts_tailscale_with_no_opt_in_and_stores_none(tmp_path: Path):
    save_zerg_url("http://100.64.0.1:8080", tmp_path)

    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.runtime_url == "http://100.64.0.1:8080"
    assert state.allow_insecure_http is False
    assert get_zerg_url(tmp_path) == "http://100.64.0.1:8080"


def test_save_zerg_url_refuses_a_lan_address_without_the_opt_in_and_keeps_it_with(tmp_path: Path):
    with pytest.raises(ValueError, match="--allow-insecure-http"):
        save_zerg_url("http://192.168.1.20:8080", tmp_path)
    assert load_machine_state(tmp_path) is None

    save_zerg_url("http://192.168.1.20:8080", tmp_path, allow_insecure_http=True)
    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.runtime_url == "http://192.168.1.20:8080"
    assert state.allow_insecure_http is True

    # A later https address does not inherit the LAN opt-in.
    save_zerg_url("https://demo.longhouse.test", tmp_path)
    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.allow_insecure_http is False


def test_a_public_http_address_is_never_saved_even_with_the_opt_in(tmp_path: Path):
    with pytest.raises(ValueError, match="Tailscale"):
        save_zerg_url("http://demo.longhouse.ai", tmp_path, allow_insecure_http=True)


def test_clearing_the_runtime_url_drops_the_opt_in_that_belonged_to_it(tmp_path: Path):
    from zerg.services.machine_state import clear_machine_runtime_url

    write_machine_state(base_dir=tmp_path, written_by="test", runtime_url="http://192.168.1.20:8080", allow_insecure_http=True)
    assert clear_machine_runtime_url(tmp_path, written_by="test") is True

    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.runtime_url is None
    assert state.allow_insecure_http is None


def test_machine_state_keeps_the_opt_in_across_unrelated_rewrites(tmp_path: Path):
    write_machine_state(base_dir=tmp_path, written_by="test", runtime_url="http://192.168.1.20:8080", allow_insecure_http=True)
    write_machine_state(base_dir=tmp_path, written_by="test", machine_name="laptop")

    state = load_machine_state(tmp_path)
    assert state is not None
    assert state.allow_insecure_http is True
    assert json.loads((tmp_path / "machine" / "state.json").read_text())["allow_insecure_http"] is True


def _ship(monkeypatch, tmp_path: Path, **kwargs):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.delenv(OPT_IN_ENV, raising=False)
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: "device-token")
    monkeypatch.setattr(connect, "get_engine_executable", lambda: "/opt/longhouse/longhouse-engine")
    monkeypatch.setattr(
        connect.subprocess,
        "run",
        lambda args, **run_kwargs: calls.append((args, run_kwargs)) or type("R", (), {"returncode": 0})(),
    )
    params = {
        "url": None,
        "token": None,
        "file": None,
        "claude_dir": None,
        "allow_insecure_http": False,
        "verbose": False,
        "quiet": False,
    }
    params.update(kwargs)
    with pytest.raises(typer.Exit) as exc:
        connect.ship(**params)
    return exc.value.exit_code, calls


def test_ship_to_a_tailscale_address_needs_no_flag(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: "http://100.64.0.1:8080")

    code, calls = _ship(monkeypatch, tmp_path)

    assert code == 0
    assert calls[0][0][3] == "http://100.64.0.1:8080"
    assert "WARNING" not in capsys.readouterr().err


def test_ship_refuses_a_lan_address_naming_the_opt_in_then_warns_when_opted_in(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)

    code, calls = _ship(monkeypatch, tmp_path, url="http://192.168.1.20:8080")
    assert code == 1
    assert calls == []
    assert OPT_IN_FLAG in capsys.readouterr().err

    code, calls = _ship(monkeypatch, tmp_path, url="http://192.168.1.20:8080", allow_insecure_http=True)
    assert code == 0
    assert calls[0][0][3] == "http://192.168.1.20:8080"
    # The engine subprocess applies the same rule, so it is told about the opt-in,
    # and it prints the warning (once per run, so not here as well).
    assert calls[0][1]["env"][OPT_IN_ENV] == "1"
    assert "WARNING" not in capsys.readouterr().err


def test_ship_uses_the_stored_opt_in_only_for_the_address_it_was_stored_with(monkeypatch, tmp_path: Path, capsys):
    write_machine_state(base_dir=tmp_path, written_by="test", runtime_url="http://192.168.1.20:8080", allow_insecure_http=True)
    monkeypatch.setattr(connect, "resolve_longhouse_home_from_provider_home", lambda claude_dir: tmp_path)

    # The stored address keeps working (the engine it runs warns).
    code, calls = _ship(monkeypatch, tmp_path, claude_dir="/unused")
    assert code == 0
    assert calls[0][0][3] == "http://192.168.1.20:8080"

    # A different LAN address handed to --url is a new decision.
    code, calls = _ship(monkeypatch, tmp_path, claude_dir="/unused", url="http://192.168.1.99:8080")
    assert code == 1
    assert calls == []
    assert OPT_IN_FLAG in capsys.readouterr().err


def test_ship_refuses_public_http_even_with_the_opt_in(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)

    code, calls = _ship(monkeypatch, tmp_path, url="http://demo.longhouse.ai", allow_insecure_http=True)

    assert code == 1
    assert calls == []
    assert "https://" in capsys.readouterr().err


def test_plain_http_bind_warning_names_the_tailscale_path_and_the_lan_opt_in(capsys):
    from zerg.cli.serve import warn_plain_http_bind

    warn_plain_http_bind("0.0.0.0", None)
    output = " ".join(capsys.readouterr().out.split())

    assert "Tailscale address" in output
    assert OPT_IN_FLAG in output
    assert "only https or loopback" not in output


def test_serve_finds_this_machines_tailscale_address(monkeypatch):
    import socket
    from types import SimpleNamespace

    import psutil

    from zerg.cli.serve import _get_tailscale_ip

    def address(family, value):
        return SimpleNamespace(family=family, address=value)

    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "en0": [address(socket.AF_INET, "192.168.1.20")],
            "lo0": [address(socket.AF_INET, "127.0.0.1")],
            "utun4": [address(socket.AF_INET6, "fd7a:115c:a1e0::1"), address(socket.AF_INET, "100.94.86.85")],
        },
    )
    assert _get_tailscale_ip() == "100.94.86.85"

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"en0": [address(socket.AF_INET, "192.168.1.20")]})
    assert _get_tailscale_ip() is None


def test_the_two_swift_copies_of_the_rule_are_identical():
    root = Path(__file__).parents[2]
    ios = (root / "ios/Sources/Shared/Auth/PlaintextHTTP.swift").read_text()
    desktop = (root / "desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/PlaintextHTTP.swift").read_text()
    assert ios == desktop


def _expect_refusal(callable_, capsys, *needles):
    with pytest.raises(typer.Exit) as exc:
        callable_()
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    for needle in needles:
        assert needle in err, err


def test_the_cli_commands_that_send_a_token_apply_the_rule(monkeypatch, tmp_path: Path, capsys):
    from zerg.cli._common import load_api_credentials
    from zerg.cli.plaintext_guard import enforce_plaintext_rule

    monkeypatch.delenv(OPT_IN_ENV, raising=False)

    def credentials(url):
        return load_api_credentials(
            url=url, token="device-token", config_dir=tmp_path, resolve_url=lambda _dir: None, resolve_token=lambda _dir: None
        )

    # Loopback, Tailscale and https pass untouched.
    for allowed in ("http://127.0.0.1:8080", "http://100.64.0.1:8080", "https://demo.longhouse.ai"):
        assert credentials(allowed) == (allowed, "device-token")
    assert capsys.readouterr().err == ""

    # A LAN address is refused with the reason and how to opt in for a command with no flag.
    _expect_refusal(
        lambda: credentials("http://192.168.1.20:8080"), capsys, "Refusing plaintext", OPT_IN_ENV, "longhouse auth --allow-insecure-http"
    )
    # A public address is refused, opt-in or not.
    monkeypatch.setenv(OPT_IN_ENV, "1")
    _expect_refusal(lambda: credentials("http://demo.longhouse.ai"), capsys, "Refusing plaintext", "https://")
    # An opted-in LAN address works and warns on stderr each time.
    for _ in range(2):
        assert credentials("http://192.168.1.20:8080")[0] == "http://192.168.1.20:8080"
        assert capsys.readouterr().err.count("WARNING") == 1
    # A value the rule cannot parse is left to the request to fail on, as before.
    enforce_plaintext_rule("not a url")
    monkeypatch.delenv(OPT_IN_ENV)

    # The stored opt-in covers only its own address.
    write_machine_state(base_dir=tmp_path, written_by="test", runtime_url="http://192.168.1.20:8080", allow_insecure_http=True)
    enforce_plaintext_rule("http://192.168.1.20:8080", tmp_path)
    assert "WARNING" in capsys.readouterr().err
    _expect_refusal(lambda: enforce_plaintext_rule("http://192.168.1.99:8080", tmp_path), capsys, "Refusing plaintext")


def test_mcp_server_and_recall_refuse_a_cleartext_address_before_sending_the_token(monkeypatch, tmp_path: Path, capsys):
    from zerg.cli import mcp_serve

    monkeypatch.delenv(OPT_IN_ENV, raising=False)
    monkeypatch.setattr(mcp_serve, "load_token", lambda *args, **kwargs: "device-token")

    _expect_refusal(
        lambda: mcp_serve.mcp_server(url="http://demo.longhouse.ai", token=None, transport="stdio", port=8001), capsys, "Refusing plaintext"
    )

    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: "device-token")
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: "http://192.168.1.20:8080")
    sent = []
    monkeypatch.setattr(connect.httpx, "Client", lambda *args, **kwargs: sent.append(args) or None)
    _expect_refusal(
        lambda: connect.recall(
            query="x", project=None, provider=None, days_back=7, limit=5, output_json=False, url=None, token=None, claude_dir=None
        ),
        capsys,
        "Refusing plaintext",
        OPT_IN_FLAG,
    )
    assert sent == []
