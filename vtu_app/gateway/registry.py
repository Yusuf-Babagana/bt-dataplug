"""
Provider registry. Adding a provider = write an adapter class, decorate it with
``@register`` and import its module in ``adapters/__init__.py``.
"""
import logging

from .base import ProviderAdapter
from .config import ProviderConfig

logger = logging.getLogger("vtu_app.gateway")

_CLASSES = {}
_loaded = False


def register(cls):
    if not cls.name or cls.name != cls.name.lower():
        raise ValueError("Adapter.name must be a non-empty lowercase slug")
    _CLASSES[cls.name] = cls
    return cls


def unregister(name):
    _CLASSES.pop(name, None)


def _load():
    global _loaded
    if not _loaded:
        from . import adapters  # noqa: F401  (importing registers the built-in adapters)
        _loaded = True


def adapter_classes():
    _load()
    return dict(_CLASSES)


def get_adapter(name) -> ProviderAdapter | None:
    cls = adapter_classes().get(name)
    if cls is None:
        return None
    return cls(ProviderConfig(cls.name, cls.config_spec, default_enabled=cls.default_enabled))


def all_adapters():
    return [get_adapter(n) for n in sorted(adapter_classes())]


def validate_configuration():
    """
    Human-readable problems for every provider that is switched on in the
    environment but cannot work. Run by ``manage.py check`` (see apps.py) and
    ``manage.py vtu_providers``.
    """
    problems = []
    for adapter in all_adapters():
        if adapter.config.enabled:
            problems += [f"[{adapter.name}] {e}" for e in adapter.config.errors()]
    return problems
