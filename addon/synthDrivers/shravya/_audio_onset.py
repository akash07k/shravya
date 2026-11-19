"""Find the first level-threshold crossing in a captured output packet."""

from __future__ import annotations

from typing import Literal

import numpy as np

type SampleEncoding = Literal["float32", "pcm16", "pcm24", "pcm32"]


def first_loud_frame(
	data: bytes,
	*,
	channels: int,
	encoding: SampleEncoding,
	level: float = 0.003,
) -> int | None:
	if channels < 1 or not 0 < level <= 1:
		raise ValueError("Invalid channel count or onset level")
	widths = {"float32": 4, "pcm16": 2, "pcm24": 3, "pcm32": 4}
	if encoding not in widths:
		raise ValueError(f"Unsupported audio encoding: {encoding}")
	width = widths[encoding]
	if len(data) % (channels * width):
		raise ValueError("Captured audio contains an incomplete frame")
	if not data:
		return None

	if encoding == "pcm24":
		raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
		integers = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
		samples = ((integers ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
	else:
		dtype, scale = {
			"float32": ("<f4", 1),
			"pcm16": ("<i2", 32768),
			"pcm32": ("<i4", 2147483648),
		}[encoding]
		samples = np.frombuffer(data, dtype=dtype).astype(np.float32) / scale
	if not np.isfinite(samples).all():
		raise ValueError("Captured audio contains non-finite samples")
	loud = np.flatnonzero((np.abs(samples.reshape(-1, channels)) >= level).any(axis=1))
	return int(loud[0]) if loud.size else None


class OutputOnset:
	"""Detect a candidate onset after a whole quiet output packet."""

	def __init__(self, *, sample_rate: int, channels: int, encoding: SampleEncoding) -> None:
		super().__init__()
		self._encoding: SampleEncoding = encoding
		widths = {"float32": 4, "pcm16": 2, "pcm24": 3, "pcm32": 4}
		if sample_rate < 1 or channels < 1 or encoding not in widths:
			raise ValueError("Invalid output audio format")
		self._sample_rate = sample_rate
		self._channels = channels
		self._frame_bytes = channels * widths[encoding]
		self._quiet = False

	@property
	def quiet(self) -> bool:
		return self._quiet

	def process(self, data: bytes, *, frames: int, flags: int, qpc_ns: int) -> int | None:
		was_quiet = self._quiet
		self._quiet = False
		if flags & 5 or frames < 1 or qpc_ns <= 0:
			raise ValueError("The output packet has invalid timing or a discontinuity")
		if flags & 2:
			if data:
				raise ValueError("A silent packet must not contain audio")
			self._quiet = True
			return None
		if len(data) != frames * self._frame_bytes:
			raise ValueError("The output packet does not match its frame count")
		frame = first_loud_frame(data, channels=self._channels, encoding=self._encoding)
		if frame is None:
			self._quiet = True
			return None
		if was_quiet:
			return qpc_ns + frame * 1_000_000_000 // self._sample_rate
		return None
