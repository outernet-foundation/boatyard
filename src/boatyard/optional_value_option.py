# noqa-wrapper: typer vendored click without a public Option class and dropped click's
# flag_value mechanic, so an optional-value flag (--push alone vs --push <addr>) cannot be
# spelled through typer's own API; this module is the pinned-typer private seam for that
# one grammar and the only place it couples
from __future__ import annotations

from typing import Any

from typer._click.core import Context
from typer._click.parser import _Option  # pyright: ignore[reportPrivateUsage]
from typer._click.parser import _OptionParser  # pyright: ignore[reportPrivateUsage]
from typer._click.parser import _ParsingState  # pyright: ignore[reportPrivateUsage]
from typer._click.parser import _normalize_opt  # pyright: ignore[reportPrivateUsage]
from typer.core import TyperCommand, TyperOption


class _OptionalValueParserOption(_Option):  # pyright: ignore[reportPrivateUsage]
    def __init__(self, obj: TyperOption, opts: list[str], dest: str) -> None:
        super().__init__(obj, opts, dest, action="store", nargs=0, const=None)

    def process(self, value: Any, state: _ParsingState) -> None:  # pyright: ignore[reportPrivateUsage]
        destination = state.rargs.pop(0) if state.rargs and not state.rargs[0].startswith("-") else ""
        assert self.dest is not None
        state.opts[self.dest] = destination
        state.order.append(self.obj)


class DestinationOption(TyperOption):
    def __init__(self, original: TyperOption) -> None:
        assert original.name is not None
        super().__init__(
            param_decls=[original.name, *original.opts],
            type=original.type,
            default=original.default,
            callback=original.callback,
            help=original.help,
        )

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:  # pyright: ignore[reportPrivateUsage]
        assert self.name is not None
        option = _OptionalValueParserOption(self, [_normalize_opt(opt, ctx) for opt in self.opts], self.name)  # pyright: ignore[reportPrivateUsage]
        parser._opt_prefixes.update(option.prefixes)  # pyright: ignore[reportPrivateUsage]
        for opt in list(option._short_opts):  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
            parser._short_opts[opt] = option  # pyright: ignore
        for opt in list(option._long_opts):  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
            parser._long_opt[opt] = option  # pyright: ignore


class DestinationCommand(TyperCommand):
    destination_parameter = "push"

    def get_params(self, ctx: Context) -> list[Any]:
        return [
            DestinationOption(param)
            if isinstance(param, TyperOption) and param.name == self.destination_parameter
            else param
            for param in super().get_params(ctx)
        ]
