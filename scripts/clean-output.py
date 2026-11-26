"""Filter for evidence logs: replaces the home directory with ~ and drops ANSI colour codes.

Reads stdin line by line (unbuffered) or, with file arguments, rewrites those files in place.
"""

import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HOME = os.path.expanduser("~")


def clean(line: str) -> str:
    return ANSI.sub("", line.replace(HOME, "~"))


if __name__ == "__main__":
    assert clean(f"\x1b[32mINF\x1b[0m {HOME}/x") == "INF ~/x"
    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            with open(path) as f:
                text = f.read()
            with open(path, "w") as f:
                f.write("".join(clean(line) for line in text.splitlines(keepends=True)))
    else:
        for line in sys.stdin:
            sys.stdout.write(clean(line))
            sys.stdout.flush()
