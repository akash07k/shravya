"""Capture packets from a Windows output endpoint while explicitly requested."""

from __future__ import annotations

import ctypes
import struct
import time
import uuid
from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast, override

if TYPE_CHECKING:
	from comtypes import IUnknown

	from ._audio_onset import SampleEncoding


class AudioFormat(NamedTuple):
	sample_rate: int
	channels: int
	encoding: SampleEncoding
	frame_bytes: int


def describe_mix_format(data: bytes) -> AudioFormat:
	if len(data) < 18:
		raise ValueError("The audio mix format is truncated")
	tag, channels, rate, _, frame_bytes, bits, extra = struct.unpack_from("<HHIIHHH", data)
	if len(data) < 18 + extra or not channels or not rate or frame_bytes != channels * (bits // 8):
		raise ValueError("The audio mix format is invalid")
	if tag == 0xFFFE:
		if extra < 22:
			raise ValueError("The extensible audio mix format is truncated")
		valid_bits = struct.unpack_from("<H", data, 18)[0]
		if not 0 < valid_bits <= bits:
			raise ValueError("The audio mix precision is invalid")
		subformat = uuid.UUID(bytes_le=data[24:40])
		if subformat == uuid.UUID("{00000001-0000-0010-8000-00aa00389b71}"):
			tag = 1
		elif subformat == uuid.UUID("{00000003-0000-0010-8000-00aa00389b71}"):
			tag = 3
	if tag == 3 and bits == 32:
		encoding: SampleEncoding = "float32"
	elif tag == 1 and bits == 16:
		encoding = "pcm16"
	elif tag == 1 and bits == 24:
		encoding = "pcm24"
	elif tag == 1 and bits == 32:
		encoding = "pcm32"
	else:
		raise ValueError(f"Unsupported audio mix format: tag={tag:#x}, bits={bits}")
	return AudioFormat(rate, channels, encoding, frame_bytes)


class _Unknown(Protocol):
	def QueryInterface(self, interface: type[object]) -> object: ...


class _Endpoint(Protocol):
	def GetId(self) -> str: ...
	def GetState(self) -> int: ...
	def QueryInterface(self, interface: type[object]) -> object: ...
	def Activate(self, iid: object, context: int, parameters: None) -> _Unknown: ...


class _EndpointFlow(Protocol):
	def GetDataFlow(self) -> int: ...


class _Enumerator(Protocol):
	def GetDevice(self, endpoint_id: str) -> _Endpoint: ...
	def GetDefaultAudioEndpoint(self, flow: int, role: int) -> _Endpoint: ...
	def RegisterEndpointNotificationCallback(self, monitor: object) -> None: ...
	def UnregisterEndpointNotificationCallback(self, monitor: object) -> None: ...


class _AudioClient(Protocol):
	def GetMixFormat(self) -> ctypes.c_void_p: ...
	def Initialize(
		self,
		share_mode: int,
		flags: int,
		duration: int,
		periodicity: int,
		mix: ctypes.c_void_p,
		session: None,
	) -> None: ...
	def GetService(self, iid: object) -> _Unknown: ...
	def Start(self) -> None: ...
	def Stop(self) -> None: ...


class _CaptureClient(Protocol):
	def GetNextPacketSize(self) -> int: ...
	def GetBuffer(self) -> tuple[ctypes.c_void_p, int, int, int, int]: ...
	def ReleaseBuffer(self, frames: int) -> None: ...


class _ChangeMonitor(Protocol):
	changed: bool

	def QueryInterface(self, interface: type[IUnknown]) -> object: ...


def _device_change_monitor() -> _ChangeMonitor:
	from pycaw.callbacks import MMNotificationClient

	class Monitor(MMNotificationClient):
		def __init__(self) -> None:
			super().__init__()
			self.changed = False

		@override
		def OnDefaultDeviceChanged(self, flow_id: int, role_id: int, default_device_id: str | None) -> None:
			if flow_id == 0 and role_id == 0:
				self.changed = True

		@override
		def OnDeviceAdded(self, added_device_id: str) -> None:
			self.changed = True

		@override
		def OnDeviceRemoved(self, removed_device_id: str) -> None:
			self.changed = True

		@override
		def OnDeviceStateChanged(self, device_id: str, new_state_id: int) -> None:
			self.changed = True

	return Monitor()


def capture_packets(
	endpoint_id: str | None,
	stop: Event,
	on_ready: Callable[[AudioFormat, str], None],
	on_packet: Callable[[bytes, int, int, int], None],
) -> None:
	from ctypes import HRESULT, POINTER, c_byte, c_uint32, c_uint64
	from ctypes.wintypes import DWORD

	import comtypes
	from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown  # pyright: ignore[reportUnknownVariableType]
	from pycaw.api.audioclient import IAudioClient
	from pycaw.api.mmdeviceapi import IMMEndpoint, IMMNotificationClient
	from pycaw.constants import EDataFlow, ERole
	from pycaw.utils import AudioUtilities

	class IAudioCaptureClient(IUnknown):
		_iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
		_methods_ = [
			COMMETHOD(
				[],
				HRESULT,
				"GetBuffer",
				(["out"], POINTER(POINTER(c_byte)), "data"),
				(["out"], POINTER(c_uint32), "frames"),
				(["out"], POINTER(DWORD), "flags"),
				(["out"], POINTER(c_uint64), "device_position"),
				(["out"], POINTER(c_uint64), "qpc_position"),
			),
			COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
			COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
		]

	comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)  # pyright: ignore[reportUnknownMemberType]
	enumerator: _Enumerator | None = None
	endpoint: _Endpoint | None = None
	client: _AudioClient | None = None
	capture: _CaptureClient | None = None
	monitor: _ChangeMonitor | None = None
	notification: object | None = None
	registered = False
	started = False
	changed_during_cleanup = False
	try:
		enumerator = cast(_Enumerator, AudioUtilities.GetDeviceEnumerator())
		monitor = _device_change_monitor()
		notification = monitor.QueryInterface(IMMNotificationClient)
		enumerator.RegisterEndpointNotificationCallback(notification)
		registered = True

		def resolve(enumerator: _Enumerator) -> _Endpoint:
			if endpoint_id:
				endpoint = enumerator.GetDevice(endpoint_id)
			else:
				endpoint = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eConsole.value)
			flow = cast(_EndpointFlow, endpoint.QueryInterface(IMMEndpoint))
			if endpoint.GetState() != 1 or flow.GetDataFlow() != EDataFlow.eRender.value:
				raise RuntimeError("The selected speech output is not an active render endpoint")
			return endpoint

		endpoint = resolve(enumerator)
		actual_id = endpoint.GetId()
		client = cast(
			_AudioClient,
			endpoint.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient),  # pyright: ignore[reportPrivateUsage]
		)
		mix = client.GetMixFormat()
		try:
			header = ctypes.string_at(mix, 18)
			extra = struct.unpack_from("<H", header, 16)[0]
			format = describe_mix_format(ctypes.string_at(mix, 18 + extra))
			client.Initialize(0, 0x20000, 10_000_000, 0, mix, None)
		finally:
			ctypes.windll.ole32.CoTaskMemFree(mix)
		capture = cast(
			_CaptureClient,
			client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient),  # pyright: ignore[reportPrivateUsage]
		)
		client.Start()
		started = True
		on_ready(format, actual_id)
		next_endpoint_check = time.monotonic() + 0.1
		while not stop.is_set():
			if monitor.changed:
				raise RuntimeError("An audio endpoint changed during capture")
			if time.monotonic() >= next_endpoint_check:
				if resolve(enumerator).GetId() != actual_id:
					raise RuntimeError("The speech output endpoint changed during capture")
				next_endpoint_check = time.monotonic() + 0.1
			while capture.GetNextPacketSize() and not stop.is_set():
				data, frames, flags, _, qpc = capture.GetBuffer()
				try:
					samples = b"" if flags & 2 else ctypes.string_at(data, frames * format.frame_bytes)
					on_packet(samples, frames, flags, qpc * 100)
				finally:
					capture.ReleaseBuffer(frames)
			_ = stop.wait(0.01)
		if monitor.changed or resolve(enumerator).GetId() != actual_id:
			raise RuntimeError("The speech output endpoint changed during capture")
	finally:
		try:
			if started and client is not None:
				client.Stop()
		finally:
			try:
				if registered and enumerator is not None and notification is not None:
					enumerator.UnregisterEndpointNotificationCallback(notification)
					changed_during_cleanup = monitor is not None and monitor.changed
			finally:
				capture = None
				client = None
				endpoint = None
				notification = None
				monitor = None
				enumerator = None
				comtypes.CoUninitialize()
	if changed_during_cleanup:
		raise RuntimeError("An audio endpoint changed during capture")
