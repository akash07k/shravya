"""Run CPU synthesis with a validated runtime/model archive pair."""

import argparse
import importlib
import os
import subprocess
import sys
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Protocol, cast

from tools.build_resources import pack_id


class _Engine(Protocol):
	def stream(self, text: str, *, speed: float, steps: int) -> Iterator[bytes]: ...
	def close(self) -> None: ...


class _EngineFactory(Protocol):
	def __call__(self, resources: Path, *, use_cuda: bool, model_resources: Path) -> _Engine: ...


def _addon_module(name: str) -> types.ModuleType:
	package = types.ModuleType("_shravya_smoke")
	package.__path__ = [str(Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya")]
	sys.modules[package.__name__] = package
	return importlib.import_module(f"{package.__name__}.{name}")


def _synthesize(runtime: Path, model: Path) -> None:
	blue_module = _addon_module("_blue")
	frontend = model / "bluev3_onnx" / "frontend" / "polyg2p_data"
	old_data = os.environ.get("POLYG2P_DATA")
	os.environ["POLYG2P_DATA"] = str(frontend)
	try:
		factory = cast(_EngineFactory, blue_module.BlueEngine)
		engine = factory(runtime, use_cuda=False, model_resources=model)
		try:
			if not sum(len(chunk) for chunk in engine.stream("Shravya resource check.", speed=1.0, steps=2)):
				raise ValueError("CPU synthesis produced no audio")
		finally:
			engine.close()
	finally:
		if old_data is None:
			del os.environ["POLYG2P_DATA"]
		else:
			os.environ["POLYG2P_DATA"] = old_data


def smoke(runtime_archive: Path, model_archive: Path) -> None:
	install_module = _addon_module("_install")
	archives: tuple[tuple[Literal["runtime", "model"], Path], ...] = (
		("runtime", runtime_archive),
		("model", model_archive),
	)
	with tempfile.TemporaryDirectory(prefix="shravya-smoke-") as temp:
		root = Path(temp)
		paths: dict[str, Path] = {}

		def progress(done: int, total: int) -> None:
			pass

		for kind, archive in archives:
			version = pack_id(archive, kind)
			destination = root / kind / version
			install_module.install(archive, destination, progress, kind=kind)
			paths[kind] = destination
		_ = subprocess.run(
			[
				sys.executable,
				"-B",
				"-m",
				"tools.smoke_resource_pair",
				"--synthesize",
				str(paths["runtime"]),
				str(paths["model"]),
			],
			cwd=Path(__file__).resolve().parents[1],
			check=True,
		)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("--synthesize", action="store_true", help=argparse.SUPPRESS)
	_ = parser.add_argument("runtime", type=Path)
	_ = parser.add_argument("model", type=Path)
	args = parser.parse_args()
	if args.synthesize:
		_synthesize(args.runtime, args.model)
	else:
		smoke(args.runtime, args.model)
