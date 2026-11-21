"""Verify that a release's configured catalog and resource ZIPs are publicly available."""

import argparse
import importlib
import sys
import types
from pathlib import Path
from urllib.request import Request, urlopen


def check_asset(url: str, kind: str) -> None:
	request = Request(
		url,
		headers={"User-Agent": "Shravya release check", "Range": "bytes=0-3"},
	)
	with urlopen(request, timeout=30) as response:
		if response.read(4) != b"PK\x03\x04":
			raise ValueError(f"The {kind} resource URL does not serve a ZIP archive")


def check(url: str, version: str) -> None:
	# Load the add-on's catalog code without importing the NVDA-dependent synth driver.
	package = types.ModuleType("_shravya_release_check")
	package.__path__ = [str(Path(__file__).resolve().parents[1] / "addon" / "synthDrivers" / "shravya")]
	sys.modules[package.__name__] = package
	catalog_module = importlib.import_module(f"{package.__name__}._catalog")
	catalog = catalog_module.fetch_catalog(url, version)
	for kind, component in (("runtime", catalog.runtime), ("model", catalog.model)):
		check_asset(component.url, kind)


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("catalog")
	_ = parser.add_argument("version")
	args = parser.parse_args()
	try:
		check(args.catalog, args.version)
	except (OSError, ValueError) as error:
		parser.exit(1, f"Resource catalog is not ready for release: {error}\n")


if __name__ == "__main__":
	main()
