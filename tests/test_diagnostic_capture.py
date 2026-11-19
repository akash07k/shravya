import importlib.util
import struct
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

DRIVER = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def load_capture():
	_ = importlib.import_module("numpy")
	package = ModuleType("shravya_capture_test")
	package.__path__ = [str(DRIVER)]
	spec = importlib.util.spec_from_file_location(
		"shravya_capture_test._diagnostic_capture", DRIVER / "_diagnostic_capture.py"
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"shravya_capture_test": package}):
		spec.loader.exec_module(module)
	return module


class TestDiagnosticCapture(unittest.TestCase):
	def test_packet_onsets_flow_into_separate_workload_report(self):
		module = load_capture()
		packets_done = threading.Event()
		errors = []
		session = None

		def capture(endpoint, stop, on_ready, on_packet):
			on_ready(module.AudioFormat(1000, 1, "pcm16", 2), "endpoint")
			silent = struct.pack("<4h", 0, 0, 0, 0)
			sound = struct.pack("<4h", 0, 0, 1000, 1000)
			on_packet(silent, 4, 0, 1_000_000_000)
			for timestamp, kind, value in [
				(1_005_000_000, "key", "controlled"),
				(1_006_000_000, "queued", 1),
				(1_007_000_000, "started", 1),
				(1_008_000_000, "pcm", 1),
			]:
				session.record((timestamp, kind, value))
			on_packet(sound, 4, 0, 1_009_000_000)
			session.record((1_013_000_000, "done", 1))
			on_packet(silent, 4, 0, 1_014_000_000)
			for timestamp, kind, value in [
				(1_020_000_000, "key", "live"),
				(1_021_000_000, "queued", 2),
				(1_022_000_000, "started", 2),
				(1_023_000_000, "pcm", 2),
			]:
				session.record((timestamp, kind, value))
			on_packet(sound, 4, 0, 1_024_000_000)
			session.record((1_028_000_000, "done", 2))
			packets_done.set()
			stop.wait(5)

		with patch.object(module, "capture_packets", capture):
			session = module.DiagnosticCapture(None, lambda endpoint: None, errors.append)
			session.start()
			try:
				self.assertTrue(packets_done.wait(5))
			finally:
				events = session.stop()
		package = ModuleType("shravya_capture_test")
		package.__path__ = [str(DRIVER)]
		spec = importlib.util.spec_from_file_location(
			"shravya_capture_test._diagnostic_report", DRIVER / "_diagnostic_report.py"
		)
		report_module = importlib.util.module_from_spec(spec)
		report_module.__dict__["_"] = lambda value: value
		with patch.dict(sys.modules, {"shravya_capture_test": package}):
			spec.loader.exec_module(report_module)
		report = report_module.format_report(events)
		controlled, live = report.split("Live navigation and typing")
		self.assertIn("Valid: 1", controlled)
		self.assertIn("Key to output: median 6.000", controlled)
		self.assertIn("Valid: 1", live)
		self.assertIn("Key to output: median 6.000", live)
		self.assertEqual(errors, [])

	def test_records_packet_clock_onsets_and_invalidates_bad_packets(self):
		module = load_capture()
		packets_done = threading.Event()
		ready = []
		errors = []

		def capture(endpoint, stop, on_ready, on_packet):
			on_ready(module.AudioFormat(1000, 1, "pcm16", 2), "endpoint")
			silent = struct.pack("<4h", 0, 0, 0, 0)
			sound = struct.pack("<4h", 0, 0, 1000, 1000)
			on_packet(silent, 4, 0, 1_000_000_000)
			on_packet(sound, 4, 0, 1_004_000_000)
			on_packet(silent, 4, 4, 1_008_000_000)
			on_packet(sound, 4, 0, 1_012_000_000)
			on_packet(silent, 4, 0, 1_016_000_000)
			on_packet(sound, 4, 0, 1_020_000_000)
			packets_done.set()
			stop.wait(5)

		with patch.object(module, "capture_packets", capture):
			session = module.DiagnosticCapture(None, ready.append, errors.append)
			session.start()
			try:
				self.assertTrue(packets_done.wait(5))
			finally:
				events = session.stop()
		self.assertEqual(ready, ["endpoint"])
		self.assertEqual(errors, [])
		self.assertEqual(
			events,
			[
				(1_004_000_000, "quiet", None),
				(1_006_000_000, "onset", None),
				(1_008_000_000, "invalid", None),
				(1_020_000_000, "quiet", None),
				(1_022_000_000, "onset", None),
			],
		)

	def test_capture_failure_is_reported_and_stop_does_not_return_results(self):
		module = load_capture()
		failed = threading.Event()
		errors = []

		def capture(endpoint, stop, on_ready, on_packet):
			raise RuntimeError("device unavailable")

		def on_error(error):
			errors.append(error)
			failed.set()

		with patch.object(module, "capture_packets", capture):
			session = module.DiagnosticCapture(None, lambda endpoint: None, on_error)
			session.start()
			self.assertTrue(failed.wait(5))
			with self.assertRaisesRegex(RuntimeError, "device unavailable"):
				session.stop()
		self.assertEqual(len(errors), 1)


if __name__ == "__main__":
	unittest.main()
