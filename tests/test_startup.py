"""Launcher regression tests use temporary fake interpreters and never kill processes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bootstrap
import main as launcher


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.python = bootstrap.environment_python(self.root)
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"fixture")
        (self.root / "requirements.txt").write_text("fixture-package\n", encoding="utf-8")
        self.marker = self.root / ".venv/toolkit-requirements.sha256"

    def test_installed_unchanged_environment_does_not_run_pip(self):
        self.marker.write_text(hashlib.sha256((self.root / "requirements.txt").read_bytes()).hexdigest())
        with patch.object(bootstrap, "interpreter_usable", return_value=True), \
             patch.object(bootstrap, "dependencies_ready", return_value=True), \
             patch.object(bootstrap.subprocess, "run") as run:
            self.assertEqual(bootstrap.prepare_environment(self.root), self.python)
        run.assert_not_called()

    def test_dependency_install_failure_never_marks_environment_ready(self):
        with patch.object(bootstrap, "interpreter_usable", return_value=True), \
             patch.object(bootstrap, "dependencies_ready", return_value=False), \
             patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=23)) as run:
            with self.assertRaisesRegex(RuntimeError, "23"):
                bootstrap.prepare_environment(self.root)
        self.assertIn("pip", run.call_args.args[0])
        self.assertFalse(self.marker.exists())

    def test_missing_interpreter_reports_environment_creation_failure(self):
        self.python.unlink()
        with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "虚拟环境创建失败"):
                bootstrap.prepare_environment(self.root)

    def test_broken_local_environment_is_repaired_using_base_python(self):
        with patch.object(bootstrap, "interpreter_usable", side_effect=[False, True]), \
             patch.object(bootstrap, "dependencies_ready", return_value=True), \
             patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            bootstrap.prepare_environment(self.root)
        self.assertIn("venv", run.call_args_list[0].args[0])
        self.assertTrue(self.marker.exists())

    def test_foreground_flags_and_environment_are_preserved(self):
        for flag in ("--verbose", "--headless", "--check"):
            self.assertTrue(bootstrap.foreground_mode([flag], {}))
        self.assertTrue(bootstrap.foreground_mode([], {"APP_VERBOSE": "1"}))
        if os.name == "nt":
            self.assertFalse(bootstrap.foreground_mode([], {"APP_VERBOSE": "0"}))
        with patch.object(bootstrap, "foreground_mode", return_value=True), \
             patch.object(bootstrap.subprocess, "call", return_value=0) as call:
            bootstrap.launch(self.python, ["--verbose", "--no-schedule"], self.root)
        self.assertEqual(call.call_args.args[0][-2:], ["--verbose", "--no-schedule"])
        self.assertEqual(call.call_args.kwargs["env"]["APP_VERBOSE"], "1")

    def test_background_waits_for_ready_and_keeps_diagnostics_on_failure(self):
        self.python.with_name("pythonw.exe").write_bytes(b"fixture")
        with patch.dict(os.environ, {"APP_DATA_DIR": str(self.root / "data")}), \
             patch.object(bootstrap, "foreground_mode", return_value=False), \
             patch.object(bootstrap.subprocess, "Popen") as popen, \
             patch.object(bootstrap, "wait_for_startup", return_value=(True, "ready")):
            self.assertEqual(bootstrap.launch(self.python, [], self.root), 0)
        self.assertEqual(Path(popen.call_args.args[0][0]).name, "pythonw.exe")
        self.assertIn("APP_STARTUP_STATUS", popen.call_args.kwargs["env"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_startup_handshake_reports_early_exit_and_explicit_error(self):
        path = self.root / "status.json"
        self.assertEqual(bootstrap.wait_for_startup(Mock(poll=lambda: 7), path)[0], False)
        path.write_text(json.dumps({"state": "error", "message": "fixture failure"}), encoding="utf-8")
        self.assertEqual(bootstrap.wait_for_startup(Mock(), path), (False, "fixture failure"))
        path.write_text(json.dumps({"state": "activated"}), encoding="utf-8")
        self.assertTrue(bootstrap.wait_for_startup(Mock(), path)[0])


class MainStartupTests(unittest.TestCase):
    @patch("app.qt_window.available", return_value=True)
    @patch("app.qt_window.run_app_window")
    @patch.object(launcher, "acquire_desktop", return_value=(None, None, True))
    @patch.object(launcher, "port_in_use")
    @patch.object(launcher, "report_startup")
    def test_duplicate_launch_activates_existing_window_without_opening_another(self, report, port, acquire, window, available):
        with patch.dict(os.environ, {"APP_QUIET": "0", "NO_BROWSER": "0"}):
            self.assertEqual(launcher.main([]), 0)
        window.assert_not_called()
        port.assert_not_called()
        report.assert_called_once_with("activated", "已唤起运行中的桌面窗口")

    @patch.object(launcher, "read_runtime", return_value={"mode": "desktop"})
    @patch.object(launcher, "port_in_use", return_value=True)
    @patch("app.qt_window.available", side_effect=AssertionError("headless must not inspect GUI"))
    def test_headless_existing_service_does_not_touch_qt(self, available, port, runtime):
        self.assertEqual(launcher.main(["--headless"]), 0)

    @patch.object(launcher, "read_runtime", side_effect=ValueError("foreign port"))
    @patch.object(launcher, "port_in_use", return_value=True)
    def test_foreign_port_fails_without_terminating_anything(self, port, runtime):
        with self.assertRaisesRegex(RuntimeError, "端口"):
            launcher.main(["--headless"])

    def test_instance_identity_is_stable_and_port_specific(self):
        self.assertEqual(launcher.instance_name("127.0.0.1", 18000), launcher.instance_name("127.0.0.1", 18000))
        self.assertNotEqual(launcher.instance_name("127.0.0.1", 18000), launcher.instance_name("127.0.0.1", 18001))

    def test_errors_write_failure_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.json"
            with patch.dict(os.environ, {"APP_STARTUP_STATUS": str(path)}), \
                 patch.object(launcher, "main", side_effect=RuntimeError("fixture startup failure")), \
                 patch.object(launcher.traceback, "print_exc"):
                self.assertEqual(launcher.entry([]), 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "error")


@unittest.skipUnless(os.name == "nt", "Windows batch integration")
class WindowsBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise unittest.SkipTest("C# fixture compiler is not installed")
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        source = cls.root / "fixture.cs"
        source.write_text('''using System; using System.IO;
class Fixture { static int Main(string[] args) {
 string executable = Environment.GetCommandLineArgs()[0];
 string name = Path.GetFileNameWithoutExtension(executable).ToUpperInvariant();
 string kind = executable.Contains(".venv") ? "LOCAL" : name == "PY" ? "PY" : "PYTHON";
 if (Array.IndexOf(args, "-c") >= 0) return Int32.Parse(Environment.GetEnvironmentVariable("FIXTURE_"+kind+"_PROBE") ?? "0");
 File.AppendAllText(Environment.GetEnvironmentVariable("FIXTURE_TRACE"), kind+"|"+String.Join(" ", args)+Environment.NewLine);
 return Int32.Parse(Environment.GetEnvironmentVariable("FIXTURE_EXIT") ?? "0");
} }''', encoding="utf-8")
        cls.stub = cls.root / "fixture.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(cls.stub), str(source)], capture_output=True)
        if result.returncode:
            raise RuntimeError("Could not compile harmless interpreter fixture")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_batch(self, *, local=True, local_probe=0, py_probe=0, python_probe=0, exit_code=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root / "启动.bat"
            batch.write_bytes((bootstrap.ROOT / "启动.bat").read_bytes())
            binary = root / "fake-bin"
            binary.mkdir()
            for name in ("py.exe", "python.exe"):
                shutil.copy2(self.stub, binary / name)
            if local:
                scripts = root / ".venv/Scripts"
                scripts.mkdir(parents=True)
                shutil.copy2(self.stub, scripts / "python.exe")
            trace = root / "trace.txt"
            environment = os.environ.copy()
            environment.update(PATH=str(binary) + os.pathsep + str(Path(os.environ["WINDIR"]) / "System32"),
                FIXTURE_TRACE=str(trace), FIXTURE_LOCAL_PROBE=str(local_probe), FIXTURE_PY_PROBE=str(py_probe),
                FIXTURE_PYTHON_PROBE=str(python_probe), FIXTURE_EXIT=str(exit_code))
            result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", str(batch), "--check", "--no-schedule"],
                cwd=root, env=environment, input=b"\n", capture_output=True, timeout=15)
            return result.returncode, trace.read_text(encoding="utf-8") if trace.exists() else "", result.stdout.decode("utf-8", errors="replace")

    def test_local_interpreter_has_priority_and_arguments_survive(self):
        code, trace, output = self.run_batch()
        self.assertEqual(code, 0, output)
        self.assertTrue(trace.startswith("LOCAL|"), trace)
        self.assertIn("--check --no-schedule", trace)

    def test_py_launcher_fallback(self):
        code, trace, output = self.run_batch(local_probe=1)
        self.assertEqual(code, 0, output)
        self.assertTrue(trace.startswith("PY|"), trace)

    def test_python_path_fallback_without_py_launcher(self):
        code, trace, output = self.run_batch(local=False, py_probe=1)
        self.assertEqual(code, 0, output)
        self.assertTrue(trace.startswith("PYTHON|"), trace)

    def test_missing_all_interpreters_has_explicit_error(self):
        code, trace, output = self.run_batch(local=False, py_probe=1, python_probe=1)
        self.assertEqual(code, 9009, output)
        self.assertEqual(trace, "")
        self.assertIn("Python 3.10", output)

    def test_startup_failure_is_not_silently_swallowed(self):
        code, trace, output = self.run_batch(exit_code=17)
        self.assertEqual(code, 17, output)
        self.assertIn("17", output)


if __name__ == "__main__":
    unittest.main()
