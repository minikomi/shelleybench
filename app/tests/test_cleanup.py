import json
import tempfile
import unittest
from pathlib import Path

from app import cleanup
from app.store import Store


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, path, text='evidence'):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_finished_jobs_only_and_evidence_retained(self):
        store = Store(self.root / 'bench.sqlite3')
        store.put('jobs', {'id': 'job-old', 'status': 'complete'})
        store.put('jobs', {'id': 'job-live', 'status': 'running'})
        live = self.file('jobs/job-live/runs/run/work/source')
        old = self.file('jobs/job-old/runs/run/work/source')
        self.file('jobs/job-old/runs/run/grading/source')
        home = self.file('jobs/job-old/runs/run/agent/home/go/module/LICENSE')
        home.parent.chmod(0o555)
        evidence = [self.file('jobs/job-old/runs/run/' + p) for p in
                    ('candidate.patch', 'agent/shelley.db', 'agent/trajectory.full.jsonl', 'grader/acceptance.log')]
        original = store.list('jobs')
        preview = cleanup.sweep(self.root, dry_run=True)
        self.assertTrue(old.exists())
        self.assertEqual(len(preview), 3)
        cleanup.sweep(self.root)
        self.assertTrue(live.exists())
        self.assertFalse(old.exists())
        self.assertTrue(all(p.read_text() == 'evidence' for p in evidence))
        self.assertEqual(store.list('jobs'), original)
        self.assertEqual(cleanup.sweep(self.root), [])

    def test_unexported_db_and_symlink_are_not_removed(self):
        db = self.file('jobs/job-safe/agent/home/shelley.db')
        outside = self.file('outside/source')
        (self.root / 'jobs/job-safe/work').symlink_to(outside.parent, target_is_directory=True)
        cleanup.clean_job(self.root, 'job-safe')
        self.assertTrue(db.exists())
        self.assertEqual(outside.read_text(), 'evidence')
        with self.assertRaises(ValueError):
            cleanup.clean_job(self.root, '../outside')

    def test_build_binary_retained_and_manifest_reusable(self):
        binary = self.file('builds/sha/build-123/work/bin/shelley', 'executable')
        binary.chmod(0o755)
        self.file('builds/sha/build-123/home/go-cache/large')
        self.file('builds/sha/build-123/build.log', 'build log')
        self.file('builds/sha/source/source.go')
        cache = self.root / 'builds/sha'
        built = cleanup.compact_build(cache, {'path': str(binary), 'binary': str(binary), 'provenance': {'binary_sha256': 'sha'}})
        self.assertEqual(Path(built['path']).read_text(), 'executable')
        self.assertTrue(Path(built['path']).stat().st_mode & 0o111)
        self.assertEqual(json.loads((cache / 'build.json').read_text()), built)
        self.assertEqual((cache / 'build-123.log').read_text(), 'build log')
        self.assertFalse((cache / 'build-123').exists())
        self.assertFalse((cache / 'source').exists())
        self.assertEqual(cleanup.compact_build(cache, built), built)


if __name__ == '__main__':
    unittest.main()
