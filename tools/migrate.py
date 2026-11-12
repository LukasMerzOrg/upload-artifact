"""Codemod: switch workflow steps from actions/upload-artifact to this action.

Usage: python3 tools/migrate.py <workflow.yml>...
Rewrites the `uses:` line and adds the two Garage secrets to the step's
`with:` block. Line-based, so comments and formatting stay untouched. Prints
the number of steps changed per file; fails if a step has no `with:` block.
"""
import re
import sys

ACTION = "LukasMerzOrg/upload-artifact@v1"
USES = re.compile(r"^(?P<lead>\s*(?:- )?)uses:\s*actions/upload-artifact@\S+(?P<rest>.*)$")
SECRETS = ("access-key-id: ${{ secrets.GARAGE_ACCESS_KEY_ID }}",
           "secret-access-key: ${{ secrets.GARAGE_SECRET_ACCESS_KEY }}")


def _indent(line):
    return len(line) - len(line.lstrip())


def _step_bounds(lines, at, col):
    """Line range [start, end) of the step whose keys sit at column `col`."""
    dash = col - 2
    start = at
    while not (lines[start][dash:dash + 2] == "- " and _indent(lines[start]) == dash):
        start -= 1
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) >= col):
        end += 1
    return start, end


def migrate(text):
    lines, changed, i = text.split("\n"), 0, 0
    while i < len(lines):
        m = USES.match(lines[i])
        if not m:
            i += 1
            continue
        col = len(m.group("lead"))  # column of the step's keys
        start, end = _step_bounds(lines, i, col)
        with_at = next((n for n in range(start, end)
                        if re.match(r"^(- )?with:\s*(#.*)?$", lines[n][col - 2 if n == start else col:])
                        and _indent(lines[n]) in (col, col - 2)), None)
        if with_at is None:
            raise SystemExit(f"step at line {i + 1} has no with: block")
        child = next(_indent(l) for l in lines[with_at + 1:end] if l.strip())
        lines[i] = f"{m.group('lead')}uses: {ACTION}{m.group('rest')}"
        lines[with_at + 1:with_at + 1] = [" " * child + s for s in SECRETS]
        changed += 1
        i = end + len(SECRETS)
    return "\n".join(lines), changed


if __name__ == "__main__":
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()
        text, n = migrate(original)
        if n:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        print(f"{n}\t{path}")
