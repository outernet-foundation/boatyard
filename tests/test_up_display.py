import pytest
from rich.console import Console

from boatyard import up_display
from boatyard.up_display import (
    ProjectState,
    WallDisplay,
    apply_event,
    apply_line,
    parse_container_line,
    parse_pull_line,
    render_tree,
)

# Real docker 29.x compose --progress plain captures; every line keeps its exact leading and
# trailing whitespace (the doubled pull lines come from compose's two progress writers)
ALPINE_PULLING = " Image docker.io/library/alpine:3.20 Pulling "
ALPINE_PULLED = " Image docker.io/library/alpine:3.20 Pulled "
MIRRORED_PULLING = " Image ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.20 Pulling "
BUSYBOX_PULLING = " Image docker.io/library/busybox:1.37.0 Pulling "
BUSYBOX_PULLED = " Image docker.io/library/busybox:1.37.0 Pulled "
POSTGRES_PULLING = " Image docker.io/library/postgres:16.9-alpine Pulling "
FRESH_STARTED = " Container pull-fixture-fresh-1 Started "
SLEEPER_HEALTHY = " Container edge-livekit-sleeper-1 Healthy "
LAYER_PULLING = " 68fe9bff2ad4 Pulling fs layer 0B"
LAYER_DOWNLOAD_COMPLETE = " 68fe9bff2ad4 Download complete 0B"
LAYER_EXTRACTING = " 68fe9bff2ad4 Extracting 1B"
LAYER_PULL_COMPLETE = " 68fe9bff2ad4 Pull complete 0B"
DOWNLOAD_CUMULATIVE = " e62b35ad5ef0 Downloading 1.049MB"
DOWNLOAD_LEGACY_BAR = " 0ff154fa3401 Downloading [===================>  ]  5.243MB/31.0MB"
IMAGE_ERROR = (
    " Image docker.io/library/definitely-missing-image-xyz:1 Error pull access denied for "
    "definitely-missing-image-xyz, repository does not exist or may require 'docker login'"
)
NETWORK_CREATING = " Network plain-fixture_default Creating "

IGNORED_LINES = [LAYER_PULLING, LAYER_DOWNLOAD_COMPLETE, LAYER_EXTRACTING, LAYER_PULL_COMPLETE, NETWORK_CREATING]


def _states_with(*project_names: str) -> dict[str, ProjectState]:
    return {name: ProjectState(name) for name in project_names}


class RecordingConsoleFactory:
    def __init__(self) -> None:
        self.console: Console | None = None

    def __call__(self) -> Console:
        self.console = Console(record=True, width=100, force_terminal=False)
        return self.console

    def export(self) -> str:
        assert self.console is not None
        return self.console.export_text()


class RefusedConsoleFactory:
    def __call__(self) -> Console:
        raise AssertionError("an inactive display must never start Live")


class TestParseContainerLine:
    def test_extracts_the_service_and_state(self) -> None:
        assert parse_container_line(FRESH_STARTED, "pull-fixture") == ("fresh", "Started")

    def test_strips_exactly_one_trailing_index(self) -> None:
        assert parse_container_line(" Container edge-demo-api2-3 Created ", "edge-demo") == ("api2", "Created")
        assert parse_container_line(" Container edge-demo-s3-initializer-1 Running ", "edge-demo") == (
            "s3-initializer",
            "Running",
        )
        assert parse_container_line(" Container edge-demo-api-12 Exited ", "edge-demo") == ("api", "Exited")

    def test_unknown_states_pass_through_verbatim(self) -> None:
        assert parse_container_line(" Container proj-svc-1 WeirdState ", "proj") == ("svc", "WeirdState")

    def test_non_container_lines_parse_to_none(self) -> None:
        for line in (ALPINE_PULLING, NETWORK_CREATING, "unexpected text"):
            assert parse_container_line(line, "plain-fixture") is None


class TestParsePullLine:
    def test_image_pull_lines_carry_the_short_label(self) -> None:
        assert parse_pull_line(BUSYBOX_PULLING) == up_display.PullUpdate("busybox:1.37.0", "pulling", None)
        assert parse_pull_line(BUSYBOX_PULLED) == up_display.PullUpdate("busybox:1.37.0", "pulled", None)
        assert parse_pull_line(MIRRORED_PULLING) == up_display.PullUpdate("alpine:3.20", "pulling", None)

    def test_image_errors_mark_the_row(self) -> None:
        assert parse_pull_line(IMAGE_ERROR) == up_display.PullUpdate("definitely-missing-image-xyz:1", "error", None)

    def test_layer_download_lines_carry_only_the_counter(self) -> None:
        assert parse_pull_line(DOWNLOAD_CUMULATIVE) == up_display.PullUpdate(None, None, "1.049MB")
        assert parse_pull_line(DOWNLOAD_LEGACY_BAR) == up_display.PullUpdate(None, None, "5.243MB/31.0MB")

    def test_unrelated_lines_parse_to_none(self) -> None:
        for line in IGNORED_LINES + [FRESH_STARTED, "unexpected text"]:
            assert parse_pull_line(line) is None


