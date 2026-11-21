import importlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_resource_install import write_pack
from tools import smoke_resource_pair


class TestSmokeResourcePair(unittest.TestCase):
	def test_valid_pair_runs_cpu_synthesis_and_empty_output_fails(self):
		with tempfile.TemporaryDirectory() as temp:
			root = Path(temp)
			runtime, model = root / "runtime.zip", root / "model.zip"
			write_pack(runtime, kind="runtime", version="runtime-1")
			write_pack(model, kind="model", version="model-1")
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
					smoke_resource_pair.smoke(runtime, model)
					self.assertEqual(os.environ["POLYG2P_DATA"], "original")
					self.assertFalse(factory.call_args.kwargs["use_cuda"])
					engine.close.assert_called_once()
					engine.stream.return_value = iter([])
					with self.assertRaisesRegex(ValueError, "no audio"):
						smoke_resource_pair.smoke(runtime, model)
			self.assertTrue(runtime.is_file() and model.is_file())
