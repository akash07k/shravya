"""Find newer Shravya add-on releases."""

from __future__ import annotations

import json
import re
import tomllib
from typing import NamedTuple, cast
from urllib.request import Request, urlopen

from ._delivery_config import http_url


class Update(NamedTuple):
	version: str
	url: str
	notes: str | None = None
	catalog_url: str | None = None


def version_parts(version: str) -> tuple[int, int, int]:
	match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
	if match is None:
		raise ValueError(f"Invalid add-on version: {version!r}")
	return int(match[1]), int(match[2]), int(match[3])


def _asset_url(assets: list[object], name: str) -> str:
	matches: list[str] = []
	for asset in assets:
		if not isinstance(asset, dict):
			continue
		fields = cast(dict[str, object], asset)
		if fields.get("name") == name:
			url = fields.get("browser_download_url")
			if not isinstance(url, str) or not url:
				raise ValueError(f"Download URL is missing for {name}")
			matches.append(url)
	if len(matches) != 1:
		raise ValueError(f"Release must contain exactly one {name}")
	return matches[0]


def check_release(url: str, current_version: str) -> Update | None:
	request = Request(
		url,
		headers={"User-Agent": "Shravya NVDA add-on", "Accept": "application/vnd.github+json"},
	)
	with urlopen(request, timeout=30) as response:
		data = cast(object, json.load(response))
	if not isinstance(data, dict):
		raise ValueError("Invalid release metadata")
	release = cast(dict[str, object], data)
	tag = release.get("tag_name")
	if (
		not isinstance(tag, str)
		or release.get("draft") is not False
		or release.get("prerelease") is not False
	):
		raise ValueError("Invalid stable release metadata")
	version = tag.removeprefix("v")
	if version_parts(version) <= version_parts(current_version):
		return None
	assets = release.get("assets")
	if not isinstance(assets, list):
		raise ValueError("Release assets are missing")
	asset_list = cast(list[object], assets)
	download_url = _asset_url(asset_list, f"shravya-{version}.nvda-addon")
	manifest_url = _asset_url(asset_list, "shravya-update.toml")
	request = Request(manifest_url, headers={"User-Agent": "Shravya NVDA add-on"})
	with urlopen(request, timeout=30) as response:
		manifest = cast(dict[str, object], tomllib.loads(response.read().decode("utf-8")))
	if manifest.get("version") != version:
		raise ValueError("Update metadata does not match the release version")
	catalog_url = http_url(manifest.get("resource_catalog"))
	notes = manifest.get("changes")
	if notes is not None and not isinstance(notes, str):
		raise ValueError("Update release notes are not text")
	if isinstance(notes, str):
		notes = notes.strip() or None
	return Update(version, download_url, notes, catalog_url)
