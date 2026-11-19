import ctypes
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

SONIC_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_sonic_audio.py"


class FakeSonicStream:
	instances = []

	def __init__(self, sampleRate, channels):
		self.rate = sampleRate
		self.channels = channels
		self.speed = 1.0
		self.pitch = 1.0
		self.pending = b""
		self.ready = b""
		self.instances.append(self)

	def writeShort(self, data, numSamples):
		self.pending += ctypes.string_at(data, numSamples * 2)
		if len(self.pending) > 2:
			self.ready = self.pending[:-2]
			self.pending = self.pending[-2:]

	def readShort(self):
		output, self.ready = self.ready, b""
		return output

	def flush(self):
		self.ready += self.pending
		self.pending = b""


class TestSonicAudio(unittest.TestCase):
	def setUp(self):
		FakeSonicStream.instances.clear()
		self.initialize_calls = []
		synth_drivers = ModuleType("synthDrivers")
		synth_drivers.__path__ = []
		sonic = ModuleType("synthDrivers._sonic")
		sonic.initialize = lambda: self.initialize_calls.append(True)
		sonic.SonicStream = FakeSonicStream
		with patch.dict(sys.modules, {"synthDrivers": synth_drivers, "synthDrivers._sonic": sonic}):
			spec = importlib.util.spec_from_file_location("sonic_audio_test", SONIC_PATH)
			self.module = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(self.module)

	def test_streams_and_flushes_all_audio(self):
		audio = list(self.module.process_audio([b"\x01\x00\x02\x00", b"\x03\x00"], 44100, 1.5, lambda: False))
		self.assertEqual(audio, [b"\x01\x00", b"\x02\x00", b"\x03\x00"])
		stream = FakeSonicStream.instances[0]
		self.assertEqual((stream.rate, stream.channels, stream.speed), (44100, 1, 1.5))
		self.assertEqual(self.initialize_calls, [True])

	def test_rejects_misaligned_pcm(self):
		with self.assertRaises(ValueError):
			list(self.module.process_audio([b"\x00"], 44100, 1.5, lambda: False))

	def test_pitch_can_be_changed_without_changing_rate(self):
		list(self.module.process_audio([b"\x01\x00"], 44100, 1.0, lambda: False, pitch=1.23))
		stream = FakeSonicStream.instances[0]
		self.assertEqual((stream.speed, stream.pitch), (1.0, 1.23))

	def test_cancellation_stops_before_requesting_another_chunk(self):
		cancelled = threading.Event()

		def chunks():
			yield b"\x01\x00"
			self.fail("requested another inference chunk after cancellation")

		def cancel_after_write(stream, data, count):
			cancelled.set()

		with patch.object(FakeSonicStream, "writeShort", cancel_after_write):
			self.assertEqual(list(self.module.process_audio(chunks(), 44100, 1.5, cancelled.is_set)), [])