class TestApplyLine:
    def test_byte_counters_attach_to_the_most_recent_pulling_row(self) -> None:
        state = ProjectState("big-fixture")
        apply_line(state, BUSYBOX_PULLING)
        apply_line(state, POSTGRES_PULLING)
        apply_line(state, DOWNLOAD_CUMULATIVE)

        assert state.rows["postgres:16.9-alpine"].counter == "1.049MB"
        assert state.rows["busybox:1.37.0"].counter is None

    def test_container_rows_and_image_rows_coexist(self) -> None:
        state = ProjectState("pull-fixture")
        apply_line(state, BUSYBOX_PULLING)
        apply_line(state, FRESH_STARTED)

        assert state.rows["busybox:1.37.0"].state == "pulling"
        assert state.rows["fresh"].state == "Started"

    def test_the_parser_never_crashes_on_unexpected_text(self) -> None:
        state = ProjectState("proj")
        for line in ("", " ", "\x1b[31mred\x1b[0m", "Container without leading space Created", ALPINE_PULLING):
            apply_line(state, line)

        assert list(state.rows) == ["alpine:3.20"]


class TestApplyEvent:
    def test_finish_flips_the_project_state(self) -> None:
        states = _states_with("pull-fixture")
        apply_event(states, up_display.LineEvent("pull-fixture", FRESH_STARTED))
        apply_event(states, up_display.FinishEvent("pull-fixture", ok=False, elapsed=3.5))

        assert states["pull-fixture"].state == "failed"
        assert states["pull-fixture"].elapsed == pytest.approx(3.5)
        assert states["pull-fixture"].rows["fresh"].state == "Started"

    def test_events_for_unknown_projects_are_dropped(self) -> None:
        states = _states_with("one")
        apply_event(states, up_display.LineEvent("ghost", FRESH_STARTED))
        apply_event(states, up_display.FinishEvent("ghost", ok=True, elapsed=1.0))

        assert states["one"].rows == {}


class TestRenderTree:
    def test_the_tree_carries_project_headers_rows_and_counters(self) -> None:
        states = _states_with("edge-livekit", "edge-core")
        apply_event(states, up_display.LineEvent("edge-livekit", POSTGRES_PULLING))
        apply_event(states, up_display.LineEvent("edge-livekit", DOWNLOAD_CUMULATIVE))
        apply_event(states, up_display.LineEvent("edge-livekit", SLEEPER_HEALTHY))
        apply_event(states, up_display.FinishEvent("edge-livekit", ok=True, elapsed=41.9))
        apply_event(states, up_display.LineEvent("edge-core", BUSYBOX_PULLING))

        console = Console(record=True, width=100, force_terminal=False)
        console.print(render_tree(states))
        exported = console.export_text()

        assert "edge-livekit" in exported
        assert "edge-core" in exported
        assert "postgres:16.9-alpine" in exported
        assert "pulling" in exported
        assert "1.049MB" in exported
        assert "sleeper" in exported
        assert "Healthy" in exported
        assert "41.9s" in exported


class TestWallDisplay:
    def test_the_pump_drains_fed_lines_into_the_final_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        display = WallDisplay(["pull-fixture"], active=True)
        for line in (BUSYBOX_PULLING, LAYER_PULLING, DOWNLOAD_CUMULATIVE, BUSYBOX_PULLED, FRESH_STARTED):
            display.feed("pull-fixture", line)
        display.finish("pull-fixture", True, 4.2)
        factory = RecordingConsoleFactory()
        monkeypatch.setattr(up_display, "build_console", factory)

        display.run([])

        exported = factory.export()
        assert "✔ pull-fixture" in exported
        assert "busybox:1.37.0" in exported
        assert "fresh" in exported
        assert "Started" in exported

    def test_an_inactive_display_holds_the_queue_and_never_draws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        display = WallDisplay(["pull-fixture"], active=False)
        display.feed("pull-fixture", FRESH_STARTED)
        display.finish("pull-fixture", True, 4.2)
        monkeypatch.setattr(up_display, "build_console", RefusedConsoleFactory())

        display.run([])

        assert display.states["pull-fixture"].state == "working"
