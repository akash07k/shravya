import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_install.py"
BUILDER_PATH = Path(__file__).resolve().parents[1] / "tools" / "build_resource_pack.py"
FILES = (
	"python/blue_onnx/__init__.py",
	"python/blue_onnx/cpu.py",
	"python/blue_onnx/text_norm.py",
	"python/numpy/__init__.py",
	"python/num2words/__init__.py",
	"python/polyg2p/__init__.py",
	"python/polyg2p/_polyg2p.pyd",
	"python/onnxruntime/__init__.py",
	"python/onnxruntime/capi/onnxruntime_pybind11_state.pyd",
	"python/onnxruntime/capi/onnxruntime_providers_cuda.dll",
	"python/nvidia/cu13/bin/x86_64/cublas64_13.dll",
	"python/nvidia/cu13/bin/x86_64/cublasLt64_13.dll",
	"python/nvidia/cudnn/bin/cudnn64_9.dll",
	"bluev3_onnx/frontend/polyg2p_data/en.lex",
	"bluev3_onnx/frontend/polyg2p_data/en-large.g2p",
	"bluev3_onnx/frontend/extra.dat",
	"bluev3_onnx/onnx/duration_predictor_style.onnx",
	"bluev3_onnx/onnx/text_encoder.onnx",
	"bluev3_onnx/onnx/vector_estimator.onnx",
	"bluev3_onnx/onnx/vector_estimator.int8.onnx",
	"bluev3_onnx/onnx/vocoder.onnx",
	"bluev3_onnx/onnx/tts.json",
	"bluev3_onnx/onnx/vocab.json",
	"bluev3_onnx/onnx/stats.npz",
	"bluev3_onnx/onnx/uncond.npz",
	"bluev3_onnx/voices/noa.json",
)


def write_pack(path, *, omit=None, version="model-v1", kind="model"):
	prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
	files = [name for name in FILES if name.startswith(prefix)]
	with zipfile.ZipFile(path, "w") as archive:
		archive.writestr("pack-kind.txt", kind)
		archive.writestr("pack-version.txt", version)
		archive.writestr("pack-files.json", json.dumps({name: len(b"example") for name in files}))
		for name in files:
			if name != omit:
				archive.writestr(name, b"example")


