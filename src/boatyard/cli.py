import typer

from boatyard.verbs.build import app as build_app
from boatyard.verbs.bump import app as bump_app
from boatyard.verbs.down import app as down_app
from boatyard.verbs.lock import app as lock_app
from boatyard.verbs.matrix import app as matrix_app
from boatyard.verbs.mirror import app as mirror_app
from boatyard.verbs.publish import app as publish_app
from boatyard.verbs.score import app as score_app
from boatyard.verbs.tree_tags import app as tree_tags_app
from boatyard.verbs.up import app as up_app

app = typer.Typer()

app.add_typer(up_app)
app.add_typer(down_app)
app.add_typer(build_app)
app.add_typer(bump_app)
app.add_typer(lock_app)
app.add_typer(tree_tags_app)
app.add_typer(mirror_app)
app.add_typer(publish_app)
app.add_typer(score_app)
app.add_typer(matrix_app)
