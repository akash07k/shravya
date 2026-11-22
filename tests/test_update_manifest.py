import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build_update_manifest.py"
PROJECT = SCRIPT.parents[1]
CONFIG_READER = PROJECT / "addon" / "synthDrivers" / "shravya" / "_delivery_config.py"
WORKFLOW = PROJECT / ".github" / "workflows" / "build_addon.yml"


class TestUpdateManifest(unittest.TestCase):
	def test_release_manifest_preserves_changelog_text(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			changelog = root / "changelog.md"
			output = root / "shravya-update.toml"
			notes = '## Changes\n\n- "Quoted" text, a backslash \\ and café.\n- Another change.'
			changelog.write_text(notes + "\n", encoding="utf-8")
			result = subprocess.run(
				[sys.executable, "-B", str(SCRIPT), "1.2.3", str(changelog), str(output),
				 "--resource-catalog", "https://example.test/resources.toml"],
				capture_output=True, text=True, check=False,
			)
			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(tomllib.loads(output.read_text(encoding="utf-8")), {
				"version": "1.2.3", "changes": notes,
				"resource_catalog": "https://example.test/resources.toml",
			})

	def test_invalid_version_and_missing_changelog_fail_without_manifest(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			changelog = root / "changelog.md"
			changelog.write_text("A change.\n", encoding="utf-8")
			output = root / "shravya-update.toml"
			for version, source in (("v1.2.3", changelog), ("1.2.3", root / "missing.md")):
				with self.subTest(version=version, source=source):
					result = subprocess.run(
						[sys.executable, "-B", str(SCRIPT), version, str(source), str(output)],
						capture_output=True, text=True, check=False,
					)
					self.assertNotEqual(result.returncode, 0)
					self.assertFalse(output.exists())
			result = subprocess.run(
				[sys.executable, "-B", str(SCRIPT), "1.2.3", str(changelog), str(output),
				 "--resource-catalog", "file:///local.toml"],
				capture_output=True, text=True, check=False,
			)
			self.assertNotEqual(result.returncode, 0)
			self.assertFalse(output.exists())

	def test_release_config_reader_reports_missing_and_invalid_settings(self):
		with tempfile.TemporaryDirectory() as directory:
			config = Path(directory) / "config.toml"
			for text in (
				'[delivery]\ncatalog_url = ""\n',
				'[delivery]\ncatalog_url = "file:///local.toml"\n',
				'[delivery]\ncatalog_url = "https://example.test/catalog"\nrelease_api = 42\n',
				"[delivery\n",
			):
				with self.subTest(text=text):
					config.write_text(text, encoding="utf-8")
					result = subprocess.run(
						[sys.executable, "-B", str(CONFIG_READER), str(config)],
						capture_output=True, text=True, check=False,
					)
					self.assertNotEqual(result.returncode, 0)
					self.assertEqual(result.stdout, "")
					self.assertIn("Invalid Shravya delivery configuration:", result.stderr)

	def test_tagged_workflow_uses_config_and_keeps_releases_draft(self):
		workflow = WORKFLOW.read_text(encoding="utf-8")
		self.assertIn("addon/synthDrivers/shravya/_delivery_config.py", workflow)
		self.assertIn("addon/synthDrivers/shravya/config.toml", workflow)
		self.assertIn('--resource-catalog "$catalog_url"', workflow)
		self.assertIn("draft: true", workflow)
		self.assertIn("'.draft == true'", workflow)
		self.assertIn("dist/*.nvda-addon", workflow)
		self.assertIn("dist/shravya-update.toml", workflow)
		self.assertIn('tags: ["*", "!resources"]', workflow)
		self.assertIn("github.ref != 'refs/tags/resources'", workflow)
		self.assertLess(workflow.index("tools/check_release_catalog.py"), workflow.index("- name: Release\n"))

	@unittest.skipUnless(shutil.which("bash"), "bash is required to exercise the workflow guard")
	def test_release_guard_refuses_published_releases_and_unexpected_api_errors(self):
		workflow = WORKFLOW.read_text(encoding="utf-8")
		guard = workflow.split("    - name: Protect published releases\n", 1)[1]
		guard = guard.split("      run: |\n", 1)[1].split("\n    - name: Release", 1)[0]
		stubs = """
			curl() {
				[ "${TEST_EXIT:-0}" -eq 0 ] || return "$TEST_EXIT"
				while [ "$#" -gt 0 ]; do
					if [ "$1" = "--output" ]; then shift; printf "%s" "$TEST_BODY" > "$1"; fi
					shift
				done
				printf "%s" "$TEST_STATUS"
			}
			jq() { grep -q '"draft": true' "$3"; }
		"""
		script = textwrap.dedent(stubs) + textwrap.dedent(guard)
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			env = os.environ.copy()
			env.update(
				RUNNER_TEMP=".",
				GH_TOKEN="test-token", RELEASE_TAG="v1.2.3",
				GITHUB_API_URL="https://example.test", GITHUB_REPOSITORY="owner/repo",
			)
			for status, body, curl_exit, expected in (
				("404", "", "0", 0), ("200", '{"draft": true}', "0", 0),
				("200", '{"draft": false}', "0", 1), ("500", "", "0", 1),
				("000", "", "7", 7),
			):
				with self.subTest(status=status, body=body, curl_exit=curl_exit):
					env.update(TEST_STATUS=status, TEST_BODY=body, TEST_EXIT=curl_exit)
					result = subprocess.run(
						[shutil.which("bash"), "-e", "-c", script],
						cwd=root, env=env, capture_output=True, text=True, check=False,
					)
					self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == "__main__":
	unittest.main()
