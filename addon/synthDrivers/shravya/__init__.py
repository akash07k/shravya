"""NVDA synthesizer backed by BlueV3."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from ctypes import c_float
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import addonHandler
import config
import core
import NVDAState
import nvwave
import synthDriverHandler
import ui
from logHandler import log
from speech.commands import BreakCommand, IndexCommand, PitchCommand
from speech.types import SpeechSequence
from synthDriverHandler import synthDoneSpeaking, synthIndexReached

from . import _settings
from ._blue import BlueEngine, CudaSynthesisError, CudaUnavailableError, validate_resources
from ._diagnostic_measurements import TimingEvent
from ._resources import resource_directories
from ._worker import SpeechPart, SpeechWorker

if TYPE_CHECKING:
	from gettext import gettext as _

	from addonHandler import Addon

addonHandler.initTranslation()


class _MonoWavePlayer(nvwave.WavePlayer):
	@override
	def setVolume(
		self, *, all: float | None = None, left: float | None = None, right: float | None = None
	) -> None:
		if all == 0.0 and left is None and right is None:
			# NVDA's all=0 path incorrectly rejects mono output after setting channel 0.
			nvwave.wasapi.wasPlay_setChannelVolume(self._player, 0, c_float(0.0))
			return
		super().setVolume(all=all, left=left, right=right)


class SynthDriver(synthDriverHandler.SynthDriver):
	name = "shravya"
	description = "Shravya"
	supportedSettings = (
		synthDriverHandler.SynthDriver.VoiceSetting(),
		synthDriverHandler.SynthDriver.RateSetting(),
		synthDriverHandler.SynthDriver.PitchSetting(),
		synthDriverHandler.SynthDriver.VolumeSetting(),
	)
	supportedCommands = {IndexCommand, BreakCommand, PitchCommand}  # noqa: RUF012
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}  # noqa: RUF012

	@classmethod
	@override
	def check(cls) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
		return True

	def __init__(self) -> None:
		super().__init__()
		self._config_dir = Path(NVDAState.WritePaths.configDir)
		addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
		self._addon_version = cast(str, addon.manifest["version"])
		self._resources, self._model_resources = resource_directories(
			self._config_dir, addon_version=self._addon_version
		)
		validate_resources(self._resources, self._model_resources)
		self._voice = "noa"
		self._rate = 50
		self._pitch = 50
		self._volume = 100
		self._use_cuda = _settings.use_cuda()
		initial_cuda = self._use_cuda
		self._worker = SpeechWorker(
			engine_factory=lambda: self._load_engine(initial_cuda),
			player_factory=self._make_player,
			on_index=self._on_index,
			on_done=self._on_done,
			on_error=self._on_error,
			on_cuda_failure=self._recover_cuda,
		)

	def _load_engine(self, use_cuda: bool) -> BlueEngine:
		runtime, model = resource_directories(self._config_dir, addon_version=self._addon_version)
		if runtime != self._resources:
			log.warning("A Shravya runtime change requires NVDA to restart")
			model = self._model_resources
		fallback_voices: set[str] = set()
		if model != self._model_resources:
			previous = (self._model_resources / "bluev3_onnx" / "voices").glob("*.json")
			current = (model / "bluev3_onnx" / "voices").glob("*.json")
			fallback_voices = {path.stem for path in previous if path.is_file()} - {
				path.stem for path in current if path.is_file()
			}

		def create_engine(cuda: bool) -> BlueEngine:
			if fallback_voices:
				return BlueEngine(
					self._resources, use_cuda=cuda, model_resources=model, fallback_voices=fallback_voices
				)
			return BlueEngine(self._resources, use_cuda=cuda, model_resources=model)

		try:
			engine = create_engine(use_cuda)
		except CudaUnavailableError:
			if not use_cuda:
				raise
			log.warning("CUDA is unavailable; using CPU for Shravya", exc_info=True)
			core.callLater(0, self._notify_cuda_fallback)  # pyright: ignore[reportUnknownMemberType]
			engine = create_engine(False)
		self._model_resources = model
		if self._voice in fallback_voices:
			self._voice = "noa"
			log.warning("The selected Shravya voice is unavailable in the updated model; using Noa")
			core.callLater(0, self._notify_voice_fallback)  # pyright: ignore[reportUnknownMemberType]
		return engine

	def _recover_cuda(self, error: CudaSynthesisError) -> BlueEngine:
		log.warning(
			"CUDA synthesis failed; using CPU for Shravya", exc_info=(type(error), error, error.__traceback__)
		)
		core.callLater(0, self._notify_cuda_fallback)  # pyright: ignore[reportUnknownMemberType]
		return self._load_engine(False)

	def apply_settings(self) -> None:
		use_cuda = _settings.use_cuda()
		if use_cuda != self._use_cuda:
			self._worker.reload(lambda: self._load_engine(use_cuda))
			self._use_cuda = use_cuda

	def set_diagnostic(self, observer: Callable[[TimingEvent], None] | None) -> None:
		self._worker.set_diagnostic(observer)

	def _notify_cuda_fallback(self) -> None:
		if synthDriverHandler.getSynth() is self:
			# Translators: Report that speech is temporarily using the CPU.
			ui.message(_("Shravya could not use CUDA and is using the CPU instead."))

	def _notify_voice_fallback(self) -> None:
		if synthDriverHandler.getSynth() is self:
			# Translators: Report a voice removed by a model update.
			ui.message(_("The selected Shravya voice is unavailable in the updated model. Using Noa."))

	def _make_player(self, sample_rate: int) -> nvwave.WavePlayer:
		return _MonoWavePlayer(
			channels=1,
			samplesPerSec=sample_rate,
			bitsPerSample=16,
			outputDevice=cast(str, config.conf["audio"]["outputDevice"]),  # pyright: ignore[reportIndexIssue]
		)

	def _on_index(self, index: int) -> None:
		synthIndexReached.notify(synth=self, index=index)  # pyright: ignore[reportUnknownMemberType]

	def _on_done(self) -> None:
		synthDoneSpeaking.notify(synth=self)  # pyright: ignore[reportUnknownMemberType]

	@override
	def _get_language(self) -> str:
		return "en"

	@override
	def _get_availableVoices(self) -> OrderedDict[str, synthDriverHandler.VoiceInfo]:
		return self._getAvailableVoices()

	@override
	def _getAvailableVoices(self) -> OrderedDict[str, synthDriverHandler.VoiceInfo]:
		voices = (self._model_resources / "bluev3_onnx" / "voices").glob("*.json")
		return OrderedDict(
			(path.stem, synthDriverHandler.VoiceInfo(path.stem, path.stem.title(), language="en"))
			for path in sorted(voices)
			if path.is_file()
		)

	@override
	def _get_voice(self) -> str:
		return self._voice

	@override
	def _set_voice(self, value: str) -> None:
		if value not in self.availableVoices:
			raise ValueError(f"Unknown BlueV3 preset voice: {value}")
		self._voice = value

	@override
	def _get_rate(self) -> int:
		return self._rate

	@override
	def _set_rate(self, value: int) -> None:
		if not 0 <= value <= 100:
			raise ValueError("Rate must be between 0 and 100")
		self._rate = value

	@override
	def _get_pitch(self) -> int:
		return self._pitch

	@override
	def _set_pitch(self, value: int) -> None:
		if not 0 <= value <= 100:
			raise ValueError("Pitch must be between 0 and 100")
		self._pitch = value

	@override
	def _get_volume(self) -> int:
		return self._volume

	@override
	def _set_volume(self, value: int) -> None:
		self._worker.set_volume(value)
		self._volume = value

	def _on_error(self, error: Exception) -> None:
		log.error("Shravya speech failed", exc_info=(type(error), error, error.__traceback__))
		core.callLater(0, self._report_error)  # pyright: ignore[reportUnknownMemberType]

	def _report_error(self) -> None:
		if synthDriverHandler.getSynth() is not self:
			return
		attempted = synthDriverHandler.findAndSetNextSynth(self.name)
		current = synthDriverHandler.getSynth()
		if not attempted or current is None or current is self:
			log.error("No fallback synthesizer is available for Shravya")
			return
		# Translators: Report a fatal synthesizer error after switching to another synth.
		ui.message(_("Shravya stopped working. NVDA switched to another synthesizer."))

	@override
	def speak(self, speechSequence: SpeechSequence) -> None:
		parts: list[SpeechPart] = []
		for item in speechSequence:
			if isinstance(item, str):
				if parts and isinstance(parts[-1], str):
					parts[-1] += item
				else:
					parts.append(item)
			elif isinstance(item, IndexCommand):
				parts.append(("index", item.index))
			elif isinstance(item, BreakCommand):
				parts.append(("break", item.time))
			elif isinstance(item, PitchCommand):
				value = cast(int, item.newValue)  # pyright: ignore[reportUnknownMemberType]
				parts.append(("pitch", max(0, min(100, value))))
		self._worker.speak(
			parts,
			speed=2 ** ((self._rate - 50) / 50),
			steps=_settings.steps(),
			seed=_settings.seed(),
			voice=self._voice,
			method=_settings.rate_method(),
			pitch=self._pitch,
		)

	@override
	def cancel(self) -> None:
		self._worker.cancel()

	@override
	def pause(self, switch: bool) -> None:
		self._worker.pause(switch)

	@override
	def terminate(self) -> None:
		self._worker.close()
		super().terminate()
