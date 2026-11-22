import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "addon" / "globalPlugins" / "shravya.py"


def module(name, **attributes):
	result = ModuleType(name)
	for key, value in attributes.items():
		setattr(result, key, value)
	return result


class Choice:
	def __init__(self, parent, *, choices):
		self.choices = choices
		self.selection = -1

	def SetSelection(self, selection):
		self.selection = selection

	def GetSelection(self):
		return self.selection


class Spin:
	def __init__(self, parent, *, min, max, initial):
		self.value = initial

	def GetValue(self):
		return self.value


class Button:
	def __init__(self, parent, *, label):
		self.label = label
		self.enabled = True
		self.handler = None

	def Enable(self, enable):
		self.enabled = enable

	def Bind(self, event, handler):
		self.handler = handler


class Helper:
	def __init__(self, panel, *, sizer):
		self.panel = panel

	def addLabeledControl(self, label, control, **kwargs):
		return control(self.panel, **kwargs)

	def addItem(self, control):
		self.panel.button = control
		return control


class FakeSynth:
	def __init__(self):
		self.apply_settings = Mock()


class TestSettingsPanel(unittest.TestCase):
	def setUp(self):
		self.settings = SimpleNamespace(
			MAX_STEPS=2**31 - 1,
			MAX_SEED=2**31 - 1,
			rate_method=Mock(return_value="native"),
			use_cuda=Mock(return_value=True),
			steps=Mock(return_value=4),
			seed=Mock(return_value=0),
			save=Mock(),
		)
		self.current = Mock(return_value=FakeSynth())
		self.profile_switch = SimpleNamespace(register=Mock(), unregister=Mock())
		self.synth_changed = SimpleNamespace(register=Mock(), unregister=Mock())
		self.secure_desktop = SimpleNamespace(register=Mock(), unregister=Mock())
		self.categories = []
		dialogs = module(
			"gui.settingsDialogs",
			SettingsPanel=type("SettingsPanel", (), {}),
			NVDASettingsDialog=SimpleNamespace(categoryClasses=self.categories),
		)
		gui_helper = module("gui.guiHelper", BoxSizerHelper=Helper)
		gui = module("gui", settingsDialogs=dialogs, guiHelper=gui_helper)
		drivers = module("synthDrivers")
		drivers.__path__ = []
		modules = {
			"addonHandler": module("addonHandler", initTranslation=lambda: None),
			"config": module("config", post_configProfileSwitch=self.profile_switch),
			"core": module("core"),
			"globalPluginHandler": module(
				"globalPluginHandler", GlobalPlugin=type("GlobalPlugin", (), {"terminate": lambda self: None}),
			),
			"globalVars": module("globalVars", appArgs=SimpleNamespace(secure=False)),
			"gui": gui,
			"gui.guiHelper": gui_helper,
			"gui.settingsDialogs": dialogs,
			"inputCore": module("inputCore", InputGesture=object),
			"keyboardHandler": module("keyboardHandler", KeyboardInputGesture=object),
			"logHandler": module("logHandler", log=Mock()),
			"nvwave": module("nvwave"),
			"scriptHandler": module("scriptHandler", script=lambda **kwargs: lambda function: function),
			"speech": module("speech"),
			"ui": module("ui"),
			"winAPI": module("winAPI"),
			"winAPI.secureDesktop": module(
				"winAPI.secureDesktop", post_secureDesktopStateChange=self.secure_desktop,
			),
			"wx": module("wx", Choice=Choice, SpinCtrl=Spin, Button=Button, EVT_BUTTON=object()),
			"synthDrivers": drivers,
			"synthDrivers.shravya": module(
				"synthDrivers.shravya", SynthDriver=FakeSynth, _settings=self.settings,
			),
			"synthDrivers.shravya._delivery_config": module(
				"synthDrivers.shravya._delivery_config",
				load_delivery_config=lambda: SimpleNamespace(
					catalog_url=None, release_api=None,
				),
			),
			"synthDrivers.shravya._diagnostic_input": module(
				"synthDrivers.shravya._diagnostic_input", InputObserver=object,
			),
			"synthDrivers.shravya._diagnostic_measurements": module(
				"synthDrivers.shravya._diagnostic_measurements", TimingEvent=tuple,
			),
			"synthDrivers.shravya._diagnostic_report": module(
				"synthDrivers.shravya._diagnostic_report", format_report=Mock(),
			),
			"synthDrivers.shravya._resource_ui": module(
				"synthDrivers.shravya._resource_ui", ResourceSetup=Mock(),
			),
			"synthDrivers.shravya._update_ui": module("synthDrivers.shravya._update_ui", Updater=Mock()),
			"synthDriverHandler": module(
				"synthDriverHandler", getSynth=self.current, synthChanged=self.synth_changed,
			),
		}
		patcher = patch.dict(sys.modules, modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		spec = importlib.util.spec_from_file_location("shravya_panel_test", PLUGIN_PATH)
		self.plugin = importlib.util.module_from_spec(spec)
		self.plugin.__dict__["_"] = lambda value: value
		spec.loader.exec_module(self.plugin)

	def test_panel_saves_and_applies_selected_controls(self):
		panel = self.plugin.ShravyaSettingsPanel()
		panel.makeSettings(object())
		self.assertEqual(panel.rate_method.GetSelection(), 0)
		self.assertEqual(panel.compute.GetSelection(), 1)
		self.assertEqual(panel.steps.GetValue(), 4)
		self.assertEqual(panel.seed.GetValue(), 0)
		panel.rate_method.SetSelection(1)
		panel.compute.SetSelection(0)
		panel.steps.value = 8
		panel.seed.value = 42
		panel.onSave()
		self.settings.save.assert_called_once_with("sonic", False, 8, 42)
		self.current.return_value.apply_settings.assert_called_once()

	def test_panel_rejects_an_invalid_selection(self):
		panel = self.plugin.ShravyaSettingsPanel()
		panel.makeSettings(object())
		panel.compute.SetSelection(-1)
		with self.assertRaises(ValueError):
			panel.onSave()
		self.settings.save.assert_not_called()

	def test_plugin_registers_and_unregisters_profile_handler_and_panel(self):
		plugin = self.plugin.GlobalPlugin()
		self.assertIn(self.plugin.ShravyaSettingsPanel, self.categories)
		panel = self.plugin.ShravyaSettingsPanel()
		panel.makeSettings(object())
		self.assertTrue(panel.button.enabled)
		panel.button.handler(object())
		plugin._resource_setup.open.assert_called_once_with(panel)
		handler = self.profile_switch.register.call_args.args[0]
		handler()
		self.current.return_value.apply_settings.assert_called_once()
		self.current.return_value = object()
		handler()
		plugin.terminate()
		self.assertIsNone(self.plugin.ShravyaSettingsPanel.resource_setup)
		self.assertNotIn(self.plugin.ShravyaSettingsPanel, self.categories)
		self.profile_switch.unregister.assert_called_once_with(handler)
		self.synth_changed.unregister.assert_called_once_with(self.synth_changed.register.call_args.args[0])
		self.secure_desktop.unregister.assert_called_once_with(
			self.secure_desktop.register.call_args.args[0],
		)

	def test_setup_button_is_disabled_when_delivery_configuration_failed(self):
		panel = self.plugin.ShravyaSettingsPanel()
		panel.makeSettings(object())
		self.assertFalse(panel.button.enabled)