def load_install():
	spec = importlib.util.spec_from_file_location("shravya_install_test", PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestResourceInstall(unittest.TestCase):
	def setUp(self):
		self.install = load_install()
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.archive = self.root / "download.zip"
		self.destination = self.root / "userConfig" / "shravya" / "model-v1"

	def create_archive(self, *, omit=None, version="model-v1"):
		write_pack(self.archive, omit=omit, version=version)

	def test_complete_pack_activates_outside_addon(self):
		self.create_archive()
		self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertEqual(
			(self.destination / "bluev3_onnx" / "onnx" / "vocoder.onnx").read_bytes(),
			b"example",
		)
		self.assertTrue(self.archive.is_file())

	@unittest.skipUnless(os.name == "nt", "Windows extended paths")
	def test_install_and_revalidate_under_long_windows_path(self):
		self.create_archive()
		destination = self.root / ("a" * 100) / ("b" * 70) / "model-v1"
		extract = zipfile.ZipFile.extract

		def limited_extract(pack, member, path=None, pwd=None):
			full_path = Path(path) / member.filename
			if len(str(full_path)) >= 260 and not str(full_path).startswith("\\\\?\\"):
				raise OSError(206, "The filename or extension is too long")
			return extract(pack, member, path, pwd)

		def cancel(done, total):
			if done == total:
				raise RuntimeError("cancelled")

		with patch.object(zipfile.ZipFile, "extract", limited_extract):
			with self.assertRaisesRegex(RuntimeError, "cancelled"):
				self.install.install(self.archive, destination, cancel, kind="model")
			stage = destination.with_name(".model-v1.installing")
			self.assertFalse(Path("\\\\?\\" + str(stage)).exists())
			self.install.install(self.archive, destination, lambda done, total: None, kind="model")
		self.install.validate_pack(destination, destination.name, kind="model", installed=True)
		extended = Path("\\\\?\\" + str(destination))
		self.assertTrue((extended / "bluev3_onnx" / "onnx" / "vocoder.onnx").is_file())

	def test_component_packs_contain_only_the_selected_resources(self):
		source = self.root / "inputs"
		for name in FILES:
			path = source / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_component_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		for kind, directory in (("runtime", "python"), ("model", "bluev3_onnx")):
			with self.subTest(kind=kind):
				archive = self.root / f"{kind}.zip"
				builder.build_component(source, archive, kind, "v1")
				with zipfile.ZipFile(archive) as pack:
					self.assertEqual(pack.read("pack-kind.txt"), kind.encode())
					self.assertEqual(pack.read("pack-version.txt"), b"v1")
					inventory = json.loads(pack.read("pack-files.json"))
					expected = {name: 7 for name in FILES if name.startswith(f"{directory}/")}
					self.assertEqual(inventory, expected)
					self.assertEqual(
						set(pack.namelist()),
						set(expected) | {"pack-kind.txt", "pack-version.txt", "pack-files.json"},
					)
				with self.assertRaises(FileExistsError):
					builder.build_component(source, archive, kind, "v1")

	def test_archive_builder_cli_requires_kind_and_version(self):
		source = self.root / "staged"
		(source / "python").mkdir(parents=True)
		(source / "python" / "example").write_bytes(b"example")
		command = [sys.executable, "-B", str(BUILDER_PATH), str(source), str(self.archive)]
		result = subprocess.run(command, capture_output=True, text=True)
		self.assertNotEqual(result.returncode, 0)
		self.assertFalse(self.archive.exists())
		result = subprocess.run(command + ["--kind", "runtime", "--version", "runtime-local"], capture_output=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		with zipfile.ZipFile(self.archive) as pack:
			self.assertEqual(pack.read("pack-kind.txt"), b"runtime")
			self.assertEqual(pack.read("pack-version.txt"), b"runtime-local")

	def test_component_pack_rejects_invalid_or_missing_inputs(self):
		source = self.root / "inputs"
		(source / "python").mkdir(parents=True)
		(source / "python" / "example").write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_component_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		for kind, version in (("other", "v1"), ("runtime", "../bad"), ("runtime", "")):
			with self.subTest(kind=kind, version=version), self.assertRaises(ValueError):
				builder.build_component(source, self.archive, kind, version)
		with self.assertRaises(FileNotFoundError):
			builder.build_component(source, self.archive, "model", "v1")
		self.assertFalse(self.archive.exists())

	def test_failed_archive_write_does_not_leave_a_partial_output(self):
		source = self.root / "inputs"
		(source / "python").mkdir(parents=True)
		(source / "python" / "example").write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_component_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		with patch.object(builder.zipfile.ZipFile, "write", side_effect=OSError("disk full")):
			with self.assertRaisesRegex(OSError, "disk full"):
				builder.build_component(source, self.archive, "runtime", "v1")
		self.assertFalse(self.archive.exists())

	def test_built_component_packs_install_separately(self):
		source = self.root / "inputs"
		for name in FILES:
			path = source / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_component_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		for kind, directory in (("runtime", "python"), ("model", "bluev3_onnx")):
			with self.subTest(kind=kind):
				archive = self.root / f"{kind}.zip"
				destination = self.root / "packs" / kind / f"{kind}-v1"
				builder.build_component(source, archive, kind, destination.name)
				self.install.install(archive, destination, lambda done, total: None, kind=kind)
				self.assertTrue((destination / directory).is_dir())
				self.assertFalse((destination / ("bluev3_onnx" if kind == "runtime" else "python")).exists())

	def test_component_install_rejects_wrong_kind_and_missing_files(self):
		for kind, archive_kind, omit in (
			("runtime", "model", None),
			("runtime", "runtime", "python/onnxruntime/capi/onnxruntime_providers_cuda.dll"),
			("model", "model", "bluev3_onnx/onnx/vector_estimator.int8.onnx"),
		):
			with self.subTest(kind=kind, archive_kind=archive_kind, omit=omit):
				destination = self.root / "packs" / kind / f"{kind}-v1"
				prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
				files = [name for name in FILES if name.startswith(prefix) and name != omit]
				with zipfile.ZipFile(self.archive, "w") as archive:
					archive.writestr("pack-kind.txt", archive_kind)
					archive.writestr("pack-version.txt", destination.name)
					archive.writestr("pack-files.json", json.dumps({name: 7 for name in files}))
					for name in files:
						archive.writestr(name, b"example")
				with self.assertRaises(ValueError):
					self.install.install(self.archive, destination, lambda done, total: None, kind=kind)
				self.assertFalse(destination.exists())

	def test_installed_component_can_be_revalidated_before_activation(self):
		source = self.root / "inputs"
		for name in FILES:
			path = source / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_validation_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		destination = self.root / "packs" / "model" / "model-v1"
		builder.build_component(source, self.archive, "model", destination.name)
		self.install.install(self.archive, destination, lambda done, total: None, kind="model")
		self.install.validate_pack(destination, destination.name, kind="model")
		(destination / "bluev3_onnx" / "onnx" / "vocoder.onnx").unlink()
		with self.assertRaises(ValueError):
			self.install.validate_pack(destination, destination.name, kind="model")

	def test_installed_runtime_allows_generated_bytecode_but_not_other_extra_files(self):
		source = self.root / "inputs"
		for name in FILES:
			path = source / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"example")
		spec = importlib.util.spec_from_file_location("shravya_bytecode_builder_test", BUILDER_PATH)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		destination = self.root / "packs" / "runtime" / "runtime-v1"
		builder.build_component(source, self.archive, "runtime", destination.name)
		self.install.install(self.archive, destination, lambda done, total: None, kind="runtime")
		cache = destination / "python" / "blue_onnx" / "__pycache__"
		cache.mkdir()
		(cache / "__init__.cpython-313.pyc").write_bytes(b"generated")
		self.install.validate_pack(destination, destination.name, kind="runtime", installed=True)
		(cache / "unknown.cpython-313.pyc").write_bytes(b"unexpected")
		with self.assertRaises(ValueError):
			self.install.validate_pack(destination, destination.name, kind="runtime", installed=True)

	def test_incomplete_or_wrong_version_never_activates(self):
		for omit, version in (("bluev3_onnx/onnx/vocoder.onnx", "model-v1"), (None, "model-v2")):
			with self.subTest(omit=omit, version=version):
				self.create_archive(omit=omit, version=version)
				with self.assertRaises(ValueError):
					self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
				self.assertFalse(self.destination.exists())
				self.assertTrue(self.archive.is_file())

	def test_missing_inventory_file_never_activates(self):
		self.create_archive(omit="bluev3_onnx/frontend/extra.dat")
		with self.assertRaises(ValueError):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse(self.destination.exists())

	def test_missing_inventory_never_activates(self):
		with zipfile.ZipFile(self.archive, "w") as archive:
			archive.writestr("pack-kind.txt", "model")
			archive.writestr("pack-version.txt", "model-v1")
			for name in (name for name in FILES if name.startswith("bluev3_onnx/")):
				archive.writestr(name, b"example")
		with self.assertRaises(ValueError):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse(self.destination.exists())

	def test_missing_kind_never_activates(self):
		files = [name for name in FILES if name.startswith("bluev3_onnx/")]
		with zipfile.ZipFile(self.archive, "w") as archive:
			archive.writestr("pack-version.txt", "model-v1")
			archive.writestr("pack-files.json", json.dumps({name: 7 for name in files}))
			for name in files:
				archive.writestr(name, b"example")
		with self.assertRaisesRegex(ValueError, "kind"):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse(self.destination.exists())
		self.assertTrue(self.archive.is_file())

	def test_inventory_size_mismatch_never_activates(self):
		inventory = {name: len(b"example") for name in FILES if name.startswith("bluev3_onnx/")}
		inventory["bluev3_onnx/onnx/vocoder.onnx"] += 1
		with zipfile.ZipFile(self.archive, "w") as archive:
			archive.writestr("pack-kind.txt", "model")
			archive.writestr("pack-version.txt", "model-v1")
			archive.writestr("pack-files.json", json.dumps(inventory))
			for name in inventory:
				archive.writestr(name, b"example")
		with self.assertRaises(ValueError):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse(self.destination.exists())

	def test_extraction_failure_leaves_no_active_pack_and_keeps_archive(self):
		self.create_archive()

		def fail(done, total):
			if done:
				raise OSError("disk failure")

		with self.assertRaises(OSError):
			self.install.install(self.archive, self.destination, fail, kind="model")
		self.assertFalse(self.destination.exists())
		self.assertTrue(self.archive.is_file())
		self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertTrue(self.destination.is_dir())

	def test_corrupt_archive_never_activates(self):
		self.create_archive()
		data = self.archive.read_bytes().replace(b"example", b"damaged", 1)
		self.archive.write_bytes(data)
		with self.assertRaises(zipfile.BadZipFile):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse(self.destination.exists())
		self.assertTrue(self.archive.is_file())

	def test_retry_cleans_up_an_interrupted_staging_directory(self):
		self.create_archive()
		stage = self.destination.with_name(".model-v1.installing")
		stage.mkdir(parents=True)
		(stage / "incomplete").write_bytes(b"partial")
		self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertFalse((self.destination / "incomplete").exists())
		self.assertFalse(stage.exists())

	def test_existing_pack_is_never_replaced(self):
		self.create_archive()
		self.destination.mkdir(parents=True)
		marker = self.destination / "keep"
		marker.write_text("existing", encoding="utf-8")
		with self.assertRaises(FileExistsError):
			self.install.install(self.archive, self.destination, lambda done, total: None, kind="model")
		self.assertEqual(marker.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
	unittest.main()
