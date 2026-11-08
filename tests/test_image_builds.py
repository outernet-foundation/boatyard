import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from boatyard.models.config_file import ConfigFile
from boatyard.models.image import Image


def write_dockerfile(tmp_path: Path, image_name: str, text: str, top: str = "workloads") -> None:
    directory = tmp_path / top / image_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def _derive(
    tmp_path: Path,
    base_images: dict[str, Any] | None = None,
    local: bool = False,
) -> dict[str, Image]:
    score = tmp_path / "stack" / "score"
    score.mkdir(parents=True, exist_ok=True)
    (score / "gpu.yaml").write_text("metadata:\n  name: gpu\ncontainers: {}\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "requires": ">=0",
        "project_name": "demo",
        "repository": "ghcr.io/example",
        "platforms": ["linux/amd64"],
        "base_images": {
            "UV_IMAGE": "ghcr.io/org/uv:1",
            "PY_IMAGE": "docker.io/library/python:3.13-slim",
            **(base_images or {}),
        },
        "services": (
            {"gpu": {"workloads": ["gpu.yaml"]}}
            if local
            else {"ext": {"ref": "oci://ghcr.io/org/ext:0.1.0"}}
        ),
    }
    (tmp_path / "boatyard.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    loaded = ConfigFile.load(tmp_path / "boatyard.yml")
    assert loaded is not None
    return loaded.images


NEURAL_BASE: dict[str, Any] = {"dockerfile": "workloads/neural-networks-base/Dockerfile"}


def _neural_entry(axis: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {**NEURAL_BASE, **({"axis": axis} if axis is not None else {})}


def _image_builds(*dockerfiles: str) -> dict[str, Image]:
    return {
        PurePosixPath(dockerfile).parent.name: Image(name=PurePosixPath(dockerfile).parent.name, dockerfile=dockerfile)
        for dockerfile in dockerfiles
    }


def _compute_shas(images: dict[str, Image], repo: Path) -> dict[str, str]:
    return {image.sha_env: image.compute_sha(repo) for image in images.values()}


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / ".dockerignore").write_text("*\n!workloads/\n!packages/\n")
    (tmp_path / "workloads" / "app").mkdir(parents=True)
    (tmp_path / "workloads" / "app" / "main.py").write_text("print('hello')")
    (tmp_path / "workloads" / "worker").mkdir(parents=True)
    (tmp_path / "workloads" / "worker" / "run.py").write_text("print('work')")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "lib.py").write_text("x = 1")
    (tmp_path / "README.md").write_text("ignored")
    _commit_all(tmp_path)
    return tmp_path


