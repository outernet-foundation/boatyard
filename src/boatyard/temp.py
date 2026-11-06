import re

PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

SHA256_DIGEST_REGEX = r"sha256:[a-f0-9]{64}"
