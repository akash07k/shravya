import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

WORKER_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_worker.py"


def load_worker():
	spec = importlib.util.spec_from_file_location("shravya_worker_test._worker", WORKER_PATH)
	module = importlib.util.module_from_spec(spec)
	package = ModuleType("shravya_worker_test")
	package.__path__ = [str(WORKER_PATH.parent)]
	with patch.dict(sys.modules, {"shravya_worker_test": package}):
		spec.loader.exec_module(module)
	return module


class FakeEngine:
	sample_rate = 1000

	def __init__(self):
		self.blocked = threading.Event()
		self.release = threading.Event()
		self.closed = False
		self.calls = []
		self.speeds = []
		self.seeds = []

	def stream(self, text, *, speed, steps, seed=0, voice="noa"):
		self.calls.append((text, voice))
		self.speeds.append(speed)
		self.seeds.append(seed)
		if text == "blocked":
			self.blocked.set()
			if not self.release.wait(5):
				raise TimeoutError("test did not release inference")
		yield text.encode()

	def close(self):
		self.closed = True


class FakePlayer:
	def __init__(self):
		self.condition = threading.Condition()
		self.feeds = []
		self.stops = 0
		self.volumes = []
		self.activated = threading.Event()
		self.stop_started = threading.Event()
		self.stop_release = threading.Event()
		self.stop_release.set()

	def feed(self, data, *, onDone=None):
		with self.condition:
			self.feeds.append((data, onDone))
			self.condition.notify_all()

	def wait_for_feeds(self, count, timeout=5):
		with self.condition:
			return self.condition.wait_for(lambda: len(self.feeds) >= count, timeout=timeout)

	def stop(self):
		self.stops += 1
		self.stop_started.set()
		if not self.stop_release.wait(5):
			raise TimeoutError("test did not release playback stop")

	def pause(self, switch):
		pass

	def setVolume(self, *, all):
		self.volumes.append(all)
		self.activated.set()

	def startTrimmingLeadingSilence(self, start=True):
		pass

	def idle(self):
		pass

	def close(self):
		pass


