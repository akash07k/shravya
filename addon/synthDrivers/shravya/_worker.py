"""Serialize speech generation and synchronize notifications with playback."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from functools import partial
from typing import Literal, NamedTuple, Protocol

from ._blue import CudaSynthesisError

type SpeechPart = str | tuple[Literal["index", "break", "pitch"], int]
type RateMethod = Literal["native", "sonic"]


class _Engine(Protocol):
	@property
	def sample_rate(self) -> int: ...

	def stream(
		self,
		text: str,
		*,
		speed: float,
		steps: int,
		seed: int,
		voice: str = "noa",
	) -> Iterator[bytes]: ...
	def close(self) -> None: ...


class _Player(Protocol):
	def feed(self, data: bytes, *, onDone: Callable[[], None] | None = None) -> None: ...
	def stop(self) -> None: ...
	def pause(self, switch: bool) -> None: ...
	def setVolume(self, *, all: float) -> None: ...
	def startTrimmingLeadingSilence(self, start: bool = True) -> None: ...
	def idle(self) -> None: ...
	def close(self) -> None: ...


class _Job(NamedTuple):
	epoch: threading.Event
	serial: int
	parts: tuple[SpeechPart, ...]
	speed: float
	steps: int
	seed: int
	voice: str
	method: RateMethod
	pitch: int


class _Reload(NamedTuple):
	engine_factory: Callable[[], _Engine]


class SpeechWorker:
	def __init__(
		self,
		engine_factory: Callable[[], _Engine],
		player_factory: Callable[[int], _Player],
		on_index: Callable[[int], None],
		on_done: Callable[[], None],
		on_error: Callable[[Exception], None],
		on_cuda_failure: Callable[[CudaSynthesisError], _Engine] | None = None,
	) -> None:
		super().__init__()
		self._engine_factory = engine_factory
		self._player_factory = player_factory
		self._on_index = on_index
		self._on_done = on_done
		self._on_error = on_error
		self._on_cuda_failure = on_cuda_failure
		self._control = threading.RLock()
		self._lock = threading.RLock()
		self._epoch = threading.Event()
		self._resumed = threading.Event()
		self._resumed.set()
		self._serial = 0
		self._volume = 100
		self._closed = False
		self._speaking = False
		self._diagnostic: Callable[[tuple[int, str, int | str | None]], None] | None = None
		self._player: _Player | None = None
		self._jobs: queue.Queue[_Job | _Reload | None] = queue.Queue()
		self._thread = threading.Thread(target=self._run, name="Shravya speech", daemon=True)
		self._thread.start()

	def speak(
		self,
		parts: Sequence[SpeechPart],
		*,
		speed: float,
		steps: int,
		seed: int = 0,
		voice: str = "noa",
		method: RateMethod = "native",
		pitch: int = 50,
	) -> None:
		if method not in ("native", "sonic"):
			raise ValueError(f"Unknown rate method: {method}")
		if not 0 <= pitch <= 100:
			raise ValueError("Pitch must be between 0 and 100")
		with self._control:
			with self._lock:
				if self._closed:
					raise RuntimeError("Shravya speech worker is closed")
				self._serial += 1
				self._speaking = True
				self._record("queued", self._serial)
				self._jobs.put(
					_Job(self._epoch, self._serial, tuple(parts), speed, steps, seed, voice, method, pitch),
				)

	def cancel(self) -> None:
		with self._control:
			_ = self._cancel(keep_reload=True)

	def set_diagnostic(self, observer: Callable[[tuple[int, str, int | str | None]], None] | None) -> None:
		with self._control:
			with self._lock:
				self._diagnostic = observer
				if observer is not None and self._speaking:
					self._record("busy", self._serial)

	def _record(self, kind: str, value: int | str | None) -> None:
		with self._lock:
			observer = self._diagnostic
			if observer is not None:
				observer((time.perf_counter_ns(), kind, value))

	def _load(self, factory: Callable[[], _Engine]) -> _Engine:
		self._record("model_start", None)
		engine = factory()
		self._record("model_done", None)
		return engine

	def reload(self, engine_factory: Callable[[], _Engine]) -> None:
		with self._control:
			if not self._cancel(keep_reload=False):
				raise RuntimeError("Shravya speech worker is closed")
			self._jobs.put(_Reload(engine_factory))

	def _cancel(self, *, keep_reload: bool) -> bool:
		with self._lock:
			if self._closed:
				return False
			self._epoch.set()
			self._speaking = False
			self._record("cancel", None)
			self._epoch = threading.Event()
			pending_reload: _Reload | None = None
			while True:
				try:
					item = self._jobs.get_nowait()
				except queue.Empty:
					break
				if isinstance(item, _Reload):
					pending_reload = item
			if keep_reload and pending_reload is not None:
				self._jobs.put(pending_reload)
			player = self._player
		if player is not None:
			player.stop()
		self._resumed.set()
		return True

	def pause(self, switch: bool) -> None:
		with self._control:
			if switch:
				self._resumed.clear()
			with self._lock:
				player = self._player
			if player is not None:
				player.pause(switch)
			if not switch:
				self._resumed.set()

	def set_volume(self, value: int) -> None:
		if not 0 <= value <= 100:
			raise ValueError("Volume must be between 0 and 100")
		with self._control:
			with self._lock:
				self._volume = value
				player = self._player
			if player is not None:
				player.setVolume(all=value / 100)

	def close(self) -> None:
		with self._control:
			with self._lock:
				player = None
				if not self._closed:
					self._closed = True
					self._epoch.set()
					if self._speaking:
						self._record("cancel", None)
						self._speaking = False
					player = self._player
					self._jobs.put(None)
			if player is not None:
				player.stop()
			self._resumed.set()
		if threading.current_thread() is not self._thread:
			self._thread.join()

	def _notify_index(self, epoch: threading.Event, index: int) -> None:
		with self._lock:
			if not self._closed and not epoch.is_set():
				self._on_index(index)

	def _notify_done(self, job: _Job) -> None:
		with self._lock:
			if not self._closed and not job.epoch.is_set():
				self._record("done", job.serial)
				if job.serial == self._serial:
					self._speaking = False
					self._on_done()

	def _feed(
		self,
		player: _Player,
		epoch: threading.Event,
		data: bytes,
		callback: Callable[[], None] | None = None,
		*,
		explicit_break: bool = False,
		first_pcm_job: int | None = None,
	) -> bool:
		_ = self._resumed.wait()
		with self._control:
			if epoch.is_set():
				return False
			if first_pcm_job is not None:
				self._record("pcm", first_pcm_job)
			if explicit_break:
				player.startTrimmingLeadingSilence(False)
		player.feed(data, onDone=callback)
		with self._control:
			if not self._resumed.is_set() and not epoch.is_set():
				player.pause(True)
		if epoch.is_set():
			player.stop()
			return False
		return True

	def _render(self, engine: _Engine, player: _Player, job: _Job) -> None:
		pitch = job.pitch
		recorded_pcm = False
		for part in job.parts:
			if job.epoch.is_set():
				return
			if isinstance(part, str):
				chunks = engine.stream(
					part,
					speed=1.0 if job.method == "sonic" else job.speed,
					steps=job.steps,
					seed=job.seed,
					voice=job.voice,
				)
				if job.method == "sonic" or pitch != 50:
					from ._sonic_audio import process_audio

					chunks = process_audio(
						chunks,
						engine.sample_rate,
						job.speed if job.method == "sonic" else 1.0,
						job.epoch.is_set,
						pitch=2 ** ((pitch - 50) / 100),
					)
				for audio in chunks:
					first_pcm_job = None
					if audio and not recorded_pcm:
						first_pcm_job = job.serial
						recorded_pcm = True
					if not self._feed(player, job.epoch, audio, first_pcm_job=first_pcm_job):
						return
			elif part[0] == "pitch":
				if not 0 <= part[1] <= 100:
					raise ValueError("Pitch must be between 0 and 100")
				pitch = part[1]
			elif part[0] == "index":
				index = part[1]
				if not self._feed(
					player,
					job.epoch,
					b"\0\0",
					lambda index=index: self._notify_index(job.epoch, index),
				):
					return
			else:
				if part[1] < 0:
					raise ValueError("A speech break cannot have a negative duration")
				silence = bytes(2 * (engine.sample_rate * part[1] // 1000))
				if not self._feed(player, job.epoch, silence, explicit_break=True):
					return
		_ = self._feed(player, job.epoch, b"\0\0", lambda: self._notify_done(job))

	def _activate_player(self, player: _Player) -> None:
		with self._control:
			with self._lock:
				self._player = player
				volume = self._volume
			player.setVolume(all=volume / 100)

	def _retire_player(self, player: _Player) -> None:
		with self._control:
			with self._lock:
				self._player = None
			player.close()

	def _run(self) -> None:
		engine: _Engine | None = None
		player: _Player | None = None
		try:
			engine = self._load(self._engine_factory)
			player = self._player_factory(engine.sample_rate)
			self._activate_player(player)
			while True:
				job = self._jobs.get()
				if job is None:
					return
				if isinstance(job, _Reload):
					with self._control:
						with self._lock:
							if self._closed:
								continue
						self._retire_player(player)
					player = None
					engine.close()
					engine = None
					engine = self._load(job.engine_factory)
					player = self._player_factory(engine.sample_rate)
					self._activate_player(player)
					continue
				if not job.epoch.is_set():
					try:
						self._record("started", job.serial)
						self._render(engine, player, job)
					except CudaSynthesisError as error:
						recover = self._on_cuda_failure
						if recover is None:
							raise
						with self._control:
							with self._lock:
								closed = self._closed
								active = not job.epoch.is_set()
							if closed:
								continue
							if active:
								_ = self._cancel(keep_reload=True)
							self._retire_player(player)
						player = None
						engine.close()
						engine = None
						engine = self._load(partial(recover, error))
						player = self._player_factory(engine.sample_rate)
						self._activate_player(player)
						continue
				if self._jobs.empty():
					player.idle()
		except Exception as exc:
			with self._lock:
				self._closed = True
				self._epoch.set()
			self._on_error(exc)
		finally:
			try:
				if player is not None:
					player.close()
			finally:
				if engine is not None:
					engine.close()
