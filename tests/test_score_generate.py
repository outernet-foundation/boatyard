import os
import shutil
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest
import typer
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from pydantic import ValidationError

from boatyard.config import config as config_module
from boatyard import score as score_module
from boatyard import score_compose as score_compose_module
from boatyard import score_k8s as score_k8s_module
from boatyard.verbs import score as score_verb_module
from boatyard.config.config import ConfigFile, WorkloadDocument
from boatyard.config.lock import LockFile
from boatyard.score import SecretsDeclaration, write_file
from boatyard.score_compose import BindVolume, ComposeService
from boatyard.score_k8s import generate_k8s

DIGEST = "sha256:" + "a" * 64


def _root_config(**sections: object) -> ConfigFile:
    payload: dict[str, object] = {"requires": ">=0.1", "project_name": "example"}
    payload.update(sections)
    if "services" in payload and "gateway" not in payload:
        payload["gateway"] = 8080
    return ConfigFile.model_validate(payload)


def _empty_secrets() -> SecretsDeclaration:
    return SecretsDeclaration.model_validate({})


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
MIRRORED_CHART = f"{MIRROR_PREFIX}/ghcr.io/grafana/helm-charts/loki@{DIGEST}"
PLATFORM_DIGEST = "sha256:" + "b" * 64
LOKI_INDEX = f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{DIGEST}"
LOKI_MANIFEST = f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{PLATFORM_DIGEST}"
LOCK_IMAGES = {
    "GRAFANA_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/grafana/grafana:11.6.0@" + DIGEST,
    "LOKI_CHART": MIRRORED_CHART,
}
VALUES_PATH = Path("stack/score/k8s/values/loki.yaml")


class K8sBashRecorder:
    def __init__(self, output_path: Path, state_path: Path) -> None:
        self.output_path = output_path
        self.state_path = state_path
        self.commands: list[str] = []
        self.state_at_generate: dict[str, str] = {}

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.commands.append(command)
        if command.startswith("score-k8s init"):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text("workloads: {}\n", encoding="utf-8")
        if command.startswith("score-k8s generate"):
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
    lock = LockFile.model_validate({
        "images": {name: {"index": ref} for name, ref in images.items() if not name.endswith("_CHART")},
        "charts": {name: ref for name, ref in images.items() if name.endswith("_CHART")},
    })
    generate_k8s(config, config.k8s, lock, {}, local=False, secrets=_empty_secrets())
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
            "charts": {"LOKI_CHART": MIRRORED_CHART},
        })

    def _compose_seam(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, platforms: list[str]) -> str:
        (tmp_path / "stack" / "score").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "api.yaml").write_text(
            "metadata:\n  name: api\ncontainers:\n  api:\n    image: ${LOKI_IMAGE}\n", encoding="utf-8"
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
            "    workloads:\n"
            "      - api.yaml\n",
            encoding="utf-8",
        )
        (tmp_path / "stack" / "secrets.yaml").write_text("", encoding="utf-8")
        recorder = WorkloadContentsRecorder()

        def _skip_tools(requirements: list[score_verb_module.ToolRequirement]) -> None:
            return None

        monkeypatch.setattr(score_module, "bash", recorder.run)
        monkeypatch.setattr(score_verb_module, "ensure_tools", _skip_tools)
        monkeypatch.chdir(tmp_path)
        score_verb_module.generate_score(target="compose")
        return recorder.workload_contents["api.yaml"]

    def test_compose_generation_receives_the_selected_platform_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        contents = self._compose_seam(monkeypatch, tmp_path, platforms=["linux/amd64"])

        assert f"image: {LOKI_MANIFEST}" in contents

    def test_compose_generation_selects_the_platform_the_environment_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "boatyard.values.yaml").write_text("platform: linux/arm64\n", encoding="utf-8")

        contents = self._compose_seam(monkeypatch, tmp_path, platforms=["linux/amd64", "linux/arm64"])

        assert f"image: {LOKI_INDEX}" in contents

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
        lock = self._multi_arch_lock()
        assert config.k8s is not None

        generate_k8s(config, config.k8s, lock, {}, local=False, secrets=_empty_secrets())

        resolved = yaml.safe_load(helm.values_contents[0])
        assert resolved["image"] == LOKI_INDEX


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

API_SERVICE_RENDERING = "services:\n  api-api:\n    image: ghcr.io/org/api:1\n"


