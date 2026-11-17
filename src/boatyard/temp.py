import re
import shlex
import sys

PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

SHA256_DIGEST_REGEX = r"sha256:[a-f0-9]{64}"

TREE_TAG_REGEX = r"tree-[0-9a-f]{40}"


def quote_command_token(value: str) -> str:
    # bashrun splits the command without posix quoting on Windows, where
    # shlex.quote's single quotes would ride the argument literally.
    if sys.platform == "win32":
        return value
    return shlex.quote(value)
