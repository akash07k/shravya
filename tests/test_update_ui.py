import importlib
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def module(name, **values):
	result = types.ModuleType(name)
	result.__dict__.update(values)
	return result


class ImmediateThread:
	def __init__(self, *, target, args=(), daemon=False):
		self.target = target
		self.args = args

	def start(self):
		self.target(*self.args)


class Timer:
	def __init__(self, delay, callback, *args):
		self.delay = delay
		self.callback = callback
		self.args = args
		self.Stop = Mock()

	def fire(self):
		self.callback(*self.args)


class TestUpdateUI(unittest.TestCase):
	def setUp(self):
		self.queue = []
		self.timers = []
		self.messages = Mock()
		self.frame = Mock()
		self.secure = Mock(return_value=False)
		self.install = Mock()
		self.dialogs = []
		self.dialog_answer = 5104
		test = self

		class MessageDialog:
			def __init__(self, parent, message, title, buttons):
				self.message = message
				self.buttons = buttons
				self.contents = types.SimpleNamespace(addLabeledControl=Mock())
				self.escape = Mock()
				self._addContents(self.contents)
				test.dialogs.append(self)

			def SetEscapeId(self, code):
				self.escape(code)
				self.escape_code = code

			def scaleSize(self, size):
				return size

			def ShowModal(self):
				return self.escape_code if test.dialog_answer == "close" else test.dialog_answer

		def later(delay, callback, *args):
			timer = Timer(delay, callback, *args)
			self.timers.append(timer)
			return timer

		self.wx = module(
			"wx",
			YES=1,
			NO=2,
			OK=4,
			ICON_INFORMATION=8,
			ICON_ERROR=16,
			TE_MULTILINE=32,
			TE_READONLY=64,
			TextCtrl=type("TextCtrl", (), {}),
			CallAfter=lambda callback, *args: self.queue.append((callback, args)),
			CallLater=later,
		)
		package = module("update_ui_test")
		package.__path__ = [str(SOURCE)]
		gui = module("gui", mainFrame=self.frame, messageBox=self.messages)
		gui.__path__ = []
		self.return_codes = types.SimpleNamespace(YES=5103, NO=5104)
		gui.message = module(
			"gui.message",
			MessageDialog=MessageDialog,
			DefaultButtonSet=types.SimpleNamespace(YES_NO=(5103, 5104)),
			ReturnCode=self.return_codes,
		)
		modules = {
			"update_ui_test": package,
			"addonHandler": module(
				"addonHandler",
				initTranslation=lambda: None,
				AddonError=type("AddonError", (Exception,), {}),
				getCodeAddon=lambda: types.SimpleNamespace(manifest={"version": "1.0.0"}),
			),
			"gui": gui,
			"gui.message": gui.message,
			"ui": module("ui", isRunningOnSecureDesktop=self.secure),
			"wx": self.wx,
			"logHandler": module("logHandler", log=Mock()),
			"update_ui_test._update_install": module(
				"update_ui_test._update_install", install_update=self.install
			),
		}
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		translation = patch("builtins._", lambda value: value, create=True)
		translation.start()
		self.addCleanup(translation.stop)
		for name in list(sys.modules):
			if name.startswith("update_ui_test.") and name != "update_ui_test._update_install":
				del sys.modules[name]
		self.updater = importlib.import_module("update_ui_test._update_ui")
		self.updater.Thread = ImmediateThread
		self.check = Mock()
		self.updater.check_release = self.check
		self.messages.return_value = self.wx.NO
		self.service = self.updater.Updater("http://127.0.0.1/latest")
		self.addCleanup(self.service.terminate)

	def run_queued(self):
		callback, args = self.queue.pop(0)
		callback(*args)

	def complete_check(self):
		self.run_queued()
		self.run_queued()

	def test_no_source_does_not_schedule_a_check(self):
		self.queue.clear()
		other = self.updater.Updater(None)
		try:
			self.assertEqual(self.queue, [])
			self.check.assert_not_called()
		finally:
			other.terminate()

	def test_daily_check_prompts_before_install(self):
		update = self.updater.Update("1.2.3", "http://127.0.0.1/update", "A speech improvement.")
		self.check.return_value = update
		self.dialog_answer = self.return_codes.YES
		self.complete_check()
		self.check.assert_called_once_with("http://127.0.0.1/latest", "1.0.0")
		self.install.assert_called_once()
		self.assertEqual(self.install.call_args.args[0], update)
		dialog = self.dialogs[-1]
		self.assertIn("1.2.3", dialog.message)
		self.assertIn("Download and install", dialog.message)
		self.assertEqual(dialog.buttons, (self.return_codes.YES, self.return_codes.NO))
		label, control = dialog.contents.addLabeledControl.call_args.args
		self.assertIn("Release notes", label)
		self.assertIs(control, self.wx.TextCtrl)
		self.assertEqual(dialog.contents.addLabeledControl.call_args.kwargs["value"], "A speech improvement.")
		self.assertEqual(
			dialog.contents.addLabeledControl.call_args.kwargs["style"],
			self.wx.TE_READONLY | self.wx.TE_MULTILINE,
		)
		dialog.escape.assert_called_once_with(self.return_codes.NO)
		self.assertEqual(self.timers[-1].delay, 24 * 60 * 60 * 1000)

	def test_declining_update_never_downloads(self):
		self.check.return_value = self.updater.Update("1.2.3", "http://127.0.0.1/update")
		self.complete_check()
		self.assertIn("No release notes", self.dialogs[-1].contents.addLabeledControl.call_args.kwargs["value"])
		self.install.assert_not_called()
		self.assertEqual(self.timers[-1].delay, 24 * 60 * 60 * 1000)

	def test_closing_update_prompt_does_not_install(self):
		self.check.return_value = self.updater.Update("1.2.3", "http://127.0.0.1/update")
		self.dialog_answer = "close"
		self.complete_check()
		self.install.assert_not_called()

	def test_failed_automatic_check_is_logged_and_retried_daily(self):
		self.check.side_effect = OSError("offline")
		self.complete_check()
		self.messages.assert_not_called()
		self.updater.log.error.assert_called_once()
		self.install.assert_not_called()
		self.assertEqual(self.timers[-1].delay, 24 * 60 * 60 * 1000)

	def test_no_update_is_quiet_and_the_next_check_runs(self):
		self.check.return_value = None
		self.complete_check()
		self.messages.assert_not_called()
		self.timers[-1].fire()
		self.run_queued()
		self.assertEqual(self.check.call_count, 2)

	def test_secure_screen_defers_the_result_until_it_is_safe(self):
		self.check.return_value = self.updater.Update("1.2.3", "http://127.0.0.1/update")
		self.secure.return_value = True
		self.complete_check()
		self.assertEqual(self.dialogs, [])
		self.secure.return_value = False
		self.timers[-1].fire()
		self.assertEqual(len(self.dialogs), 1)

	def test_addon_prompt_waits_for_an_active_resource_dialog(self):
		self.check.return_value = self.updater.Update("1.2.3", "http://127.0.0.1/update")
		with self.updater.interaction_lock:
			self.complete_check()
		self.assertEqual(self.dialogs, [])
		self.assertEqual(self.timers[-1].delay, 60_000)
		self.timers[-1].fire()
		self.assertEqual(len(self.dialogs), 1)

	def test_termination_suppresses_late_results_and_stops_timer(self):
		self.run_queued()
		self.service.terminate()
		self.run_queued()
		self.messages.assert_not_called()
		self.assertEqual(self.timers, [])

	def test_termination_stops_the_scheduled_check(self):
		self.check.return_value = None
		self.complete_check()
		timer = self.timers[-1]
		self.service.terminate()
		timer.Stop.assert_called_once()

	def test_local_release_downloads_only_after_consent(self):
		requests = []
		payload = b"fake NVDA add-on for the integration test"
		notes = "## Improvements\n\n- Clearer speech."
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		changelog = Path(directory.name) / "changelog.md"
		manifest = Path(directory.name) / "shravya-update.toml"
		changelog.write_text(notes, encoding="utf-8")
		subprocess.run(
			[
				sys.executable, "-B",
				str(SOURCE.parents[2] / "tools" / "build_update_manifest.py"),
				"1.2.3", str(changelog), str(manifest),
				"--resource-catalog", "http://127.0.0.1/catalog",
			],
			check=True,
		)
		manifest_bytes = manifest.read_bytes()

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, format, *args):
				pass

			def do_GET(self):
				requests.append(self.path)
				if self.path == "/latest":
					body = json.dumps({
						"tag_name": "v1.2.3", "draft": False, "prerelease": False,
						"assets": [
							{
								"name": "shravya-1.2.3.nvda-addon",
								"browser_download_url": f"http://127.0.0.1:{self.server.server_port}/bundle",
							},
							{
								"name": "shravya-update.toml",
								"browser_download_url": f"http://127.0.0.1:{self.server.server_port}/notes",
							},
						],
					}).encode()
				elif self.path == "/notes":
					body = manifest_bytes
				else:
					body = payload
				self.send_response(200)
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)

		server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		thread = threading.Thread(target=server.serve_forever)
		thread.start()
		self.addCleanup(thread.join)
		self.addCleanup(server.server_close)
		self.addCleanup(server.shutdown)
		self.service._source = f"http://127.0.0.1:{server.server_port}/latest"
		self.updater.check_release = importlib.import_module("update_ui_test._update").check_release
		self.complete_check()
		self.assertEqual(requests, ["/latest", "/notes"])
		self.assertEqual(self.dialogs[-1].contents.addLabeledControl.call_args.kwargs["value"], notes)

		gui = sys.modules["gui"]
		gui.__path__ = []
		install = Mock(side_effect=lambda frame, path: self.assertEqual(Path(path).read_bytes(), payload) or True)
		addon_gui = module("gui.addonGui", installAddon=install, promptUserForRestart=Mock())
		gui.addonGui = addon_gui
		handler = sys.modules["addonHandler"]
		handler.getAvailableAddons = lambda: iter(())
		handler.AddonBundle = Mock(return_value=types.SimpleNamespace(
			manifest={"name": "shravya", "version": "1.2.3"}
		))
		dialog = Mock()
		dialog.Update.return_value = (True, False)
		self.wx.PD_CAN_ABORT = 32
		self.wx.PD_ELAPSED_TIME = 64
		self.wx.ProgressDialog = Mock(return_value=dialog)
		exec_module = module(
			"systemUtils", ExecAndPump=lambda func, *args: types.SimpleNamespace(funcRes=func(*args))
		)
		sys.modules.pop("update_ui_test._update_install")
		with patch.dict(sys.modules, {
			"gui.addonGui": addon_gui,
			"systemUtils": exec_module,
			"NVDAState": module("NVDAState", WritePaths=module("WritePaths", configDir=directory.name)),
		}):
			installer = importlib.import_module("update_ui_test._update_install")
			installer.plan_addon_resources = Mock(return_value=types.SimpleNamespace(missing=()))
			installer.prepare_addon_resources = Mock(return_value=True)
			self.updater.install_update = installer.install_update
			self.messages.return_value = self.wx.YES
			self.dialog_answer = self.return_codes.YES
			self.timers[-1].fire()
			self.run_queued()
		self.assertEqual(requests, ["/latest", "/notes", "/latest", "/notes", "/bundle"])
		install.assert_called_once()
		addon_gui.promptUserForRestart.assert_called_once()


if __name__ == "__main__":
	unittest.main()
