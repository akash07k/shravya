"""Download and hand a confirmed add-on update to NVDA."""

from __future__ import annotations

import tempfile
import threading
import zipfile
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

import addonHandler
import gui
import gui.addonGui
import NVDAState
from logHandler import log
from systemUtils import ExecAndPump

from ._download import download
from ._resources import discard_for_addon
from ._setup import Stage, plan_addon_resources, prepare_addon_resources
from ._update import Update, version_parts

if TYPE_CHECKING:
	from gettext import gettext as _

	class _ProgressDialog(Protocol):
		def Update(self, value: int, message: str) -> tuple[bool, bool]: ...
		def Destroy(self) -> bool: ...

	class _Wx(Protocol):
		OK: int
		YES: int
		NO: int
		ICON_INFORMATION: int
		ICON_ERROR: int
		PD_CAN_ABORT: int
		PD_ELAPSED_TIME: int

		def ProgressDialog(
			self, title: str, message: str, *, style: int, parent: object
		) -> _ProgressDialog: ...
		def CallAfter(self, callback: Callable[..., None], *args: object) -> None: ...

	wx: _Wx
else:
	import wx

addonHandler.initTranslation()
_message_box = cast(Callable[[str, str, int], int], gui.messageBox)  # pyright: ignore[reportUnknownMemberType]


class _Cancelled(Exception):
	pass


def _pending() -> bool:
	return any(
		addon.name == "shravya" and (addon.isPendingInstall or addon.isPendingRemove)
		for addon in addonHandler.getAvailableAddons()
	)


def install_update(update: Update, stopped: threading.Event) -> None:
	if stopped.is_set():
		return
	if _pending():
		_ignored = _message_box(
			_("Restart NVDA to finish the pending Shravya add-on change."), _("Shravya update"), wx.OK
		)
		return
	frame = gui.mainFrame
	if frame is None:
		raise RuntimeError("NVDA's main window is unavailable")
	frame.prePopup()
	try:
		_ignored = version_parts(update.version)
		if update.catalog_url is None:
			raise ValueError("The update does not specify a resource catalog")
		config_dir = Path(NVDAState.WritePaths.configDir)
		plan = ExecAndPump(plan_addon_resources, update.catalog_url, config_dir, update.version).funcRes
		if plan is None:
			raise RuntimeError("Resource planning did not return a result")
		if stopped.is_set():
			raise _Cancelled()
		if plan.missing:
			if len(plan.missing) == 2:
				message = _("This update requires new runtime and model resources. Download them now?")
			elif plan.missing[0] == "runtime":
				message = _("This update requires new runtime resources. Download them now?")
			else:
				message = _("This update requires new model resources. Download them now?")
			answer = _message_box(message, _("Shravya update"), wx.YES | wx.NO | wx.ICON_INFORMATION)
			if answer != wx.YES:
				return
			if stopped.is_set():
				raise _Cancelled()
		resources_prepared = False
		installed = False
		try:
			dialog = wx.ProgressDialog(
				_("Shravya update"),
				_("Preparing resources"),
				style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME,
				parent=frame,
			)
			cancelled = threading.Event()
			active = True
			last: tuple[str, int] | None = None

			def display(stage: str, percent: int) -> None:
				message = {
					"download": _("Downloading resources"),
					"extract": _("Extracting resources"),
					"addon": _("Downloading update"),
				}[stage]
				if active and not dialog.Update(percent, message)[0]:
					cancelled.set()

			def progress(stage: Stage | Literal["addon"], done: int, total: int) -> None:
				nonlocal last
				if stopped.is_set() or cancelled.is_set():
					raise _Cancelled()
				percent = min(99, done * 100 // total) if total else 0
				if (stage, percent) != last:
					last = stage, percent
					wx.CallAfter(display, stage, percent)

			def addon_progress(done: int, total: int) -> None:
				progress("addon", done, total)

			try:
				clean = ExecAndPump(prepare_addon_resources, plan, config_dir, progress).funcRes
				if clean is None:
					raise RuntimeError("Resource preparation did not return a result")
				resources_prepared = True
				progress("addon", 0, 0)
				if not clean:
					_ignored = _message_box(
						_("Resources are ready, but a downloaded archive could not be removed."),
						_("Shravya update"),
						wx.OK,
					)
				cache = config_dir / "shravya" / "downloads" / "addon"
				cache.mkdir(parents=True, exist_ok=True)
				archive = cache / f"{update.version}.nvda-addon"
				with tempfile.TemporaryDirectory(prefix=".install-", dir=cache) as directory:
					path = Path(directory) / "update.nvda-addon"
					try:
						_ignored = ExecAndPump(download, update.url, archive, addon_progress)
						_ignored = archive.replace(path)
						if stopped.is_set() or cancelled.is_set():
							raise _Cancelled()
						active = False
						_ignored = dialog.Destroy()
						bundle = addonHandler.AddonBundle(str(path))
						if (
							bundle.manifest["name"] != "shravya"
							or bundle.manifest["version"] != update.version
						):
							raise ValueError(
								"The downloaded add-on identity or version does not match the release"
							)
						if _pending():
							raise RuntimeError("A Shravya add-on change is already pending")
						if gui.addonGui.installAddon(frame, str(path)):  # pyright: ignore[reportUnknownMemberType]
							installed = True
							gui.addonGui.promptUserForRestart()
					finally:
						archive.unlink(missing_ok=True)
			finally:
				if active:
					active = False
					_ignored = dialog.Destroy()
		finally:
			if resources_prepared and not installed:
				discard_for_addon(config_dir, update.version)
	except _Cancelled:
		if not stopped.is_set():
			_ignored = _message_box(_("Shravya update cancelled."), _("Shravya update"), wx.OK)
	except (
		OSError,
		ValueError,
		RuntimeError,
		KeyError,
		HTTPException,
		zipfile.BadZipFile,
		addonHandler.AddonError,
	):
		log.error("Shravya update failed", exc_info=True)
		_ignored = _message_box(_("Shravya update failed."), _("Shravya update"), wx.OK | wx.ICON_ERROR)
	finally:
		frame.postPopup()
