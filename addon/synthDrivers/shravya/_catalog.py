"""Read a configured resource catalog and verify its compatibility."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, NamedTuple, cast
from urllib.request import Request, urlopen

from ._delivery_config import http_url
from ._resources import RESOURCE_INTERFACE, component_directory

_MAX_CATALOG_BYTES = 1024 * 1024


class Component(NamedTuple):
	id: str
	url: str


class Catalog(NamedTuple):
	interface: str
	runtime: Component
	model: Component


def _component(value: object, kind: Literal["runtime", "model"]) -> Component:
	if not isinstance(value, dict):
		raise ValueError(f"{kind} resource metadata is missing")
	fields = cast(dict[str, object], value)
	version = fields.get("id")
	if not isinstance(version, str):
		raise ValueError(f"{kind} resource ID is missing")
	_ = component_directory(Path("."), kind, version)
	return Component(version, http_url(fields.get("url")))


def fetch_catalog(url: str, addon_version: str) -> Catalog:
	request = Request(http_url(url), headers={"User-Agent": "Shravya NVDA add-on"})
	with urlopen(request, timeout=30) as response:
		content = response.read(_MAX_CATALOG_BYTES + 1)
	if len(content) > _MAX_CATALOG_BYTES:
		raise ValueError("Resource catalog is too large")
	try:
		data = tomllib.loads(content.decode("utf-8"))
	except (UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ValueError("Resource catalog is invalid") from error
	format_version = data.get("format")
	addons: object = data.get("addons")
	interface = data.get("interface")
	if type(format_version) is not int or format_version != 1:
		raise ValueError("Unsupported resource catalog format")
	if not isinstance(addons, list):
		raise ValueError("Compatible add-on versions are missing")
	versions = cast(list[object], addons)
	if not versions or not all(isinstance(item, str) and item for item in versions):
		raise ValueError("Compatible add-on versions are missing")
	if addon_version not in versions or interface != RESOURCE_INTERFACE:
		raise ValueError("Resource catalog is incompatible with this add-on")
	runtime = _component(data.get("runtime"), "runtime")
	model = _component(data.get("model"), "model")
	return Catalog(RESOURCE_INTERFACE, runtime, model)
