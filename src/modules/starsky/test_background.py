"""Offline regression checks for StarSky's background check-in boundary."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from modules.starsky import background, core


class BackgroundCheckinTests(unittest.TestCase):
    executable = Path("D:/Program/StarSky/StarSky.exe")

    def invoke(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch.object(Path, "is_file", return_value=True), patch.object(
            background.subprocess, "run", return_value=completed
        ) as runner:
            result = background.run_checkin(self.executable)
        return result, runner

    def test_success_and_already_checked_in_results(self):
        for message in ("领取成功", "今天已经领取过流量 code:109"):
            with self.subTest(message=message):
                stdout = json.dumps({"ok": True, "error": "", "message": message}).encode("utf-8")
                result, _ = self.invoke(stdout)
                self.assertTrue(result["ok"])
                self.assertIn(message, result["message"])

    def test_utf8_bom_is_accepted(self):
        result, _ = self.invoke(b'\xef\xbb\xbf{"ok":true,"message":"done"}')
        self.assertTrue(result["ok"])

    def test_already_signed_flag_has_clear_success_message(self):
        result, _ = self.invoke(b'{"ok":true,"message":"","already_signed":true}')
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "今日已签到（后台确认）")

    def test_stderr_is_never_returned_or_printed(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result, _ = self.invoke(b'{"ok":true,"message":"done"}', stderr=b"PRIVATE_STDERR")
        self.assertTrue(result["ok"])
        self.assertNotIn("PRIVATE_STDERR", repr(result))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_invalid_output_is_not_exposed(self):
        for stdout in (b"PRIVATE_STDOUT", b"\xffPRIVATE_STDOUT", b'noise\n{"ok":true}'):
            with self.subTest(stdout=stdout):
                result, _ = self.invoke(stdout, stderr=b"PRIVATE_STDERR")
                self.assertFalse(result["ok"])
                self.assertNotIn("PRIVATE_", repr(result))

    def test_invalid_json_shapes_and_field_types_fail_closed(self):
        values = [
            None, [], True, {"message": "missing status"},
            {"ok": 1}, {"ok": "true"},
            {"ok": True, "message": []},
            {"ok": True, "error": []},
            {"ok": False, "error": {}},
            {"ok": False, "error": None},
        ]
        for value in values:
            with self.subTest(value=value):
                result, _ = self.invoke(json.dumps(value).encode("utf-8"))
                self.assertFalse(result["ok"])

    def test_nonzero_exit_prevents_success(self):
        result, _ = self.invoke(b'{"ok":true,"message":"done"}', returncode=1)
        self.assertFalse(result["ok"])

    def test_fixed_error_code_maps_to_safe_message(self):
        result, _ = self.invoke(b'{"ok":false,"error":"local_account_missing"}', returncode=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], background.ERROR_MESSAGES["local_account_missing"])

    def test_unknown_error_code_is_not_echoed(self):
        result, _ = self.invoke(b'{"ok":false,"error":"PRIVATE_ERROR"}')
        self.assertFalse(result["ok"])
        self.assertNotIn("PRIVATE_ERROR", result["message"])

    def test_timeout_and_launch_errors_do_not_expose_details(self):
        errors = [
            subprocess.TimeoutExpired("PRIVATE_COMMAND", 1, output=b"PRIVATE_OUTPUT", stderr=b"PRIVATE_STDERR"),
            OSError("PRIVATE_LAUNCH_ERROR"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(
                Path, "is_file", return_value=True
            ), patch.object(background.subprocess, "run", side_effect=error):
                result = background.run_checkin(self.executable)
                self.assertFalse(result["ok"])
                self.assertNotIn("PRIVATE_", repr(result))

    def test_missing_bridge_environment_does_not_launch(self):
        with patch.object(Path, "is_file", return_value=False), patch.object(
            background.subprocess, "run"
        ) as runner:
            result = background.run_checkin(self.executable)
        self.assertFalse(result["ok"])
        runner.assert_not_called()

    def test_child_command_passes_only_executable_path(self):
        _, runner = self.invoke(b'{"ok":true,"message":"done"}')
        command = runner.call_args.args[0]
        self.assertIn("-NonInteractive", command)
        self.assertIn("-NoProfile", command)
        self.assertEqual(command[-2:], ["-ExePath", str(self.executable)])
        self.assertEqual(runner.call_args.kwargs["cwd"], str(self.executable.parent))
        self.assertTrue(runner.call_args.kwargs["capture_output"])


class CheckinConfigTests(unittest.TestCase):
    def test_missing_config_defaults_to_background(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(core.read_config()["签到方式"], "api")

    def test_existing_config_without_mode_defaults_to_background(self):
        with patch.object(Path, "read_text", return_value="[StarSky]\n自动启动=0\n"):
            config = core.read_config()
        self.assertEqual(config["签到方式"], "api")
        self.assertEqual(config["自动启动"], "0")

    def test_explicit_desktop_mode_is_preserved(self):
        with patch.object(Path, "read_text", return_value="[StarSky]\n签到方式=desktop\n"):
            self.assertEqual(core.read_config()["签到方式"], "desktop")


@unittest.skipUnless(os.name == "nt", "StarSky's core dispatcher requires Windows")
class CheckinDispatchTests(unittest.TestCase):
    executable = Path("D:/Program/StarSky/StarSky.exe")

    def test_background_success_and_failure_never_use_desktop(self):
        for ok in (True, False):
            with self.subTest(ok=ok), ExitStack() as stack:
                stack.enter_context(patch.object(core, "read_config", return_value={
                    "签到方式": "api", "软件路径": str(self.executable), "等待超时": "12"
                }))
                resolver = stack.enter_context(patch.object(core, "resolve_exe", return_value=self.executable))
                runner = stack.enter_context(patch.object(background, "run_checkin", return_value={
                    "ok": ok, "message": "safe result"
                }))
                logger = stack.enter_context(patch.object(core, "log"))
                for name in ("is_admin", "Windows", "run_elevated"):
                    stack.enter_context(patch.object(core, name, side_effect=AssertionError("desktop path used")))
                self.assertEqual(core.run_checkin(quiet=True), 0 if ok else 1)
                resolver.assert_called_once_with(str(self.executable))
                runner.assert_called_once_with(self.executable, 12)
                logger.assert_any_call("safe result", True)

    def test_missing_executable_fails_without_starting_background_or_desktop(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, "read_config", return_value={"签到方式": "api", "软件路径": ""}))
            stack.enter_context(patch.object(core, "resolve_exe", return_value=None))
            runner = stack.enter_context(patch.object(background, "run_checkin"))
            stack.enter_context(patch.object(core, "log"))
            stack.enter_context(patch.object(core, "Windows", side_effect=AssertionError("desktop initialized")))
            stack.enter_context(patch.object(core, "is_admin", side_effect=AssertionError("admin checked")))
            self.assertEqual(core.run_checkin(quiet=True), 2)
            runner.assert_not_called()

    def test_explicit_desktop_mode_uses_existing_elevation_path(self):
        with patch.object(core, "read_config", return_value={"签到方式": "desktop"}), patch.object(
            core, "is_admin", return_value=False
        ), patch.object(core, "run_elevated", return_value=(7, "")) as elevated, patch.object(
            background, "run_checkin"
        ) as runner, patch.object(core, "log"):
            self.assertEqual(core.run_checkin(quiet=True), 7)
        elevated.assert_called_once()
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
