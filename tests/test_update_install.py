import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from functools import partial
from http.client import IncompleteRead
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from test_resource_download import Handler as RangeHandler
from test_resource_install import FILES

SOURCE = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def module(name, **values):
	result = types.ModuleType(name)
	result.__dict__.update(values)
	return result


class TestUpdateInstall(unittest.TestCase):
	def setUp(self):
		self.config = tempfile.TemporaryDirectory()
		self.addCleanup(self.config.cleanup)
		self.dialog = Mock()
		self.dialog.Update.return_value = (True, False)
		self.messages = Mock()
		self.frame = Mock()
		self.install_addon = Mock(return_value=True)
		self.restart = Mock()
		addon_gui = module("gui.addonGui", installAddon=self.install_addon, promptUserForRestart=self.restart)
		gui = module("gui", mainFrame=self.frame, messageBox=self.messages, addonGui=addon_gui)
		gui.__path__ = []
		self.bundle = Mock(manifest={"name": "shravya", "version": "1.2.3"})
		self.addons = []
		addon_handler = module(
			"addonHandler",
			initTranslation=lambda: None,
			AddonError=type("AddonError", (Exception,), {}),
			AddonBundle=Mock(return_value=self.bundle),
			getAvailableAddons=lambda: iter(self.addons),
		)
		self.wx = module(
			"wx",
			OK=1,
			ICON_ERROR=2,
			PD_CAN_ABORT=4,
			PD_ELAPSED_TIME=8,
			YES=16,
			NO=32,
			ICON_INFORMATION=64,
			ProgressDialog=Mock(return_value=self.dialog),
			CallAfter=lambda callback, *args: callback(*args),
		)
		package = module("update_install_test")
		package.__path__ = [str(SOURCE)]
		modules = {
			"update_install_test": package,
			"addonHandler": addon_handler,
			"gui": gui,
			"gui.addonGui": addon_gui,
			"NVDAState": module("NVDAState", WritePaths=module("WritePaths", configDir=self.config.name)),
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
			if name.startswith("update_install_test."):
				del sys.modules[name]
		self.ui = importlib.import_module("update_install_test._update_install")
		self.update = self.ui.Update("1.2.3", "http://127.0.0.1/update", None, "http://127.0.0.1/catalog")
		self.plan = types.SimpleNamespace(missing=())
		self.ui.plan_addon_resources = Mock(return_value=self.plan)
		self.ui.prepare_addon_resources = Mock(return_value=True)
		self.ui.discard_for_addon = Mock()
		self.stop = threading.Event()
		self.paths = []

		def download(url, path, progress):
			self.paths.append(path)
			path.write_bytes(b"bundle")
			progress(10, 10)

		self.ui.download = Mock(side_effect=download)

	def test_download_installs_matching_bundle_and_offers_restart(self):
		def install(frame, path):
			self.dialog.Destroy.assert_called_once_with()
			self.assertTrue(Path(path).exists())
			return True

		self.install_addon.side_effect = install
		self.ui.install_update(self.update, self.stop)
		self.install_addon.assert_called_once()
		self.restart.assert_called_once()
		self.assertFalse(self.paths[0].exists())
		self.assertLess(self.dialog.Update.call_args.args[0], 100)

	def test_missing_resources_are_confirmed_and_prepared_before_addon_download(self):
		self.plan.missing = ("model",)
		self.messages.return_value = self.wx.YES
		order = []
		self.ui.prepare_addon_resources.side_effect = lambda *args: order.append("resources") or True
		download = self.ui.download.side_effect
		self.ui.download.side_effect = lambda *args: (order.append("addon"), download(*args))[1]
		self.ui.install_update(self.update, self.stop)
		self.assertEqual(order, ["resources", "addon"])
		self.assertIn("model", self.messages.call_args.args[0].lower())
		self.install_addon.assert_called_once()
		self.ui.discard_for_addon.assert_not_called()

	def test_declining_required_resources_preserves_the_installed_addon(self):
		self.plan.missing = ("runtime", "model")
		self.messages.return_value = self.wx.NO
		self.ui.install_update(self.update, self.stop)
		self.ui.prepare_addon_resources.assert_not_called()
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()

	def test_resource_preparation_failure_never_downloads_the_addon(self):
		self.ui.prepare_addon_resources.side_effect = OSError("disk failure")
		self.ui.install_update(self.update, self.stop)
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.assertIn("failed", self.messages.call_args.args[0].lower())
		self.ui.discard_for_addon.assert_not_called()

	def test_cancellation_before_staging_does_not_discard_an_existing_selection(self):
		def prepare(plan, config, progress):
			progress("download", 1, 2)
			progress("download", 2, 2)
			return True

		self.dialog.Update.return_value = (False, False)
		self.ui.prepare_addon_resources.side_effect = prepare
		self.ui.install_update(self.update, self.stop)
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.ui.discard_for_addon.assert_not_called()
		self.assertIn("cancelled", self.messages.call_args.args[0].lower())

	def test_invalid_resource_catalog_blocks_addon_download(self):
		self.ui.plan_addon_resources.side_effect = ValueError("incompatible catalog")
		self.ui.install_update(self.update, self.stop)
		self.ui.prepare_addon_resources.assert_not_called()
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.assertIn("failed", self.messages.call_args.args[0].lower())

	def test_missing_resource_catalog_blocks_addon_download(self):
		update = self.ui.Update("1.2.3", "http://127.0.0.1/update")
		self.ui.install_update(update, self.stop)
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.assertIn("failed", self.messages.call_args.args[0].lower())

	def test_wrong_bundle_identity_is_reported_without_installing(self):
		for key, value in (("name", "other"), ("version", "1.2.4")):
			with self.subTest(key=key):
				self.bundle.manifest[key] = value
				self.ui.install_update(self.update, self.stop)
				self.install_addon.assert_not_called()
				self.restart.assert_not_called()
				self.assertIn("failed", self.messages.call_args.args[0].lower())
				self.bundle.manifest[key] = "shravya" if key == "name" else "1.2.3"

	def test_failed_download_is_reported_without_installing(self):
		for error in (OSError("network failure"), IncompleteRead(b"partial", 1)):
			with self.subTest(error=error):
				self.ui.download.side_effect = error
				self.ui.install_update(self.update, self.stop)
				self.install_addon.assert_not_called()
				self.restart.assert_not_called()
				self.assertIn("failed", self.messages.call_args.args[0].lower())
				self.ui.discard_for_addon.assert_called()

	def test_interrupted_addon_download_resumes_on_retry(self):
		server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
		server.payload = b"valid addon bundle content"
		server.etag = '"release-1"'
		server.interrupt = True
		server.invalid_resume = False
		server.reject_range = False
		server.requests = []
		thread = threading.Thread(target=server.serve_forever)
		thread.start()
		try:
			url = f"http://127.0.0.1:{server.server_port}/update"
			update = self.ui.Update("1.2.3", url, None, "http://127.0.0.1/catalog")
			self.ui.download = importlib.import_module("update_install_test._download").download
			self.ui.install_update(update, self.stop)
			self.install_addon.assert_not_called()
			self.assertIn("failed", self.messages.call_args.args[0].lower())
			cache = Path(self.config.name) / "shravya" / "downloads" / "addon"
			self.assertEqual((cache / "1.2.3.nvda-addon.part").read_bytes(), server.payload[:6])
			self.assertTrue((cache / "1.2.3.nvda-addon.json").is_file())
			self.ui.install_update(update, self.stop)
			self.assertEqual(server.requests, [(None, None), ("bytes=6-", '"release-1"')])
			self.install_addon.assert_called_once()
			self.assertEqual(list(cache.iterdir()), [])
		finally:
			server.shutdown()
			server.server_close()
			thread.join()

	def test_interrupted_addon_download_restarts_for_changed_source_or_version(self):
		server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
		server.invalid_resume = False
		server.reject_range = False
		server.requests = []
		thread = threading.Thread(target=server.serve_forever)
		thread.start()
		try:
			base = f"http://127.0.0.1:{server.server_port}"
			self.ui.download = importlib.import_module("update_install_test._download").download
			installed = []
			self.install_addon.side_effect = lambda frame, path: installed.append(Path(path).read_bytes()) or True
			for index, change in enumerate(("url", "validator", "version"), start=4):
				with self.subTest(change=change):
					version = f"1.2.{index * 2}"
					server.payload = b"old addon contents"
					server.etag = '"old"'
					server.interrupt = True
					server.requests.clear()
					update = self.ui.Update(version, f"{base}/old", None, f"{base}/catalog")
					self.ui.install_update(update, self.stop)
					server.payload = b"new addon contents"
					if change == "url":
						update = update._replace(url=f"{base}/new")
					elif change == "validator":
						server.etag = '"new"'
					else:
						update = update._replace(version=f"1.2.{index * 2 + 1}")
					self.bundle.manifest["version"] = update.version
					self.ui.install_update(update, self.stop)
					expected_range = ("bytes=6-", '"old"') if change == "validator" else (None, None)
					self.assertEqual(server.requests, [(None, None), expected_range])
					self.assertEqual(installed[-1], server.payload)
		finally:
			server.shutdown()
			server.server_close()
			thread.join()

	def test_invalid_version_cannot_escape_the_download_directory(self):
		update = self.update._replace(version="..\\outside")
		self.ui.install_update(update, self.stop)
		self.ui.plan_addon_resources.assert_not_called()
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.assertIn("failed", self.messages.call_args.args[0].lower())

	def test_cancelled_download_does_not_install(self):
		self.dialog.Update.return_value = (False, False)
		self.ui.install_update(self.update, self.stop)
		self.install_addon.assert_not_called()
		self.restart.assert_not_called()
		self.assertIn("cancelled", self.messages.call_args.args[0].lower())

	def test_pending_update_blocks_replacement(self):
		self.addons.append(types.SimpleNamespace(name="shravya", isPendingInstall=True, isPendingRemove=False))
		self.ui.install_update(self.update, self.stop)
		self.ui.download.assert_not_called()
		self.install_addon.assert_not_called()
		self.assertIn("restart", self.messages.call_args.args[0].lower())

	def test_nvda_declining_install_does_not_prompt_restart(self):
		self.install_addon.return_value = False
		self.ui.install_update(self.update, self.stop)
		self.restart.assert_not_called()
		self.ui.discard_for_addon.assert_called_once()

	def test_shutdown_cancels_without_installing_or_showing_a_dialog(self):
		def download(url, path, progress):
			self.stop.set()
			progress(1, 2)

		self.ui.download.side_effect = download
		self.ui.install_update(self.update, self.stop)
		self.install_addon.assert_not_called()
		self.messages.assert_not_called()

	def test_local_update_prepares_resources_and_preserves_selection_until_matching_version(self):
		requests = []
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			served = root / "served"
			served.mkdir()
			config = root / "config"

			class Handler(SimpleHTTPRequestHandler):
				def log_message(self, format, *args):
					pass

				def do_GET(self):
					requests.append(self.path)
					super().do_GET()

			server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(served)))
			thread = threading.Thread(target=server.serve_forever)
			thread.start()
			try:
				base = f"http://127.0.0.1:{server.server_port}"

				def pack(kind, version):
					path = served / f"{version}.zip"
					prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
					files = [name for name in FILES if name.startswith(prefix)]
					with zipfile.ZipFile(path, "w") as archive:
						archive.writestr("pack-kind.txt", kind)
						archive.writestr("pack-version.txt", version)
						archive.writestr("pack-files.json", json.dumps({name: 7 for name in files}))
						for name in files:
							archive.writestr(name, b"example")

				def catalog(version, model):
					(served / "catalog.toml").write_text(
						f'format = 1\ninterface = "bluev3-v1"\naddons = ["{version}"]\n'
						f'[runtime]\nid = "runtime-1"\nurl = "{base}/runtime-1.zip"\n'
						f'[model]\nid = "{model}"\nurl = "{base}/{model}.zip"\n',
						encoding="utf-8",
					)

				for kind, version in (("runtime", "runtime-1"), ("model", "model-1"), ("model", "model-2")):
					pack(kind, version)
				(served / "update.nvda-addon").write_bytes(b"bundle")
				setup = importlib.import_module("update_install_test._setup")
				resources = importlib.import_module("update_install_test._resources")
				catalog("1.0.0", "model-1")
				setup.prepare_split(f"{base}/catalog.toml", config, "1.0.0", lambda *args: None)
				before = resources.resource_selection(config)
				catalog("1.2.3", "model-2")
				requests.clear()
				sys.modules["NVDAState"].WritePaths.configDir = str(config)
				self.ui.plan_addon_resources = setup.plan_addon_resources
				self.ui.prepare_addon_resources = setup.prepare_addon_resources
				self.ui.discard_for_addon = resources.discard_for_addon
				self.ui.download = importlib.import_module("update_install_test._download").download
				update = self.ui.Update("1.2.3", f"{base}/update.nvda-addon", None, f"{base}/catalog.toml")

				self.messages.return_value = self.wx.NO
				self.ui.install_update(update, self.stop)
				self.assertEqual(requests, ["/catalog.toml"])
				self.assertEqual(resources.resource_selection(config), before)
				self.install_addon.assert_not_called()

				self.messages.return_value = self.wx.YES
				model = served / "model-2.zip"
				unavailable = served / "model-2.unavailable"
				model.rename(unavailable)
				requests.clear()
				self.ui.install_update(update, self.stop)
				self.assertEqual(requests, ["/catalog.toml", "/model-2.zip"])
				self.assertIn("failed", self.messages.call_args.args[0].lower())
				self.assertEqual(resources.resource_selection(config), before)
				self.assertFalse((config / "shravya" / "prepared-1.2.3.json").exists())
				self.install_addon.assert_not_called()
				unavailable.rename(model)

				def decline_install(frame, path):
					self.assertEqual(resources.resource_selection(config), before)
					self.assertTrue((config / "shravya" / "prepared-1.2.3.json").is_file())
					return False

				self.install_addon.side_effect = decline_install
				requests.clear()
				self.ui.install_update(update, self.stop)
				self.assertEqual(requests, ["/catalog.toml", "/model-2.zip", "/update.nvda-addon"])
				self.assertFalse((config / "shravya" / "prepared-1.2.3.json").exists())
				self.assertEqual(resources.resource_selection(config), before)

				self.install_addon.side_effect = None
				self.install_addon.return_value = True
				self.messages.reset_mock()
				requests.clear()
				self.ui.install_update(update, self.stop)
				self.assertEqual(requests, ["/catalog.toml", "/update.nvda-addon"])
				self.messages.assert_not_called()
				self.assertEqual(resources.resource_selection(config), before)
				resources.resource_directories(config, addon_version="1.0.0")
				self.assertEqual(resources.resource_selection(config), before)
				resources.resource_directories(config, addon_version="1.2.3")
				self.assertEqual(resources.resource_selection(config), before)
				with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
					resources.resource_directories(config, addon_version="1.2.3")
				self.assertEqual(resources.resource_selection(config), ("runtime-1", "model-2"))
			finally:
				server.shutdown()
				server.server_close()
				thread.join()


if __name__ == "__main__":
	unittest.main()
