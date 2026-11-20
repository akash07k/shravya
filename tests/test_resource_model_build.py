import importlib.util
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "tools" / "prepare_resource_model.py"
REQUIRED = (
	"onnx/duration_predictor_style.onnx",
	"onnx/text_encoder.onnx",
	"onnx/vector_estimator.onnx",
	"onnx/vocoder.onnx",
	"onnx/tts.json",
	"onnx/vocab.json",
	"onnx/stats.npz",
	"onnx/uncond.npz",
	"frontend/polyg2p_data/en.lex",
	"frontend/polyg2p_data/en-large.g2p",
	"frontend/polyg2p_data/en.g2p",
	"frontend/polyg2p_data/LICENSE",
	"frontend/polyg2p_data/NOTICE",
	"voices/noa.json",
	"voices/adam.json",
	"LICENSES.md",
	"LICENSE.OpenRAIL-M",
)


class TestModelPreparation(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.bundle = self.root / "custom-bundle"
		self.output = self.root / "build" / "bluev3_onnx"
		for name in REQUIRED:
			path = self.bundle / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"fixture")
		spec = importlib.util.spec_from_file_location("prepare_model_test", PATH)
		self.builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.builder)

	def test_missing_int8_is_generated_and_matching_files_are_copied(self):
		(self.bundle / "frontend" / "polyg2p_data" / "de.lex").write_bytes(b"unused")
		calls = []

		def quantize(source, output):
			calls.append((source.read_bytes(), output.name))
			output.write_bytes(b"quantized")

		self.builder.prepare_model(self.bundle, self.output, quantize)
		self.assertEqual(calls, [(b"fixture", "vector_estimator.int8.onnx")])
		for name in REQUIRED:
			self.assertEqual((self.output / name).read_bytes(), b"fixture")
		self.assertEqual((self.output / "onnx" / "vector_estimator.int8.onnx").read_bytes(), b"quantized")
		self.assertFalse((self.output / "frontend" / "polyg2p_data" / "de.lex").exists())
		self.assertFalse((self.bundle / "onnx" / "vector_estimator.int8.onnx").exists())

	def test_supplied_int8_is_preserved_without_quantizing(self):
		(self.bundle / "onnx" / "vector_estimator.int8.onnx").write_bytes(b"existing")

		def quantize(source, output):
			self.fail("The supplied int8 graph should be preserved")

		self.builder.prepare_model(self.bundle, self.output, quantize)
		self.assertEqual((self.output / "onnx" / "vector_estimator.int8.onnx").read_bytes(), b"existing")

	def test_incomplete_or_failed_build_does_not_create_destination(self):
		(self.bundle / "voices" / "noa.json").unlink()
		with self.assertRaises(FileNotFoundError):
			self.builder.prepare_model(self.bundle, self.output, lambda source, output: None)
		self.assertFalse(self.output.exists())
		(self.bundle / "voices" / "noa.json").write_bytes(b"fixture")

		def fail(source, output):
			output.write_bytes(b"incomplete")
			raise RuntimeError("quantization failed")

		with self.assertRaisesRegex(RuntimeError, "quantization failed"):
			self.builder.prepare_model(self.bundle, self.output, fail)
		self.assertFalse(self.output.exists())

	def test_existing_destination_is_not_replaced(self):
		self.output.mkdir(parents=True)
		(self.output / "keep").write_text("existing", encoding="utf-8")
		with self.assertRaises(FileExistsError):
			self.builder.prepare_model(self.bundle, self.output, lambda source, output: None)
		self.assertEqual((self.output / "keep").read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
	unittest.main()
