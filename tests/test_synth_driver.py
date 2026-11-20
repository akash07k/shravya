import importlib.util
import json
import sys
import tempfile
import unittest
from ctypes import c_float
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

DRIVER_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "__init__.py"


class IndexCommand:
	def __init__(self, index):
		self.index = index


class BreakCommand:
	def __init__(self, time):
		self.time = time


class PitchCommand:
	def __init__(self, value=50):
		self.newValue = value


class BaseSynthDriver:
	@property
	def availableVoices(self):
		return self._get_availableVoices()

	def _get_availableVoices(self):
		if not hasattr(self, "_availableVoices"):
			self._availableVoices = self._getAvailableVoices()
		return self._availableVoices

	@classmethod
	def VoiceSetting(cls):
		return object()

	@classmethod
	def RateSetting(cls):
		return object()

	@classmethod
	def PitchSetting(cls):
		return object()

	@classmethod
	def VolumeSetting(cls):
		return object()

	def terminate(self):
		pass


class Action:
	def __init__(self, notify):
		self.notify = notify


class MonoWavePlayer:
	def __init__(self, **kwargs):
		self._player = object()
		self.settings = kwargs

	def setVolume(self, *, all=None, left=None, right=None):
		if all is not None:
			left = all
		sys.modules["nvwave"].wasapi.wasPlay_setChannelVolume(self._player, 0, c_float(left))
		if not all:
			raise OSError("NVDA attempted to set a nonexistent second channel")


class FakeConfig(dict):
	def __init__(self):
		super().__init__(
			audio={"outputDevice": "test-device"},
			shravya={"rateMethod": "native", "useCuda": True, "steps": 4, "seed": 0},
		)
		self.spec = {}


def module(name, **attributes):
	result = ModuleType(name)
	for key, value in attributes.items():
		setattr(result, key, value)
	return result


