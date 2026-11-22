import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

DRIVER = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya"


def load_report():
	package = ModuleType("shravya_report_test")
	package.__path__ = [str(DRIVER)]
	spec = importlib.util.spec_from_file_location(
		"shravya_report_test._diagnostic_report", DRIVER / "_diagnostic_report.py",
	)
	module = importlib.util.module_from_spec(spec)
	module.__dict__["_"] = lambda value: value
	with patch.dict(sys.modules, {"shravya_report_test": package}):
		spec.loader.exec_module(module)
	return module


class TestDiagnosticReport(unittest.TestCase):
	def test_reports_workloads_timings_and_exclusions_in_milliseconds(self):
		events = [
			(1, "quiet", None),
			(10_000_000, "key", "controlled"),
			(20_000_000, "queued", 1),
			(25_000_000, "started", 1),
			(35_000_000, "pcm", 1),
			(50_000_000, "onset", None),
			(70_000_000, "done", 1),
			(80_000_000, "quiet", None),
			(100_000_000, "key", "live"),
			(110_000_000, "queued", 2),
			(120_000_000, "cancel", None),
			(140_000_000, "key", "live"),
			(200_000_000, "model_start", None),
			(250_000_000, "model_done", None),
		]
		report = load_report().format_report(events)
		self.assertIn("Controlled", report)
		self.assertIn("Live", report)
		self.assertIn("40.000", report)
		self.assertIn("5.000", report)
		self.assertIn("10.000", report)
		self.assertIn("50.000", report)
		self.assertIn("Interrupted: 1", report)
		self.assertIn("Ambiguous: 1", report)
		self.assertIn("milliseconds", report)
		self.assertIn("other applications", report.lower())

	def test_does_not_report_missing_measurements_as_zero(self):
		report = load_report().format_report([])
		self.assertIn("No valid samples", report)
		self.assertIn("not observed", report.lower())
		self.assertNotIn("median: 0", report.lower())


if __name__ == "__main__":
	unittest.main()
