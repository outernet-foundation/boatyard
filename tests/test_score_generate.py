import os
import shutil
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from pydantic import ValidationError

from boatyard.models import config_file as config_file_module
from boatyard import score as score_module
from boatyard import score_compose as score_compose_module
from boatyard import score_k8s as score_k8s_module
from boatyard import score_generate as score_generate_module
from boatyard.verbs import score as score_verb_module
from boatyard.models import workload as workload_module
from boatyard.models.config_file import ConfigFile, K8sConfig
from boatyard.models.lock_file import LockFile
from boatyard.models.workload import LoadedWorkload, WorkloadDocument
from boatyard.models.image import Image
from boatyard.paths import K8S_PINS_PATH
from boatyard.score import write_file
from boatyard.score_compose import BindVolume, ComposeService
from boatyard.score_k8s import generate_k8s
from boatyard.yaml import Yaml

DIGEST = "sha256:" + "a" * 64
PLATFORM_DIGEST = "sha256:" + "b" * 64


def _root_config(**sections: object) -> ConfigFile:
    payload: dict[str, object] = {"requires": ">=0.1", "project_name": "example"}
    payload.update(sections)
    if "services" in payload:
        payload.setdefault("gateway_port", 8080)
        payload.setdefault("platforms", ["linux/amd64"])
    payload.setdefault("services", {"ext": {"ref": "oci://ghcr.io/org/ext:0.1.0"}})
    return ConfigFile.model_validate(payload)


def _generated_config(tmp_path: Path, workload_names: list[str], **sections: object) -> ConfigFile:
    # The generate-time companion to ConfigFile.load for fixtures that inject images or
    # lock edges the load-time derivation refuses: the one workload pass runs here
    config = _root_config(**sections)
    config.root = tmp_path
    score_dir = tmp_path / "stack" / "score"
    config.workloads = {name: load_workload(score_dir / name) for name in workload_names}
    return config


def load_workload(path: Path) -> LoadedWorkload:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return LoadedWorkload(path.name, parse_workload(raw, path.name))


def parse_workload(raw: Yaml, file_name: str) -> WorkloadDocument:
    return WorkloadDocument.model_validate(raw, context={"source": file_name})


def test_tool_pin_defaults_live_on_the_root_model() -> None:
    config = _root_config(k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}})

    assert config.helm_version == "3.22.1"
    assert config.score_compose_version == "0.42.0"
    assert config.score_k8s_version == "0.15.0"

    overridden = _root_config(
        k8s={"workloads": ["api.yaml"], "charts": {"loki": "oci://r/chart:1.0.0"}},
        helm_version="3.23.0",
    )

    assert overridden.helm_version == "3.23.0"


def test_bind_volume_carries_required_source_and_target() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "bind", "source": "a", "target": "/b"}]})

    assert volume.volumes is not None
    assert isinstance(volume.volumes[0], BindVolume)
    assert volume.volumes[0].source == "a"
    assert volume.volumes[0].target == "/b"


def test_malformed_bind_fails_validation_instead_of_passing_through() -> None:
    with pytest.raises(ValidationError):
        ComposeService.model_validate({"volumes": [{"type": "bind", "target": "/b"}]})


def test_non_bind_volume_kind_passes_through_untyped() -> None:
    volume = ComposeService.model_validate({"volumes": [{"type": "tmpfs", "target": "/cache"}]})

    assert volume.volumes is not None
    assert not isinstance(volume.volumes[0], BindVolume)


def test_an_absolute_bind_source_parses_as_an_authored_host_mount() -> None:
    volume = ComposeService.model_validate({
        "volumes": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
    })

    assert volume.volumes is not None
    assert volume.bind_volumes == []
    assert volume.host_bind_volumes == volume.volumes


def test_write_normalises_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)

    write_file(path, "content\n")

    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text(encoding="utf-8") == "content\n"


WORKLOAD_MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: workload\n"
HELM_RENDERED = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: chart\n"
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
CHART_REF = "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"
PINNED_CHART = f"ghcr.io/grafana/helm-charts/loki@{DIGEST}"
MIRRORED_CHART = f"{MIRROR_PREFIX}/ghcr.io/grafana/helm-charts/loki@{DIGEST}"

LOKI_INDEX = f"docker.io/grafana/loki:3.5.0@{DIGEST}"
LOKI_MANIFEST = f"docker.io/grafana/loki:3.5.0@{PLATFORM_DIGEST}"
MIRRORED_LOKI_INDEX = f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{DIGEST}"
MIRRORED_LOKI_MANIFEST = f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{PLATFORM_DIGEST}"
LOCK_IMAGES = {
    "GRAFANA_IMAGE": "docker.io/grafana/grafana:11.6.0@" + DIGEST,
    "LOKI_CHART": PINNED_CHART,
}
VALUES_PATH = Path("stack/score/k8s/values/loki.yaml")


class K8sBashRecorder:
    def __init__(self, output_path: Path, state_path: Path) -> None:
        self.output_path = output_path
        self.state_path = state_path
        self.commands: list[str] = []
        self.state_at_generate: dict[str, str] = {}
        self.command_envs: dict[str, dict[str, str] | None] = {}
        self.workload_contents: dict[str, str] = {}

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.commands.append(command)
        self.command_envs[command.split()[1]] = env
        if command.startswith("score-k8s init"):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text("workloads: {}\n", encoding="utf-8")
        if command.startswith("score-k8s generate"):
            for argument in command.split()[2:]:
                if argument == "--output":
                    break
                self.workload_contents[Path(argument).name] = Path(argument).read_text(encoding="utf-8")
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(WORKLOAD_MANIFEST, encoding="utf-8")
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_at_generate[self.output_path.name] = self.state_path.read_text(encoding="utf-8")
            self.state_path.write_text("workloads: {}\n", encoding="utf-8")


class HelmRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.values_contents: list[str] = []

    def run(self, command: str, env: dict[str, str] | None = None) -> str:
        self.commands.append(command)
        for argument in command.split("--values ")[1:]:
            self.values_contents.append(Path(argument.split()[0]).read_text(encoding="utf-8"))
        return HELM_RENDERED


def _charts_config() -> ConfigFile:
    return _root_config(k8s={"charts": {"loki": CHART_REF}})


def _write_values(tmp_path: Path, text: str) -> None:
    path = tmp_path / VALUES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_pins(tmp_path: Path, pins: dict[str, str]) -> None:
    (tmp_path / K8S_PINS_PATH).write_text(yaml.safe_dump(pins), encoding="utf-8")


def _generate_with_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: ConfigFile, images: dict[str, str]
) -> HelmRecorder:
    output = Path("stack/generated/k8s/manifests.yaml")
    k8s = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    helm = HelmRecorder()
    monkeypatch.setattr(score_module, "bash", k8s.run)
    monkeypatch.setattr(score_k8s_module, "bash_output", helm.run)
    assert config.k8s is not None
    config.root = tmp_path
    _write_pins(tmp_path, {})
    lock = LockFile.model_validate({
        "images": {name: {"index": ref} for name, ref in images.items() if not name.endswith("_CHART")},
        "charts": {name: ref for name, ref in images.items() if name.endswith("_CHART")},
    })
    generate_k8s(config, config.k8s, lock, local=False)
    return helm


def test_values_without_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "service:\n  port: 3000\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    argument = helm.commands[0].split("--values ")[1].split()[0]
    assert argument != (tmp_path / VALUES_PATH).as_posix()
    assert argument.endswith("values/loki.yaml")
    assert helm.values_contents == ["service:\n  port: 3000\n"]


def test_values_without_markers_refuse_a_literal_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "image: ghcr.io/example/app:v1\n")
    config = _charts_config()

    with pytest.raises(ValueError, match=r"loki\.yaml.*'ghcr.io/example/app:v1' must consume the lock"):
        _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)


def test_a_missing_chart_lock_entry_refuses_naming_the_chart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "service:\n  port: 3000\n")
    config = _charts_config()

    with pytest.raises(ValueError, match=r"chart 'loki'.*LOKI_CHART"):
        _generate_with_chart(monkeypatch, tmp_path, config, {})


def test_values_with_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources").mkdir(parents=True)
    (tmp_path / "workloads" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").write_text(
        "apiVersion: 1\ndatasources:\n  - name: Loki\n    url: http://loki:3100\n",
        encoding="utf-8",
    )
    _write_values(
        tmp_path,
        "datasources:\n  datasources.yaml:\n    include-yaml: workloads/grafana/provisioning/datasources/datasources.yaml\n",
    )
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    argument = helm.commands[0].split("--values ")[1].split()[0]
    assert argument != (tmp_path / VALUES_PATH).as_posix()
    assert argument.endswith("values/loki.yaml")
    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["datasources"]["datasources.yaml"] == {
        "apiVersion": 1,
        "datasources": [{"name": "Loki", "url": "http://loki:3100"}],
    }


