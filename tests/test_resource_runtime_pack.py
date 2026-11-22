import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import build_resource_runtime, prepare_resource_runtime, resource_sources


class TestRuntimePack(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.output = self.root / "runtime.zip"
		self.windows_python = SimpleNamespace(platform="win32", version_info=(3, 13), version=sys.version)

	def test_runtime_pack_records_inputs_and_excludes_model(self):
		def source(work, repository, revision):
			root = work / repository.split("/")[-1]
			root.mkdir()
			if repository.endswith("/BlueTTS"):
				(root / "src" / "blue_onnx").mkdir(parents=True)
				for name in ("__init__.py", "cpu.py"):
					(root / "src" / "blue_onnx" / name).write_bytes(b"source")
				(root / "LICENSE").write_bytes(b"license")
			else:
				(root / "Cargo.toml").write_bytes(b"source")
			return root

		def install(python, wheel, destination):
			for name in prepare_resource_runtime.RUNTIME_FILES:
				if not name.startswith("blue_onnx/"):
					path = destination / name
					path.parent.mkdir(parents=True, exist_ok=True)
					path.write_bytes(b"runtime")

		def compile_polyg2p(work, source, python):
			(source / "target").mkdir()
			return self.root / "wheel", "MSVC"

		saved = self.root / "sources"
		saved.mkdir()
		with (
			patch.object(build_resource_runtime, "sys", self.windows_python),
			patch.object(
				resource_sources, "load_blue_source",
				return_value=resource_sources.BlueSource("example/BlueTTS", "feature"),
			),
			patch.object(
				resource_sources, "github_revision", side_effect=["a" * 40, "b" * 40],
			) as resolve,
			patch.object(resource_sources, "download_source", side_effect=source),
			patch.object(build_resource_runtime, "build_environment", return_value=self.root / "python.exe"),
			patch.object(build_resource_runtime, "build_polyg2p", side_effect=compile_polyg2p),
			patch.object(build_resource_runtime, "install_runtime", side_effect=install),
			patch.object(build_resource_runtime, "tool_version", side_effect=lambda command: command + " version"),
		):
			build_resource_runtime.build(self.output, source_output=saved)
			self.assertFalse((saved / "polyg2p" / "target").exists())
			build_resource_runtime.build(
				self.root / "runtime-rebuilt.zip",
				bluetts_dir=saved / "bluetts", bluetts_description="saved BlueTTS",
				polyg2p_dir=saved / "polyg2p", polyg2p_description="saved polyg2p",
			)
		self.assertEqual((saved / "bluetts" / "src" / "blue_onnx" / "cpu.py").read_bytes(), b"source")
		self.assertEqual((saved / "polyg2p" / "Cargo.toml").read_bytes(), b"source")
		self.assertEqual(
			resolve.call_args_list,
			[unittest.mock.call("example/BlueTTS", "feature"), unittest.mock.call("maxmelichov/polyg2p")],
		)
		with zipfile.ZipFile(self.output) as pack:
			self.assertEqual(pack.read("pack-kind.txt"), b"runtime")
			self.assertTrue(all(not name.startswith("bluev3_onnx/") for name in pack.namelist()))
			info = json.loads(pack.read("python/build-inputs.json"))
			self.assertEqual(info["bluetts_repository"], "example/BlueTTS")
			self.assertEqual(info["bluetts_revision"], "a" * 40)
			self.assertEqual(info["polyg2p_repository"], "maxmelichov/polyg2p")
			self.assertEqual(info["polyg2p_revision"], "b" * 40)
			self.assertIn("onnxruntime-gpu==", info["runtime_requirements"])
			self.assertEqual(info["rustc"], "rustc version")

	def test_local_sources_record_descriptions_without_resolving_upstream(self):
		bluetts = self.root / "bluetts"
		(bluetts / "src" / "blue_onnx").mkdir(parents=True)
		for name in ("__init__.py", "cpu.py"):
			(bluetts / "src" / "blue_onnx" / name).write_bytes(b"source")
		(bluetts / "LICENSE").write_bytes(b"license")
		polyg2p = self.root / "polyg2p"
		polyg2p.mkdir()

		def install(python, wheel, destination):
			for name in prepare_resource_runtime.RUNTIME_FILES:
				if not name.startswith("blue_onnx/"):
					path = destination / name
					path.parent.mkdir(parents=True, exist_ok=True)
					path.write_bytes(b"runtime")

		with (
			patch.object(build_resource_runtime, "sys", self.windows_python),
			patch.object(resource_sources, "github_revision", side_effect=AssertionError("unexpected network")),
			patch.object(build_resource_runtime, "build_environment", return_value=self.root / "python.exe"),
			patch.object(build_resource_runtime, "build_polyg2p", return_value=(self.root / "wheel", "MSVC")),
			patch.object(build_resource_runtime, "install_runtime", side_effect=install),
			patch.object(build_resource_runtime, "tool_version", return_value="tool version"),
		):
			build_resource_runtime.build(
				self.output,
				bluetts_dir=bluetts, bluetts_description="local BlueTTS edits",
				polyg2p_dir=polyg2p, polyg2p_description="local polyg2p edits",
			)
		with zipfile.ZipFile(self.output) as pack:
			info = json.loads(pack.read("python/build-inputs.json"))
		self.assertEqual(info["bluetts_source"], "local BlueTTS edits")
		self.assertEqual(info["polyg2p_source"], "local polyg2p edits")
		self.assertNotIn("bluetts_revision", info)
		self.assertNotIn("polyg2p_revision", info)

	def test_runtime_build_rejects_unsupported_platform_or_python(self):
		for platform, version in (("linux", (3, 13)), ("win32", (3, 12))):
			with self.subTest(platform=platform, version=version):
				fake_sys = SimpleNamespace(platform=platform, version_info=version)
				with patch.object(build_resource_runtime, "sys", fake_sys):
					with self.assertRaisesRegex(RuntimeError, "requires Windows and Python 3.13"):
						build_resource_runtime.build(self.output)
				self.assertFalse(self.output.exists())

	def test_existing_archive_is_preserved(self):
		self.output.write_bytes(b"existing")
		with self.assertRaises(FileExistsError):
			build_resource_runtime.build(self.output)
		self.assertEqual(self.output.read_bytes(), b"existing")

	def test_source_archive_cannot_escape_its_root(self):
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w") as archive:
			archive.writestr("BlueTTS-abc/../../outside", b"unexpected")
		buffer.seek(0)
		with patch.object(resource_sources.urllib.request, "urlopen", return_value=buffer):
			with self.assertRaisesRegex(ValueError, "Unexpected source archive path"):
				resource_sources.download_source(self.root, "example/BlueTTS", "abc")
		self.assertFalse((self.root / "outside").exists())

	def test_local_wheel_install_does_not_package_the_temporary_build_path(self):
		destination = self.root / "python"

		def install(command, check):
			info = destination / "polyg2p-0.1.0.dist-info"
			info.mkdir(parents=True)
			(info / "direct_url.json").write_text('{"url": "file:///private/build"}', encoding="utf-8")
			(info / "RECORD").write_text(
				"polyg2p-0.1.0.dist-info/direct_url.json,sha256=placeholder,42\n"
				"polyg2p/__init__.py,,\n",
				encoding="utf-8",
			)

		with patch.object(build_resource_runtime.subprocess, "run", side_effect=install):
			build_resource_runtime.install_runtime(self.root / "python.exe", self.root / "wheel", destination)
		info = destination / "polyg2p-0.1.0.dist-info"
		self.assertFalse((info / "direct_url.json").exists())
		self.assertEqual((info / "RECORD").read_text(encoding="utf-8"), "polyg2p/__init__.py,,\n")


if __name__ == "__main__":
	unittest.main()
