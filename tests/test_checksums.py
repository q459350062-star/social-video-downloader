import hashlib
import io
import unittest

from app.checksums import calculate_file_hashes


class ChecksumTests(unittest.TestCase):
    def test_streams_md5_and_sha256(self):
        payload = b"sample video bytes\x00\x01\x02"
        result = calculate_file_hashes(io.BytesIO(payload), chunk_size=4)
        self.assertEqual(result["size_bytes"], len(payload))
        self.assertEqual(result["md5"], hashlib.md5(payload, usedforsecurity=False).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())

    def test_rejects_empty_file(self):
        with self.assertRaisesRegex(ValueError, "文件为空"):
            calculate_file_hashes(io.BytesIO(b""))

    def test_rejects_file_over_limit(self):
        with self.assertRaisesRegex(ValueError, "大小限制"):
            calculate_file_hashes(io.BytesIO(b"12345"), max_bytes=4, chunk_size=2)


if __name__ == "__main__":
    unittest.main()
