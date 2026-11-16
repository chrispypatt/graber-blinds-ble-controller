import json

from graber.diagnostic_cli import main


def test_report_includes_rotation_and_honors_time_boundaries(tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    path.with_suffix(".jsonl.1").write_text(
        json.dumps({"event": "command_completed", "recorded_at": 100,
                    "blind_id": "old", "outcome": "completed"}) + "\n"
    )
    path.write_text(
        json.dumps({"event": "command_completed", "recorded_at": 200,
                    "blind_id": "new", "outcome": "completed"}) + "\n"
    )
    assert main(["report", str(path), "--since", "150"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["period"]["records"] == 1
    assert set(report["by_blind"]) == {"new"}


def test_report_can_exclude_rotated_file(tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    path.with_suffix(".jsonl.1").write_text(
        json.dumps({"event": "command_completed", "recorded_at": 100,
                    "blind_id": "old", "outcome": "completed"}) + "\n"
    )
    path.write_text(
        json.dumps({"event": "command_completed", "recorded_at": 200,
                    "blind_id": "new", "outcome": "completed"}) + "\n"
    )
    assert main(["report", str(path), "--no-rotated"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["period"]["records"] == 1

