"""Shravya resource cleanup preference and NVDA uninstall hook."""

from pathlib import Path
from typing import cast

import addonHandler
import NVDAState
import queueHandler

_PREFERENCE = "Delete Shravya resources when the add-on is uninstalled.\n"


def _resource_directory() -> Path:
	directory = Path(NVDAState.WritePaths.configDir) / "shravya"
	if directory.is_symlink() or directory.is_junction():
		raise ValueError("Shravya resource directory is linked; refusing cleanup")
	return directory


def _preference_path() -> Path:
	path = _resource_directory() / ".delete-on-uninstall"
	if path.is_symlink() or path.is_junction():
		raise ValueError("Shravya cleanup preference is linked")
	return path


def cleanup_enabled() -> bool:
	try:
		value = _preference_path().read_text(encoding="utf-8")
	except FileNotFoundError:
		return False
	if value != _PREFERENCE:
		raise ValueError("Invalid Shravya cleanup preference")
	return True


def set_cleanup_enabled(enabled: bool) -> None:
	if not NVDAState.shouldWriteToDisk():
		raise PermissionError("Cannot change Shravya cleanup preference in secure mode")
	path = _preference_path()
	if enabled:
		path.parent.mkdir(parents=True, exist_ok=True)
		_ = path.write_text(_PREFERENCE, encoding="utf-8")
	else:
		path.unlink(missing_ok=True)


def _exists(path: Path) -> bool:
	try:
		_ = path.lstat()
	except FileNotFoundError:
		return False
	return True


def _raise_walk_error(error: OSError) -> None:
	raise error


def _remove_resource_files(directory: Path) -> None:
	for root, directories, files in directory.walk(top_down=False, on_error=_raise_walk_error):
		for name in files:
			path = root / name
			if path.suffix.casefold() == ".zip":
				continue
			if path.is_junction():
				path.rmdir()
			else:
				path.unlink()
		for name in directories:
			path = root / name
			if path.is_symlink() or path.is_junction():
				raise ValueError("Shravya resource directory changed during cleanup")
			if not any(path.iterdir()):
				path.rmdir()
	if not any(directory.iterdir()):
		directory.rmdir()


def _remove_after_uninstall(name: str, installed: Path, pending: Path) -> None:
	if not NVDAState.shouldWriteToDisk():
		return
	if name in addonHandler.state[addonHandler.AddonStateCategory.PENDING_INSTALL]:
		return
	if _exists(installed) or _exists(pending):
		return
	if cleanup_enabled():
		_remove_resource_files(_resource_directory())


def onUninstall() -> None:
	if not NVDAState.shouldWriteToDisk():
		return
	addon = cast(addonHandler.Addon, addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
	if addon.name.casefold() != "shravya":
		raise ValueError("Unexpected add-on for Shravya resource cleanup")
	if not addon.isPendingRemove:
		return
	installed = Path(addon.installPath)
	pending = Path(addon.pendingInstallPath)
	if addon.name in addonHandler.state[addonHandler.AddonStateCategory.PENDING_INSTALL] or _exists(pending):
		return
	if cleanup_enabled():
		# NVDA runs this hook before it attempts the actual add-on removal.
		queueHandler.queueFunction(  # pyright: ignore[reportUnknownMemberType]
			queueHandler.eventQueue,  # pyright: ignore[reportUnknownMemberType]
			_remove_after_uninstall,
			addon.name,
			installed,
			pending,
		)