def test_text_markers_in_values_dump_as_literal_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads" / "loki").mkdir(parents=True)
    (tmp_path / "workloads" / "loki" / "config.yaml").write_text(
        "storage:\n  s3:\n    access_key_id: ${S3_ACCESS_KEY}\n",
        encoding="utf-8",
    )
    _write_values(tmp_path, "loki:\n  config:\n    include: workloads/loki/config.yaml\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    assert "config: |" in helm.values_contents[0]
    assert "${S3_ACCESS_KEY}" in helm.values_contents[0]


def test_values_with_lock_markers_resolve_into_the_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_values(tmp_path, "image:\n  lock-image:\n    key: GRAFANA_IMAGE\n    digest_field: sha\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["registry"] == "ghcr.io/outernet-foundation/mirror/docker.io/grafana"
    assert resolved["image"]["repository"] == "grafana"
    assert resolved["image"]["tag"] == "11.6.0"
    assert resolved["image"]["sha"] == DIGEST.removeprefix("sha256:")


def test_values_lock_markers_resolve_after_includes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "workloads").mkdir()
    (tmp_path / "workloads" / "images.yaml").write_text(
        "image:\n  lock-image: GRAFANA_IMAGE\n",
        encoding="utf-8",
    )
    _write_values(tmp_path, "include-yaml: workloads/images.yaml\n")
    config = _charts_config()

    helm = _generate_with_chart(monkeypatch, tmp_path, config, LOCK_IMAGES)

    resolved = yaml.safe_load(helm.values_contents[0])
    assert resolved["image"]["digest"] == DIGEST


def _offline_tool_ensure(monkeypatch: pytest.MonkeyPatch, on_path: str | None, version_output: str = "") -> list[str]:
    commands: list[str] = []

    def _record(command: str, **_kwargs: object) -> None:
        commands.append(command)

    def _which(binary: str) -> str | None:
        return on_path

    def _always_true(command: str) -> bool:
        return True

    def _version_output(command: str) -> str:
        return version_output

    monkeypatch.setattr(score_generate_module, "shutil", SimpleNamespace(which=_which))
    monkeypatch.setattr(score_generate_module, "bash_check", _always_true)
    monkeypatch.setattr(score_generate_module, "bash_output", _version_output)
    monkeypatch.setattr(score_generate_module, "bash", _record)
    return commands


class TestMarkerSubstrateResolution:
    def _multi_arch_lock(self) -> LockFile:
        return LockFile.model_validate({
            "platforms": ["linux/amd64", "linux/arm64"],
            "images": {
                "LOKI_IMAGE": {
                    "index": LOKI_INDEX,
                    "platforms": {"linux/amd64": LOKI_MANIFEST, "linux/arm64": LOKI_INDEX},
                }
            },
            "charts": {"LOKI_CHART": PINNED_CHART},
        })

    def _compose_seam(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, platforms: list[str]) -> str:
        (tmp_path / "stack" / "score").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "api.yaml").write_text(
            "metadata:\n  name: api\nboatyard:\n  lock:\n    api: LOKI_IMAGE\ncontainers:\n  api: {}\n",
            encoding="utf-8",
        )
        loki_platforms = {
            platform: LOKI_MANIFEST if platform == "linux/amd64" else LOKI_INDEX for platform in platforms
        }
        alpine_platforms = dict.fromkeys(platforms, ALPINE_LOCK["ALPINE_IMAGE"])
        (tmp_path / "boatyard.lock.yml").write_text(
            yaml.safe_dump({
                "platforms": platforms,
                "images": {
                    "LOKI_IMAGE": {"index": LOKI_INDEX, "platforms": loki_platforms},
                    "ALPINE_IMAGE": {"index": ALPINE_LOCK["ALPINE_IMAGE"], "platforms": alpine_platforms},
                },
            }),
            encoding="utf-8",
        )
        platform_lines = "".join(f"  - {platform}\n" for platform in platforms)
        base_image_lines = (
            "base_images:\n"
            "  LOKI_IMAGE: docker.io/grafana/loki:3.5.0\n"
            "  ALPINE_IMAGE: docker.io/library/alpine:3.21.0\n"
        )
        (tmp_path / "boatyard.yml").write_text(
            "requires: '>=0'\n"
            "repository: ghcr.io/org/repo\n"
            "project_name: example\n"
            f"platforms:\n{platform_lines}"
            f"{base_image_lines}"
            "services:\n"
            "  core:\n"
            "    options:\n"
            "      main:\n"
            "        workloads:\n"
            "          - api.yaml\n",
            encoding="utf-8",
        )
        recorder = WorkloadContentsRecorder()
        self.recorder = recorder

        monkeypatch.setattr(score_module, "bash", recorder.run)
        _offline_tool_ensure(monkeypatch, on_path=None)
        monkeypatch.chdir(tmp_path)
        score_verb_module.generate_score(target="compose")
        return recorder.workload_contents["api.yaml"]

    def test_compose_generation_receives_the_selected_platform_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        contents = self._compose_seam(monkeypatch, tmp_path, platforms=["linux/amd64"])

        assert f"image: {MIRRORED_LOKI_MANIFEST}" in contents

    def test_the_score_tool_receives_the_variable_values_as_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._compose_seam(monkeypatch, tmp_path, platforms=["linux/amd64"])

        generate_env = self.recorder.command_envs["score-compose generate"]
        assert generate_env is not None
        assert generate_env["LOKI_IMAGE"] == MIRRORED_LOKI_MANIFEST
        assert generate_env["ALPINE_IMAGE"] == f"{MIRROR_PREFIX}/" + ALPINE_LOCK["ALPINE_IMAGE"]
        assert self.recorder.command_envs["score-compose init"] == generate_env

    def test_compose_generation_selects_the_platform_the_environment_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "boatyard.values.yaml").write_text("platform: linux/arm64\n", encoding="utf-8")

        contents = self._compose_seam(monkeypatch, tmp_path, platforms=["linux/amd64", "linux/arm64"])

        assert f"image: {MIRRORED_LOKI_INDEX}" in contents

    def test_k8s_marker_resolution_selects_the_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_values(tmp_path, "image:\n  lock: LOKI_IMAGE\n")
        output = Path("stack/generated/k8s/manifests.yaml")
        k8s = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
        helm = HelmRecorder()
        monkeypatch.setattr(score_module, "bash", k8s.run)
        monkeypatch.setattr(score_k8s_module, "bash_output", helm.run)
        config = _root_config(
            platforms=["linux/amd64"],
            k8s={"workloads": [], "charts": {"loki": CHART_REF}},
        )
        config.root = tmp_path
        _write_pins(tmp_path, {})
        lock = self._multi_arch_lock()
        assert config.k8s is not None

        generate_k8s(config, config.k8s, lock, local=False)

        resolved = yaml.safe_load(helm.values_contents[0])
        assert resolved["image"] == MIRRORED_LOKI_INDEX


class ComposeBashRecorder:
    def __init__(self, generated: str) -> None:
        self.generated = generated
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.commands.append(command)
        if command.startswith("score-compose generate"):
            output = Path(command.rsplit("--output ", 1)[1].split()[0])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(self.generated, encoding="utf-8")


class CompiledProvisionersRecorder(ComposeBashRecorder):
    def __init__(self, generated: str) -> None:
        super().__init__(generated)
        self.compiled: list[list[Yaml]] = []

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        super().run(command, cwd=cwd, env=env)
        if not command.startswith("score-compose init"):
            return
        base = cwd if cwd is not None else Path.cwd()
        for token in command.split():
            if "compiled-provisioners-" in token:
                self.compiled.append(yaml.safe_load((base / token).read_text(encoding="utf-8")))


GENERATED_COMPOSE = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: workloads/api/config.yaml\n"
    "        target: /etc/placeframe/config.yaml\n"
    "  pf-postgres:\n"
    "    image: pg:16\n"
)

GENERATED_COMPOSE_WITH_HOST_BIND = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: workloads/api/config.yaml\n"
    "        target: /etc/placeframe/config.yaml\n"
    "      - type: bind\n"
    "        source: /var/run/docker.sock\n"
    "        target: /var/run/docker.sock\n"
    "  pf-postgres:\n"
    "    image: pg:16\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: /var/run/docker.sock\n"
    "        target: /var/run/docker.sock\n"
)

API_SERVICE_RENDERING = "services:\n  api-api:\n    image: ghcr.io/org/api:1\n"


def _generate_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generated: str,
    workload: str = "api",
    container: str | None = None,
) -> Path:
    container_name = workload if container is None else container
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / f"{workload}.yaml").write_text(
        "metadata:\n"
        f"  name: {workload}\n"
        "boatyard:\n"
        f"  lock:\n    {container_name}: ALPINE_IMAGE\n"
        "containers:\n"
        f"  {container_name}: {{}}\n",
        encoding="utf-8",
    )
    (tmp_path / "workloads" / "api").mkdir(parents=True)
    (tmp_path / "workloads" / "api" / "config.yaml").write_text("key: ${FOO}\n", encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        [f"{workload}.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": [f"{workload}.yaml"]}}}},
    )

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK))
    return tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml"


def test_generate_compose_collapses_doubled_names_and_inlines_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GENERATED_COMPOSE)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["api", "pf-postgres"]
    api = document["services"]["api"]
    assert api["image"] == "ghcr.io/org/api:1"
    assert "volumes" not in api
    assert "networks" not in api
    assert api["configs"] == [{"source": "config.yaml", "target": "/etc/placeframe/config.yaml"}]
    assert document["configs"] == {"config.yaml": {"content": "key: $${FOO}\n"}}
    assert "networks" not in document


def test_generate_compose_passes_authored_host_binds_through_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GENERATED_COMPOSE_WITH_HOST_BIND)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    postgres = document["services"]["pf-postgres"]
    assert postgres["volumes"] == [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
    assert "configs" not in postgres


def test_a_service_with_both_bind_kinds_inlines_only_the_relative_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GENERATED_COMPOSE_WITH_HOST_BIND)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    api = document["services"]["api"]
    assert api["volumes"] == [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
    assert api["configs"] == [{"source": "config.yaml", "target": "/etc/placeframe/config.yaml"}]
    assert document["configs"] == {"config.yaml": {"content": "key: $${FOO}\n"}}


def test_generate_compose_collapses_doubled_names_with_no_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  gateway-gateway:\n    image: ghcr.io/org/gateway:1\n",
        workload="gateway",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["gateway"]
    assert document["services"]["gateway"]["image"] == "ghcr.io/org/gateway:1"
    assert "configs" not in document
    assert "networks" not in document


def test_generate_compose_carries_a_coincidentally_prefixed_service_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = GENERATED_COMPOSE + "  api-migrations:\n    image: ghcr.io/org/migrate:1\n"

    output = _generate_compose(monkeypatch, tmp_path, generated)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["api", "pf-postgres", "api-migrations"]


def test_generate_compose_strips_the_workload_prefix_from_differing_container_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  tunnel-rathole-rathole-client:\n    image: ghcr.io/org/rathole-client:1\n",
        workload="tunnel-rathole",
        container="rathole-client",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["rathole-client"]
    assert document["services"]["rathole-client"]["image"] == "ghcr.io/org/rathole-client:1"


COMPOSE_WITH_BARRIER = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    depends_on:\n"
    "      wait-for-resources:\n"
    "        condition: service_started\n"
    "  pf-postgres:\n"
    "    image: pg:16\n"
    "    depends_on: [wait-for-resources]\n"
    "  wait-for-resources:\n"
    "    image: ghcr.io/org/alpine:1\n"
)


def test_generate_compose_renames_the_barrier_and_keeps_its_references_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, COMPOSE_WITH_BARRIER)

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["api", "pf-postgres", "wait-for-resources"]
    assert document["services"]["api"]["depends_on"] == {"wait-for-resources": {"condition": "service_started"}}
    assert document["services"]["pf-postgres"]["depends_on"] == ["wait-for-resources"]
    assert document["services"]["wait-for-resources"]["image"] == "${BOATYARD_BARRIER_IMAGE:?err}"


def test_generate_compose_barrier_names_are_one_global_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\nboatyard:\n  lock:\n    api: API_IMAGE\ncontainers:\n  api: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "stack" / "score" / "edge.yaml").write_text(
        "metadata:\n  name: edge\ncontainers: {}\n", encoding="utf-8"
    )
    recorder = ComposeBashRecorder(COMPOSE_WITH_BARRIER)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml", "edge.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={
            "core": {"options": {"main": {"workloads": ["api.yaml"]}}},
            "edge": {"options": {"main": {"workloads": ["edge.yaml"]}}},
        },
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "docker.io/library/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    core = yaml.safe_load((tmp_path / "stack/generated/compose/compose.core-main.yaml").read_text(encoding="utf-8"))
    edge = yaml.safe_load((tmp_path / "stack/generated/compose/compose.edge-main.yaml").read_text(encoding="utf-8"))
    assert core["services"]["wait-for-resources"]["image"] == "${BOATYARD_BARRIER_IMAGE:?err}"
    assert edge["services"]["wait-for-resources"]["image"] == "${BOATYARD_BARRIER_IMAGE:?err}"


class WorkloadContentsRecorder:
    def __init__(self, generated: str = API_SERVICE_RENDERING, generated_by_name: dict[str, str] | None = None) -> None:
        self.generated = generated
        self.generated_by_name = generated_by_name
        self.workload_contents: dict[str, str] = {}
        self.command_envs: dict[str, dict[str, str] | None] = {}
        self.generate_envs: list[dict[str, str] | None] = []

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.command_envs[command.split()[0] + " " + command.split()[1]] = env
        if command.startswith("score-compose generate"):
            self.generate_envs.append(env)
            for argument in command.split()[2:]:
                if argument.startswith("--output"):
                    break
                path = Path(argument)
                self.workload_contents[path.name] = path.read_text(encoding="utf-8")
            output = Path(command.rsplit("--output ", 1)[1].split()[0])
            output.parent.mkdir(parents=True, exist_ok=True)
            rendered = self.generated if self.generated_by_name is None else self.generated_by_name[output.name]
            output.write_text(rendered, encoding="utf-8")


