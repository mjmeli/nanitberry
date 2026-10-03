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


class SyntheticOwnership:
    """Keep scheduling tests focused on intervals; ownership has its own tests."""
    def __init__(self, *_): pass
    def save(self): pass
    def candidates(self, *_): return []
    async def sync_span(self, api, nanit_uid, child_uid, span, *, write, kind):
        sync.LOG.info("%s %s–%s (%s)", "WRITE" if write else "DRY RUN", *span, kind)
        if write:
            current = await sync.strict_sleep_intervals(api, child_uid, *span)
            if any(sync.overlap(span, sync.existing_range(entry)) for entry in current):
                sync.LOG.warning("Skipping %s–%s: Huckleberry changed during sync", *span)
                return
            await api.log_sleep(child_uid, start_time=span[0], end_time=span[1])
