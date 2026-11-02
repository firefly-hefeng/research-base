from pathlib import Path
import json

import pytest
import yaml

from research_base import integration as module
from research_base.integration import attach_project, detach_project, doctor_project
from research_base.models import ResearchError


def attach(root: Path, **kwargs):
    return attach_project(root, "lab-default", "library-one", "collection-one", **kwargs)


def test_attach_is_idempotent_and_detach_preserves_original_bytes(tmp_path):
    original = b"\xef\xbb\xbf# Existing rules\r\n\r\n<!-- vesti:start -->\r\nKeep me\r\n<!-- vesti:end -->\r\n"
    entry = tmp_path / "AGENTS.md"
    entry.write_bytes(original)
    assert attach(tmp_path)["status"] == "updated"
    installed = entry.read_bytes()
    assert installed.startswith(original)
    assert b"\r\n" in installed
    assert installed.count(module.START) == 1
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert attach(tmp_path)["status"] == "unchanged"
    assert snapshot == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert doctor_project(tmp_path)["managed_block"] == "present"
    detach_project(tmp_path)
    assert entry.read_bytes() == original
    assert detach_project(tmp_path)["status"] == "unchanged"


def test_new_entry_removed_but_user_content_after_install_is_preserved(tmp_path):
    attach(tmp_path)
    entry = tmp_path / "AGENTS.md"
    entry.write_bytes(entry.read_bytes() + b"\n# User additions\n")
    detach_project(tmp_path)
    assert entry.exists()
    assert b"# User additions" in entry.read_bytes()
    assert module.START not in entry.read_bytes()


def test_owned_entry_deleted_only_when_empty(tmp_path):
    attach(tmp_path)
    detach_project(tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".research/config.yaml").exists()


def test_no_binding_detach_does_not_create_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Existing external rules", encoding="utf-8")
    assert detach_project(tmp_path)["status"] == "unchanged"
    assert not (tmp_path / ".research").exists()


def test_dry_run_has_no_side_effects(tmp_path):
    result = attach(tmp_path, dry_run=True)
    assert result["dry_run"] is True
    assert list(tmp_path.iterdir()) == []


def test_manual_and_mode_switching(tmp_path):
    attach(tmp_path, mode="manual")
    assert not (tmp_path / "AGENTS.md").exists()
    attach(tmp_path, mode="agents")
    assert (tmp_path / "AGENTS.md").exists()
    attach(tmp_path, mode="manual")
    assert not (tmp_path / "AGENTS.md").exists()
    assert yaml.safe_load((tmp_path / ".research/config.yaml").read_text())["integration"]["mode"] == "manual"


def test_user_edited_block_conflicts_and_does_not_change_config(tmp_path):
    attach(tmp_path)
    entry = tmp_path / "AGENTS.md"
    entry.write_bytes(entry.read_bytes().replace("文献使用规则".encode(), "用户修改规则".encode()))
    previous_config = (tmp_path / ".research/config.yaml").read_bytes()
    for action in (lambda: attach(tmp_path, mode="manual"), lambda: detach_project(tmp_path)):
        with pytest.raises(ResearchError, match="edited") as error:
            action()
        assert error.value.code == "RULES_CONFLICT"
        assert (tmp_path / ".research/config.yaml").read_bytes() == previous_config


@pytest.mark.parametrize("content", [module.START, module.END, module.START + module.END + module.END])
def test_malformed_markers_never_guess_scope(tmp_path, content):
    (tmp_path / "AGENTS.md").write_bytes(content)
    with pytest.raises(ResearchError) as error:
        attach(tmp_path)
    assert error.value.code == "RULES_CONFLICT"
    assert (tmp_path / "AGENTS.md").read_bytes() == content


def test_unrelated_config_fields_and_user_edits_outside_block_survive(tmp_path):
    directory = tmp_path / ".research"
    directory.mkdir()
    (directory / "config.yaml").write_text("schema_version: 1\nresearch:\n  title: My title\ncustom:\n  setting: 7\n", encoding="utf-8")
    attach(tmp_path)
    config = yaml.safe_load((directory / "config.yaml").read_text())
    assert config["research"]["title"] == "My title"
    assert config["custom"]["setting"] == 7
    entry = tmp_path / "AGENTS.md"
    entry.write_bytes(b"User prefix\n" + entry.read_bytes())
    attach_project(tmp_path, "lab-default", "library-one", "collection-two")
    assert entry.read_bytes().startswith(b"User prefix\n")
    assert yaml.safe_load((directory / "config.yaml").read_text())["collection_id"] == "collection-two"


def test_entry_cannot_escape_project(tmp_path):
    directory = tmp_path / ".research"
    directory.mkdir()
    (directory / "config.yaml").write_text("integration:\n  entry_file: ../outside.md\n", encoding="utf-8")
    with pytest.raises(ResearchError) as error:
        attach(tmp_path)
    assert error.value.code == "PATH_OUTSIDE_PROJECT"


def test_cloned_known_template_can_be_adopted_without_local_state(tmp_path):
    (tmp_path / "AGENTS.md").write_text(module.TEMPLATE, encoding="utf-8")
    attach(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count(module.START.decode()) == 1


def test_unsupported_entry_encoding_is_not_rewritten(tmp_path):
    raw = "中文说明".encode("utf-16")
    (tmp_path / "AGENTS.md").write_bytes(raw)
    with pytest.raises(ResearchError) as error:
        attach(tmp_path)
    assert error.value.code == "UNSUPPORTED_ENCODING"
    assert (tmp_path / "AGENTS.md").read_bytes() == raw


def test_interrupted_multi_file_write_can_be_recovered(tmp_path, monkeypatch):
    original = module._atomic_write
    failures = []
    def failing_write(path, value):
        if path.name == "AGENTS.md" and not failures:
            failures.append(True)
            raise OSError("Simulated process interruption")
        original(path, value)
    monkeypatch.setattr(module, "_atomic_write", failing_write)
    with pytest.raises(OSError):
        attach(tmp_path)
    assert (tmp_path / ".research/state/pending.json").exists()
    monkeypatch.setattr(module, "_atomic_write", original)
    attach(tmp_path)
    assert not (tmp_path / ".research/state/pending.json").exists()
    assert doctor_project(tmp_path)["managed_block"] == "present"


def test_recovery_never_overwrites_later_external_changes(tmp_path, monkeypatch):
    original = module._atomic_write
    def failing_write(path, value):
        if path.name == "AGENTS.md":
            raise OSError("Simulated interruption")
        original(path, value)
    monkeypatch.setattr(module, "_atomic_write", failing_write)
    with pytest.raises(OSError):
        attach(tmp_path)
    entry = tmp_path / "AGENTS.md"
    entry.write_text("External new content", encoding="utf-8")
    monkeypatch.setattr(module, "_atomic_write", original)
    with pytest.raises(ResearchError) as error:
        attach(tmp_path)
    assert error.value.code == "RECOVERY_REQUIRED"
    assert entry.read_text() == "External new content"


def test_template_copy_matches_repository_source():
    source = Path(__file__).parents[1] / "templates/AGENTS.research.md"
    assert source.read_text(encoding="utf-8").strip() == module.TEMPLATE
