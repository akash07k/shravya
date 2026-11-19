"""Shravya settings stored in NVDA's active configuration profile."""

from __future__ import annotations

from typing import Protocol, cast

import config

from ._worker import RateMethod

MAX_STEPS = 2**31 - 1
MAX_SEED = 2**31 - 1

config.conf.spec["shravya"] = {  # pyright: ignore[reportUnknownMemberType]
	"rateMethod": "option(native, sonic, default=native)",
	"useCuda": "boolean(default=True)",
	"steps": f"integer(min=1, max={MAX_STEPS}, default=4)",
	"seed": f"integer(min=0, max={MAX_SEED}, default=0)",
}


class _Section(Protocol):
	def __getitem__(self, key: str) -> object: ...
	def __setitem__(self, key: str, value: object) -> None: ...


def _section() -> _Section:
	return cast(_Section, config.conf["shravya"])


def rate_method() -> RateMethod:
	value = _section()["rateMethod"]
	if value not in ("native", "sonic"):
		raise ValueError(f"Unknown rate method: {value}")
	return value


def use_cuda() -> bool:
	value = _section()["useCuda"]
	if not isinstance(value, bool):
		raise ValueError("CUDA setting must be a boolean")
	return value


def steps() -> int:
	value = _section()["steps"]
	if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_STEPS:
		raise ValueError("Step count is outside the supported numeric range")
	return value


def seed() -> int:
	value = _section()["seed"]
	if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SEED:
		raise ValueError("Seed is outside the supported numeric range")
	return value


def save(method: RateMethod, cuda: bool, count: int, seed_value: int) -> None:
	if method not in ("native", "sonic"):
		raise ValueError(f"Unknown rate method: {method}")
	if isinstance(count, bool) or not 1 <= count <= MAX_STEPS:
		raise ValueError("Step count is outside the supported numeric range")
	if isinstance(seed_value, bool) or not 0 <= seed_value <= MAX_SEED:
		raise ValueError("Seed is outside the supported numeric range")
	section = _section()
	section["rateMethod"] = method
	section["useCuda"] = cuda
	section["steps"] = count
	section["seed"] = seed_value
