import importlib.util
import unittest
from pathlib import Path

PATH = (
	Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_diagnostic_measurements.py"
)


def load_module():
	spec = importlib.util.spec_from_file_location("shravya_diagnostic_measurements_test", PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestDiagnosticMeasurements(unittest.TestCase):
	def setUp(self):
		self.summarize = load_module().summarize

	def test_separates_controlled_and_live_timings(self):
		events = [
			(1, "quiet", None),
			(10, "key", "controlled"),
			(20, "queued", 1),
			(25, "started", 1),
			(35, "pcm", 1),
			(50, "onset", None),
			(65, "quiet", None),
			(70, "done", 1),
			(100, "key", "live"),
			(110, "queued", 2),
			(115, "started", 2),
			(125, "pcm", 2),
			(150, "onset", None),
			(170, "done", 2),
		]
		results = self.summarize(reversed(events))
		self.assertEqual(results["controlled"].latencies_ns, [40])
		self.assertEqual(results["live"].latencies_ns, [50])
		self.assertEqual(results["controlled"].queue_ns, [5])
		self.assertEqual(results["controlled"].synthesis_ns, [10])
		self.assertEqual(results["live"].ambiguous, 0)
		self.assertEqual(results["live"].interrupted, 0)

	def test_cancelled_and_overlapping_attempts_are_excluded(self):
		events = [
			(1, "quiet", None),
			(10, "key", "live"),
			(20, "queued", 1),
			(25, "started", 1),
			(30, "key", "live"),
			(35, "cancel", None),
			(40, "queued", 2),
			(45, "started", 2),
			(50, "pcm", 2),
			(60, "onset", None),
			(70, "done", 2),
		]
		result = self.summarize(events)["live"]
		self.assertEqual(result.latencies_ns, [])
		self.assertEqual(result.interrupted, 1)
		self.assertEqual(result.ambiguous, 1)

	def test_no_output_and_multiple_requests_are_ambiguous(self):
		events = [
			(1, "quiet", None),
			(10, "key", "live"),
			(15, "queued", 1),
			(20, "started", 1),
			(25, "pcm", 1),
			(30, "done", 1),
			(40, "key", "controlled"),
			(45, "queued", 2),
			(46, "queued", 3),
			(50, "started", 2),
			(55, "pcm", 2),
			(60, "onset", None),
			(65, "done", 2),
			(70, "done", 3),
		]
		results = self.summarize(events)
		self.assertEqual(results["live"].ambiguous, 1)
		self.assertEqual(results["controlled"].ambiguous, 1)
		self.assertEqual(results["live"].latencies_ns, [])
		self.assertEqual(results["controlled"].latencies_ns, [])

	def test_invalid_output_and_unrelated_playback_are_ambiguous(self):
		events = [
			(0, "quiet", None),
			(1, "queued", 1),
			(5, "key", "live"),
			(10, "queued", 2),
			(15, "done", 1),
			(20, "started", 2),
			(25, "pcm", 2),
			(30, "invalid", None),
			(35, "onset", None),
			(40, "done", 2),
		]
		result = self.summarize(events)["live"]
		self.assertEqual(result.ambiguous, 1)
		self.assertEqual(result.latencies_ns, [])

	def test_queued_input_or_already_loud_output_is_not_clean(self):
		events = [
			(1, "quiet", None),
			(2, "onset", None),
			(10, "key", "live"),
			(15, "key", "live"),
			(20, "queued", 1),
			(25, "started", 1),
			(30, "pcm", 1),
			(40, "quiet", None),
			(45, "onset", None),
			(50, "done", 1),
		]
		result = self.summarize(events)["live"]
		self.assertEqual(result.ambiguous, 2)
		self.assertEqual(result.latencies_ns, [])

	def test_preexisting_speech_blocks_new_measurements_until_it_finishes(self):
		events = [
			(1, "busy", 1),
			(2, "quiet", None),
			(5, "key", "live"),
			(10, "queued", 2),
			(15, "started", 2),
			(20, "pcm", 2),
			(25, "onset", None),
			(28, "done", 1),
			(30, "done", 2),
			(35, "quiet", None),
			(40, "key", "live"),
			(45, "queued", 3),
			(50, "started", 3),
			(55, "pcm", 3),
			(60, "onset", None),
			(65, "done", 3),
		]
		result = self.summarize(events)["live"]
		self.assertEqual(result.ambiguous, 1)
		self.assertEqual(result.latencies_ns, [20])

	def test_preexisting_speech_remains_busy_until_the_latest_job_finishes(self):
		events = [
			(1, "busy", 2),
			(2, "quiet", None),
			(3, "done", 1),
			(5, "key", "live"),
			(10, "queued", 3),
			(15, "started", 3),
			(20, "pcm", 3),
			(25, "onset", None),
			(28, "done", 2),
			(30, "done", 3),
			(35, "quiet", None),
			(40, "key", "live"),
			(45, "queued", 4),
			(50, "started", 4),
			(55, "pcm", 4),
			(60, "onset", None),
			(65, "done", 4),
		]
		result = self.summarize(events)["live"]
		self.assertEqual(result.ambiguous, 1)
		self.assertEqual(result.latencies_ns, [20])

	def test_reports_only_model_loads_started_and_finished_during_capture(self):
		module = load_module()
		events = [
			(5, "model_done", None),
			(10, "model_start", None),
			(30, "model_done", None),
			(40, "model_start", None),
		]
		self.assertEqual(module.model_loads_ns(events), [20])
		self.assertEqual(module.summarize(events)["live"].latencies_ns, [])


if __name__ == "__main__":
	unittest.main()
