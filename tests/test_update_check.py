import importlib
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_update.py"


class Handler(BaseHTTPRequestHandler):
	def log_message(self, format, *args):
		pass

	def do_GET(self):
		self.server.requests.append(self.path)
		data = (
			self.server.manifest
			if self.path == "/update.toml"
			else json.dumps(self.server.release).encode("utf-8")
		)
		status = self.server.manifest_status if self.path == "/update.toml" else self.server.status
		self.send_response(status)
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)


class TestUpdateCheck(unittest.TestCase):
	def setUp(self):
		package = types.ModuleType("shravya_update_test")
		package.__path__ = [str(PATH.parent)]
		sys.modules[package.__name__] = package
		self.update = importlib.import_module("shravya_update_test._update")
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.server.status = 200
		self.server.manifest_status = 200
		self.server.requests = []
		self.server.manifest = (
			b'version = "1.2.3"\nchanges = "First change\\nSecond change"\n'
			b'resource_catalog = "https://example.test/resources.toml"\n'
		)
		self.server.release = {
			"tag_name": "v1.2.3",
			"draft": False,
			"prerelease": False,
			"assets": [
				{
					"name": "shravya-1.2.3.nvda-addon",
					"browser_download_url": "https://example.test/shravya-1.2.3.nvda-addon",
				},
				{
					"name": "shravya-update.toml",
					"browser_download_url": f"http://127.0.0.1:{self.server.server_port}/update.toml",
				},
			],
		}
		self.thread = threading.Thread(target=self.server.serve_forever)
		self.thread.start()
		self.addCleanup(self.close_server)
		self.url = f"http://127.0.0.1:{self.server.server_port}/latest"

	def close_server(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def test_newer_release_selects_matching_addon(self):
		result = self.update.check_release(self.url, "1.2.2")
		self.assertEqual(result.version, "1.2.3")
		self.assertEqual(result.url, "https://example.test/shravya-1.2.3.nvda-addon")
		self.assertEqual(result.notes, "First change\nSecond change")
		self.assertEqual(result.catalog_url, "https://example.test/resources.toml")

	def test_configured_release_manifest_is_accepted_by_updater(self):
		root = PATH.parents[3]
		with tempfile.TemporaryDirectory() as directory:
			temporary = Path(directory)
			config = temporary / "config.toml"
			changelog = temporary / "changelog.md"
			output = temporary / "shravya-update.toml"
			config.write_text(
				'[delivery]\ncatalog_url = "https://example.test/configured-catalog"\n',
				encoding="utf-8",
			)
			changelog.write_text("New release.", encoding="utf-8")
			read = subprocess.run(
				[sys.executable, "-B", str(PATH.parent / "_delivery_config.py"), str(config)],
				capture_output=True, text=True, check=True,
			)
			subprocess.run(
				[sys.executable, "-B", str(root / "tools" / "build_update_manifest.py"),
				 "1.2.3", str(changelog), str(output), "--resource-catalog", read.stdout.strip()],
				check=True,
			)
			self.server.manifest = output.read_bytes()
			result = self.update.check_release(self.url, "1.2.2")
			self.assertEqual(result.catalog_url, "https://example.test/configured-catalog")

	def test_equal_or_older_version_does_not_offer_update(self):
		for current in ("1.2.3", "1.10.0"):
			with self.subTest(current=current):
				self.assertIsNone(self.update.check_release(self.url, current))
		self.assertEqual(self.server.requests, ["/latest", "/latest"])

	def test_bad_release_or_missing_addon_is_an_error(self):
		for change in (
			{"tag_name": "v1.2.3-beta"},
			{"prerelease": True},
			{"assets": []},
			{"assets": [{"name": "shravya-1.2.3.nvda-addon", "browser_download_url": ""}]},
			{"assets": [self.server.release["assets"][0]]},
			{"assets": [*self.server.release["assets"], self.server.release["assets"][1]]},
			{"assets": [self.server.release["assets"][0], {"name": "shravya-update.toml"}]},
		):
			with self.subTest(change=change):
				release = self.server.release.copy()
				self.server.release.update(change)
				with self.assertRaises(ValueError):
					self.update.check_release(self.url, "1.2.2")
				self.server.release = release

	def test_missing_notes_are_explicitly_absent(self):
		for contents in (
			b'version = "1.2.3"\nresource_catalog = "https://example.test/catalog"\n',
			b'version = "1.2.3"\nchanges = "  "\nresource_catalog = "https://example.test/catalog"\n',
		):
			with self.subTest(contents=contents):
				self.server.manifest = contents
				self.assertIsNone(self.update.check_release(self.url, "1.2.2").notes)

	def test_invalid_manifest_is_reported_as_an_error(self):
		for contents in (
			b'version = "1.2.4"\nchanges = "Wrong release"\n',
			b'changes = "Missing version"\n',
			b'version = "1.2.3"\nchanges = 42\n',
			b'version = "1.2.3"\nchanges = [\n',
			b'version = "1.2.3"\nchanges = "\xff"\n',
			b'version = "1.2.3"\n',
			b'version = "1.2.3"\nresource_catalog = "file:///local.toml"\n',
		):
			with self.subTest(contents=contents):
				self.server.manifest = contents
				with self.assertRaises(ValueError):
					self.update.check_release(self.url, "1.2.2")

	def test_failed_manifest_download_is_not_reported_as_no_update(self):
		self.server.manifest_status = 404
		with self.assertRaises(OSError):
			self.update.check_release(self.url, "1.2.2")

	def test_http_failure_is_not_reported_as_no_update(self):
		self.server.status = 404
		with self.assertRaises(OSError):
			self.update.check_release(self.url, "1.2.2")


if __name__ == "__main__":
	unittest.main()