LOCK_SOURCE_API = "metadata:\n  name: api\nboatyard:\n  lock:\n    api: LOCK_KEY\ncontainers:\n  api: {}\n"


def _lock_source_workload(key: str) -> str:
    return LOCK_SOURCE_API.replace("LOCK_KEY", key)


def test_workload_lock_sources_inject_the_locked_reference_into_the_rendered_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(_lock_source_workload("API_IMAGE"), encoding="utf-8")
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api@sha256:" + "a" * 64, **ALPINE_LOCK}),
    )

    assert f"image: {MIRROR_PREFIX}/ghcr.io/org/api@sha256:{'a' * 64}\n" in recorder.workload_contents["api.yaml"]


DOCKERFILE_SOURCE_API = (
    "metadata:\n  name: api\nboatyard:\n  dockerfile:\n    api: workloads/api/Dockerfile\ncontainers:\n  api: {}\n"
)


def _generate_compose_with_derived_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    image: Image | None = None,
    entry_vars: dict[str, str] | None = None,
    generated: str | None = None,
) -> tuple[WorkloadContentsRecorder, str]:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(DOCKERFILE_SOURCE_API, encoding="utf-8")
    variant_fragment = (
        ""
        if image is None or image.axis is None or entry_vars is None or image.axis not in entry_vars
        else f"-{entry_vars[image.axis]}"
    )
    recorder = WorkloadContentsRecorder(
        generated
        if generated is not None
        else f"services:\n  api-api:\n    image: ghcr.io/org/repo/api{variant_fragment}:__BOATYARD_TREE__API_SHA__\n"
    )
    monkeypatch.setattr(score_module, "bash", recorder.run)
    entry: dict[str, object] = {"workloads": ["api.yaml"]}
    if entry_vars is not None:
        entry["vars"] = entry_vars
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": entry}}},
    )
    config.images = {"api": image if image is not None else Image(name="api", dockerfile="workloads/api/Dockerfile")}

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK))
    output = tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml"
    return recorder, output.read_text(encoding="utf-8")


def test_workload_dockerfile_sources_inject_the_composed_tree_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, artifact = _generate_compose_with_derived_images(monkeypatch, tmp_path)

    assert "image: ghcr.io/org/repo/api:__BOATYARD_TREE__API_SHA__\n" in recorder.workload_contents["api.yaml"]
    generate_env = recorder.command_envs["score-compose generate"]
    assert generate_env is not None
    assert generate_env["API_SHA"] == "__BOATYARD_TREE__API_SHA__"
    assert "image: ghcr.io/org/repo/api:${CORE_MAIN__BOATYARD_TREE__API_SHA}" in artifact
    assert "tree-" not in artifact
    assert config_file_module.TREE_SENTINEL_PATTERN.search(artifact) is None


def test_an_image_with_an_axis_injects_the_selected_variant_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, artifact = _generate_compose_with_derived_images(
        monkeypatch,
        tmp_path,
        image=Image(name="api", dockerfile="workloads/api/Dockerfile", axis="TORCH_DEVICE", variants=["cuda", "rocm"]),
        entry_vars={"TORCH_DEVICE": "cuda"},
    )

    assert "image: ghcr.io/org/repo/api-cuda:__BOATYARD_TREE__API_SHA__\n" in recorder.workload_contents["api.yaml"]
    assert "image: ghcr.io/org/repo/api-cuda:${CORE_MAIN__BOATYARD_TREE__API_SHA}" in artifact


def test_an_image_with_an_axis_and_no_selected_variant_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(
        ValueError, match=r"api carries the axis TORCH_DEVICE, which this rendering selects no value for"
    ):
        _generate_compose_with_derived_images(
            monkeypatch,
            tmp_path,
            image=Image(name="api", dockerfile="workloads/api/Dockerfile", axis="TORCH_DEVICE", variants=["cuda"]),
        )


def test_generate_compose_never_walks_the_source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse_the_walk(_self: Image, _root: Path) -> str:
        raise AssertionError("compose generation reads only the config's image set")

    monkeypatch.setattr(Image, "compute_sha", _refuse_the_walk)
    _recorder, artifact = _generate_compose_with_derived_images(monkeypatch, tmp_path)

    assert "image: ghcr.io/org/repo/api:${CORE_MAIN__BOATYARD_TREE__API_SHA}" in artifact


def _generate_compose_multi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generated: str,
    workload_containers: dict[str, list[str]],
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    for workload_name, containers in workload_containers.items():
        document: dict[str, object] = {
            "metadata": {"name": workload_name},
            "boatyard": {"lock": dict.fromkeys(containers, "ALPINE_IMAGE")},
            "containers": {name: {} for name in containers},
        }
        path = tmp_path / "stack" / "score" / f"{workload_name}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        [f"{name}.yaml" for name in workload_containers],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": [f"{name}.yaml" for name in workload_containers]}}}},
    )
    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK))


class TestRenderedNameClaims:
    def test_a_container_name_shared_across_workloads_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(
            ValueError,
            match=r"is claimed at alpha.yaml: containers.sidecar and beta.yaml: containers.sidecar",
        ):
            _generate_compose_multi(
                monkeypatch,
                tmp_path,
                "services:\n  alpha-sidecar:\n    image: demo/a:1\n  beta-sidecar:\n    image: demo/b:1\n",
                {"alpha": ["sidecar"], "beta": ["sidecar"]},
            )

    def test_a_container_claiming_the_barrier_name_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="the reserved barrier name"):
            _generate_compose_multi(
                monkeypatch,
                tmp_path,
                "services:\n"
                "  api-api:\n    image: demo/api:1\n"
                "  api-wait-for-resources:\n    image: demo/wait:1\n"
                "  wait-for-resources:\n    image: demo/alpine:1\n",
                {"api": ["api", "wait-for-resources"]},
            )

    def test_a_provisioner_service_claiming_a_container_name_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(
            ValueError,
            match=r"is claimed at api.yaml: containers.migrations and the rendered services text",
        ):
            _generate_compose_multi(
                monkeypatch,
                tmp_path,
                "services:\n  api-migrations:\n    image: demo/migrate:1\n  migrations:\n    image: demo/x:1\n",
                {"api": ["migrations"]},
            )

    def test_ambiguous_rendered_spellings_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(
            ValueError,
            match=r"the rendered spelling 'web-api-x' is claimed at web.yaml: containers.api-x"
            r" and web-api.yaml: containers.x",
        ):
            _generate_compose_multi(
                monkeypatch,
                tmp_path,
                "services:\n  web-api-x:\n    image: demo/web:1\n",
                {"web": ["api-x"], "web-api": ["x"]},
            )

    def test_a_provisioner_service_lands_beside_workload_containers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _generate_compose_multi(
            monkeypatch,
            tmp_path,
            "services:\n  api-api:\n    image: demo/api:1\n  pf-postgres:\n    image: demo/pg:16\n",
            {"api": ["api"]},
        )

        output = tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml"
        document = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert list(document["services"]) == ["api", "pf-postgres"]


SHIPPED_TEMPLATES = {
    "probe-adapter-healthchecks.tpl",
    "restart.tpl",
    "gpu-cuda.tpl",
    "gpu-rocm.tpl",
    "gpu-pod.tpl",
    "scale-to-zero.tpl",
}


def test_shipped_patch_templates_resolve() -> None:
    directory = files("boatyard") / "score_patch_templates"

    assert {item.name for item in directory.iterdir()} == SHIPPED_TEMPLATES
    for name in SHIPPED_TEMPLATES:
        assert (directory / name).read_text(encoding="utf-8").strip()


SELF_GATING_FALLBACK = "{{- if not $matched }}\n[]\n{{- end }}"


def test_self_gating_templates_fall_back_to_an_empty_patches_document() -> None:
    directory = files("boatyard") / "score_patch_templates"

    for name in SHIPPED_TEMPLATES:
        text = (directory / name).read_text(encoding="utf-8")
        assert SELF_GATING_FALLBACK in text
        assert "{{- $matched = true }}" in text


def test_the_healthcheck_template_ships_both_probe_adapter_spells() -> None:
    text = (files("boatyard") / "score_patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert workload_module.PROBE_ADAPTERS == ("python-urllib", "bash-http")
    assert "urllib.request.urlopen" in text
    assert "/dev/tcp/127.0.0.1" in text


def test_the_bash_http_spell_escapes_its_dollar_for_compose_interpolation() -> None:
    text = (files("boatyard") / "score_patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert "$$status" in text
    assert "$status" not in text.replace("$$status", "")


def test_the_bash_http_spell_uses_the_slash_port_spelling() -> None:
    text = (files("boatyard") / "score_patch_templates" / "probe-adapter-healthchecks.tpl").read_text(encoding="utf-8")

    assert "/dev/tcp/127.0.0.1/{{ .port }}" in text
    assert "/dev/tcp/127.0.0.1:{{ .port }}" not in text


PROVISIONER_WITH_SERVICES = """
- uri: template://example/config
  type: example-config
  services:
    example-config:
      image: ${EXAMPLE_IMAGE:?err}
"""

PROVISIONER_WITH_CLAIM = """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: 3000
"""

PROVISIONER_WITHOUT_SERVICES = """
- uri: template://example/state
  type: example-state
  state:
    key: value
"""

ALPINE_LOCK = {"ALPINE_IMAGE": "docker.io/library/alpine:3.21.0@" + DIGEST}


def _lock_for_images(images: dict[str, str]) -> LockFile:
    return LockFile.model_validate({
        "platforms": ["linux/amd64"],
        "images": {
            name: {"index": reference, "platforms": {"linux/amd64": reference}} for name, reference in images.items()
        },
    })


def _bundle_config(tmp_path: Path, **bundle: object) -> ConfigFile:
    # Sources must be on disk before this call — the one workload pass runs at load
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "requires": ">=0.1",
        "repository": "ghcr.io/org/repo",
        "project_name": "example",
        "platforms": ["linux/amd64"],
        "gateway_port": 8080,
        "base_images": {"API_IMAGE": "docker.io/library/api:1", "ALPINE_IMAGE": "docker.io/library/alpine:3.21.0"},
        "services": {"core": {"options": {"main": {"workloads": ["api.yaml"], **bundle}}}},
    }
    (tmp_path / "boatyard.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return ConfigFile.load(tmp_path / "boatyard.yml")


def _compose_init_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: ConfigFile | None,
    images: dict[str, str],
    sources: dict[str, str],
    generated: str = "services: {}\n",
) -> list[list[str]]:
    # Return one template-name list per bundle rendering, in declaration order. Pass a
    # config for fixtures that inject state load refuses; None builds the bundle config
    # here after the sources are on disk.
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    if config is None:
        config = _bundle_config(tmp_path)
    score_compose_module.generate_compose(config, _lock_for_images({**images, **ALPINE_LOCK}))
    inits = [command for command in recorder.commands if command.startswith("score-compose init")]
    return [[Path(token).name for token in command.split() if token.endswith(".tpl")] for command in inits]


def _workload_referencing(resource: str, name: str) -> str:
    return f"""
resources:
  {resource}:
    type: example-{resource}
metadata:
  name: {name}
containers: {{}}
"""


def test_barrier_pin_stays_unregistered_without_provisioner_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        None,
        {},
        {
            "stack/score/api.yaml": _workload_referencing("state", "api"),
            "stack/score/compose/state.yaml": PROVISIONER_WITHOUT_SERVICES,
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_provisioners_register_by_resource_type_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        "stack/score/api.yaml": _workload_referencing("state", "api"),
        "stack/score/gateway.yaml": _workload_referencing("config", "gateway"),
        "stack/score/compose/config.yaml": PROVISIONER_WITH_SERVICES,
        "stack/score/compose/state.yaml": PROVISIONER_WITHOUT_SERVICES,
        "stack/score/compose/orphan.yaml": "- uri: template://example/orphan\n  type: orphan-type\n",
    }
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    config = _bundle_config(tmp_path, workloads=["api.yaml", "gateway.yaml"])
    recorder = ComposeBashRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK))

    init = next(command for command in recorder.commands if command.startswith("score-compose init"))
    provisioners = [token for token in init.split() if token.endswith(".yaml")]
    assert [Path(token).name for token in provisioners] == ["config.yaml", "state.yaml"]


