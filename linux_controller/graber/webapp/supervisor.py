"""Run one setup or control child process at a time."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout


class ModeSupervisor:
    def __init__(self, *, setup_runner: Path, setup_settings: Path,
                 config_path: Path, secrets_path: Path, token_file: Path,
                 mode_file: Path, internal_host: str, internal_port: int) -> None:
        self.setup_runner = setup_runner
        self.setup_settings = setup_settings
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.token_file = token_file
        self.mode_file = mode_file
        self.internal_host = internal_host
        self.internal_port = internal_port
        self.mode = "setup"
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.active_commands = 0

    def saved_mode(self) -> str:
        try:
            value = self.mode_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "setup"
        return value if value in {"setup", "control"} else "setup"

    def control_token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    async def start(self) -> None:
        self.mode = self.saved_mode()
        if self.mode == "control" and not (self.config_path.exists() and self.secrets_path.exists()):
            self.mode = "setup"
        await self._start_child(self.mode)

    async def close(self) -> None:
        async with self.lock:
            await self._stop_child()

    async def ensure_running(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self._start_child(self.mode)

    async def switch(self, mode: str) -> None:
        if mode not in {"setup", "control"}:
            raise ValueError("invalid portal mode")
        async with self.lock:
            if mode == self.mode and self.process is not None and self.process.returncode is None:
                return
            previous = self.mode
            await self._stop_child()
            try:
                await self._start_child(mode)
            except Exception:
                await self._start_child(previous)
                raise
            self.mode = mode
            self.mode_file.parent.mkdir(parents=True, exist_ok=True)
            self.mode_file.write_text(mode + "\n", encoding="utf-8")
            os.chmod(self.mode_file, 0o600)

    async def _start_child(self, mode: str) -> None:
        environment = os.environ.copy()
        if mode == "setup":
            environment.update({
                "GRABER_SETUP_BIND_HOST": self.internal_host,
                "GRABER_SETUP_BIND_PORT": str(self.internal_port),
                "GRABER_SETUP_BIND_ALLOWED_NETWORKS": "127.0.0.0/8,::1/128",
                "GRABER_SETUP_EXISTING_CONFIG": str(self.config_path),
                "GRABER_SETUP_EXISTING_SECRETS": str(self.secrets_path),
            })
            command = (str(self.setup_runner), str(self.setup_settings))
        else:
            command = (
                sys.executable, "-m", "graber.main",
                "--config", str(self.config_path), "--secrets", str(self.secrets_path),
                "--portal-host", self.internal_host, "--portal-port", str(self.internal_port),
                "--portal-allowed-network", "127.0.0.0/8",
                "--portal-allowed-network", "::1/128",
                "--portal-control-token-file", str(self.token_file),
            )
        self.process = await asyncio.create_subprocess_exec(*command, env=environment)
        await self._wait_ready(mode)

    async def _wait_ready(self, mode: str) -> None:
        deadline = asyncio.get_running_loop().time() + 20
        headers = {"X-Graber-Control-Token": self.control_token()} if mode == "setup" else {}
        async with ClientSession(timeout=ClientTimeout(total=1)) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self.process is None or self.process.returncode is not None:
                    raise RuntimeError(f"{mode} service exited during startup")
                try:
                    async with client.get(
                        f"http://{self.internal_host}:{self.internal_port}/api/state",
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            return
                except (OSError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.2)
        await self._stop_child()
        raise RuntimeError(f"{mode} service did not become ready")

    async def _stop_child(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