class TestSynthDriver(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		(self.root / "python").mkdir()
		(self.root / "bluev3_onnx" / "onnx").mkdir(parents=True)
		voices = self.root / "bluev3_onnx" / "voices"
		voices.mkdir()
		(voices / "noa.json").write_text("{}", encoding="utf-8")
		self.index = Mock()
		self.done = Mock()
		self.log = Mock()
		self.message = Mock()
		self.find_next = Mock()
		self.current = Mock()
		self.conf = FakeConfig()
		self.modules = {
			"addonHandler": module(
				"addonHandler",
				initTranslation=lambda: None,
				getCodeAddon=lambda: SimpleNamespace(manifest={"version": "1.0.0"}),
			),
			"config": module("config", conf=self.conf),
			"core": module("core", callLater=lambda delay, function: function()),
			"NVDAState": module(
				"NVDAState", WritePaths=module("WritePaths", configDir=str(self.root / "config"))
			),
			"nvwave": module(
				"nvwave",
				WavePlayer=MonoWavePlayer,
				wasapi=SimpleNamespace(wasPlay_setChannelVolume=Mock()),
			),
			"speech": module("speech"),
			"speech.commands": module(
				"speech.commands",
				IndexCommand=IndexCommand,
				BreakCommand=BreakCommand,
				PitchCommand=PitchCommand,
			),
			"speech.types": module("speech.types", SpeechSequence=list),
			"synthDriverHandler": module(
				"synthDriverHandler",
				SynthDriver=BaseSynthDriver,
				VoiceInfo=lambda id, name, language=None: (id, name, language),
				synthIndexReached=Action(self.index),
				synthDoneSpeaking=Action(self.done),
				getSynth=self.current,
				findAndSetNextSynth=self.find_next,
			),
			"logHandler": module("logHandler", log=self.log),
			"ui": module("ui", message=self.message),
		}
		patcher = patch.dict(sys.modules, self.modules)
		patcher.start()
		self.addCleanup(patcher.stop)
		spec = importlib.util.spec_from_file_location(
			"shravya_test_driver", DRIVER_PATH, submodule_search_locations=[str(DRIVER_PATH.parent)]
		)
		self.driver_module = importlib.util.module_from_spec(spec)
		self.driver_module.__dict__["_"] = lambda value: value
		sys.modules[spec.name] = self.driver_module
		spec.loader.exec_module(self.driver_module)
		self.real_resource_directories = self.driver_module.resource_directories
		locations = patch.object(self.driver_module, "resource_directories", return_value=(self.root, self.root))
		locations.start()
		self.addCleanup(locations.stop)
		self.worker_patcher = patch.object(self.driver_module, "SpeechWorker")
		self.worker = self.worker_patcher.start()
		self.addCleanup(self.worker_patcher.stop)

	def test_missing_resources_fail_before_starting_worker(self):
		(self.root / "bluev3_onnx" / "voices" / "noa.json").unlink()
		with self.assertRaises(FileNotFoundError):
			self.driver_module.SynthDriver()
		self.worker.assert_not_called()

	def test_mono_player_mutes_exactly_and_propagates_channel_failures(self):
		driver = self.driver_module.SynthDriver()
		player = driver._make_player(24000)
		native = self.modules["nvwave"].wasapi.wasPlay_setChannelVolume
		self.assertEqual(player.settings["channels"], 1)
		player.setVolume(all=0)
		self.assertEqual(native.call_args.args[1], 0)
		self.assertEqual(native.call_args.args[2].value, 0.0)
		player.setVolume(all=0.5)
		self.assertEqual(native.call_args.args[2].value, 0.5)
		native.side_effect = OSError("first channel failed")
		with self.assertRaisesRegex(OSError, "first channel failed"):
			player.setVolume(all=0)

	def test_driver_uses_persistent_pack_for_voices_and_engine(self):
		config = self.root / "config"
		packs = config / "shravya" / "packs"
		runtime = packs / "runtime" / "runtime-1"
		model = packs / "model" / "model-1"
		(runtime / "python").mkdir(parents=True)
		(model / "bluev3_onnx" / "onnx").mkdir(parents=True)
		voices = model / "bluev3_onnx" / "voices"
		voices.mkdir()
		(voices / "noa.json").write_text("{}", encoding="utf-8")
		(voices / "other.json").write_text("{}", encoding="utf-8")
		(config / "shravya" / "active-resources.json").write_text(
			json.dumps({"interface": "bluev3-v1", "runtime": "runtime-1", "model": "model-1"}),
			encoding="utf-8",
		)
		with (
			patch.object(self.driver_module, "resource_directories", side_effect=self.real_resource_directories),
			patch.object(self.driver_module, "BlueEngine") as engine,
		):
			driver = self.driver_module.SynthDriver()
			self.assertIn("other", driver._getAvailableVoices())
			driver._load_engine(False)
			engine.assert_called_once_with(runtime, use_cuda=False, model_resources=model)

	def test_split_model_update_is_used_on_next_engine_reload(self):
		runtime = self.root / "runtime"
		(runtime / "python").mkdir(parents=True)
		models = []
		for name in ("model-1", "model-2"):
			model = self.root / name
			(model / "bluev3_onnx" / "onnx").mkdir(parents=True)
			voices = model / "bluev3_onnx" / "voices"
			voices.mkdir()
			(voices / "noa.json").write_text("{}", encoding="utf-8")
			(voices / f"{name}.json").write_text("{}", encoding="utf-8")
			models.append(model)
		selection = [(runtime, models[0])]
		with (
			patch.object(
				self.driver_module,
				"resource_directories",
				side_effect=lambda *args, **kwargs: selection[0],
				create=True,
			),
			patch.object(self.driver_module, "BlueEngine") as engine,
		):
			driver = self.driver_module.SynthDriver()
			self.current.return_value = driver
			driver._load_engine(False)
			engine.assert_called_once_with(runtime, use_cuda=False, model_resources=models[0])
			driver._set_voice("model-1")
			selection[0] = (runtime, models[1])
			self.assertIn("model-1", driver.availableVoices)
			driver._load_engine(False)
			engine.assert_called_with(
				runtime, use_cuda=False, model_resources=models[1], fallback_voices={"model-1"}
			)
			self.assertIn("model-2", driver.availableVoices)
			self.assertNotIn("model-1", driver.availableVoices)
			self.assertEqual(driver._get_voice(), "noa")
			driver._set_voice("model-2")
			with self.assertRaises(ValueError):
				driver._set_voice("model-1")
			self.message.assert_called_once()

	def test_runtime_change_does_not_replace_loaded_runtime_during_model_reload(self):
		other = self.root / "other-runtime"
		with (
			patch.object(
				self.driver_module, "resource_directories", return_value=(self.root, self.root), create=True
			) as select,
			patch.object(self.driver_module, "BlueEngine") as engine,
		):
			driver = self.driver_module.SynthDriver()
			self.assertEqual(select.call_args.kwargs["addon_version"], "1.0.0")
			select.return_value = (other, other)
			driver._load_engine(False)
			engine.assert_called_once_with(self.root, use_cuda=False, model_resources=self.root)

	def test_forwards_speech_controls_and_playback_notifications(self):
		driver = self.driver_module.SynthDriver()
		self.assertEqual(driver._get_language(), "en")
		driver.speak(["hello", " world", IndexCommand(4), BreakCommand(20), "next"])
		instance = self.worker.return_value
		instance.speak.assert_called_once_with(
			["hello world", ("index", 4), ("break", 20), "next"],
			speed=1.0,
			steps=4,
			seed=0,
			voice="noa",
			method="native",
			pitch=50,
		)
		kwargs = self.worker.call_args.kwargs
		kwargs["on_index"](4)
		kwargs["on_done"]()
		self.index.assert_called_once_with(synth=driver, index=4)
		self.done.assert_called_once_with(synth=driver)
		driver.pause(True)
		driver.cancel()
		driver.terminate()
		instance.pause.assert_called_once_with(True)
		instance.cancel.assert_called_once_with()
		instance.close.assert_called_once_with()

	def test_cuda_unavailable_falls_back_to_cpu(self):
		driver = self.driver_module.SynthDriver()
		self.current.return_value = driver
		engine_factory = self.worker.call_args.kwargs["engine_factory"]
		error = self.driver_module.CudaUnavailableError("missing runtime")
		cpu = object()
		with patch.object(self.driver_module, "BlueEngine", side_effect=[error, cpu]) as engine:
			self.assertIs(engine_factory(), cpu)
		self.assertEqual(
			engine.call_args_list,
			[
				unittest.mock.call(self.root, use_cuda=True, model_resources=self.root),
				unittest.mock.call(self.root, use_cuda=False, model_resources=self.root),
			],
		)
		self.message.assert_called_once()

	def test_cuda_synthesis_failure_reports_and_loads_cpu(self):
		driver = self.driver_module.SynthDriver()
		self.current.return_value = driver
		recover = self.worker.call_args.kwargs["on_cuda_failure"]
		error = self.driver_module.CudaSynthesisError("CUDA synthesis failed")
		with patch.object(self.driver_module, "BlueEngine") as engine:
			self.assertIs(recover(error), engine.return_value)
		engine.assert_called_once_with(self.root, use_cuda=False, model_resources=self.root)
		self.message.assert_called_once()
		self.assertTrue(self.conf["shravya"]["useCuda"])

	def test_fatal_error_switches_to_a_working_synth_and_is_reported(self):
		driver = self.driver_module.SynthDriver()
		self.current.side_effect = [driver, object()]
		on_error = self.worker.call_args.kwargs["on_error"]
		on_error(RuntimeError("broken"))
		self.find_next.assert_called_once_with("shravya")
		self.log.error.assert_called_once()
		self.message.assert_called_once()

	def test_fatal_error_does_not_claim_a_failed_fallback_succeeded(self):
		driver = self.driver_module.SynthDriver()
		self.current.return_value = driver
		self.worker.call_args.kwargs["on_error"](RuntimeError("broken"))
		self.message.assert_not_called()
		self.assertEqual(self.log.error.call_count, 2)

	def test_volume_setting_controls_player(self):
		driver = self.driver_module.SynthDriver()
		driver._set_volume(35)
		self.assertEqual(driver._get_volume(), 35)
		self.worker.return_value.set_volume.assert_called_once_with(35)

	def test_diagnostic_observer_can_be_attached_and_detached(self):
		driver = self.driver_module.SynthDriver()
		observer = Mock()
		driver.set_diagnostic(observer)
		driver.set_diagnostic(None)
		self.assertEqual(
			self.worker.return_value.set_diagnostic.call_args_list,
			[unittest.mock.call(observer), unittest.mock.call(None)],
		)

	def test_rate_is_applied_to_new_speech_without_interrupting_current_speech(self):
		driver = self.driver_module.SynthDriver()
		driver.speak(["first"])
		driver._set_rate(100)
		driver.speak(["second"])
		self.assertEqual(driver._get_rate(), 100)
		self.assertEqual(
			self.worker.return_value.speak.call_args_list,
			[
				unittest.mock.call(["first"], speed=1.0, steps=4, seed=0, voice="noa", method="native", pitch=50),
				unittest.mock.call(["second"], speed=2.0, steps=4, seed=0, voice="noa", method="native", pitch=50),
			],
		)
		self.worker.return_value.cancel.assert_not_called()
		driver._set_rate(0)
		driver.speak(["third"])
		self.worker.return_value.speak.assert_called_with(
			["third"], speed=0.5, steps=4, seed=0, voice="noa", method="native", pitch=50
		)

	def test_preset_voice_selector_defaults_to_noa_and_snapshots_changes(self):
		(self.root / "bluev3_onnx" / "voices" / "adam.json").write_text("{}", encoding="utf-8")
		driver = self.driver_module.SynthDriver()
		self.assertEqual(list(driver.availableVoices), ["adam", "noa"])
		self.assertEqual(driver._get_voice(), "noa")
		driver.speak(["before"])
		driver._set_voice("adam")
		driver.speak(["after"])
		self.assertEqual(driver._get_voice(), "adam")
		self.assertEqual(self.worker.return_value.speak.call_args_list[0].kwargs["voice"], "noa")
		self.assertEqual(self.worker.return_value.speak.call_args_list[1].kwargs["voice"], "adam")
		with self.assertRaises(ValueError):
			driver._set_voice("../outside")

	def test_rate_method_and_steps_apply_only_to_new_requests(self):
		driver = self.driver_module.SynthDriver()
		driver.speak(["first"])
		self.conf["shravya"].update(rateMethod="sonic", steps=8, seed=42)
		driver.speak(["second"])
		self.assertEqual(
			self.worker.return_value.speak.call_args_list,
			[
				unittest.mock.call(["first"], speed=1.0, steps=4, seed=0, voice="noa", method="native", pitch=50),
				unittest.mock.call(["second"], speed=1.0, steps=8, seed=42, voice="noa", method="sonic", pitch=50),
			],
		)
		self.worker.return_value.reload.assert_not_called()
		self.worker.return_value.cancel.assert_not_called()

	def test_compute_change_requests_worker_reload_with_selected_provider(self):
		driver = self.driver_module.SynthDriver()
		self.conf["shravya"]["useCuda"] = False
		driver.apply_settings()
		self.worker.return_value.reload.assert_called_once()
		factory = self.worker.return_value.reload.call_args.args[0]
		with patch.object(self.driver_module, "BlueEngine") as engine:
			factory()
		engine.assert_called_once_with(self.root, use_cuda=False, model_resources=self.root)
		driver.apply_settings()
		self.worker.return_value.reload.assert_called_once()

	def test_pitch_setting_and_commands_apply_to_the_speech_sequence(self):
		driver = self.driver_module.SynthDriver()
		self.assertIn(PitchCommand, driver.supportedCommands)
		driver._set_pitch(30)
		driver.speak(["before", PitchCommand(80), "capital", PitchCommand(30), "after"])
		self.worker.return_value.speak.assert_called_once_with(
			["before", ("pitch", 80), "capital", ("pitch", 30), "after"],
			speed=1.0,
			steps=4,
			seed=0,
			voice="noa",
			method="native",
			pitch=30,
		)
		self.assertEqual(driver._get_pitch(), 30)

	def test_pitch_command_is_clamped_and_invalid_setting_is_rejected(self):
		driver = self.driver_module.SynthDriver()
		driver.speak([PitchCommand(130), "high", PitchCommand(-10), "low"])
		self.assertEqual(
			self.worker.return_value.speak.call_args.args[0],
			[("pitch", 100), "high", ("pitch", 0), "low"],
		)
		with self.assertRaises(ValueError):
			driver._set_pitch(-1)
		with self.assertRaises(ValueError):
			driver._set_pitch(101)

	def test_cpu_preference_loads_cpu_directly(self):
		self.conf["shravya"]["useCuda"] = False
		self.driver_module.SynthDriver()
		factory = self.worker.call_args.kwargs["engine_factory"]
		with patch.object(self.driver_module, "BlueEngine") as engine:
			factory()
		engine.assert_called_once_with(self.root, use_cuda=False, model_resources=self.root)
		self.message.assert_not_called()

	def test_invalid_settings_are_rejected_without_partial_save(self):
		settings = self.driver_module._settings
		before = self.conf["shravya"].copy()
		with self.assertRaises(ValueError):
			settings.save("sonic", False, 0, 0)
		with self.assertRaises(ValueError):
			settings.save("sonic", False, settings.MAX_STEPS + 1, 0)
		with self.assertRaises(ValueError):
			settings.save("sonic", False, 8, -1)
		with self.assertRaises(ValueError):
			settings.save("sonic", False, 8, settings.MAX_SEED + 1)
		self.assertEqual(self.conf["shravya"], before)
		settings.save("sonic", False, 8, 42)
		self.assertEqual(self.conf["shravya"], {"rateMethod": "sonic", "useCuda": False, "steps": 8, "seed": 42})
