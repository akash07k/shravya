"""Prepare BlueV3 model files without modifying the source bundle."""

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

MODEL_FILES: tuple[str, ...] = (
	"onnx/duration_predictor_style.onnx",
	"onnx/text_encoder.onnx",
	"onnx/vector_estimator.onnx",
	"onnx/vocoder.onnx",
	"onnx/tts.json",
	"onnx/vocab.json",
	"onnx/stats.npz",
	"onnx/uncond.npz",
	"frontend/polyg2p_data/en.lex",
	"frontend/polyg2p_data/en-large.g2p",
	"voices/noa.json",
)


def _require_files(root: Path, names: tuple[str, ...]) -> None:
	for name in names:
		path = root / name
		if not path.is_file() or path.stat().st_size == 0:
			raise FileNotFoundError(f"Model bundle is missing a nonempty file: {name}")


def prepare_model(bundle: Path, destination: Path, quantize: Callable[[Path, Path], None]) -> None:
	bundle = bundle.resolve()
	destination = destination.resolve()
	if destination.exists():
		raise FileExistsError(f"Model destination already exists: {destination}")
	if destination.is_relative_to(bundle):
		raise ValueError("Model destination must be outside the source bundle")
	_require_files(bundle, MODEL_FILES)
	destination.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(prefix=".prepare-model-", dir=destination.parent) as temp:
		stage = Path(temp) / destination.name
		stage.mkdir()
		_ = shutil.copytree(bundle / "onnx", stage / "onnx")
		_ = shutil.copytree(bundle / "voices", stage / "voices")
		frontend = stage / "frontend" / "polyg2p_data"
		frontend.mkdir(parents=True)
		input_frontend = bundle / "frontend" / "polyg2p_data"
		for pattern in ("en.lex", "en.g2p", "en-large.g2p", "LICENSE*", "NOTICE*"):
			for path in input_frontend.glob(pattern):
				if path.is_file():
					_ = shutil.copy2(path, frontend / path.name)
		for pattern in ("LICENSE*", "NOTICE*"):
			for path in bundle.glob(pattern):
				if path.is_file():
					_ = shutil.copy2(path, stage / path.name)
		int8 = stage / "onnx" / "vector_estimator.int8.onnx"
		if not int8.exists():
			quantize(stage / "onnx" / "vector_estimator.onnx", int8)
		_require_files(stage, MODEL_FILES + ("onnx/vector_estimator.int8.onnx",))
		_ = stage.rename(destination)
