"""CAPTCHA handoff has no SMS side effects and only resumes its own challenge."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from modules.heybox import captcha_bridge as bridge, core, routes


class HeyboxCaptchaBridgeTests(unittest.TestCase):
    def setUp(self):
        self.current = {"stage": "captcha", "captcha_id": "challenge-A",
                        "captcha_for": "send", "captcha_app_id": "12345",
                        "expires_in": 180}
        bridge._BRIDGES.clear()
        self.status = patch.object(core, "app_login_status", side_effect=lambda: dict(self.current))
        self.status.start()
        self.addCleanup(self.status.stop)
        self.addCleanup(bridge._BRIDGES.clear)
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def open(self):
        response = self.client.post("/heybox/api/app-login/captcha/open", json={
            "captcha_id": self.current["captcha_id"],
            "captcha_for": self.current["captcha_for"]})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def result(self, token):
        response = self.client.get("/heybox/api/app-login/captcha/result/" + token)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def test_complete_flow_only_hands_proof_back_once(self):
        with patch.object(core, "app_login_send_code") as send, \
                patch.object(core, "app_login_verify") as verify:
            opened = self.open()
            token = opened["bridge_id"]
            self.assertEqual(self.result(token), {"ok": True, "stage": "waiting"})
            page = self.client.get(opened["url"])
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers["referrer-policy"], "no-referrer")
            self.assertIn('captcha.show()', page.text)
            self.assertNotIn('phone_num', page.text)
            self.assertNotIn('login_code', page.text)
            self.assertNotIn('challenge-A', page.text)
            proof = {"ticket": "ticket-A", "randstr": "random-A"}
            response = self.client.post(opened["url"], json=proof)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.result(token), {"ok": True, "stage": "ready", "captcha": proof})
            self.assertEqual(self.result(token)["stage"], "expired")
            send.assert_not_called()
            verify.assert_not_called()

    def test_same_challenge_reuses_active_bridge(self):
        first = self.open()
        self.assertEqual(self.open(), first)
        self.assertGreaterEqual(len(first["bridge_id"]), 40)

    def test_new_challenge_invalidates_old_page_and_result(self):
        first = self.open()
        self.current["captcha_id"] = "challenge-B"
        self.assertEqual(self.client.get(first["url"]).status_code, 410)
        self.assertEqual(self.result(first["bridge_id"])["stage"], "expired")
        self.assertNotEqual(self.open()["bridge_id"], first["bridge_id"])

    def test_stage_change_rejects_browser_submission(self):
        opened = self.open()
        self.current["stage"] = "code"
        response = self.client.post(opened["url"], json={"ticket": "ticket", "randstr": "random"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_send_proof_cannot_resume_verify_challenge(self):
        opened = self.open()
        self.current["captcha_for"] = "verify"
        self.assertEqual(self.result(opened["bridge_id"])["stage"], "expired")

    def test_open_rejects_stale_id_or_wrong_phase(self):
        for body in ({"captcha_id": "old", "captcha_for": "send"},
                     {"captcha_id": "challenge-A", "captcha_for": "verify"},
                     {"captcha_id": "", "captcha_for": "send"}):
            response = self.client.post("/heybox/api/app-login/captcha/open", json=body)
            self.assertEqual(response.status_code, 400)
        self.assertFalse(bridge._BRIDGES)

    def test_expiry_invalidates_ready_proof(self):
        opened = self.open()
        self.client.post(opened["url"], json={"ticket": "ticket", "randstr": "random"})
        bridge._BRIDGES[opened["bridge_id"]]["expires_at"] = 0
        self.assertEqual(self.result(opened["bridge_id"])["stage"], "expired")

    def test_invalid_or_duplicate_proofs_are_rejected(self):
        opened = self.open()
        for proof in ({}, {"ticket": 1, "randstr": "random"},
                      {"ticket": "ticket", "randstr": ""},
                      {"ticket": "x" * 2049, "randstr": "random"},
                      {"ticket": "ticket\n", "randstr": "random"}):
            self.assertEqual(self.client.post(opened["url"], json=proof).status_code, 400)
        proof = {"ticket": "ticket", "randstr": "random"}
        self.assertEqual(self.client.post(opened["url"], json=proof).status_code, 200)
        self.assertEqual(self.client.post(opened["url"], json=proof).status_code, 400)
        self.assertEqual(self.result(opened["bridge_id"])["stage"], "ready")

    def test_invalid_app_id_cannot_enter_html(self):
        self.current["captcha_app_id"] = '</script><script>alert(1)</script>'
        response = self.client.post("/heybox/api/app-login/captcha/open", json={
            "captcha_id": "challenge-A", "captcha_for": "send"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('alert(1)', response.text)

    def test_cancel_route_uses_core_state_without_touching_sms(self):
        with patch.object(core, "app_login_cancel", return_value={"stage": "idle"}) as cancel:
            response = self.client.post("/heybox/api/app-login/cancel")
        self.assertEqual(response.json(), {"ok": True, "stage": "idle"})
        cancel.assert_called_once_with()

    def test_cancel_busy_request_keeps_current_status(self):
        with patch.object(core, "app_login_cancel", side_effect=core.HeyboxError("请求正在处理")):
            response = self.client.post("/heybox/api/app-login/cancel")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["captcha_id"], "challenge-A")


if __name__ == "__main__":
    unittest.main()
