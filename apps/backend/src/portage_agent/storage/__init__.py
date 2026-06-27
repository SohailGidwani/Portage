"""Artifact storage adapters (implements core.StorageBackend)."""

from .local import LocalStorage

__all__ = ["LocalStorage"]
