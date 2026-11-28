import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.ai_gateway.connection import build_connection_info


def provider(provider_id, *, chat=True):
    return {"id": provider_id, "name": provider_id.upper(),
            "capabilities": {"chat": chat}}


class ConnectionInfoTests(unittest.TestCase):
    def build(self, providers=None, accounts=None, models=None, base_url="http://127.0.0.1:8765/v1"):
        return build_connection_info(base_url, providers or [], accounts or [], models or [])

    def test_urls_and_client_fields_distinguish_base_and_complete_endpoint(self):
        for source in ("http://127.0.0.1:8765", "http://127.0.0.1:8765/",
                       "http://127.0.0.1:8765/v1", "http://127.0.0.1:8765/v1/"):
            with self.subTest(source=source):
                info = self.build(base_url=source)
                self.assertEqual(info["base_url"], "http://127.0.0.1:8765/v1")
                self.assertEqual(info["endpoints"], {
                    "models": "http://127.0.0.1:8765/v1/models",
                    "chat_completions": "http://127.0.0.1:8765/v1/chat/completions",
                    "messages": "http://127.0.0.1:8765/v1/messages",
                })
                self.assertEqual(info["client_fields"]["base_url"], info["base_url"])
                self.assertEqual(info["client_fields"]["full_url"], info["endpoints"]["chat_completions"])
                self.assertIn("workbuddy/default", info["client_fields"]["model_id_help"])
                self.assertEqual(info["authentication"], {
                    "header": "Authorization", "value_template": "Bearer <API_KEY>",
                    "alternative_header": "x-api-key",
                })

    def test_url_preserves_deployment_path_and_ipv6_without_url_secrets(self):
        info = self.build(base_url="https://private-user:private-pass@[::1]:8765/proxy/v1/?token=private-query#private-fragment")
        self.assertEqual(info["base_url"], "https://[::1]:8765/proxy/v1")
        encoded = json.dumps(info)
        for secret in ("private-user", "private-pass", "private-query", "private-fragment"):
            self.assertNotIn(secret, encoded)
        with self.assertRaises(ValueError):
            self.build(base_url="/relative")

    def test_public_whitelist_ignores_account_identity_and_catalog_metadata_without_mutation(self):
        providers = [provider("workbuddy")]
        providers[0].update({"credential": {"token": "private-provider-token"}})
        providers[0]["capabilities"]["reason"] = "private-provider-reason"
        accounts = [{"provider": "workbuddy", "enabled": True, "id": "private-account-id",
                     "external_id": "private-identity", "label": "private-label",
                     "credential": {"token": "private-account-token"}, "api_key": "private-key"}]
        models = [{"id": "workbuddy/default", "provider": "private-misleading-provider",
                   "owned_by": "private-owner", "encrypted_model_params": "private-signed-model-data",
                   "metadata": {"api_key": "private-model-key"}}]
        before = copy.deepcopy((providers, accounts, models))
        info = self.build(providers, accounts, models)
        self.assertEqual((providers, accounts, models), before)
        self.assertNotIn("private-", json.dumps(info))
        self.assertEqual(set(info["models"][0]), {
            "id", "provider", "provider_name", "api_formats", "input_mode",
            "tools_supported", "agent_compatible", "usage_note"})
        self.assertEqual(set(info["providers"][0]), {
            "id", "name", "chat_enabled", "enabled_accounts", "model_count",
            "api_formats", "input_mode", "tools_supported", "agent_compatible", "usage_note"})
        self.assertEqual(info["models"][0]["provider"], "workbuddy")
        info["models"][0]["api_formats"].clear()
        self.assertEqual(len(info["providers"][0]["api_formats"]), 2)
        self.assertEqual((providers, accounts, models), before)

    def test_single_user_text_platform_limits_are_explicit(self):
        info = self.build([provider("trae"), provider("zhipu")],
                          [{"provider": value, "enabled": True} for value in ("trae", "zhipu")],
                          [{"id": "trae/deepseek-v4.1-flash"}, {"id": "zhipu/default"}])
        for row in info["models"] + info["providers"]:
            self.assertEqual(row["api_formats"], ["openai-chat-completions"])
            self.assertEqual(row["input_mode"], "single_user_text")
            self.assertIs(row["tools_supported"], False)
            self.assertIs(row["agent_compatible"], False)
            for restriction in ("system", "历史", "tools", "图片", "max_tokens", "temperature", "IDE 默认 Agent"):
                self.assertIn(restriction, row["usage_note"])

    def test_other_chat_platforms_do_not_claim_verified_tools_or_agent_support(self):
        info = self.build([provider("workbuddy"), provider("kimi")],
                          [{"provider": "workbuddy", "enabled": True}],
                          [{"id": "workbuddy/default"}, {"id": "kimi/model"}])
        for row in info["models"] + info["providers"]:
            self.assertEqual(row["api_formats"], ["openai-chat-completions", "anthropic-messages"])
            self.assertEqual(row["input_mode"], "messages")
            self.assertIsNone(row["tools_supported"])
            self.assertIsNone(row["agent_compatible"])
            self.assertIn("取决于上游模型", row["usage_note"])
            self.assertIn("未逐项验证", row["usage_note"])

    def test_summary_deduplicates_models_and_only_counts_enabled_accounts(self):
        info = self.build(
            [provider("workbuddy"), provider("trae"), provider("qoder", chat=False)],
            [{"provider": "workbuddy", "enabled": True},
             {"provider": "workbuddy", "enabled": False},
             {"provider": "trae", "enabled": True},
             {"provider": "qoder", "enabled": True}],
            [{"id": value} for value in ("workbuddy/default", "workbuddy/default",
                                         "trae/deepseek-v4.1-flash", "qoder/unavailable")])
        self.assertEqual(info["summary"], {"model_count": 2, "chat_provider_count": 2, "enabled_account_count": 3})
        by_id = {row["id"]: row for row in info["providers"]}
        self.assertEqual(by_id["workbuddy"]["model_count"], 1)
        self.assertEqual(by_id["workbuddy"]["enabled_accounts"], 1)
        self.assertEqual(by_id["qoder"]["api_formats"], [])
        self.assertEqual(by_id["qoder"]["model_count"], 0)
        self.assertIn("尚未接入", by_id["qoder"]["usage_note"])

    def test_invalid_and_unknown_catalog_rows_are_ignored_but_nested_model_names_survive(self):
        info = self.build([provider("workbuddy")], models=[
            None, {}, {"id": None}, {"id": 123}, {"id": "bare"}, {"id": "/model"},
            {"id": "workbuddy/"}, {"id": "workbuddy/  "}, {"id": "unknown/model"},
            {"id": "workbuddy/vendor/model"},
        ])
        self.assertEqual([row["id"] for row in info["models"]], ["workbuddy/vendor/model"])

    def test_example_prefers_workbuddy_then_messages_then_first_catalog_model(self):
        providers = [provider(value) for value in ("trae", "kimi", "workbuddy")]
        cases = [
            (["trae/model", "kimi/model", "workbuddy/default"], "workbuddy/default"),
            (["trae/model", "kimi/model", "workbuddy/other"], "kimi/model"),
            (["trae/model"], "trae/model"),
            ([], "平台/模型"),
        ]
        for model_ids, expected in cases:
            with self.subTest(models=model_ids):
                info = self.build(providers, models=[{"id": value} for value in model_ids])
                self.assertEqual(info["example"], {
                    "method": "POST", "url": info["endpoints"]["chat_completions"],
                    "headers": {"Authorization": "Bearer <API_KEY>", "Content-Type": "application/json"},
                    "body": {"model": expected, "messages": [{"role": "user", "content": "Reply exactly OK"}], "stream": True},
                })

    def test_empty_account_and_catalog_states_have_actionable_notes(self):
        info = self.build([provider("workbuddy"), provider("trae")],
                          [{"provider": "trae", "enabled": True}])
        by_id = {row["id"]: row for row in info["providers"]}
        self.assertIn("没有启用账号", by_id["workbuddy"]["usage_note"])
        self.assertIn("模型目录为空", by_id["workbuddy"]["usage_note"])
        self.assertIn("模型目录为空", by_id["trae"]["usage_note"])
        self.assertNotIn("没有启用账号", by_id["trae"]["usage_note"])
        self.assertEqual(info["example"]["body"]["model"], "平台/模型")


if __name__ == "__main__":
    unittest.main()
