from pathlib import Path
from unittest.mock import patch

from cron.scripts import memory_metrics_report


def test_build_report_is_bounded_and_deterministic(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("alpha fact\nalpha fact\n§\n")
    (memories / "USER.md").write_text("user fact\n")

    with patch.object(memory_metrics_report.memory_projection, "get_status", return_value={"behind": False}), patch.object(
        memory_metrics_report.pq, "list_evictions", return_value=[{"id": 1}]
    ), patch.object(memory_metrics_report.pq, "list_all", return_value=[]):
        report = memory_metrics_report.build_report(tmp_path)

    assert report["memory_entry_count"] == 2
    assert report["evictions_recorded"] == 1
    assert report["dead_letters_recorded"] == 0
    assert report["duplicate_candidate_pairs"] == 1
    assert report["journal_status"] == {"behind": False}
