"""Extract and validate a split BlueV3 resource pack."""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

_REQUIRED = (
	"python/blue_onnx/__init__.py",
	"python/blue_onnx/cpu.py",
	"python/numpy/__init__.py",
	"python/num2words/__init__.py",
	"python/polyg2p/__init__.py",
	"python/polyg2p/_polyg2p.pyd",
	"python/onnxruntime/__init__.py",
	"python/onnxruntime/capi/onnxruntime_pybind11_state.pyd",
	"python/onnxruntime/capi/onnxruntime_providers_cuda.dll",
	"python/nvidia/cu13/bin/x86_64/cublas64_13.dll",
	"python/nvidia/cu13/bin/x86_64/cublasLt64_13.dll",
	"python/nvidia/cudnn/bin/cudnn64_9.dll",
	"bluev3_onnx/frontend/polyg2p_data/en.lex",
	"bluev3_onnx/frontend/polyg2p_data/en-large.g2p",
	"bluev3_onnx/onnx/duration_predictor_style.onnx",
	"bluev3_onnx/onnx/text_encoder.onnx",
	"bluev3_onnx/onnx/vector_estimator.onnx",
	"bluev3_onnx/onnx/vector_estimator.int8.onnx",
	"bluev3_onnx/onnx/vocoder.onnx",
	"bluev3_onnx/onnx/tts.json",
	"bluev3_onnx/onnx/vocab.json",
	"bluev3_onnx/onnx/stats.npz",
	"bluev3_onnx/onnx/uncond.npz",
	"bluev3_onnx/voices/noa.json",
)


class InvalidPackError(ValueError):
	pass


def _filesystem_path(path: Path) -> Path:
	if os.name != "nt":
		return path
	absolute = os.path.abspath(path)
	if absolute.startswith(("\\\\?\\", "\\\\.\\")):
		return Path(absolute)
	if absolute.startswith("\\\\"):
		return Path("\\\\?\\UNC\\" + absolute[2:])
	return Path("\\\\?\\" + absolute)


def validate_pack(
	directory: Path,
	version: str,
	*,
	kind: Literal["runtime", "model"],
	installed: bool = False,
) -> None:
	if kind not in ("runtime", "model"):
		raise ValueError(f"Unknown resource kind: {kind}")
	directory = _filesystem_path(directory)
	version_file = directory / "pack-version.txt"
	if not version_file.is_file():
		raise InvalidPackError("Resource pack has no version")
	try:
		pack_version = version_file.read_text(encoding="utf-8").strip()
	except UnicodeError as error:
		raise InvalidPackError("Resource pack version is invalid") from error
	if pack_version != version:
		raise InvalidPackError("Resource pack version does not match the requested version")
	try:
		pack_kind = (directory / "pack-kind.txt").read_text(encoding="utf-8").strip()
	except (FileNotFoundError, UnicodeError) as error:
		raise InvalidPackError("Resource pack kind is missing or invalid") from error
	if pack_kind != kind:
		raise InvalidPackError("Resource pack kind does not match the requested resource")
	try:
		inventory: object = json.loads((directory / "pack-files.json").read_text(encoding="utf-8"))
	except (FileNotFoundError, UnicodeError, json.JSONDecodeError) as error:
		raise InvalidPackError("Resource pack inventory is missing or invalid") from error
	if not isinstance(inventory, dict):
		raise InvalidPackError("Resource pack inventory is invalid")
	files = cast(dict[object, object], inventory)
	metadata_names = {"pack-version.txt", "pack-files.json", "pack-kind.txt"}

	def generated_bytecode(path: Path) -> bool:
		match = re.fullmatch(r"(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc", path.name)
		if not match or path.parent.name != "__pycache__":
			return False
		source = path.parent.parent / f"{match.group(1)}.py"
		name = source.relative_to(directory).as_posix()
		return name.startswith("python/") and name in files

	actual = {
		path.relative_to(directory).as_posix(): path.stat().st_size
		for path in directory.rglob("*")
		if path.is_file()
		and path.relative_to(directory).as_posix() not in metadata_names
		and (not installed or path.relative_to(directory).as_posix() in files or not generated_bytecode(path))
	}
	if (
		any(
			not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0
			for name, size in files.items()
		)
		or files != actual
	):
		raise InvalidPackError("Resource pack does not match its inventory")
	prefix = {"runtime": "python/", "model": "bluev3_onnx/"}[kind]
	if any(not name.startswith(prefix) for name in actual):
		raise InvalidPackError("Resource pack contains files for another resource")
	for relative in (name for name in _REQUIRED if name.startswith(prefix)):
		path = directory / relative
		if not path.is_file() or path.stat().st_size == 0:
			raise InvalidPackError(f"Resource pack is incomplete: {relative}")


def install(
	archive: Path,
	destination: Path,
	progress: Callable[[int, int], None],
	*,
	kind: Literal["runtime", "model"],
) -> None:
	if kind not in ("runtime", "model"):
		raise ValueError(f"Unknown resource kind: {kind}")
	destination = _filesystem_path(destination)
	if destination.exists():
		raise FileExistsError(f"Resource pack is already present: {destination}")
	destination.parent.mkdir(parents=True, exist_ok=True)
	stage = destination.with_name(f".{destination.name}.installing")
	if stage.exists():
		shutil.rmtree(stage)
	stage.mkdir()
	try:
		with zipfile.ZipFile(archive) as pack:
			entries = pack.infolist()
			progress(0, len(entries))
			for index, entry in enumerate(entries, 1):
				try:
					_ = pack.extract(entry, stage)
				except zlib.error as error:
					raise InvalidPackError("Resource archive contains invalid compressed data") from error
				progress(index, len(entries))
		validate_pack(stage, destination.name, kind=kind)
		_ = stage.rename(destination)
	finally:
		if stage.exists():
			shutil.rmtree(stage)
