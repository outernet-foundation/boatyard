from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import typer
from bashrun.bash import bash_output
from build_artifact_registry.builds import list_build_tags, pull_artifact
from build_artifact_registry.setup_oras import install_oras
from pydantic import BaseModel, ConfigDict, RootModel

from boatyard import images
from boatyard.build import DIGEST_FILE_NAME, DIGEST_PLATFORM, DIGEST_PROJECT
from boatyard.config.config import ConfigFile
from boatyard.config.k8s_pins import PINS_HEADER, parse_pins
from boatyard.paths import DEFAULT_CONFIG_PATH, K8S_PINS_PATH
from boatyard.score_generate import generate_artifacts

app = typer.Typer()


class ShardImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    digest: str
    tags: list[str]


class ShardManifest(RootModel[dict[str, ShardImage]]):
    pass


@app.command()
def bump() -> None:
    root = Path.cwd()
    config_path = root / DEFAULT_CONFIG_PATH
    config = ConfigFile.load(config_path) if config_path.is_file() else None
    if config is None or config.k8s is None:
        print("no k8s section; nothing to pin")
        return

    dirty = [
        line for line in bash_output("git status --porcelain").splitlines() if line[3:] != K8S_PINS_PATH.as_posix()
    ]
    if dirty:
        raise ValueError("the working tree is dirty; the bump certifies HEAD — commit or stash first")

    image_shas = images.compute_image_shas(root, config.images)
    certify_head(image_shas)

    pins = parse_pins(image_shas)
    body = "".join(f"{name}: {tag}\n" for name, tag in sorted(pins.items())) or "{}\n"
    (root / K8S_PINS_PATH).write_text(PINS_HEADER + body, encoding="utf-8")
    generate_artifacts(config, target="k8s")
    for name, tag in sorted(image_shas.items()):
        print(f"{name}={tag}")
    print(f"wrote {K8S_PINS_PATH.as_posix()}")


def certify_head(image_shas: dict[str, str]) -> None:
    # A pin is writable only for content CI built and pushed at exactly this head.
    registry = os.environ.get("REGISTRY", "")
    if not registry:
        raise ValueError("REGISTRY not set — the bump verifies CI-shelved build shards under it")
    head = bash_output("git rev-parse HEAD").strip()
    install_oras()
    exact_tag = f"sha-{head}"
    shard_tags = sorted(
        tag
        for tag in list_build_tags(registry, DIGEST_PROJECT, DIGEST_PLATFORM)
        if tag == exact_tag or tag.startswith(f"{exact_tag}-")
    )
    if not shard_tags:
        raise ValueError(
            f"{registry}/builds shelves no build at {head[:8]}: nothing to pin — rebuild the head through CI "
            "(ad-hoc --push-to builds shelve nothing)"
        )
    recorded: set[str] = set()
    seen_targets: set[str] = set()
    with TemporaryDirectory(prefix="bump-shards-") as staging:
        for shard_tag in shard_tags:
            directory = Path(staging) / shard_tag
            pull_artifact(registry, DIGEST_PROJECT, DIGEST_PLATFORM, shard_tag, directory)
            manifest = ShardManifest.model_validate(
                json.loads((directory / DIGEST_FILE_NAME).read_text(encoding="utf-8"))
            )
            for build_target, image in manifest.root.items():
                if build_target in seen_targets:
                    raise ValueError(f"shard {shard_tag!r} repeats build target {build_target!r} from another shard")
                seen_targets.add(build_target)
                recorded.update(image.tags)
    missing = sorted(set(image_shas.values()) - recorded)
    if missing:
        raise ValueError(
            f"no CI image at {head[:8]} carries the tags {missing}: nothing to pin — rebuild the head through CI"
        )
