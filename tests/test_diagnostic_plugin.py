import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "addon" / "globalPlugins" / "shravya.py"
REAL_THREAD = threading.Thread


def module(name, **values):
	result = ModuleType(name)
	result.__dict__.update(values)
	return result


class Hook:
	def __init__(self):
		self.handlers = []

	def register(self, handler):
		self.handlers.append(handler)

	def unregister(self, handler):
		self.handlers.remove(handler)
		return True

	def notify(self, **kwargs):
		for handler in self.handlers[:]:
			handler(**kwargs)


class BasePlugin:
	def terminate(self):
		pass


class Synth:
	def __init__(self):
		self.set_diagnostic = Mock()
		self.apply_settings = Mock()


class KeyboardGesture:
	pass


class ImmediateThread:
	def __init__(self, *, target, args=(), **kwargs):
		self.target = target
		self.args = args

	def start(self):
		self.target(*self.args)


class TestDiagnosticPlugin(unittest.TestCase):
	def setUp(self):
		self.synth = Synth()
		self.current = Mock(return_value=self.synth)
		self.capture = Mock()
		self.capture.stop.return_value = []
		self.capture_factory = Mock(return_value=self.capture)
		self.observer = Mock()
		self.input_factory = Mock(return_value=self.observer)
		self.resource_setup = Mock()
		self.resource_factory = Mock(return_value=self.resource_setup)
		self.updater = Mock()
		self.updater_factory = Mock(return_value=self.updater)
		self.delivery = SimpleNamespace(
			catalog_url="https://example.test/catalog.toml",
			release_api="https://example.test/latest",
		)
		self.load_delivery = Mock(return_value=self.delivery)
		self.ui = module(
			"ui", message=Mock(), browseableMessage=Mock(), isRunningOnSecureDesktop=Mock(return_value=False),
		)
		self.speech = module("speech", speakMessage=Mock())
		self.secure_desktop = Hook()
		self.conf = {"audio": {"outputDevice": "default"}}
		gui = module("gui")
		gui.__path__ = []
		gui.guiHelper = module("gui.guiHelper")
		gui.settingsDialogs = module(
			"gui.settingsDialogs",
			SettingsPanel=type("SettingsPanel", (), {}),
			NVDASettingsDialog=SimpleNamespace(categoryClasses=[]),
		)
		driver = module("synthDrivers.shravya", SynthDriver=Synth, _settings=Mock())
		driver.__path__ = []
		modules = {
			"addonHandler": module("addonHandler", initTranslation=lambda: None),
			"config": module("config", conf=self.conf, post_configProfileSwitch=Hook()),
			"core": module("core", callLater=lambda delay, function, *args: function(*args)),
			"globalPluginHandler": module("globalPluginHandler", GlobalPlugin=BasePlugin),
			"globalVars": module("globalVars", appArgs=SimpleNamespace(secure=False)),
			"gui": gui,
			"gui.guiHelper": gui.guiHelper,
			"gui.settingsDialogs": gui.settingsDialogs,
			"inputCore": module("inputCore", InputGesture=object),
			"keyboardHandler": module("keyboardHandler", KeyboardInputGesture=KeyboardGesture),
			"logHandler": module("logHandler", log=Mock()),
			"nvwave": module("nvwave", WavePlayer=SimpleNamespace(DEFAULT_DEVICE_KEY="default")),
			"scriptHandler": module("scriptHandler", script=lambda **kwargs: lambda function: function),
			"speech": self.speech,
			"synthDriverHandler": module("synthDriverHandler", getSynth=self.current, synthChanged=Hook()),
			"synthDrivers": module("synthDrivers"),
			"synthDrivers.shravya": driver,
			"synthDrivers.shravya._delivery_config": module(
				"synthDrivers.shravya._delivery_config", load_delivery_config=self.load_delivery,
			),
			"synthDrivers.shravya._diagnostic_capture": module(
				"synthDrivers.shravya._diagnostic_capture", DiagnosticCapture=self.capture_factory,
			),
			"synthDrivers.shravya._diagnostic_input": module(
				"synthDrivers.shravya._diagnostic_input", InputObserver=self.input_factory,
			),
			"synthDrivers.shravya._diagnostic_measurements": module(
				"synthDrivers.shravya._diagnostic_measurements", TimingEvent=tuple,
			),
			"synthDrivers.shravya._diagnostic_report": module(
				"synthDrivers.shravya._diagnostic_report", format_report=lambda events: "report",
			),
			"synthDrivers.shravya._resource_ui": module(
				"synthDrivers.shravya._resource_ui", ResourceSetup=self.resource_factory,
			),
			"synthDrivers.shravya._update_ui": module(
				"synthDrivers.shravya._update_ui", Updater=self.updater_factory,
			),
			"ui": self.ui,
			"winAPI": module("winAPI"),
			"winAPI.secureDesktop": module(
				"winAPI.secureDesktop", post_secureDesktopStateChange=self.secure_desktop,
			),
			"wx": module("wx"),
		}
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		spec = importlib.util.spec_from_file_location("shravya_test_plugin", PATH)
		self.plugin_module = importlib.util.module_from_spec(spec)
		self.plugin_module.__dict__["_"] = lambda value: value
		spec.loader.exec_module(self.plugin_module)
		thread_patch = patch.object(self.plugin_module.threading, "Thread", ImmediateThread)
		thread_patch.start()
		self.addCleanup(thread_patch.stop)
		self.plugin = self.plugin_module.GlobalPlugin()
		self.addCleanup(self.plugin.terminate)

	def test_capture_attaches_only_when_ready_and_stops_before_report(self):
		order = []
		self.capture.stop.side_effect = lambda: order.append("stop") or []
		self.ui.browseableMessage.side_effect = lambda *args, **kwargs: order.append("report")
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		self.capture_factory.assert_called_once()
		self.assertIsNone(self.capture_factory.call_args.args[0])
		self.synth.set_diagnostic.assert_not_called()
		self.capture_factory.call_args.args[1]("endpoint")
		self.synth.set_diagnostic.assert_called_once_with(self.capture.record)
		self.observer.start.assert_called_once()
		self.plugin.script_controlledDiagnostic(KeyboardGesture())
		self.speech.speakMessage.assert_called_once()
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		self.observer.stop.assert_called_once()
		self.capture.stop.assert_called_once()
		self.ui.browseableMessage.assert_called_once()
		self.assertTrue(self.ui.browseableMessage.call_args.kwargs["copyButton"])
		self.assertEqual(order, ["stop", "report"])

	def test_resource_setup_is_started_and_stopped_with_plugin(self):
		self.resource_factory.assert_called_once_with(self.delivery.catalog_url)
		self.updater_factory.assert_called_once_with(self.delivery.release_api)
		self.plugin.terminate()
		self.resource_setup.terminate.assert_called_once_with()
		self.updater.terminate.assert_called_once_with()

	def test_secure_plugin_does_not_start_resource_setup(self):
		self.plugin_module.globalVars.appArgs.secure = True
		self.resource_factory.reset_mock()
		self.updater_factory.reset_mock()
		plugin = self.plugin_module.GlobalPlugin()
		try:
			self.resource_factory.assert_not_called()
			self.updater_factory.assert_not_called()
		finally:
			plugin.terminate()

	def test_invalid_delivery_configuration_is_reported_without_starting_services(self):
		self.load_delivery.side_effect = ValueError("invalid delivery configuration")
		self.resource_factory.reset_mock()
		self.updater_factory.reset_mock()
		plugin = self.plugin_module.GlobalPlugin()
		try:
			self.resource_factory.assert_not_called()
			self.updater_factory.assert_not_called()
			self.assertIn("configuration is invalid", self.ui.message.call_args.args[0].lower())
			self.plugin_module.log.error.assert_called()
		finally:
			plugin.terminate()

	def start_ready(self):
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		self.capture_factory.call_args.args[1]("endpoint")

	def test_capture_failure_never_presents_a_report(self):
		self.start_ready()
		self.capture.stop.side_effect = RuntimeError("device changed")
		self.capture_factory.call_args.args[2](RuntimeError("device changed"))
		self.ui.browseableMessage.assert_not_called()
		self.observer.stop.assert_called_once()
		self.assertIn("failed", self.ui.message.call_args.args[0].lower())

	def test_changing_synth_discards_the_capture(self):
		self.start_ready()
		self.plugin._on_synth_changed(object())
		self.capture.stop.assert_called_once()
		self.ui.browseableMessage.assert_not_called()
		self.assertIn("changed", self.ui.message.call_args.args[0].lower())

	def test_secure_desktop_prevents_capture(self):
		self.ui.isRunningOnSecureDesktop.return_value = True
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		self.capture_factory.assert_not_called()

	def test_entering_secure_desktop_discards_capture_and_defers_notification(self):
		self.start_ready()
		self.ui.message.reset_mock()
		self.secure_desktop.notify(isSecureDesktop=True)
		self.observer.stop.assert_called_once()
		self.capture.stop.assert_called_once()
		self.ui.browseableMessage.assert_not_called()
		self.ui.message.assert_not_called()
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		self.assertEqual(self.capture_factory.call_count, 1)
		self.ui.message.reset_mock()
		self.secure_desktop.notify(isSecureDesktop=False)
		self.assertIn("discarded", self.ui.message.call_args.args[0].lower())

	def test_secure_desktop_discards_a_report_still_being_collected(self):
		self.start_ready()
		entered, release, finished = threading.Event(), threading.Event(), threading.Event()

		def stop():
			entered.set()
			if not release.wait(5):
				raise RuntimeError("test timed out")
			return [(1, "key", "controlled")]

		self.capture.stop.side_effect = stop
		original = self.plugin._capture_stopped

		def stopped(*args):
			original(*args)
			finished.set()

		self.plugin._capture_stopped = stopped
		with patch.object(self.plugin_module.threading, "Thread", REAL_THREAD):
			try:
				self.plugin.script_toggleDiagnostic(KeyboardGesture())
				self.assertTrue(entered.wait(5))
				self.ui.message.reset_mock()
				self.secure_desktop.notify(isSecureDesktop=True)
			finally:
				release.set()
			self.assertTrue(finished.wait(5))
		self.ui.browseableMessage.assert_not_called()
		self.ui.message.assert_not_called()
		self.secure_desktop.notify(isSecureDesktop=False)
		self.assertIn("discarded", self.ui.message.call_args.args[0].lower())

	def test_explicit_endpoint_is_used_and_profile_change_discards_capture(self):
		self.conf["audio"]["outputDevice"] = "output-id"
		self.start_ready()
		self.assertEqual(self.capture_factory.call_args.args[0], "output-id")
		self.conf["audio"]["outputDevice"] = "other-id"
		self.plugin._on_profile_switch()
		self.capture.stop.assert_called_once()
		self.ui.browseableMessage.assert_not_called()

	def test_late_ready_callback_does_not_restart_stopped_capture(self):
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		ready = self.capture_factory.call_args.args[1]
		self.plugin.script_toggleDiagnostic(KeyboardGesture())
		ready("endpoint")
		self.input_factory.assert_not_called()

	def test_failed_observer_registration_discards_capture(self):
		self.observer.start.side_effect = RuntimeError("hook unavailable")
		self.start_ready()
		self.capture.stop.assert_called_once()
		self.synth.set_diagnostic.assert_called_with(None)
		self.ui.browseableMessage.assert_not_called()

	def test_stopping_does_not_block_the_input_thread(self):
		self.start_ready()
		entered, release, finished = threading.Event(), threading.Event(), threading.Event()

		def stop():
			entered.set()
			if not release.wait(5):
				raise RuntimeError("test timed out")
			return []

		self.capture.stop.side_effect = stop
		self.ui.browseableMessage.side_effect = lambda *args, **kwargs: finished.set()
		with patch.object(self.plugin_module.threading, "Thread", REAL_THREAD):
			try:
				self.plugin.script_toggleDiagnostic(KeyboardGesture())
				self.assertTrue(entered.wait(5))
				self.ui.browseableMessage.assert_not_called()
				self.plugin.script_toggleDiagnostic(KeyboardGesture())
				self.assertIn("stopping", self.ui.message.call_args.args[0].lower())
			finally:
				release.set()
			self.assertTrue(finished.wait(5))


if __name__ == "__main__":
	unittest.main()
