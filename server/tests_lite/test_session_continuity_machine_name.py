from zerg.services import session_continuity


def test_get_machine_name_label_prefers_saved_machine_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "longhouse-machine-name").write_text("work-laptop\n")

    assert session_continuity.get_machine_name_label() == "work-laptop"


def test_get_machine_name_label_falls_back_to_hostname(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(session_continuity.platform, "node", lambda: "zerg")

    assert session_continuity.get_machine_name_label() == "zerg"
