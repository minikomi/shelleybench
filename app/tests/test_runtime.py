import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import native, runtime


class RuntimeTests(unittest.TestCase):
    def test_unconfigured_profile_is_unavailable_and_installs_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runtime.RuntimeProfileUnavailable):
                runtime.freeze("threads")
            with self.assertRaises(runtime.RuntimeProfileUnavailable):
                runtime.freeze("configured")
        with tempfile.TemporaryDirectory() as tmp:
            runtime.install(Path(tmp), None)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_explicit_generic_profile_is_frozen_without_personal_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_dir, home = root / "profile", root / "home"
            source = profile_dir / ".config/shelley/AGENTS.md"
            source.parent.mkdir(parents=True)
            source.write_text("Generic operator guidance.\n")
            (profile_dir / "profile.json").write_text(json.dumps({"files": [".config/shelley/AGENTS.md"]}))
            with mock.patch.dict(os.environ, {runtime.PROFILE_DIRECTORY_ENV: str(profile_dir)}, clear=True):
                profile = runtime.freeze("configured")
            runtime.install(home, profile)
            self.assertEqual((home / ".config/shelley/AGENTS.md").read_text(), "Generic operator guidance.\n")
            self.assertFalse((home / "threads-app").exists())

    def test_profile_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profile.json").write_text(json.dumps({"files": ["../secret"]}))
            with mock.patch.dict(os.environ, {runtime.PROFILE_DIRECTORY_ENV: str(root)}, clear=True):
                with self.assertRaises(runtime.RuntimeProfileUnavailable):
                    runtime.freeze("configured")
        with self.assertRaises(ValueError):
            runtime.install(Path("/tmp"), {"name": "configured", "files": {"../bad": "text"}})

    @mock.patch("app.native.shutil.which", side_effect=lambda executable: {
        "bwrap": "/usr/bin/bwrap", "node": "/tools/node/bin/node", "go": "/tools/go/bin/go",
    }.get(executable))
    @mock.patch("app.native.os.path.exists", return_value=False)
    @mock.patch("app.native.os.path.isdir", side_effect=lambda path: path in {"/usr", "/bin"})
    def test_sandbox_discovers_toolchains_without_host_defaults(self, _isdir, _exists, _which):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            work, home = root / "work", root / "home"
            work.mkdir(); home.mkdir()
            command = native.sandbox_command(work, home, ["true"])
        self.assertIn("--ro-bind", command)
        self.assertIn("/tools/node", command)
        self.assertIn("/tools/go", command)
        self.assertIn("/home/shelley/bin:/opt/bench-node/bin:/opt/bench-go/bin:/usr/local/bin:/usr/bin:/bin", command)
        self.assertNotIn("/home/host-user/node", command)
        self.assertNotIn("/exe.dev", command)


if __name__ == "__main__":
    unittest.main()
