"""Install network dependency stand-ins before importing application modules."""
import sys
import types

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

import service
import intervals
import huckleberry_sleep


class SyntheticOwnership:
    """Keep scheduling tests focused on intervals; ownership has its own tests."""
    def __init__(self, *_, **kwargs): pass
    def save(self): pass
    def candidates(self, *_): return []
    async def sync_span(self, api, nanit_uid, child_uid, span, *, write, kind):
        service.LOG.info("%s %s–%s (%s)", "WRITE" if write else "DRY RUN", *span, kind)
        if write:
            current = await huckleberry_sleep.strict_sleep_intervals(api, child_uid, *span)
            if any(intervals.overlap(span, intervals.existing_range(entry)) for entry in current):
                service.LOG.warning("Skipping %s–%s: Huckleberry changed during sync", *span)
                return
            await api.log_sleep(child_uid, start_time=span[0], end_time=span[1])