def test_a_provisioner_file_with_duplicate_keys_refuses_at_load(tmp_path: Path) -> None:
    (tmp_path / "stack" / "score" / "compose").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(_workload_referencing("config", "api"), encoding="utf-8")
    (tmp_path / "stack" / "score" / "compose" / "config.yaml").write_text(
        "- uri: template://example/config\n  type: example-config\n  type: example-config\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"compose/config\.yaml: line 3: duplicate key 'type'"):
        _bundle_config(tmp_path)


ROUTE_CLAIM_WORKLOAD = (
    "metadata:\n"
    "  name: api\n"
    "boatyard:\n"
    "  route:\n"
    "    path: /auth/*\n"
    "    port: web\n"
    "    strip: true\n"
    "  lock:\n"
    "    api: API_IMAGE\n"
    "listeners:\n"
    "  web: 8081\n"
    "  admin: 9090\n"
    "containers:\n"
    "  api: {}\n"
)

ROUTE_CLAIM_GENERATED = (
    "services:\n"
    "  api-api:\n"
    "    image: ghcr.io/org/api:1\n"
    "    annotations:\n"
    "      boatyard/route-path: /auth/*\n"
    "      boatyard/route-port: '8081'\n"
    "      boatyard/route-strip: 'true'\n"
)


def _generate_claiming_workload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generated: str,
) -> WorkloadContentsRecorder:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(ROUTE_CLAIM_WORKLOAD, encoding="utf-8")
    recorder = WorkloadContentsRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )
    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )
    return recorder


def test_a_workload_claim_carries_the_resolved_route_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _generate_claiming_workload(monkeypatch, tmp_path, ROUTE_CLAIM_GENERATED)

    document = yaml.safe_load(
        (tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml").read_text(encoding="utf-8")
    )
    assert document["services"]["api"]["annotations"] == {
        "boatyard/route-path": "/auth/*",
        "boatyard/route-port": "8081",
        "boatyard/route-strip": "true",
    }


def test_the_route_path_export_rides_the_tool_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _generate_claiming_workload(monkeypatch, tmp_path, ROUTE_CLAIM_GENERATED)

    generate_env = recorder.command_envs["score-compose generate"]
    assert generate_env is not None
    assert generate_env["API_ROUTE_PATH"] == "/auth"
    assert recorder.command_envs["score-compose init"] == generate_env


def test_the_route_path_export_crosses_bundle_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(ROUTE_CLAIM_WORKLOAD, encoding="utf-8")
    (tmp_path / "stack" / "score" / "aux.yaml").write_text(
        "metadata:\n  name: aux\nboatyard:\n  lock:\n    aux: AUX_IMAGE\ncontainers:\n  aux: {}\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder(
        generated_by_name={
            "compose.core-main.yaml": ROUTE_CLAIM_GENERATED,
            "compose.auxiliary-other.yaml": "services:\n  aux-aux:\n    image: ghcr.io/org/aux:1\n",
        }
    )
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml", "aux.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={
            "core": {"options": {"main": {"workloads": ["api.yaml"]}}},
            "auxiliary": {"options": {"other": {"workloads": ["aux.yaml"]}}},
        },
    )
    score_compose_module.generate_compose(
        config,
        _lock_for_images({
            "API_IMAGE": "ghcr.io/org/api:1@" + DIGEST,
            "AUX_IMAGE": "ghcr.io/org/aux:1@" + DIGEST,
            **ALPINE_LOCK,
        }),
    )

    # A provisioner in one bundle templates a claim authored in another, so every
    # entry's generate env carries the export, not just the claiming entry's
    assert len(recorder.generate_envs) == 2
    for generate_env in recorder.generate_envs:
        assert generate_env is not None
        assert generate_env["API_ROUTE_PATH"] == "/auth"


def test_two_claiming_workloads_spelling_one_export_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    for name in ("api", "API"):
        (tmp_path / "stack" / "score" / f"{name}.yaml").write_text(
            ROUTE_CLAIM_WORKLOAD.replace("  name: api\n", f"  name: {name}\n"),
            encoding="utf-8",
        )
    recorder = WorkloadContentsRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["API.yaml", "api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["API.yaml", "api.yaml"]}}}},
    )

    with pytest.raises(ValueError, match=r"API\.yaml and api\.yaml spell one route path export API_ROUTE_PATH"):
        score_compose_module.generate_compose(
            config,
            _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
        )


def test_the_expanded_workload_copy_carries_the_resolved_route_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _generate_claiming_workload(monkeypatch, tmp_path, ROUTE_CLAIM_GENERATED)

    assert "boatyard/route-path: /auth/*\n" in recorder.workload_contents["api.yaml"]
    assert "boatyard/route-port: '8081'\n" in recorder.workload_contents["api.yaml"]
    assert "boatyard/route-strip: 'true'\n" in recorder.workload_contents["api.yaml"]


def test_the_expanded_workload_copy_derives_service_ports_from_the_listeners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _generate_claiming_workload(monkeypatch, tmp_path, ROUTE_CLAIM_GENERATED)
    expanded = recorder.workload_contents["api.yaml"]

    document = yaml.safe_load(expanded)
    assert document["service"] == {
        "ports": {"web": {"port": 8081, "targetPort": 8081}, "admin": {"port": 9090, "targetPort": 9090}}
    }
    assert "listeners" not in document
    assert "boatyard" not in document


LISTENER_INVENTORY_WORKLOAD = (
    "metadata:\n"
    "  name: api\n"
    "boatyard:\n"
    "  lock:\n"
    "    api: API_IMAGE\n"
    "listeners:\n"
    "  web: 8081\n"
    "  sidecar-admin: 9090\n"
    "  debug:\n"
    "    port: 8082\n"
    "    internal: true\n"
    "containers:\n"
    "  api: {}\n"
)

LISTENER_INVENTORY_GENERATED = "services:\n  api-api:\n    image: ghcr.io/org/api:1\n"


def _generate_listening_workload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_vars: dict[str, str] | None = None,
) -> WorkloadContentsRecorder:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(LISTENER_INVENTORY_WORKLOAD, encoding="utf-8")
    recorder = WorkloadContentsRecorder(LISTENER_INVENTORY_GENERATED)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    entry: dict[str, object] = {"workloads": ["api.yaml"]}
    if entry_vars is not None:
        entry["vars"] = entry_vars
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": entry}}},
    )
    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )
    return recorder


def test_the_listener_port_exports_ride_the_tool_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _generate_listening_workload(monkeypatch, tmp_path)

    generate_env = recorder.command_envs["score-compose generate"]
    assert generate_env is not None
    assert generate_env["API_LISTENER_WEB"] == "8081"
    assert generate_env["API_LISTENER_SIDECAR_ADMIN"] == "9090"
    assert recorder.command_envs["score-compose init"] == generate_env


def test_an_internal_listener_spells_no_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _generate_listening_workload(monkeypatch, tmp_path)

    generate_env = recorder.command_envs["score-compose generate"]
    assert generate_env is not None
    assert "API_LISTENER_DEBUG" not in generate_env


def test_two_listening_workloads_spelling_one_export_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    for name in ("api", "API"):
        (tmp_path / "stack" / "score" / f"{name}.yaml").write_text(
            LISTENER_INVENTORY_WORKLOAD.replace("  name: api\n", f"  name: {name}\n"),
            encoding="utf-8",
        )
    recorder = WorkloadContentsRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["API.yaml", "api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["API.yaml", "api.yaml"]}}}},
    )

    with pytest.raises(ValueError, match=r"API\.yaml and api\.yaml spell one listener port export API_LISTENER_WEB"):
        score_compose_module.generate_compose(
            config,
            _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
        )


def test_a_listener_port_export_colliding_with_an_authored_value_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        ValueError, match=r"the listener port export API_LISTENER_WEB collides with an authored rendering value"
    ):
        _generate_listening_workload(monkeypatch, tmp_path, entry_vars={"API_LISTENER_WEB": "7000"})


RICH_SEAM_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: api
  annotations:
    boatyard/gpu: "true"
