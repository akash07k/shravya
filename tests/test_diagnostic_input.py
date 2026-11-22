import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "_diagnostic_input.py"


class Hook:
	def __init__(self):
		self.handlers = []

	def register(self, handler):
		self.handlers.append(handler)

	def unregister(self, handler):
		self.handlers.remove(handler)

	def decide(self, **kwargs):
		return all(handler(**kwargs) for handler in self.handlers)


class Gesture:
	def __init__(self, *, script=None, modifier=False, vk=65):
		self.script = script
		self.isModifier = modifier
		self.vkCode = vk
		self.scanCode = 30
		self.isExtended = False


def load_input():
	package = ModuleType("shravya_input_test")
	package.__path__ = [str(PATH.parent)]
	input_core = ModuleType("inputCore")
	input_core.InputGesture = object
	input_core.decide_handleRawKey = Hook()
	input_core.decide_executeGesture = Hook()
	keyboard = ModuleType("keyboardHandler")
	keyboard.KeyboardInputGesture = Gesture
	spec = importlib.util.spec_from_file_location("shravya_input_test._diagnostic_input", PATH)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		sys.modules,
		{"shravya_input_test": package, "inputCore": input_core, "keyboardHandler": keyboard},
	):
		spec.loader.exec_module(module)
	return module, input_core


class TestDiagnosticInput(unittest.TestCase):
	def setUp(self):
		self.module, self.hooks = load_input()
		self.events = []
		self.scripts = SimpleNamespace(controlled=lambda gesture: None, toggle=lambda gesture: None)
		self.observer = self.module.InputObserver(
			self.events.append, self.scripts.controlled, self.scripts.toggle,
		)

	def raw(self, *, pressed=True, injected=False, vk=65):
		return self.hooks.decide_handleRawKey.decide(
			vkCode=vk, scanCode=30, extended=False, pressed=pressed, injected=injected,
		)

	def gesture(self, gesture):
		return self.hooks.decide_executeGesture.decide(gesture=gesture)

	def test_registers_only_during_capture_and_preserves_input(self):
		self.assertEqual(self.hooks.decide_handleRawKey.handlers, [])
		self.observer.start()
		self.assertTrue(self.raw())
		self.assertTrue(self.gesture(Gesture()))
		self.observer.stop()
		self.assertEqual(self.hooks.decide_handleRawKey.handlers, [])
		self.assertEqual(self.hooks.decide_executeGesture.handlers, [])
		self.assertEqual(len(self.events), 1)
		self.assertEqual(self.events[0][1:], ("key", "live"))
		self.assertGreater(self.events[0][0], 0)

	def test_distinguishes_controlled_and_toggle_gestures(self):
		self.observer.start()
		try:
			self.raw()
			self.gesture(Gesture(script=self.scripts.controlled))
			self.raw()
			self.gesture(Gesture(script=self.scripts.toggle))
		finally:
			self.observer.stop()
		self.assertEqual([event[2] for event in self.events], ["controlled"])

	def test_ignores_modifiers_injected_input_and_unmatched_gestures(self):
		self.observer.start()
		try:
			self.raw()
			self.gesture(Gesture(modifier=True))
			self.raw(injected=True)
			self.gesture(Gesture())
			self.raw()
			self.gesture(Gesture(vk=66))
			self.gesture(Gesture())
			self.raw()
			self.raw(pressed=False)
			self.gesture(Gesture())
			self.raw()
			self.gesture(object())
		finally:
			self.observer.stop()
		self.assertEqual(self.events, [])

	def test_does_not_match_events_from_another_thread(self):
		self.observer.start()
		try:
			self.raw()
			thread = threading.Thread(target=lambda: self.gesture(Gesture()))
			thread.start()
			thread.join()
			self.assertEqual(self.events, [])
		finally:
			self.observer.stop()

	def test_registration_failure_removes_raw_hook(self):
		with patch.object(self.hooks.decide_executeGesture, "register", side_effect=RuntimeError("failed")):
			with self.assertRaisesRegex(RuntimeError, "failed"):
				self.observer.start()
		self.assertEqual(self.hooks.decide_handleRawKey.handlers, [])


if __name__ == "__main__":
	unittest.main()
