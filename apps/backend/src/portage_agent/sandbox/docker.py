"""DockerSandbox — ephemeral, network-off Docker execution (implements core.Sandbox).

Docker-out-of-Docker: the worker mounts the host Docker socket and spawns the sandbox as a
*sibling* container. Sibling bind mounts resolve on the daemon/host, so we mount the shared
**named volume** (`workspaces_volume`) rather than a bind path — the worker's
`/workspaces/<job_id>` and the sandbox's are the same files on that volume.

Isolation: `--network none`, CPU/memory/pids caps, a wall-clock timeout, `--rm`. Untrusted
repo code only ever runs here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from portage_agent.config import settings
from portage_agent.core.interfaces import SandboxResult

log = logging.getLogger("portage.sandbox")


class DockerSandbox:
    """core.Sandbox adapter backed by `docker run` against the shared workspaces volume."""

    def __init__(
        self,
        *,
        image: str | None = None,
        volume: str | None = None,
        mount: str | None = None,
    ) -> None:
        self.image = image or settings.sandbox_image
        self.volume = volume or settings.workspaces_volume
        self.mount = mount or settings.workspaces_mount

    async def run(
        self, command: list[str], *, workdir: str, timeout: int | None = None
    ) -> SandboxResult:
        timeout = timeout or settings.sandbox_timeout_seconds
        name = f"portage-sbx-{uuid.uuid4().hex[:12]}"
        docker_args = [
            "docker", "run", "--rm", "--name", name,
            "--network", "none",
            "--cpus", settings.sandbox_cpus,
            "--memory", settings.sandbox_memory,
            "--pids-limit", str(settings.sandbox_pids_limit),
            "-v", f"{self.volume}:{self.mount}",
            "-w", workdir,
            self.image,
            *command,
        ]
        log.info("sandbox run: %s (workdir=%s timeout=%ss)", " ".join(command), workdir, timeout)
        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            log.warning("sandbox timeout after %ss — killing container %s", timeout, name)
            await self._kill(name)
            with contextlib.suppress(Exception):
                await proc.wait()
            return SandboxResult(
                exit_code=124,
                stdout="",
                stderr=f"sandbox timed out after {timeout}s",
            )
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    @staticmethod
    async def _kill(name: str) -> None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except Exception:  # pragma: no cover - best effort
            log.exception("failed to kill sandbox container %s", name)
