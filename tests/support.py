"""Import sync.py with stand-ins for optional network dependencies."""
import importlib.util
import sys
import types
from pathlib import Path

fake_aiohttp = types.ModuleType("aiohttp")
fake_aiohttp.ClientError = type("ClientError", (Exception,), {})
fake_aiohttp.ClientResponseError = type("ClientResponseError", (fake_aiohttp.ClientError,), {})
fake_aiohttp.ClientTimeout = lambda **kwargs: kwargs
sys.modules.setdefault("aiohttp", fake_aiohttp)

fake_nanit = types.ModuleType("aionanit")
fake_nanit.NanitAuthError = type("NanitAuthError", (Exception,), {})
fake_nanit.NanitConnectionError = type("NanitConnectionError", (Exception,), {})
fake_nanit.NanitMfaRequiredError = type("NanitMfaRequiredError", (fake_nanit.NanitAuthError,), {})
fake_nanit.NanitClient = object
sys.modules.setdefault("aionanit", fake_nanit)

fake_api = types.ModuleType("huckleberry_api")
fake_api.HuckleberryAPI = object
sys.modules.setdefault("huckleberry_api", fake_api)

spec = importlib.util.spec_from_file_location("sync", Path(__file__).resolve().parents[1] / "sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
