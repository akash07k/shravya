"""Accessible setup for the BlueV3 resource pack."""

from __future__ import annotations

import threading
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Protocol, cast

import addonHandler
import gui
import NVDAState
import ui
from logHandler import log
from systemUtils import ExecAndPump

from ._blue import validate_resources
from ._interaction import interaction_lock
from ._resources import RESOURCE_INTERFACE, resource_directories, take_pending_failure
from ._setup import (
	ResourceUpdate,
	Stage,
	check_resource_update,
	prepare_local,
	prepare_resource_update,
	prepare_split,
)

if TYPE_CHECKING:
	from gettext import gettext as _

	from addonHandler import Addon

	class _Timer(Protocol):
		def Stop(self) -> None: ...


class _Menu(Protocol):
	def Append(self, id: int, text: str) -> object: ...
	def DestroyItem(self, item: object) -> bool: ...


class _Tray(Protocol):
	toolsMenu: _Menu

	def Bind(self, event: object, handler: Callable[[object | None], None], item: object) -> None: ...
	def Unbind(self, event: object, *, handler: Callable[[object | None], None], source: object) -> bool: ...


class _Frame(Protocol):
	sysTrayIcon: _Tray

	def prePopup(self) -> None: ...
	def postPopup(self) -> None: ...


class _ProgressDialog(Protocol):
	def Update(self, value: int, message: str) -> tuple[bool, bool]: ...
	def Destroy(self) -> bool: ...


class _ChoiceDialog(Protocol):
	def ShowModal(self) -> int: ...
	def GetSelection(self) -> int: ...
	def Destroy(self) -> bool: ...


class _FileDialog(Protocol):
	def ShowModal(self) -> int: ...
	def GetPath(self) -> str: ...
	def Destroy(self) -> bool: ...


if TYPE_CHECKING:

	class _Wx(Protocol):
		ID_ANY: int
		ID_OK: int
		EVT_MENU: object
		YES: int
		NO: int
		OK: int
		ICON_INFORMATION: int
		ICON_ERROR: int
		PD_CAN_ABORT: int
		PD_ELAPSED_TIME: int
		FD_OPEN: int
		FD_FILE_MUST_EXIST: int

		def SingleChoiceDialog(
			self,
			parent: object,
			message: str,
			caption: str,
			choices: list[str],
		) -> _ChoiceDialog: ...
		def FileDialog(self, parent: object, message: str, *, wildcard: str, style: int) -> _FileDialog: ...
		def ProgressDialog(
			self,
			title: str,
			message: str,
			*,
			style: int,
			parent: object,
		) -> _ProgressDialog: ...
		def CallAfter(self, callback: Callable[..., None], *args: object) -> None: ...
		def CallLater(self, delay: int, callback: Callable[..., None], *args: object) -> _Timer: ...

	wx: _Wx
else:
	import wx

addonHandler.initTranslation()
_DAY_MS = 24 * 60 * 60 * 1000


class _MessageBox(Protocol):
	def __call__(self, message: str, caption: str, style: int, parent: object | None = None) -> int: ...


_message_box = cast(_MessageBox, gui.messageBox)  # pyright: ignore[reportUnknownMemberType]


class _Cancelled(Exception):
	pass


