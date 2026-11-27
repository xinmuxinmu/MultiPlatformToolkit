import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_checkin import core  # noqa: E402


class QoderResultClassificationTests(unittest.TestCase):
    def test_empty_campaign_list_is_not_claimed_as_already_signed(self):
        with patch.object(core, "_qoder_query", return_value=(200, {
            "claimable": False,
            "campaigns": [],
        })):
            result = core._qoder_flow("https://openapi.qoder.sh", "access", "", dry=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("无法确认今日是否已签", result["message"])

    def test_claim_http_success_with_business_error_is_failure(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "code": 40001,
            "message": "claim unavailable",
        }).encode("utf-8")
        with patch.object(core, "_qoder_query", return_value=(200, {
            "campaigns": [{"id": "daily", "status": "CLAIMABLE"}],
        })), patch("urllib.request.urlopen", return_value=response):
            result = core._qoder_flow("https://openapi.qoder.sh", "access", "", dry=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("claim unavailable", result["message"])


if __name__ == "__main__":
    unittest.main()