boatyard:
  route:
    path: /auth/*
    port: web
    strip: true
  lock:
    api: API_IMAGE
listeners:
  web: 8081
  admin:
    port: 9090
  mgmt:
    port: 9000
    internal: true
resources:
  db:
    type: example-db
containers:
  api:
    command: ["/bin/sh"]
    args: ["-c", "serve"]
    files:
      /etc/api/config.yaml:
        content: |
          key: ${FOO}
    variables:
      SERVICE_PORT: ${listeners.admin.port}
      RETRIES: 3
    readinessProbe:
      httpGet:
        path: /ready
        port: web
"""


def test_the_seam_composes_the_tool_document_from_the_lens() -> None:
    workload = LoadedWorkload("api.yaml", parse_workload(yaml.safe_load(RICH_SEAM_WORKLOAD), "api.yaml"))

    tool = score_module.injected_workload(
        _root_config(),
        workload,
        {"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST},
    )

    assert tool == {
        "apiVersion": "score.dev/v1b1",
        "metadata": {
            "name": "api",
            "annotations": {
                "boatyard/gpu": "true",
                "boatyard/route-path": "/auth/*",
                "boatyard/route-port": "8081",
                "boatyard/route-strip": "true",
            },
        },
        "resources": {"db": {"type": "example-db"}},
        "containers": {
            "api": {
                "command": ["/bin/sh"],
                "args": ["-c", "serve"],
                "image": "ghcr.io/org/api:1@" + DIGEST,
                "files": {"/etc/api/config.yaml": {"content": "key: ${FOO}\n"}},
                "variables": {"SERVICE_PORT": "9090", "RETRIES": 3},
                "readinessProbe": {"httpGet": {"path": "/ready", "port": 8081}},
            }
        },
        "service": {"ports": {"web": {"port": 8081, "targetPort": 8081}, "admin": {"port": 9090, "targetPort": 9090}}},
    }


def test_internal_listeners_leave_the_derived_service_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n"
        "  name: api\n"
        "boatyard:\n"
        "  lock:\n    api: API_IMAGE\n"
        "listeners:\n"
        "  web: 8080\n"
        "  mgmt:\n"
        "    port: 9000\n"
        "    internal: true\n"
        "containers:\n"
        "  api: {}\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    document = yaml.safe_load(recorder.workload_contents["api.yaml"])
    assert document["service"] == {"ports": {"web": {"port": 8080, "targetPort": 8080}}}


def test_provisioner_claims_translate_into_labels_under_the_string_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = {
        "stack/score/api.yaml": _workload_referencing("config", "api"),
        "stack/score/compose/config.yaml": PROVISIONER_WITH_CLAIM,
    }
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    recorder = CompiledProvisionersRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _bundle_config(tmp_path)

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"EXAMPLE_IMAGE": "ghcr.io/org/example:1@" + DIGEST, **ALPINE_LOCK}),
    )

    [document] = recorder.compiled
    [entry] = document
    assert isinstance(entry, dict)
    services_text = entry["services"]
    assert isinstance(services_text, str)
    assert yaml.safe_load(services_text) == {
        "grafana": {
            "image": "${EXAMPLE_IMAGE:?err}",
            "annotations": {"boatyard/route-path": "/grafana/*", "boatyard/route-port": "3000"},
        }
    }


def _invoke_claiming_provisioner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provisioner: str
) -> CompiledProvisionersRecorder:
    sources = {
        "stack/score/api.yaml": _workload_referencing("config", "api"),
        "stack/score/compose/config.yaml": provisioner,
    }
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    recorder = CompiledProvisionersRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _bundle_config(tmp_path)
    score_compose_module.generate_compose(
        config,
        _lock_for_images({"EXAMPLE_IMAGE": "ghcr.io/org/example:1@" + DIGEST, **ALPINE_LOCK}),
    )
    return recorder


def test_a_provisioner_route_without_its_port_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match=r"port\n.*Field required"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
""",
        )


def test_a_provisioner_port_without_a_route_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=r"services\.grafana: port: rides a route: claim"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      port: 3000
""",
        )


def test_a_provisioner_claim_with_a_qualified_path_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match=r"route path .* must be a /-rooted segment chain"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*@web
        port: 3000
""",
        )


def test_a_provisioner_stanza_annotating_the_labels_directly_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"annotates boatyard/route-path directly"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: 3000
      annotations:
        boatyard/route-path: /grafana/*
""",
        )


def test_a_boolean_provisioner_route_port_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match=r"route\.port must be an integer, not a boolean"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: true
""",
        )


PROVISIONER_WITH_TEMPLATED_CLAIM = """
- uri: template://example/config
  type: example-config
  state:
    grafana_port: 3000
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: "{{ .State.grafana_port }}"
      healthcheck:
        test:
          [
            "CMD",
            "wget",
            "--quiet",
            "--tries=1",
            "--output-document=-",
            "http://localhost:{{ .State.grafana_port }}/health",
          ]
        interval: 5s
"""


def test_a_templated_provisioner_route_port_renders_into_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _invoke_claiming_provisioner(monkeypatch, tmp_path, PROVISIONER_WITH_TEMPLATED_CLAIM)

    [document] = recorder.compiled
    [entry] = document
    assert isinstance(entry, dict)
    services_text = entry["services"]
    assert isinstance(services_text, str)
    grafana = yaml.safe_load(services_text)["grafana"]
    assert "route" not in grafana
    assert grafana["annotations"] == {"boatyard/route-path": "/grafana/*", "boatyard/route-port": "3000"}


def test_the_healthcheck_templating_the_same_state_key_rides_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _invoke_claiming_provisioner(monkeypatch, tmp_path, PROVISIONER_WITH_TEMPLATED_CLAIM)

    [document] = recorder.compiled
    [entry] = document
    assert isinstance(entry, dict)
    services_text = entry["services"]
    assert isinstance(services_text, str)
    grafana = yaml.safe_load(services_text)["grafana"]
    assert grafana["healthcheck"]["test"][-1] == "http://localhost:{{ .State.grafana_port }}/health"
    assert grafana["annotations"]["boatyard/route-port"] == "3000"


def test_a_templated_provisioner_route_port_refuses_a_non_integer_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"renders to 'not-a-port', not a port in the range 1-65535"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  state:
    grafana_port: not-a-port
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: "{{ .State.grafana_port }}"
""",
        )


def test_a_templated_provisioner_route_port_refuses_an_out_of_range_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"renders to '70000', not a port in the range 1-65535"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  state:
    grafana_port: 70000
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: "{{ .State.grafana_port }}"
""",
        )


def test_a_templated_provisioner_route_port_refuses_a_missing_state_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"names no entry the state declares"):
        _invoke_claiming_provisioner(
            monkeypatch,
            tmp_path,
            """
- uri: template://example/config
  type: example-config
  state:
    grafana_port: 3000
  services:
    grafana:
      image: ${EXAMPLE_IMAGE:?err}
      route:
        path: /grafana/*
        port: "{{ .State.admin_port }}"
""",
        )


WORKLOAD_WITH_LIVENESS = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  health:
    port: 5678
    internal: true
containers:
  api:
    livenessProbe:
      httpGet:
        path: /health
        port: health
"""

WORKLOAD_WITH_READINESS = """
metadata:
  name: api
  annotations:
    boatyard/probe-adapter: python-urllib
boatyard:
  lock:
    api: API_IMAGE
listeners:
  health:
    port: 5678
    internal: true
containers:
  api:
    readinessProbe:
      httpGet:
        path: /ready
        port: health
"""

WORKLOAD_WITH_UNADAPTED_READINESS = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  health:
    port: 5678
    internal: true
containers:
  api:
    readinessProbe:
      httpGet:
        path: /ready
        port: health
"""


def test_httpget_probe_without_the_adapter_annotation_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The compose-surface check runs at generation over the once-loaded documents
    with pytest.raises(ValueError, match=r"readinessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            None,
            {"API_IMAGE": "docker.io/library/api:1@" + DIGEST},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_UNADAPTED_READINESS,
            },
        )

    with pytest.raises(ValueError, match=r"livenessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            None,
            {"API_IMAGE": "docker.io/library/api:1@" + DIGEST},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_LIVENESS,
            },
        )


def test_unknown_probe_adapter_values_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\n  annotations:\n    boatyard/probe-adapter: curl\ncontainers: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"supported adapters"):
        _bundle_config(tmp_path)


WORKLOAD_PROBE_BARE_LISTENER = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  web: 5678
containers:
  api:
    readinessProbe:
      httpGet:
        path: /ready
        port: web
"""

WORKLOAD_PROBE_MAPPING_LISTENER = """
metadata:
  name: api
  annotations:
    boatyard/probe-adapter: python-urllib
boatyard:
  lock:
    api: API_IMAGE
listeners:
  web:
    port: 9090
containers:
  api:
    readinessProbe:
      httpGet:
        path: /ready
        port: web
"""

WORKLOAD_PROBE_INTERNAL_LISTENER = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  mgmt:
    port: 9000
    internal: true
containers:
  api:
    livenessProbe:
      httpGet:
        path: /health
        port: mgmt
"""


def test_probe_listener_names_resolve_over_both_listener_shapes() -> None:
    for fixture, resolved in ((WORKLOAD_PROBE_BARE_LISTENER, 5678), (WORKLOAD_PROBE_MAPPING_LISTENER, 9090)):
        document = parse_workload(yaml.safe_load(fixture), "api.yaml")

        assert document.listener_ports == {"web": resolved}


def test_probe_listener_names_resolve_internal_listeners() -> None:
    document = parse_workload(yaml.safe_load(WORKLOAD_PROBE_INTERNAL_LISTENER), "api.yaml")

    assert document.listener_ports == {"mgmt": 9000}


def test_internal_listeners_leave_the_routable_inventory() -> None:
    document = parse_workload(
        yaml.safe_load(
            "metadata:\n  name: api\nlisteners:\n  web: 8080\n  mgmt:\n    port: 9000\n    internal: true\n"
        ),
        "api.yaml",
    )

    assert document.routable_listener_ports == {"web": 8080}


def test_literal_probe_ports_refuse() -> None:
    with pytest.raises(ValidationError, match=r"probe ports are listener names"):
        parse_workload(
            {
                "metadata": {"name": "api"},
                "boatyard": {"lock": {"api": "API_IMAGE"}},
                "listeners": {"web": 5678},
                "containers": {"api": {"livenessProbe": {"httpGet": {"path": "/health", "port": 5678}}}},
            },
            "api.yaml",
        )


def test_a_probe_without_a_port_refuses() -> None:
    with pytest.raises(ValidationError, match=r"declares no port"):
        parse_workload(
            {
                "metadata": {"name": "api"},
                "boatyard": {"lock": {"api": "API_IMAGE"}},
                "listeners": {"web": 5678},
                "containers": {"api": {"livenessProbe": {"httpGet": {"path": "/health"}}}},
            },
            "api.yaml",
        )


def test_unknown_probe_listener_names_refuse_listing_the_declared_listeners() -> None:
    with pytest.raises(ValidationError, match=r"names no declared listener \(mgmt, web\)"):
        parse_workload(
            {
                "metadata": {"name": "api"},
                "boatyard": {"lock": {"api": "API_IMAGE"}},
                "listeners": {"web": 5678, "mgmt": {"port": 9000, "internal": True}},
                "containers": {"api": {"livenessProbe": {"httpGet": {"path": "/health", "port": "admin"}}}},
            },
            "api.yaml",
        )


def test_malformed_listener_specs_refuse() -> None:
    malformed: list[tuple[dict[str, Yaml], str]] = [
        ({"web": 0}, "outside 1-65535"),
        ({"web": 65536}, "outside 1-65535"),
        ({"web": {"port": 9000, "unknown": True}}, "Extra inputs are not permitted"),
        ({"bad name!": 9000}, "must be a plain name"),
    ]
    for listeners, message in malformed:
        with pytest.raises(ValidationError, match=message):
            parse_workload({"metadata": {"name": "api"}, "listeners": listeners}, "api.yaml")


def test_boolean_listener_ports_refuse() -> None:
    malformed: list[tuple[dict[str, Yaml], str]] = [
        ({"web": True}, r"the listener 'web' port must be an integer, not a boolean"),
        ({"web": {"port": True}}, r"listeners\.port must be an integer, not a boolean"),
    ]
    for listeners, message in malformed:
        with pytest.raises(ValidationError, match=message):
            parse_workload({"metadata": {"name": "api"}, "listeners": listeners}, "api.yaml")


def test_an_authored_service_stanza_refuses() -> None:
    with pytest.raises(ValidationError, match=r"service: is compiler output derived from listeners:"):
        parse_workload(
            {
                "metadata": {"name": "api"},
                "boatyard": {"lock": {"api": "API_IMAGE"}},
                "listeners": {"web": 5678},
                "service": {"ports": {"web": 5678}},
                "containers": {"api": {}},
            },
            "api.yaml",
        )


def test_removed_and_unknown_boatyard_annotations_refuse() -> None:
    for key, value in (
        ("boatyard/route", "/auth/*"),
        ("boatyard/container-ports", "mgmt=9000"),
        ("boatyard/dockerfile", "api=workloads/api/Dockerfile"),
        ("boatyard/typo", "whatever"),
    ):
        with pytest.raises(ValidationError, match=r"is not a workload annotation.*boatyard: block"):
            parse_workload(
                {
                    "metadata": {"name": "api", "annotations": {key: value}},
                    "containers": {},
                },
                "api.yaml",
            )


def test_the_gpu_annotation_parses() -> None:
    document = parse_workload(
        {
            "metadata": {"name": "api", "annotations": {"boatyard/gpu": "true"}},
            "containers": {},
        },
        "api.yaml",
    )

    assert document.metadata.annotations == {"boatyard/gpu": "true"}


def test_the_compose_rendering_receives_the_resolved_probe_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(WORKLOAD_PROBE_MAPPING_LISTENER, encoding="utf-8")
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    assert "port: 9090\n" in recorder.workload_contents["api.yaml"]
    assert "port: web\n" not in recorder.workload_contents["api.yaml"]


WORKLOAD_PROBE_DOCKERFILE_PORT = (
    "metadata:\n"
    "  name: api\n"
    "boatyard:\n"
    "  dockerfile:\n"
    "    api: workloads/api/Dockerfile\n"
    "listeners:\n"
    "  mgmt:\n"
    "    port: 9000\n"
    "    internal: true\n"
    "containers:\n"
    "  api:\n"
    "    livenessProbe:\n"
    "      httpGet:\n"
    "        path: /health\n"
    "        port: mgmt\n"
)


def test_the_k8s_rendering_receives_the_resolved_probe_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG})
    recorder = _pins_k8s_recorder(monkeypatch, tmp_path, WORKLOAD_PROBE_DOCKERFILE_PORT)
    config, k8s = _pinned_config(tmp_path)

    generate_k8s(config, k8s, LockFile.model_validate({}), local=False)

    assert "port: 9000\n" in recorder.workload_contents["api.yaml"]
    assert "port: mgmt\n" not in recorder.workload_contents["api.yaml"]


WORKLOAD_VARIABLES_BARE_LISTENER = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  web: 5678
containers:
  api:
    variables:
      SERVICE_PORT: ${listeners.web.port}
"""

WORKLOAD_VARIABLES_MAPPING_LISTENER = """
apiVersion: score.dev/v1b1
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  web:
    port: 9090
containers:
  api:
    variables:
      SERVICE_PORT: ${listeners.web.port}
"""

WORKLOAD_VARIABLES_INTERNAL_LISTENER = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  mgmt:
    port: 9000
    internal: true
containers:
  api:
    variables:
      SERVICE_PORT: ${listeners.mgmt.port}
"""


def test_listener_variable_references_resolve_over_both_listener_shapes() -> None:
    for fixture, resolved in (
        (WORKLOAD_VARIABLES_BARE_LISTENER, 5678),
        (WORKLOAD_VARIABLES_MAPPING_LISTENER, 9090),
    ):
        document = parse_workload(yaml.safe_load(fixture), "api.yaml")

        assert document.listener_ports == {"web": resolved}


def test_listener_variable_references_resolve_internal_listeners() -> None:
    document = parse_workload(yaml.safe_load(WORKLOAD_VARIABLES_INTERNAL_LISTENER), "api.yaml")

    assert document.listener_ports == {"mgmt": 9000}


def test_unknown_listener_variable_names_refuse_listing_the_declared_listeners() -> None:
    with pytest.raises(ValidationError, match=r"names no declared listener \(mgmt, web\)"):
        parse_workload(
            {
                "metadata": {"name": "api", "boatyard": {"lock": {"api": "API_IMAGE"}}},
                "listeners": {"web": 5678, "mgmt": {"port": 9000, "internal": True}},
                "containers": {"api": {"variables": {"SERVICE_PORT": "${listeners.admin.port}"}}},
            },
            "api.yaml",
        )


def test_malformed_listener_variable_paths_refuse() -> None:
    malformed = ("${listeners.web}", "${listeners.web.port.extra}", "${listeners.web.hostname}")
    for value in malformed:
        with pytest.raises(ValidationError, match=r"must be \$\{listeners\.<listener>\.port\}"):
            parse_workload(
                {
                    "metadata": {"name": "api", "boatyard": {"lock": {"api": "API_IMAGE"}}},
                    "listeners": {"web": 5678},
                    "containers": {"api": {"variables": {"SERVICE_PORT": value}}},
                },
                "api.yaml",
            )


def test_the_compose_rendering_receives_the_resolved_listener_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(WORKLOAD_VARIABLES_MAPPING_LISTENER, encoding="utf-8")
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    assert "SERVICE_PORT: '9090'\n" in recorder.workload_contents["api.yaml"]
    assert "${listeners." not in recorder.workload_contents["api.yaml"]


WORKLOAD_VARIABLES_DOCKERFILE_PORT = (
    "metadata:\n"
    "  name: api\n"
    "boatyard:\n"
    "  dockerfile:\n"
    "    api: workloads/api/Dockerfile\n"
    "listeners:\n"
    "  mgmt:\n"
    "    port: 9000\n"
    "    internal: true\n"
    "containers:\n"
    "  api:\n"
    "    variables:\n"
    "      SERVICE_PORT: ${listeners.mgmt.port}\n"
)


def test_the_k8s_rendering_receives_the_resolved_listener_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG})
    recorder = _pins_k8s_recorder(monkeypatch, tmp_path, WORKLOAD_VARIABLES_DOCKERFILE_PORT)
    config, k8s = _pinned_config(tmp_path)

    generate_k8s(config, k8s, LockFile.model_validate({}), local=False)

    assert "SERVICE_PORT: '9000'\n" in recorder.workload_contents["api.yaml"]
    assert "${listeners." not in recorder.workload_contents["api.yaml"]


WORKLOAD_SELF_PORT_RESOURCE = """
metadata:
  name: api
boatyard:
  lock:
    api: API_IMAGE
listeners:
  web: 8000
resources:
  self-port:
    type: service-port
    id: api-web
    params:
      workload: api
      port: web
containers:
  api:
    variables:
      SERVICE_PORT: ${resources.self-port.port}
"""


def test_the_self_port_resource_idiom_rides_through_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(WORKLOAD_SELF_PORT_RESOURCE, encoding="utf-8")
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    assert "SERVICE_PORT: ${resources.self-port.port}\n" in recorder.workload_contents["api.yaml"]


SELF_PORT_PROVISIONER = (
    "- uri: template://example/self-port\n  type: service-port\n  state:\n    port: 9090\n  outputs:\n    port: 9090\n"
)

WORKLOAD_IDIOM_SELF_PORT = (
    "apiVersion: score.dev/v1b1\n"
    "metadata:\n"
    "  name: idiom\n"
    "boatyard:\n"
    "  lock:\n"
    "    idiom: API_IMAGE\n"
    "resources:\n"
    "  self-port:\n"
    "    type: service-port\n"
    "    id: idiom-main\n"
    "containers:\n"
    "  idiom:\n"
    "    variables:\n"
    "      SERVICE_PORT: ${resources.self-port.port}\n"
)


def test_listener_variable_ports_render_as_the_self_port_idiom_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_unless_pinned_score_compose()
    score_dir = tmp_path / "stack" / "score"
    (score_dir / "compose").mkdir(parents=True)
    (score_dir / "api.yaml").write_text(WORKLOAD_VARIABLES_MAPPING_LISTENER, encoding="utf-8")
    (score_dir / "idiom.yaml").write_text(WORKLOAD_IDIOM_SELF_PORT, encoding="utf-8")
    (score_dir / "compose" / "self-port.yaml").write_text(SELF_PORT_PROVISIONER, encoding="utf-8")
    config = _generated_config(
        tmp_path,
        ["api.yaml", "idiom.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"options": {"main": {"workloads": ["api.yaml", "idiom.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api:1@" + DIGEST, **ALPINE_LOCK}),
    )

    document = yaml.safe_load(
        (tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml").read_text(encoding="utf-8")
    )
    assert document["services"]["api"]["environment"] == {"SERVICE_PORT": "9090"}
    assert document["services"]["idiom"]["environment"] == {"SERVICE_PORT": "9090"}


def test_workload_resources_parse_typed() -> None:
    document = parse_workload({"resources": {"db": {"type": "example-db"}}, "metadata": {"name": "api"}}, "api.yaml")

    assert document.resources["db"].type == "example-db"


DOCKERFILE_SOURCES_API: dict[str, Yaml] = {
    "metadata": {"name": "api"},
    "boatyard": {"dockerfile": {"api": "workloads/api/Dockerfile"}},
    "containers": {"api": {}},
}


class TestContainerSources:
    def test_container_sources_exposes_the_declared_edges(self) -> None:
        document = parse_workload(DOCKERFILE_SOURCES_API, "api.yaml")

        assert document.container_sources() == {"api": ("dockerfile", "workloads/api/Dockerfile")}

    def test_lock_sources_parse_beside_dockerfile_sources(self) -> None:
        document = parse_workload(
            {
                "metadata": {"name": "api"},
                "boatyard": {
                    "dockerfile": {"api": "workloads/api/Dockerfile"},
                    "lock": {"sidecar": "KEYCLOAK_IMAGE"},
                },
                "containers": {"api": {}, "sidecar": {}},
            },
            "api.yaml",
        )

        assert document.container_sources() == {
            "api": ("dockerfile", "workloads/api/Dockerfile"),
            "sidecar": ("lock", "KEYCLOAK_IMAGE"),
        }

    def test_a_container_without_a_source_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"container 'sidecar' carries no source in the boatyard block"):
            parse_workload(
                {
                    "metadata": {"name": "api"},
                    "boatyard": {"dockerfile": {"api": "workloads/api/Dockerfile"}},
                    "containers": {"api": {}, "sidecar": {}},
                },
                "api.yaml",
            )

    def test_both_source_maps_naming_one_container_refuse(self) -> None:
        with pytest.raises(ValidationError, match=r"container 'api' is named by both dockerfile and lock"):
            parse_workload(
                {
                    "metadata": {"name": "api"},
                    "boatyard": {
                        "dockerfile": {"api": "workloads/api/Dockerfile"},
                        "lock": {"api": "KEYCLOAK_IMAGE"},
                    },
                    "containers": {"api": {}},
                },
                "api.yaml",
            )

    def test_a_source_map_naming_no_container_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"names no container 'ghost'"):
            parse_workload(
                {
                    "metadata": {"name": "api"},
                    "boatyard": {"lock": {"ghost": "KEYCLOAK_IMAGE"}},
                    "containers": {},
                },
                "api.yaml",
            )

    def test_an_empty_source_value_refuses(self) -> None:
        with pytest.raises(ValidationError):
            parse_workload(
                {
                    "metadata": {"name": "api"},
                    "boatyard": {"dockerfile": {"api": ""}},
                    "containers": {"api": {}},
                },
                "api.yaml",
            )


def test_workload_resources_without_a_type_refuse() -> None:
    with pytest.raises(ValidationError, match=r"resources\.db\.type"):
        parse_workload({"resources": {"db": {"id": "main"}}, "metadata": {"name": "api"}}, "api.yaml")


def test_adapted_probe_bearing_workloads_register_the_healthcheck_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        None,
        {"API_IMAGE": "docker.io/library/api:1@" + DIGEST},
        {
            "stack/score/api.yaml": WORKLOAD_WITH_READINESS,
        },
        generated=API_SERVICE_RENDERING,
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_restart_values_outside_the_compose_enum_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\n  annotations:\n    boatyard/restart: always-please\ncontainers: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"restart policies"):
        _bundle_config(tmp_path)


def test_restart_annotation_with_an_enum_value_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        None,
        {},
        {
            "stack/score/api.yaml": (
                "metadata:\n  name: api\n  annotations:\n    boatyard/restart: on-failure\ncontainers: {}\n"
            ),
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_bare_literal_workload_image_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\ncontainers:\n  api:\n    image: ghcr.io/example/api:1\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match=r"image is never authored"):
        _bundle_config(tmp_path)


def test_an_authored_image_slot_refuses_like_a_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\ncontainers:\n  api:\n    image: ${API_IMAGE}\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match=r"image is never authored"):
        _bundle_config(tmp_path)


def test_a_workload_without_metadata_name_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("containers: {}\n", encoding="utf-8")

    with pytest.raises(ValidationError, match=r"metadata\.name must be 'api'"):
        _bundle_config(tmp_path)


def test_a_workload_name_mismatching_the_file_stem_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: apis\ncontainers: {}\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError, match=r"metadata\.name must be 'api'"):
        _bundle_config(tmp_path)


def test_consumer_interpolated_names_must_match_declared_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\nboatyard:\n  consumer_interpolated:\n    - /etc/rathole/client.toml\ncontainers: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"names no declared files"):
        _bundle_config(tmp_path)


WORKLOAD_WITH_INTERPOLATED_FILE = """\
metadata:
  name: rathole-client
boatyard:
  consumer_interpolated:
    - /etc/rathole/client.toml
  lock:
    rathole-client: ALPINE_IMAGE
containers:
  rathole-client:
    files:
      /etc/rathole/client.toml:
        content: |
          remote_addr = "$${TUNNEL_CONTROL_ENDPOINT:?err}"
"""

INTERPOLATED_GENERATED_COMPOSE = (
    "services:\n"
    "  rathole-client-rathole-client:\n"
    "    image: ghcr.io/org/rathole:1\n"
    "    volumes:\n"
    "      - type: bind\n"
    "        source: .score-compose/mounts/files/rathole-client-files-client.toml\n"
    "        target: /etc/rathole/client.toml\n"
)


class MaterializingComposeRecorder(ComposeBashRecorder):
    def __init__(self, generated: str, materialized: Path, text: str) -> None:
        super().__init__(generated)
        self.materialized = materialized
        self.text = text

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        super().run(command, cwd=cwd, env=env)
        if command.startswith("score-compose generate"):
            self.materialized.parent.mkdir(parents=True, exist_ok=True)
            self.materialized.write_text(self.text, encoding="utf-8")


def test_consumer_interpolated_binds_land_undoubled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    score_dir = tmp_path / "stack" / "score"
    score_dir.mkdir(parents=True)
    (score_dir / "rathole-client.yaml").write_text(WORKLOAD_WITH_INTERPOLATED_FILE, encoding="utf-8")
    recorder = MaterializingComposeRecorder(
        INTERPOLATED_GENERATED_COMPOSE,
        tmp_path / "stack/generated/compose/.score-compose/mounts/files/rathole-client-files-client.toml",
        'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n',
    )
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _generated_config(
        tmp_path,
        ["rathole-client.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"rathole": {"options": {"client": {"workloads": ["rathole-client.yaml"]}}}},
    )

    score_compose_module.generate_compose(
        config,
        _lock_for_images(ALPINE_LOCK),
    )

    output = tmp_path / "stack" / "generated" / "compose" / "compose.rathole-client.yaml"
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    service = document["services"]["rathole-client"]
    assert service["configs"] == [{"source": "rathole-client-files-client.toml", "target": "/etc/rathole/client.toml"}]
    assert (
        document["configs"]["rathole-client-files-client.toml"]["content"]
        == 'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n'
    )


def test_gpu_compose_templates_register_per_the_renderings_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
    config = _generated_config(
        tmp_path,
        ["api.yaml"],
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={
            "core": {
                "options": {
                    "cuda": {"workloads": ["api.yaml"], "vars": {"TORCH_DEVICE": "cuda"}},
                    "rocm": {"workloads": ["api.yaml"], "vars": {"TORCH_DEVICE": "rocm"}},
                }
            },
        },
    )
    config.images = {
        "api": Image(name="api", dockerfile="workloads/api/Dockerfile", axis="TORCH_DEVICE", variants=["cuda", "rocm"])
    }

    templates = _compose_init_templates(monkeypatch, tmp_path, config, {}, {})

    assert templates == [
        ["probe-adapter-healthchecks.tpl", "restart.tpl", "gpu-cuda.tpl"],
        ["probe-adapter-healthchecks.tpl", "restart.tpl", "gpu-rocm.tpl"],
    ]


def test_k8s_builtin_templates_register_on_every_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
    output = Path("stack/generated/k8s/manifests.yaml")
    recorder = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    monkeypatch.setattr(score_k8s_module, "bash_output", HelmRecorder().run)
    config = _generated_config(tmp_path, ["api.yaml"], k8s={"workloads": ["api.yaml"]})
    _write_pins(tmp_path, {})

    assert config.k8s is not None
    generate_k8s(config, config.k8s, LockFile.model_validate({}), local=False)

    init = next(command for command in recorder.commands if command.startswith("score-k8s init"))
    assert [Path(token).name for token in init.split() if token.endswith(".tpl")] == [
        "gpu-pod.tpl",
        "scale-to-zero.tpl",
    ]


def test_k8s_instance_suffixes_are_preseeded_deterministically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
    output = Path("stack/generated/k8s/manifests.yaml")
    recorder = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    monkeypatch.setattr(score_k8s_module, "bash_output", HelmRecorder().run)
    config = _generated_config(tmp_path, ["api.yaml"], k8s={"workloads": ["api.yaml"]})
    _write_pins(tmp_path, {})

    assert config.k8s is not None
    generate_k8s(config, config.k8s, LockFile.model_validate({}), local=False)

    seeded = yaml.safe_load(recorder.state_at_generate[output.name])
    assert seeded["workloads"]["api"]["instance_suffix"] == "-" + sha256(b"api").hexdigest()[:10]


PINNED_TAG = "tree-" + "e" * 40
LOCAL_TAG = "tree-" + "f" * 40
SHA_WORKLOAD = DOCKERFILE_SOURCE_API


def _pins_k8s_recorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workload: str, output_name: str = "manifests.yaml"
) -> K8sBashRecorder:
    (tmp_path / "stack" / "score").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(workload, encoding="utf-8")
    output = Path("stack/generated/k8s") / output_name
    recorder = K8sBashRecorder(tmp_path / output, tmp_path / "stack/generated/k8s/.score-k8s/state.yaml")
    monkeypatch.setattr(score_module, "bash", recorder.run)
    monkeypatch.setattr(score_k8s_module, "bash_output", HelmRecorder().run)
    return recorder


def _pinned_config(tmp_path: Path) -> tuple[ConfigFile, K8sConfig]:
    config = _generated_config(tmp_path, ["api.yaml"], repository="ghcr.io/org/repo", k8s={"workloads": ["api.yaml"]})
    config.images = {"api": Image(name="api", dockerfile="workloads/api/Dockerfile")}
    assert config.k8s is not None
    return config, config.k8s


def _refuse_the_tree_walk(_self: Image, _root: Path) -> str:
    raise AssertionError("the pinned k8s rendering never walks the tree")


def test_k8s_generation_bakes_the_pins_file_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG})
    recorder = _pins_k8s_recorder(monkeypatch, tmp_path, SHA_WORKLOAD)
    config, k8s = _pinned_config(tmp_path)
    monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)

    generate_k8s(config, k8s, LockFile.model_validate({}), local=False)

    assert recorder.command_envs["generate"] is not None
    assert recorder.command_envs["generate"]["API_SHA"] == PINNED_TAG
    assert f"image: ghcr.io/org/repo/api:{PINNED_TAG}" in recorder.workload_contents["api.yaml"]


def test_k8s_generation_without_a_pins_file_refuses_naming_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pins_k8s_recorder(monkeypatch, tmp_path, SHA_WORKLOAD)
    config, k8s = _pinned_config(tmp_path)

    with pytest.raises(ValueError, match=r"boatyard\.k8s-pins\.yml: the k8s pins file is required"):
        generate_k8s(config, k8s, LockFile.model_validate({}), local=False)


def test_k8s_generation_refuses_a_workload_sha_slot_no_pin_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG})
    workload = (
        "metadata:\n"
        "  name: api\n"
        "boatyard:\n"
        "  dockerfile:\n"
        "    api: workloads/api/Dockerfile\n"
        "    sidecar: workloads/worker/Dockerfile\n"
        "containers:\n"
        "  api: {}\n"
        "  sidecar: {}\n"
    )
    _pins_k8s_recorder(monkeypatch, tmp_path, workload)
    config = _generated_config(tmp_path, ["api.yaml"], repository="ghcr.io/org/repo", k8s={"workloads": ["api.yaml"]})
    config.images = {
        "api": Image(name="api", dockerfile="workloads/api/Dockerfile"),
        "worker": Image(name="worker", dockerfile="workloads/worker/Dockerfile"),
    }
    assert config.k8s is not None

    with pytest.raises(ValueError, match=r"boatyard\.k8s-pins\.yml: lacks pins for WORKER_SHA"):
        generate_k8s(config, config.k8s, LockFile.model_validate({}), local=False)


ISSUER_PROVISIONER = (
    "- uri: template://example/issuer\n"
    "  type: example-issuer\n"
    "  manifests:\n"
    "    - name: configurator\n"
    "      image: {dockerfile: workloads/initializer/Dockerfile}\n"
)

WORKLOAD_WITH_ISSUER = (
    "metadata:\n"
    "  name: api\n"
    "boatyard:\n"
    "  dockerfile:\n"
    "    api: workloads/api/Dockerfile\n"
    "resources:\n"
    "  issuer:\n"
    "    type: example-issuer\n"
    "containers:\n"
    "  api: {}\n"
)


def _issuer_config(tmp_path: Path) -> tuple[ConfigFile, K8sConfig]:
    config = _generated_config(tmp_path, ["api.yaml"], repository="ghcr.io/org/repo", k8s={"workloads": ["api.yaml"]})
    config.images = {
        "api": Image(name="api", dockerfile="workloads/api/Dockerfile"),
        "initializer": Image(name="initializer", dockerfile="workloads/initializer/Dockerfile"),
    }
    assert config.k8s is not None
    return config, config.k8s


def test_k8s_generation_refuses_a_provisioner_sha_slot_no_pin_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG})
    _pins_k8s_recorder(monkeypatch, tmp_path, WORKLOAD_WITH_ISSUER)
    (tmp_path / "stack" / "score" / "k8s").mkdir()
    (tmp_path / "stack" / "score" / "k8s" / "issuer.yaml").write_text(ISSUER_PROVISIONER, encoding="utf-8")
    config, k8s = _issuer_config(tmp_path)

    with pytest.raises(ValueError, match=r"boatyard\.k8s-pins\.yml: lacks pins for INITIALIZER_SHA"):
        generate_k8s(config, k8s, LockFile.model_validate({}), local=False)


def test_k8s_generation_passes_provisioner_pins_into_the_generate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pins(tmp_path, {"API_SHA": PINNED_TAG, "INITIALIZER_SHA": PINNED_TAG})
    recorder = _pins_k8s_recorder(monkeypatch, tmp_path, WORKLOAD_WITH_ISSUER)
    (tmp_path / "stack" / "score" / "k8s").mkdir()
    (tmp_path / "stack" / "score" / "k8s" / "issuer.yaml").write_text(ISSUER_PROVISIONER, encoding="utf-8")
    config, k8s = _issuer_config(tmp_path)

    generate_k8s(config, k8s, LockFile.model_validate({}), local=False)

    assert recorder.command_envs["generate"] is not None
    assert recorder.command_envs["generate"]["INITIALIZER_SHA"] == PINNED_TAG


def test_local_k8s_generation_derives_working_tree_tags_without_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _pins_k8s_recorder(monkeypatch, tmp_path, SHA_WORKLOAD, output_name="manifests.local.yaml")
    config, k8s = _pinned_config(tmp_path)

    def _local_tree_sha(_self: Image, _root: Path) -> str:
        return LOCAL_TAG

    monkeypatch.setattr(Image, "compute_sha", _local_tree_sha)

    generate_k8s(config, k8s, LockFile.model_validate({}), local=True)

    assert recorder.command_envs["generate"] is not None
    assert recorder.command_envs["generate"]["API_SHA"] == LOCAL_TAG


A0_ESCAPED_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ${SPIKE_IMAGE:?err}
    files:
      /etc/spike/slots.toml:
        content: |
          doubled_err = "$${SPIKE_SET:?err}"
          doubled_default = "$${SPIKE_UNSET:-fallback}"
          doubled_bare = "$${SPIKE_SET}"
          lone_dollar = "$$"
          dotted = "${metadata.name}"
"""

A0_MATERIALIZED = (
    'doubled_err = "${SPIKE_SET:?err}"\n'
    'doubled_default = "${SPIKE_UNSET:-fallback}"\n'
    'doubled_bare = "${SPIKE_SET}"\n'
    'lone_dollar = "$"\n'
    'dotted = "spike"\n'
)

A0_ENV_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ${SPIKE_BARE_SET}
    variables:
      DOUBLED_ERR: $${SPIKE_SET:?err}
      DOUBLED_DEFAULT: $${SPIKE_UNSET:-fallback}
"""

A0_REFUSED_FILES_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ghcr.io/example/spike:1.0
    files:
      /etc/spike/slots.toml:
        content: |
          err = "${SPIKE_SET:?err}"
"""

A0_REFUSED_VARIABLES_WORKLOAD = """\
apiVersion: score.dev/v1b1
metadata:
  name: spike
containers:
  spike:
    image: ghcr.io/example/spike:1.0
    variables:
      CONTROL: ${SPIKE_SET}
"""


def _skip_unless_pinned_score_compose() -> None:
    if shutil.which("score-compose") is None:
        pytest.skip("score-compose is not on PATH; the A0 pin needs the pinned binary")
    pinned = ConfigFile.model_fields["score_compose_version"].default
    if f"score-compose {pinned} (" not in bash_output("score-compose version"):
        pytest.skip(f"score-compose on PATH is not the pinned {pinned}")


def test_files_placeholders_materialize_undoubled(tmp_path: Path) -> None:
    _skip_unless_pinned_score_compose()
    (tmp_path / "escaped.yaml").write_text(A0_ESCAPED_WORKLOAD, encoding="utf-8")
    bash("score-compose init --project spike --no-sample", cwd=tmp_path)
    bash(
        "score-compose generate escaped.yaml --output escaped-compose.yaml",
        cwd=tmp_path,
        env={**os.environ, "SPIKE_SET": "hello", "SPIKE_IMAGE": "ghcr.io/example/spike:1.0"},
    )

    materialized = tmp_path / ".score-compose/mounts/files/spike-files-slots.toml"
    assert materialized.read_text(encoding="utf-8") == A0_MATERIALIZED

    artifact = yaml.safe_load((tmp_path / "escaped-compose.yaml").read_text(encoding="utf-8"))
    service = artifact["services"]["spike-spike"]
    assert service["image"] == "${SPIKE_IMAGE:?err}"
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": ".score-compose/mounts/files/spike-files-slots.toml",
            "target": "/etc/spike/slots.toml",
        }
    ]


def test_variable_placeholders_refuse_or_stay_escaped(tmp_path: Path) -> None:
    _skip_unless_pinned_score_compose()
    (tmp_path / "env.yaml").write_text(A0_ENV_WORKLOAD, encoding="utf-8")
    (tmp_path / "refused-files.yaml").write_text(A0_REFUSED_FILES_WORKLOAD, encoding="utf-8")
    (tmp_path / "refused-variables.yaml").write_text(A0_REFUSED_VARIABLES_WORKLOAD, encoding="utf-8")
    bash("score-compose init --project spike --no-sample", cwd=tmp_path)
    bash(
        "score-compose generate env.yaml --output env-compose.yaml",
        cwd=tmp_path,
        env={**os.environ, "SPIKE_BARE_SET": "ghcr.io/example/set:2.0"},
    )

    service = yaml.safe_load((tmp_path / "env-compose.yaml").read_text(encoding="utf-8"))["services"]["spike-spike"]
    assert service["image"] == "${SPIKE_BARE_SET}"
    assert service["environment"] == {
        "DOUBLED_ERR": "$${SPIKE_SET:?err}",
        "DOUBLED_DEFAULT": "$${SPIKE_UNSET:-fallback}",
    }

    with pytest.raises(CalledProcessError):
        bash("score-compose generate refused-files.yaml --output refused.yaml", cwd=tmp_path)
    with pytest.raises(CalledProcessError):
        bash("score-compose generate refused-variables.yaml --output refused.yaml", cwd=tmp_path)


def _verb_repo(tmp_path: Path) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\nboatyard:\n  lock:\n    api: LOKI_IMAGE\ncontainers:\n  api: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "boatyard.lock.yml").write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": {
                "LOKI_IMAGE": {
                    "index": LOKI_INDEX,
                    "platforms": {"linux/amd64": LOKI_MANIFEST},
                },
                "ALPINE_IMAGE": {
                    "index": ALPINE_LOCK["ALPINE_IMAGE"],
                    "platforms": {"linux/amd64": ALPINE_LOCK["ALPINE_IMAGE"]},
                },
            },
        }),
        encoding="utf-8",
    )
    (tmp_path / "boatyard.yml").write_text(
        yaml.safe_dump({
            "requires": ">=0",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "platforms": ["linux/amd64"],
            "base_images": {
                "LOKI_IMAGE": "docker.io/grafana/loki:3.5.0",
                "ALPINE_IMAGE": "docker.io/library/alpine:3.21.0",
            },
            "services": {"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
        }),
        encoding="utf-8",
    )


def test_generate_score_writes_the_mint_list_from_the_k8s_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("chart: {}\n", encoding="utf-8")
    (tmp_path / "boatyard.lock.yml").write_text(
        yaml.safe_dump({"charts": {"LOKI_CHART": PINNED_CHART}}), encoding="utf-8"
    )
    config = _root_config(repository="ghcr.io/org/repo", k8s={"workloads": ["api.yaml"], "charts": {"loki": CHART_REF}})
    config.root = tmp_path

    def _stub_generate_k8s(*args: object, **kwargs: object) -> tuple[Path, set[str]]:
        return tmp_path / "manifests.yaml", {"PUBLIC_URL", "S3_ACCESS_KEY"}

    _offline_tool_ensure(monkeypatch, on_path=None)
    monkeypatch.setattr(score_generate_module, "generate_k8s", _stub_generate_k8s)

    score_generate_module.generate_artifacts(config)

    mint = (tmp_path / "stack" / "generated" / "secrets.txt").read_text(encoding="utf-8")
    assert mint == "# Generated by generate-score from the k8s rendering's consumed keys\nPUBLIC_URL\nS3_ACCESS_KEY\n"


def _k8s_only_repo(tmp_path: Path) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
    (tmp_path / "boatyard.yml").write_text(
        "requires: '>=0'\nproject_name: example\nrepository: ghcr.io/org/repo\n"
        "services:\n  ext:\n    ref: oci://ghcr.io/org/ext:0.1.0\n"
        "k8s:\n  workloads:\n    - api.yaml\n",
        encoding="utf-8",
    )


def test_generate_score_on_a_compose_only_repo_derives_compose_and_requests_only_the_compose_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _verb_repo(tmp_path)
    recorder = WorkloadContentsRecorder()

    monkeypatch.setattr(score_module, "bash", recorder.run)
    _offline_tool_ensure(monkeypatch, on_path=None)
    monkeypatch.chdir(tmp_path)

    score_verb_module.generate_score()

    installed = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("Install ")]
    assert installed == ["score-compose"]
    assert (tmp_path / "stack" / "generated" / "compose" / "compose.core-main.yaml").is_file()
    mint = (tmp_path / "stack" / "generated" / "secrets.txt").read_text(encoding="utf-8")
    assert mint == "# Generated by generate-score from the k8s rendering's consumed keys\n"


def test_generate_artifacts_on_a_k8s_only_repo_requests_no_compose_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("chart: {}\n", encoding="utf-8")
    (tmp_path / "boatyard.lock.yml").write_text(
        yaml.safe_dump({"charts": {"LOKI_CHART": PINNED_CHART}}), encoding="utf-8"
    )
    config = _root_config(
        repository="ghcr.io/org/repo",
        k8s={"workloads": ["api.yaml"], "charts": {"loki": CHART_REF}},
    )
    config.root = tmp_path

    def _stub_generate_k8s(*args: object, **kwargs: object) -> tuple[Path, set[str]]:
        return tmp_path / "manifests.yaml", set()

    _offline_tool_ensure(monkeypatch, on_path=None)
    monkeypatch.setattr(score_generate_module, "generate_k8s", _stub_generate_k8s)

    outputs = score_generate_module.generate_artifacts(config)

    installed = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("Install ")]
    assert installed == ["score-k8s", "helm"]
    assert outputs == [tmp_path / "manifests.yaml", tmp_path / "stack" / "generated" / "secrets.txt"]


def test_generate_score_on_a_k8s_only_repo_derives_the_k8s_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _k8s_only_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="the image lock is required"):
        score_verb_module.generate_score()


def test_generate_score_on_a_compose_only_repo_refuses_an_explicit_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verb_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.BadParameter, match="pass --target compose"):
        score_verb_module.generate_score(target="both")


def test_generate_score_on_a_k8s_only_repo_refuses_an_explicit_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _k8s_only_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.BadParameter, match="pass --target k8s"):
        score_verb_module.generate_score(target="both")


def _generate_compose_with_stubbed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_path: str | None, version_output: str
) -> list[str]:
    _verb_repo(tmp_path)
    monkeypatch.setattr(score_module, "bash", WorkloadContentsRecorder().run)
    commands = _offline_tool_ensure(monkeypatch, on_path=on_path, version_output=version_output)
    monkeypatch.chdir(tmp_path)

    score_verb_module.generate_score(target="compose")

    return commands


def test_generation_installs_a_tool_missing_from_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _generate_compose_with_stubbed_tools(tmp_path, monkeypatch, on_path=None, version_output="")

    pinned = ConfigFile.model_fields["score_compose_version"].default
    assert commands[0].startswith(
        f"curl -fsSL https://github.com/score-spec/score-compose/releases/download/{pinned}/"
        f"score-compose_{pinned}_linux_amd64.tar.gz -o "
    )


def test_generation_reinstalls_a_tool_on_a_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _generate_compose_with_stubbed_tools(
        tmp_path, monkeypatch, on_path="/bin/true", version_output="score-compose 0.0.0"
    )

    pinned = ConfigFile.model_fields["score_compose_version"].default
    assert commands[0].startswith(
        f"curl -fsSL https://github.com/score-spec/score-compose/releases/download/{pinned}/"
        f"score-compose_{pinned}_linux_amd64.tar.gz -o "
    )


def test_generation_skips_install_when_the_pinned_version_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = ConfigFile.model_fields["score_compose_version"].default
    commands = _generate_compose_with_stubbed_tools(
        tmp_path, monkeypatch, on_path="/bin/true", version_output=f"score-compose {pinned}"
    )

    assert commands == []


def test_generate_score_refuses_compose_on_a_k8s_only_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _k8s_only_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        typer.BadParameter, match="--target compose: this config declares no local service entries \\(k8s-only\\)"
    ):
        score_verb_module.generate_score(target="compose")