def _generate_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    generated: str,
    secrets: SecretsDeclaration,
    workload: str = "api",
    container: str | None = None,
) -> Path:
    container_name = workload if container is None else container
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / f"{workload}.yaml").write_text(
        f"metadata:\n  name: {workload}\ncontainers:\n  {container_name}: {{}}\n", encoding="utf-8"
    )
    (tmp_path / "workloads" / "api").mkdir(parents=True)
    (tmp_path / "workloads" / "api" / "config.yaml").write_text("key: ${FOO}\n", encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"workloads": [f"{workload}.yaml"]}},
    )
    config.root = tmp_path

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK), {}, secrets=secrets)
    return tmp_path / "stack" / "generated" / "compose" / "compose.core.yaml"


def test_generate_compose_collapses_doubled_names_and_inlines_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, GENERATED_COMPOSE, _empty_secrets())

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["core-api", "core-pf-postgres"]
    api = document["services"]["core-api"]
    assert api["image"] == "ghcr.io/org/api:1"
    assert "volumes" not in api
    assert api["networks"] == {"core-net": {"aliases": ["api"]}}
    assert api["configs"] == [{"source": "core-config.yaml", "target": "/etc/placeframe/config.yaml"}]
    assert document["configs"] == {"core-config.yaml": {"content": "key: $${FOO}\n"}}
    assert document["networks"] == {"core-net": {}}


def test_generate_compose_collapses_doubled_names_with_no_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  gateway-gateway:\n    image: ghcr.io/org/gateway:1\n",
        _empty_secrets(),
        workload="gateway",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["core-gateway"]
    assert document["services"]["core-gateway"]["image"] == "ghcr.io/org/gateway:1"
    assert "configs" not in document
    assert document["networks"] == {"core-net": {}}


def test_generate_compose_refuses_a_rename_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    colliding = GENERATED_COMPOSE + "  api:\n    image: ghcr.io/org/api:2\n"

    with pytest.raises(ValueError, match="claimed by more than one"):
        _generate_compose(monkeypatch, tmp_path, colliding, _empty_secrets())


def test_generate_compose_refuses_a_container_and_residual_service_sharing_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = (
        "services:\n  api-sidecar:\n    image: ghcr.io/org/api:1\n  sidecar:\n    image: ghcr.io/org/sidecar:1\n"
    )

    with pytest.raises(ValueError, match="claimed by more than one"):
        _generate_compose(monkeypatch, tmp_path, generated, _empty_secrets(), workload="api", container="sidecar")


def test_generate_compose_carries_a_coincidentally_prefixed_service_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = GENERATED_COMPOSE + "  api-migrations:\n    image: ghcr.io/org/migrate:1\n"

    output = _generate_compose(monkeypatch, tmp_path, generated, _empty_secrets())

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["core-api", "core-pf-postgres", "core-api-migrations"]


def test_generate_compose_strips_the_workload_prefix_from_differing_container_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(
        monkeypatch,
        tmp_path,
        "services:\n  tunnel-rathole-rathole-client:\n    image: ghcr.io/org/rathole-client:1\n",
        _empty_secrets(),
        workload="tunnel-rathole",
        container="rathole-client",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["core-rathole-client"]
    assert document["services"]["core-rathole-client"]["image"] == "ghcr.io/org/rathole-client:1"


def test_generate_compose_refuses_a_container_name_shared_across_workloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "alpha.yaml").write_text(
        "metadata:\n  name: alpha\ncontainers:\n  sidecar: {}\n", encoding="utf-8"
    )
    (tmp_path / "stack" / "score" / "beta.yaml").write_text(
        "metadata:\n  name: beta\ncontainers:\n  sidecar: {}\n", encoding="utf-8"
    )
    recorder = ComposeBashRecorder(
        "services:\n  alpha-sidecar:\n    image: ghcr.io/org/alpha:1\n  beta-sidecar:\n    image: ghcr.io/org/beta:1\n"
    )
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"workloads": ["alpha.yaml", "beta.yaml"]}},
    )
    config.root = tmp_path

    with pytest.raises(ValueError, match="claimed by more than one"):
        score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK), {}, secrets=_empty_secrets())


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


