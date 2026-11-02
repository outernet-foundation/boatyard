from __future__ import annotations

import functools
from collections.abc import Callable


def entry[ReturnType](command: Callable[..., ReturnType]) -> Callable[..., ReturnType]:
    # The verb boundary: authored-input failures (ValueError-family) surface as
    # one-line stderr with exit 1, never a traceback.
    @functools.wraps(command)
    def wrapper(*arguments: object, **keywords: object) -> ReturnType:
        try:
            return command(*arguments, **keywords)
        except ValueError as error:
            raise SystemExit(str(error)) from error

    return wrapper
