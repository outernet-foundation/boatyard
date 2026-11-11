from __future__ import annotations

import queue
import re
import threading
import time
from typing import NamedTuple

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text
from rich.tree import Tree

# compose --progress plain line shapes, pinned against real docker 29.x output; the bracketed
# bar form is accepted alongside the cumulative form this daemon emits for large layers
_CONTAINER_LINE_PATTERN = re.compile(r"^ Container (\S+) (\S+?)\s*$")
_CONTAINER_INDEX_PATTERN = re.compile(r"-\d+$")
_IMAGE_LINE_PATTERN = re.compile(r"^ Image (\S+) (Pulling|Pulled|Error)\s*(.*)$")
_LAYER_DOWNLOAD_PATTERN = re.compile(r"^ ([0-9a-f]{6,64}) Downloading (?:\[[^]]*\]\s*)?(\S.*)$")

PROJECT_GLYPHS = {"working": "⠿", "ready": "✔", "failed": "✖"}
PROJECT_STYLES = {"working": "yellow", "ready": "green", "failed": "red"}
ROW_GLYPHS = {"started": "✔", "healthy": "✔", "pulled": "✔", "pulling": "⠿", "error": "✖", "exited": "✖"}
ROW_STYLES = {"started": "green", "healthy": "green", "pulled": "cyan", "pulling": "yellow", "error": "red", "exited": "red"}


class ContainerRow:
    # One service or image row of a project's tree: label, latest state, latest byte counter
    def __init__(self, label: str) -> None:
        self.label = label
        self.state = ""
        self.counter: str | None = None


class ProjectState:
    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self.state = "working"
        self.started = time.monotonic()
        self.elapsed: float | None = None
        self.rows: dict[str, ContainerRow] = {}
        self.latest_pulling: str | None = None


class LineEvent(NamedTuple):
    project: str
    line: str


class FinishEvent(NamedTuple):
    project: str
    ok: bool
    elapsed: float


class PullUpdate(NamedTuple):
    key: str | None
    state: str | None
    counter: str | None


class WallDisplay:
    # The terminal owner while the wall runs: workers put events, run() alone draws
    def __init__(self, project_names: list[str], active: bool) -> None:
        self.states = {name: ProjectState(name) for name in project_names}
        self.active = active
        self._events: queue.Queue[LineEvent | FinishEvent] = queue.Queue()

    def feed(self, project: str, line: str) -> None:
        if self.active:
            self._events.put(LineEvent(project, line))

    def finish(self, project: str, ok: bool, elapsed: float) -> None:
        if self.active:
            self._events.put(FinishEvent(project=project, ok=ok, elapsed=elapsed))

    def run(self, workers: list[threading.Thread]) -> None:
        # Pump until every worker joined and the queue drained; Live prints the final frame on exit
        if not self.active:
            return
        with Live(console=build_console(), refresh_per_second=4) as live:
            live.update(render_tree(self.states))
            while not (all(not worker.is_alive() for worker in workers) and self._events.empty()):
                try:
                    event = self._events.get(timeout=0.25)
                except queue.Empty:
                    continue
                apply_event(self.states, event)
                live.update(render_tree(self.states))


def build_console() -> Console:
    return Console()


def apply_event(states: dict[str, ProjectState], event: LineEvent | FinishEvent) -> None:
    state = states.get(event.project)
    if state is None:
        return
    if isinstance(event, FinishEvent):
        state.state = "ready" if event.ok else "failed"
        state.elapsed = event.elapsed
        return
    apply_line(state, event.line)


def apply_line(state: ProjectState, line: str) -> None:
    container = parse_container_line(line, state.project_name)
    if container is not None:
        state.rows.setdefault(container[0], ContainerRow(container[0])).state = container[1]
        return
    update = parse_pull_line(line)
    if update is None:
        return
    if update.key is None:
        if state.latest_pulling is not None:
            state.rows[state.latest_pulling].counter = update.counter
        return
    row = state.rows.setdefault(update.key, ContainerRow(update.key))
    if update.state is not None:
        row.state = update.state
    if update.counter is not None:
        row.counter = update.counter
    if update.state == "pulling":
        state.latest_pulling = update.key


def parse_container_line(line: str, project_name: str) -> tuple[str, str] | None:
    # Container <project>-<service>-<n> <State>; strips the project prefix and one trailing index
    match = _CONTAINER_LINE_PATTERN.match(line)
    if match is None:
        return None
    container_name, state = match.groups()
    service = _CONTAINER_INDEX_PATTERN.sub("", container_name.removeprefix(f"{project_name}-"))
    return service, state


def parse_pull_line(line: str) -> PullUpdate | None:
    download = _LAYER_DOWNLOAD_PATTERN.match(line)
    if download is not None:
        return PullUpdate(key=None, state=None, counter=download.group(2).strip())
    image = _IMAGE_LINE_PATTERN.match(line)
    if image is None:
        return None
    reference, verb, _message = image.groups()
    state = "pulling" if verb == "Pulling" else ("pulled" if verb == "Pulled" else "error")
    return PullUpdate(key=_image_label(reference), state=state, counter=None)


def _image_label(reference: str) -> str:
    return reference.rpartition("/")[2]


def render_tree(states: dict[str, ProjectState]) -> Group:
    return Group(*(_project_tree(state) for state in states.values()))


def _project_tree(state: ProjectState) -> Tree:
    elapsed = state.elapsed if state.elapsed is not None else max(time.monotonic() - state.started, 0.0)
    header = Text(f"{PROJECT_GLYPHS[state.state]} {state.project_name}  {elapsed:.1f}s", style=PROJECT_STYLES[state.state])
    tree = Tree(header, guide_style="dim")
    for row in state.rows.values():
        tree.add(_row_text(row))
    return tree


def _row_text(row: ContainerRow) -> Text:
    text = Text(f"{ROW_GLYPHS.get(row.state, '…')} {row.label}", style=ROW_STYLES.get(row.state, ""))
    text.append(f"  {row.state}")
    if row.counter is not None:
        text.append(f"  {row.counter}", style="dim")
    return text
