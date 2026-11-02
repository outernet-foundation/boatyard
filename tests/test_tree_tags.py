import subprocess
from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from docker_devkit.verbs.tree_tags import app, tree_tags

runner = CliRunner()


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], cwd=str(path), capture_output=True, check=True)


def _write_service(root: Path, name: str, text: str = "ARG PY_IMAGE\nFROM ${PY_IMAGE}\n") -> None:
    directory = root / "workloads" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def _tags(repo: Path, capfd: pytest.CaptureFixture[str], *services: str) -> list[str]:
    # Direct command calls: the git hashing underneath shells out through bashrun, whose
    # inherited stdout has no fileno under the CliRunner's capture.
    tree_tags(services=list(services) or None)
    return capfd.readouterr().out.splitlines()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / ".dockerignore").write_text("*\n!workloads/\n", encoding="utf-8")
    (tmp_path / "docker-devkit.yaml").write_text(
        yaml.safe_dump({"requires": ">=0", "repository": "ghcr.io/org/demo"}), encoding="utf-8"
    )
    _write_service(tmp_path, "api")
    _write_service(tmp_path, "worker")
    _commit_all(tmp_path)
    return tmp_path


class TestTreeTags:
    def test_answers_one_bare_tag_per_line_in_argument_order(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo)

        lines = _tags(repo, capfd, "worker", "api")

        assert len(lines) == 2
        assert all(line.startswith("tree-") and " " not in line for line in lines)
        assert lines[0] != lines[1]

    def test_the_tags_track_head(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo)
        before = _tags(repo, capfd, "api", "worker")

        (repo / "workloads" / "api" / "main.py").write_text("print('changed')\n", encoding="utf-8")
        _commit_all(repo)
        after = _tags(repo, capfd, "api", "worker")

        assert after[0] != before[0]
        assert after[1] == before[1]

    def test_uncommitted_edits_do_not_move_the_tags(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo)
        before = _tags(repo, capfd, "api")

        (repo / "workloads" / "api" / "main.py").write_text("print('uncommitted')\n", encoding="utf-8")
        after = _tags(repo, capfd, "api")

        assert after == before

    def test_unknown_names_refuse_listing_the_vocabulary(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(repo)

        with pytest.raises(typer.BadParameter, match=r"unknown service\(s\) \['ghost'\].*\['api', 'worker'\]"):
            tree_tags(services=["api", "ghost"])

    def test_a_bare_invocation_refuses_listing_the_vocabulary(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo)

        with pytest.raises(typer.BadParameter, match=r"name at least one service.*\['api', 'worker'\]"):
            tree_tags(services=None)

    def test_a_repo_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["api"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "no docker-devkit.yaml" in str(result.exception)
