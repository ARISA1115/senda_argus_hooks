from __future__ import annotations

from pathlib import Path

from senda_argus_hooks.autoinstall import PTH_CONTENT, PTH_FILENAME, install, status, uninstall


def test_autohook_install_status_uninstall_target(tmp_path: Path):
    result = install(target=str(tmp_path))
    pth = tmp_path / PTH_FILENAME
    assert result["installed"] is True
    assert pth.read_text(encoding="utf-8") == PTH_CONTENT

    current = status(target=str(tmp_path))
    assert current["installed"] is True
    assert current["entries"][0]["managed"] is True

    removed = uninstall(target=str(tmp_path))
    assert str(pth) in removed["removed"]
    assert not pth.exists()


def test_autohook_does_not_overwrite_unknown_file_without_force(tmp_path: Path):
    pth = tmp_path / PTH_FILENAME
    pth.write_text("import something_else\n", encoding="utf-8")
    try:
        install(target=str(tmp_path))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected RuntimeError")
    assert pth.read_text(encoding="utf-8") == "import something_else\n"
