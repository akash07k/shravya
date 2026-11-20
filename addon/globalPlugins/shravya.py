"""Shravya's NVDA settings panel."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, ClassVar, Protocol, cast, override

import addonHandler
import config
import core
import globalPluginHandler
import globalVars
import gui.guiHelper
import gui.settingsDialogs
import inputCore
import keyboardHandler
import nvwave
import speech
import synthDriverHandler
import ui
from logHandler import log
from scriptHandler import script
from synthDrivers.shravya import SynthDriver, _settings
from synthDrivers.shravya._delivery_config import load_delivery_config
from synthDrivers.shravya._diagnostic_input import InputObserver
from synthDrivers.shravya._diagnostic_measurements import TimingEvent
from synthDrivers.shravya._diagnostic_report import format_report
from synthDrivers.shravya._resource_ui import ResourceSetup
from synthDrivers.shravya._update_ui import Updater
from winAPI.secureDesktop import post_secureDesktopStateChange

if TYPE_CHECKING:
	from gettext import gettext as _
	from synthDrivers.shravya._diagnostic_capture import DiagnosticCapture
	from synthDrivers.shravya._worker import RateMethod
else:
	import wx

addonHandler.initTranslation()
_RATE_METHODS: tuple[RateMethod, ...] = ("native", "sonic")


class _Choice(Protocol):
	def SetSelection(self, selection: int) -> None: ...
	def GetSelection(self) -> int: ...


class _Spin(Protocol):
	def GetValue(self) -> int: ...


class _Button(Protocol):
	def Bind(self, event: object, handler: object) -> None: ...
	def Enable(self, enable: bool) -> None: ...


if TYPE_CHECKING:

	class _Wx(Protocol):
		Choice: type[_Choice]
		SpinCtrl: type[_Spin]
		EVT_BUTTON: object

		def Button(self, parent: object, *, label: str) -> _Button: ...

	wx: _Wx


def _apply_active_synth() -> None:
	synth = synthDriverHandler.getSynth()
	if isinstance(synth, SynthDriver):
		synth.apply_settings()


class ShravyaSettingsPanel(gui.settingsDialogs.SettingsPanel):
	# Translators: Name of the Shravya settings category.
	title = _("Shravya")
	resource_setup: ClassVar[ResourceSetup | None] = None

	@override
	def makeSettings(self, sizer: object) -> None:
		helper = gui.guiHelper.BoxSizerHelper(self, sizer=sizer)
		# Translators: Select the way Shravya changes speech rate.
		self.rate_method = helper.addLabeledControl(  # pyright: ignore[reportUnknownMemberType]
			_("&Rate method:"), wx.Choice, choices=[_("BlueV3 native"), _("Sonic")]
		)
		self.rate_method.SetSelection(_RATE_METHODS.index(_settings.rate_method()))
		# Translators: Select the processor used by Shravya.
		self.compute = helper.addLabeledControl(  # pyright: ignore[reportUnknownMemberType]
			_("&Compute device:"), wx.Choice, choices=[_("CPU"), _("NVIDIA CUDA")]
		)
		self.compute.SetSelection(1 if _settings.use_cuda() else 0)
		# Translators: Select the number of BlueV3 inference steps.
		self.steps = helper.addLabeledControl(  # pyright: ignore[reportUnknownMemberType]
			_("&Inference steps:"), wx.SpinCtrl, min=1, max=_settings.MAX_STEPS, initial=_settings.steps()
		)
		# Translators: Set the fixed seed for repeatable speech.
		self.seed = helper.addLabeledControl(  # pyright: ignore[reportUnknownMemberType]
			_("Random &seed:"), wx.SpinCtrl, min=0, max=_settings.MAX_SEED, initial=_settings.seed()
		)
		# Translators: Open the Shravya resource setup dialog.
		button = helper.addItem(wx.Button(self, label=_("Set up Shravya &resources...")))  # pyright: ignore[reportUnknownMemberType]
		button.Enable(self.resource_setup is not None)
		button.Bind(wx.EVT_BUTTON, self._setup_resources)

	def _setup_resources(self, event: object) -> None:
		setup = self.resource_setup
		if setup is None:
			ui.message(_("Shravya resource setup is unavailable."))
			return
		setup.open(self)

	@override
	def onSave(self) -> None:
		rate_index = self.rate_method.GetSelection()
		compute_index = self.compute.GetSelection()
		if not 0 <= rate_index < len(_RATE_METHODS) or compute_index not in (0, 1):
			raise ValueError("A Shravya settings selection is missing")
		_settings.save(
			_RATE_METHODS[rate_index], compute_index == 1, self.steps.GetValue(), self.seed.GetValue()
		)
		_apply_active_synth()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._registered = False
		self._capture: DiagnosticCapture | None = None
		self._capture_synth: SynthDriver | None = None
		self._capture_endpoint: str | None = None
		self._observer: InputObserver | None = None
		self._stopping = False
		self._terminated = False
		self._secure_desktop = False
		self._discard_report = False
		self._deferred_message: str | None = None
		self._resource_setup: ResourceSetup | None = None
		self._updater: Updater | None = None
		if globalVars.appArgs.secure:
			return
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(ShravyaSettingsPanel)
		config.post_configProfileSwitch.register(self._on_profile_switch)
		synthDriverHandler.synthChanged.register(self._on_synth_changed)
		post_secureDesktopStateChange.register(self._on_secure_desktop_change)
		self._registered = True
		try:
			delivery = load_delivery_config()
		except (OSError, ValueError):
			log.error("Could not read Shravya delivery configuration", exc_info=True)
			ui.message(_("Shravya delivery configuration is invalid. Check the NVDA log."))
		else:
			self._resource_setup = ResourceSetup(delivery.catalog_url)
			ShravyaSettingsPanel.resource_setup = self._resource_setup
			self._updater = Updater(delivery.release_api)

	def _on_profile_switch(self) -> None:
		if self._capture is not None and self._output_device() != self._capture_endpoint:
			self._stop_capture(False, _("Audio output changed; diagnostic capture was discarded."))
		_apply_active_synth()

	@staticmethod
	def _output_device() -> str:
		return cast(str, config.conf["audio"]["outputDevice"])  # pyright: ignore[reportIndexIssue]

	def _on_synth_changed(self, synth: synthDriverHandler.SynthDriver) -> None:
		if self._capture is not None and synth is not self._capture_synth:
			self._stop_capture(False, _("Synthesizer changed; diagnostic capture was discarded."))

	def _on_secure_desktop_change(self, isSecureDesktop: bool) -> None:
		self._secure_desktop = isSecureDesktop
		if isSecureDesktop:
			if self._stopping:
				self._discard_report = True
			self._stop_capture(False, _("Secure desktop opened; diagnostic capture was discarded."))
		elif self._deferred_message is not None:
			message, self._deferred_message = self._deferred_message, None
			ui.message(message)

	# Translators: Label of a command in the Input Gestures dialog.
	@script(
		description=_("Start or stop Shravya latency capture"),
		category=_("Shravya"),
		gesture="kb:NVDA+shift+f11",
	)
	def script_toggleDiagnostic(self, gesture: inputCore.InputGesture) -> None:
		secure = globalVars.appArgs.secure or self._secure_desktop or ui.isRunningOnSecureDesktop()
		if self._capture is not None:
			valid = synthDriverHandler.getSynth() is self._capture_synth
			valid = valid and self._output_device() == self._capture_endpoint and not secure
			self._stop_capture(valid, None if valid else _("Diagnostic capture was discarded."))
			return
		if secure:
			ui.message(_("Shravya latency capture is unavailable on secure screens."))
			return
		if self._stopping:
			ui.message(_("Shravya latency capture is still stopping."))
			return
		synth = synthDriverHandler.getSynth()
		if not isinstance(synth, SynthDriver):
			ui.message(_("Select Shravya before starting latency capture."))
			return
		try:
			from synthDrivers.shravya._diagnostic_capture import DiagnosticCapture

			endpoint = self._output_device()

			def ready(actual: str) -> None:
				core.callLater(0, self._capture_ready, capture)  # pyright: ignore[reportUnknownMemberType]

			def failed(error: Exception) -> None:
				core.callLater(0, self._capture_failed, capture, error)  # pyright: ignore[reportUnknownMemberType]

			capture = DiagnosticCapture(
				None if endpoint == nvwave.WavePlayer.DEFAULT_DEVICE_KEY else endpoint,
				ready,
				failed,
			)
			self._capture, self._capture_synth, self._capture_endpoint = capture, synth, endpoint
			capture.start()
		except (ImportError, RuntimeError):
			self._capture = None
			self._capture_synth = None
			log.error("Could not start Shravya latency capture", exc_info=True)
			ui.message(_("Could not start Shravya latency capture."))
			return
		ui.message(_("Starting Shravya latency capture."))

	def _capture_ready(self, capture: DiagnosticCapture) -> None:
		if self._capture is not capture:
			return
		synth = self._capture_synth
		if (
			synth is None
			or synthDriverHandler.getSynth() is not synth
			or self._output_device() != self._capture_endpoint
			or self._secure_desktop
			or ui.isRunningOnSecureDesktop()
		):
			self._stop_capture(False, _("Speech output changed; diagnostic capture was discarded."))
			return
		try:
			synth.set_diagnostic(capture.record)
			observer = InputObserver(
				capture.record, self.script_controlledDiagnostic, self.script_toggleDiagnostic
			)
			observer.start()
			self._observer = observer
		except Exception:
			log.error("Could not observe Shravya latency events", exc_info=True)
			self._stop_capture(False, _("Could not start Shravya latency capture."))
			return
		ui.message(_("Shravya latency capture is ready."))

	def _capture_failed(self, capture: DiagnosticCapture, error: Exception) -> None:
		if self._capture is capture:
			log.error("Shravya latency capture failed", exc_info=(type(error), error, error.__traceback__))
			self._stop_capture(False, _("Shravya latency capture failed."))

	# Translators: Label of a command in the Input Gestures dialog.
	@script(
		description=_("Speak the Shravya latency test phrase"),
		category=_("Shravya"),
		gesture="kb:NVDA+shift+f12",
	)
	def script_controlledDiagnostic(self, gesture: inputCore.InputGesture) -> None:
		if self._observer is None or not isinstance(gesture, keyboardHandler.KeyboardInputGesture):
			ui.message(_("Start latency capture and use a keyboard gesture for the test."))
			return
		# Translators: The fixed phrase used by the controlled latency diagnostic.
		speech.speakMessage(_("The quick brown fox jumps over the lazy dog."))

	def _stop_capture(self, report: bool, message: str | None = None) -> None:
		capture, synth, observer = self._capture, self._capture_synth, self._observer
		if capture is None:
			return
		self._capture = self._capture_synth = self._observer = None
		if observer is not None:
			observer.stop()
		if synth is not None:
			synth.set_diagnostic(None)
		self._stopping = True
		threading.Thread(
			target=self._collect_capture,
			args=(capture, report, message),
			name="Shravya diagnostic stop",
			daemon=True,
		).start()

	def _collect_capture(self, capture: DiagnosticCapture, report: bool, message: str | None) -> None:
		events: list[TimingEvent] | None = None
		try:
			events = capture.stop()
		except Exception:
			log.error("Could not finish Shravya latency capture", exc_info=True)
			message = _("Shravya latency capture failed.")
		core.callLater(0, self._capture_stopped, events if report else None, message)  # pyright: ignore[reportUnknownMemberType]

	def _capture_stopped(self, events: list[TimingEvent] | None, message: str | None) -> None:
		self._stopping = False
		if self._terminated:
			return
		if self._discard_report:
			events = None
			message = message or _("Secure desktop opened; diagnostic capture was discarded.")
			self._discard_report = False
		if self._secure_desktop:
			self._deferred_message = message
			return
		if message is not None:
			ui.message(message)
		elif events is not None:
			ui.browseableMessage(
				format_report(events), title=_("Shravya latency report"), closeButton=True, copyButton=True
			)

	@override
	def terminate(self) -> None:
		self._terminated = True
		self._stop_capture(False)
		if self._updater is not None:
			self._updater.terminate()
			self._updater = None
		if self._resource_setup is not None:
			if ShravyaSettingsPanel.resource_setup is self._resource_setup:
				ShravyaSettingsPanel.resource_setup = None
			self._resource_setup.terminate()
			self._resource_setup = None
		if self._registered:
			_ = config.post_configProfileSwitch.unregister(self._on_profile_switch)
			_ = synthDriverHandler.synthChanged.unregister(self._on_synth_changed)
			_ = post_secureDesktopStateChange.unregister(self._on_secure_desktop_change)
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(ShravyaSettingsPanel)
			self._registered = False
		super().terminate()
