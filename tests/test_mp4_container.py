import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.main import ensure_mp4_container


class Mp4ContainerTests(unittest.TestCase):
    def test_mp4_input_is_returned_without_processing(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "already.mp4"
            source.write_bytes(b"mp4")
            with patch("app.main.subprocess.run") as run:
                result = ensure_mp4_container(source)
            self.assertEqual(result, source)
            run.assert_not_called()

    def test_mkv_is_remuxed_to_mp4_without_reencoding_video(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            source.write_bytes(b"mkv")
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if command[0] == "ffprobe":
                    return SimpleNamespace(stdout=json.dumps({
                        "streams": [
                            {"codec_type": "video", "codec_name": "hevc"},
                            {"codec_type": "audio", "codec_name": "aac"},
                        ]
                    }))
                Path(command[-1]).write_bytes(b"valid-mp4")
                return SimpleNamespace(stdout="")

            with patch("app.main.subprocess.run", side_effect=fake_run):
                result = ensure_mp4_container(source)

            self.assertEqual(result.suffix, ".mp4")
            self.assertTrue(result.exists())
            self.assertFalse(source.exists())
            ffmpeg_command = next(command for command in calls if command[0] == "ffmpeg")
            self.assertIn("-c:v", ffmpeg_command)
            self.assertIn("copy", ffmpeg_command)
            self.assertIn("-tag:v", ffmpeg_command)
            self.assertIn("hvc1", ffmpeg_command)
            self.assertIn("-f", ffmpeg_command)
            self.assertIn("mp4", ffmpeg_command)


if __name__ == "__main__":
    unittest.main()
