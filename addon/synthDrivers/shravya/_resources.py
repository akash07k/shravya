"""Locate and select BlueV3 resources outside the replaceable add-on directory."""

import builtins
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Literal, cast

from ._blue import validate_resources
from ._install import validate_pack

RESOURCE_INTERFACE = "bluev3-v1"
_SELECTION = "active-resources.json"
_SELECTION_LOCK = threading.RLock()
_LOG = logging.getLogger(__name__)
_PROCESS_TOKEN_NAME = "_shravya_resource_process_token"
if not hasattr(builtins, _PROCESS_TOKEN_NAME):
	setattr(builtins, _PROCESS_TOKEN_NAME, uuid.uuid4().hex)
_PROCESS_TOKEN = cast(str, getattr(builtins, _PROCESS_TOKEN_NAME))
_PENDING = "pending-resources.json"
_PENDING_FAILURES: set[Path] = set()


def component_directory(config_directory: Path, kind: Literal["runtime", "model"], version: str) -> Path:
	if kind not in ("runtime", "model"):
		raise ValueError(f"Unknown resource kind: {kind}")
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version) or version.endswith("."):
		raise ValueError(f"Invalid {kind} resource version: {version!r}")
	return config_directory / "shravya" / "packs" / kind / version


def _paths(config_directory: Path, runtime: str | None, model: str | None) -> tuple[Path, Path]:
	if runtime is None or model is None:
		raise FileNotFoundError("No complete Shravya resource selection")
	return (
		component_directory(config_directory, "runtime", runtime),
		component_directory(config_directory, "model", model),
	)


def _read_selection(path: Path, config_directory: Path) -> tuple[str, str]:
	try:
		content = path.read_text(encoding="utf-8")
	except UnicodeError as error:
		raise ValueError("Invalid active resource selection") from error
	try:
		data: object = json.loads(content)
	except json.JSONDecodeError as error:
		raise ValueError("Invalid active resource selection") from error
	if not isinstance(data, dict):
		raise ValueError("Invalid active resource selection")
	fields = cast(dict[str, object], data)
	if (
		set(fields) != {"interface", "runtime", "model"}
		or fields["interface"] != RESOURCE_INTERFACE
		or not isinstance(fields["runtime"], str)
		or not isinstance(fields["model"], str)
	):
		raise ValueError("Invalid or incompatible active resource selection")
	runtime, model = fields["runtime"], fields["model"]
	_ = component_directory(config_directory, "runtime", runtime)
	_ = component_directory(config_directory, "model", model)
	return runtime, model


def resource_selection(config_directory: Path) -> tuple[str | None, str | None]:
	try:
		return _read_selection(config_directory / "shravya" / _SELECTION, config_directory)
	except FileNotFoundError:
		return None, None


def _prepared_path(config_directory: Path, addon_version: str) -> Path:
	if not re.fullmatch(r"\d+\.\d+\.\d+", addon_version):
		raise ValueError(f"Invalid add-on version: {addon_version!r}")
	return config_directory / "shravya" / f"prepared-{addon_version}.json"


def _promote_pending(config_directory: Path, pending: Path) -> None:
	with _SELECTION_LOCK:
		try:
			data: object = json.loads(pending.read_text(encoding="utf-8"))
		except FileNotFoundError:
			return
		except (OSError, UnicodeError, json.JSONDecodeError):
			_LOG.exception("Could not read pending Shravya resources; retaining the current selection")
			_PENDING_FAILURES.add(config_directory)
			return
		try:
			if not isinstance(data, dict):
				raise ValueError("Invalid pending resource selection")
			fields = cast(dict[str, object], data)
			previous_field = fields.get("previous")
			if not isinstance(previous_field, list):
				raise ValueError("Invalid pending resource selection")
			previous_values = cast(list[object], previous_field)
			if (
				set(fields) != {"process", "previous", "runtime", "model"}
				or not isinstance(fields["process"], str)
				or len(previous_values) != 2
				or not all(value is None or isinstance(value, str) for value in previous_values)
				or not isinstance(fields["runtime"], str)
				or not isinstance(fields["model"], str)
			):
				raise ValueError("Invalid pending resource selection")
			if fields["process"] != _PROCESS_TOKEN:
				previous = cast(list[str | None], previous_values)
				target = (fields["runtime"], fields["model"])
				if resource_selection(config_directory) == target:
					_validate_pair(config_directory, *target)
				else:
					activate(
						config_directory,
						*target,
						RESOURCE_INTERFACE,
						expected=(previous[0], previous[1]),
					)
				pending.unlink()
		except (OSError, ValueError, RuntimeError):
			_LOG.exception("Could not activate prepared Shravya resources; retaining the current selection")
			_PENDING_FAILURES.add(config_directory)


