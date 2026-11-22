import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_resource_install import write_pack
from tools import publish_resources


class TestResourcePublication(unittest.TestCase):
	def test_workflow_is_manual_serialized_and_windows_based(self):
		workflow = (
			Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build_resources.yml"
		).read_text(encoding="utf-8")
		self.assertIn("workflow_dispatch:", workflow)
		self.assertIn("cancel-in-progress: false", workflow)
		self.assertIn("runs-on: windows-latest", workflow)
		self.assertIn("$env:GITHUB_REF -ne 'refs/heads/main'", workflow)
		self.assertIn("tools.publish_resources", workflow)

	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name)
		config = self.root / "addon" / "synthDrivers" / "shravya" / "config.toml"
		config.parent.mkdir(parents=True)
		config.write_text(
			'[delivery]\ncatalog_url = "https://raw.githubusercontent.com/owner/repo/main/catalogs/current.toml"\n',
			encoding="utf-8",
		)
		self.events = []
		self.remote_checks = 0
		self.conflict = False
		self.smoke_failure = False
		self.public_failure = False
		self.root_patch = patch.object(publish_resources, "ROOT", self.root)
		self.root_patch.start()
		self.addCleanup(self.root_patch.stop)

	def command(self, *args):
		self.events.append(" ".join(args))
		if args[:2] == ("git", "status"):
			return ""
		if args == ("git", "rev-parse", "HEAD"):
			return "base"
		if args == ("git", "rev-parse", "FETCH_HEAD"):
			self.remote_checks += 1
			return "changed" if self.conflict and self.remote_checks == 2 else "base"
		return ""

	def build(self, kind, output, repo, tag, addons, existing, *, preserve_sources):
		self.assertFalse(preserve_sources)
		self.events.append("build")
		for component in ("runtime", "model"):
			write_pack(output / f"{component}-new.zip", kind=component, version=f"{component}-1")
		catalog = output / "catalog.toml"
		catalog.write_text(
			'format = 1\ninterface = "bluev3-v1"\naddons = ["1.0.0"]\n'
			'[runtime]\nid = "runtime-1"\nurl = "https://example.test/runtime.zip"\n'
			'[model]\nid = "model-1"\nurl = "https://example.test/model.zip"\n', encoding="utf-8",
		)
		return catalog

	def smoke(self, runtime, model):
		self.events.append("smoke")
		if self.smoke_failure:
			raise ValueError("synthesis failed")

	def public_asset(self, *args):
		self.events.append("public")
		if self.public_failure:
			raise OSError("not publicly available")

	def run_publish(self):
		with (
			patch.object(publish_resources, "command", side_effect=self.command),
			patch.object(publish_resources.build_resources, "build", side_effect=self.build),
			patch.object(publish_resources.smoke_resource_pair, "smoke", side_effect=self.smoke),
			patch.object(publish_resources, "release", side_effect=lambda *args: self.events.append("release") or 42),
			patch.object(publish_resources, "verify_assets", side_effect=lambda *args: self.events.append("verify")),
			patch.object(
				publish_resources.check_release_catalog, "check_asset",
				side_effect=self.public_asset,
			),
		):
			publish_resources.publish("both", ["1.0.0"], "owner/repo", "token")

	def test_publication_orders_smoke_upload_verification_and_catalog_commit(self):
		self.run_publish()
		upload = next(index for index, event in enumerate(self.events) if event.startswith("gh release upload"))
		push = next(index for index, event in enumerate(self.events) if event.startswith("git push"))
		self.assertLess(self.events.index("smoke"), self.events.index("release"))
		self.assertLess(self.events.index("release"), upload)
		self.assertLess(upload, self.events.index("verify"))
		self.assertLess(self.events.index("verify"), self.events.index("public"))
		self.assertLess(self.events.index("public"), push)
		self.assertTrue((self.root / "catalogs" / "current.toml").is_file())

	def test_failed_smoke_or_changed_branch_never_publishes_catalog(self):
		for failure in ("smoke", "conflict", "public"):
			with self.subTest(failure=failure):
				self.events.clear()
				self.remote_checks = 0
				self.smoke_failure = failure == "smoke"
				self.conflict = failure == "conflict"
				self.public_failure = failure == "public"
				with self.assertRaises((ValueError, RuntimeError, OSError)):
					self.run_publish()
				self.assertFalse((self.root / "catalogs" / "current.toml").exists())
				self.assertFalse(any(event.startswith("git push") for event in self.events))
				if self.smoke_failure:
					self.assertNotIn("release", self.events)

	def test_existing_release_must_be_a_published_prerelease(self):
		for data in ({"id": 1, "draft": True, "prerelease": True}, {"id": 1, "draft": False, "prerelease": False}):
			with self.subTest(data=data), patch.object(publish_resources, "api", return_value=data):
				with self.assertRaises(ValueError):
					publish_resources.release("owner/repo", "token", "base")
		with patch.object(publish_resources, "api", return_value={"id": 3, "draft": False, "prerelease": True}):
			self.assertEqual(publish_resources.release("owner/repo", "token", "base"), 3)

	def test_missing_release_is_created_as_a_prerelease_without_changing_existing_tags(self):
		missing = urllib.error.HTTPError("url", 404, "missing", {}, io.BytesIO())
		with patch.object(
			publish_resources, "api",
			side_effect=[missing, {"id": 3, "draft": False, "prerelease": True}],
		) as api:
			self.assertEqual(publish_resources.release("owner/repo", "token", "base"), 3)
		payload = api.call_args.args[3]
		self.assertTrue(payload["prerelease"])
		self.assertFalse(payload["draft"])
		self.assertEqual(payload["target_commitish"], "base")

	def test_partial_build_smokes_the_existing_catalog_pack(self):
		existing = self.root / "catalogs" / "current.toml"
		existing.parent.mkdir()
		existing.write_text("existing", encoding="utf-8")
		previous = self.root / "model.zip"
		write_pack(previous, kind="model", version="model-old")
		original_build = self.build

		def build_partial(kind, output, repo, tag, addons, old, *, preserve_sources):
			self.assertEqual(old, existing)
			catalog = original_build(kind, output, repo, tag, addons, old, preserve_sources=preserve_sources)
			(output / "model-new.zip").unlink()
			catalog.write_text(
				catalog.read_text(encoding="utf-8").replace("model-1", "model-old"), encoding="utf-8"
			)
			return catalog

		def smoke(runtime, model):
			self.assertEqual(publish_resources.build_resources.pack_id(model, "model"), "model-old")
			self.events.append("smoke")

		with (
			patch.object(publish_resources, "command", side_effect=self.command),
			patch.object(publish_resources.build_resources, "build", side_effect=build_partial),
			patch.object(publish_resources.smoke_resource_pair, "smoke", side_effect=smoke),
			patch.object(publish_resources.urllib.request, "urlopen", return_value=io.BytesIO(previous.read_bytes())),
			patch.object(publish_resources, "release", return_value=42),
			patch.object(publish_resources, "verify_assets") as verify,
			patch.object(publish_resources.check_release_catalog, "check_asset"),
		):
			publish_resources.publish("runtime", ["1.0.0"], "owner/repo", "token")
		self.assertEqual(verify.call_args.args[3], {"runtime-new.zip"})
		self.assertIn("smoke", self.events)

	def test_upload_verification_rejects_missing_or_incomplete_assets(self):
		with patch.object(
			publish_resources, "api",
			side_effect=[
				{"id": 1, "draft": False, "prerelease": True},
				[{"name": "a.zip", "state": "new"}],
			],
		):
			with self.assertRaisesRegex(ValueError, "missing"):
				publish_resources.verify_assets("owner/repo", "token", 1, {"a.zip"})