class TestSpeechWorker(unittest.TestCase):
	def setUp(self):
		self.module = load_worker()
		self.engine = FakeEngine()
		self.player = FakePlayer()
		self.indices = []
		self.done = []
		self.errors = []
		self.worker = self.module.SpeechWorker(
			lambda: self.engine,
			lambda rate: self.player,
			self.indices.append,
			lambda: self.done.append(True),
			self.errors.append,
		)
		self.addCleanup(self.worker.close)
		self.addCleanup(self.engine.release.set)
		self.assertTrue(self.player.activated.wait(5))

	def test_queued_requests_keep_their_seed(self):
		self.worker.speak(["blocked"], speed=1.0, steps=4, seed=7)
		self.assertTrue(self.engine.blocked.wait(5))
		self.worker.speak(["next"], speed=1.0, steps=4, seed=42)
		self.engine.release.set()
		self.assertTrue(self.player.wait_for_feeds(4))
		self.assertEqual(self.engine.seeds, [7, 42])

	def test_diagnostic_events_are_recorded_only_while_enabled(self):
		events = []
		self.worker.set_diagnostic(events.append)
		self.worker.speak(["first"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(2))
		self.player.feeds[1][1]()
		self.assertEqual([event[1] for event in events], ["queued", "started", "pcm", "done"])
		self.assertEqual(len({event[2] for event in events}), 1)
		self.assertEqual([event[0] for event in events], sorted(event[0] for event in events))

		self.worker.set_diagnostic(None)
		self.worker.speak(["second"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(4))
		self.player.feeds[3][1]()
		self.assertEqual(len(events), 4)

	def test_diagnostic_marks_speech_already_active_at_start(self):
		self.worker.speak(["blocked"], speed=1.0, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		events = []
		self.worker.set_diagnostic(events.append)
		self.assertEqual(events[0][1:], ("busy", 1))
		self.worker.cancel()
		self.assertEqual(events[1][1:], ("cancel", None))
		self.worker.set_diagnostic(None)

	def test_diagnostic_pcm_is_recorded_after_speech_resumes(self):
		pcm_seen = threading.Event()
		self.worker.set_diagnostic(lambda event: pcm_seen.set() if event[1] == "pcm" else None)
		self.worker.pause(True)
		self.worker.speak(["blocked"], speed=1.0, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		self.engine.release.set()
		self.assertFalse(pcm_seen.wait(0.2))
		self.worker.pause(False)
		self.assertTrue(pcm_seen.wait(5))
		self.worker.set_diagnostic(None)

	def test_diagnostic_records_model_reloads(self):
		started = threading.Event()
		release = threading.Event()
		finished = threading.Event()
		events = []

		def observer(event):
			events.append(event)
			if event[1] == "model_done":
				finished.set()

		def load():
			started.set()
			if not release.wait(5):
				raise TimeoutError("test did not release model load")
			return FakeEngine()

		self.worker.set_diagnostic(observer)
		self.worker.reload(load)
		try:
			self.assertTrue(started.wait(5))
		finally:
			release.set()
		self.assertTrue(finished.wait(5))
		self.assertEqual([event[1] for event in events], ["cancel", "model_start", "model_done"])
		self.assertLess(events[1][0], events[2][0])
		self.worker.set_diagnostic(None)

	def test_order_pause_and_playback_timed_notifications(self):
		self.worker.speak(["first", ("index", 7), ("break", 10), "second", ("index", 8)], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(6))
		feeds = self.player.feeds
		self.assertEqual(
			[item[0] for item in feeds], [b"first", b"\0\0", bytes(20), b"second", b"\0\0", b"\0\0"]
		)
		self.assertEqual(self.indices, [])
		self.assertEqual(self.done, [])
		feeds[1][1]()
		feeds[4][1]()
		feeds[5][1]()
		self.assertEqual(self.indices, [7, 8])
		self.assertEqual(self.done, [True])
		self.assertEqual(self.errors, [])

	def test_cancel_invalidates_late_audio_and_callbacks(self):
		self.worker.speak(["first", ("index", 7), "blocked"], speed=1.0, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		self.assertTrue(self.player.wait_for_feeds(2))
		old_callback = self.player.feeds[1][1]
		self.worker.cancel()
		self.worker.speak(["new"], speed=1.0, steps=4)
		self.engine.release.set()
		self.assertTrue(self.player.wait_for_feeds(4))
		old_callback()
		self.assertNotIn(b"blocked", [item[0] for item in self.player.feeds])
		self.assertEqual(self.indices, [])
		self.assertGreaterEqual(self.player.stops, 1)
		self.assertEqual(self.errors, [])

	def test_prior_completion_does_not_end_a_newer_request(self):
		self.worker.speak(["first"], speed=1.0, steps=4)
		self.worker.speak(["second"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(4))
		self.player.feeds[1][1]()
		self.assertEqual(self.done, [])
		self.player.feeds[3][1]()
		self.assertEqual(self.done, [True])

	def test_diagnostic_finishes_each_queued_job_without_extra_done_notifications(self):
		events = []
		self.worker.set_diagnostic(events.append)
		self.worker.speak(["first"], speed=1.0, steps=4)
		self.worker.speak(["second"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(4))
		self.player.feeds[1][1]()
		self.assertEqual(self.done, [])
		self.player.feeds[3][1]()
		self.assertEqual(self.done, [True])
		self.assertEqual([event[2] for event in events if event[1] == "done"], [1, 2])

	def test_new_speech_waits_until_cancellation_stops_playback(self):
		self.worker.speak(["first"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(2))
		self.player.stop_release.clear()
		stopped = threading.Thread(target=self.worker.cancel)
		queued = threading.Event()
		new_speech = threading.Thread(
			target=lambda: (self.worker.speak(["new"], speed=1.0, steps=4), queued.set())
		)
		try:
			stopped.start()
			self.assertTrue(self.player.stop_started.wait(5))
			new_speech.start()
			self.assertFalse(queued.wait(0.1))
		finally:
			self.player.stop_release.set()
			stopped.join(5)
			if new_speech.ident is not None:
				new_speech.join(5)
		self.assertTrue(queued.is_set())

	def test_pause_during_model_loading_holds_speech_until_resume(self):
		started, ready, player_ready = (threading.Event() for _ in range(3))
		player = FakePlayer()

		def engine_factory():
			started.set()
			if not ready.wait(5):
				raise TimeoutError("test did not release model loading")
			return FakeEngine()

		def player_factory(rate):
			player_ready.set()
			return player

		worker = self.module.SpeechWorker(
			engine_factory, player_factory, lambda index: None, lambda: None, self.errors.append
		)
		self.addCleanup(worker.close)
		self.addCleanup(ready.set)
		self.assertTrue(started.wait(5))
		worker.pause(True)
		worker.speak(["paused"], speed=1.0, steps=4)
		ready.set()
		self.assertTrue(player_ready.wait(5))
		self.assertFalse(player.wait_for_feeds(1, timeout=0.1))
		worker.pause(False)
		self.assertTrue(player.wait_for_feeds(2))
		self.assertEqual(self.errors, [])

	def test_cancel_while_paused_discards_old_speech_and_unblocks_new_speech(self):
		self.worker.pause(True)
		self.worker.speak(["old"], speed=1.0, steps=4)
		self.assertFalse(self.player.wait_for_feeds(1, timeout=0.1))
		self.worker.cancel()
		self.worker.speak(["new"], speed=1.0, steps=4)
		self.assertTrue(self.player.wait_for_feeds(2))
		self.assertEqual(self.player.feeds[0][0], b"new")
		self.assertEqual(self.errors, [])

	def test_leading_explicit_break_is_not_trimmed(self):
		class TrimmingPlayer(FakePlayer):
			trimming = True

			def startTrimmingLeadingSilence(self, start=True):
				self.trimming = start

			def feed(self, data, *, onDone=None):
				if self.trimming and data and not any(data):
					data = b"\0\0"
				super().feed(data, onDone=onDone)

		player = TrimmingPlayer()
		worker = self.module.SpeechWorker(
			FakeEngine, lambda rate: player, lambda index: None, lambda: None, self.errors.append
		)
		self.addCleanup(worker.close)
		worker.speak([("break", 10), "speech"], speed=1.0, steps=4)
		self.assertTrue(player.wait_for_feeds(3))
		self.assertEqual(player.feeds[0][0], bytes(20))

	def test_close_waits_for_inference_and_releases_model(self):
		self.worker.speak(["blocked"], speed=1.0, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		closed = threading.Event()
		closer = threading.Thread(target=lambda: (self.worker.close(), closed.set()))
		try:
			closer.start()
			self.assertFalse(closed.wait(1.2))
		finally:
			self.engine.release.set()
			closer.join(5)
		self.assertTrue(closed.is_set())
		self.assertTrue(self.engine.closed)

	def test_close_waits_for_error_cleanup(self):
		started, release, closed = (threading.Event() for _ in range(3))

		class FailingEngine(FakeEngine):
			def stream(self, text, *, speed, steps, seed=0, voice="noa"):
				if text == "fail":
					raise RuntimeError("synthesis failed")
				yield from super().stream(text, speed=speed, steps=steps, seed=seed, voice=voice)

		engine = FailingEngine()

		def on_error(error):
			started.set()
			if not release.wait(5):
				raise TimeoutError("test did not release error handling")

		worker = self.module.SpeechWorker(
			lambda: engine, lambda rate: FakePlayer(), lambda index: None, lambda: None, on_error
		)
		worker.speak(["fail"], speed=1.0, steps=4)
		closer = threading.Thread(target=lambda: (worker.close(), closed.set()))
		try:
			self.assertTrue(started.wait(5))
			closer.start()
			self.assertFalse(closed.wait(0.1))
		finally:
			release.set()
			if closer.ident is not None:
				closer.join(5)
			worker.close()
		self.assertTrue(closed.is_set())
		self.assertTrue(engine.closed)

	def test_volume_applies_before_and_after_player_initialization(self):
		started, ready, player_ready = (threading.Event() for _ in range(3))
		player = FakePlayer()

		def engine_factory():
			started.set()
			if not ready.wait(5):
				raise TimeoutError("test did not release model loading")
			return FakeEngine()

		def player_factory(rate):
			player_ready.set()
			return player

		worker = self.module.SpeechWorker(
			engine_factory, player_factory, lambda index: None, lambda: None, self.errors.append
		)
		self.addCleanup(worker.close)
		self.addCleanup(ready.set)
		self.assertTrue(started.wait(5))
		worker.set_volume(0)
		worker.speak(["one"], speed=1.0, steps=4)
		ready.set()
		self.assertTrue(player_ready.wait(5))
		self.assertTrue(player.wait_for_feeds(2))
		self.assertEqual(player.volumes, [0.0])
		worker.set_volume(25)
		worker.set_volume(70)
		worker.set_volume(0)
		self.assertEqual(player.volumes, [0.0, 0.25, 0.7, 0.0])

	def test_voice_is_snapshotted_per_queued_request(self):
		self.worker.speak(["first"], speed=1, steps=4, voice="noa")
		self.worker.speak(["second"], speed=1, steps=4, voice="adam")
		self.assertTrue(self.player.wait_for_feeds(4))
		self.assertEqual(self.engine.calls, [("first", "noa"), ("second", "adam")])

	def test_sonic_is_snapshotted_and_flushes_before_index_and_break(self):
		processor = ModuleType("shravya_worker_test._sonic_audio")
		rates = []

		def process_audio(chunks, sample_rate, speed, cancelled, *, pitch=1.0):
			rates.append((sample_rate, speed))
			for chunk in chunks:
				yield b"sonic:" + chunk
			yield b"tail"

		processor.process_audio = process_audio
		with patch.dict(sys.modules, {"shravya_worker_test._sonic_audio": processor}):
			self.worker.speak(["blocked", ("index", 7), ("break", 10)], speed=1.5, steps=4, method="sonic")
			self.assertTrue(self.engine.blocked.wait(5))
			self.worker.speak(["new"], speed=2.0, steps=4, method="native")
			self.engine.release.set()
			self.assertTrue(self.player.wait_for_feeds(7))
		self.assertEqual(
			[item[0] for item in self.player.feeds],
			[b"sonic:blocked", b"tail", b"\0\0", bytes(20), b"\0\0", b"new", b"\0\0"],
		)
		self.assertEqual(self.engine.speeds, [1.0, 2.0])
		self.assertEqual(rates, [(1000, 1.5)])
		self.assertEqual(self.errors, [])

	def test_pitch_commands_apply_to_following_text_without_changing_native_rate(self):
		processor = ModuleType("shravya_worker_test._sonic_audio")
		processed = []

		def process_audio(chunks, sample_rate, speed, cancelled, *, pitch=1.0):
			processed.append((speed, pitch))
			for chunk in chunks:
				yield b"pitched:" + chunk

		processor.process_audio = process_audio
		with patch.dict(sys.modules, {"shravya_worker_test._sonic_audio": processor}):
			self.worker.speak(
				["normal", ("pitch", 80), "capital", ("pitch", 50), "normal"],
				speed=1.5,
				steps=4,
				pitch=50,
			)
			self.assertTrue(self.player.wait_for_feeds(4))
		self.assertEqual(
			[item[0] for item in self.player.feeds],
			[b"normal", b"pitched:capital", b"normal", b"\0\0"],
		)
		self.assertEqual(self.engine.speeds, [1.5, 1.5, 1.5])
		self.assertEqual(processed, [(1.0, 2**0.3)])
		self.assertEqual(self.errors, [])

	def test_sonic_rate_and_pitch_are_applied_together(self):
		processor = ModuleType("shravya_worker_test._sonic_audio")
		processed = []

		def process_audio(chunks, sample_rate, speed, cancelled, *, pitch=1.0):
			processed.append((speed, pitch))
			yield from chunks

		processor.process_audio = process_audio
		with patch.dict(sys.modules, {"shravya_worker_test._sonic_audio": processor}):
			self.worker.speak(["first"], speed=2.0, steps=4, method="sonic", pitch=80)
			self.assertTrue(self.player.wait_for_feeds(2))
		self.assertEqual(processed, [(2.0, 2**0.3)])
		self.assertEqual(self.engine.speeds, [1.0])
		self.assertEqual(self.errors, [])

	def test_reload_waits_for_inference_and_preserves_new_requests_after_cancel(self):
		replacement = FakeEngine()
		created = threading.Event()

		def factory():
			self.assertTrue(self.engine.closed)
			created.set()
			return replacement

		self.worker.speak(["blocked"], speed=1, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		self.worker.reload(factory)
		self.worker.cancel()
		self.worker.speak(["new"], speed=1, steps=4)
		self.assertFalse(created.is_set())
		self.engine.release.set()
		self.assertTrue(created.wait(5))
		self.assertTrue(self.player.wait_for_feeds(2))
		self.assertEqual([item[0] for item in self.player.feeds], [b"new", b"\0\0"])
		self.assertEqual(replacement.calls, [("new", "noa")])
		self.assertEqual(self.errors, [])

	def test_latest_pending_reload_supersedes_older_reload(self):
		replacement = FakeEngine()
		unused = []
		self.worker.speak(["blocked"], speed=1, steps=4)
		self.assertTrue(self.engine.blocked.wait(5))
		self.worker.reload(lambda: unused.append(True))
		self.worker.reload(lambda: replacement)
		self.worker.speak(["new"], speed=1, steps=4)
		self.engine.release.set()
		self.assertTrue(self.player.wait_for_feeds(2))
		self.assertEqual(unused, [])
		self.assertEqual(replacement.calls, [("new", "noa")])
		self.assertEqual(self.errors, [])

	def test_volume_change_during_reload_applies_to_new_player(self):
		started, ready = threading.Event(), threading.Event()
		player = FakePlayer()

		def factory():
			started.set()
			if not ready.wait(5):
				raise TimeoutError("test did not release reload")
			return FakeEngine()

		worker = self.module.SpeechWorker(
			FakeEngine, lambda rate: player, lambda index: None, lambda: None, self.errors.append
		)
		self.addCleanup(worker.close)
		self.addCleanup(ready.set)
		worker.reload(factory)
		self.assertTrue(started.wait(5))
		worker.set_volume(35)
		worker.speak(["new"], speed=1, steps=4)
		ready.set()
		self.assertTrue(player.wait_for_feeds(2))
		self.assertEqual(player.volumes[-1], 0.35)
		self.assertEqual(self.errors, [])

	def test_cuda_failure_stops_old_speech_and_loads_cpu_without_replay(self):
		failing, release_failure, loading, ready = (threading.Event() for _ in range(4))
		cpu = FakeEngine()
		player = FakePlayer()
		failures = []
		error_type = self.module.CudaSynthesisError

		class Gpu(FakeEngine):
			def stream(self, text, *, speed, steps, seed=0, voice="noa"):
				if text == "fail":
					failing.set()
					if not release_failure.wait(5):
						raise TimeoutError("test did not release GPU failure")
					raise error_type("CUDA failed")
				yield from super().stream(text, speed=speed, steps=steps, seed=seed, voice=voice)

		gpu = Gpu()

		def recover(error):
			self.assertTrue(gpu.closed)
			failures.append(error)
			loading.set()
			if not ready.wait(5):
				raise TimeoutError("test did not release CPU loading")
			return cpu

		worker = self.module.SpeechWorker(
			lambda: gpu,
			lambda rate: player,
			self.indices.append,
			lambda: self.done.append(True),
			self.errors.append,
			on_cuda_failure=recover,
		)
		self.addCleanup(worker.close)
		self.addCleanup(ready.set)
		self.addCleanup(release_failure.set)
		worker.speak(["first", ("index", 7), "fail"], speed=1, steps=4)
		self.assertTrue(failing.wait(5))
		old_callback = player.feeds[1][1]
		worker.speak(["obsolete"], speed=1, steps=4)
		release_failure.set()
		self.assertTrue(loading.wait(5))
		old_callback()
		worker.speak(["new"], speed=1, steps=4)
		ready.set()
		self.assertTrue(player.wait_for_feeds(4))
		self.assertEqual(cpu.calls, [("new", "noa")])
		self.assertEqual(self.indices, [])
		self.assertEqual(len(failures), 1)
		self.assertEqual(self.errors, [])

	def test_cuda_failure_after_cancellation_keeps_new_speech(self):
		failing, release = threading.Event(), threading.Event()
		cpu = FakeEngine()
		player = FakePlayer()
		error_type = self.module.CudaSynthesisError

		class Gpu(FakeEngine):
			def stream(self, text, *, speed, steps, seed=0, voice="noa"):
				failing.set()
				if not release.wait(5):
					raise TimeoutError("test did not release GPU failure")
				raise error_type("CUDA failed")
				yield

		worker = self.module.SpeechWorker(
			Gpu,
			lambda rate: player,
			lambda index: None,
			lambda: None,
			self.errors.append,
			on_cuda_failure=lambda error: cpu,
		)
		self.addCleanup(worker.close)
		self.addCleanup(release.set)
		worker.speak(["old"], speed=1, steps=4)
		self.assertTrue(failing.wait(5))
		worker.cancel()
		worker.speak(["new"], speed=1, steps=4)
		release.set()
		self.assertTrue(player.wait_for_feeds(2))
		self.assertEqual(cpu.calls, [("new", "noa")])
		self.assertEqual(self.errors, [])
