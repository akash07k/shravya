import builtins
import http.server
import importlib
import json
import struct
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_resource_install import FILES, write_pack

SOURCE = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def load_setup():
	package = types.ModuleType("shravya_setup_test")
	package.__path__ = [str(SOURCE)]
	sys.modules[package.__name__] = package
	return importlib.import_module("shravya_setup_test._setup")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
	def log_message(self, format, *args):
		pass


class TestResourceSetup(unittest.TestCase):
	def setUp(self):
		self.setup = load_setup()
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		published = self.root / "served"
		published.mkdir()
		self.archive = published / "pack.zip"
		write_pack(self.archive, version="model-1", kind="runtime")
		handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(published), **kwargs)
		self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
		self.thread = threading.Thread(target=self.server.serve_forever)
		self.thread.start()
		self.addCleanup(self.close_server)
		self.url = f"http://127.0.0.1:{self.server.server_port}/pack.zip"
		self.config = self.root / "userConfig"

	def close_server(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def component_archive(self, name, kind, version, data=b"example"):
		path = self.archive.parent / name
		prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
		files = [name for name in FILES if name.startswith(prefix)]
		with zipfile.ZipFile(path, "w") as archive:
			archive.writestr("pack-kind.txt", kind)
			archive.writestr("pack-version.txt", version)
			archive.writestr("pack-files.json", json.dumps({name: len(data) for name in files}))
			for filename in files:
				archive.writestr(filename, data)
		return f"http://127.0.0.1:{self.server.server_port}/{name}"

	def test_setup_imports_without_filecmp(self):
		package = types.ModuleType("shravya_no_filecmp_test")
		package.__path__ = [str(SOURCE)]
		original_import = builtins.__import__

		def import_without_filecmp(name, *args, **kwargs):
			if name == "filecmp":
				raise ModuleNotFoundError("No module named 'filecmp'")
			return original_import(name, *args, **kwargs)

		with (
			patch.dict(sys.modules, {package.__name__: package}),
			patch("builtins.__import__", side_effect=import_without_filecmp),
		):
			importlib.import_module(f"{package.__name__}._setup")

	def test_local_file_comparison_checks_full_contents_and_length(self):
		first, second = self.root / "first", self.root / "second"
		content = b"a" * (1024 * 1024) + b"last"
		first.write_bytes(content)
		second.write_bytes(content)
		self.assertTrue(self.setup._same_file(first, second))
		second.write_bytes(content[:-1] + b"x")
		self.assertFalse(self.setup._same_file(first, second))
		second.write_bytes(content + b"x")
		self.assertFalse(self.setup._same_file(first, second))
		second.unlink()
		with self.assertRaises(FileNotFoundError):
			self.setup._same_file(first, second)

	def test_downloads_and_reuses_separate_components(self):
		for kind in ("runtime", "model"):
			version = f"{kind}-1"
			url = self.component_archive(f"{kind}.zip", kind, version)
			stages = []
			self.setup.prepare_component(
				url, self.config, kind, version, lambda stage, done, total: stages.append(stage),
			)
			self.assertEqual(set(stages), {"download", "extract"})
			with patch.object(self.setup, "download", side_effect=AssertionError("unexpected download")):
				self.setup.prepare_component(url, self.config, kind, version, lambda *args: None)
			self.assertTrue((self.config / "shravya" / "packs" / kind / version).is_dir())

	def test_local_setup_and_runtime_replacement_waits_for_restart(self):
		resources = importlib.import_module("shravya_setup_test._resources")
		for kind in ("runtime", "model"):
			self.component_archive(f"{kind}.zip", kind, f"{kind}-1")
		runtime = self.archive.parent / "runtime.zip"
		model = self.archive.parent / "model.zip"
		with patch.object(self.setup, "download", side_effect=AssertionError("unexpected network")):
			self.assertFalse(self.setup.prepare_local(runtime, model, self.config, lambda *args: None))
			self.component_archive("runtime2.zip", "runtime", "runtime-2")
			new = self.archive.parent / "runtime2.zip"
			self.assertTrue(self.setup.prepare_local(new, model, self.config, lambda *args: None))
		self.assertEqual(resources.resource_selection(self.config), ("runtime-1", "model-1"))
		self.assertEqual(resources.resource_directories(self.config)[0].name, "runtime-1")
		with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
			self.assertEqual(resources.resource_directories(self.config)[0].name, "runtime-2")
		self.assertTrue(runtime.is_file() and model.is_file() and new.is_file())

	def test_local_failure_and_cancellation_preserve_selection_and_archives(self):
		resources = importlib.import_module("shravya_setup_test._resources")
		for kind in ("runtime", "model"):
			self.component_archive(f"{kind}.zip", kind, f"{kind}-1")
		runtime = self.archive.parent / "runtime.zip"
		model = self.archive.parent / "model.zip"
		self.setup.prepare_local(runtime, model, self.config, lambda *args: None)
		self.component_archive("runtime2.zip", "runtime", "runtime-2")
		new = self.archive.parent / "runtime2.zip"

		def cancel(stage, done, total):
			raise RuntimeError("cancelled")

		with self.assertRaisesRegex(RuntimeError, "cancelled"):
			self.setup.prepare_local(new, model, self.config, cancel)
		with self.assertRaises(ValueError):
			self.setup.prepare_local(model, runtime, self.config, lambda *args: None)
		self.component_archive("different.zip", "model", "model-1", b"changed")
		with self.assertRaisesRegex(ValueError, "differs"):
			self.setup.prepare_local(runtime, self.archive.parent / "different.zip", self.config, lambda *args: None)
		self.assertEqual(resources.resource_selection(self.config), ("runtime-1", "model-1"))
		self.assertTrue(runtime.is_file() and model.is_file() and new.is_file())

	def test_failed_restart_promotion_keeps_the_previous_pair(self):
		resources = importlib.import_module("shravya_setup_test._resources")
		for kind in ("runtime", "model"):
			self.component_archive(f"{kind}.zip", kind, f"{kind}-1")
		runtime = self.archive.parent / "runtime.zip"
		model = self.archive.parent / "model.zip"
		self.setup.prepare_local(runtime, model, self.config, lambda *args: None)
		self.component_archive("runtime2.zip", "runtime", "runtime-2")
		self.setup.prepare_local(self.archive.parent / "runtime2.zip", model, self.config, lambda *args: None)
		(self.config / "shravya" / "packs" / "runtime" / "runtime-2" / "python" / "blue_onnx" / "cpu.py").unlink()
		with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
			with self.assertLogs("shravya_setup_test._resources", level="ERROR"):
				self.assertEqual(resources.resource_directories(self.config)[0].name, "runtime-1")
		self.assertEqual(resources.resource_selection(self.config), ("runtime-1", "model-1"))
		with self.assertRaisesRegex(RuntimeError, "pending"):
			self.setup.prepare_local(runtime, model, self.config, lambda *args: None)

	def test_retry_reuses_archive_only_for_the_same_source(self):
		url = self.component_archive("model.zip", "model", "model-1")

		def fail(stage, done, total):
			if stage == "extract" and done:
				raise OSError("disk failure")

		with self.assertRaises(OSError):
			self.setup.prepare_component(url, self.config, "model", "model-1", fail)
		with patch.object(self.setup, "download", side_effect=AssertionError("unexpected download")):
			self.setup.prepare_component(url, self.config, "model", "model-1", lambda *args: None)

		url_a = self.component_archive("model-a.zip", "model", "model-2", b"first")
		url_b = self.component_archive("model-b.zip", "model", "model-2", b"second")
		with self.assertRaises(OSError):
			self.setup.prepare_component(url_a, self.config, "model", "model-2", fail)
		self.setup.prepare_component(url_b, self.config, "model", "model-2", lambda *args: None)
		model = self.config / "shravya" / "packs" / "model" / "model-2"
		self.assertEqual((model / "bluev3_onnx" / "voices" / "noa.json").read_bytes(), b"second")

	def test_corrupt_deflate_is_discarded_and_retry_downloads_repaired_component(self):
		published = self.archive.parent / "model.zip"
		with zipfile.ZipFile(published, "w", compression=zipfile.ZIP_DEFLATED) as archive:
			archive.writestr("bad", b"compressed content" * 100)
		data = bytearray(published.read_bytes())
		name_length, extra_length = struct.unpack_from("<HH", data, 26)
		data[30 + name_length + extra_length] = 0x06
		published.write_bytes(data)
		url = f"http://127.0.0.1:{self.server.server_port}/model.zip"
		cache = self.config / "shravya" / "downloads" / "model"
		with self.assertRaises(self.setup.InvalidPackError):
			self.setup.prepare_component(url, self.config, "model", "model-1", lambda *args: None)
		self.assertFalse((cache / "model-1.zip").exists())
		self.assertFalse((cache / "model-1.source").exists())
		self.component_archive("model.zip", "model", "model-1")
		self.setup.prepare_component(url, self.config, "model", "model-1", lambda *args: None)
		self.assertTrue((self.config / "shravya" / "packs" / "model" / "model-1").is_dir())

	def test_existing_incomplete_component_is_not_overwritten(self):
		pack = self.config / "shravya" / "packs" / "model" / "model-1"
		pack.mkdir(parents=True)
		(pack / "keep").write_text("keep", encoding="utf-8")
		with patch.object(self.setup, "download", side_effect=AssertionError("unexpected download")):
			with self.assertRaises(ValueError):
				self.setup.prepare_component(self.url, self.config, "model", "model-1", lambda *args: None)
		self.assertEqual((pack / "keep").read_text(encoding="utf-8"), "keep")

	def test_split_setup_checks_compatibility_and_activates_both_packs(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		model_url = self.component_archive("model.zip", "model", "model-1")
		catalog = self.archive.parent / "resources.toml"
		catalog.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
			f'[model]\nid = "model-1"\nurl = "{model_url}"\n',
			encoding="utf-8",
		)
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		with self.assertRaises(ValueError):
			self.setup.prepare_split(url, self.config, "2.0.0", lambda *args: None)
		self.assertFalse((self.config / "shravya").exists())
		self.setup.prepare_split(url, self.config, "1.0.0", lambda *args: None)
		resources = importlib.import_module("shravya_setup_test._resources")
		self.assertEqual(
			resources.resource_directories(self.config),
			(
				self.config / "shravya" / "packs" / "runtime" / "runtime-1",
				self.config / "shravya" / "packs" / "model" / "model-1",
			),
		)

	def test_split_setup_does_not_activate_when_a_component_fails(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		catalog = self.archive.parent / "resources.toml"
		catalog.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
			f'[model]\nid = "model-1"\nurl = "{self.url}"\n',
			encoding="utf-8",
		)
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		with self.assertRaises(ValueError):
			self.setup.prepare_split(url, self.config, "1.0.0", lambda *args: None)
		self.assertFalse((self.config / "shravya" / "active-resources.json").exists())

	def test_split_setup_checks_cancellation_before_activating_prepared_packs(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		model_url = self.component_archive("model.zip", "model", "model-1")
		catalog = self.archive.parent / "resources.toml"
		catalog.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
			f'[model]\nid = "model-1"\nurl = "{model_url}"\n',
			encoding="utf-8",
		)
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		self.setup.prepare_component(runtime_url, self.config, "runtime", "runtime-1", lambda *args: None)
		self.setup.prepare_component(model_url, self.config, "model", "model-1", lambda *args: None)

		def cancel(*args):
			raise RuntimeError("cancelled")

		with self.assertRaisesRegex(RuntimeError, "cancelled"):
			self.setup.prepare_split(url, self.config, "1.0.0", cancel)
		self.assertFalse((self.config / "shravya" / "active-resources.json").exists())

	def test_model_update_downloads_only_model_and_preserves_selection_until_ready(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		old_url = self.component_archive("old.zip", "model", "model-1")
		new_url = self.component_archive("new.zip", "model", "model-2", b"updated")
		catalog = self.archive.parent / "resources.toml"
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"

		def write_catalog(model_id, model_url):
			catalog.write_text(
				'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
				f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
				f'[model]\nid = "{model_id}"\nurl = "{model_url}"\n',
				encoding="utf-8",
			)

		write_catalog("model-1", old_url)
		self.setup.prepare_split(url, self.config, "1.0.0", lambda *args: None)
		self.assertIsNone(self.setup.check_resource_update(url, self.config, "1.0.0"))
		write_catalog("model-2", new_url)
		update = self.setup.check_resource_update(url, self.config, "1.0.0")
		resources = importlib.import_module("shravya_setup_test._resources")
		before = resources.resource_directories(self.config)

		def cancel(stage, done, total):
			if stage == "extract" and done == total:
				raise RuntimeError("cancelled")

		with self.assertRaisesRegex(RuntimeError, "cancelled"):
			self.setup.prepare_resource_update(update, self.config, cancel)
		self.assertEqual(resources.resource_directories(self.config), before)
		with patch.object(self.setup, "download", side_effect=AssertionError("unexpected download")):
			self.setup.prepare_resource_update(update, self.config, lambda *args: None)
		runtime, model = resources.resource_directories(self.config)
		self.assertEqual(runtime, before[0])
		self.assertEqual((model / "bluev3_onnx" / "voices" / "noa.json").read_bytes(), b"updated")

	def test_runtime_and_model_update_waits_for_restart(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		model_url = self.component_archive("model.zip", "model", "model-1")
		catalog = self.archive.parent / "resources.toml"
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		content = (
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
			f'[model]\nid = "model-1"\nurl = "{model_url}"\n'
		)
		catalog.write_text(content, encoding="utf-8")
		self.setup.prepare_split(url, self.config, "1.0.0", lambda *args: None)
		new_runtime_url = self.component_archive("runtime2.zip", "runtime", "runtime-2")
		new_model_url = self.component_archive("model2.zip", "model", "model-2")
		catalog.write_text(
			content.replace("runtime-1", "runtime-2").replace(runtime_url, new_runtime_url)
			.replace("model-1", "model-2").replace(model_url, new_model_url), encoding="utf-8",
		)
		update = self.setup.check_resource_update(url, self.config, "1.0.0")
		self.assertEqual(self.setup.prepare_resource_update(update, self.config, lambda *args: None), (True, True))
		resources = importlib.import_module("shravya_setup_test._resources")
		self.assertEqual(resources.resource_selection(self.config), ("runtime-1", "model-1"))
		self.assertEqual(resources.resource_directories(self.config)[0].name, "runtime-1")
		with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
			self.assertEqual(
				tuple(path.name for path in resources.resource_directories(self.config)),
				("runtime-2", "model-2"),
			)

	def test_addon_resources_are_prepared_without_activating_them(self):
		runtime_url = self.component_archive("runtime.zip", "runtime", "runtime-1")
		old_url = self.component_archive("old.zip", "model", "model-1")
		new_url = self.component_archive("new.zip", "model", "model-2")
		catalog = self.archive.parent / "resources.toml"
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		content = (
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{runtime_url}"\n'
			f'[model]\nid = "model-1"\nurl = "{old_url}"\n'
		)
		catalog.write_text(content, encoding="utf-8")
		self.setup.prepare_split(url, self.config, "1.0.0", lambda *args: None)
		resources = importlib.import_module("shravya_setup_test._resources")
		before = resources.resource_directories(self.config)
		catalog.write_text(
			content.replace('"1.0.0"', '"2.0.0"').replace("model-1", "model-2").replace(old_url, new_url),
			encoding="utf-8",
		)
		plan = self.setup.plan_addon_resources(url, self.config, "2.0.0")
		self.assertEqual(plan.missing, ("model",))

		def cancel(stage, done, total):
			if stage == "extract":
				raise RuntimeError("cancelled")

		with self.assertRaisesRegex(RuntimeError, "cancelled"):
			self.setup.prepare_addon_resources(plan, self.config, cancel)
		self.assertEqual(resources.resource_directories(self.config, addon_version="2.0.0"), before)
		self.setup.prepare_addon_resources(plan, self.config, lambda *args: None)
		self.assertEqual(resources.resource_directories(self.config, addon_version="1.0.0"), before)
		self.assertEqual(resources.resource_directories(self.config, addon_version="2.0.0"), before)
		with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
			after = resources.resource_directories(self.config, addon_version="2.0.0")
		self.assertEqual(after[0], before[0])
		self.assertEqual(after[1].name, "model-2")

	def test_addon_update_requires_both_split_packs(self):
		(self.config / "shravya" / "bluev3-v1").mkdir(parents=True)
		catalog = self.archive.parent / "resources.toml"
		catalog.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["2.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{self.url}"\n'
			f'[model]\nid = "model-1"\nurl = "{self.url}"\n',
			encoding="utf-8",
		)
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		plan = self.setup.plan_addon_resources(url, self.config, "2.0.0")
		self.assertEqual((plan.runtime, plan.model), ("runtime-1", "model-1"))
		self.assertEqual(plan.missing, ("runtime", "model"))

	def test_split_setup_rejects_invalid_catalog_before_downloading(self):
		catalog = self.archive.parent / "resources.toml"
		url = f"http://127.0.0.1:{self.server.server_port}/resources.toml"
		valid = (
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			f'[runtime]\nid = "runtime-1"\nurl = "{self.url}"\n'
			f'[model]\nid = "model-1"\nurl = "{self.url}"\n'
		)
		cases = (
			"{",
			valid.replace('interface = "bluev3-v1"', 'interface = "unsupported"'),
			valid.replace('id = "model-1"', 'id = "../escape"'),
			valid.replace(self.url, "file:///local.zip", 1),
			valid.replace("format = 1", "format = true"),
		)
		for content in cases:
			with self.subTest(content=content):
				catalog.write_text(content, encoding="utf-8")
				with patch.object(self.setup, "download", side_effect=AssertionError("unexpected download")):
					with self.assertRaises(ValueError):
						self.setup.prepare_split(
							url, self.config, "1.0.0", lambda *args: None,
						)
		self.assertFalse((self.config / "shravya").exists())


if __name__ == "__main__":
	unittest.main()
