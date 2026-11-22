import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ENGINE_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_blue.py"


def load_engine():
	spec = importlib.util.spec_from_file_location("shravya_blue_test", ENGINE_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class FakeModel:
	sample_rate = 44100
	style = object()

	def __init__(self, providers):
		session = SimpleNamespace(get_providers=lambda: providers, disable_fallback=unittest.mock.Mock())
		self.tts = SimpleNamespace(
			dp_ort=session,
			text_enc_ort=session,
			vector_est_ort=session,
			vocoder_ort=session,
			check_style=lambda style: None,
			sample_rate=1000,
			base_chunk_size=10,
			chunk_compress_factor=2,
			ldim=1,
			sample_noisy_latent=lambda duration: (None, None),
		)
		self.calls = []

	def stream(self, text, *, lang, speed, total_step, style=None):
		self.calls.append((text, lang, speed, total_step, style))
		yield np.array([-1.0, 0.0, 1.0], dtype=np.float32)


class OrtFailure(Exception):
	pass


class TestBlueEngine(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		(self.root / "python").mkdir()
		(self.root / "bluev3_onnx" / "onnx").mkdir(parents=True)
		voices = self.root / "bluev3_onnx" / "voices"
		voices.mkdir()
		(voices / "noa.json").write_text("{}", encoding="utf-8")
		self.engine_module = load_engine()

	def fake_imports(self, model):
		self.preload_calls = []
		self.factory_calls = []
		self.load_style = unittest.mock.Mock(return_value=object())

		def factory(**kwargs):
			self.factory_calls.append(kwargs)
			if isinstance(model, Exception):
				raise model
			return model

		def load(name):
			if name == "blue_onnx":
				return SimpleNamespace(
					BlueTTS=factory, load_voice_style=self.load_style,
					latent_frames_for_duration=lambda duration, rate, size, compression: 3,
					get_latent_mask=lambda lengths, size, compression, max_len: np.ones((1, 1, max_len)),
				)
			if name == "onnxruntime":
				return SimpleNamespace(
					get_available_providers=lambda: ["CPUExecutionProvider", "CUDAExecutionProvider"],
					preload_dlls=lambda **kwargs: self.preload_calls.append(kwargs),
				)
			if name == "onnxruntime.capi.onnxruntime_pybind11_state":
				return SimpleNamespace(Fail=OrtFailure, RuntimeException=OrtFailure, EPFail=OrtFailure)
			raise AssertionError(name)

		return patch.object(self.engine_module.importlib, "import_module", side_effect=load)

	def test_streams_english_pcm_and_cleans_up_import_path(self):
		model = FakeModel(["CPUExecutionProvider"])
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(self.root, use_cuda=False, model_resources=self.root)
			self.assertIn(str(self.root / "python"), sys.path)
			self.assertEqual(engine.sample_rate, 44100)
			audio = list(engine.stream("Hello", speed=1.25, steps=4))
			engine.close()

		self.assertEqual(model.calls, [("Hello", "en", 1.25, 4, None)])
		self.assertEqual(np.frombuffer(audio[0], dtype="<i2").tolist(), [-32767, 0, 32767])
		self.assertNotIn(str(self.root / "python"), sys.path)
		self.assertEqual(model.tts.dp_ort.disable_fallback.call_count, 4)

	def test_seed_repeats_speech_without_changing_global_random_state(self):
		model = FakeModel(["CPUExecutionProvider"])
		original = model.tts.sample_noisy_latent

		def stream(*args, **kwargs):
			for _ in range(2):
				noise, _ = model.tts.sample_noisy_latent(np.array([0.1], dtype=np.float32))
				yield noise.flatten() * 0.1

		model.stream = stream
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(self.root, use_cuda=False, model_resources=self.root)
			try:
				state = np.random.get_state()
				first = list(engine.stream("same text", speed=1, steps=4, seed=7))
				after = np.random.get_state()
				second = list(engine.stream("same text", speed=1, steps=4, seed=7))
				other = list(engine.stream("same text", speed=1, steps=4, seed=8))
				self.assertEqual(first, second)
				self.assertNotEqual(first, other)
				self.assertNotEqual(first[0], first[1])
				self.assertEqual(state[0], after[0])
				np.testing.assert_array_equal(state[1], after[1])
				self.assertEqual(state[2:], after[2:])
				self.assertIs(model.tts.sample_noisy_latent, original)
				interrupted = engine.stream("same text", speed=1, steps=4, seed=7)
				next(interrupted)
				interrupted.close()
				self.assertIs(model.tts.sample_noisy_latent, original)
			finally:
				engine.close()

	def test_loads_model_from_a_separate_resource_directory(self):
		model_root = self.root / "model-pack"
		model_root.mkdir()
		(self.root / "bluev3_onnx").rename(model_root / "bluev3_onnx")
		model = FakeModel(["CPUExecutionProvider"])
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(self.root, model_resources=model_root, use_cuda=False)
			engine.close()
		self.assertEqual(self.factory_calls[0]["onnx_dir"], str(model_root / "bluev3_onnx" / "onnx"))
		self.assertEqual(
			self.factory_calls[0]["style_json"], str(model_root / "bluev3_onnx" / "voices" / "noa.json"),
		)

	def test_queued_voice_removed_by_model_update_uses_default(self):
		model = FakeModel(["CPUExecutionProvider"])
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(
				self.root, use_cuda=False, model_resources=self.root, fallback_voices={"adam"},
			)
			try:
				list(engine.stream("test", speed=1, steps=4, voice="adam"))
				with self.assertRaises(ValueError):
					list(engine.stream("test", speed=1, steps=4, voice="unrecognized"))
			finally:
				engine.close()
		self.assertIsNone(model.calls[0][-1])

	def test_cuda_registration_failure_cleans_up(self):
		model = FakeModel(["CPUExecutionProvider"])
		for directory in ("cu13/bin/x86_64", "cudnn/bin"):
			(self.root / "python" / "nvidia" / directory).mkdir(parents=True)
		with (
			self.fake_imports(model),
			patch.object(self.engine_module.os, "add_dll_directory", create=True) as add_dll,
		):
			add_dll.return_value = SimpleNamespace(close=lambda: None)
			with self.assertRaises(self.engine_module.CudaUnavailableError):
				self.engine_module.BlueEngine(self.root, use_cuda=True, model_resources=self.root)

		self.assertNotIn(str(self.root / "python"), sys.path)
		self.assertEqual(self.preload_calls, [{"cuda": False, "cudnn": True, "msvc": False, "directory": ""}])

	def test_cuda_registration_failure_in_one_stage_is_not_ignored(self):
		model = FakeModel(["CUDAExecutionProvider", "CPUExecutionProvider"])
		model.tts.vocoder_ort = SimpleNamespace(
			get_providers=lambda: ["CPUExecutionProvider"], disable_fallback=unittest.mock.Mock(),
		)
		for directory in ("cu13/bin/x86_64", "cudnn/bin"):
			(self.root / "python" / "nvidia" / directory).mkdir(parents=True)
		with (
			self.fake_imports(model),
			patch.object(self.engine_module.os, "add_dll_directory", create=True) as add_dll,
		):
			add_dll.return_value = SimpleNamespace(close=lambda: None)
			with self.assertRaises(self.engine_module.CudaUnavailableError):
				self.engine_module.BlueEngine(self.root, use_cuda=True, model_resources=self.root)

	def test_missing_voice_is_reported(self):
		(self.root / "bluev3_onnx" / "voices" / "noa.json").unlink()
		with self.assertRaises(FileNotFoundError):
			self.engine_module.BlueEngine(self.root, use_cuda=False, model_resources=self.root)
		self.assertNotIn(str(self.root / "python"), sys.path)

	def test_cuda_inference_failure_is_classified_but_unrelated_errors_are_not(self):
		model = FakeModel(["CUDAExecutionProvider", "CPUExecutionProvider"])
		for directory in ("cu13/bin/x86_64", "cudnn/bin"):
			(self.root / "python" / "nvidia" / directory).mkdir(parents=True)

		def fail(error):
			def stream(*args, **kwargs):
				raise error
				yield

			model.stream = stream

		with (
			self.fake_imports(model),
			patch.object(self.engine_module.os, "add_dll_directory", create=True) as add_dll,
		):
			add_dll.return_value = SimpleNamespace(close=lambda: None)
			engine = self.engine_module.BlueEngine(self.root, use_cuda=True, model_resources=self.root)
			try:
				fail(OrtFailure("CUDA failure 2: out of memory"))
				with self.assertRaises(self.engine_module.CudaSynthesisError):
					list(engine.stream("test", speed=1, steps=4))
				fail(OrtFailure("invalid model input"))
				with self.assertRaisesRegex(OrtFailure, "invalid model input"):
					list(engine.stream("test", speed=1, steps=4))
			finally:
				engine.close()

	def test_cuda_initialization_failure_is_classified(self):
		for directory in ("cu13/bin/x86_64", "cudnn/bin"):
			(self.root / "python" / "nvidia" / directory).mkdir(parents=True)
		with (
			self.fake_imports(OrtFailure("CUDA failure 100: no CUDA-capable device")),
			patch.object(self.engine_module.os, "add_dll_directory", create=True) as add_dll,
		):
			add_dll.return_value = SimpleNamespace(close=lambda: None)
			with self.assertRaises(self.engine_module.CudaUnavailableError):
				self.engine_module.BlueEngine(self.root, use_cuda=True, model_resources=self.root)
		self.assertNotIn(str(self.root / "python"), sys.path)

	def test_cpu_inference_errors_are_not_classified_as_cuda_failures(self):
		model = FakeModel(["CPUExecutionProvider"])

		def stream(*args, **kwargs):
			raise OrtFailure("CUDA failure 2: unrelated to this CPU run")
			yield

		model.stream = stream
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(self.root, use_cuda=False, model_resources=self.root)
			try:
				with self.assertRaises(OrtFailure):
					list(engine.stream("test", speed=1, steps=4))
			finally:
				engine.close()

	def test_switches_preset_style_without_reloading_model(self):
		voice = self.root / "bluev3_onnx" / "voices" / "adam.json"
		voice.write_text("{}", encoding="utf-8")
		model = FakeModel(["CPUExecutionProvider"])
		with self.fake_imports(model):
			engine = self.engine_module.BlueEngine(self.root, use_cuda=False, model_resources=self.root)
			list(engine.stream("one", speed=1, steps=4, voice="adam"))
			list(engine.stream("two", speed=1, steps=4, voice="adam"))
			list(engine.stream("three", speed=1, steps=4, voice="noa"))
			with self.assertRaises(ValueError):
				list(engine.stream("invalid", speed=1, steps=4, voice="../outside"))
			engine.close()
		self.load_style.assert_called_once_with([str(voice)])
		self.assertIs(model.calls[0][-1], self.load_style.return_value)
		self.assertIs(model.calls[1][-1], self.load_style.return_value)
		self.assertIsNone(model.calls[2][-1])
