"""BlueV3 model loading and streamed PCM conversion."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
	import numpy as np
	from numpy.typing import NDArray


class CudaUnavailableError(RuntimeError):
	pass


class CudaSynthesisError(RuntimeError):
	pass


def _is_cuda_failure(error: Exception) -> bool:
	message = str(error).lower()
	return any(
		marker in message
		for marker in ("cuda failure", "cudnn failure", "cublas failure", "cudnn_status_", "cublas_status_")
	)


class _Session(Protocol):
	def get_providers(self) -> list[str]: ...
	def disable_fallback(self) -> None: ...


class _Core(Protocol):
	dp_ort: _Session
	text_enc_ort: _Session
	vector_est_ort: _Session
	vocoder_ort: _Session
	sample_rate: int
	base_chunk_size: int
	chunk_compress_factor: int
	ldim: int
	sample_noisy_latent: Callable[[NDArray[np.float32]], tuple[NDArray[np.float32], NDArray[np.float32]]]

	def check_style(self, style: object) -> None: ...


class _Model(Protocol):
	sample_rate: int
	tts: _Core

	def stream(
		self, text: str, *, lang: str, speed: float, total_step: int, style: object | None
	) -> Iterator[NDArray[np.float32]]: ...


class _Factory(Protocol):
	def __call__(self, *, onnx_dir: str, style_json: str, providers: list[str]) -> _Model: ...


class _LatentMask(Protocol):
	def __call__(
		self, lengths: NDArray[np.int64], chunk_size: int, compression: int, *, max_len: int
	) -> NDArray[np.float32]: ...


def validate_resources(resources: Path, model_resources: Path) -> None:
	if not (
		(resources / "python").is_dir()
		and (model_resources / "bluev3_onnx" / "onnx").is_dir()
		and (model_resources / "bluev3_onnx" / "voices" / "noa.json").is_file()
	):
		raise FileNotFoundError(
			f"BlueV3 resources are incomplete: runtime={resources}, model={model_resources}"
		)


class BlueEngine:
	def __init__(
		self,
		resources: Path,
		*,
		use_cuda: bool,
		model_resources: Path,
		fallback_voices: set[str] | None = None,
	) -> None:
		super().__init__()
		validate_resources(resources, model_resources)
		python_dir = resources / "python"
		model_dir = model_resources / "bluev3_onnx"
		voice = model_dir / "voices" / "noa.json"
		self._voices = {path.stem: path for path in (model_dir / "voices").glob("*.json") if path.is_file()}
		self._fallback_voices = frozenset(fallback_voices or ())
		self._voice = "noa"
		self._style: object | None = None
		self._use_cuda = use_cuda

		self._python_path = str(python_dir)
		self._owns_path = self._python_path not in sys.path
		self._dlls = ExitStack()
		self._model: _Model | None = None
		if self._owns_path:
			sys.path.insert(0, self._python_path)
		try:
			if use_cuda:
				for relative in ("cu13/bin/x86_64", "cudnn/bin"):
					directory = python_dir / "nvidia" / relative
					if not directory.is_dir():
						raise CudaUnavailableError(f"CUDA runtime directory is missing: {directory}")
					_ = self._dlls.callback(os.add_dll_directory(str(directory)).close)

			onnxruntime = importlib.import_module("onnxruntime")
			errors = importlib.import_module("onnxruntime.capi.onnxruntime_pybind11_state")
			self._ort_errors = (
				RuntimeError,
				cast(type[Exception], getattr(errors, "Fail")),
				cast(type[Exception], getattr(errors, "RuntimeException")),
				cast(type[Exception], getattr(errors, "EPFail")),
			)
			if use_cuda:
				preload = cast(Callable[..., None], getattr(onnxruntime, "preload_dlls"))
				preload(cuda=False, cudnn=True, msvc=False, directory="")
			available = cast(Callable[[], list[str]], getattr(onnxruntime, "get_available_providers"))()
			providers = ["CPUExecutionProvider"]
			if use_cuda:
				if "CUDAExecutionProvider" not in available:
					raise CudaUnavailableError("ONNX Runtime does not provide CUDA")
				providers.insert(0, "CUDAExecutionProvider")
			blue = importlib.import_module("blue_onnx")
			factory = cast(_Factory, getattr(blue, "BlueTTS"))
			self._load_style = cast(Callable[[list[str]], object], getattr(blue, "load_voice_style"))
			self._latent_frames = cast(
				Callable[[float, int, int, int], int], getattr(blue, "latent_frames_for_duration")
			)
			self._latent_mask = cast(_LatentMask, getattr(blue, "get_latent_mask"))
			try:
				self._model = factory(
					onnx_dir=str(model_dir / "onnx"), style_json=str(voice), providers=providers
				)
			except self._ort_errors as error:
				if use_cuda and _is_cuda_failure(error):
					raise CudaUnavailableError("CUDA initialization failed") from error
				raise
			core = self._model.tts
			sessions = (core.dp_ort, core.text_enc_ort, core.vector_est_ort, core.vocoder_ort)
			for session in sessions:
				session.disable_fallback()
			if use_cuda and not all(
				"CUDAExecutionProvider" in session.get_providers() for session in sessions
			):
				raise CudaUnavailableError("A BlueV3 stage did not register the CUDA execution provider")
		except BaseException:
			self.close()
			raise

	@property
	def sample_rate(self) -> int:
		if self._model is None:
			raise RuntimeError("BlueV3 engine is closed")
		return self._model.sample_rate

	def stream(
		self, text: str, *, speed: float, steps: int, seed: int = 0, voice: str = "noa"
	) -> Iterator[bytes]:
		import numpy as np

		if self._model is None:
			raise RuntimeError("BlueV3 engine is closed")
		if voice not in self._voices:
			if voice not in self._fallback_voices:
				raise ValueError(f"Unknown BlueV3 preset voice: {voice}")
			voice = "noa"
		if voice != self._voice:
			style = None if voice == "noa" else self._load_style([str(self._voices[voice])])
			if style is not None:
				self._model.tts.check_style(style)
			self._voice = voice
			self._style = style
		core = self._model.tts
		random = np.random.RandomState(seed)

		def sample(duration: NDArray[np.float32]) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
			wav_lengths = (duration * core.sample_rate).astype(np.int64)
			length = self._latent_frames(
				float(duration.max()), core.sample_rate, core.base_chunk_size, core.chunk_compress_factor
			)
			dimension = core.ldim * core.chunk_compress_factor
			noise = random.randn(len(duration), dimension, length).astype(np.float32)
			mask = self._latent_mask(
				wav_lengths, core.base_chunk_size, core.chunk_compress_factor, max_len=length
			)
			return noise * mask, mask

		original_sample = core.sample_noisy_latent
		core.sample_noisy_latent = sample
		try:
			for audio in self._model.stream(
				text, lang="en", speed=speed, total_step=steps, style=self._style
			):
				if audio.ndim != 1 or audio.size == 0 or not np.isfinite(audio).all():
					raise ValueError("BlueV3 returned empty, non-mono, or non-finite audio")
				yield (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
		except self._ort_errors as error:
			if self._use_cuda and _is_cuda_failure(error):
				raise CudaSynthesisError("CUDA synthesis failed") from error
			raise
		finally:
			core.sample_noisy_latent = original_sample

	def close(self) -> None:
		self._model = None
		self._style = None
		self._dlls.close()
		if self._owns_path:
			sys.path.remove(self._python_path)
			self._owns_path = False
