"""Schedule add-on update checks and ask before installing."""

from __future__ import annotations

from http.client import HTTPException
from threading import Event, Thread
from typing import TYPE_CHECKING, Protocol, cast, override

import addonHandler
import gui
import gui.message
import ui
from logHandler import log

from ._interaction import interaction_lock
from ._update import Update, check_release
from ._update_install import install_update

if TYPE_CHECKING:
	from collections.abc import Callable
	from gettext import gettext as _

	from addonHandler import Addon
	from gui.guiHelper import BoxSizerHelper

	class _Timer(Protocol):
		def Stop(self) -> None: ...

	class _Wx(Protocol):
		YES: int
		NO: int
		OK: int
		ICON_INFORMATION: int
		ICON_ERROR: int
		TE_MULTILINE: int
		TE_READONLY: int
		TextCtrl: type[object]

		def CallAfter(self, callback: Callable[..., None], *args: object) -> None: ...
		def CallLater(self, delay: int, callback: Callable[..., None], *args: object) -> _Timer: ...

	wx: _Wx
else:
	import wx

addonHandler.initTranslation()
_DAY_MS = 24 * 60 * 60 * 1000
_message_box = cast("Callable[[str, str, int], int]", gui.messageBox)  # pyright: ignore[reportUnknownMemberType]


class _UpdateDialog(gui.message.MessageDialog):
	def __init__(self, update: Update) -> None:
		self._notes = update.notes or _("No release notes are available.")
		super().__init__(  # pyright: ignore[reportUnknownMemberType]
			parent=None,
			message=_("Version {version} is available.\n\nDownload and install it now?").format(
				version=update.version
			),
			title=_("Shravya update"),
			buttons=gui.message.DefaultButtonSet.YES_NO,
		)
		_ignored = self.SetEscapeId(gui.message.ReturnCode.NO)

	@override
	def _addContents(self, contentsSizer: BoxSizerHelper) -> None:
		_ignored = contentsSizer.addLabeledControl(  # pyright: ignore[reportUnknownMemberType]
			_("&Release notes:"),
			wx.TextCtrl,
			value=self._notes,
			style=wx.TE_MULTILINE | wx.TE_READONLY,
			size=self.scaleSize((480, 200)),
		)


class Updater:
	def __init__(self, source: str | None) -> None:
		super().__init__()
		self._source = source
		self._stopped = Event()
		self._timer: _Timer | None = None
		self._busy = False
		if self._source is not None:
			wx.CallAfter(self._check)

	def _check(self) -> None:
		if self._stopped.is_set() or self._busy or self._source is None:
			return
		self._busy = True
		Thread(target=self._worker, args=(self._source,), daemon=True).start()

	def _worker(self, source: str) -> None:
		try:
			addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
			version = cast(str, addon.manifest["version"])
			update = check_release(source, version)
			error: Exception | None = None
		except (
			OSError,
			ValueError,
			TypeError,
			KeyError,
			RuntimeError,
			HTTPException,
			addonHandler.AddonError,
		) as caught:
			log.error("Could not check for Shravya updates", exc_info=True)
			update, error = None, caught
		wx.CallAfter(self._finish, update, error)

	def _finish(self, update: Update | None, error: Exception | None) -> None:
		if self._stopped.is_set():
			return
		if ui.isRunningOnSecureDesktop():
			self._timer = wx.CallLater(60_000, self._finish, update, error)
			return
		if not interaction_lock.acquire(blocking=False):
			self._timer = wx.CallLater(60_000, self._finish, update, error)
			return
		try:
			if error is None and update is not None:
				dialog = _UpdateDialog(update)
				answer = dialog.ShowModal()
				if answer == gui.message.ReturnCode.YES and not self._stopped.is_set():
					install_update(update, self._stopped)
		finally:
			interaction_lock.release()
			self._busy = False
			if not self._stopped.is_set():
				self._timer = wx.CallLater(_DAY_MS, self._check)

	def terminate(self) -> None:
		self._stopped.set()
		if self._timer is not None:
			self._timer.Stop()
