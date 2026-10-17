"""Controller-only .env loading. Values are never executed or logged."""

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from urllib.parse import urlparse


def read_environment(path=".env", *, environ=None):
    values = {}
    path = Path(path)
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError(f"invalid environment entry on line {number}")
            if key in values:
                raise ValueError(f"duplicate environment name: {key}")
            if value[:1] in ("'", '"'):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f"unclosed value on line {number}")
                value = value[1:-1]
            values[key] = value
    for key, value in (os.environ if environ is None else environ).items():
        if value:
            values[key] = value
    return values


@dataclass(frozen=True)
class DaytonaSettings:
    api_key: str = field(default="", repr=False)
    api_url: str = "https://app.daytona.io/api"
    target: str = ""
    snapshot: str = ""
    max_sandboxes: int = 100
    max_compute_seconds: float = 1800
    run_budget_usd: float = 5
    conservative_hourly_usd: float = 0.50
    ttl_minutes: int = 5

    @classmethod
    def load(cls, path=".env"):
        env = read_environment(path)
        return cls(
            api_key=env.get("DAYTONA_API_KEY", ""),
            api_url=env.get("DAYTONA_API_URL") or cls.api_url,
            target=env.get("DAYTONA_TARGET", ""),
            snapshot=env.get("DAYTONA_SNAPSHOT", ""),
            max_sandboxes=int(env.get("DAYTONA_MAX_SANDBOXES") or 100),
            max_compute_seconds=float(env.get("DAYTONA_MAX_COMPUTE_SECONDS") or 1800),
            run_budget_usd=float(env.get("DAYTONA_RUN_BUDGET_USD") or 5),
            conservative_hourly_usd=float(env.get("DAYTONA_HOURLY_USD") or 0.50),
        )

    def validate(self, *, require_key=True):
        from math import isfinite
        url = urlparse(self.api_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ValueError("Daytona API URL must be HTTPS without credentials/query")
        if require_key and not self.api_key:
            raise ValueError("Add DAYTONA_API_KEY to the local .env file first")
        if type(self.max_sandboxes) is not int or not 1 <= self.max_sandboxes <= 1000:
            raise ValueError("invalid sandbox count budget")
        for value in (self.max_compute_seconds, self.run_budget_usd,
                      self.conservative_hourly_usd):
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError("invalid Daytona budget")
        if not 1 <= self.ttl_minutes <= 10:
            raise ValueError("invalid sandbox TTL")

    def public_status(self):
        self.validate(require_key=False)
        return {"daytona_key_configured": bool(self.api_key),
                "api_url": self.api_url, "target": self.target or "organization default",
                "snapshot": self.snapshot or "organization default",
                "max_sandboxes": self.max_sandboxes,
                "max_compute_seconds": self.max_compute_seconds,
                "budget_usd": self.run_budget_usd,
                "live_checks_performed_by_doctor": False}
