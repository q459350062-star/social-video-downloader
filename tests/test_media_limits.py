import unittest

from app.media_limits import validate_enhancement_limits


class EnhancementLimitsTests(unittest.TestCase):
    def validate(self, **overrides):
        values = {
            "width": 1280,
            "height": 720,
            "duration": 10,
            "upscale_to": "original",
            "target_fps": 60,
            "source_fps": 30,
            "interpolate": True,
        }
        values.update(overrides)
        return validate_enhancement_limits(**values)

    def test_allows_short_720p_60fps_interpolation(self):
        self.validate()

    def test_rejects_1080p_interpolation_on_free_memory(self):
        with self.assertRaisesRegex(ValueError, "仅支持 720p"):
            self.validate(width=1920, height=1080)

    def test_rejects_long_60fps_interpolation(self):
        with self.assertRaisesRegex(ValueError, "最多 30 秒"):
            self.validate(duration=31)

    def test_rejects_4k_upscale_and_interpolation_combo(self):
        with self.assertRaisesRegex(ValueError, "不能安全执行 4K"):
            self.validate(upscale_to="2160")

    def test_allows_short_4k_upscale_without_interpolation(self):
        self.validate(upscale_to="2160", interpolate=False, target_fps=0, source_fps=30)

    def test_rejects_long_4k_upscale(self):
        with self.assertRaisesRegex(ValueError, "最多 30 秒"):
            self.validate(upscale_to="2160", interpolate=False, target_fps=0, source_fps=30, duration=31)

    def test_rejects_input_above_4k(self):
        with self.assertRaisesRegex(ValueError, "最多处理原生 4K"):
            self.validate(width=4096, height=2160)


if __name__ == "__main__":
    unittest.main()
