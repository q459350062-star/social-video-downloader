import unittest

from app.video_enhance import build_detail_filters, build_video_rate_args


class DetailFilterTests(unittest.TestCase):
    def test_default_is_mild_and_ordered(self):
        filters = build_detail_filters()
        self.assertEqual(len(filters), 2)
        self.assertTrue(filters[0].startswith("hqdn3d="))
        self.assertTrue(filters[1].startswith("unsharp="))

    def test_can_disable_both(self):
        self.assertEqual(build_detail_filters(sharpen="off", denoise="off"), [])

    def test_rejects_unknown_sharpen(self):
        with self.assertRaises(ValueError):
            build_detail_filters(sharpen="maximum")

    def test_4k_final_pass_targets_40mbps_and_caps_50mbps(self):
        self.assertEqual(build_video_rate_args(upscale_to="2160"), ["-b:v", "40M", "-maxrate", "50M", "-bufsize", "80M"])

    def test_staged_first_pass_uses_lower_bitrate(self):
        self.assertEqual(build_video_rate_args(upscale_to="2160", staged=True), ["-b:v", "18M", "-maxrate", "25M", "-bufsize", "36M"])

    def test_rejects_unknown_denoise(self):
        with self.assertRaises(ValueError):
            build_detail_filters(denoise="maximum")


if __name__ == "__main__":
    unittest.main()
