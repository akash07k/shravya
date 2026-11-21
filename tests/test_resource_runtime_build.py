import tempfile
import unittest
from pathlib import Path

from tools import prepare_resource_runtime


class TestRuntimePreparation(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.source = self.root / "bluetts"
		self.output = self.root / "resources" / "python"
		(self.source / "src" / "blue_onnx").mkdir(parents=True)
		(self.source / "src" / "blue_onnx" / "__init__.py").write_bytes(b"source")
		(self.source / "src" / "blue_onnx" / "cpu.py").write_bytes(b"source")
		(self.source / "LICENSE").write_bytes(b"license")

	def install_dependencies(self, destination):
		for name in prepare_resource_runtime.RUNTIME_FILES:
			if name.startswith("blue_onnx/"):
				continue
			path = destination / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"runtime")

	def test_runtime_contains_bluetts_and_cpu_and_cuda_dependencies(self):
		info = {"bluetts_revision": "source-revision", "polyg2p_revision": "phonemizer-revision"}
		prepare_resource_runtime.prepare_runtime(self.source, self.output, self.install_dependencies, info)
		self.assertEqual((self.output / "blue_onnx" / "cpu.py").read_bytes(), b"source")
		self.assertEqual((self.output / "blue_onnx" / "LICENSE").read_bytes(), b"license")
		self.assertTrue((self.output / "onnxruntime" / "capi" / "onnxruntime_providers_cuda.dll").is_file())
		self.assertIn("source-revision", (self.output / "build-inputs.json").read_text(encoding="utf-8"))

	def test_failed_or_incomplete_preparation_does_not_replace_destination(self):
		def incomplete(destination):
			(destination / "partial").write_bytes(b"partial")

		with self.assertRaises(FileNotFoundError):
			prepare_resource_runtime.prepare_runtime(self.source, self.output, incomplete, {})
		self.assertFalse(self.output.exists())
		self.output.mkdir(parents=True)
		(self.output / "keep").write_bytes(b"existing")
		with self.assertRaises(FileExistsError):
			prepare_resource_runtime.prepare_runtime(self.source, self.output, self.install_dependencies, {})
		self.assertEqual((self.output / "keep").read_bytes(), b"existing")


if __name__ == "__main__":
	unittest.main()
