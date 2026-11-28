import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.heybox.device_token import TOKEN_RE  # noqa: E402


class DeviceTokenShapeTests(unittest.TestCase):
    def test_official_b_token_shape(self):
        self.assertIsNotNone(TOKEN_RE.fullmatch("B" + "A" * 88))

    def test_invalid_token_shapes_are_rejected(self):
        self.assertIsNone(TOKEN_RE.fullmatch("D" + "A" * 88))
        self.assertIsNone(TOKEN_RE.fullmatch("B" + "A" * 87))
        self.assertIsNone(TOKEN_RE.fullmatch("B" + "A" * 88 + "Z"))


if __name__ == "__main__":
    unittest.main()
