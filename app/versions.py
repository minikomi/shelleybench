"""Read-only names and metadata for choosing Shelley versions."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


_FORMAT = "%H%x00%s%x00%an%x00%aI%x00%P"


def _git(repo: Path | str, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "--no-pager", *args],
        capture_output=True, timeout=15,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "Cannot read Shelley versions")
    return result.stdout


def _commits(raw: bytes) -> list[dict]:
    fields = raw.decode("utf-8", "replace").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    rows = []
    for i in range(0, len(fields), 5):
        ref, subject, author, date, parents = fields[i:i + 5]
        rows.append({"ref": ref, "subject": subject, "author": author,
                     "date": date, "parents": parents.split()})
    return rows


def _ref(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200 or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid version selection")
    return value


def describe(repo: Path | str, ref: str) -> dict:
    ref = _ref(ref)
    if ref == "WORKTREE":
        head = describe(repo, "HEAD")
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
        paths = _git(repo, "status", "--porcelain=v1", "-z").split(b"\0")
        count, i = 0, 0
        while i < len(paths) and paths[i]:
            count += 1
            i += 2 if b"R" in paths[i][:2] or b"C" in paths[i][:2] else 1
        return {"ref": ref, "subject": "Current local code", "head": head["ref"],
                "head_subject": head["subject"], "author": head["author"], "date": head["date"],
                "branch": "" if branch == "HEAD" else branch, "dirty": bool(count), "changed_files": count}
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    return _commits(_git(repo, "log", "-1", "-z", f"--format={_FORMAT}", sha, "--"))[0]


def catalog(repo: Path | str, refs: list[str] | tuple[str, ...] = (), query: str = "") -> dict:
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("Keep version searches under 200 characters")
    if len(refs) > 20:
        raise ValueError("Too many selected versions")
    args = ["log", "--all", "-n", "100", "-z", f"--format={_FORMAT}"]
    if query.strip():
        args += ["--fixed-strings", "--regexp-ignore-case", f"--grep={query.strip()}"]
    commits = _commits(_git(repo, *args, "--"))
    raw_refs = _git(repo, "for-each-ref", "--sort=-creatordate", "--count=200",
                    "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(*objectname)%00%(*objecttype)"
                    "%00%(subject)%00%(authorname)%00%(authordate:iso-strict)"
                    "%00%(*subject)%00%(*authorname)%00%(*authordate:iso-strict)",
                    "refs/heads", "refs/remotes", "refs/tags")
    named = []
    for line in raw_refs.decode("utf-8", "replace").splitlines():
        name, sha, kind, peeled, peeled_kind, subject, author, date, tag_subject, tag_author, tag_date = line.split("\0")
        if kind != "commit":
            if peeled_kind != "commit":
                continue
            sha = peeled
            subject, author, date = tag_subject, tag_author, tag_date
        prefix = next(p for p in ("refs/heads/", "refs/remotes/", "refs/tags/") if name.startswith(p))
        named.append({"ref": name, "label": name[len(prefix):],
                      "kind": "tag" if prefix == "refs/tags/" else "branch", "commit": sha,
                      "subject": subject, "author": author, "date": date})
    worktree = describe(repo, "WORKTREE")
    selected, errors = {}, {}
    for ref in dict.fromkeys(refs):
        _ref(ref)
        try:
            selected[ref] = worktree if ref == "WORKTREE" else describe(repo, ref)
        except ValueError:
            errors[ref] = "This saved version is no longer available in the configured checkout."
    return {"commits": commits, "refs": named, "worktree": worktree,
            "selected": selected, "errors": errors}
