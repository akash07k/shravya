import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import addonVersion
import buildVars


class TestBuildVersions(unittest.TestCase):
	def test_rejects_invalid_versions_and_mismatched_release_tags(self):
		self.assertEqual(buildVars.incrementAddonBuildVersion("0.1.99"), "0.1.100")
		self.assertEqual(buildVars.validateReleaseTagVersion("1.2.3", "1.2.3"), "1.2.3")
		for version in ("", "1.2", "1.2.3.4", "1.2.x", "v1.2.3"):
			with self.subTest(version=version), self.assertRaises(ValueError):
				buildVars.incrementAddonBuildVersion(version)
		with self.assertRaises(ValueError):
			buildVars.validateReleaseTagVersion("1.2.4", "1.2.3")

	def test_invalid_counter_and_failed_persistence_preserve_existing_state(self):
		with tempfile.TemporaryDirectory() as directory:
			counter = Path(directory) / ".build-version.toml"
			buildVars.persistLocalBuildVersion("0.1.2", counter)
			self.assertEqual(buildVars.readLocalBuildVersion("0.1.0", counter), "0.1.2")
			counter.with_name(".build-version.toml.tmp").write_text("occupied\n", encoding="ascii")
			with self.assertRaises(FileExistsError):
				buildVars.persistLocalBuildVersion("0.1.3", counter)
			self.assertEqual(counter.read_text(encoding="ascii"), 'version = "0.1.2"\n')
			for contents in ("", "version = 1.2", 'version = "0.0.1"', 'version = "invalid"', "version = ["):
				with self.subTest(contents=contents):
					counter.write_text(contents, encoding="ascii")
					with self.assertRaises(ValueError):
						buildVars.readLocalBuildVersion("0.1.0", counter)
					self.assertEqual(counter.read_text(encoding="ascii"), contents)


class TestPackagedBuildVersions(unittest.TestCase):
	def setUp(self):
		temporary = tempfile.TemporaryDirectory()
		self.addCleanup(temporary.cleanup)
		self.root = Path(temporary.name)
		project = Path(__file__).resolve().parents[1]
		for name in (
			"sconstruct",
			"buildVars.py",
			"addonVersion.py",
			"manifest.ini.tpl",
			"manifest-translated.ini.tpl",
			"README.md",
			"style.css",
		):
			shutil.copy2(project / name, self.root / name)
		shutil.copytree(project / "site_scons", self.root / "site_scons")
		(self.root / "addon").mkdir()
		(self.root / "addon" / ".gitkeep").touch()
		shutil.copy2(project / "addon" / "installTasks.py", self.root / "addon" / "installTasks.py")
		config = self.root / "addon" / "synthDrivers" / "shravya" / "config.toml"
		config.parent.mkdir(parents=True)
		shutil.copy2(project / "addon" / "synthDrivers" / "shravya" / "config.toml", config)
		self.counter = self.root / ".build-version.toml"

	def counterVersion(self):
		return tomllib.loads(self.counter.read_text(encoding="utf-8"))["version"]

	def runScons(self, *arguments):
		return subprocess.run(
			[sys.executable, "-m", "SCons", *arguments],
			cwd=self.root,
			capture_output=True,
			text=True,
			check=False,
		)

	def build(self, *arguments):
		result = self.runScons(*arguments)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

	def assertPackage(self, version, channel="None"):
		package = self.root / "dist" / f"shravya-{version}.nvda-addon"
		with ZipFile(package) as archive:
			manifest = archive.read("manifest.ini").decode("utf-8").splitlines()
			self.assertIn(f"version = {version}", manifest)
			self.assertIn(f"updateChannel = {channel}", manifest)
			self.assertIn(
				f"<title>Shravya {version}</title>",
				archive.read("doc/en/readme.html").decode("utf-8"),
			)
			self.assertNotIn(".gitkeep", archive.namelist())
			self.assertEqual(
				archive.read("installTasks.py"),
				(self.root / "addon" / "installTasks.py").read_bytes(),
			)
			self.assertEqual(
				archive.read("synthDrivers/shravya/config.toml"),
				(self.root / "addon" / "synthDrivers" / "shravya" / "config.toml").read_bytes(),
			)
			self.assertNotIn("debug-diagnostics.enabled", archive.namelist())
		self.assertFalse((self.root / package.name).exists())
		return package

	def test_successful_builds_increment_but_failed_builds_do_not(self):
		first = buildVars.incrementAddonBuildVersion(addonVersion.version)
		second = buildVars.incrementAddonBuildVersion(first)
		for version in (first, second):
			self.build()
			self.assertPackage(version)
			self.assertEqual(self.counterVersion(), version)

		nextVersion = buildVars.incrementAddonBuildVersion(second)
		(self.root / "dist" / f"shravya-{nextVersion}.nvda-addon").mkdir()
		result = self.runScons()
		self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self.counterVersion(), second)

	def test_explicit_development_clean_and_nonpackage_commands_do_not_increment(self):
		self.build()
		first = buildVars.incrementAddonBuildVersion(addonVersion.version)
		defaultPackage = self.assertPackage(first)
		state = self.counter.read_bytes()

		self.build("version=9.8.7")
		self.assertPackage("9.8.7")
		self.build("version=9.8.7", "channel=dev")
		self.assertPackage("9.8.7", "dev")
		self.build("dev=1")
		self.assertPackage(date.today().strftime("%Y%m%d") + ".0.0", "dev")
		self.build("-n")
		nextVersion = buildVars.incrementAddonBuildVersion(first)
		self.assertFalse((self.root / "dist" / f"shravya-{nextVersion}.nvda-addon").exists())
		self.build("-n", "pot")
		self.build("addon/manifest.ini")
		self.build("--clean")
		self.assertFalse(defaultPackage.exists())
		self.assertEqual(self.counter.read_bytes(), state)

	def test_production_excludes_stale_debug_marker(self):
		(self.root / "addon" / "debug-diagnostics.enabled").touch()
		self.build("version=9.8.11")
		self.assertPackage("9.8.11")

	def test_packaged_help_renders_license_table_and_manifest_has_release_notes(self):
		self.build("version=9.8.12")
		package = self.assertPackage("9.8.12")
		with ZipFile(package) as archive:
			help_html = archive.read("doc/en/readme.html").decode("utf-8")
			self.assertIn("<table>", help_html)
			self.assertIn("<th>Component</th>", help_html)
			self.assertIn("<th>License</th>", help_html)
			self.assertIn("<pre><code", help_html)
			self.assertIn('href="https://github.com/akash07k/shravya/blob/main/LICENSE"', help_html)
			self.assertIn('href="https://github.com/akash07k/shravya/blob/main/COPYING.txt"', help_html)
			self.assertNotIn("In development.", archive.read("manifest.ini").decode("utf-8"))

	def test_removed_debug_option_does_not_build_a_production_package(self):
		result = self.runScons("version=9.8.13", "debugDiagnostics=true")
		self.assertNotEqual(result.returncode, 0)
		self.assertIn("debugDiagnostics option has been removed", result.stderr)
		self.assertFalse((self.root / "dist" / "shravya-9.8.13.nvda-addon").exists())


if __name__ == "__main__":
	unittest.main()
