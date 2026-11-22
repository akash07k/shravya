import importlib.util
import struct
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

LOOPBACK_PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_loopback.py"


def load_loopback():
	spec = importlib.util.spec_from_file_location("shravya_loopback_test", LOOPBACK_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def wave_format(tag, channels, rate, bits, extra=b""):
	align = channels * (bits // 8)
	return struct.pack("<HHIIHHH", tag, channels, rate, rate * align, align, bits, len(extra)) + extra


class TestLoopbackFormat(unittest.TestCase):
	def setUp(self):
		self.describe = load_loopback().describe_mix_format

	def test_extensible_float_stereo(self):
		guid = uuid.UUID("{00000003-0000-0010-8000-00aa00389b71}").bytes_le
		data = wave_format(0xFFFE, 2, 48000, 32, struct.pack("<HI", 32, 3) + guid)
		format = self.describe(data)
		self.assertEqual(
			(format.sample_rate, format.channels, format.encoding, format.frame_bytes),
			(48000, 2, "float32", 8),
		)

	def test_pcm24_mono(self):
		format = self.describe(wave_format(1, 1, 44100, 24))
		self.assertEqual(
			(format.sample_rate, format.channels, format.encoding, format.frame_bytes), (44100, 1, "pcm24", 3),
		)

	def test_rejects_truncated_and_unsupported_formats(self):
		with self.assertRaises(ValueError):
			self.describe(b"\0" * 17)
		with self.assertRaises(ValueError):
			self.describe(wave_format(0xFFFE, 2, 48000, 32))
		with self.assertRaises(ValueError):
			self.describe(wave_format(3, 2, 48000, 64))
		with self.assertRaises(ValueError):
			self.describe(wave_format(1, 0, 48000, 16))
		with self.assertRaises(ValueError):
			self.describe(wave_format(1, 2, 48000, 16)[:-1])
		with self.assertRaises(ValueError):
			data = bytearray(wave_format(1, 2, 48000, 16))
			struct.pack_into("<H", data, 12, 2)
			self.describe(bytes(data))


class TestLoopbackThread(unittest.TestCase):
	@unittest.skipUnless(sys.platform == "win32", "Endpoint notifications require Windows")
	def test_device_change_monitor_registers_and_signals(self):
		from pycaw.api.mmdeviceapi import IMMNotificationClient
		from pycaw.utils import AudioUtilities

		module = load_loopback()
		monitor = module._device_change_monitor()
		notification = monitor.QueryInterface(IMMNotificationClient)
		enumerator = AudioUtilities.GetDeviceEnumerator()
		enumerator.RegisterEndpointNotificationCallback(notification)
		try:
			notification.OnDefaultDeviceChanged(1, 0, "capture-endpoint")
			self.assertFalse(monitor.changed)
			notification.OnDefaultDeviceChanged(0, 0, "endpoint")
			self.assertTrue(monitor.changed)
			monitor.changed = False
			notification.OnDeviceStateChanged("endpoint", 2)
			self.assertTrue(monitor.changed)
		finally:
			enumerator.UnregisterEndpointNotificationCallback(notification)

	@unittest.skipUnless(sys.platform == "win32", "COM initialization requires Windows")
	def test_capture_initializes_after_comtypes_is_first_imported_on_capture_thread(self):
		script = """
import importlib.util
import sys
import threading
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("loopback", sys.argv[1])
loopback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loopback)
errors = []

def run():
    try:
        from pycaw.utils import AudioUtilities
        with patch.object(AudioUtilities, "GetDeviceEnumerator", side_effect=LookupError("expected")):
            loopback.capture_packets(None, threading.Event(), lambda *args: None, lambda *args: None)
        errors.append("capture unexpectedly succeeded")
    except LookupError:
        pass
    except Exception as error:
        errors.append(repr(error))

thread = threading.Thread(target=run)
thread.start()
thread.join(5)
if thread.is_alive() or errors:
    raise RuntimeError(f"capture thread failed: {errors}")
"""
		result = subprocess.run(
			[sys.executable, "-B", "-c", script, str(LOOPBACK_PATH)],
			capture_output=True,
			text=True,
			timeout=15,
		)
		self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
	unittest.main()
