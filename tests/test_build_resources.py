import os
import importlib
import io
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import build_resources, resource_sources

PROJECT = Path(__file__).resolve().parents[1]


def fake_archive(output, kind):
	with zipfile.ZipFile(output, "w") as archive:
		archive.writestr("pack-kind.txt", kind)
		archive.writestr("pack-version.txt", f"{kind}-generated")


class TestResourceBuildEntry(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		self.build_id = "a" * 32
		unique = patch.object(build_resources.uuid, "uuid4", return_value=types.SimpleNamespace(hex=self.build_id))
		unique.start()
		self.addCleanup(unique.stop)
		resolver = patch.object(
			resource_sources, "resolve_blue_source",
			return_value=resource_sources.GitSource("example/BlueTTS", "b" * 40),
		)
		self.resolve = resolver.start()
		self.addCleanup(resolver.stop)
		runtime = patch.object(
			build_resources.build_resource_runtime, "build",
			side_effect=lambda output, **kwargs: fake_archive(output, "runtime"),
		)
		self.runtime = runtime.start()
		self.addCleanup(runtime.stop)
		model = patch.object(
			build_resources.build_resource_model, "build",
			side_effect=lambda output, bundle, source, **kwargs: fake_archive(output, "model"),
		)
		self.model = model.start()
		self.addCleanup(model.stop)

	def build(self, kind="both", existing=None):
		return build_resources.build(
			kind, self.root, "akash07k/shravya", "v1.2.3", ["1.2.3"], existing
		)

	def test_builds_both_and_generates_catalog_from_real_archive_ids(self):
		catalog = self.build()
		self.assertEqual(self.runtime.call_count, 1)
		self.assertEqual(self.model.call_count, 1)
		self.resolve.assert_called_once_with()
		self.assertEqual(self.runtime.call_args.kwargs["blue_source"], self.model.call_args.kwargs["blue_source"])
		source_output = self.root / f"sources-{self.build_id}"
		self.assertTrue(source_output.is_dir())
		self.assertEqual(self.runtime.call_args.kwargs["source_output"], source_output)
		self.assertEqual(self.model.call_args.kwargs["source_output"], source_output)
		data = tomllib.loads(catalog.read_text(encoding="utf-8"))
		self.assertEqual(data["addons"], ["1.2.3"])
		self.assertEqual(data["interface"], "bluev3-v1")
		for kind in ("runtime", "model"):
			self.assertEqual(data[kind]["id"], f"{kind}-generated")
			self.assertEqual(
				data[kind]["url"],
				f"https://github.com/akash07k/shravya/releases/download/v1.2.3/"
				f"{kind}-v1.2.3-{self.build_id}.zip",
			)
		self.assert_catalog_is_accepted(catalog, "1.2.3")

	def assert_catalog_is_accepted(self, path, version):
		package = types.ModuleType("builder_catalog_test")
		package.__path__ = [str(PROJECT / "addon" / "synthDrivers" / "shravya")]
		with patch.dict(sys.modules, {"builder_catalog_test": package}):
			catalog = importlib.import_module("builder_catalog_test._catalog")
			with patch.object(catalog, "urlopen", return_value=io.BytesIO(path.read_bytes())):
				catalog.fetch_catalog("https://example.test/catalog", version)

	def test_example_catalog_matches_the_runtime_schema(self):
		self.assert_catalog_is_accepted(PROJECT / "resource-catalog.example.toml", "0.1.0")

	def test_one_pack_reuses_other_entry(self):
		existing = self.root / "old.toml"
		existing.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			'[runtime]\nid = "runtime-old"\nurl = "https://example.test/old.zip"\n'
			'[model]\nid = "model-old"\nurl = "https://example.test/model.zip"\n',
			encoding="utf-8",
		)
		catalog = self.build("model", existing)
		self.assertEqual(self.model.call_count, 1)
		self.runtime.assert_not_called()
		data = tomllib.loads(catalog.read_text(encoding="utf-8"))
		self.assertEqual(data["runtime"], {
			"id": "runtime-old", "url": "https://example.test/old.zip",
		})
		self.assertEqual(data["model"]["id"], "model-generated")
		catalog = build_resources.build(
			"runtime", self.root / "other", "akash07k/shravya", "v1.2.3", ["1.2.3"], existing
		)
		self.assertEqual(
			tomllib.loads(catalog.read_text(encoding="utf-8"))["model"],
			{"id": "model-old", "url": "https://example.test/model.zip"},
		)

	def test_rebuilding_same_release_keeps_previous_assets(self):
		original = self.build()
		with patch.object(build_resources.uuid, "uuid4", return_value=types.SimpleNamespace(hex="b" * 32)):
			updated = self.build("model", original)
		first = tomllib.loads(original.read_text(encoding="utf-8"))
		second = tomllib.loads(updated.read_text(encoding="utf-8"))
		self.assertEqual(first["runtime"], second["runtime"])
		self.assertNotEqual(first["model"]["url"], second["model"]["url"])
		self.assertTrue((self.root / f"model-v1.2.3-{self.build_id}.zip").exists())

	def test_local_only_build_needs_no_publication_details(self):
		bluetts = self.root / "bluetts"
		polyg2p = self.root / "polyg2p"
		model = self.root / "model"
		for directory in (bluetts, polyg2p, model):
			directory.mkdir()
		sources = build_resources.LocalSources(
			bluetts, "local blue", polyg2p, "local g2p", model, "local model",
		)
		catalog = build_resources.build(
			"both", self.root / "output", None, None, None, None,
			source_choice="local", sources=sources,
		)
		self.assertIsNone(catalog)
		self.assertTrue((self.root / "output" / f"runtime-{self.build_id}.zip").is_file())
		self.assertTrue((self.root / "output" / f"model-{self.build_id}.zip").is_file())
		self.assertFalse(list((self.root / "output").glob("*.toml")))
		self.assertIsNone(self.runtime.call_args.kwargs["source_output"])
		self.assertIsNone(self.model.call_args.kwargs["source_output"])
		self.resolve.assert_not_called()
		self.assertEqual(self.runtime.call_args.kwargs["bluetts_dir"], bluetts)
		self.assertEqual(self.model.call_args.kwargs["bluetts_dir"], bluetts)
		self.assertEqual(self.model.call_args.args[1:3], (model, "local model"))

	def test_missing_local_source_fails_before_building(self):
		with self.assertRaisesRegex(ValueError, "local model"):
			build_resources.build(
				"both", self.root, None, None, None, None,
				source_choice="local", sources=build_resources.LocalSources(),
			)
		self.runtime.assert_not_called()
		self.model.assert_not_called()
		self.resolve.assert_not_called()

	def test_interactive_entry_builds_the_requested_catalog(self):
		answers = ["1", "1", str(self.root), "2", "akash07k/shravya", "resources", "1.2.3,1.2.4"]
		with (
			patch.object(sys, "argv", ["build_resources"]),
			patch("builtins.input", side_effect=answers),
		):
			self.assertEqual(build_resources.main(), 0)
		catalog = self.root / f"catalog-resources-{self.build_id}.toml"
		data = tomllib.loads(catalog.read_text(encoding="utf-8"))
		self.assertEqual(data["addons"], ["1.2.3", "1.2.4"])
		self.assertIn(f"/resources/runtime-resources-{self.build_id}.zip", data["runtime"]["url"])

	def test_interactive_local_model_does_not_request_publication_or_quantizer_for_int8(self):
		model = self.root / "source"
		(model / "onnx").mkdir(parents=True)
		(model / "onnx" / "vector_estimator.int8.onnx").touch()
		answers = ["3", "2", str(self.root / "out"), "1", str(model), "local model"]
		with (
			patch.object(sys, "argv", ["build_resources"]),
			patch("builtins.input", side_effect=answers) as prompt,
		):
			self.assertEqual(build_resources.main(), 0)
		self.assertEqual(prompt.call_count, len(answers))
		self.resolve.assert_not_called()
		self.assertIsNone(self.model.call_args.kwargs["bluetts_dir"])
		self.assertFalse(list((self.root / "out").glob("*.toml")))

	def test_cli_requires_explicit_source_and_delivery_without_prompting(self):
		for args in (
			["--kind", "both", "--output", str(self.root)],
			["--kind", "both", "--output", str(self.root), "--source", "latest"],
			["--kind", "both", "--output", str(self.root), "--source", "latest",
			 "--delivery", "local", "--repository", "owner/repo"],
		):
			with (
				self.subTest(args=args),
				patch.object(sys, "argv", ["build_resources", *args]),
				patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
				self.assertRaises(SystemExit),
			):
				build_resources.main()
		self.runtime.assert_not_called()
		self.model.assert_not_called()

	def test_noninteractive_local_runtime_uses_supplied_sources(self):
		blue = self.root / "blue"
		poly = self.root / "poly"
		blue.mkdir()
		poly.mkdir()
		args = [
			"build_resources", "--kind", "runtime", "--output", str(self.root / "out"),
			"--source", "local", "--delivery", "local",
			"--bluetts-dir", str(blue), "--bluetts-description", "blue source",
			"--polyg2p-dir", str(poly), "--polyg2p-description", "poly source",
		]
		with (
			patch.object(sys, "argv", args),
			patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
		):
			self.assertEqual(build_resources.main(), 0)
		self.assertEqual(self.runtime.call_args.kwargs["bluetts_dir"], blue)
		self.assertEqual(self.runtime.call_args.kwargs["polyg2p_dir"], poly)
		self.model.assert_not_called()
		self.resolve.assert_not_called()

	def test_invalid_menu_selection_fails_without_building(self):
		with (
			patch.object(sys, "argv", ["build_resources"]),
			patch("builtins.input", return_value="4"),
		):
			self.assertEqual(build_resources.main(), 1)
		self.runtime.assert_not_called()
		self.model.assert_not_called()

	def test_existing_outputs_invalid_input_and_failed_build_do_not_write_catalog(self):
		catalog = self.root / f"catalog-v1.2.3-{self.build_id}.toml"
		archive = self.root / f"runtime-v1.2.3-{self.build_id}.zip"
		archive.write_bytes(b"keep")
		with self.assertRaises(FileExistsError):
			self.build()
		self.runtime.assert_not_called()
		self.model.assert_not_called()
		self.assertEqual(archive.read_bytes(), b"keep")
		self.assertFalse(catalog.exists())
		with self.assertRaises(ValueError):
			build_resources.build("model", self.root, "invalid", "v1.2.3", ["1.2.3"], None)
		with self.assertRaises(ValueError):
			self.build("model")
		archive.unlink()
		self.runtime.side_effect = subprocess.CalledProcessError(1, "build")
		with self.assertRaises(subprocess.CalledProcessError):
			self.build()
		self.assertFalse(catalog.exists())

	def test_existing_source_folder_is_not_replaced(self):
		saved = self.root / f"sources-{self.build_id}"
		saved.mkdir()
		(saved / "keep").write_bytes(b"existing")
		with self.assertRaises(FileExistsError):
			self.build()
		self.runtime.assert_not_called()
		self.assertEqual((saved / "keep").read_bytes(), b"existing")

	def test_wrong_archive_kind_is_rejected(self):
		def wrong_kind(output, **kwargs):
			with zipfile.ZipFile(output, "w") as archive:
				archive.writestr("pack-kind.txt", "model")
				archive.writestr("pack-version.txt", "model-1")

		self.runtime.side_effect = wrong_kind
		with self.assertRaisesRegex(ValueError, "runtime"):
			self.build()
		self.assertFalse((self.root / f"catalog-v1.2.3-{self.build_id}.toml").exists())

	def test_invalid_existing_pack_id_is_rejected(self):
		existing = self.root / "old.toml"
		existing.write_text(
			'format = 1\ninterface = "bluev3-v1"\n'
			'[runtime]\nid = "runtime."\nurl = "https://example.test/runtime.zip"\n',
			encoding="utf-8",
		)
		with self.assertRaisesRegex(ValueError, "runtime"):
			self.build("model", existing)
		self.model.assert_not_called()

	@unittest.skipUnless(os.name == "nt", "Windows batch entry point")
	def test_batch_entry_point_runs_commands_and_propagates_failures(self):
		batch = self.root / "build_resources.bat"
		batch.write_bytes((PROJECT / "build_resources.bat").read_bytes())
		fake_uv = self.root / "uv.cmd"
		fake_uv.write_text(
			'@echo off\r\necho %*>>"%SHRAVYA_TEST_LOG%"\r\n'
			'if "%SHRAVYA_FAIL_SYNC%"=="1" if "%1"=="sync" exit /b 9\r\nexit /b 0\r\n',
			encoding="ascii",
		)
		log = self.root / "calls.txt"
		env = os.environ.copy()
		env.update(PATH=f"{self.root};{env['PATH']}", SHRAVYA_TEST_LOG=str(log))
		result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", str(batch), "--help"], env=env)
		self.assertEqual(result.returncode, 0)
		self.assertEqual(log.read_text().splitlines(), [
			"sync --locked", "run --no-sync python -B -m tools.build_resources --help",
		])
		log.unlink()
		env["SHRAVYA_FAIL_SYNC"] = "1"
		result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", str(batch), "--help"], env=env)
		self.assertEqual(result.returncode, 9)
		self.assertEqual(log.read_text().splitlines(), ["sync --locked"])


