"""Observe physical keyboard gestures only while a diagnostic is active."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import cast

import inputCore
import keyboardHandler

from ._diagnostic_measurements import TimingEvent


class _Pending(threading.local):
	def __init__(self) -> None:
		super().__init__()
		self.event: tuple[int, int, bool, int] | None = None


class InputObserver:
	def __init__(
		self,
		record: Callable[[TimingEvent], None],
		controlled: Callable[[inputCore.InputGesture], None],
		toggle: Callable[[inputCore.InputGesture], None],
	) -> None:
		super().__init__()
		self._record = record
		self._controlled = controlled
		self._toggle = toggle
		self._pending = _Pending()
		self._active = False

	def start(self) -> None:
		self._active = True
		inputCore.decide_handleRawKey.register(self._raw_key)
		try:
			inputCore.decide_executeGesture.register(self._gesture)
		except Exception:
			self._active = False
			_ = inputCore.decide_handleRawKey.unregister(self._raw_key)
			raise

	def stop(self) -> None:
		self._active = False
		_ = inputCore.decide_executeGesture.unregister(self._gesture)
		_ = inputCore.decide_handleRawKey.unregister(self._raw_key)

	def _raw_key(self, vkCode: int, scanCode: int, extended: bool, pressed: bool, injected: bool) -> bool:
		self._pending.event = (
			(vkCode, scanCode, extended, time.perf_counter_ns())
			if self._active and pressed and not injected
			else None
		)
		return True

	def _gesture(self, gesture: inputCore.InputGesture) -> bool:
		pending = self._pending.event
		self._pending.event = None
		if (
			not self._active
			or pending is None
			or not isinstance(gesture, keyboardHandler.KeyboardInputGesture)
			or gesture.isModifier
			or (
				cast(int, gesture.vkCode),  # pyright: ignore[reportUnknownMemberType]
				cast(int, gesture.scanCode),  # pyright: ignore[reportUnknownMemberType]
				cast(bool, gesture.isExtended),  # pyright: ignore[reportUnknownMemberType]
			)
			!= pending[:3]
		):
			return True
		script = gesture.script
		if script != self._toggle:
			self._record((pending[3], "key", "controlled" if script == self._controlled else "live"))
		return True
