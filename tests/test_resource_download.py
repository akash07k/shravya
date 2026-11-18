import importlib.util
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_download.py"


def load_download():
	spec = importlib.util.spec_from_file_location("shravya_download_test", PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class Handler(BaseHTTPRequestHandler):
	def log_message(self, format, *args):
		pass

	def do_GET(self):
		server = self.server
		server.requests.append((self.headers.get("Range"), self.headers.get("If-Range")))
		data = server.payload
		start = 0
		if self.headers.get("Range") and server.reject_range:
			self.send_response(416)
			self.send_header("Content-Length", "0")
			self.end_headers()
			return
		if self.headers.get("Range") and self.headers.get("If-Range") == server.etag:
			start = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
		self.send_response(206 if start else 200)
		self.send_header("Content-Length", str(len(data) - start))
		if server.etag:
			self.send_header("ETag", '"different"' if start and server.invalid_resume else server.etag)
		if start:
			self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
		self.end_headers()
		if server.interrupt:
			server.interrupt = False
			self.wfile.write(data[start : start + 6])
			self.wfile.flush()
			self.connection.shutdown(socket.SHUT_RDWR)
			self.connection.close()
			return
		self.wfile.write(data[start:])


class TestResourceDownload(unittest.TestCase):
	def setUp(self):
		self.download = load_download().download
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.archive = Path(self.directory.name) / "pack.zip"
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.server.payload = b"original pack contents"
		self.server.etag = '"version-a"'
		self.server.interrupt = False
		self.server.invalid_resume = False
		self.server.reject_range = False
		self.server.requests = []
		self.thread = threading.Thread(target=self.server.serve_forever)
		self.thread.start()
		self.addCleanup(self.close_server)
		self.url = f"http://127.0.0.1:{self.server.server_port}/pack"

	def close_server(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def test_interrupted_transfer_resumes_the_same_version(self):
		self.server.interrupt = True
		with self.assertRaises(OSError):
			self.download(self.url, self.archive, lambda done, total: None)
		self.download(self.url, self.archive, lambda done, total: None)
		self.assertEqual(self.archive.read_bytes(), self.server.payload)
		self.assertEqual(self.server.requests[-1], ("bytes=6-", '"version-a"'))

	def test_changed_version_restarts_instead_of_mixing(self):
		self.server.interrupt = True
		with self.assertRaises(OSError):
			self.download(self.url, self.archive, lambda done, total: None)
		self.server.payload = b"replacement pack contents"
		self.server.etag = '"version-b"'
		self.download(self.url, self.archive, lambda done, total: None)
		self.assertEqual(self.archive.read_bytes(), self.server.payload)

	def test_without_a_strong_validator_an_interrupted_transfer_restarts(self):
		self.server.etag = None
		self.server.interrupt = True
		with self.assertRaises(OSError):
			self.download(self.url, self.archive, lambda done, total: None)
		self.download(self.url, self.archive, lambda done, total: None)
		self.assertEqual(self.archive.read_bytes(), self.server.payload)
		self.assertEqual(self.server.requests[-1], (None, None))

	def test_invalid_resumed_response_restarts_from_zero(self):
		self.server.interrupt = True
		with self.assertRaises(OSError):
			self.download(self.url, self.archive, lambda done, total: None)
		self.server.invalid_resume = True
		self.download(self.url, self.archive, lambda done, total: None)
		self.assertEqual(self.archive.read_bytes(), self.server.payload)
		self.assertEqual(self.server.requests[-1], (None, None))

	def test_rejected_range_restarts_from_zero(self):
		self.server.interrupt = True
		with self.assertRaises(OSError):
			self.download(self.url, self.archive, lambda done, total: None)
		self.server.reject_range = True
		self.download(self.url, self.archive, lambda done, total: None)
		self.assertEqual(self.archive.read_bytes(), self.server.payload)
		self.assertEqual(self.server.requests[-1], (None, None))


if __name__ == "__main__":
	unittest.main()
