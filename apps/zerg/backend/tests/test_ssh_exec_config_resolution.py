from pathlib import Path

from zerg.tools.builtin.ssh_tools import _resolve_ssh_target


def test_resolve_ssh_alias_uses_config_and_identity_file(tmp_path: Path):
    home_dir = tmp_path / "home"
    ssh_dir = home_dir / ".ssh"
    ssh_dir.mkdir(parents=True)

    # Default identity + alias mapping
    (ssh_dir / "config").write_text(
        "\n".join(
            [
                "Host *",
                "  IdentityFile ~/.ssh/rosetta",
                "",
                "Host cube",
                "  HostName 100.104.187.47",
                "  Port 2222",
                "  User drose",
                "",
            ]
        )
    )

    (ssh_dir / "rosetta").write_text("dummy")

    resolved = _resolve_ssh_target("cube", home_dir=home_dir)
    assert resolved is not None
    user, hostname, port, identity = resolved
    assert user == "drose"
    assert hostname == "100.104.187.47"
    assert port == "2222"
    assert identity == ssh_dir / "rosetta"


def test_resolve_explicit_host_still_uses_default_identity_file(tmp_path: Path):
    home_dir = tmp_path / "home"
    ssh_dir = home_dir / ".ssh"
    ssh_dir.mkdir(parents=True)

    (ssh_dir / "config").write_text(
        "\n".join(
            [
                "Host *",
                "  IdentityFile ~/.ssh/rosetta",
                "",
            ]
        )
    )
    (ssh_dir / "rosetta").write_text("dummy")

    resolved = _resolve_ssh_target("drose@100.104.187.47:2222", home_dir=home_dir)
    assert resolved is not None
    user, hostname, port, identity = resolved
    assert user == "drose"
    assert hostname == "100.104.187.47"
    assert port == "2222"
    assert identity == ssh_dir / "rosetta"