class TestDeriveImages:
    def test_dockerfile_dirs_derive_bare_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "database-manager", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")

        images = _derive(tmp_path)

        # The image name IS the directory name — the old manifest's create-database key
        # renames to database-manager under directory-derived naming.
        assert sorted(images) == ["api", "database-manager"]
        assert [target for target, _variant in images["api"].target_variants] == ["api"]
        assert images["api"].dockerfile == "workloads/api/Dockerfile"
        assert images["api"].contexts == []

    def test_a_built_base_from_declares_a_base_edge(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(
            tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE} AS dev\nARG LOCALIZER_SHA\n"
        )

        images = _derive(tmp_path, {"NEURAL_NETWORKS_BASE": NEURAL_BASE})

        assert images["localizer"].bases == {"NEURAL_NETWORKS_BASE": "neural-networks-base"}
        assert images["localizer"].contexts == ["neural-networks-base"]
        assert images["localizer"].sha_args == ["LOCALIZER_SHA"]

    def test_an_owned_image_key_resolves_a_from(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n")

        images = _derive(tmp_path, local=True)

        assert sorted(images) == ["api"]

    def test_stage_names_are_not_context_edges(self, tmp_path: Path) -> None:
        write_dockerfile(
            tmp_path,
            "api",
            "ARG UV_IMAGE\nFROM ${UV_IMAGE} AS base\nFROM base AS dev\nFROM base AS prod\n",
        )

        images = _derive(tmp_path)

        assert images["api"].contexts == []

    def test_a_bare_name_from_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM python\n")

        with pytest.raises(ValueError, match=r"FROM python must be a \$\{KEY\} reference"):
            _derive(tmp_path)

    def test_a_from_key_naming_no_base_images_entry_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG GHOST_IMAGE\nFROM ${GHOST_IMAGE}\n")

        with pytest.raises(ValueError, match=r"FROM \$\{GHOST_IMAGE\} names no base_images key"):
            _derive(tmp_path)

    def test_literal_from_refuses_naming_base_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "migrator", "FROM golang:1.22-alpine AS build\nFROM ghcr.io/org/base:1\n")

        with pytest.raises(ValueError, match=r"FROM golang:1\.22-alpine must be a \$\{KEY\} reference"):
            _derive(tmp_path)

    def test_registry_path_with_an_arg_suffix_still_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM ghcr.io/org/uv${UV_BASE_DIGEST}\n")

        with pytest.raises(ValueError, match=r"FROM ghcr\.io/org/uv\$\{UV_BASE_DIGEST\} must be a"):
            _derive(tmp_path)

    def test_literal_copy_from_refuses_naming_base_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG PY_IMAGE\nFROM ${PY_IMAGE}\nCOPY --from=ghcr.io/org/uv:1 /uv /bin/\n")

        with pytest.raises(ValueError, match=r"COPY --from=ghcr\.io/org/uv:1 must reference a base_images build arg"):
            _derive(tmp_path)

    def test_stage_and_build_arg_copy_from_pass(self, tmp_path: Path) -> None:
        write_dockerfile(
            tmp_path,
            "api",
            "ARG PY_IMAGE\nFROM ${PY_IMAGE} AS base\nCOPY --from=base /x /y\nCOPY --from=${UV_IMAGE} /uv /bin/\n",
        )

        images = _derive(tmp_path)

        assert images["api"].contexts == []

    def test_a_declared_axis_fans_the_built_base(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        images = _derive(tmp_path, {"NEURAL_NETWORKS_BASE": _neural_entry({"TORCH_DEVICE": ["cuda", "rocm"]})})

        assert images["neural-networks-base"].axis == "TORCH_DEVICE"
        assert images["neural-networks-base"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in images["neural-networks-base"].target_variants] == [
            "neural-networks-base-cuda",
            "neural-networks-base-rocm",
        ]

    def test_an_undeclared_axis_arg_stays_plain(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        images = _derive(tmp_path, {"NEURAL_NETWORKS_BASE": NEURAL_BASE})

        assert images["neural-networks-base"].axis is None
        assert images["neural-networks-base"].variants == []
        assert [target for target, _variant in images["neural-networks-base"].target_variants] == [
            "neural-networks-base"
        ]

    def test_declared_values_keep_their_declared_order(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        images = _derive(tmp_path, {"NEURAL_NETWORKS_BASE": _neural_entry({"TORCH_DEVICE": ["rocm", "cuda"]})})

        assert images["localizer"].variants == ["rocm", "cuda"]
        assert [target for target, _variant in images["localizer"].target_variants] == [
            "localizer-rocm",
            "localizer-cuda",
        ]

    def test_the_axis_is_inherited_one_hop(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        images = _derive(tmp_path, {"NEURAL_NETWORKS_BASE": _neural_entry({"TORCH_DEVICE": ["cuda", "rocm"]})})

        assert images["localizer"].axis == "TORCH_DEVICE"
        assert images["localizer"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in images["localizer"].target_variants] == [
            "localizer-cuda",
            "localizer-rocm",
        ]

    def test_sha_arg_naming_no_image_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG GHOST_SHA\n")

        with pytest.raises(ValueError, match="ARG GHOST_SHA names no image directory"):
            _derive(tmp_path)

    def test_dockerfiles_spanning_multiple_top_level_dirs_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "bridge", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n", top="docker")

        with pytest.raises(ValueError, match="span multiple top-level directories"):
            _derive(tmp_path)


class TestBuiltBaseRefusals:
    def test_a_built_entry_with_a_missing_dockerfile_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"base_images\.NEURAL_NETWORKS_BASE: .*does not exist"):
            _derive(tmp_path, {"NEURAL_NETWORKS_BASE": NEURAL_BASE})

    def test_a_dead_built_entry_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")

        with pytest.raises(ValueError, match=r"base_images\.NEURAL_NETWORKS_BASE: no Dockerfile FROMs"):
            _derive(tmp_path, {"NEURAL_NETWORKS_BASE": NEURAL_BASE})

    def test_a_declared_axis_without_a_matching_arg_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        with pytest.raises(ValueError, match=r"axis TORCH_DEVICE names no ARG"):
            _derive(tmp_path, {"NEURAL_NETWORKS_BASE": _neural_entry({"TORCH_DEVICE": ["cuda"]})})

    def test_two_entries_declaring_one_axis_with_different_values_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")
        write_dockerfile(tmp_path, "tools-base", "ARG UV_IMAGE\nARG TORCH_DEVICE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "tools", "ARG TOOLS_BASE\nFROM ${TOOLS_BASE}\n")

        with pytest.raises(ValueError, match="already declares"):
            _derive(
                tmp_path,
                {
                    "NEURAL_NETWORKS_BASE": _neural_entry({"TORCH_DEVICE": ["cuda", "rocm"]}),
                    "TOOLS_BASE": {"dockerfile": "workloads/tools-base/Dockerfile", "axis": {"TORCH_DEVICE": ["cuda"]}},
                },
            )

    def test_a_built_base_on_a_built_base_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "tools-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "app-base", "ARG TOOLS_BASE\nFROM ${TOOLS_BASE}\n")
        write_dockerfile(tmp_path, "app", "ARG APP_BASE\nFROM ${APP_BASE}\n")

        with pytest.raises(ValueError, match="a built base must not FROM the built base"):
            _derive(
                tmp_path,
                {
                    "TOOLS_BASE": {"dockerfile": "workloads/tools-base/Dockerfile"},
                    "APP_BASE": {"dockerfile": "workloads/app-base/Dockerfile"},
                },
            )

    def test_a_dockerfile_froming_two_built_bases_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "tools-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "app-base", "ARG PY_IMAGE\nFROM ${PY_IMAGE}\n")
        write_dockerfile(tmp_path, "app", "ARG TOOLS_BASE\nARG APP_BASE\nFROM ${TOOLS_BASE}\nFROM ${APP_BASE}\n")

        with pytest.raises(ValueError, match="FROMs more than one built base"):
            _derive(
                tmp_path,
                {
                    "TOOLS_BASE": {"dockerfile": "workloads/tools-base/Dockerfile"},
                    "APP_BASE": {"dockerfile": "workloads/app-base/Dockerfile"},
                },
            )


class TestComputeImageShasZeroImages:
    def test_an_imageless_repo_walks_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        def _refuse_the_walk(_self: Image, _root: Path) -> str:
            raise AssertionError("zero images must not walk the tree")

        monkeypatch.setattr(Image, "compute_sha", _refuse_the_walk)

        assert _compute_shas(_image_builds(), tmp_path) == {}


class TestComputeImageShas:
    def test_should_return_deterministic_hashes(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas1 = _compute_shas(images, repo)
        shas2 = _compute_shas(images, repo)

        assert shas1 == shas2

    def test_should_have_tree_prefix(self, repo: Path):
        shas = _compute_shas(_image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile"), repo)

        for sha in shas.values():
            assert sha.startswith("tree-")

    def test_should_return_only_tagged_images(self, repo: Path):
        shas = _compute_shas(_image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile"), repo)

        assert set(shas.keys()) == {"APP_SHA", "WORKER_SHA"}

    def test_should_change_only_affected_image_when_image_file_changes(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "workloads" / "app" / "main.py").write_text("print('changed')")
        _commit_all(repo)

        shas_after = _compute_shas(images, repo)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_should_change_all_images_when_shared_file_changes(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "packages" / "lib.py").write_text("x = 2")
        _commit_all(repo)

        shas_after = _compute_shas(images, repo)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] != shas_after["WORKER_SHA"]

    def test_should_not_change_when_ignored_file_changes(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "README.md").write_text("changed readme")
        _commit_all(repo)

        shas_after = _compute_shas(images, repo)
        assert shas_before == shas_after

    def test_should_not_change_when_untagged_workload_file_changes(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "workloads" / "loki").mkdir()
        (repo / "workloads" / "loki" / "config.yaml").write_text("unused by tagged images")
        _commit_all(repo)

        shas_after = _compute_shas(images, repo)
        assert shas_before == shas_after

    def test_should_rekey_only_the_affected_image_on_a_dirty_edit(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "workloads" / "app" / "main.py").write_text("print('uncommitted')")

        shas_after = _compute_shas(images, repo)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_should_rekey_on_an_untracked_context_visible_file(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        (repo / "workloads" / "app" / "new_module.py").write_text("print('untracked')")

        shas_after = _compute_shas(images, repo)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_ignored_files_under_the_allowlist_never_hash(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        (repo / ".gitignore").write_text("__pycache__/\n")
        shas_before = _compute_shas(images, repo)

        (repo / "workloads" / "app" / "__pycache__").mkdir()
        (repo / "workloads" / "app" / "__pycache__" / "main.cpython-313.pyc").write_text("machine-local bytes")

        assert _compute_shas(images, repo) == shas_before

    def test_staged_and_unstaged_edits_hash_identically(self, repo: Path):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        (repo / "workloads" / "app" / "main.py").write_text("print('edited')")
        shas_unstaged = _compute_shas(images, repo)

        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True)

        assert _compute_shas(images, repo) == shas_unstaged

    def test_global_excludes_do_not_affect_visibility(self, repo: Path, tmp_path_factory: pytest.TempPathFactory):
        images = _image_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = _compute_shas(images, repo)

        global_ignore = tmp_path_factory.mktemp("global-excludes") / "gitignore"
        global_ignore.write_text("main.py\n")
        subprocess.run(
            ["git", "config", "core.excludesFile", str(global_ignore)],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        assert _compute_shas(images, repo) == shas_before


class TestComputeImageShasGrammar:
    def test_should_refuse_a_bare_pattern(self, tmp_path: Path):
        (tmp_path / ".dockerignore").write_text("*\n!workloads/\n__pycache__\n")

        with pytest.raises(ValueError, match="only allowlists are supported"):
            _compute_shas(_image_builds("workloads/app/Dockerfile"), tmp_path)

    def test_should_refuse_a_wildcard_re_include(self, tmp_path: Path):
        (tmp_path / ".dockerignore").write_text("*\n!workloads/*\n")

        with pytest.raises(ValueError, match="literal paths without wildcards"):
            _compute_shas(_image_builds("workloads/app/Dockerfile"), tmp_path)

    def test_should_refuse_a_missing_star(self, tmp_path: Path):
        (tmp_path / ".dockerignore").write_text("!workloads/\n")

        with pytest.raises(ValueError, match=r"exactly one '\*' line"):
            _compute_shas(_image_builds("workloads/app/Dockerfile"), tmp_path)

    def test_should_refuse_a_second_star(self, tmp_path: Path):
        (tmp_path / ".dockerignore").write_text("*\n*\n!workloads/\n")

        with pytest.raises(ValueError, match=r"exactly one '\*' line"):
            _compute_shas(_image_builds("workloads/app/Dockerfile"), tmp_path)


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class TestComputeImageShasContextGolden:
    pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker daemon required")

    def test_the_hashed_file_set_is_the_git_visible_build_context(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / ".dockerignore").write_text("*\n!workloads/app/\n")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        app = tmp_path / "workloads" / "app"
        app.mkdir(parents=True)
        (app / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
        (app / "main.py").write_text("print('hello')\n")
        helper = app / "helper.py"
        helper.write_text("x = 1\n")
        helper.chmod(0o755)
        (app / "link.py").symlink_to("helper.py")
        (app / "__pycache__").mkdir()
        (app / "__pycache__" / "main.cpython-313.pyc").write_text("machine-local bytes")

        images = _image_builds("workloads/app/Dockerfile")
        shas = _compute_shas(images, tmp_path)
        tree_hash = shas["APP_SHA"].removeprefix("tree-")
        listed = subprocess.run(
            ["git", "ls-tree", "-r", tree_hash],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.splitlines()
        hashed: dict[str, str] = {}
        for line in listed:
            meta, path = line.split("\t", 1)
            hashed[path] = meta.split()[0]

        # The oracle is the exported context: FROM scratch + COPY . / makes buildx write out exactly what it ships.
        # The walk is deliberately narrower: an ignored file rides the context but never the tree tag.
        builder = f"boatyard-golden-{os.getpid()}"
        create = subprocess.run(["docker", "buildx", "create", "--name", builder, "--use"], capture_output=True)
        if create.returncode != 0:
            subprocess.run(["docker", "buildx", "use", builder], capture_output=True, check=True)
        try:
            export = tmp_path / "export"
            command = [
                "docker",
                "buildx",
                "build",
                "-f",
                "workloads/app/Dockerfile",
                "--output",
                f"type=local,dest={export}",
                ".",
            ]
            subprocess.run(command, cwd=str(tmp_path), capture_output=True, check=True, timeout=300)
        finally:
            subprocess.run(["docker", "buildx", "rm", builder], capture_output=True, check=False)

        exported: dict[str, str] = {}
        for directory, _dirnames, filenames in os.walk(export):
            for name in filenames:
                full = Path(directory) / name
                relative = full.relative_to(export).as_posix()
                if full.is_symlink():
                    exported[relative] = "120000"
                elif full.stat().st_mode & 0o111:
                    exported[relative] = "100755"
                else:
                    exported[relative] = "100644"

        ignored_export = "workloads/app/__pycache__/main.cpython-313.pyc"
        assert ignored_export in exported
        assert ignored_export not in hashed
        exported.pop(ignored_export)
        assert exported == hashed
