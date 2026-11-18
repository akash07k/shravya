import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


class TestDeliveryConfig(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.path = Path(self.temp.name) / "config.toml"
		package = types.ModuleType("delivery_config_test")
		package.__path__ = [str(SOURCE)]
		patcher = patch.dict(sys.modules, {"delivery_config_test": package})
		patcher.start()
		self.addCleanup(patcher.stop)
		for name in list(sys.modules):
			if name.startswith("delivery_config_test."):
				del sys.modules[name]
		self.config = importlib.import_module("delivery_config_test._delivery_config")

	def test_configured_urls_and_disabled_checks(self):
		self.path.write_text(
			'[delivery]\ncatalog_url = "https://example.test/catalog.toml"\n'
			'release_api = "https://example.test/latest"\n',
			encoding="utf-8",
		)
		config = self.config.load_delivery_config(self.path)
		self.assertEqual(config.catalog_url, "https://example.test/catalog.toml")
		self.assertEqual(config.release_api, "https://example.test/latest")
		self.path.write_text('[delivery]\n', encoding="utf-8")
		config = self.config.load_delivery_config(self.path)
		self.assertIsNone(config.catalog_url)
		self.assertIsNone(config.release_api)

	def test_default_config_uses_github_endpoints(self):
		config = self.config.load_delivery_config()
		self.assertEqual(config, (
			"https://raw.githubusercontent.com/akash07k/shravya/main/catalogs/current.toml",
			"https://api.github.com/repos/akash07k/shravya/releases/latest",
		))

	def test_invalid_values_and_missing_config_are_reported(self):
		for value in ('"file:///tmp/catalog"', "17", '"https://"'):
			with self.subTest(value=value):
				self.path.write_text(f"[delivery]\ncatalog_url = {value}\n", encoding="utf-8")
				with self.assertRaisesRegex(ValueError, "catalog_url"):
					self.config.load_delivery_config(self.path)
		self.path.write_text("[delivery\n", encoding="utf-8")
		with self.assertRaises(ValueError):
			self.config.load_delivery_config(self.path)
		self.path.unlink()
		with self.assertRaises(FileNotFoundError):
			self.config.load_delivery_config(self.path)

	def test_missing_section_or_unknown_setting_is_reported(self):
		for text in ("", '[delivery]\ncatalog_urll = "https://example.test"\n'):
			with self.subTest(text=text):
				self.path.write_text(text, encoding="utf-8")
				with self.assertRaises(ValueError):
					self.config.load_delivery_config(self.path)


if __name__ == "__main__":
	unittest.main()
