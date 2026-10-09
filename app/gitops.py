"""Read-only git inspection and reproducible local checkout snapshots."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


def _git(path: Path, *args: str, env: dict[str, str] | None = None, text: bool = True) -> str | bytes:
    fullenv = os.environ.copy()
    if env:
        fullenv.update(env)
    p = subprocess.run(["git", "-C", str(path), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=fullenv, text=text)
    if p.returncode:
        error = p.stderr if text else p.stderr.decode(errors="replace")
        raise RuntimeError(f"git {' '.join(args)}: {error.strip()}")
    return p.stdout


def _root(path: Path) -> Path:
    return Path(str(_git(path, "rev-parse", "--show-toplevel")).strip()).resolve()


def _branch(root: Path) -> bool:
    return subprocess.run(["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root)
        return True
    except ValueError:
        return False


def _check_symlink(root: Path, name: str, target: str) -> None:
    link = root / name
    if not _inside(root, link.parent / target):
        raise RuntimeError(f"external symlink is not permitted: {name}")


def _safe_tree(root: Path, ref: str) -> None:
    raw = _git(root, "ls-tree", "-r", "-z", ref, text=False)
    assert isinstance(raw, bytes)
    for row in raw.split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, _, blob = meta.decode().split()
        decoded = name.decode("utf-8", "surrogateescape")
        if decoded.startswith("/") or ".." in Path(decoded).parts:
            raise RuntimeError(f"unsafe git path in {ref}: {decoded!r}")
        if mode == "120000":
            target = _git(root, "cat-file", "-p", blob, text=False)
            assert isinstance(target, bytes)
            _check_symlink(root, decoded, target.decode("utf-8", "surrogateescape"))


def _worktree_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z", text=False)
    assert isinstance(raw, bytes)
    names = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"unsafe untracked path: {name!r}")
        candidate = root / name
        if candidate.is_symlink():
            _check_symlink(root, name, os.readlink(candidate))
    return names


def _safe_index(root: Path, env: dict[str, str]) -> None:
    raw = _git(root, "ls-files", "-s", "-z", env=env, text=False)
    assert isinstance(raw, bytes)
    for row in raw.split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, blob, _ = meta.decode().split()
        if mode == "120000":
            decoded = name.decode("utf-8", "surrogateescape")
            target = _git(root, "cat-file", "-p", blob, env=env, text=False)
            assert isinstance(target, bytes)
            _check_symlink(root, decoded, target.decode("utf-8", "surrogateescape"))


def _worktree_index(root: Path):
    """A private object database/index so staging never writes user git state."""
    temporary = tempfile.TemporaryDirectory(prefix="shelley-bench-git-")
    object_path = Path(str(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects")).strip())
    root_objects = object_path.resolve()
    private = Path(temporary.name) / "objects"
    private.mkdir()
    env = {"GIT_INDEX_FILE": str(Path(temporary.name) / "index"), "GIT_OBJECT_DIRECTORY": str(private),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(root_objects)}
    return temporary, env


def _stage_untracked(root: Path, env: dict[str, str]) -> None:
    names = _worktree_paths(root)
    if names:
        subprocess.run(
            ["git", "-C", str(root), "--literal-pathspecs", "add",
             "--pathspec-from-file=-", "--pathspec-file-nul"],
            input=b"".join(os.fsencode(name) + b"\0" for name in names),
            check=True, env={**os.environ, **env},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )


def worktree_patch(path: Path | str, base_ref: str = "HEAD") -> str:
    """Binary patch for tracked edits and all nonignored untracked files."""
    root = _root(Path(path))
    _safe_tree(root, "HEAD")
    temporary, env = _worktree_index(root)
    try:
        _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-u", env=env)
        _stage_untracked(root, env)
        _safe_index(root, env)
        patch = _git(root, "diff", "--cached", "--binary", base_ref, env=env)
        return str(patch)
    finally:
        temporary.cleanup()


def inspect_repo(path: Path | str) -> dict[str, Any]:
    root = _root(Path(path))
    head = str(_git(root, "rev-parse", "HEAD")).strip()
    branch = str(_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")).strip() if _branch(root) else None
    upstream = None
    if branch:
        try:
            upstream = str(_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")).strip()
        except RuntimeError:
            pass
    return {"path": str(root), "head": head, "branch": branch,
            "dirty": bool(str(_git(root, "status", "--porcelain", "--untracked-files=all")).strip()),
            "upstream_ref": upstream}


def history(path: Path | str, limit: int = 60) -> list[dict[str, Any]]:
    root = _root(Path(path))
    headers = str(_git(root, "log", f"--max-count={max(0, limit)}", "--format=%H%x1f%P%x1f%s")).splitlines()
    answer = []
    for header in headers:
        commit, parents, subject = header.split("\x1f", 2)
        stats = str(_git(root, "show", "--format=", "--numstat", "--no-renames", commit)).splitlines()
        files, adds, dels = [], 0, 0
        for line in stats:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            a, d, name = parts
            files.append(name)
            adds += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        answer.append({"commit": commit, "parent": parents.split()[0] if parents else None,
                       "subject": subject, "files": files, "insertions": adds, "deletions": dels})
    return answer


def tracked_files(path: Path | str, commit: str) -> list[str]:
    return str(_git(_root(Path(path)), "ls-tree", "-r", "--name-only", "-z", commit)).rstrip("\0").split("\0")


def _context_excerpt(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    note = "\n[Excerpt truncated; request specific file ranges for more source.]\n"
    return content[:limit - len(note)] + note


def read_context_file(path: Path | str, commit: str, name: str, start_line: int = 1, end_line: int = 200) -> str:
    if not isinstance(name, str) or name not in tracked_files(path, commit):
        raise ValueError("Requested file is not tracked at this revision")
    if (type(start_line) is not int or type(end_line) is not int
            or start_line < 1 or end_line < start_line or end_line - start_line >= 400):
        raise ValueError("Request a positive line range of at most 400 lines")
    content = str(_git(_root(Path(path)), "show", f"{commit}:{name}"))
    if "\0" in content:
        raise ValueError("Requested file is not text")
    lines = content.splitlines()
    if start_line > max(1, len(lines)):
        raise ValueError(f"Requested start is past the file's {len(lines)} lines")
    excerpt = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start_line - 1:end_line], start_line))
    return f"--- {commit}:{name}; requested lines {start_line}-{end_line}; total {len(lines)} lines ---\n" + _context_excerpt(excerpt, 16000)


def commit_context(path: Path | str, reference_commit: str) -> str:
    root = _root(Path(path))
    diff = str(_git(root, "show", "--format=fuller", "--find-renames", "--no-ext-diff", "--binary", reference_commit))
    changed = str(_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", reference_commit)).splitlines()
    tests = [name for name in tracked_files(root, reference_commit) if "test" in name or "spec" in name]
    related = [name for name in tests if name in changed or any(Path(name).parent == Path(item).parent for item in changed)]
    extras = [name for name in ("go.mod", "package.json", *related) if name not in changed]
    context = ""
    for name in extras[:8]:
        try:
            content = str(_git(root, "show", f"{reference_commit}:{name}"))
        except RuntimeError:
            continue
        context += f"\n\n--- context: {name} ---\n{_context_excerpt(content, 4000)}"
    return _context_excerpt(diff + context, 24000)


def _init_snapshot(dest: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "Shelley Bench"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "bench@local"], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "--no-gpg-sign", "-m", "snapshot"], check=True)
    tree = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD^{tree}"], text=True).strip()
    listing = subprocess.check_output(["git", "-C", str(dest), "ls-tree", "-r", "-z", "HEAD"], text=False)
    return tree, hashlib.sha256(listing).hexdigest()


def snapshot(path: Path | str, dest: Path | str, ref: str = "WORKTREE") -> dict[str, Any]:
    """Materialize a checkout without writing to its source index, objects, or worktree."""
    root, target = _root(Path(path)), Path(dest).resolve()
    if target == root or root in target.parents or target in root.parents:
        raise ValueError("snapshot destination must be outside source checkout")
    if target.exists() and any(target.iterdir()):
        raise ValueError("snapshot destination must be empty")
    target.mkdir(parents=True, exist_ok=True)
    commit = str(_git(root, "rev-parse", "HEAD" if ref == "WORKTREE" else f"{ref}^{{commit}}")).strip()
    _safe_tree(root, commit)
    patch = ""
    if ref == "WORKTREE":
        temporary, env = _worktree_index(root)
        try:
            _git(root, "read-tree", "HEAD", env=env)
            _git(root, "add", "-u", env=env)
            _stage_untracked(root, env)
            _safe_index(root, env)
            patch = str(_git(root, "diff", "--cached", "--binary", "HEAD", env=env))
            subprocess.run(["git", "-C", str(root), "checkout-index", "--all", f"--prefix={target}/"], check=True,
                           env={**os.environ, **env})
        finally:
            temporary.cleanup()
    else:
        with tempfile.TemporaryDirectory(prefix="shelley-bench-archive-") as td:
            archive = Path(td) / "source.tar"
            with archive.open("wb") as f:
                subprocess.run(["git", "-C", str(root), "archive", "--format=tar", commit], check=True, stdout=f)
            subprocess.run(["tar", "-xf", str(archive), "-C", str(target), "--no-same-owner", "--no-same-permissions"], check=True)
    evidence = target.parent / f"{target.name}.source.patch"
    evidence.write_text(patch)
    tree, tree_sha = _init_snapshot(target)
    return {"path": str(target), "commit": commit, "ref": ref, "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "patch_path": str(evidence), "tree": tree, "tree_sha256": tree_sha}
