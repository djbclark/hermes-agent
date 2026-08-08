import subprocess
import sys


def test_installed_console_entrypoint_exposes_memory_journal():
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "memory-journal", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "memory-journal" in result.stdout
    assert "status" in result.stdout