def test_generate_compose_renames_the_barrier_and_its_references_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _generate_compose(monkeypatch, tmp_path, COMPOSE_WITH_BARRIER, _empty_secrets())

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert list(document["services"]) == ["core-api", "core-pf-postgres", "core-wait-for-resources"]
    assert document["services"]["core-api"]["depends_on"] == {
        "core-wait-for-resources": {"condition": "service_started"}
    }
    assert document["services"]["core-pf-postgres"]["depends_on"] == ["core-wait-for-resources"]
    assert document["services"]["core-wait-for-resources"]["image"] == "${CORE__LOCK__ALPINE_IMAGE:?err}"


def test_generate_compose_barrier_names_differ_per_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\ncontainers:\n  api: {}\n", encoding="utf-8"
    )
    (tmp_path / "stack" / "score" / "edge.yaml").write_text(
        "metadata:\n  name: edge\ncontainers: {}\n", encoding="utf-8"
    )
    recorder = ComposeBashRecorder(COMPOSE_WITH_BARRIER)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={
            "core": {"workloads": ["api.yaml"]},
            "edge": {"workloads": ["edge.yaml"]},
        },
    )
    config.root = tmp_path

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK), {}, secrets=_empty_secrets())

    core = yaml.safe_load((tmp_path / "stack/generated/compose/compose.core.yaml").read_text(encoding="utf-8"))
    edge = yaml.safe_load((tmp_path / "stack/generated/compose/compose.edge.yaml").read_text(encoding="utf-8"))
    assert core["services"]["core-wait-for-resources"]["image"] == "${CORE__LOCK__ALPINE_IMAGE:?err}"
    assert edge["services"]["edge-wait-for-resources"]["image"] == "${EDGE__LOCK__ALPINE_IMAGE:?err}"


class WorkloadContentsRecorder:
    def __init__(self) -> None:
        self.workload_contents: dict[str, str] = {}

    def run(self, command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        if command.startswith("score-compose generate"):
            for argument in command.split()[2:]:
                if argument.startswith("--output"):
                    break
                path = Path(argument)
                self.workload_contents[path.name] = path.read_text(encoding="utf-8")
            output = Path(command.rsplit("--output ", 1)[1].split()[0])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(API_SERVICE_RENDERING, encoding="utf-8")


def test_workload_image_slots_resolve_from_the_lock_into_the_rendered_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\ncontainers:\n  api:\n    image: ${API_IMAGE}\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"workloads": ["api.yaml"]}},
    )
    config.root = tmp_path

    score_compose_module.generate_compose(
        config,
        _lock_for_images({"API_IMAGE": "ghcr.io/org/api@sha256:" + "a" * 64, **ALPINE_LOCK}),
        {},
        secrets=_empty_secrets(),
    )

    assert (
        recorder.workload_contents["api.yaml"]
        == "metadata:\n  name: api\ncontainers:\n  api:\n    image: ghcr.io/org/api@sha256:" + "a" * 64 + "\n"
    )


def test_workload_image_slot_with_no_lock_entry_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "stack" / "score").mkdir(parents=True)
    (tmp_path / "stack" / "score" / "api.yaml").write_text(
        "metadata:\n  name: api\ncontainers:\n  api:\n    image: ${API_IMAGE}\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder()
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"workloads": ["api.yaml"]}},
    )
    config.root = tmp_path

    with pytest.raises(ValueError, match=r"api\.yaml.*API_IMAGE.*image lock"):
        score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK), {}, secrets=_empty_secrets())


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

    assert config_module.PROBE_ADAPTERS == ("python-urllib", "bash-http")
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

PROVISIONER_WITHOUT_SERVICES = """
- uri: template://example/state
  type: example-state
  state:
    key: value
"""

ALPINE_LOCK = {"ALPINE_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.21.0@" + DIGEST}


def _lock_for_images(images: dict[str, str]) -> LockFile:
    return LockFile.model_validate({
        "platforms": ["linux/amd64"],
        "images": {
            name: {"index": reference, "platforms": {"linux/amd64": reference}} for name, reference in images.items()
        },
    })


def _bundle_config(**bundle: object) -> ConfigFile:
    return _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"core": {"workloads": ["api.yaml"], **bundle}},  # type: ignore[arg-type]
    )


