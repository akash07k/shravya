import importlib
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_resource_install import write_pack
from tools import smoke_resource_pair


class TestSmokeResourcePair(unittest.TestCase):
	def test_synthesis_runs_on_cpu_and_empty_output_fails(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime, model = root / "runtime", root / "model"
			engine = Mock()
			engine.stream.return_value = iter([b"sound"])
			factory = Mock(return_value=engine)
			original_import = importlib.import_module

			def imports(name):
				if name == "_shravya_smoke._blue":
					return types.SimpleNamespace(BlueEngine=factory)
				return original_import(name)

			with patch.object(smoke_resource_pair.importlib, "import_module", side_effect=imports):
				with patch.dict(os.environ, {"POLYG2P_DATA": "original"}):
					smoke_resource_pair._synthesize(runtime, model)
					self.assertEqual(os.environ["POLYG2P_DATA"], "original")
					self.assertFalse(factory.call_args.kwargs["use_cuda"])
					engine.close.assert_called_once()
					engine.stream.return_value = iter([])
					with self.assertRaisesRegex(ValueError, "no audio"):
						smoke_resource_pair._synthesize(runtime, model)
					self.assertEqual(os.environ["POLYG2P_DATA"], "original")
					self.assertEqual(engine.close.call_count, 2)

	def test_synthesis_child_finishes_before_cleanup_and_failure_propagates(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime, model = root / "runtime.zip", root / "model.zip"
			write_pack(runtime, kind="runtime", version="runtime-1")
			write_pack(model, kind="model", version="model-1")
			extracted = []

			def run(args, *, cwd, check):
				self.assertTrue(check)
				self.assertIn("--synthesize", args)
				runtime_path, model_path = Path(args[-2]), Path(args[-1])
				self.assertTrue((runtime_path / "python").is_dir())
				self.assertTrue((model_path / "bluev3_onnx").is_dir())
				extracted.append(runtime_path)
				if len(extracted) == 2:
					raise subprocess.CalledProcessError(1, args)

			with patch.object(smoke_resource_pair.subprocess, "run", side_effect=run):
				smoke_resource_pair.smoke(runtime, model)
				with self.assertRaises(subprocess.CalledProcessError):
					smoke_resource_pair.smoke(runtime, model)
			for path in extracted:
				self.assertFalse(path.exists())
			self.assertTrue(runtime.is_file() and model.is_file())
