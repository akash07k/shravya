"""Assemble the BlueTTS, CPU, and CUDA runtime files."""

import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

RUNTIME_FILES: tuple[str, ...] = (
	"blue_onnx/__init__.py",
	"blue_onnx/cpu.py",
	"blue_onnx/LICENSE",
	"numpy/__init__.py",
	"num2words/__init__.py",
	"polyg2p/__init__.py",
	"polyg2p/_polyg2p.pyd",
	"onnxruntime/__init__.py",
	"onnxruntime/capi/onnxruntime_pybind11_state.pyd",
	"onnxruntime/capi/onnxruntime_providers_cuda.dll",
	"nvidia/cu13/bin/x86_64/cublas64_13.dll",
	"nvidia/cu13/bin/x86_64/cublasLt64_13.dll",
	"nvidia/cudnn/bin/cudnn64_9.dll",
)


def prepare_runtime(
	bluetts: Path,
	destination: Path,
	install_dependencies: Callable[[Path], None],
	inputs: dict[str, object],
) -> None:
	bluetts = bluetts.resolve()
	destination = destination.resolve()
	if destination.exists():
		raise FileExistsError(f"Runtime destination already exists: {destination}")
	if destination.is_relative_to(bluetts):
		raise ValueError("Runtime destination must be outside the BlueTTS source")
	destination.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(prefix=".prepare-runtime-", dir=destination.parent) as temp:
		stage = Path(temp) / destination.name
		stage.mkdir()
		install_dependencies(stage)
		_ = shutil.copytree(bluetts / "src" / "blue_onnx", stage / "blue_onnx")
		_ = shutil.copy2(bluetts / "LICENSE", stage / "blue_onnx" / "LICENSE")
		for name in RUNTIME_FILES:
			path = stage / name
			if not path.is_file() or path.stat().st_size == 0:
				raise FileNotFoundError(f"Runtime is missing a nonempty file: {name}")
		_ = (stage / "build-inputs.json").write_text(
			json.dumps(inputs, indent=2, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		_ = stage.rename(destination)
