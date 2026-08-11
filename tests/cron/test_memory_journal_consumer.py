from unittest.mock import MagicMock, patch

from cron.scripts import memory_journal_consumer


def _fake_status(**overrides):
    """Minimal status dict matching memory_projection.get_status() shape."""
    return {
        "active_count": 0,
        "pending_count": 0,
        "processing_count": 0,
        "failed_count": 0,
        "dead_letter_count": 0,
        "evicted_count": 2,
        "behind": False,
        **overrides,
    }


def _fake_store(memory_entries=(), user_entries=(), memory_char_limit=2200, user_char_limit=1375):
    store = MagicMock()
    store.memory_entries = list(memory_entries)
    store.user_entries = list(user_entries)
    store.memory_char_limit = memory_char_limit
    store.user_char_limit = user_char_limit
    return store


def test_main_idle_is_silent(capsys):
    """When nothing is processed, retain metrics locally but print nothing."""
    with patch.object(
        memory_journal_consumer.memory_projection,
        "run_once",
        return_value={"processed": 0, "counts": {}, "results": []},
    ), patch.object(
        memory_journal_consumer.memory_projection,
        "get_status",
        return_value=_fake_status(),
    ), patch.object(
        memory_journal_consumer, "_append_metrics_line"
    ), patch.object(
        memory_journal_consumer, "load_on_disk_store",
        return_value=_fake_store(),
    ):
        assert memory_journal_consumer.main() == 0

    output = capsys.readouterr().out
    assert output == ""


def test_main_reports_processing_and_failures(capsys):
    """When work is done, keep the existing processing output."""
    with patch.object(
        memory_journal_consumer.memory_projection,
        "run_once",
        return_value={
            "processed": 2,
            "counts": {"done": 1, "failed": 1},
            "results": [
                {"id": "r1", "outcome": "done"},
                {"id": "r2", "outcome": "failed", "error": "conflict"},
            ],
        },
    ), patch.object(
        memory_journal_consumer.memory_projection,
        "get_status",
        return_value=_fake_status(active_count=1, failed_count=1),
    ), patch.object(
        memory_journal_consumer, "_append_metrics_line"
    ):
        assert memory_journal_consumer.main() == 0

    output = capsys.readouterr().out
    assert "processed 2 record(s)" in output
    assert "done=1 failed=1" in output
    assert "r2 failed: conflict" in output
    # The metrics summary should NOT appear when work was done
    assert "memory journal:" not in output


def test_append_metrics_line_creates_file(tmp_path, monkeypatch):
    """_append_metrics_line writes a JSON line and creates parent dirs."""
    metrics_file = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(memory_journal_consumer, "_METRICS_PATH", metrics_file)

    store = _fake_store(
        memory_entries=["knows Python", "works from home"],
        user_entries=["allergic to shellfish"],
    )
    store.memory_char_limit = 2200
    store.user_char_limit = 1375

    monkeypatch.setattr(
        memory_journal_consumer, "load_on_disk_store", lambda: store
    )

    status = _fake_status(active_count=1, pending_count=1, behind=True)
    memory_journal_consumer._append_metrics_line(status)

    assert metrics_file.exists()
    lines = metrics_file.read_text().strip().splitlines()
    assert len(lines) == 1

    import json
    data = json.loads(lines[0])
    assert data["active_count"] == 1
    assert data["pending_count"] == 1
    assert data["behind"] is True
    assert data["evicted_count"] == 2
    # memory entries: "knows Python", "works from home" joined by \n§\n
    assert data["memory_char_used"] > 0
    assert data["memory_char_limit"] == 2200
    assert data["user_char_used"] > 0
    assert data["user_char_limit"] == 1375
    assert "timestamp" in data


def test_append_metrics_line_rotates_at_100kb(tmp_path, monkeypatch):
    """When the metrics file exceeds 100KB, it is rotated to .1."""
    metrics_file = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(memory_journal_consumer, "_METRICS_PATH", metrics_file)

    store = _fake_store()
    monkeypatch.setattr(
        memory_journal_consumer, "load_on_disk_store", lambda: store
    )

    # Pre-fill the file to just over 100KB
    big_line = "x" * 500
    with open(metrics_file, "w") as f:
        for _ in range(205):  # 205 * ~500 chars = ~102,500 bytes (plus newlines)
            f.write(big_line + "\n")

    size_before = metrics_file.stat().st_size
    assert size_before >= 100_000, f"Expected {size_before} >= 100000"

    status = _fake_status()
    memory_journal_consumer._append_metrics_line(status)

    # The old content should now be in the .1 backup
    rotated = metrics_file.with_suffix(".jsonl.1")
    assert rotated.exists(), f"Expected rotated file at {rotated}"
    rotated_size = rotated.stat().st_size
    assert rotated_size >= 100_000

    # The current file should be much smaller (just one new line)
    assert metrics_file.exists()
    new_size = metrics_file.stat().st_size
    assert new_size < 5000

    # New file should contain the new status line
    lines = metrics_file.read_text().strip().splitlines()
    assert len(lines) == 1
    import json
    data = json.loads(lines[0])
    assert data["evicted_count"] == 2
