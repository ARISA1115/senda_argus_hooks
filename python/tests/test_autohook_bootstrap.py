from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from senda_argus_hooks.autoinstall import install


def test_pth_bootstrap_runs_in_fresh_python(tmp_path: Path):
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    install(target=str(site_dir))
    event_file = tmp_path / "events.jsonl"
    script = tmp_path / "check.py"
    script.write_text(
        "import senda_argus_hooks.autohook as a; print('bootstrapped=' + str(a.is_bootstrapped()))\n",
        encoding="utf-8",
    )
    # Python only processes .pth files in recognized site directories, so create
    # an isolated venv-like layout by using site.addsitedir before the check.
    runner = tmp_path / "runner.py"
    runner.write_text(
        f"import site; site.addsitedir({str(site_dir)!r}); exec(open({str(script)!r}).read())\n",
        encoding="utf-8",
    )
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "SENDA_ARGUS_EXPORTER": "jsonl",
        "SENDA_ARGUS_JSONL_PATH": str(event_file),
        "SENDA_ARGUS_BOOTSTRAP_DEBUG": "0",
    }
    completed = subprocess.run([sys.executable, str(runner)], text=True, capture_output=True, env=env, check=True)
    assert "bootstrapped=True" in completed.stdout
