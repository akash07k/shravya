"""Download and install split BlueV3 resource packs."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

from ._blue import validate_resources
from ._catalog import Catalog, Component, fetch_catalog
from ._download import download
from ._install import InvalidPackError, install, validate_pack
from ._resources import (
	activate,
	activate_if_no_pending,
	component_directory,
	resource_directories,
	resource_selection,
	stage_for_addon,
	stage_for_restart,
)

_LOG = logging.getLogger(__name__)
type Stage = Literal["download", "extract"]


class ResourceUpdate(NamedTuple):
	catalog: Catalog
	previous: tuple[str | None, str | None]


class AddonResources(NamedTuple):
	version: str
	catalog: Catalog
	previous: tuple[str | None, str | None]
	runtime: str
	model: str
	missing: tuple[Literal["runtime", "model"], ...]


def plan_addon_resources(catalog_url: str, config_directory: Path, addon_version: str) -> AddonResources:
	catalog = fetch_catalog(catalog_url, addon_version)
	previous = resource_selection(config_directory)
	runtime = catalog.runtime.id
	model = catalog.model.id
	missing: list[Literal["runtime", "model"]] = []
	selection: tuple[tuple[Literal["runtime", "model"], str], ...] = (
		("runtime", runtime),
		("model", model),
	)
	for kind, version in selection:
		destination = component_directory(config_directory, kind, version)
		if destination.exists():
			validate_pack(destination, version, kind=kind, installed=True)
		else:
			missing.append(kind)
	return AddonResources(addon_version, catalog, previous, runtime, model, tuple(missing))


def prepare_addon_resources(
	plan: AddonResources,
	config_directory: Path,
	progress: Callable[[Stage, int, int], None],
) -> bool:
	if resource_selection(config_directory) != plan.previous:
		raise RuntimeError("The active resource selection changed during preparation")
	clean = True
	components: tuple[tuple[Literal["runtime", "model"], str, Component], ...] = (
		("runtime", plan.runtime, plan.catalog.runtime),
		("model", plan.model, plan.catalog.model),
	)
	for kind, version, component in components:
		clean = prepare_component(component.url, config_directory, kind, version, progress) and clean
	progress("extract", 1, 1)
	stage_for_addon(config_directory, plan.version, plan.runtime, plan.model, expected=plan.previous)
	return clean


def check_resource_update(
	catalog_url: str,
	config_directory: Path,
	addon_version: str,
) -> ResourceUpdate | None:
	runtime_dir, model_dir = resource_directories(config_directory)
	validate_resources(runtime_dir, model_dir)
	previous = resource_selection(config_directory)
	catalog = fetch_catalog(catalog_url, addon_version)
	runtime, model = previous
	if runtime == catalog.runtime.id and model == catalog.model.id:
		return None
	return ResourceUpdate(catalog, previous)


def prepare_resource_update(
	update: ResourceUpdate,
	config_directory: Path,
	progress: Callable[[Stage, int, int], None],
) -> tuple[bool, bool]:
	if resource_selection(config_directory) != update.previous:
		raise RuntimeError("The active resource selection changed during preparation")
	clean = True
	components: tuple[tuple[Literal["runtime", "model"], Component, str | None], ...] = (
		("runtime", update.catalog.runtime, update.previous[0]),
		("model", update.catalog.model, update.previous[1]),
	)
	for kind, component, previous in components:
		if component.id != previous:
			clean = prepare_component(component.url, config_directory, kind, component.id, progress) and clean
	progress("extract", 1, 1)
	runtime, model = update.catalog.runtime.id, update.catalog.model.id
	restart = runtime != update.previous[0]
	if restart:
		stage_for_restart(config_directory, runtime, model, expected=update.previous)
	else:
		activate_if_no_pending(config_directory, runtime, model, expected=update.previous)
	return clean, restart


def prepare_component(
	url: str,
	config_directory: Path,
	kind: Literal["runtime", "model"],
	version: str,
	progress: Callable[[Stage, int, int], None],
) -> bool:
	destination = component_directory(config_directory, kind, version)
	if destination.exists():
		validate_pack(destination, version, kind=kind, installed=True)
		return True
	cache = config_directory / "shravya" / "downloads" / kind
	archive = cache / f"{version}.zip"
	source = cache / f"{version}.source"
	try:
		same_source = archive.is_file() and source.read_text(encoding="utf-8") == url
	except FileNotFoundError:
		same_source = False
	except UnicodeError:
		_LOG.warning("Resource archive source metadata is invalid; restarting the download", exc_info=True)
		same_source = False
	if not same_source:
		archive.unlink(missing_ok=True)
		source.unlink(missing_ok=True)
		download(url, archive, lambda done, total: progress("download", done, total))
		_ = source.write_text(url, encoding="utf-8")
	try:
		install(archive, destination, lambda done, total: progress("extract", done, total), kind=kind)
	except (InvalidPackError, zipfile.BadZipFile):
		archive.unlink(missing_ok=True)
		source.unlink(missing_ok=True)
		raise
	try:
		archive.unlink()
		source.unlink()
	except OSError:
		_LOG.warning(
			"Resource pack installed, but the downloaded archive could not be removed",
			exc_info=True,
		)
		return False
	return True


def prepare_split(
	catalog_url: str,
	config_directory: Path,
	addon_version: str,
	progress: Callable[[Stage, int, int], None],
) -> bool:
	catalog = fetch_catalog(catalog_url, addon_version)
	clean = prepare_component(catalog.runtime.url, config_directory, "runtime", catalog.runtime.id, progress)
	clean = (
		prepare_component(catalog.model.url, config_directory, "model", catalog.model.id, progress) and clean
	)
	progress("extract", 1, 1)
	activate(config_directory, catalog.runtime.id, catalog.model.id, catalog.interface)
	return clean


def _same_file(left: Path, right: Path) -> bool:
	with left.open("rb") as first, right.open("rb") as second:
		while chunk := first.read(1024 * 1024):
			if chunk != second.read(len(chunk)):
				return False
		return not second.read(1)


def prepare_local(
	runtime_archive: Path,
	model_archive: Path,
	config_directory: Path,
	progress: Callable[[Stage, int, int], None],
) -> bool:
	previous = resource_selection(config_directory)
	versions: dict[Literal["runtime", "model"], str] = {}
	archives: tuple[tuple[Literal["runtime", "model"], Path], ...] = (
		("runtime", runtime_archive),
		("model", model_archive),
	)
	for kind, archive in archives:
		with zipfile.ZipFile(archive) as source:
			entries = [entry for entry in source.infolist() if entry.filename == "pack-version.txt"]
			if len(entries) != 1 or entries[0].file_size > 1024:
				raise InvalidPackError("Resource archive has no unique valid version")
			try:
				version = source.read(entries[0]).decode("utf-8").strip()
			except UnicodeError as error:
				raise InvalidPackError("Resource archive version is invalid") from error
		destination = component_directory(config_directory, kind, version)
		destination.parent.mkdir(parents=True, exist_ok=True)
		with tempfile.TemporaryDirectory(prefix=".local-", dir=destination.parent) as temp:
			candidate = Path(temp) / version
			install(archive, candidate, lambda done, total: progress("extract", done, total), kind=kind)
			if destination.exists():
				validate_pack(destination, version, kind=kind, installed=True)
				for path in candidate.rglob("*"):
					if path.is_file() and not _same_file(path, destination / path.relative_to(candidate)):
						raise InvalidPackError(f"Existing {kind} pack differs from the selected archive")
			else:
				_ = candidate.rename(destination)
		versions[kind] = version
	progress("extract", 1, 1)
	runtime, model = versions["runtime"], versions["model"]
	restart = previous[0] is not None and previous[0] != runtime
	if restart:
		stage_for_restart(config_directory, runtime, model, expected=previous)
	else:
		activate_if_no_pending(config_directory, runtime, model, expected=previous)
	return restart
