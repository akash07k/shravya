import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

PATH = Path(__file__).resolve().parents[1] / "tools" / "download_resource_model.py"
FILES = (
	"onnx/duration_predictor_style.onnx",
	"onnx/text_encoder.onnx",
	"onnx/vector_estimator.onnx",
	"onnx/vocoder.onnx",
	"onnx/tts.json",
	"onnx/vocab.json",
	"onnx/stats.npz",
	"onnx/uncond.npz",
	"frontend/polyg2p_data/en.lex",
	"frontend/polyg2p_data/en-large.g2p",
	"frontend/polyg2p_data/en.g2p",
	"frontend/polyg2p_data/LICENSE",
	"frontend/polyg2p_data/NOTICE",
	"voices/noa.json",
	"voices/adam.json",
	"LICENSES.md",
	"LICENSE.OpenRAIL-M",
)


class TestModelDownload(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.bundle = self.root / "downloaded"
		spec = importlib.util.spec_from_file_location("download_model_test", PATH)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)
		self.revision = "a" * 40
		resolver = patch.object(self.module.resource_sources, "model_revision", return_value=self.revision)
		resolver.start()
		self.addCleanup(resolver.stop)
		self.listing_url = (
			f"https://huggingface.co/api/models/{self.module.REPOSITORY}/tree/{self.revision}"
			"?recursive=true&expand=false"
		)

	def test_downloads_only_matching_english_model_and_notices(self):
		listing = [{"type": "file", "path": name, "size": len(name.encode())} for name in FILES]
		listing.extend(
			{"type": "file", "path": name}
			for name in (
				"onnx/reference_encoder.onnx",
				"frontend/polyg2p_data/de.lex",
				"voices/..\\outside.json",
			)
		)
		downloaded = []

		def open_url(url, timeout):
			if url == self.listing_url:
				return io.BytesIO(json.dumps(listing).encode())
			name = unquote(url.split("/resolve/", 1)[1].split("/", 1)[1])
			downloaded.append(name)
			return io.BytesIO(name.encode())

		with patch.object(self.module.urllib.request, "urlopen", side_effect=open_url):
			self.assertEqual(self.module.download_model(self.bundle), self.revision)
		self.assertEqual(set(downloaded), set(FILES))
		for name in FILES:
			self.assertEqual((self.bundle / name).read_bytes(), name.encode())
		self.assertFalse((self.bundle / "onnx" / "reference_encoder.onnx").exists())

	def test_incomplete_listing_or_transfer_never_leaves_a_bundle(self):
		listing = [
			{"type": "file", "path": name, "size": len(name.encode())}
			for name in FILES
			if name != "voices/noa.json"
		]
		with patch.object(
			self.module.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(listing).encode())
		):
			with self.assertRaisesRegex(ValueError, "voices/noa.json"):
				self.module.download_model(self.bundle)
		self.assertFalse(self.bundle.exists())

		listing.append({"type": "file", "path": "voices/noa.json", "size": 1})

		def interrupted(url, timeout):
			if url == self.listing_url:
				return io.BytesIO(json.dumps(listing).encode())
			raise OSError("transfer failed")

		with patch.object(self.module.urllib.request, "urlopen", side_effect=interrupted):
			with self.assertRaisesRegex(OSError, "transfer failed"):
				self.module.download_model(self.bundle)
		self.assertFalse(self.bundle.exists())

	def test_short_transfer_never_leaves_a_bundle(self):
		listing = [{"type": "file", "path": name, "size": 100} for name in FILES]

		def open_url(url, timeout):
			if url == self.listing_url:
				return io.BytesIO(json.dumps(listing).encode())
			return io.BytesIO(b"short")

		with patch.object(self.module.urllib.request, "urlopen", side_effect=open_url):
			with self.assertRaisesRegex(OSError, "size"):
				self.module.download_model(self.bundle)
		self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
	unittest.main()
