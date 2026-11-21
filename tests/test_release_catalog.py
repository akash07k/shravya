import http.server
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from tools import check_release_catalog


class Handler(http.server.SimpleHTTPRequestHandler):
	def log_message(self, format, *args):
		pass


class TestReleaseCatalog(unittest.TestCase):
	def test_release_requires_compatible_catalog_and_available_zip_assets(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=temp))
			thread = threading.Thread(target=server.serve_forever)
			thread.start()
			try:
				base = f"http://127.0.0.1:{server.server_port}"
				(root / "catalog.toml").write_text(
					'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
					f'[runtime]\nid = "runtime-1"\nurl = "{base}/runtime.zip"\n'
					f'[model]\nid = "model-1"\nurl = "{base}/model.zip"\n',
					encoding="utf-8",
				)
				for name in ("runtime.zip", "model.zip"):
					(root / name).write_bytes(b"PK\x03\x04archive")
				(root / "bad.zip").write_bytes(b"not a zip")
				check_release_catalog.check(f"{base}/catalog.toml", "1.0.0")
				with self.assertRaises(ValueError):
					check_release_catalog.check(f"{base}/catalog.toml", "2.0.0")
				catalog = root / "catalog.toml"
				content = catalog.read_text(encoding="utf-8")
				catalog.write_text(content.replace("/model.zip", "/bad.zip"), encoding="utf-8")
				with self.assertRaisesRegex(ValueError, "model"):
					check_release_catalog.check(f"{base}/catalog.toml", "1.0.0")
				catalog.write_text(content.replace("/model.zip", "/missing.zip"), encoding="utf-8")
				with self.assertRaises(OSError):
					check_release_catalog.check(f"{base}/catalog.toml", "1.0.0")
			finally:
				server.shutdown()
				server.server_close()
				thread.join()
