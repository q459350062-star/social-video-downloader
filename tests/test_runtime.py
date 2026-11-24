import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import runtime


class RuntimeJobLifecycleTests(unittest.TestCase):
    def test_create_job_dir_uses_configured_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "jobs"
            with patch.object(runtime, "DOWNLOAD_ROOT", root):
                job = runtime.create_job_dir("edit-")
                self.assertTrue(job.is_dir())
                self.assertEqual(job.parent, root)
                runtime.cleanup_job_dir(job)
                self.assertFalse(job.exists())

    def test_startup_cleanup_removes_only_old_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_job = root / "old-job"
            recent_job = root / "recent-job"
            plain_file = root / "keep.txt"
            old_job.mkdir()
            recent_job.mkdir()
            plain_file.write_text("keep", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(old_job, (old_time, old_time))
            with patch.object(runtime, "DOWNLOAD_ROOT", root):
                removed = runtime.cleanup_stale_jobs(max_age_seconds=3600)
            self.assertEqual(removed, 1)
            self.assertFalse(old_job.exists())
            self.assertTrue(recent_job.exists())
            self.assertTrue(plain_file.exists())

    def test_cleanup_refuses_paths_outside_job_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "jobs"
            root.mkdir()
            outside = Path(temp) / "important.txt"
            outside.write_text("keep", encoding="utf-8")
            with patch.object(runtime, "DOWNLOAD_ROOT", root):
                runtime.cleanup_job_dir(outside)
            self.assertTrue(outside.exists())

    def test_invalid_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(runtime, "DOWNLOAD_ROOT", Path(temp)):
                with self.assertRaises(ValueError):
                    runtime.create_job_dir("../")


if __name__ == "__main__":
    unittest.main()
