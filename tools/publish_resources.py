"""Build, verify and publish split resource packs from a GitHub Actions checkout."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from tools import build_resources, check_release_catalog, smoke_resource_pair

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("catalogs") / "current.toml"


def command(*args: str) -> str:
	result = subprocess.run(args, cwd=ROOT, check=False, text=True, capture_output=True)
	if result.returncode:
		raise RuntimeError(f"{args[0]} failed (exit {result.returncode}): {result.stderr.strip()}")
	return result.stdout.strip()


def api(method: str, path: str, token: str, data: dict[str, object] | None = None) -> object:
	request = urllib.request.Request(
		f"https://api.github.com{path}",
		data=None if data is None else json.dumps(data).encode("utf-8"),
		method=method,
		headers={
			"Authorization": f"Bearer {token}",
			"Accept": "application/vnd.github+json",
			"User-Agent": "Shravya resource publication",
			"Content-Type": "application/json",
		},
	)
	with urllib.request.urlopen(request, timeout=60) as response:
		return cast(object, json.load(response))


def validate_release(data: object) -> int:
	if not isinstance(data, dict):
		raise ValueError("Invalid resource release response")
	fields = cast(dict[str, object], data)
	if fields.get("draft") is not False or fields.get("prerelease") is not True:
		raise ValueError("The resources release must be a published prerelease")
	release_id = fields.get("id")
	if type(release_id) is not int:
		raise ValueError("The resource release has no valid ID")
	return release_id


def release(repo: str, token: str, baseline: str) -> int:
	path = f"/repos/{repo}/releases/tags/resources"
	try:
		data = api("GET", path, token)
	except urllib.error.HTTPError as error:
		if error.code != 404:
			raise
		data = api(
			"POST",
			f"/repos/{repo}/releases",
			token,
			{
				"tag_name": "resources",
				"target_commitish": baseline,
				"name": "Resources",
				"draft": False,
				"prerelease": True,
			},
		)
	return validate_release(data)


def verify_assets(repo: str, token: str, release_id: int, names: set[str]) -> None:
	if validate_release(api("GET", f"/repos/{repo}/releases/tags/resources", token)) != release_id:
		raise ValueError("The resources release changed during publication")
	found: set[str] = set()
	page = 1
	while True:
		data = api("GET", f"/repos/{repo}/releases/{release_id}/assets?per_page=100&page={page}", token)
		if not isinstance(data, list):
			raise ValueError("Invalid release asset response")
		assets = cast(list[object], data)
		for asset in assets:
			if isinstance(asset, dict):
				fields = cast(dict[str, object], asset)
				name = fields.get("name")
				if isinstance(name, str) and fields.get("state") == "uploaded":
					found.add(name)
		if len(assets) < 100:
			break
		page += 1
	if not names <= found:
		raise ValueError("The resource release is missing uploaded assets")


def publish(kind: str, addons: list[str], repo: str, token: str) -> None:
	if not token:
		raise ValueError("GH_TOKEN is required")
	config = tomllib.loads((ROOT / "addon" / "synthDrivers" / "shravya" / "config.toml").read_text("utf-8"))
	expected_url = f"https://raw.githubusercontent.com/{repo}/main/catalogs/current.toml"
	if config.get("delivery", {}).get("catalog_url") != expected_url:
		raise ValueError("Configured catalog URL must point to this repository's main catalog")
	if command("git", "status", "--porcelain"):
		raise ValueError("The publication checkout must be clean")
	baseline = command("git", "rev-parse", "HEAD")
	_ = command("git", "fetch", "--no-tags", "origin", "main")
	if command("git", "rev-parse", "FETCH_HEAD") != baseline:
		raise RuntimeError("The default branch changed; restart the resource build")
	existing = ROOT / CATALOG
	with tempfile.TemporaryDirectory(prefix="shravya-publish-") as directory:
		output = Path(directory)
		catalog = build_resources.build(
			kind,
			output,
			repo,
			"resources",
			addons,
			existing if kind != "both" else None,
			preserve_sources=False,
		)
		if catalog is None:
			raise RuntimeError("Resource builder did not create a catalog")
		new_archives = list(output.glob("*.zip"))
		data = tomllib.loads(catalog.read_text("utf-8"))
		pair: dict[str, Path] = {}
		for component in ("runtime", "model"):
			matches = [path for path in new_archives if path.name.startswith(f"{component}-")]
			if matches:
				pair[component] = matches[0]
			else:
				pair[component] = output / f"existing-{component}.zip"
				request = urllib.request.Request(
					data[component]["url"], headers={"User-Agent": "Shravya resource publication"}
				)
				with (
					urllib.request.urlopen(request, timeout=60) as response,
					pair[component].open("xb") as file,
				):
					shutil.copyfileobj(response, file)
			if build_resources.pack_id(pair[component], component) != data[component]["id"]:
				raise ValueError(f"The selected {component} archive does not match the catalog ID")
		smoke_resource_pair.smoke(pair["runtime"], pair["model"])
		release_id = release(repo, token, baseline)
		_ = command(
			"gh", "release", "upload", "resources", *(str(path) for path in new_archives), "--repo", repo
		)
		verify_assets(repo, token, release_id, {path.name for path in new_archives})
		for component in ("runtime", "model"):
			check_release_catalog.check_asset(data[component]["url"], component)
		_ = command("git", "fetch", "--no-tags", "origin", "main")
		if command("git", "rev-parse", "FETCH_HEAD") != baseline:
			raise RuntimeError("The default branch changed; catalog was not published")
		destination = ROOT / CATALOG
		destination.parent.mkdir(parents=True, exist_ok=True)
		_ = shutil.copyfile(catalog, destination)
		_ = command("git", "add", "--", str(CATALOG))
		_ = command(
			"git",
			"-c",
			"user.name=github-actions[bot]",
			"-c",
			"user.email=41898282+github-actions[bot]@users.noreply.github.com",
			"commit",
			"-m",
			"Update resource catalog",
		)
		_ = command("git", "push", "origin", "HEAD:refs/heads/main")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	_ = parser.add_argument("--kind", choices=("both", "runtime", "model"), required=True)
	_ = parser.add_argument("--addons", required=True, help="comma-separated compatible add-on versions")
	_ = parser.add_argument("--repository", required=True)
	args = parser.parse_args()
	publish(
		args.kind,
		[part.strip() for part in args.addons.split(",")],
		args.repository,
		os.getenv("GH_TOKEN", ""),
	)
