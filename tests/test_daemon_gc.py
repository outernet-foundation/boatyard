from pathlib import Path

import pytest
from pydantic import ValidationError

from docker_devkit.bake import MIN_KEEP_STORAGE_GB, check_gc_limits


def _daemon_json(root: Path, body: str) -> Path:
    path = root / "daemon.json"
    path.write_text(body, encoding="utf-8")
    return path


def test_passes_at_the_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": "60GB"}}}')
    check_gc_limits(candidates=[config])
    output = capsys.readouterr().out
    assert "[OK]" in output
    assert "60.0GB" in output


def test_refuses_below_the_floor(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": "50GB"}}}')
    with pytest.raises(RuntimeError, match="below the required"):
        check_gc_limits(candidates=[config])


def test_converts_units_before_the_threshold(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": "61440MB"}}}')
    check_gc_limits(candidates=[config])


def test_accepts_byte_counts_and_binary_units(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": 107374182400}}}')
    check_gc_limits(candidates=[config])
    config.write_text('{"builder": {"gc": {"defaultKeepStorage": "1TiB"}}}', encoding="utf-8")
    check_gc_limits(candidates=[config])


def test_missing_key_refuses_with_sample(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"registry-mirrors": ["https://mirror.example"]}')
    with pytest.raises(RuntimeError, match="defaultKeepStorage"):
        check_gc_limits(candidates=[config])


def test_zero_is_a_set_value_and_refuses_below_the_floor(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": 0}}}')
    with pytest.raises(RuntimeError, match="below the required"):
        check_gc_limits(candidates=[config])
    config.write_text('{"builder": {"gc": {"defaultKeepStorage": "0GB"}}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="below the required"):
        check_gc_limits(candidates=[config])


def test_null_counts_as_missing(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": null}}}')
    with pytest.raises(RuntimeError, match="defaultKeepStorage"):
        check_gc_limits(candidates=[config])


def test_unparseable_value_refuses(tmp_path: Path) -> None:
    config = _daemon_json(tmp_path, '{"builder": {"gc": {"defaultKeepStorage": "lots"}}}')
    with pytest.raises(ValidationError, match="must be a number"):
        check_gc_limits(candidates=[config])


def test_no_readable_file_warns_and_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    check_gc_limits(candidates=[tmp_path / "missing.json"])
    assert "[WARN]" in capsys.readouterr().out


def test_first_readable_candidate_wins(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    first.write_text('{"builder": {"gc": {"defaultKeepStorage": "50GB"}}}', encoding="utf-8")
    second = tmp_path / "second.json"
    second.write_text('{"builder": {"gc": {"defaultKeepStorage": "100GB"}}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match=str(first)):
        check_gc_limits(candidates=[first, second])


def test_floor_is_sixty_gb() -> None:
    assert MIN_KEEP_STORAGE_GB == 60
