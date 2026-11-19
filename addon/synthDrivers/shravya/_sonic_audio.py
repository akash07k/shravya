"""Stream mono PCM through NVDA's Sonic audio processor."""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterable, Iterator

from synthDrivers._sonic import SonicStream, initialize


def process_audio(
	chunks: Iterable[bytes],
	sample_rate: int,
	speed: float,
	cancelled: Callable[[], bool],
	*,
	pitch: float = 1.0,
) -> Iterator[bytes]:
	if cancelled():
		return
	initialize()
	stream = SonicStream(sample_rate, 1)
	stream.speed = speed
	stream.pitch = pitch
	for chunk in chunks:
		if cancelled():
			return
		if len(chunk) % 2:
			raise ValueError("Sonic requires 16-bit mono PCM")
		if chunk:
			samples = (ctypes.c_short * (len(chunk) // 2)).from_buffer_copy(chunk)
			stream.writeShort(ctypes.cast(samples, ctypes.POINTER(ctypes.c_short)), len(samples))
			output = bytes(stream.readShort())
			if output:
				yield output
		if cancelled():
			return
	stream.flush()
	output = bytes(stream.readShort())
	if output:
		yield output
