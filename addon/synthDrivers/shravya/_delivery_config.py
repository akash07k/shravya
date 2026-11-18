"""Read the packaged resource and update delivery configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import NamedTuple, cast
from urllib.parse import urlsplit


class DeliveryConfig(NamedTuple):
	catalog_url: str | None
	release_api: str | None


def http_url(value: object) -> str:
	if not isinstance(value, str) or not value:
		raise ValueError("Resource URL is missing")
	parsed = urlsplit(value)
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		raise ValueError("Resource URL must be an absolute HTTP or HTTPS URL")
	return value


def load_delivery_config(path: Path | None = None) -> DeliveryConfig:
	config_path = path if path is not None else Path(__file__).with_name("config.toml")
	with config_path.open("rb") as source:
		data = tomllib.load(source)
	delivery: object = data.get("delivery")
	if not isinstance(delivery, dict):
		raise ValueError("The delivery configuration section is missing")
	fields = cast(dict[str, object], delivery)
	unknown = fields.keys() - {"catalog_url", "release_api"}
	if unknown:
		raise ValueError(f"Unknown delivery configuration setting: {', '.join(sorted(unknown))}")

	def url(name: str) -> str | None:
		value = fields.get(name)
		if value is None or value == "":
			return None
		try:
			return http_url(value)
		except ValueError as error:
			raise ValueError(f"Invalid delivery setting {name}: {error}") from error

	return DeliveryConfig(url("catalog_url"), url("release_api"))


if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser(description="Print the configured release catalog URL.")
	_ = parser.add_argument("config", type=Path, help="delivery configuration TOML")
	args = parser.parse_args()
	try:
		delivery = load_delivery_config(args.config)
		if delivery.catalog_url is None:
			raise ValueError("Set delivery.catalog_url before building a release")
		print(delivery.catalog_url)
	except (OSError, ValueError) as error:
		parser.exit(1, f"Invalid Shravya delivery configuration: {error}\n")
