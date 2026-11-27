import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from modules.ai_checkin import core  # noqa: E402


class CheckinConfigMigrationTests(unittest.TestCase):
    def test_legacy_config_enables_every_catalog_platform_once(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(json.dumps({
                "platforms": {item["id"]: False for item in core.PLATFORMS},
                "schedule": {"enabled": False, "time": "08:30"},
            }), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", config_path):
                config = core.load_config()

        self.assertEqual(config["config_version"], core.DEFAULT_CONFIG["config_version"])
        self.assertTrue(all(config["platforms"].values()))
        self.assertFalse(config["schedule"]["enabled"])
        self.assertEqual(config["schedule"]["time"], "08:30")

    def test_current_config_keeps_individual_platform_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(json.dumps({
                "config_version": core.DEFAULT_CONFIG["config_version"],
                "platforms": {"doubao": False},
            }), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", config_path):
                config = core.load_config()

        self.assertFalse(config["platforms"]["doubao"])
        self.assertTrue(config["platforms"]["trae"])


if __name__ == "__main__":
    unittest.main()
