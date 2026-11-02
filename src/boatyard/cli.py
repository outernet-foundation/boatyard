import typer

from boatyard.verbs.build import build
from boatyard.verbs.down import down
from boatyard.verbs.lock import lock
from boatyard.verbs.matrix import docker_build_matrix
from boatyard.verbs.mirror import mirror
from boatyard.verbs.publish import publish_compose
from boatyard.verbs.score import generate_score
from boatyard.verbs.tree_tags import tree_tags
from boatyard.verbs.up import up

app = typer.Typer()

app.command()(up)
app.command()(down)
app.command()(build)
app.command()(lock)
app.command()(tree_tags)
app.command()(mirror)
app.command()(publish_compose)
app.command()(generate_score)
app.command()(docker_build_matrix)
