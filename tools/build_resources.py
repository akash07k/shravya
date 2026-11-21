"""Build separate resource packs and generate a GitHub release catalog."""

import argparse
import re
import subprocess
import sys
import tomllib
import uuid
import zipfile
from pathlib import Path
from typing import NamedTuple, cast
from urllib.parse import quote, urlsplit

import tomlkit

from tools import build_resource_model, build_resource_runtime, resource_sources

INTERFACE = "bluev3-v1"


class LocalSources(NamedTuple):
	bluetts: Path | None = None
	bluetts_description: str | None = None
	polyg2p: Path | None = None
	polyg2p_description: str | None = None
	model: Path | None = None
	model_description: str | None = None


def valid_pack_id(value: object) -> bool:
	return (
		isinstance(value, str)
		and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None
		and not value.endswith(".")
	)


def existing_component(catalog: Path, kind: str) -> dict[str, object]:
	with catalog.open("rb") as source:
		data = tomllib.load(source)
	if type(data.get("format")) is not int or data.get("format") != 1 or data.get("interface") != INTERFACE:
		raise ValueError("Existing catalog has an unsupported format or interface")
	component: object = data.get(kind)
	if not isinstance(component, dict):
		raise ValueError(f"Existing catalog is missing the {kind} pack")
	fields = cast(dict[str, object], component)
	pack_id, url = fields.get("id"), fields.get("url")
	if not valid_pack_id(pack_id):
		raise ValueError(f"Existing {kind} pack ID is invalid")
	if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
		raise ValueError(f"Existing {kind} pack URL is invalid")
	return {"id": pack_id, "url": url}


def pack_id(archive: Path, kind: str) -> str:
	with zipfile.ZipFile(archive) as source:
		actual = source.read("pack-kind.txt").decode("utf-8")
		version = source.read("pack-version.txt").decode("utf-8")
	if actual != kind or not valid_pack_id(version):
		raise ValueError(f"Built {kind} archive has an invalid kind or ID")
	return version