class ResourceSetup:
	def __init__(self, catalog_url: str | None) -> None:
		super().__init__()
		self._catalog_url = catalog_url
		self._terminated = False
		self._busy = False
		self._cancel = threading.Event()
		self._model_timer: _Timer | None = None
		self._prompt_timer: _Timer | None = None
		if gui.mainFrame is None:
			raise RuntimeError("NVDA's main window is unavailable")
		self._frame = cast(_Frame, gui.mainFrame)
		self._tray = self._frame.sysTrayIcon
		self._menu = self._tray.toolsMenu
		# Translators: Menu action to install or retry the Shravya voice resources.
		self._item = self._menu.Append(wx.ID_ANY, _("Set up Shravya &resources..."))
		self._tray.Bind(wx.EVT_MENU, self._on_menu, self._item)
		wx.CallAfter(self._prompt_if_needed)
		if self._catalog_url is not None:
			wx.CallAfter(self._check_model)

	def _prompt_if_needed(self) -> None:
		if self._terminated:
			return
		if not interaction_lock.acquire(blocking=False):
			self._prompt_timer = wx.CallLater(60_000, self._prompt_if_needed)
			return
		try:
			config_dir = Path(NVDAState.WritePaths.configDir)
			try:
				addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
				_directories = resource_directories(
					config_dir,
					addon_version=cast(str, addon.manifest["version"]),
				)
			except FileNotFoundError:
				pass
			except (OSError, ValueError, KeyError, addonHandler.AddonError):
				log.error("Could not check the Shravya resource selection", exc_info=True)
			if take_pending_failure(config_dir):
				_ignored = _message_box(
					_(
						"Shravya could not activate the prepared resources. The previous resources remain selected.",
					),
					_("Shravya resources"),
					wx.OK | wx.ICON_ERROR,
				)
			if self._catalog_url is not None:
				self._prompt_locked()
		finally:
			interaction_lock.release()

	def _prompt_locked(self) -> None:
		config_dir = Path(NVDAState.WritePaths.configDir)
		ready = self._resources_ready()
		if ready is None or ready:
			return
		marker = config_dir / "shravya" / f".{RESOURCE_INTERFACE}-prompted"
		try:
			if marker.exists():
				return
			marker.parent.mkdir(parents=True, exist_ok=True)
			marker.touch()
		except OSError:
			log.error("Could not save Shravya resource setup state", exc_info=True)
			_ignored = _message_box(_("Could not save Shravya setup state."), _("Shravya resources"), wx.OK)
			return
		self._ask()

	def _message(self, message: str, style: int, parent: object | None = None) -> int:
		if parent is None:
			return _message_box(message, _("Shravya resources"), style)
		return _message_box(message, _("Shravya resources"), style, parent=parent)

	@contextmanager
	def _popup(self, parent: object | None) -> Iterator[object]:
		if parent is None:
			self._frame.prePopup()
		try:
			yield self._frame if parent is None else parent
		finally:
			if parent is None:
				self._frame.postPopup()

	def _resources_ready(self, parent: object | None = None) -> bool | None:
		try:
			addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
			version = cast(str, addon.manifest["version"])
			directories = resource_directories(Path(NVDAState.WritePaths.configDir), addon_version=version)
			validate_resources(*directories)
		except FileNotFoundError:
			return False
		except (OSError, ValueError, KeyError, addonHandler.AddonError):
			log.error("Could not read Shravya resource selection", exc_info=True)
			_ignored = self._message(
				_("Could not read the Shravya resource selection."),
				wx.OK | wx.ICON_ERROR,
				parent,
			)
			return None
		return True

	def _on_menu(self, event: object | None) -> None:
		self.open()

	def open(self, parent: object | None = None) -> None:
		if self._terminated:
			raise RuntimeError("Shravya resource setup has terminated")
		if not interaction_lock.acquire(blocking=False):
			_ignored = self._message(_("Another Shravya operation is in progress."), wx.OK, parent)
			return
		try:
			if self._busy:
				_ignored = self._message(_("Shravya resource setup is already running."), wx.OK, parent)
				return
			if self._catalog_url is None:
				self._run_local(parent)
				return
			with self._popup(parent) as owner:
				dialog = wx.SingleChoiceDialog(
					owner,
					_("Choose how to set up resources."),
					_("Shravya resources"),
					[_("Download resources"), _("Install local ZIP files")],
				)
				try:
					if dialog.ShowModal() != wx.ID_OK:
						return
					selection = dialog.GetSelection()
				finally:
					_ignored = dialog.Destroy()
			if selection == 0:
				self._ask(parent)
			elif selection == 1:
				self._run_local(parent)
		finally:
			interaction_lock.release()

	def _check_model(self) -> None:
		if self._terminated or self._catalog_url is None:
			return
		if self._busy or interaction_lock.locked():
			self._model_timer = wx.CallLater(60_000, self._check_model)
			return
		if not self._resources_ready():
			self._model_timer = wx.CallLater(_DAY_MS, self._check_model)
			return
		self._busy = True
		Thread(target=self._model_worker, args=(self._catalog_url,), daemon=True).start()

	def _model_worker(self, source: str) -> None:
		try:
			addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
			version = cast(str, addon.manifest["version"])
			update = check_resource_update(source, Path(NVDAState.WritePaths.configDir), version)
			error: Exception | None = None
		except (
			OSError,
			ValueError,
			TypeError,
			RuntimeError,
			KeyError,
			HTTPException,
			addonHandler.AddonError,
		) as caught:
			log.error("Could not check for Shravya resource updates", exc_info=True)
			update, error = None, caught
		wx.CallAfter(self._finish_model, update, error)

	def _finish_model(self, update: ResourceUpdate | None, error: Exception | None) -> None:
		if self._terminated:
			return
		if ui.isRunningOnSecureDesktop() or not interaction_lock.acquire(blocking=False):
			self._model_timer = wx.CallLater(60_000, self._finish_model, update, error)
			return
		try:
			if error is None and update is not None:
				answer = _message_box(
					_("A Shravya resource update is available. Download it now?"),
					_("Shravya resources"),
					wx.YES | wx.NO | wx.ICON_INFORMATION,
				)
				if answer == wx.YES and not self._terminated:
					self._cancel.clear()
					self._frame.prePopup()
					try:
						self._run_dialog(None, resource_update=update)
					finally:
						self._frame.postPopup()
		finally:
			interaction_lock.release()
			self._busy = False
			if not self._terminated:
				self._model_timer = wx.CallLater(_DAY_MS, self._check_model)

	def _ask(self, parent: object | None = None) -> None:
		if self._busy:
			_ignored = self._message(_("Shravya resource setup is already running."), wx.OK, parent)
			return
		ready = self._resources_ready(parent)
		if ready is None:
			return
		if ready:
			_ignored = self._message(_("Shravya resources are already installed."), wx.OK, parent)
			return
		answer = self._message(
			_("Download and install the BlueV3 speech resources now?"),
			wx.YES | wx.NO | wx.ICON_INFORMATION,
			parent,
		)
		if answer == wx.YES:
			self._run(parent)

	def _run(self, parent: object | None = None) -> None:
		url = self._catalog_url
		if url is None:
			return
		self._busy = True
		self._cancel.clear()
		try:
			with self._popup(parent):
				self._run_dialog(url, parent=parent)
		finally:
			self._busy = False

	def _run_local(self, parent: object | None = None) -> None:
		self._busy = True
		self._cancel.clear()
		try:
			with self._popup(parent) as owner:
				archives: list[Path] = []
				for message in (_("Select the runtime ZIP"), _("Select the model ZIP")):
					dialog = wx.FileDialog(
						owner,
						message,
						wildcard=_("ZIP files (*.zip)|*.zip"),
						style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
					)
					try:
						if dialog.ShowModal() != wx.ID_OK:
							return
						archives.append(Path(dialog.GetPath()))
					finally:
						_ignored = dialog.Destroy()
				self._run_dialog(None, local=(archives[0], archives[1]), parent=parent)
		finally:
			self._busy = False

	def _run_dialog(
		self,
		url: str | None,
		*,
		resource_update: ResourceUpdate | None = None,
		local: tuple[Path, Path] | None = None,
		parent: object | None = None,
	) -> None:
		dialog = wx.ProgressDialog(
			_("Shravya resources"),
			_("Preparing download"),
			style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME,
			parent=self._frame if parent is None else parent,
		)
		active = True
		last: tuple[Stage, int] | None = None

		def display(stage: Stage, percent: int) -> None:
			if active:
				message = _("Downloading resources") if stage == "download" else _("Extracting resources")
				if not dialog.Update(percent, message)[0]:
					self._cancel.set()

		def progress(stage: Stage, done: int, total: int) -> None:
			nonlocal last
			if self._cancel.is_set():
				raise _Cancelled()
			percent = min(99, done * 100 // total) if total else 0
			if last != (stage, percent):
				last = stage, percent
				wx.CallAfter(display, stage, percent)

		try:
			config_dir = Path(NVDAState.WritePaths.configDir)
			if local is not None:
				local_result = ExecAndPump(prepare_local, local[0], local[1], config_dir, progress).funcRes
				if local_result is None:
					raise RuntimeError("Local resource setup did not return a result")
				message = (
					_("Restart NVDA to use the new Shravya resources.")
					if local_result
					else _("Shravya resources are ready. Select Shravya to use them.")
				)
			elif resource_update is not None:
				update_result = ExecAndPump(
					prepare_resource_update,
					resource_update,
					config_dir,
					progress,
				).funcRes
				if update_result is None:
					raise RuntimeError("Resource update did not return a result")
				clean, restart = update_result
				if not clean and restart:
					message = _(
						"Restart NVDA to use the new Shravya resources. A downloaded archive could not be removed.",
					)
				elif not clean:
					message = _("Shravya resources are ready, but a downloaded archive could not be removed.")
				elif restart:
					message = _("Restart NVDA to use the new Shravya resources.")
				else:
					message = _(
						"The Shravya model update is ready and will be used on the next model reload.",
					)
			else:
				if url is None:
					raise ValueError("A resource catalog URL is required")
				addon = cast("Addon", addonHandler.getCodeAddon())  # pyright: ignore[reportUnknownMemberType]
				version = cast(str, addon.manifest["version"])
				result = ExecAndPump(prepare_split, url, config_dir, version, progress).funcRes
				if result is None:
					raise RuntimeError("Resource setup did not return a result")
				message = (
					_("Shravya resources are ready. Select Shravya to use them.")
					if result
					else _("Shravya resources are ready, but the downloaded archive could not be removed.")
				)
			style = wx.OK | wx.ICON_INFORMATION
		except _Cancelled:
			message = _("Shravya resource setup was cancelled.")
			style = wx.OK
		except (OSError, ValueError, RuntimeError, KeyError, HTTPException, zipfile.BadZipFile):
			log.error("Shravya resource setup failed", exc_info=True)
			message = _("Shravya resource setup failed. You can retry from the Tools menu.")
			style = wx.OK | wx.ICON_ERROR
		finally:
			active = False
			_ignored = dialog.Destroy()
		_ignored = self._message(message, style, parent)

	def terminate(self) -> None:
		self._terminated = True
		self._cancel.set()
		if self._model_timer is not None:
			self._model_timer.Stop()
		if self._prompt_timer is not None:
			self._prompt_timer.Stop()
		_ = self._tray.Unbind(wx.EVT_MENU, handler=self._on_menu, source=self._item)
		_ = self._menu.DestroyItem(self._item)
