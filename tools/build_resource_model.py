"""Build a BlueV3 model ZIP from the upstream or a custom model bundle."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

from tools import build_resource_pack, download_resource_model, prepare_resource_model, resource_sources

BUILD_REQUIREMENTS = Path(__file__).with_name("resource-build-requirements.txt")


def build_environment(work: Path, name: str) -> Path:
	environment = work / name
	python = environment / "Scripts" / "python.exe"
	_ = subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
	_ = subprocess.run(
		["uv", "pip", "install", "--python", str(python), "-r", str(BUILD_REQUIREMENTS)],
		check=True,
	)
	return python


def quantize_model(
	work: Path,
	source: Path,
	destination: Path,
	bluetts: Path | resource_sources.GitSource,
) -> None:
	python = build_environment(work, "quantizer")
	if isinstance(bluetts, Path):
		cpu_module = bluetts / "src" / "blue_onnx" / "cpu.py"
		if not cpu_module.is_file():
			raise FileNotFoundError(f"Local BlueTTS quantizer is missing: {cpu_module}")
	else:
		url = (
			f"https://raw.githubusercontent.com/{bluetts.repository}/{bluetts.revision}/src/blue_onnx/cpu.py"
		)
		cpu_module = work / "cpu.py"
		with urllib.request.urlopen(url, timeout=60) as response, cpu_module.open("xb") as output:
			shutil.copyfileobj(response, output)
	script = "import runpy, sys; runpy.run_path(sys.argv[1])['quantize_vector_estimator'](*sys.argv[2:])"
	_ = subprocess.run(
		[str(python), "-B", "-c", script, str(cpu_module), str(source), str(destination)],
		check=True,
	)


def build(
	output: Path,
	bundle: Path | None,
	source: str | None,
	*,
	bluetts_dir: Path | None = None,
	bluetts_description: str | None = None,
	blue_source: resource_sources.GitSource | None = None,
	source_output: Path | None = None,
) -> None:
	output = output.resolve()
	if output.exists():
		raise FileExistsError(f"Archive already exists: {output}")
	if not output.parent.is_dir():
		raise FileNotFoundError(f"Archive directory does not exist: {output.parent}")
	if bundle is not None and not (source and source.strip()):
		raise ValueError("A source description is required for a custom model bundle")
	with tempfile.TemporaryDirectory(prefix="shravya-model-build-") as temp:
		work = Path(temp)
		downloaded_model = bundle is None
		if bundle is None:
			bundle = work / "upstream"
			revision = download_resource_model.download_model(bundle)
			source = f"https://huggingface.co/{download_resource_model.REPOSITORY} @ {revision}"
		stage = work / "resources"
		int8_generated = not (bundle / "onnx" / "vector_estimator.int8.onnx").exists()
		int8_info: dict[str, str] = {"source": "provided"}
		bluetts: Path | resource_sources.GitSource | None = None
		if int8_generated:
			int8_info = {
				"source": "generated",
				"build_requirements": BUILD_REQUIREMENTS.read_text(encoding="utf-8"),
				"python": sys.version,
			}
			if bluetts_dir is None:
				if bluetts_description is not None:
					raise ValueError("A local BlueTTS description requires a source directory")
				bluetts = blue_source if blue_source is not None else resource_sources.resolve_blue_source()
				int8_info["bluetts_repository"] = bluetts.repository
				int8_info["bluetts_revision"] = bluetts.revision
				if source_output is not None:
					saved_blue = source_output / "bluetts"
					if not saved_blue.exists():
						downloaded_blue = resource_sources.download_source(
							work,
							bluetts.repository,
							bluetts.revision,
						)
						resource_sources.preserve_source(downloaded_blue, saved_blue, move=True)
					bluetts = saved_blue
			else:
				if blue_source is not None:
					raise ValueError("Choose either local or upstream BlueTTS sources")
				int8_info["bluetts_source"] = resource_sources.local_description(
					bluetts_dir,
					bluetts_description,
				)
				bluetts = bluetts_dir

		def quantize(model: Path, target: Path) -> None:
			if bluetts is None:
				raise RuntimeError("BlueTTS source is required to quantize the model")
			quantize_model(work, model, target, bluetts)

		prepare_resource_model.prepare_model(bundle, stage / "bluev3_onnx", quantize)
		info = {"model_source": source, "int8": int8_info}
		_ = (stage / "bluev3_onnx" / "build-inputs.json").write_text(
			json.dumps(info, indent=2, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		build_resource_pack.build_component(stage, output, "model", f"model-{uuid.uuid4().hex}")
		if downloaded_model and source_output is not None:
			resource_sources.preserve_source(bundle, source_output / "model", move=True)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("output", type=Path, help="new model ZIP")
	_ = parser.add_argument("--source-choice", choices=("latest", "local"), required=True)
	_ = parser.add_argument("--bundle", type=Path, help="compatible local model bundle")
	_ = parser.add_argument("--source", help="source and revision of the custom bundle")
	_ = parser.add_argument("--bluetts-dir", type=Path, help="local BlueTTS source directory")
	_ = parser.add_argument("--bluetts-description", help="identity of the local BlueTTS source")
	args = parser.parse_args()
	if args.source_choice == "latest" and any(
		(args.bundle, args.source, args.bluetts_dir, args.bluetts_description),
	):
		parser.error("Local source options require --source-choice local")
	if args.source_choice == "local":
		if args.bundle is None or not args.source:
			parser.error("Local builds require a model bundle and its source description")
		if not (args.bundle / "onnx" / "vector_estimator.int8.onnx").is_file() and not (
			args.bluetts_dir and args.bluetts_description
		):
			parser.error("Local quantization requires a BlueTTS source directory and description")
	build(
		args.output,
		args.bundle,
		args.source,
		bluetts_dir=args.bluetts_dir,
		bluetts_description=args.bluetts_description,
	)
	print(f"Built {args.output} ({args.output.stat().st_size} bytes)")
