import json
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import build_resource_model, resource_sources
from tools.prepare_resource_model import MODEL_FILES


class TestModelPack(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.bundle = self.root / "custom"
		self.output = self.root / "model.zip"
		for name in MODEL_FILES:
			path = self.bundle / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"fixture")

	def test_custom_bundle_records_source_and_generated_int8(self):
		def quantize(work, source, destination, bluetts):
			destination.write_bytes(b"quantized")

		with (
			patch.object(
				resource_sources, "load_blue_source",
				return_value=resource_sources.BlueSource("example/BlueTTS", "feature"),
			),
			patch.object(resource_sources, "github_revision", return_value="a" * 40) as resolve,
			patch.object(build_resource_model, "quantize_model", side_effect=quantize),
		):
			build_resource_model.build(self.output, self.bundle, "my model revision 42")
		resolve.assert_called_once_with("example/BlueTTS", "feature")
		with zipfile.ZipFile(self.output) as pack:
			self.assertEqual(pack.read("pack-kind.txt"), b"model")
			version = pack.read("pack-version.txt").decode()
			self.assertTrue(version.startswith("model-"))
			self.assertEqual(pack.read("bluev3_onnx/onnx/vector_estimator.int8.onnx"), b"quantized")
			info = json.loads(pack.read("bluev3_onnx/build-inputs.json"))
			self.assertEqual(info["model_source"], "my model revision 42")
			self.assertEqual(info["int8"]["source"], "generated")
			self.assertEqual(info["int8"]["bluetts_repository"], "example/BlueTTS")
			self.assertEqual(info["int8"]["bluetts_revision"], "a" * 40)
			self.assertIn("onnx==", info["int8"]["build_requirements"])

	def test_existing_int8_needs_no_build_environment(self):
		(self.bundle / "onnx" / "vector_estimator.int8.onnx").write_bytes(b"existing")
		with patch.object(build_resource_model, "quantize_model") as quantize:
			build_resource_model.build(self.output, self.bundle, "provided locally")
		quantize.assert_not_called()
		with zipfile.ZipFile(self.output) as pack:
			self.assertEqual(pack.read("bluev3_onnx/onnx/vector_estimator.int8.onnx"), b"existing")
			info = json.loads(pack.read("bluev3_onnx/build-inputs.json"))
			self.assertEqual(info["int8"]["source"], "provided")

	def test_default_model_records_the_resolved_upstream_revision(self):
		def download(destination):
			shutil.copytree(self.bundle, destination)
			return "a" * 40

		def quantize(work, source, destination, bluetts):
			destination.write_bytes(b"quantized")

		with (
			patch.object(build_resource_model.download_resource_model, "download_model", side_effect=download),
			patch.object(
				resource_sources, "resolve_blue_source",
				return_value=resource_sources.GitSource("example/BlueTTS", "b" * 40),
			),
			patch.object(build_resource_model, "quantize_model", side_effect=quantize),
		):
			build_resource_model.build(self.output, None, None)
		with zipfile.ZipFile(self.output) as pack:
			info = json.loads(pack.read("bluev3_onnx/build-inputs.json"))
			self.assertIn("a" * 40, info["model_source"])

	def test_latest_model_saves_reusable_sources_and_preserves_revision(self):
			saved = self.root / "sources"
			saved.mkdir()

			def download_model(destination):
				shutil.copytree(self.bundle, destination)
				return "a" * 40

			def download_blue(work, repository, revision):
				root = work / "blue-source"
				cpu = root / "src" / "blue_onnx" / "cpu.py"
				cpu.parent.mkdir(parents=True)
				cpu.write_bytes(b"quantizer")
				return root

			def quantize(work, source, destination, bluetts):
				self.assertEqual(bluetts, saved / "bluetts")
				destination.write_bytes(b"quantized")

			with (
				patch.object(build_resource_model.download_resource_model, "download_model", side_effect=download_model),
				patch.object(resource_sources, "download_source", side_effect=download_blue) as download,
				patch.object(build_resource_model, "quantize_model", side_effect=quantize),
			):
				build_resource_model.build(
					self.output, None, None,
					blue_source=resource_sources.GitSource("example/BlueTTS", "b" * 40),
					source_output=saved,
				)
				build_resource_model.build(
					self.root / "model-rebuilt.zip", saved / "model", "saved model",
					bluetts_dir=saved / "bluetts", bluetts_description="saved BlueTTS",
				)
			download.assert_called_once()
			self.assertEqual((saved / "model" / MODEL_FILES[0]).read_bytes(), b"fixture")
			self.assertEqual((saved / "bluetts" / "src" / "blue_onnx" / "cpu.py").read_bytes(), b"quantizer")
			with zipfile.ZipFile(self.output) as pack:
				info = json.loads(pack.read("bluev3_onnx/build-inputs.json"))
			self.assertIn("a" * 40, info["model_source"])
			self.assertEqual(info["int8"]["bluetts_revision"], "b" * 40)

	def test_model_reuses_runtime_builds_saved_bluetts(self):
			saved = self.root / "sources"
			cpu = saved / "bluetts" / "src" / "blue_onnx" / "cpu.py"
			cpu.parent.mkdir(parents=True)
			cpu.write_bytes(b"shared")

			def quantize(work, source, destination, bluetts):
				self.assertEqual(bluetts, saved / "bluetts")
				destination.write_bytes(b"quantized")

			with (
				patch.object(build_resource_model.download_resource_model, "download_model",
					side_effect=lambda destination: (shutil.copytree(self.bundle, destination), "a" * 40)[1]),
				patch.object(resource_sources, "download_source", side_effect=AssertionError("unexpected download")),
				patch.object(build_resource_model, "quantize_model", side_effect=quantize),
			):
				build_resource_model.build(
					self.output, None, None,
					blue_source=resource_sources.GitSource("example/BlueTTS", "b" * 40),
					source_output=saved,
				)
			self.assertEqual(cpu.read_bytes(), b"shared")
			self.assertTrue((saved / "model" / MODEL_FILES[0]).is_file())

	def test_local_quantizer_records_description_without_resolving_upstream(self):
		bluetts = self.root / "bluetts"
		bluetts.mkdir()

		def quantize(work, source, destination, source_dir):
			self.assertEqual(source_dir, bluetts)
			destination.write_bytes(b"quantized")

		with (
			patch.object(resource_sources, "resolve_blue_source", side_effect=AssertionError("unexpected network")),
			patch.object(build_resource_model, "quantize_model", side_effect=quantize),
		):
			build_resource_model.build(
				self.output, self.bundle, "local model",
				bluetts_dir=bluetts, bluetts_description="local BlueTTS edits",
			)
		with zipfile.ZipFile(self.output) as pack:
			info = json.loads(pack.read("bluev3_onnx/build-inputs.json"))
		self.assertEqual(info["model_source"], "local model")
		self.assertEqual(info["int8"]["bluetts_source"], "local BlueTTS edits")
		self.assertNotIn("bluetts_revision", info["int8"])

	def test_quantizer_uses_the_selected_fork_revision_or_local_file(self):
		bluetts = self.root / "bluetts"
		cpu = bluetts / "src" / "blue_onnx" / "cpu.py"
		cpu.parent.mkdir(parents=True)
		cpu.write_text("# local source", encoding="utf-8")
		source = self.bundle / "onnx" / "vector_estimator.onnx"
		with (
			patch.object(build_resource_model, "build_environment", return_value=self.root / "python.exe"),
			patch.object(build_resource_model.subprocess, "run") as run,
			patch.object(build_resource_model.urllib.request, "urlopen", return_value=io.BytesIO(b"# remote")) as fetch,
		):
			build_resource_model.quantize_model(self.root, source, self.root / "local.onnx", bluetts)
			fetch.assert_not_called()
			self.assertEqual(run.call_args.args[0][4], str(cpu))
			remote = resource_sources.GitSource("example/BlueTTS", "b" * 40)
			build_resource_model.quantize_model(self.root, source, self.root / "remote.onnx", remote)
			self.assertEqual(
				fetch.call_args.args[0],
				f"https://raw.githubusercontent.com/example/BlueTTS/{'b' * 40}/src/blue_onnx/cpu.py",
			)

	def test_missing_source_or_existing_output_fails_before_build(self):
		with self.assertRaisesRegex(ValueError, "source"):
			build_resource_model.build(self.output, self.bundle, None)
		self.output.write_bytes(b"keep")
		with self.assertRaises(FileExistsError):
			build_resource_model.build(self.output, self.bundle, "custom")
		self.assertEqual(self.output.read_bytes(), b"keep")


if __name__ == "__main__":
	unittest.main()
