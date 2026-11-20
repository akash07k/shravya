"""Generate release update metadata from the changelog."""

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit


def build(version: str, changelog: Path, output: Path, catalog_url: str | None = None) -> None:
	if not re.fullmatch(r"\d+\.\d+\.\d+", version):
		raise ValueError("The release version must have three numeric parts")
	if catalog_url is not None:
		parsed = urlsplit(catalog_url)
		if parsed.scheme not in ("http", "https") or not parsed.netloc:
			raise ValueError("Resource catalog must be an absolute HTTP or HTTPS URL")
	notes = changelog.read_text(encoding="utf-8").strip()
	data = {"version": version, "changes": notes}
	if catalog_url is not None:
		data["resource_catalog"] = catalog_url
	manifest = tomlkit.dumps(data)
	with output.open("x", encoding="utf-8", newline="\n") as destination:
		_ = destination.write(manifest)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("version", help="three-part release version")
	_ = parser.add_argument("changelog", type=Path, help="release changelog")
	_ = parser.add_argument("output", type=Path, help="new TOML manifest")
	_ = parser.add_argument("--resource-catalog", help="absolute URL for this release's resource catalog")
	args = parser.parse_args()
	build(args.version, args.changelog, args.output, args.resource_catalog)
