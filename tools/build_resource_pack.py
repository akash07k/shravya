"""Build a split BlueV3 resource archive from the staged layout."""

import argparse
import json
import re
import zipfile
from pathlib import Path


def _archive(source: Path, output: Path, directories: list[Path], version: str, kind: str) -> None:
	source = source.resolve()
	output = output.resolve()
	if output.is_relative_to(source):
		raise ValueError("The archive must be outside the resource directory")
	if not all(directory.is_dir() for directory in directories):
		raise FileNotFoundError("A required resource directory is missing")
	files = sorted(path for directory in directories for path in directory.rglob("*") if path.is_file())
	if not files:
		raise ValueError("The resource directories contain no files")
	inventory = {path.relative_to(source).as_posix(): path.stat().st_size for path in files}
	stream = output.open("xb")
	complete = False
	try:
		with (
			stream,
			zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive,
		):
			archive.writestr("pack-version.txt", version)
			archive.writestr("pack-files.json", json.dumps(inventory, ensure_ascii=False))
			archive.writestr("pack-kind.txt", kind)
			for path in files:
				archive.write(path, path.relative_to(source).as_posix())
		complete = True
	finally:
		if not complete:
			output.unlink(missing_ok=True)


def build_component(source: Path, output: Path, kind: str, version: str) -> None:
	directories = {"runtime": "python", "model": "bluev3_onnx"}
	if kind not in directories:
		raise ValueError(f"Unknown resource kind: {kind}")
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
		raise ValueError(f"Invalid resource version: {version!r}")
	source = source.resolve()
	_archive(source, output, [source / directories[kind]], version, kind)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("source", type=Path, help="resource directory")
	_ = parser.add_argument("output", type=Path, help="new ZIP archive")
	_ = parser.add_argument("--kind", choices=("runtime", "model"), required=True, help="resource kind")
	_ = parser.add_argument("--version", required=True, help="resource version")
	args = parser.parse_args()
	build_component(args.source, args.output, args.kind, args.version)
	print(f"Built {args.output} ({args.output.stat().st_size} bytes)")
