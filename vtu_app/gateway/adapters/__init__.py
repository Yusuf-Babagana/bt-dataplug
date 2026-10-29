"""Importing this package registers the built-in adapters."""
from ..registry import register
from .gongoz import GongozAdapter
from .nellobytes import NellobytesAdapter

register(NellobytesAdapter)
register(GongozAdapter)
