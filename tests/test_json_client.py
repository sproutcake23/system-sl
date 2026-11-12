"""Focused tests for the data-file load statuses."""

import builtins
import json

from system_sl.utils.atomic_json import LoadStatus
from system_sl.utils.data_health import pop_issues
from system_sl.utils.json_client import load_data, load_data_result


def _reset_issues():
    pop_issues()


def test_missing_file_reports_missing(tmp_path):
    _reset_issues()
    missing = tmp_path / "tasks.json"

    result = load_data_result(str(missing))

    assert result.status is LoadStatus.MISSING
    assert result.data is None
    assert not result.ok
    assert load_data(str(missing)) == {}
    assert pop_issues() == []


def test_empty_file_reports_empty(tmp_path):
    _reset_issues()
    empty = tmp_path / "tasks.json"
    empty.write_text("   \n", encoding="utf-8")

    result = load_data_result(str(empty))

    assert result.status is LoadStatus.EMPTY
    assert result.data is None
    assert load_data(str(empty)) == {}


def test_valid_file_reports_ok(tmp_path):
    _reset_issues()
    path = tmp_path / "tasks.json"
    payload = [{"title": "Write tests"}]
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_data_result(str(path))

    assert result.status is LoadStatus.OK
    assert result.ok
    assert result.data == payload
    assert load_data(str(path)) == payload
    assert pop_issues() == []


def test_corrupt_file_is_quarantined_and_reported(tmp_path):
    _reset_issues()
    path = tmp_path / "tasks.json"
    path.write_text("{not json", encoding="utf-8")

    result = load_data_result(str(path))

    assert result.status is LoadStatus.CORRUPT
    assert result.data is None
    assert result.corrupt_path == tmp_path / "tasks.json.corrupt"
    assert result.corrupt_path.is_file()
    assert not path.exists()
    assert load_data(str(path)) == {}

    issues = pop_issues()
    assert len(issues) == 1
    assert issues[0].kind == "corrupt"
    assert issues[0].path == path
    assert issues[0].corrupt_path == result.corrupt_path
    assert str(result.corrupt_path) in issues[0].message()


def test_unreadable_file_reports_unreadable(tmp_path, monkeypatch):
    _reset_issues()
    path = tmp_path / "tasks.json"
    path.write_text("{}", encoding="utf-8")

    real_open = builtins.open

    def deny(target, *args, **kwargs):
        if str(target) == str(path):
            raise PermissionError("permission denied")
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny)
    result = load_data_result(str(path))

    assert result.status is LoadStatus.UNREADABLE
    assert result.error == "permission denied"

    issues = pop_issues()
    assert [issue.kind for issue in issues] == ["unreadable"]
    assert load_data(str(path)) == {}