def _compose_init_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: ConfigFile,
    images: dict[str, str],
    sources: dict[str, str],
    generated: str = "services: {}\n",
) -> list[list[str]]:
    # Return one template-name list per bundle rendering, in declaration order.
    for relative, text in sources.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
    recorder = ComposeBashRecorder(generated)
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config.root = tmp_path
    score_compose_module.generate_compose(
        config, _lock_for_images({**images, **ALPINE_LOCK}), {}, secrets=_empty_secrets()
    )
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
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": _workload_referencing("state", "api"),
            "stack/score/compose/state.yaml": PROVISIONER_WITHOUT_SERVICES,
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_generate_compose_requires_the_alpine_image_lock_entry(tmp_path: Path) -> None:
    config = _bundle_config()
    config.root = tmp_path

    with pytest.raises(ValueError, match="must carry the ALPINE_IMAGE entry"):
        score_compose_module.generate_compose(config, LockFile.model_validate({}), {}, secrets=_empty_secrets())


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
    config = _bundle_config(workloads=["api.yaml", "gateway.yaml"])
    config.root = tmp_path
    config.platforms = ["linux/amd64"]
    recorder = ComposeBashRecorder("services: {}\n")
    monkeypatch.setattr(score_module, "bash", recorder.run)

    score_compose_module.generate_compose(config, _lock_for_images(ALPINE_LOCK), {}, secrets=_empty_secrets())

    init = next(command for command in recorder.commands if command.startswith("score-compose init"))
    provisioners = [token for token in init.split() if token.endswith(".yaml")]
    assert [Path(token).name for token in provisioners] == ["config.yaml", "state.yaml"]


WORKLOAD_WITH_LIVENESS = """
metadata:
  name: api
containers:
  api:
    image: ${API_IMAGE:?err}
    livenessProbe:
      httpGet:
        path: /health
        port: 5678
"""

WORKLOAD_WITH_READINESS = """
metadata:
  name: api
  annotations:
    outernet.internal/probe-adapter: python-urllib
containers:
  api:
    image: ${API_IMAGE:?err}
    readinessProbe:
      httpGet:
        path: /ready
        port: 5678
"""

WORKLOAD_WITH_UNADAPTED_READINESS = """
metadata:
  name: api
containers:
  api:
    image: ${API_IMAGE:?err}
    readinessProbe:
      httpGet:
        path: /ready
        port: 5678
"""


def test_httpget_probe_without_the_adapter_annotation_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"readinessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_UNADAPTED_READINESS,
            },
        )

    with pytest.raises(ValidationError, match=r"livenessProbe httpGet.*probe-adapter"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": WORKLOAD_WITH_LIVENESS,
            },
        )


def test_unknown_probe_adapter_values_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"supported adapters"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": "metadata:\n  name: api\n  annotations:\n    outernet.internal/probe-adapter: curl\ncontainers: {}\n",
            },
        )


def test_workload_resources_parse_typed() -> None:
    document = WorkloadDocument.parse(
        {"resources": {"db": {"type": "example-db"}}, "metadata": {"name": "api"}}, "api.yaml"
    )

    assert document.resources["db"].type == "example-db"


def test_workload_resources_without_a_type_refuse() -> None:
    with pytest.raises(ValidationError, match=r"resources\.db\.type"):
        WorkloadDocument.parse({"resources": {"db": {"id": "main"}}, "metadata": {"name": "api"}}, "api.yaml")


def test_adapted_probe_bearing_workloads_register_the_healthcheck_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": WORKLOAD_WITH_READINESS,
        },
        generated=API_SERVICE_RENDERING,
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_restart_values_outside_the_compose_enum_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"restart policies"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": (
                    "metadata:\n  name: api\n  annotations:\n    outernet.internal/restart: always-please\ncontainers: {}\n"
                ),
            },
        )


def test_restart_annotation_with_an_enum_value_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": (
                "metadata:\n  name: api\n  annotations:\n    outernet.internal/restart: on-failure\ncontainers: {}\n"
            ),
        },
    )

    assert templates == [["probe-adapter-healthchecks.tpl", "restart.tpl"]]


def test_bare_literal_workload_image_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"image.*must bind"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": "metadata:\n  name: api\ncontainers:\n  api:\n    image: ghcr.io/example/api:1\n",
            },
        )


def test_a_workload_without_metadata_name_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"metadata\.name must be 'api'"):
        _compose_init_templates(monkeypatch, tmp_path, config, {}, {"stack/score/api.yaml": "containers: {}\n"})


def test_a_workload_name_mismatching_the_file_stem_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()
    source = "metadata:\n  name: apis\ncontainers: {}\n"

    with pytest.raises(ValidationError, match=r"metadata\.name must be 'api'"):
        _compose_init_templates(monkeypatch, tmp_path, config, {}, {"stack/score/api.yaml": source})


