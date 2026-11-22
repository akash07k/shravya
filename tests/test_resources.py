import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_resource_install import FILES

PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_resources.py"


def load_resources():
	package = types.ModuleType("shravya_resources_test")
	package.__path__ = [str(PATH.parent)]
	sys.modules[package.__name__] = package
	return importlib.import_module("shravya_resources_test._resources")


class TestResourceLocation(unittest.TestCase):
	def component(self, root, kind, version):
		pack = root / "shravya" / "packs" / kind / version
		prefix = "python/" if kind == "runtime" else "bluev3_onnx/"
		files = {name: 7 for name in FILES if name.startswith(prefix)}
		for name in files:
			path = pack / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(b"example")
		(pack / "pack-version.txt").write_text(version, encoding="utf-8")
		(pack / "pack-kind.txt").write_text(kind, encoding="utf-8")
		(pack / "pack-files.json").write_text(json.dumps(files), encoding="utf-8")
		return pack

	def test_missing_selection_does_not_use_old_resource_directories(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			config = root / "portable" / "userConfig"
			addon = config / "addons" / "shravya" / "bluetts-resources"
			stored = config / "shravya" / "bluev3-v1"
			addon.mkdir(parents=True)
			stored.mkdir(parents=True)
			with self.assertRaises(FileNotFoundError):
				resources.resource_directories(config)

	def test_split_selection_is_persistent_and_rejects_bad_replacements(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime = self.component(root, "runtime", "runtime-1")
			model = self.component(root, "model", "model-1")
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			self.assertEqual(resources.resource_directories(root), (runtime, model))
			incomplete = self.component(root, "model", "model-2")
			(incomplete / "bluev3_onnx" / "onnx" / "vocoder.onnx").unlink()
			for runtime_id, model_id, interface in (
				("runtime-1", "model-2", "bluev3-v1"),
				("runtime-1", "model-1", "other"),
				("../outside", "model-1", "bluev3-v1"),
			):
				with self.subTest(model=model_id, interface=interface), self.assertRaises(ValueError):
					resources.activate(root, runtime_id, model_id, interface)
				self.assertEqual(resources.resource_directories(root), (runtime, model))

	def test_activation_requires_both_split_packs(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			model = self.component(root, "model", "model-1")
			with self.assertRaises(ValueError):
				resources.activate(root, None, "model-1", "bluev3-v1")
			self.assertTrue(model.is_dir())
			with self.assertRaises(FileNotFoundError):
				resources.resource_directories(root)

	def test_failed_selection_write_preserves_current_selection(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime = self.component(root, "runtime", "runtime-1")
			model = self.component(root, "model", "model-1")
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			new_model = self.component(root, "model", "model-2")
			with patch.object(resources.os, "replace", side_effect=OSError("cannot replace")):
				with self.assertRaises(OSError):
					resources.activate(root, "runtime-1", "model-2", "bluev3-v1")
			self.assertTrue(new_model.exists())
			self.assertEqual(resources.resource_directories(root), (runtime, model))

	def test_stale_selection_cannot_overwrite_a_newer_selection(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			self.component(root, "runtime", "runtime-1")
			self.component(root, "model", "model-1")
			new_model = self.component(root, "model", "model-2")
			resources.activate(root, "runtime-1", "model-2", "bluev3-v1")
			with self.assertRaisesRegex(RuntimeError, "selection changed"):
				resources.activate(
					root, "runtime-1", "model-1", "bluev3-v1",
					expected=("runtime-1", "model-1"),
				)
			with self.assertRaisesRegex(RuntimeError, "selection changed"):
				resources.stage_for_addon(
					root, "2.0.0", "runtime-1", "model-1",
					expected=("runtime-1", "model-1"),
				)
			self.assertEqual(resources.resource_directories(root)[1], new_model)
			self.assertEqual(resources.resource_directories(root, addon_version="2.0.0")[1], new_model)

	def test_prepared_resources_activate_only_for_matching_addon_version(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime = self.component(root, "runtime", "runtime-1")
			model = self.component(root, "model", "model-1")
			new_model = self.component(root, "model", "model-2")
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			resources.stage_for_addon(root, "2.0.0", "runtime-1", "model-2")
			self.assertEqual(resources.resource_directories(root, addon_version="1.0.0"), (runtime, model))
			self.assertEqual(resources.resource_directories(root, addon_version="dev"), (runtime, model))
			self.assertEqual(resources.resource_directories(root, addon_version="2.0.0"), (runtime, model))
			with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
				self.assertEqual(resources.resource_directories(root, addon_version="2.0.0"), (runtime, new_model))
			self.assertEqual(resources.resource_directories(root), (runtime, new_model))

	def test_invalid_prepared_resources_preserve_current_selection(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime = self.component(root, "runtime", "runtime-1")
			model = self.component(root, "model", "model-1")
			new_model = self.component(root, "model", "model-2")
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			resources.stage_for_addon(root, "2.0.0", "runtime-1", "model-2")
			(new_model / "bluev3_onnx" / "onnx" / "vocoder.onnx").unlink()
			with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
				with self.assertLogs("shravya_resources_test._resources", level="ERROR"):
					self.assertEqual(resources.resource_directories(root, addon_version="2.0.0"), (runtime, model))
			self.assertEqual(resources.resource_directories(root), (runtime, model))

	def test_cancelled_addon_install_discards_its_prepared_selection(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime = self.component(root, "runtime", "runtime-1")
			model = self.component(root, "model", "model-1")
			self.component(root, "model", "model-2")
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			resources.stage_for_addon(root, "2.0.0", "runtime-1", "model-2")
			resources.discard_for_addon(root, "2.0.0")
			self.assertEqual(resources.resource_directories(root, addon_version="2.0.0"), (runtime, model))
			with self.assertRaises(ValueError):
				resources.discard_for_addon(root, "../escape")

	def test_pending_addon_and_resource_updates_cannot_replace_each_other(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			for kind, version in (("runtime", "runtime-1"), ("runtime", "runtime-2"), ("model", "model-1")):
				self.component(root, kind, version)
			resources.activate(root, "runtime-1", "model-1", "bluev3-v1")
			resources.stage_for_addon(root, "2.0.0", "runtime-2", "model-1")
			with self.assertRaisesRegex(RuntimeError, "pending"):
				resources.stage_for_restart(
					root, "runtime-2", "model-1", expected=("runtime-1", "model-1"),
				)
			with self.assertRaisesRegex(RuntimeError, "pending"):
				resources.stage_for_addon(root, "2.0.0", "runtime-1", "model-1")
			with patch.object(resources, "_PROCESS_TOKEN", "next-process"):
				self.assertEqual(resources.resource_directories(root, addon_version="2.0.0")[0].name, "runtime-2")

	def test_corrupt_or_incompatible_selection_is_reported(self):
		resources = load_resources()
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			selection = root / "shravya" / "active-resources.json"
			selection.parent.mkdir()
			for content in (
				b"\xff",
				b"{",
				b'{"interface":"other","runtime":null,"model":null}',
				b'{"interface":"bluev3-v1","runtime":null,"model":null}',
				b'{"interface":"bluev3-v1","runtime":"runtime-1","model":null}',
				b'{"interface":"bluev3-v1","runtime":"../outside","model":null}',
			):
				with self.subTest(content=content):
					selection.write_bytes(content)
					with self.assertRaises(ValueError):
						resources.resource_directories(root)


if __name__ == "__main__":
	unittest.main()