class TestResourceSources(unittest.TestCase):
	def test_configured_repository_and_branch_are_resolved(self):
		with tempfile.TemporaryDirectory() as temp:
			config = Path(temp) / "config.toml"
			config.write_text(
				'[sources]\nbluetts_repository = "example/BlueTTS"\nbluetts_branch = "feature/voice"\n',
				encoding="utf-8",
			)
			source = resource_sources.load_blue_source(config)
			self.assertEqual(source, ("example/BlueTTS", "feature/voice"))
			with patch.object(
				resource_sources.urllib.request,
				"urlopen",
				return_value=io.BytesIO(b'[{"sha":"' + b"a" * 40 + b'"}]'),
			) as open_url:
				self.assertEqual(resource_sources.github_revision(*source), "a" * 40)
			url = open_url.call_args.args[0].full_url
			self.assertIn("/example/BlueTTS/commits?", url)
			self.assertIn("sha=feature%2Fvoice", url)

	def test_default_source_and_model_revisions_are_validated(self):
		self.assertEqual(resource_sources.load_blue_source(), ("maxmelichov/BlueTTS", None))
		with patch.object(
			resource_sources.urllib.request,
			"urlopen",
			return_value=io.BytesIO(b'{"sha":"' + b"b" * 40 + b'"}'),
		):
			self.assertEqual(resource_sources.model_revision(), "b" * 40)
		with patch.object(resource_sources.urllib.request, "urlopen", return_value=io.BytesIO(b'{"sha":"main"}')):
			with self.assertRaisesRegex(ValueError, "revision"):
				resource_sources.model_revision()

	def test_invalid_source_configuration_is_reported(self):
		with tempfile.TemporaryDirectory() as temp:
			config = Path(temp) / "config.toml"
			for text in (
				'[sources]\nbluetts_repository = "https://example.test/repo"\n',
				'[sources]\nbluetts_repository = "owner/repo"\nbluetts_branch = 4\n',
				'[sources]\nbluetts_repository = "owner/repo"\nunexpected = "value"\n',
			):
				with self.subTest(text=text):
					config.write_text(text, encoding="utf-8")
					with self.assertRaises(ValueError):
						resource_sources.load_blue_source(config)


if __name__ == "__main__":
	unittest.main()
