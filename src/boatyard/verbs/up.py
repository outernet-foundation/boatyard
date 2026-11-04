from __future__ import annotations

import os
import re
import shlex
from contextlib import ExitStack
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from boatyard.bake import run_build
from boatyard.compose import compose_command, lock_env_unwind, require_env_file
from boatyard.config import (
    CADDY_IMAGE,
    Config,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    Yaml,
    bundle_lock_key,
    compose_bundle_output_path,
    double_dollars,
    is_mapping,
    load_yaml_file,
    lock_key_prefix,
    qualified_option_name,
    refuse_unresolved_references,
    require_compose_stack,
    required_compose_holes,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock, read_config_lock
from boatyard.crane import install_crane
from boatyard.environment import BUNDLES_VARIABLE, BUNDLE_OVERRIDE_PREFIX, Environment
from boatyard.lock import compose_env_entries, producer_env_entries, render_env_entries

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"

app = typer.Typer()


@app.command()
def up(
    attached: Annotated[bool, typer.Option("--attached", "-a", help="Run in foreground (not detached)")] = False,
    quiet_pull: Annotated[
        bool,
        typer.Option("--quiet-pull", "-q", help="Suppress per-layer pull progress (still shows pull/push totals)."),
    ] = False,
    build: Annotated[
        bool,
        typer.Option("--build", help="Build all images locally before bringing the stack up; skips pulling"),
    ] = False,
) -> None:
    root = Path.cwd()
    config = Config.load(root)

    require_env_file(root)
    environment = Environment.load(root)
    if config is None or not config.services:
        if environment.bundles:
            raise ValueError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
        config = require_compose_stack(config)

    # Refuse --build in a repo with no build declarations.
    if build and not (config.repository or config.base_images):
        raise typer.BadParameter("--build requires build declarations (repository or base_images) in boatyard.yml")

    # Refuse an empty selection in an options-only repo.
    if not environment.bundles and not config.bare_entries and config.selectable_options:
        raise ValueError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; "
            f"the options are {sorted(config.selectable_options)}"
        )

    # Refuse bundle names outside the selectable options.
    unknown = [name for name in environment.bundles if name not in config.selectable_options]
    if unknown:
        bare_entries_note = f"; bare entries, always included: {config.bare_entries}" if config.bare_entries else ""
        raise ValueError(
            f"{BUNDLES_VARIABLE}= names {unknown}; "
            f"the selectable options are {sorted(config.selectable_options)}{bare_entries_note}"
        )

    # Fold the selected names into at most one option per group.
    choices: dict[str, str] = {}
    for name in environment.bundles:
        group, option_name = config.selectable_options[name]
        if group in choices:
            raise ValueError(
                f"{BUNDLES_VARIABLE}= selects {qualified_option_name(group, choices[group])} and {name}; "
                f"at most one option per group ({group})"
            )
        choices[group] = option_name

    # Refuse override names outside the bundle vocabulary.
    unknown = sorted(
        name
        for name in environment.path_overrides
        if name not in config.selectable_options and name not in config.bare_entries
    )
    if unknown:
        raise ValueError(
            f"{BUNDLE_OVERRIDE_PREFIX}* names {unknown}; "
            f"the bundle vocabulary is {sorted(config.selectable_options.keys() | set(config.bare_entries))}"
        )

    # Resolve the chosen options into leaf services.
    services = config.resolve_services(choices)

    # Collect each service's include, override, and per-run lock env entries in declaration order.
    lock = read_config_lock(config.root)
    consumer_entries = compose_env_entries(lock, config, environment.platform)
    entries: dict[str, str] = dict(consumer_entries)
    includes: list[Yaml] = []
    overridden: set[str] = set()
    requirements: dict[str, list[str]] = {}

    for service in services:
        name = service.name

        # Refuse a missing override path or one that defines the reserved edge service.
        if name in environment.path_overrides:
            candidate = environment.path_overrides[name]
            path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
            if not path.is_file():
                raise ValueError(
                    f"{BUNDLE_OVERRIDE_PREFIX + name.upper().replace('-', '_')}: {path.as_posix()} does not exist"
                )
            loaded = load_yaml_file(path)
            loaded_services = loaded.get("services") if is_mapping(loaded) else None
            if is_mapping(loaded_services) and EDGE_SERVICE_NAME in loaded_services:
                raise ValueError(
                    f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}"
                )
            refuse_unresolved_references(
                name,
                service.routes,
                service.ports,
                set(loaded_services) if is_mapping(loaded_services) else set(),
                prefix=f"{name}-",
            )
            requirements[name] = sorted(required_compose_holes(path.read_text(encoding="utf-8")))
            includes.append(path.as_posix())
            overridden.add(name)
            # Fill the override's namespaced holes from its producer's lock, three parents up
            producer_lock = path.parents[3] / LOCK_PATH
            if producer_lock.is_file():
                producer_entries = producer_env_entries(parse_config_lock(producer_lock), environment.platform)
                entries.update({lock_key_prefix(name) + key: value for key, value in producer_entries.items()})
            continue

        # Include a remote ref as its lock-pinned digest; the lock's recorded requires is the contract.
        if service.entry.ref is not None:
            key = bundle_lock_key(name)
            pinned = lock.require(key, f"bundle {name!r}", hint="run boatyard lock")
            requirements[name] = lock.bundles[key].requires
            includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
            continue

        # Include a local entry through its generated bundle output.
        output = compose_bundle_output_path(config.root, name)
        if not output.is_file():
            raise ValueError(
                f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
            )
        output_text = output.read_text(encoding="utf-8")
        loaded_output = yaml.safe_load(output_text)
        output_services = loaded_output.get("services") if is_mapping(loaded_output) else None
        refuse_unresolved_references(
            name,
            service.routes,
            service.ports,
            set(output_services) if is_mapping(output_services) else set(),
            prefix=f"{name}-",
        )
        requirements[name] = sorted(required_compose_holes(output_text))
        includes.append(output.resolve().as_posix())
        entries.update({lock_key_prefix(name) + key: value for key, value in consumer_entries.items()})

    # Refuse selected bundles whose required interpolation no environment source fills; lock-key
    # holes are satisfied by construction through the per-run env file's entries.
    filled = {variable for variable, value in {**os.environ, **environment.variables, **entries}.items() if value}
    _refuse_unfilled_requirements(requirements, filled)

    # Namespace the routes and ports under their declaring entries.
    routes = [
        route.model_copy(update={"upstream": f"{service.name}-{route.upstream}"})
        for service in services
        for route in service.routes
    ]
    ports = [
        port.model_copy(update={"service": f"{service.name}-{port.service}"})
        for service in services
        for port in service.ports
    ]

    # Render the edge service and its Caddyfile when any route rides.
    document_services: dict[str, Yaml] = {}
    caddyfile: str | None = None
    if routes:
        # Require PUBLIC_URL and derive the forwarded environment under the auth option.
        auth_service = next((service for service in services if service.auth), None)
        forwarded = None
        if auth_service is not None:
            if environment.public_url is None:
                raise ValueError("an auth option is selected; PUBLIC_URL is required")
            forwarded = environment.public_url.forwarded

        edge: dict[str, Yaml] = {
            "image": f"${{{CADDY_IMAGE}:?err}}",
            "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": str(EDGE_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            # Attach the edge to every selected bundle's network.
            "networks": dict.fromkeys(f"{service.name}-net" for service in services),
        }
        if forwarded is not None:
            edge["environment"] = forwarded
        document_services[EDGE_SERVICE_NAME] = edge

        # Render one site with one handle block per route in declaration order.
        lines = [f":{EDGE_LISTEN_PORT} {{"]
        for route in routes:
            matcher = "" if route.path == "/*" else f" {route.path}"
            if route.raw is not None:
                block = ["\t\t" + line for line in route.raw.splitlines()]
            elif auth_service is not None and route.path == auth_service.routes[0].path:
                block = [
                    f"\t\treverse_proxy {route.upstream} {{",
                    "\t\t\theader_up X-Forwarded-Proto {$FORWARDED_PROTO}",
                    "\t\t\theader_up X-Forwarded-Port {$FORWARDED_PORT}",
                    "\t\t}",
                ]
            else:
                block = [f"\t\treverse_proxy {route.upstream}"]
            head = "\thandle_path" if route.strip_prefix else "\thandle"
            lines.extend((f"{head}{matcher} {{", *block, "\t}"))
        lines.append("}")
        caddyfile = double_dollars("\n".join(lines) + "\n")

    # Group the namespaced ports into per-service stanzas.
    document_services.update({
        service: {"ports": [port.model_dump(exclude={"service"}, exclude_none=True) for port in service_ports]}
        for service, service_ports in groupby(
            sorted(ports, key=lambda port: port.service), key=lambda port: port.service
        )
    })
    # Assemble the root document.
    document: dict[str, Yaml] = {"name": config.project_name}
    if includes:
        document["include"] = includes
    if document_services:
        document["services"] = document_services
    if caddyfile is not None:
        document["configs"] = {EDGE_CONFIG_NAME: {"content": caddyfile}}

    # Materialize the per-run files and hand off; the handoff execs, so a successful run never unwinds.
    with ExitStack() as stack:
        # Write the per-run lock env file.
        env_file: Path | None = None
        if entries:
            with NamedTemporaryFile(
                "w", suffix=".env", prefix="devkit-lock-env-", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(render_env_entries(entries))
                env_file = Path(handle.name)
            stack.enter_context(lock_env_unwind(env_file))

        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        assembly = Path(handle.name)
        stack.callback(assembly.unlink, missing_ok=True)
        compose_files = [assembly]

        # Probe every lock entry before bring-up; an overridden bundle skips its probe —
        # testing a bundle not published at any digest yet is the override's point.
        _probe_lock_entries(config, overridden)

        # Build local images.
        if build:
            run_build(config=config)

        # Hand off to docker compose up.
        up_command = compose_command(
            root,
            compose_files,
            "up",
            env_file=env_file,
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)


def _refuse_unfilled_requirements(requirements: dict[str, list[str]], filled: set[str]) -> None:
    missing = {name: sorted(set(required) - filled) for name, required in requirements.items() if set(required) - filled}
    if missing:
        name, absent = min(missing.items())
        raise ValueError(
            f"bundle {name!r} requires {absent}, which no environment source sets (shell, .env, or the lock env file)"
        )


def _probe_lock_entries(config: Config, overridden: set[str]) -> None:
    lock = read_config_lock(config.root)
    references = sorted(
        set(
            [reference for entry in lock.images.values() for reference in entry.recorded_digests]
            + list(lock.charts.values())
        )
    )
    for value in references:
        reference, _, digest = value.rpartition("@")
        try:
            bash_output(f"docker buildx imagetools inspect {shlex.quote(value)}")
        except CalledProcessError as error:
            stderr = (error.stderr or str(error)).strip()
            if re.search(r"\b404\b|not found|manifest unknown", stderr, re.IGNORECASE):
                raise ValueError(f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored")
            raise ValueError(
                f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe failed: {stderr}"
            )

    skip = {bundle_lock_key(name) for name in overridden}
    bundle_references = sorted({bundle.reference for key, bundle in lock.bundles.items() if key not in skip})
    if not bundle_references:
        return
    # Compose-bundle media types are not image indexes; imagetools may refuse them, so bundle
    # entries probe via crane under the same stderr-classification law.
    install_crane()
    for value in bundle_references:
        reference, _, digest = value.rpartition("@")
        try:
            bash_output(f"crane digest {shlex.quote(value)}")
        except CalledProcessError as error:
            stderr = (error.stderr or str(error)).strip()
            if re.search(r"\b404\b|not found|manifest unknown", stderr, re.IGNORECASE):
                raise ValueError(f"Bundle {reference} (digest {digest}) is not published; run boatyard lock")
            raise ValueError(f"Bundle {reference} (digest {digest}): the bundle probe failed: {stderr}")
