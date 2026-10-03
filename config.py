"""Environment configuration and data-directory paths."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def setting(name, default):
    return os.getenv(name, default)


def boolean(name, default=False):
    return setting(name, str(default)).lower() in ("true", "yes", "1", "on")


@dataclass
class StoragePaths:
    """Paths shared by a run; credentials and journal remain in one directory."""

    tokens: Path = Path("/data/nanit_tokens.json")
    status: Path = Path("/data/sync_status.json")

    @classmethod
    def for_directory(cls, directory):
        directory = Path(directory)
        return cls(directory / "nanit_tokens.json", directory / "sync_status.json")

    @property
    def huckleberry_tokens(self):
        return self.tokens.with_name("huckleberry_tokens.json")

    @property
    def ownership(self):
        return self.tokens.with_name("sleep_ownership.json")

    @property
    def lock(self):
        return self.tokens.parent / "sync.lock"


DEFAULT_PATHS = StoragePaths()


@dataclass(frozen=True)
class Settings:
    """Snapshot environment values once per run; validate at their point of use."""

    timezone: str = "America/New_York"
    max_wake_minutes: str = "20"
    write_enabled: bool = False
    sync_daytime: bool = False
    use_huckleberry_hours: bool = True
    night_start: str = "18:00"
    morning_cutoff: str = "10:00"
    child_uid_map: str = ""
    nanit_email: Optional[str] = field(default=None, repr=False)
    nanit_password: Optional[str] = field(default=None, repr=False)
    huckleberry_email: Optional[str] = field(default=None, repr=False)
    huckleberry_password: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_env(cls):
        return cls(
            timezone=setting("TZ", "America/New_York"),
            max_wake_minutes=setting("MAX_WAKE_MINUTES", "20"),
            write_enabled=boolean("WRITE_ENABLED"),
            sync_daytime=boolean("SYNC_DAYTIME"),
            use_huckleberry_hours=boolean("USE_HUCKLEBERRY_HOURS", True),
            night_start=setting("NIGHT_START", "18:00"),
            morning_cutoff=setting("MORNING_CUTOFF", "10:00"),
            child_uid_map=setting("CHILD_UID_MAP", ""),
            nanit_email=os.getenv("NANIT_EMAIL"),
            nanit_password=os.getenv("NANIT_PASSWORD"),
            huckleberry_email=os.getenv("HUCKLEBERRY_EMAIL"),
            huckleberry_password=os.getenv("HUCKLEBERRY_PASSWORD"),
        )

    def huckleberry_credentials(self):
        # Retain the actionable missing-environment-variable errors.
        if self.huckleberry_email is None:
            raise KeyError("HUCKLEBERRY_EMAIL")
        if self.huckleberry_password is None:
            raise KeyError("HUCKLEBERRY_PASSWORD")
        return {"email": self.huckleberry_email, "password": self.huckleberry_password}