def take_pending_failure(config_directory: Path) -> bool:
	with _SELECTION_LOCK:
		if config_directory not in _PENDING_FAILURES:
			return False
		_PENDING_FAILURES.remove(config_directory)
		return True


def resource_directories(config_directory: Path, *, addon_version: str | None = None) -> tuple[Path, Path]:
	_promote_pending(config_directory, config_directory / "shravya" / _PENDING)
	if addon_version is not None and re.fullmatch(r"\d+\.\d+\.\d+", addon_version):
		_promote_pending(config_directory, _prepared_path(config_directory, addon_version))
	runtime, model = resource_selection(config_directory)
	return _paths(config_directory, runtime, model)


def _validate_pair(
	config_directory: Path,
	runtime: str | None,
	model: str | None,
) -> None:
	if runtime is None or model is None:
		raise ValueError("Both runtime and model resources are required")
	runtime_dir, model_dir = _paths(config_directory, runtime, model)
	validate_pack(runtime_dir, runtime, kind="runtime", installed=True)
	validate_pack(model_dir, model, kind="model", installed=True)
	validate_resources(runtime_dir, model_dir)


def _write_selection(path: Path, runtime: str | None, model: str | None) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
			temporary = Path(file.name)
			json.dump({"interface": RESOURCE_INTERFACE, "runtime": runtime, "model": model}, file)
		os.replace(temporary, path)
	finally:
		if temporary is not None:
			temporary.unlink(missing_ok=True)


def stage_for_addon(
	config_directory: Path,
	addon_version: str,
	runtime: str | None,
	model: str | None,
	*,
	expected: tuple[str | None, str | None] | None = None,
) -> None:
	path = _prepared_path(config_directory, addon_version)
	_validate_pair(config_directory, runtime, model)
	assert runtime is not None and model is not None
	with _SELECTION_LOCK:
		directory = path.parent
		if (directory / _PENDING).exists() or any(directory.glob("prepared-*.json")):
			raise RuntimeError("A Shravya resource or add-on change is already pending")
		previous = resource_selection(config_directory)
		if expected is not None and previous != expected:
			raise RuntimeError("The active resource selection changed during preparation")
		_write_pending(path, runtime, model, previous)


def _write_pending(path: Path, runtime: str, model: str, previous: tuple[str | None, str | None]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
			temporary = Path(file.name)
			json.dump(
				{"process": _PROCESS_TOKEN, "previous": previous, "runtime": runtime, "model": model},
				file,
			)
		os.replace(temporary, path)
	finally:
		if temporary is not None:
			temporary.unlink(missing_ok=True)


def stage_for_restart(
	config_directory: Path,
	runtime: str,
	model: str,
	*,
	expected: tuple[str | None, str | None],
) -> None:
	_validate_pair(config_directory, runtime, model)
	directory = config_directory / "shravya"
	with _SELECTION_LOCK:
		if resource_selection(config_directory) != expected:
			raise RuntimeError("The active resource selection changed during preparation")
		if (directory / _PENDING).exists() or any(directory.glob("prepared-*.json")):
			raise RuntimeError("A Shravya resource or add-on change is already pending")
		_write_pending(directory / _PENDING, runtime, model, expected)


def activate_if_no_pending(
	config_directory: Path,
	runtime: str,
	model: str,
	*,
	expected: tuple[str | None, str | None],
) -> None:
	directory = config_directory / "shravya"
	with _SELECTION_LOCK:
		if (directory / _PENDING).exists() or any(directory.glob("prepared-*.json")):
			raise RuntimeError("A Shravya resource or add-on change is already pending")
		activate(config_directory, runtime, model, RESOURCE_INTERFACE, expected=expected)


def discard_for_addon(config_directory: Path, addon_version: str) -> None:
	with _SELECTION_LOCK:
		_prepared_path(config_directory, addon_version).unlink(missing_ok=True)


def activate(
	config_directory: Path,
	runtime: str | None,
	model: str | None,
	interface: str,
	*,
	expected: tuple[str | None, str | None] | None = None,
) -> None:
	if interface != RESOURCE_INTERFACE:
		raise ValueError(f"Incompatible resource interface: {interface!r}")
	_validate_pair(config_directory, runtime, model)
	with _SELECTION_LOCK:
		if expected is not None and resource_selection(config_directory) != expected:
			raise RuntimeError("The active resource selection changed during preparation")
		_write_selection(config_directory / "shravya" / _SELECTION, runtime, model)
