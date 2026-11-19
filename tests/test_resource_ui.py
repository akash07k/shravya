import http.server
import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from test_resource_install import FILES
from test_resource_setup import QuietHandler

SOURCE = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def module(name, **values):
	result = types.ModuleType(name)
	result.__dict__.update(values)
	return result


class TestResourceUI(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.messages = Mock()
		self.menu = Mock()
		self.tray = Mock(toolsMenu=self.menu)
		self.gui = module("gui", mainFrame=Mock(sysTrayIcon=self.tray), messageBox=self.messages)
		self.dialog = Mock()
		self.dialog.Update.return_value = (True, False)
		self.choice = Mock()
		self.choice.ShowModal.return_value = 10
		self.choice.GetSelection.return_value = 0
		self.file_dialog = Mock()
		self.file_dialog.ShowModal.return_value = 11
		self.scheduled = []

		def call_after(callback, *args):
			if args:
				callback(*args)
			else:
				self.scheduled.append(callback)

		self.wx = module(
			"wx",
			ID_ANY=-1,
			ID_OK=10,
			EVT_MENU=object(),
			YES=1,
			NO=2,
			OK=4,
			ICON_INFORMATION=8,
			ICON_ERROR=16,
			PD_CAN_ABORT=32,
			PD_ELAPSED_TIME=64,
			FD_OPEN=128,
			FD_FILE_MUST_EXIST=256,
			ProgressDialog=Mock(return_value=self.dialog),
			SingleChoiceDialog=Mock(return_value=self.choice),
			FileDialog=Mock(return_value=self.file_dialog),
			CallAfter=call_after,
			CallLater=Mock(),
		)
		package = module("resource_ui_test")
		package.__path__ = [str(SOURCE)]
		modules = {
			"resource_ui_test": package,
			"addonHandler": module(
				"addonHandler",
				initTranslation=lambda: None,
				AddonError=type("AddonError", (Exception,), {}),
				getCodeAddon=lambda: types.SimpleNamespace(manifest={"version": "1.0.0"}),
			),
			"NVDAState": module("NVDAState", WritePaths=module("WritePaths", configDir=str(self.root))),
			"core": module("core", callLater=Mock()),
			"gui": self.gui,
			"ui": module("ui", isRunningOnSecureDesktop=lambda: False),
			"wx": self.wx,
			"logHandler": module("logHandler", log=Mock()),
			"systemUtils": module(
				"systemUtils", ExecAndPump=lambda func, *args: types.SimpleNamespace(funcRes=func(*args))
			),
		}
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		translation = patch("builtins._", lambda value: value, create=True)
		translation.start()
		self.addCleanup(translation.stop)
		for name in list(sys.modules):
			if name.startswith("resource_ui_test."):
				del sys.modules[name]
		self.ui = importlib.import_module("resource_ui_test._resource_ui")
		self.messages.return_value = self.wx.NO
		self.setup = self.ui.ResourceSetup("http://127.0.0.1/catalog")
		self.addCleanup(self.setup.terminate)

	def test_first_prompt_is_scheduled_outside_nvda_core_queue(self):
		self.assertEqual(self.scheduled, [self.setup._prompt_if_needed, self.setup._check_model])
		self.scheduled.pop(0)()
		self.messages.assert_called_once()

	def test_model_checks_run_independently_on_startup_and_daily(self):
		self.ui.Thread = lambda **kwargs: types.SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"]))
		self.ui.check_resource_update = Mock(return_value=None)
		self.scheduled.clear()
		other = self.ui.ResourceSetup("http://127.0.0.1/catalog")
		self.addCleanup(other.terminate)
		other._resources_ready = Mock(return_value=True)
		self.assertIn(other._check_model, self.scheduled)
		other._check_model()
		self.ui.check_resource_update.assert_called_once()
		self.messages.assert_not_called()
		self.assertEqual(self.wx.CallLater.call_args.args[0], 24 * 60 * 60 * 1000)

	def test_model_update_asks_before_preparing_and_can_be_declined(self):
		self.setup._catalog_url = "http://127.0.0.1/catalog"
		self.ui.Thread = lambda **kwargs: types.SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"]))
		self.ui.check_resource_update = Mock(return_value=object())
		self.ui.prepare_resource_update = Mock()
		self.setup._resources_ready = Mock(return_value=True)
		self.setup._check_model()
		self.assertIn("resource", self.messages.call_args.args[0].lower())
		self.ui.prepare_resource_update.assert_not_called()

	def test_accepted_model_update_is_prepared_and_reports_when_it_takes_effect(self):
		update = self.ui.ResourceUpdate(object(), ("runtime", "old"))
		self.ui.prepare_resource_update = Mock(return_value=(True, False))
		self.messages.return_value = self.wx.YES
		self.setup._busy = True
		self.setup._finish_model(update, None)
		self.ui.prepare_resource_update.assert_called_once()
		self.assertIn("next model reload", self.messages.call_args.args[0].lower())
		self.dialog.Destroy.assert_called_once_with()
		self.assertFalse(self.setup._busy)

	def test_model_prompt_waits_while_another_shravya_dialog_is_open(self):
		update = self.ui.ResourceUpdate(object(), ("runtime", "old"))
		self.setup._busy = True
		with self.ui.interaction_lock:
			self.setup._finish_model(update, None)
		self.messages.assert_not_called()
		self.assertEqual(self.wx.CallLater.call_args.args[0], 60_000)
		self.setup._finish_model(update, None)
		self.messages.assert_called_once()

	def test_late_model_results_are_suppressed_after_termination(self):
		self.setup.terminate()
		self.setup._finish_model(None, OSError("offline"))
		self.messages.assert_not_called()
		self.wx.CallLater.assert_not_called()

	def test_termination_removes_the_tools_menu_item(self):
		item = self.setup._item
		self.setup.terminate()
		self.setup._menu.DestroyItem.assert_called_once_with(item)

	def test_tools_menu_has_a_resource_setup_mnemonic(self):
		self.assertEqual(self.menu.Append.call_args.args[1], "Set up Shravya &resources...")

	def test_settings_setup_parents_dialogs_to_settings_panel(self):
		parent = object()
		self.choice.GetSelection.return_value = 1
		self.file_dialog.ShowModal.return_value = self.wx.ID_OK
		self.file_dialog.GetPath.side_effect = ["runtime.zip", "model.zip"]
		self.ui.prepare_local = Mock(return_value=False)
		self.setup.open(parent)
		self.assertIs(self.wx.SingleChoiceDialog.call_args.args[0], parent)
		self.assertTrue(all(call.args[0] is parent for call in self.wx.FileDialog.call_args_list))
		self.assertIs(self.wx.ProgressDialog.call_args.kwargs["parent"], parent)
		self.assertIs(self.messages.call_args.kwargs["parent"], parent)
		self.gui.mainFrame.prePopup.assert_not_called()
		self.gui.mainFrame.postPopup.assert_not_called()

	def test_settings_download_parents_confirmation_and_progress(self):
		parent = object()
		self.messages.return_value = self.wx.YES
		self.ui.prepare_split = Mock(return_value=True)
		self.setup.open(parent)
		self.assertIs(self.wx.SingleChoiceDialog.call_args.args[0], parent)
		self.assertIs(self.wx.ProgressDialog.call_args.kwargs["parent"], parent)
		self.assertTrue(all(call.kwargs["parent"] is parent for call in self.messages.call_args_list))
		self.gui.mainFrame.prePopup.assert_not_called()

	def test_model_check_failure_is_reported_and_rescheduled(self):
		self.setup._catalog_url = "http://127.0.0.1/catalog"
		self.ui.Thread = lambda **kwargs: types.SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"]))
		self.ui.check_resource_update = Mock(side_effect=OSError("offline"))
		self.setup._resources_ready = Mock(return_value=True)
		self.setup._check_model()
		self.messages.assert_not_called()
		self.assertEqual(self.wx.CallLater.call_args.args[0], 24 * 60 * 60 * 1000)

	def test_scheduled_prompt_does_not_run_after_termination(self):
		self.assertEqual(self.scheduled, [self.setup._prompt_if_needed, self.setup._check_model])
		self.setup.terminate()
		self.scheduled.pop(0)()
		self.messages.assert_not_called()

	def test_deferred_prompt_is_not_repeated(self):
		self.setup._prompt_if_needed()
		self.setup._prompt_if_needed()
		self.messages.assert_called_once()
		self.assertTrue((self.root / "shravya" / ".bluev3-v1-prompted").is_file())

	def test_unavailable_source_does_not_prompt(self):
		self.setup._catalog_url = None
		self.setup._prompt_if_needed()
		self.messages.assert_not_called()
		self.assertFalse((self.root / "shravya").exists())
		self.setup._on_menu(None)
		self.wx.FileDialog.assert_called_once()
		self.messages.assert_not_called()

	def test_failed_restart_promotion_is_reported_without_catalog(self):
		self.setup._catalog_url = None
		with patch.object(self.ui, "take_pending_failure", return_value=True):
			self.setup._prompt_if_needed()
		self.assertIn("previous resources remain selected", self.messages.call_args.args[0].lower())

	def test_local_setup_is_available_without_a_catalog_and_cancellation_does_not_install(self):
		self.setup._catalog_url = None
		self.file_dialog.ShowModal.side_effect = [self.wx.ID_OK, 11]
		self.file_dialog.GetPath.return_value = str(self.root / "runtime.zip")
		self.ui.prepare_local = Mock()
		self.setup._on_menu(None)
		self.ui.prepare_local.assert_not_called()
		self.assertEqual(self.file_dialog.Destroy.call_count, 2)
		self.assertFalse(self.setup._busy)

	def test_local_setup_can_replace_existing_resources(self):
		self.setup._resources_ready = Mock(return_value=True)
		self.choice.GetSelection.return_value = 1
		self.file_dialog.ShowModal.return_value = self.wx.ID_OK
		self.file_dialog.GetPath.side_effect = ["runtime.zip", "model.zip"]
		self.ui.prepare_local = Mock(return_value=True)
		self.setup._on_menu(None)
		self.ui.prepare_local.assert_called_once()
		self.assertIn("restart", self.messages.call_args.args[0].lower())

	def test_old_manual_resources_do_not_prevent_first_run_prompt(self):
		legacy = self.root / "addons" / "shravya" / "bluetts-resources"
		(legacy / "python").mkdir(parents=True)
		(legacy / "bluev3_onnx" / "onnx").mkdir(parents=True)
		voice = legacy / "bluev3_onnx" / "voices"
		voice.mkdir()
		(voice / "noa.json").write_text("{}", encoding="utf-8")
		self.setup._prompt_if_needed()
		self.messages.assert_called_once()

	def test_existing_split_resources_do_not_prompt_or_offer_reinstallation(self):
		packs = self.root / "shravya" / "packs"
		(packs / "runtime" / "runtime-1" / "python").mkdir(parents=True)
		model = packs / "model" / "model-1" / "bluev3_onnx"
		(model / "onnx").mkdir(parents=True)
		(model / "voices").mkdir()
		(model / "voices" / "noa.json").write_text("{}", encoding="utf-8")
		(self.root / "shravya" / "active-resources.json").write_text(
			json.dumps({"interface": "bluev3-v1", "runtime": "runtime-1", "model": "model-1"}),
			encoding="utf-8",
		)
		self.setup._prompt_if_needed()
		self.messages.assert_not_called()
		self.setup._on_menu(None)
		self.assertIn("already installed", self.messages.call_args.args[0].lower())

	def test_invalid_resource_selection_is_reported_instead_of_prompting_for_installation(self):
		selection = self.root / "shravya" / "active-resources.json"
		selection.parent.mkdir()
		selection.write_text("{", encoding="utf-8")
		self.setup._prompt_if_needed()
		self.assertIn("could not", self.messages.call_args.args[0].lower())
		self.assertFalse((selection.parent / ".bluev3-v1-prompted").exists())

	def test_menu_download_reports_progress_and_success(self):
		def prepare(url, config, version, progress):
			progress("download", 1, 2)
			progress("extract", 2, 2)
			return True

		self.ui.prepare_split = prepare
		self.messages.return_value = self.wx.YES
		self.setup._on_menu(None)
		self.assertEqual(self.dialog.Update.call_count, 2)
		self.assertIn("ready", self.messages.call_args.args[0].lower())
		self.dialog.Destroy.assert_called_once_with()
		self.assertFalse(self.setup._busy)

	def test_completion_does_not_wait_for_progress_dialog_close(self):
		def prepare(url, config, version, progress):
			progress("download", 2, 2)
			progress("extract", 2, 2)
			return True

		def message(text, title, style):
			if "ready" in text.lower():
				self.dialog.Destroy.assert_called_once_with()
			return self.wx.YES

		self.ui.prepare_split = prepare
		self.messages.side_effect = message
		self.setup._on_menu(None)
		self.assertTrue(all(call.args[0] < 100 for call in self.dialog.Update.call_args_list))

	def test_cancelled_progress_reports_cancellation(self):
		def prepare(url, config, version, progress):
			progress("download", 1, 2)
			progress("download", 2, 2)
			return True

		self.ui.prepare_split = prepare
		self.dialog.Update.return_value = (False, False)
		self.messages.return_value = self.wx.YES
		self.setup._on_menu(None)
		self.assertIn("cancelled", self.messages.call_args.args[0].lower())
		self.dialog.Destroy.assert_called_once_with()
		self.assertFalse(self.setup._busy)

	def test_failure_is_reported_and_can_be_retried(self):
		self.ui.prepare_split = Mock(side_effect=OSError("download failed"))
		self.messages.return_value = self.wx.YES
		self.setup._on_menu(None)
		self.assertIn("failed", self.messages.call_args.args[0].lower())
		self.assertFalse(self.setup._busy)
		self.ui.prepare_split.side_effect = None
		self.ui.prepare_split.return_value = True
		self.setup._on_menu(None)
		self.assertEqual(self.ui.prepare_split.call_count, 2)
		self.assertIn("ready", self.messages.call_args.args[0].lower())

	def test_dialog_creation_failure_releases_busy_state(self):
		self.wx.ProgressDialog.side_effect = RuntimeError("dialog unavailable")
		self.messages.return_value = self.wx.YES
		with self.assertRaises(RuntimeError):
			self.setup._on_menu(None)
		self.assertFalse(self.setup._busy)
		self.assertEqual(self.gui.mainFrame.postPopup.call_count, 2)

	def test_menu_installs_separate_packs_from_catalog(self):
		published = self.root / "served"
		published.mkdir()
		for kind in ("runtime", "model"):
			prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
			files = [name for name in FILES if name.startswith(prefix)]
			with zipfile.ZipFile(published / f"{kind}.zip", "w") as archive:
				archive.writestr("pack-kind.txt", kind)
				archive.writestr("pack-version.txt", f"{kind}-1")
				archive.writestr("pack-files.json", json.dumps({name: 7 for name in files}))
				for name in files:
					archive.writestr(name, b"example")
		handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(published), **kwargs)
		server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
		thread = threading.Thread(target=server.serve_forever)
		thread.start()
		try:
			base = f"http://127.0.0.1:{server.server_port}"
			(published / "resources.toml").write_text(
				'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
				f'[runtime]\nid = "runtime-1"\nurl = "{base}/runtime.zip"\n'
				f'[model]\nid = "model-1"\nurl = "{base}/model.zip"\n',
				encoding="utf-8",
			)
			self.setup._catalog_url = f"{base}/resources.toml"
			self.messages.return_value = self.wx.YES
			self.setup._on_menu(None)
			packs = self.root / "shravya" / "packs"
			self.assertTrue((packs / "runtime" / "runtime-1" / "python").is_dir())
			self.assertTrue((packs / "model" / "model-1" / "bluev3_onnx").is_dir())
			self.assertTrue((self.root / "shravya" / "active-resources.json").is_file())
			self.assertIn("ready", self.messages.call_args.args[0].lower())
		finally:
			server.shutdown()
			server.server_close()
			thread.join()


if __name__ == "__main__":
	unittest.main()
