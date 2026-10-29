"""
Environment-based provider configuration.

Each provider NAME reads ``VTU_<NAME>_<SETTING>`` variables, e.g.
``VTU_NELLOBYTES_API_KEY``. An adapter declares which settings it needs via a
``ConfigSpec``; values are read lazily from ``os.environ`` (django-environ
already loads ``.env`` into it) and are never written to logs or the DB.
"""
import os
from dataclasses import dataclass, field

TRUE = {"1", "true", "yes", "on"}


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class ConfigSpec:
    required: tuple = ()                # setting names that must be non-empty, e.g. ("API_KEY",)
    optional: dict = field(default_factory=dict)        # name -> default
    secrets: tuple = ()                 # settings that must never be displayed
    legacy_env: dict = field(default_factory=dict)      # setting name -> older env var name(s) still honoured


class ProviderConfig:
    """Resolved settings for one provider. ``repr`` never shows secret values."""

    def __init__(self, name, spec, default_enabled=False):
        self.name = name
        self.spec = spec
        self.prefix = f"VTU_{name.upper()}_"
        flag = _env(self.prefix + "ENABLED")
        self.enabled = (flag.lower() in TRUE) if flag else default_enabled
        self.values = {}
        for key in (*spec.required, *spec.optional):
            val = _env(self.prefix + key)
            if not val and key in spec.legacy_env:
                legacy = spec.legacy_env[key]
                for old_name in ((legacy,) if isinstance(legacy, str) else legacy):
                    val = val or _env(old_name)
            self.values[key] = val or spec.optional.get(key, "")
        pr = _env(self.prefix + "PRIORITY")
        self.priority = int(pr) if pr.lstrip("-").isdigit() else None

    def get(self, key, default=""):
        return self.values.get(key) or default

    def errors(self):
        return [f"{self.prefix}{k} is not set" for k in self.spec.required if not self.values.get(k)]

    @property
    def is_valid(self):
        return not self.errors()

    def public_view(self):
        """Safe-to-display settings: secrets masked."""
        return {
            k: (("(set)" if v else "(missing)") if k in self.spec.secrets else v)
            for k, v in self.values.items()
        }

    def __repr__(self):
        return f"<ProviderConfig {self.name} enabled={self.enabled} {self.public_view()}>"


def failure_threshold():
    return int(_env("VTU_FAILURE_THRESHOLD", "5"))


def suspend_minutes():
    return int(_env("VTU_SUSPEND_MINUTES", "10"))


def routing_policy_path():
    return _env("VTU_ROUTING_POLICY", "vtu_app.gateway.router.PriorityPolicy")
