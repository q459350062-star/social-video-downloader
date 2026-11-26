import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.mp4 import ensure_mp4_container


class Mp4ContainerTests(unittest.TestCase):
    def test_compatible_mp4_is_returned_without_processing(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "already.mp4"
            source.write_bytes(b"mp4")
            with patch("app.mp4.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({
                "streams": [{"codec_type": "video", "codec_name": "h264"},
                            {"codec_type": "audio", "codec_name": "aac"}]
            }))) as run:
                result = ensure_mp4_container(source)
            self.assertEqual(result, source)
            run.assert_called_once()

    def test_vp9_mp4_is_transcoded_to_h264_even_when_container_is_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            source.write_bytes(b"mp4")
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if command[0] == "ffprobe":
                    codec = "vp9" if len([c for c in calls if c[0] == "ffprobe"]) == 1 else "h264"
                    return SimpleNamespace(stdout=json.dumps({"streams": [
                        {"codec_type": "video", "codec_name": codec},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]}))
                Path(command[-1]).write_bytes(b"compatible-mp4")
                return SimpleNamespace(stdout="")

            with patch("app.mp4.subprocess.run", side_effect=fake_run):
                result = ensure_mp4_container(source)

            self.assertEqual(result.suffix, ".mp4")
            self.assertTrue(result.exists())
            self.assertFalse(source.exists())
            ffmpeg = next(command for command in calls if command[0] == "ffmpeg")
            self.assertIn("libx264", ffmpeg)
            self.assertIn("yuv420p", ffmpeg)
            self.assertIn("ultrafast", ffmpeg)

    def test_source_mode_preserves_vp9_file_without_transcoding(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.webm"
            original = b"original-vp9-webm"
            source.write_bytes(original)
            with patch("app.mp4.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({
                "streams": [
                    {"codec_type": "video", "codec_name": "vp9"},
                    {"codec_type": "audio", "codec_name": "opus"},
                ]
            }))) as run:
                result = ensure_mp4_container(source, preserve_source=True)
            self.assertEqual(result, source)
            self.assertEqual(result.read_bytes(), original)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], "ffprobe")

    def test_mkv_with_hevc_is_remuxed_without_reencoding_video(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            source.write_bytes(b"mkv")
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if command[0] == "ffprobe":
                    return SimpleNamespace(stdout=json.dumps({"streams": [
                        {"codec_type": "video", "codec_name": "hevc"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]}))
                Path(command[-1]).write_bytes(b"valid-mp4")
                return SimpleNamespace(stdout="")

            with patch("app.mp4.subprocess.run", side_effect=fake_run):
                result = ensure_mp4_container(source)

            self.assertEqual(result.suffix, ".mp4")
            self.assertTrue(result.exists())
            self.assertFalse(source.exists())
            ffmpeg = next(command for command in calls if command[0] == "ffmpeg")
            self.assertIn("copy", ffmpeg)
            self.assertIn("hvc1", ffmpeg)


if __name__ == "__main__":
    unittest.main()