def build(
	kind: str,
	output: Path,
	repository: str | None,
	tag: str | None,
	addons: list[str] | None,
	existing: Path | None,
	*,
	source_choice: str = "latest",
	sources: LocalSources | None = None,
	preserve_sources: bool = True,
) -> Path | None:
	if kind not in ("both", "runtime", "model"):
		raise ValueError("Choose both, runtime, or model")
	distribution = repository is not None or tag is not None or addons is not None or existing is not None
	if distribution:
		if (
			repository is None
			or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
			or any(part in (".", "..") for part in repository.split("/"))
		):
			raise ValueError("Repository must be a GitHub owner/name")
		if tag is None or not re.fullmatch(r"(?:v?\d+\.\d+\.\d+|resources)", tag):
			raise ValueError("Release tag must be resources or a three-part version")
		if not addons or not all(re.fullmatch(r"\d+\.\d+\.\d+", version) for version in addons):
			raise ValueError("Enter at least one compatible three-part add-on version")
		if kind != "both" and existing is None:
			raise ValueError("An existing catalog is required when building one pack")
	if source_choice not in ("latest", "local"):
		raise ValueError("Choose latest or local resource sources")
	if source_choice == "latest" and sources is not None:
		raise ValueError("Local source options require local source selection")
	if source_choice == "local":
		if sources is None:
			raise ValueError("Local source directories are required")
		if kind in ("both", "model"):
			if sources.model is None:
				raise ValueError("A local model bundle is required")
			_ = resource_sources.local_description(sources.model, sources.model_description)
		needs_blue = kind in ("both", "runtime") or (
			sources.model is not None
			and not (sources.model / "onnx" / "vector_estimator.int8.onnx").is_file()
		)
		if needs_blue:
			if sources.bluetts is None:
				raise ValueError("A local BlueTTS source is required")
			_ = resource_sources.local_description(sources.bluetts, sources.bluetts_description)
		if kind in ("both", "runtime"):
			if sources.polyg2p is None:
				raise ValueError("A local polyg2p source is required")
			_ = resource_sources.local_description(sources.polyg2p, sources.polyg2p_description)

	kinds = ("runtime", "model") if kind == "both" else (kind,)
	components: dict[str, dict[str, object]] = {}
	if distribution and kind != "both":
		assert existing is not None
		other = "model" if kind == "runtime" else "runtime"
		components[other] = existing_component(existing, other)

	output.mkdir(parents=True, exist_ok=True)
	build_id = uuid.uuid4().hex
	label = f"{tag}-" if distribution else ""
	archives = {name: output / f"{name}-{label}{build_id}.zip" for name in kinds}
	catalog = output / f"catalog-{tag}-{build_id}.toml" if distribution else None
	outputs = list(archives.values()) + ([catalog] if catalog is not None else [])
	for path in outputs:
		if path.exists():
			raise FileExistsError(f"Output already exists: {path}")
	source_output = output / f"sources-{build_id}" if source_choice == "latest" and preserve_sources else None
	if source_output is not None:
		source_output.mkdir()
	blue_source = resource_sources.resolve_blue_source() if source_choice == "latest" else None
	for name, archive in archives.items():
		print(f"Building {name} pack: {archive}", flush=True)
		if name == "runtime":
			build_resource_runtime.build(
				archive,
				bluetts_dir=sources.bluetts if sources else None,
				bluetts_description=sources.bluetts_description if sources else None,
				polyg2p_dir=sources.polyg2p if sources else None,
				polyg2p_description=sources.polyg2p_description if sources else None,
				blue_source=blue_source,
				source_output=source_output,
			)
		else:
			build_resource_model.build(
				archive,
				sources.model if sources else None,
				sources.model_description if sources else None,
				bluetts_dir=sources.bluetts if sources else None,
				bluetts_description=sources.bluetts_description if sources else None,
				blue_source=blue_source,
				source_output=source_output,
			)
		version = pack_id(archive, name)
		if distribution:
			assert repository is not None and tag is not None
			url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(archive.name, safe='')}"
			components[name] = {"id": version, "url": url}
	if source_output is not None:
		print(f"Downloaded source folders saved in: {source_output}", flush=True)
	if catalog is None:
		return None

	data = {
		"format": 1,
		"interface": INTERFACE,
		"addons": addons,
		"runtime": components["runtime"],
		"model": components["model"],
	}
	content = tomlkit.dumps(data)
	created = False
	try:
		with catalog.open("x", encoding="utf-8", newline="\n") as destination:
			created = True
			_ = destination.write(content)
	except OSError:
		if created:
			catalog.unlink(missing_ok=True)
		raise
	return catalog


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("--kind", choices=("both", "runtime", "model"))
	_ = parser.add_argument("--output", type=Path)
	_ = parser.add_argument("--source", choices=("latest", "local"), help="source selection")
	_ = parser.add_argument("--delivery", choices=("local", "github"), help="output or publication catalog")
	_ = parser.add_argument("--repository", help="GitHub owner/name")
	_ = parser.add_argument("--tag", help="GitHub release tag, such as resources")
	_ = parser.add_argument("--addons", help="comma-separated compatible add-on versions")
	_ = parser.add_argument("--existing-catalog", type=Path, help="existing catalog when building one pack")
	_ = parser.add_argument("--bluetts-dir", type=Path, help="local BlueTTS source directory")
	_ = parser.add_argument("--bluetts-description", help="identity of the local BlueTTS source")
	_ = parser.add_argument("--polyg2p-dir", type=Path, help="local polyg2p source directory")
	_ = parser.add_argument("--polyg2p-description", help="identity of the local polyg2p source")
	_ = parser.add_argument("--model-dir", type=Path, help="local model bundle")
	_ = parser.add_argument("--model-description", help="identity of the local model bundle")
	args = parser.parse_args()
	try:
		if len(sys.argv) == 1:

			def choose(title: str, choices: tuple[str, ...]) -> str:
				print(title)
				for number, choice in enumerate(choices, 1):
					print(f"  {number}. {choice}")
				answer = input("Choose a number: ").strip()
				if not answer.isdecimal() or not 1 <= int(answer) <= len(choices):
					raise ValueError("Choose one of the displayed numbers")
				return choices[int(answer) - 1]

			def source_path(prompt: str) -> Path:
				answer = input(prompt).strip()
				if not answer:
					raise ValueError("A local source directory is required")
				return Path(answer)

			args.kind = choose("Which packs?", ("both", "runtime", "model"))
			args.source = choose("Which sources?", ("latest", "local"))
			args.output = Path(input("Output directory [tmp]: ").strip() or "tmp")
			args.delivery = choose("Build for local use or GitHub publication?", ("local", "github"))
			if args.source == "local":
				if args.kind in ("both", "model"):
					args.model_dir = source_path("Local model bundle directory: ")
					args.model_description = input("Model source description: ").strip()
				needs_blue = args.kind in ("both", "runtime") or (
					args.model_dir is not None
					and not (args.model_dir / "onnx" / "vector_estimator.int8.onnx").is_file()
				)
				if needs_blue:
					args.bluetts_dir = source_path("Local BlueTTS source directory: ")
					args.bluetts_description = input("BlueTTS source description: ").strip()
				if args.kind in ("both", "runtime"):
					args.polyg2p_dir = source_path("Local polyg2p source directory: ")
					args.polyg2p_description = input("polyg2p source description: ").strip()
			if args.delivery == "github":
				args.repository = input("GitHub repository (owner/name): ").strip()
				args.tag = input("Release tag (for example resources): ").strip()
				args.addons = input("Compatible add-on versions, separated by commas: ").strip()
				if args.kind != "both":
					catalog_path = input("Existing catalog path: ").strip()
					if not catalog_path:
						raise ValueError("An existing catalog path is required")
					args.existing_catalog = Path(catalog_path)
		elif not all((args.kind, args.output, args.source, args.delivery)):
			parser.error("--kind, --output, --source, and --delivery are required")
		if args.delivery == "github" and not all((args.repository, args.tag, args.addons)):
			parser.error("GitHub delivery requires --repository, --tag, and --addons")
		if args.delivery == "local" and any((args.repository, args.tag, args.addons, args.existing_catalog)):
			parser.error("Publication options require --delivery github")
		local_values = (
			args.bluetts_dir,
			args.bluetts_description,
			args.polyg2p_dir,
			args.polyg2p_description,
			args.model_dir,
			args.model_description,
		)
		if args.source == "latest" and any(local_values):
			parser.error("Local source options require --source local")
		sources = LocalSources(*local_values) if args.source == "local" else None
		versions = [version.strip() for version in args.addons.split(",")] if args.addons else None
		catalog = build(
			args.kind,
			args.output,
			args.repository,
			args.tag,
			versions,
			args.existing_catalog,
			source_choice=args.source,
			sources=sources,
		)
		if catalog is None:
			print(f"Resource ZIPs created in: {args.output}")
		else:
			print(f"Catalog created: {catalog}")
			print("Upload the new ZIPs to the chosen GitHub release and publish the catalog separately.")
		return 0
	except (EOFError, KeyboardInterrupt):
		print("Resource build cancelled.", file=sys.stderr)
		return 1
	except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
		print(f"Resource build failed: {error}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
