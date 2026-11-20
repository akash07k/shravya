"""Download an upstream English BlueV3 model bundle at a resolved revision."""

import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import cast

from tools import resource_sources
from tools.prepare_resource_model import MODEL_FILES

REPOSITORY = resource_sources.MODEL_REPOSITORY
REQUIRED_FILES = MODEL_FILES + (
	"frontend/polyg2p_data/LICENSE",
	"frontend/polyg2p_data/NOTICE",
	"LICENSES.md",
	"LICENSE.OpenRAIL-M",
)
OPTIONAL_FILES = {
	"onnx/vector_estimator.int8.onnx",
	"frontend/polyg2p_data/en.g2p",
}


def _selected(path: str) -> bool:
	parts = PurePosixPath(path).parts
	return (
		path in REQUIRED_FILES
		or path in OPTIONAL_FILES
		or (
			len(parts) == 2
			and parts[0] == "voices"
			and re.fullmatch(r"[A-Za-z0-9_-]+\.json", parts[1]) is not None
		)
	)


def download_model(destination: Path) -> str:
	if destination.exists():
		raise FileExistsError(f"Model destination already exists: {destination}")
	revision = resource_sources.model_revision()
	listing_url = (
		f"https://huggingface.co/api/models/{REPOSITORY}/tree/{revision}?recursive=true&expand=false"
	)
	files_url = f"https://huggingface.co/{REPOSITORY}/resolve/{revision}"
	with urllib.request.urlopen(listing_url, timeout=60) as response:
		listing: object = json.load(response)
	if not isinstance(listing, list):
		raise ValueError("Invalid upstream model file listing")
	files: dict[str, int] = {}
	for item in cast(list[object], listing):
		if not isinstance(item, dict):
			raise ValueError("Invalid upstream model file entry")
		entry = cast(dict[str, object], item)
		name = entry.get("path")
		if entry.get("type") == "file" and isinstance(name, str) and _selected(name):
			size = entry.get("size")
			if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or name in files:
				raise ValueError(f"Invalid upstream model file size or duplicate: {name}")
			files[name] = size
	missing = set(REQUIRED_FILES) - files.keys()
	if missing:
		raise ValueError(f"Upstream model bundle is incomplete: {', '.join(sorted(missing))}")
	destination.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(prefix=".download-model-", dir=destination.parent) as temp:
		stage = Path(temp) / destination.name
		stage.mkdir()
		for name, size in sorted(files.items()):
			path = stage.joinpath(*PurePosixPath(name).parts)
			path.parent.mkdir(parents=True, exist_ok=True)
			url = f"{files_url}/{urllib.parse.quote(name, safe='/')}"
			print(f"Downloading {name}", flush=True)
			with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as output:
				shutil.copyfileobj(response, output)
			if path.stat().st_size != size:
				raise OSError(f"Unexpected model file size: {name}")
		_ = stage.rename(destination)
	return revision
