"""Resolve the source revisions used to build resource packs."""

import json
import re
import shutil
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple, cast

CONFIG = Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya" / "config.toml"
POLYG2P_REPOSITORY = "maxmelichov/polyg2p"
MODEL_REPOSITORY = "notmax123/BlueV3-onnx"


class BlueSource(NamedTuple):
	repository: str
	branch: str | None


class GitSource(NamedTuple):
	repository: str
	revision: str


def download_source(work: Path, repository: str, revision: str) -> Path:
	name = repository.split("/")[-1]
	root_name = f"{name}-{revision}"
	archive_path = work / f"{name}.zip"
	url = f"https://codeload.github.com/{repository}/zip/{revision}"
	print(f"Downloading {repository} at {revision}", flush=True)
	with urllib.request.urlopen(url, timeout=60) as response, archive_path.open("xb") as output:
		shutil.copyfileobj(response, output)
	with zipfile.ZipFile(archive_path) as archive:
		for info in archive.infolist():
			parts = PurePosixPath(info.filename).parts
			if (
				not parts
				or parts[0] != root_name
				or ".." in parts
				or "\\" in info.filename
				or ":" in info.filename
			):
				raise ValueError(f"Unexpected source archive path: {info.filename}")
		archive.extractall(work)
	return work / root_name


def preserve_source(source: Path, destination: Path, *, move: bool = False) -> None:
	if destination.exists():
		raise FileExistsError(f"Source destination already exists: {destination}")
	with tempfile.TemporaryDirectory(prefix=".source-", dir=destination.parent) as temp:
		stage = Path(temp) / destination.name
		if move:
			_ = shutil.move(str(source), str(stage))
		else:
			_ = shutil.copytree(source, stage)
		_ = stage.rename(destination)


def load_blue_source(config: Path = CONFIG) -> BlueSource:
	with config.open("rb") as source:
		data = tomllib.load(source)
	settings: object = data.get("sources")
	if not isinstance(settings, dict):
		raise ValueError("The resource source configuration section is missing")
	fields = cast(dict[str, object], settings)
	unknown = fields.keys() - {"bluetts_repository", "bluetts_branch"}
	if unknown:
		raise ValueError(f"Unknown resource source setting: {', '.join(sorted(unknown))}")
	repository = fields.get("bluetts_repository")
	if (
		not isinstance(repository, str)
		or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
		or any(part in (".", "..") for part in repository.split("/"))
	):
		raise ValueError("BlueTTS repository must be a GitHub owner/name")
	branch = fields.get("bluetts_branch", "")
	if not isinstance(branch, str) or any(ord(char) < 32 or ord(char) == 127 for char in branch):
		raise ValueError("BlueTTS branch must be a branch name or empty")
	return BlueSource(repository, branch or None)


def _revision(value: object, source: str) -> str:
	if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
		raise ValueError(f"Invalid revision returned by {source}")
	return value


def github_revision(repository: str, branch: str | None = None) -> str:
	params = {"per_page": "1"}
	if branch is not None:
		params["sha"] = branch
	url = f"https://api.github.com/repos/{repository}/commits?{urllib.parse.urlencode(params)}"
	request = urllib.request.Request(url, headers={"User-Agent": "Shravya resource builder"})
	with urllib.request.urlopen(request, timeout=60) as response:
		result: object = json.load(response)
	if not isinstance(result, list) or not result or not isinstance(result[0], dict):
		raise ValueError(f"Invalid commit response for {repository}")
	commit = cast(dict[str, object], result[0])
	return _revision(commit.get("sha"), repository)


def model_revision() -> str:
	request = urllib.request.Request(
		f"https://huggingface.co/api/models/{MODEL_REPOSITORY}",
		headers={"User-Agent": "Shravya resource builder"},
	)
	with urllib.request.urlopen(request, timeout=60) as response:
		result: object = json.load(response)
	if not isinstance(result, dict):
		raise ValueError("Invalid upstream model metadata")
	return _revision(cast(dict[str, object], result).get("sha"), MODEL_REPOSITORY)


def resolve_blue_source() -> GitSource:
	source = load_blue_source()
	return GitSource(source.repository, github_revision(source.repository, source.branch))


def local_description(path: Path, description: str | None) -> str:
	if not path.is_dir():
		raise FileNotFoundError(f"Local source directory does not exist: {path}")
	if description is None or not description.strip():
		raise ValueError(f"A source description is required for {path}")
	return description.strip()
