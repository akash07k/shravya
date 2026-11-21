"""Build a Windows CPU and CUDA BlueTTS runtime ZIP."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from tools import build_resource_pack, prepare_resource_runtime, resource_sources
from tools.build_resource_model import (
	BUILD_REQUIREMENTS,
	build_environment,
)

RUNTIME_REQUIREMENTS = Path(__file__).with_name("resource-runtime-requirements.txt")


def tool_version(command: str) -> str:
	return subprocess.check_output([command, "--version"], text=True).strip()


def build_polyg2p(work: Path, source: Path, python: Path) -> tuple[Path, str]:
	binding = source / "bindings" / "python"
	wheels = work / "wheels"
	wheels.mkdir()
	command = [
		str(python),
		"-m",
		"maturin",
		"build",
		"--locked",
		"--release",
		"--interpreter",
		str(python),
		"--out",
		str(wheels),
	]
	if shutil.which("cl"):
		msvc = "preconfigured MSVC environment"
		_ = subprocess.run(command, cwd=binding, check=True)
	else:
		vswhere = (
			Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
		)
		query = [
			str(vswhere),
			"-latest",
			"-products",
			"*",
			"-requires",
			"Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
		]
		installation = subprocess.check_output(query + ["-property", "installationPath"], text=True).strip()
		if not installation:
			raise RuntimeError("Visual Studio C++ Build Tools were not found")
		msvc = subprocess.check_output(query + ["-property", "installationVersion"], text=True).strip()
		vcvars = Path(installation) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
		if not vcvars.is_file():
			raise FileNotFoundError(f"MSVC environment setup not found: {vcvars}")
		quoted = " ".join(f'"{argument}"' for argument in command)
		script = f'call "{vcvars}" >nul && cd /d "{binding}" && {quoted}'
		_ = subprocess.run(f'"{os.environ["ComSpec"]}" /d /s /c "{script}"', check=True)
	matches = list(wheels.glob("polyg2p-*.whl"))
	if len(matches) != 1:
		raise RuntimeError(f"Expected one polyg2p wheel, found {len(matches)}")
	return matches[0], msvc


def install_runtime(python: Path, wheel: Path, destination: Path) -> None:
	_ = subprocess.run(
		[
			"uv",
			"pip",
			"install",
			"--python",
			str(python),
			"--target",
			str(destination),
			"--no-deps",
			"-r",
			str(RUNTIME_REQUIREMENTS),
			str(wheel),
		],
		check=True,
	)
	matches = list(destination.glob("polyg2p-*.dist-info"))
	if len(matches) != 1:
		raise RuntimeError(f"Expected one installed polyg2p distribution, found {len(matches)}")
	info = matches[0]
	direct_url = info / "direct_url.json"
	if direct_url.exists():
		record = info / "RECORD"
		lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
		prefix = f"{info.name}/direct_url.json,"
		remaining = [line for line in lines if not line.startswith(prefix)]
		if len(remaining) != len(lines) - 1:
			raise RuntimeError("The polyg2p installation record is inconsistent")
		_ = record.write_text("".join(remaining), encoding="utf-8", newline="")
		direct_url.unlink()


def build(
	output: Path,
	*,
	bluetts_dir: Path | None = None,
	bluetts_description: str | None = None,
	polyg2p_dir: Path | None = None,
	polyg2p_description: str | None = None,
	blue_source: resource_sources.GitSource | None = None,
	source_output: Path | None = None,
) -> None:
	output = output.resolve()
	if output.exists():
		raise FileExistsError(f"Archive already exists: {output}")
	if not output.parent.is_dir():
		raise FileNotFoundError(f"Archive directory does not exist: {output.parent}")
	if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
		raise RuntimeError("The runtime build requires Windows and Python 3.13")
	rustc, cargo = tool_version("rustc"), tool_version("cargo")
	with tempfile.TemporaryDirectory(prefix="shravya-runtime-build-") as temp:
		work = Path(temp)
		inputs: dict[str, object] = {}
		if bluetts_dir is None:
			if bluetts_description is not None:
				raise ValueError("A local BlueTTS description requires a source directory")
			resolved = blue_source if blue_source is not None else resource_sources.resolve_blue_source()
			bluetts = resource_sources.download_source(work, resolved.repository, resolved.revision)
			if source_output is not None:
				resource_sources.preserve_source(bluetts, source_output / "bluetts")
			inputs["bluetts_repository"] = resolved.repository
			inputs["bluetts_revision"] = resolved.revision
		else:
			if blue_source is not None:
				raise ValueError("Choose either local or upstream BlueTTS sources")
			inputs["bluetts_source"] = resource_sources.local_description(bluetts_dir, bluetts_description)
			bluetts = bluetts_dir
		if polyg2p_dir is None:
			if polyg2p_description is not None:
				raise ValueError("A local polyg2p description requires a source directory")
			repository = resource_sources.POLYG2P_REPOSITORY
			revision = resource_sources.github_revision(repository)
			polyg2p = resource_sources.download_source(work, repository, revision)
			if source_output is not None:
				resource_sources.preserve_source(polyg2p, source_output / "polyg2p")
			inputs["polyg2p_repository"] = repository
			inputs["polyg2p_revision"] = revision
		else:
			inputs["polyg2p_source"] = resource_sources.local_description(polyg2p_dir, polyg2p_description)
			polyg2p = polyg2p_dir
		python = build_environment(work, "builder")
		wheel, msvc = build_polyg2p(work, polyg2p, python)
		inputs.update(
			{
				"runtime_requirements": RUNTIME_REQUIREMENTS.read_text(encoding="utf-8"),
				"build_requirements": BUILD_REQUIREMENTS.read_text(encoding="utf-8"),
				"python": sys.version,
				"rustc": rustc,
				"cargo": cargo,
				"msvc": msvc,
			}
		)
		stage = work / "resources"
		prepare_resource_runtime.prepare_runtime(
			bluetts, stage / "python", lambda target: install_runtime(python, wheel, target), inputs
		)
		build_resource_pack.build_component(stage, output, "runtime", f"runtime-{uuid.uuid4().hex}")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("output", type=Path, help="new runtime ZIP")
	_ = parser.add_argument("--source-choice", choices=("latest", "local"), required=True)
	_ = parser.add_argument("--bluetts-dir", type=Path, help="local BlueTTS source directory")
	_ = parser.add_argument("--bluetts-description", help="identity of the local BlueTTS source")
	_ = parser.add_argument("--polyg2p-dir", type=Path, help="local polyg2p source directory")
	_ = parser.add_argument("--polyg2p-description", help="identity of the local polyg2p source")
	args = parser.parse_args()
	local = (args.bluetts_dir, args.bluetts_description, args.polyg2p_dir, args.polyg2p_description)
	if args.source_choice == "local" and not all(local):
		parser.error("Local builds require both source directories and their descriptions")
	if args.source_choice == "latest" and any(local):
		parser.error("Local source options require --source-choice local")
	build(
		args.output,
		bluetts_dir=args.bluetts_dir,
		bluetts_description=args.bluetts_description,
		polyg2p_dir=args.polyg2p_dir,
		polyg2p_description=args.polyg2p_description,
	)
	print(f"Built {args.output} ({args.output.stat().st_size} bytes)")
