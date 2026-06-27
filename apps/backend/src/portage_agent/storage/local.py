"""LocalStorage — filesystem blob store (implements core.StorageBackend).

Phase 1 adapter: writes artifacts (e.g. a job's report.json) under a base directory on the
shared `workspaces` volume. The `s3` adapter is the cloud swap, behind the same interface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from portage_agent.config import settings


class LocalStorage:
    """core.StorageBackend backed by the local filesystem, rooted at `base_dir`."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or settings.artifacts_dir)

    def _path(self, key: str) -> Path:
        # Keys are relative; guard against escaping the base dir.
        p = (self.base / key).resolve()
        if not str(p).startswith(str(self.base.resolve())):
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        path = self._path(key)
        await asyncio.to_thread(self._write, path, data)
        return str(path)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return await asyncio.to_thread(path.read_bytes)
