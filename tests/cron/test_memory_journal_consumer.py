from unittest.mock import patch

from cron.scripts import memory_journal_consumer


def test_main_is_silent_when_nothing_processed(capsys):
    with patch.object(
        memory_journal_consumer.memory_projection,
        "run_once",
        return_value={"processed": 0, "counts": {}, "results": []},
    ):
        assert memory_journal_consumer.main() == 0
    assert capsys.readouterr().out == ""


def test_main_reports_processing_and_failures(capsys):
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
    ):
        assert memory_journal_consumer.main() == 0
    output = capsys.readouterr().out
    assert "processed 2 record(s)" in output
    assert "done=1 failed=1" in output
    assert "r2 failed: conflict" in output
