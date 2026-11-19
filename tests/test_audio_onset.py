import importlib.util
import struct
import unittest
from pathlib import Path

ONSET_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_audio_onset.py"


def load_onset():
	spec = importlib.util.spec_from_file_location("shravya_audio_onset_test", ONSET_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestAudioOnset(unittest.TestCase):
	def setUp(self):
		self.onset = load_onset().first_loud_frame

	def test_finds_first_loud_stereo_frame_in_float_audio(self):
		data = struct.pack("<6f", 0.0, 0.0, 0.001, -0.2, 0.4, 0.0)
		self.assertEqual(self.onset(data, channels=2, encoding="float32", level=0.01), 1)

	def test_finds_negative_24_bit_pcm_sample(self):
		data = b"\x00\x00\x00\x00\x00\xc0"
		self.assertEqual(self.onset(data, channels=1, encoding="pcm24", level=0.25), 1)

	def test_silence_and_short_integer_samples(self):
		self.assertIsNone(
			self.onset(struct.pack("<4h", 0, 1, -1, 0), channels=2, encoding="pcm16", level=0.01)
		)

	def test_signed_integer_formats_handle_minimum_samples(self):
		for encoding, data in (
			("pcm16", struct.pack("<2h", 0, -32768)),
			("pcm32", struct.pack("<2i", 0, -2147483648)),
		):
			with self.subTest(encoding=encoding):
				self.assertEqual(self.onset(data, channels=1, encoding=encoding, level=0.5), 1)

	def test_rejects_bad_frames_and_nonfinite_audio(self):
		with self.assertRaises(ValueError):
			self.onset(b"\x00", channels=2, encoding="pcm16")
		with self.assertRaises(ValueError):
			self.onset(struct.pack("<f", float("nan")), channels=1, encoding="float32")


class TestPacketOnset(unittest.TestCase):
	def setUp(self):
		self.tracker = load_onset().OutputOnset(sample_rate=1000, channels=1, encoding="pcm16")
		self.silence = struct.pack("<4h", 0, 0, 0, 0)
		self.sound = struct.pack("<4h", 0, 0, 1000, 1000)

	def test_timestamp_uses_packet_clock_and_first_loud_frame(self):
		self.assertIsNone(self.tracker.process(self.silence, frames=4, flags=0, qpc_ns=900_000_000))
		self.assertEqual(
			self.tracker.process(self.sound, frames=4, flags=0, qpc_ns=1_000_000_000), 1_002_000_000
		)
		self.assertIsNone(self.tracker.process(self.sound, frames=4, flags=0, qpc_ns=1_004_000_000))

	def test_requires_observed_silence_before_detecting_sound(self):
		self.assertIsNone(self.tracker.process(self.sound, frames=4, flags=0, qpc_ns=1_000_000_000))
		self.assertIsNone(self.tracker.process(b"", frames=4, flags=2, qpc_ns=1_004_000_000))
		self.assertEqual(
			self.tracker.process(self.sound, frames=4, flags=0, qpc_ns=1_008_000_000), 1_010_000_000
		)

	def test_invalid_packets_invalidate_the_silence_baseline(self):
		for flags in (1, 4):
			with self.subTest(flags=flags):
				self.tracker.process(self.silence, frames=4, flags=0, qpc_ns=1_000_000_000)
				with self.assertRaises(ValueError):
					self.tracker.process(self.silence, frames=4, flags=flags, qpc_ns=1_004_000_000)
				self.assertIsNone(self.tracker.process(self.sound, frames=4, flags=0, qpc_ns=1_008_000_000))

	def test_rejects_bad_packet_shape_and_timestamp(self):
		with self.assertRaises(ValueError):
			self.tracker.process(self.silence, frames=3, flags=0, qpc_ns=1_000_000_000)
		with self.assertRaises(ValueError):
			self.tracker.process(self.silence, frames=4, flags=0, qpc_ns=0)


if __name__ == "__main__":
	unittest.main()
