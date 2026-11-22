"""Own one on-demand, in-memory output capture session."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ._audio_onset import OutputOnset
from ._diagnostic_measurements import TimingEvent
from ._loopback import AudioFormat, capture_packets


class DiagnosticCapture:
	def __init__(
		self,
		endpoint_id: str | None,
		on_ready: Callable[[str], None],
		on_error: Callable[[Exception], None],
	) -> None:
		super().__init__()
		self._endpoint_id = endpoint_id
		self._on_ready = on_ready
		self._on_error = on_error
		self._stop = threading.Event()
		self._lock = threading.Lock()
		self._events: list[TimingEvent] = []
		self._error: Exception | None = None
		self._format: AudioFormat | None = None
		self._onset: OutputOnset | None = None
		self._last_start: int | None = None
		self._last_end: int | None = None
		self._quiet = False
		self._thread = threading.Thread(target=self._run, name="Shravya output capture", daemon=True)

	def start(self) -> None:
		self._thread.start()

	def record(self, event: TimingEvent) -> None:
		with self._lock:
			if not self._stop.is_set():
				self._events.append(event)

	def stop(self) -> list[TimingEvent]:
		self._stop.set()
		self._thread.join()
		if self._error is not None:
			raise self._error
		with self._lock:
			return self._events.copy()

	def _append(self, event: TimingEvent) -> None:
		with self._lock:
			self._events.append(event)

	def _ready(self, format: AudioFormat, actual_id: str) -> None:
		self._format = format
		self._onset = OutputOnset(
			sample_rate=format.sample_rate,
			channels=format.channels,
			encoding=format.encoding,
		)
		if not self._stop.is_set():
			self._on_ready(actual_id)

	def _packet(self, data: bytes, frames: int, flags: int, qpc_ns: int) -> None:
		if self._format is None or self._onset is None:
			raise RuntimeError("Audio capture delivered a packet before its format")
		try:
			if self._last_start is not None and qpc_ns <= self._last_start:
				qpc_ns = 0
			onset = self._onset.process(data, frames=frames, flags=flags, qpc_ns=qpc_ns)
		except ValueError:
			self._quiet = False
			self._append((self._last_end or time.perf_counter_ns(), "invalid", None))
			return
		self._last_start = qpc_ns
		self._last_end = qpc_ns + frames * 1_000_000_000 // self._format.sample_rate
		if self._onset.quiet and not self._quiet:
			self._append((self._last_end, "quiet", None))
		elif onset is not None:
			self._append((onset, "onset", None))
		self._quiet = self._onset.quiet

	def _run(self) -> None:
		try:
			capture_packets(self._endpoint_id, self._stop, self._ready, self._packet)
		except Exception as error:
			self._error = error
			self._on_error(error)
