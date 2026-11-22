import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


class TestUninstallCleanup(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.config = Path(temporary.name)
		self.resources = self.config / "shravya"
		self.addon = SimpleNamespace(
			name="shravya",
			isPendingRemove=True,
			installPath=str(self.config / "addons" / "shravya"),
			pendingInstallPath=str(self.config / "addons" / "shravya.pendingInstall"),
		)
		Path(self.addon.installPath).mkdir(parents=True)
		self.pending = set()
		self.queued = []
		state = ModuleType("NVDAState")
		state.WritePaths = SimpleNamespace(configDir=str(self.config))
		state.shouldWriteToDisk = lambda: True
		handler = ModuleType("addonHandler")
		handler.Addon = SimpleNamespace
		handler.AddonStateCategory = SimpleNamespace(PENDING_INSTALL="pending")
		handler.state = {"pending": self.pending}
		handler.getCodeAddon = lambda: self.addon
		queue = ModuleType("queueHandler")
		queue.eventQueue = object()
		queue.queueFunction = lambda queue, callback, *args: self.queued.append((callback, args))
		self.state = state
		patcher = patch.dict(
			sys.modules, {"NVDAState": state, "addonHandler": handler, "queueHandler": queue},
		)
		patcher.start()
		self.addCleanup(patcher.stop)
		path = Path(__file__).resolve().parents[1] / "addon" / "installTasks.py"
		spec = importlib.util.spec_from_file_location("shravya_install_tasks_test", path)
		self.tasks = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.tasks)

	def test_preference_is_default_off_and_persisted_globally(self):
		self.assertFalse(self.tasks.cleanup_enabled())
		self.tasks.set_cleanup_enabled(False)
		self.assertFalse(self.resources.exists())
		self.tasks.set_cleanup_enabled(True)
		self.assertTrue(self.tasks.cleanup_enabled())
		self.tasks.set_cleanup_enabled(False)
		self.assertFalse(self.tasks.cleanup_enabled())

	def test_confirmed_removal_deletes_only_opted_in_resources(self):
		self.resources.mkdir()
		(self.resources / "runtime.dat").write_text("installed")
		external_zip = self.config / "original.zip"
		external_zip.write_text("original")
		self.tasks.onUninstall()
		self.assertTrue((self.resources / "runtime.dat").exists())
		self.assertFalse(self.queued)
		self.tasks.set_cleanup_enabled(True)
		self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		Path(self.addon.installPath).rmdir()
		callback, args = self.queued.pop()
		callback(*args)
		self.assertFalse(self.resources.exists())
		self.assertEqual(external_zip.read_text(), "original")

	def test_original_zip_files_inside_resources_are_preserved(self):
		self.tasks.set_cleanup_enabled(True)
		nested = self.resources / "packs" / "model"
		nested.mkdir(parents=True)
		archive = nested / "original.ZIP"
		archive.write_text("original")
		(nested / "model.onnx").write_text("installed")
		self.tasks.onUninstall()
		Path(self.addon.installPath).rmdir()
		callback, args = self.queued.pop()
		callback(*args)
		self.assertEqual(archive.read_text(), "original")
		self.assertFalse((nested / "model.onnx").exists())

	def test_failed_removal_does_not_delete_resources(self):
		self.tasks.set_cleanup_enabled(True)
		self.tasks.onUninstall()
		callback, args = self.queued.pop()
		callback(*args)
		self.assertTrue(self.resources.exists())

	def test_update_started_after_hook_does_not_delete_resources(self):
		self.tasks.set_cleanup_enabled(True)
		self.tasks.onUninstall()
		Path(self.addon.installPath).rmdir()
		self.pending.add("shravya")
		callback, args = self.queued.pop()
		callback(*args)
		self.assertTrue(self.resources.exists())

	def test_unreadable_addon_path_fails_closed(self):
		self.tasks.set_cleanup_enabled(True)
		self.tasks.onUninstall()
		callback, args = self.queued.pop()
		with patch.object(Path, "lstat", side_effect=PermissionError("unreadable")):
			with self.assertRaises(PermissionError):
				callback(*args)
		self.assertTrue(self.resources.exists())

	def test_nested_directory_link_does_not_delete_its_target(self):
		self.tasks.set_cleanup_enabled(True)
		external = self.config / "external"
		external.mkdir()
		(external / "keep").write_text("outside")
		try:
			(self.resources / "link").symlink_to(external, target_is_directory=True)
		except OSError as error:
			self.skipTest(f"Creating a directory symlink is unavailable: {error}")
		self.tasks.onUninstall()
		Path(self.addon.installPath).rmdir()
		callback, args = self.queued.pop()
		callback(*args)
		self.assertEqual((external / "keep").read_text(), "outside")

	def test_cancelled_removal_and_pending_addon_leave_resources(self):
		self.tasks.set_cleanup_enabled(True)
		self.addon.isPendingRemove = False
		self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		self.assertFalse(self.queued)

	def test_update_is_preserved_in_either_pending_state_or_directory_order(self):
		self.tasks.set_cleanup_enabled(True)
		self.pending.add("shravya")
		self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		self.assertFalse(self.queued)
		self.pending.clear()
		Path(self.addon.pendingInstallPath).mkdir(parents=True)
		self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		self.assertFalse(self.queued)

	def test_secure_mode_does_not_remove_resources_or_change_preference(self):
		self.tasks.set_cleanup_enabled(True)
		self.state.shouldWriteToDisk = lambda: False
		self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		self.assertFalse(self.queued)
		with self.assertRaises(PermissionError):
			self.tasks.set_cleanup_enabled(False)

	def test_linked_resource_root_and_marker_are_rejected(self):
		with patch.object(Path, "is_junction", return_value=True):
			with self.assertRaisesRegex(ValueError, "linked"):
				self.tasks.set_cleanup_enabled(True)
		self.tasks.set_cleanup_enabled(True)
		with patch.object(Path, "is_symlink", return_value=True):
			with self.assertRaisesRegex(ValueError, "linked"):
				self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())

	def test_corrupt_preference_and_deletion_failure_are_not_silent(self):
		self.tasks.set_cleanup_enabled(True)
		marker = self.resources / ".delete-on-uninstall"
		marker.write_text("invalid", encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "preference"):
			self.tasks.onUninstall()
		self.assertTrue(self.resources.exists())
		self.tasks.set_cleanup_enabled(True)
		self.tasks.onUninstall()
		Path(self.addon.installPath).rmdir()
		callback, args = self.queued.pop()
		with patch.object(Path, "unlink", side_effect=OSError("locked")):
			with self.assertRaisesRegex(OSError, "locked"):
				callback(*args)
		self.assertTrue(self.resources.exists())
