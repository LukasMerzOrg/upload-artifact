"""Upload a workflow artifact to Garage (S3) instead of GitHub's artifact store.

Reads the same inputs as actions/upload-artifact from INPUT_* environment
variables (set by action.yml), uploads, and writes a download link to the job
summary and the `artifact-url` output.
"""
import datetime
import glob
import mimetypes
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3 import MAX_PRESIGN_SECONDS, S3  # noqa: E402

# Objects are stored under ttl-<days>d/ and removed by the bucket's lifecycle
# rules (setup_lifecycle.py). retention-days is rounded up to the next bucket.
TTL_DAYS = (3, 7, 14, 30, 90)

mimetypes.add_type("application/vnd.android.package-archive", ".apk")
mimetypes.add_type("application/octet-stream", ".aab")


def ttl_for(retention_days):
    if not str(retention_days or "").strip():
        return TTL_DAYS[-1]
    wanted = int(retention_days)
    return next((d for d in TTL_DAYS if d >= wanted), TTL_DAYS[-1])


def _hidden(path, root):
    return any(part.startswith(".") for part in os.path.relpath(path, root).split(os.sep))


def find_files(patterns, workspace, include_hidden=False):
    """Resolves upload-artifact style path patterns to (files, root).

    One pattern per line, globs with ** allowed, a leading ! excludes. A
    matched directory contributes every file below it. `root` is the directory
    the paths inside the archive are relative to.
    """
    included, excluded = {}, set()  # included: file -> directory it was found under
    for line in patterns.splitlines():
        line = line.strip()
        if not line:
            continue
        negate = line.startswith("!")
        pattern = os.path.normpath(os.path.join(workspace, line[1:] if negate else line))
        for match in glob.glob(pattern, recursive=True, include_hidden=True):
            if os.path.isdir(match):
                found = [(os.path.join(d, n), match) for d, _, names in os.walk(match) for n in names]
            else:
                found = [(match, os.path.dirname(match))]
            for path, base in found:
                if negate:
                    excluded.add(path)
                else:
                    included.setdefault(path, base)
    files = sorted(f for f in included if f not in excluded)
    if not files:
        return [], workspace
    root = os.path.commonpath([included[f] for f in files])
    if not include_hidden:
        files = [f for f in files if not _hidden(f, root)]
    return files, root


def make_zip(files, root, dest, level):
    compression = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(dest, "w", compression,
                         compresslevel=None if level == 0 else level) as z:
        for f in files:
            z.write(f, os.path.relpath(f, root).replace(os.sep, "/"))


def _size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB"


def main():
    env = os.environ
    inp = lambda name, default="": env.get(f"INPUT_{name}", "").strip() or default  # noqa: E731
    name = inp("NAME", "artifact")
    files, root = find_files(inp("PATH"), env.get("GITHUB_WORKSPACE", os.getcwd()),
                             inp("INCLUDE_HIDDEN_FILES", "false") == "true")
    if not files:
        message = f"No files were found with the provided path: {inp('PATH')!r}. No artifact uploaded."
        mode = inp("IF_NO_FILES_FOUND", "warn")
        if mode == "error":
            print(f"::error::{message}")
            return 1
        print(f"::warning::{message}" if mode == "warn" else message)
        return 0

    for required in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        if not inp(required):
            print(f"::error::input '{required.lower().replace('_', '-')}' is empty "
                  "(is the repository secret set?)")
            return 1

    ttl = ttl_for(inp("RETENTION_DAYS"))
    run = f"{env.get('GITHUB_RUN_ID', 'local')}-{env.get('GITHUB_RUN_ATTEMPT', '1')}"
    prefix = f"ttl-{ttl}d/{env.get('GITHUB_REPOSITORY', 'local')}/{run}"

    with tempfile.TemporaryDirectory() as tmp:
        if len(files) == 1:
            # A single file goes up as it is, so e.g. an APK link installs directly.
            source = files[0]
            key = f"{prefix}/{name}/{os.path.basename(source)}"
        else:
            source = os.path.join(tmp, "artifact.zip")
            make_zip(files, root, source, int(inp("COMPRESSION_LEVEL", "6")))
            key = f"{prefix}/{name}.zip"
        size = os.path.getsize(source)
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        s3 = S3(inp("ENDPOINT"), inp("REGION"), inp("ACCESS_KEY_ID"), inp("SECRET_ACCESS_KEY"))
        s3.put_file(inp("BUCKET"), key, source, content_type)

    link_seconds = min(ttl * 86400, MAX_PRESIGN_SECONDS)
    url = s3.presign_get(inp("BUCKET"), key, link_seconds)
    now = datetime.datetime.now(datetime.timezone.utc)
    link_until = (now + datetime.timedelta(seconds=link_seconds)).strftime("%Y-%m-%d")
    deleted = (now + datetime.timedelta(days=ttl)).strftime("%Y-%m-%d")
    print(f"Uploaded {len(files)} file(s) as s3://{inp('BUCKET')}/{key} ({_size(size)})")
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8", newline="\n") as f:
            f.write(f"**Artifact [{name}]({url})** — {_size(size)}, {len(files)} file(s). "
                    f"Link valid until {link_until}; stored in Garage as "
                    f"`{inp('BUCKET')}/{key}` until {deleted}.\n\n")
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as f:
            f.write(f"artifact-url={url}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
