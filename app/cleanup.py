"""Remove disposable benchmark workspaces; retain exported evidence and cached binaries."""
import argparse
import fcntl
import json
import os
import shutil
from pathlib import Path

TERMINAL = {'complete', 'failed', 'cancelled', 'interrupted'}


def remove_tree(path):
    def writable_retry(function, target, exc):
        if not isinstance(exc, PermissionError):
            raise exc
        target = Path(target)
        parent = target.parent
        parent.chmod(parent.stat().st_mode | 0o700)
        if not target.is_symlink() and target.is_dir():
            target.chmod(target.stat().st_mode | 0o700)
        function(target)
    shutil.rmtree(path, onexc=writable_retry)


def disposable(job):
    job = Path(job).resolve()
    for root, dirs, _ in os.walk(job, followlinks=False):
        root = Path(root)
        for name in list(dirs):
            path = root / name
            if path.is_symlink():
                dirs.remove(name)
                continue
            if name not in {'work', 'home', 'grading', 'worktree-source'}:
                continue
            # Keep the only copy of an actor DB when export did not succeed.
            if name == 'home' and (path / 'shelley.db').exists() and not (root / 'shelley.db').exists():
                dirs.remove(name)
                continue
            dirs.remove(name)
            yield path


def clean_job(data, key, dry_run=False):
    root = (Path(data) / 'jobs').resolve()
    if not isinstance(key, str) or Path(key).name != key or not key.startswith('job-'):
        raise ValueError('Invalid job ID')
    path = root / key
    if path.is_symlink() or not path.exists():
        return []
    removed = []
    for directory in disposable(path):
        if root not in directory.resolve().parents:
            raise ValueError('Workspace outside jobs directory')
        removed.append(str(directory))
        if not dry_run:
            remove_tree(directory)
    return removed


def finish_job(data, key):
    with (Path(data) / '.cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return clean_job(data, key)


def compact_build(cache, built):
    cache = Path(cache).resolve()
    binary = Path(built['path']).resolve()
    if cache not in binary.parents or not binary.is_file():
        raise ValueError('Build binary outside cache')
    saved = cache / 'shelley'
    if binary != saved:
        shutil.copy2(binary, saved)
    result = {**built, 'path': str(saved), 'binary': str(saved)}
    # Publish the retained binary before removing source/build trees.
    tmp = cache / 'build.json.tmp'
    tmp.write_text(json.dumps(result))
    tmp.replace(cache / 'build.json')
    for child in cache.iterdir():
        if child.is_dir() and not child.is_symlink() and (child.name == 'source' or child.name.startswith('build-')):
            # Build logs are small and useful when investigating a cached binary.
            if (child / 'build.log').is_file():
                shutil.copy2(child / 'build.log', cache / (child.name + '.log'))
            remove_tree(child)
    return result


def sweep(data, dry_run=False):
    from .store import Store
    store = Store(Path(data) / 'bench.sqlite3')
    # Scheduled/manual sweeps must not overlap each other.
    with (Path(data) / '.cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sweep(store, data, dry_run)


def _sweep(store, data, dry_run):
    removed = []
    for job in store.list('jobs'):
        if job.get('status') in TERMINAL:
            removed.extend(clean_job(data, job['id'], dry_run=dry_run))
    return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    removed = sweep(args.data, dry_run=args.dry_run)
    print(json.dumps({'dry_run': args.dry_run, 'directories': len(removed), 'paths': removed}, indent=1))


if __name__ == '__main__':
    main()
