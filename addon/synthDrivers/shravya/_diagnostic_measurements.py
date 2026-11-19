"""Associate captured timing events conservatively after capture has stopped."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, NamedTuple

type Workload = Literal["controlled", "live"]
type TimingEvent = tuple[int, str, int | str | None]


class Summary(NamedTuple):
	latencies_ns: list[int]
	queue_ns: list[int]
	synthesis_ns: list[int]
	interrupted: int
	ambiguous: int


class _Attempt:
	def __init__(self, workload: Workload, key_ns: int) -> None:
		super().__init__()
		self.workload = workload
		self.key_ns = key_ns
		self.job: int | None = None
		self.queued_ns: int | None = None
		self.started_ns: int | None = None
		self.pcm_ns: int | None = None
		self.onset_ns: int | None = None
		self.done_ns: int | None = None
		self.interrupted = False
		self.ambiguous = False


def summarize(events: Iterable[TimingEvent]) -> dict[Workload, Summary]:
	attempts: list[_Attempt] = []
	active: dict[int, _Attempt | None] = {}
	current: _Attempt | None = None
	output_quiet = False
	for when, kind, value in sorted(events, key=lambda event: event[0]):
		if kind == "key":
			if value not in ("controlled", "live"):
				raise ValueError("Invalid diagnostic workload")
			pending_key = current is not None and current.job is None and not current.interrupted
			if current is not None and current.done_ns is None and not current.interrupted:
				current.ambiguous = True
			for attempt in active.values():
				if attempt is not None:
					attempt.ambiguous = True
			current = _Attempt(value, when)
			current.ambiguous = bool(active) or pending_key or not output_quiet
			attempts.append(current)
		elif kind == "queued":
			if not isinstance(value, int) or value in active:
				raise ValueError("Invalid diagnostic speech job")
			for attempt in active.values():
				if attempt is not None:
					attempt.ambiguous = True
			if current is not None and current.job is None:
				current.job = value
				current.queued_ns = when
				current.ambiguous |= bool(active)
				active[value] = current
			else:
				if current is not None and current.done_ns is None:
					current.ambiguous = True
				active[value] = None
		elif kind in ("started", "pcm", "done"):
			if not isinstance(value, int):
				raise ValueError("Invalid diagnostic speech job")
			attempt = active.get(value)
			if attempt is not None:
				if kind == "started":
					if attempt.started_ns is not None:
						attempt.ambiguous = True
					attempt.started_ns = when
				elif kind == "pcm":
					if attempt.pcm_ns is None:
						attempt.pcm_ns = when
				else:
					attempt.done_ns = when
			if kind == "done":
				_ = active.pop(value, None)
		elif kind == "busy":
			if not isinstance(value, int):
				raise ValueError("Invalid diagnostic speech job")
			active[value] = None
		elif kind == "cancel":
			for attempt in active.values():
				if attempt is not None:
					attempt.interrupted = True
			active.clear()
		elif kind == "invalid":
			output_quiet = False
			if current is not None and current.done_ns is None:
				current.ambiguous = True
			for attempt in active.values():
				if attempt is not None:
					attempt.ambiguous = True
		elif kind == "quiet":
			output_quiet = True
		elif kind == "onset":
			output_quiet = False
			if len(active) != 1:
				for attempt in active.values():
					if attempt is not None:
						attempt.ambiguous = True
				if current is not None and current.done_ns is None:
					current.ambiguous = True
			else:
				attempt = next(iter(active.values()))
				if attempt is not None:
					if attempt.pcm_ns is None:
						attempt.ambiguous = True
					elif attempt.onset_ns is None:
						attempt.onset_ns = when
		elif kind in ("model_start", "model_done"):
			continue
		else:
			raise ValueError(f"Unknown diagnostic event: {kind}")

	results: dict[Workload, Summary] = {}
	for workload in ("controlled", "live"):
		latencies: list[int] = []
		queue: list[int] = []
		synthesis: list[int] = []
		interrupted = ambiguous = 0
		for attempt in attempts:
			if attempt.workload != workload:
				continue
			if attempt.interrupted:
				interrupted += 1
				continue
			queued = attempt.queued_ns
			started = attempt.started_ns
			pcm = attempt.pcm_ns
			onset = attempt.onset_ns
			done = attempt.done_ns
			if (
				attempt.ambiguous
				or queued is None
				or started is None
				or pcm is None
				or onset is None
				or done is None
				or not attempt.key_ns <= queued <= started <= pcm <= onset <= done
			):
				ambiguous += 1
				continue
			latencies.append(onset - attempt.key_ns)
			queue.append(started - queued)
			synthesis.append(pcm - started)
		results[workload] = Summary(latencies, queue, synthesis, interrupted, ambiguous)
	return results


def model_loads_ns(events: Iterable[TimingEvent]) -> list[int]:
	loads: list[int] = []
	started: int | None = None
	for when, kind, _ in sorted(events, key=lambda event: event[0]):
		if kind == "model_start":
			started = when
		elif kind == "model_done" and started is not None:
			loads.append(when - started)
			started = None
	return loads
