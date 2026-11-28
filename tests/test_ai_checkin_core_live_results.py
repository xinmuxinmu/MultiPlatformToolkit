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
    def test_client_status_response_reports_explicitly_claimed_campaign(self):
        with patch.object(core, "_qoder_query", return_value=(200, {
            "showCampaign": True,
            "claimable": False,
            "campaigns": [{"claimStatus": "CLAIMED", "actionType": "VIEW_DETAILS"}],
        })), patch("urllib.request.urlopen") as urlopen:
            result = core._qoder_flow("https://openapi.qoder.sh", "access", "", dry=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "already")
        self.assertIn("今日已签到", result["message"])
        urlopen.assert_not_called()

    def test_visible_nonclaimable_campaign_reports_activity_state(self):
        with patch.object(core, "_qoder_query", return_value=(200, {
            "showCampaign": True,
            "claimable": False,
            "campaigns": [{"claimStatus": "UNCLAIMED"}],
        })):
            result = core._qoder_flow("https://openapi.qoder.sh", "access", "", dry=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("活动存在但当前不可领取", result["message"])
        self.assertIn("无法确认今日是否已签", result["message"])

    def test_qoder_query_uses_desktop_path_and_cosy_headers(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"campaigns": []}'
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            status, payload = core._qoder_query(
                "https://openapi.qoder.sh", {"Authorization": "Bearer test"})

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"campaigns": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://openapi.qoder.sh/sash/api/v1/me/campaigns")
        self.assertEqual(request.get_header("Cosy-clienttype"), "10")
        self.assertEqual(request.get_header("User-agent"), "Qoder")

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

    def test_qoder_reports_the_actual_auth_status_after_cross_site_retry(self):
        with patch.object(core, "_qoder_query", side_effect=[(401, None), (403, None)]):
            result = core._qoder_flow("https://openapi.qoder.sh", "access", "", dry=False)

        self.assertFalse(result["ok"])
        self.assertIn("HTTP 403", result["message"])


if __name__ == "__main__":
    unittest.main()