def test_consumer_interpolated_names_must_match_declared_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bundle_config()

    with pytest.raises(ValidationError, match=r"names no declared files"):
        _compose_init_templates(
            monkeypatch,
            tmp_path,
            config,
            {},
            {
                "stack/score/api.yaml": (
                    "metadata:\n"
                    "  name: api\n"
                    "  annotations:\n"
                    "    outernet.internal/consumer-interpolated: /etc/rathole/client.toml\n"
                    "containers: {}\n"
                ),
            },
        )


WORKLOAD_WITH_INTERPOLATED_FILE = """\
metadata:
  name: rathole-client
  annotations:
    outernet.internal/consumer-interpolated: /etc/rathole/client.toml
containers:
  rathole-client:
    image: ${RATHOLE_IMAGE:?err}
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


def test_consumer_interpolated_binds_ride_undoubled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    score_dir = tmp_path / "stack" / "score"
    score_dir.mkdir(parents=True)
    (score_dir / "rathole-client.yaml").write_text(WORKLOAD_WITH_INTERPOLATED_FILE, encoding="utf-8")
    recorder = MaterializingComposeRecorder(
        INTERPOLATED_GENERATED_COMPOSE,
        tmp_path / "stack/generated/compose/.score-compose/mounts/files/rathole-client-files-client.toml",
        'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n',
    )
    monkeypatch.setattr(score_module, "bash", recorder.run)
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={"rathole-client": {"workloads": ["rathole-client.yaml"]}},
    )
    config.root = tmp_path

    score_compose_module.generate_compose(
        config,
        _lock_for_images(ALPINE_LOCK),
        {},
        secrets=SecretsDeclaration.model_validate({"TUNNEL_CONTROL_ENDPOINT": "tunnel"}),
    )

    output = tmp_path / "stack" / "generated" / "compose" / "compose.rathole-client.yaml"
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    service = document["services"]["rathole-client-rathole-client"]
    assert service["configs"] == [
        {"source": "rathole-client-rathole-client-files-client.toml", "target": "/etc/rathole/client.toml"}
    ]
    assert (
        document["configs"]["rathole-client-rathole-client-files-client.toml"]["content"]
        == 'remote_addr = "${TUNNEL_CONTROL_ENDPOINT:?err}"\n'
    )


def test_gpu_compose_templates_register_per_the_renderings_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _root_config(
        repository="ghcr.io/org/repo",
        project_name="example",
        platforms=["linux/amd64"],
        services={
            "core-cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
            "core-rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
        },
    )

    templates = _compose_init_templates(
        monkeypatch,
        tmp_path,
        config,
        {},
        {
            "stack/score/api.yaml": "metadata:\n  name: api\ncontainers: {}\n",
        },
    )

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
    config = _root_config(k8s={"workloads": ["api.yaml"]})
    config.root = tmp_path

    assert config.k8s is not None
    generate_k8s(config, config.k8s, LockFile.model_validate({}), {}, local=False, secrets=_empty_secrets())

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
    config = _root_config(k8s={"workloads": ["api.yaml"]})
    config.root = tmp_path

    assert config.k8s is not None
    generate_k8s(config, config.k8s, LockFile.model_validate({}), {}, local=False, secrets=_empty_secrets())

    seeded = yaml.safe_load(recorder.state_at_generate[output.name])
    assert seeded["workloads"]["api"]["instance_suffix"] == "-" + sha256(b"api").hexdigest()[:10]


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
        "metadata:\n  name: api\ncontainers:\n  api:\n    image: ${LOKI_IMAGE}\n", encoding="utf-8"
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
        "requires: '>=0'\n"
        "repository: ghcr.io/org/repo\n"
        "project_name: example\n"
        "platforms:\n"
        "  - linux/amd64\n"
        "base_images:\n"
        "  LOKI_IMAGE: docker.io/grafana/loki:3.5.0\n"
        "  ALPINE_IMAGE: docker.io/library/alpine:3.21.0\n"
        "services:\n"
        "  core:\n"
        "    workloads:\n"
        "      - api.yaml\n",
        encoding="utf-8",
    )


def test_generate_score_refuses_a_missing_secrets_declaration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _verb_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="secrets declaration is required"):
        score_verb_module.generate_score(target="compose")


def test_generate_score_writes_the_mint_list_from_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verb_repo(tmp_path)
    (tmp_path / "stack" / "secrets.yaml").write_text(
        "PUBLIC_URL: the public address\nS3_ACCESS_KEY:\n  description: s3 credentials\n  mint: true\n",
        encoding="utf-8",
    )
    recorder = WorkloadContentsRecorder()

    def _skip_tools(requirements: list[score_verb_module.ToolRequirement]) -> None:
        return None

    monkeypatch.setattr(score_module, "bash", recorder.run)
    monkeypatch.setattr(score_verb_module, "ensure_tools", _skip_tools)
    monkeypatch.chdir(tmp_path)

    score_verb_module.generate_score(target="compose")

    mint = (tmp_path / "stack" / "generated" / "secrets.txt").read_text(encoding="utf-8")
    assert mint == "# Generated by generate-score from the secrets declaration\nS3_ACCESS_KEY\n"


def _k8s_only_repo(tmp_path: Path) -> None:
    (tmp_path / "boatyard.yml").write_text(
        "requires: '>=0'\nproject_name: example\nrepository: ghcr.io/org/repo\nk8s:\n  workloads:\n    - api.yaml\n",
        encoding="utf-8",
    )


def test_generate_score_on_a_compose_only_repo_derives_compose_and_requests_only_the_compose_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verb_repo(tmp_path)
    (tmp_path / "stack" / "secrets.yaml").write_text("PUBLIC_URL: the public address\n", encoding="utf-8")
    recorder = WorkloadContentsRecorder()
    requested: list[list[score_verb_module.ToolRequirement]] = []

    def _capture_tools(requirements: list[score_verb_module.ToolRequirement]) -> None:
        requested.append(requirements)

    monkeypatch.setattr(score_module, "bash", recorder.run)
    monkeypatch.setattr(score_verb_module, "ensure_tools", _capture_tools)
    monkeypatch.chdir(tmp_path)

    score_verb_module.generate_score()

    assert [requirement.binary for requirement in requested[0]] == ["score-compose"]
    assert (tmp_path / "stack" / "generated" / "compose" / "compose.core.yaml").is_file()


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


def test_ensure_tools_installs_a_binary_missing_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    commands: list[str] = []

    def _record(command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        commands.append(command)

    monkeypatch.setattr(score_verb_module, "bash", _record)
    requirement = score_verb_module.ToolRequirement(
        binary="score-compose",
        version="1.2.3",
        probe="score-compose 1.2.3",
        archive="https://example.com/score-compose_1.2.3_linux_amd64.tar.gz",
        extract="score-compose",
    )

    score_verb_module.ensure_tools([requirement])

    assert commands[0].startswith("curl -fsSL https://example.com/score-compose_1.2.3_linux_amd64.tar.gz")


def test_ensure_tools_reinstalls_on_a_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "score-compose"
    stub.write_text('#!/bin/sh\necho "score-compose 0.0.0"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    commands: list[str] = []

    def _record(command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        commands.append(command)

    monkeypatch.setattr(score_verb_module, "bash", _record)
    requirement = score_verb_module.ToolRequirement(
        binary="score-compose",
        version="1.2.3",
        probe="score-compose 1.2.3",
        archive="https://example.com/score-compose_1.2.3_linux_amd64.tar.gz",
        extract="score-compose",
    )

    score_verb_module.ensure_tools([requirement])

    assert commands[0].startswith("curl -fsSL https://example.com/score-compose_1.2.3_linux_amd64.tar.gz")


def test_ensure_tools_skips_install_when_the_pinned_version_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "score-compose"
    stub.write_text('#!/bin/sh\necho "score-compose 1.2.3"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    commands: list[str] = []

    def _record(command: str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        commands.append(command)

    monkeypatch.setattr(score_verb_module, "bash", _record)
    requirement = score_verb_module.ToolRequirement(
        binary="score-compose",
        version="1.2.3",
        probe="score-compose 1.2.3",
        archive="https://example.com/score-compose_1.2.3_linux_amd64.tar.gz",
        extract="score-compose",
    )

    score_verb_module.ensure_tools([requirement])

    assert commands == []


def test_generate_score_refuses_compose_on_a_k8s_only_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _k8s_only_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        typer.BadParameter, match="--target compose: this config declares no local service entries \\(k8s-only\\)"
    ):
        score_verb_module.generate_score(target="compose")
