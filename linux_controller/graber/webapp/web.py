"""Local-network web control and diagnostics interface."""

from __future__ import annotations

import inspect
import ipaddress
import json
import secrets
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


class WebController(Protocol):
    async def snapshot(self) -> Mapping[str, Any]: ...

    async def refresh_status(self, target_id: str) -> Any: ...

    async def submit_command(
        self,
        target_id: str,
        action: str,
        *,
        position: int | None = None,
        command_id: str,
        source: str,
        target_type: str | None = None,
    ) -> Any: ...


MOVEMENT_ACTIONS = {
    "open",
    "close",
    "stop",
    "favorite",
    "stack_up",
    "stack_down",
    "set_bottom_position",
    "set_top_position",
}


class WebRequestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    live_controls: bool = False
    allowed_networks: tuple[str, ...] = (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
    control_token: str | None = None


class WebControlService:
    """Framework-independent request validation for easy offline testing."""

    def __init__(self, controller: WebController, settings: WebSettings | None = None) -> None:
        self.controller = controller
        self.settings = settings or WebSettings()
        self._networks = tuple(ipaddress.ip_network(item) for item in self.settings.allowed_networks)

    def client_allowed(self, remote: str | None) -> bool:
        if not remote:
            return False
        try:
            address = ipaddress.ip_address(remote.split("%", 1)[0])
        except ValueError:
            return False
        return any(address in network for network in self._networks)

    async def snapshot(self) -> Mapping[str, Any]:
        value = self.controller.snapshot()
        snapshot = await value if inspect.isawaitable(value) else value
        return {
            **snapshot,
            "_web": {
                "live_controls": self.settings.live_controls,
                "token_required": self.settings.control_token is not None,
            },
        }

    async def dispatch(
        self,
        target_id: str,
        body: Mapping[str, Any],
        *,
        target_type: str,
        supplied_token: str | None = None,
    ) -> Mapping[str, Any]:
        if target_type not in {"blind", "group"}:
            raise WebRequestError("invalid target type")
        action = str(body.get("action", "")).lower()
        if action == "refresh_status":
            if self.settings.control_token is None or not secrets.compare_digest(
                supplied_token or "", self.settings.control_token
            ):
                raise WebRequestError("invalid control token", 403)
            if target_type != "blind":
                raise WebRequestError(
                    "status refresh is available only for individual blinds", 400
                )
            try:
                result = self.controller.refresh_status(target_id)
                if inspect.isawaitable(result):
                    result = await result
            except ValueError as exc:
                raise WebRequestError(str(exc), 400) from exc
            except KeyError as exc:
                raise WebRequestError(str(exc), 404) from exc
            return {"accepted": True, "result": _json_value(result)}
        if action not in MOVEMENT_ACTIONS:
            raise WebRequestError("unsupported action")
        if not self.settings.live_controls:
            raise WebRequestError("live controls are disabled by configuration", 403)
        if self.settings.control_token is not None and not secrets.compare_digest(
            supplied_token or "", self.settings.control_token
        ):
            raise WebRequestError("invalid control token", 403)

        position: int | None = None
        if action.startswith("set_"):
            raw_position = body.get("position")
            if isinstance(raw_position, bool):
                raise WebRequestError("position must be an integer from 0 to 100")
            try:
                position = int(raw_position)
            except (TypeError, ValueError):
                raise WebRequestError("position must be an integer from 0 to 100") from None
            if not 0 <= position <= 100:
                raise WebRequestError("position must be an integer from 0 to 100")

        command_id = str(body.get("command_id") or secrets.token_hex(16))
        result = self.controller.submit_command(
            target_id,
            action,
            position=position,
            command_id=command_id,
            source="web",
            target_type=target_type,
        )
        try:
            if inspect.isawaitable(result):
                result = await result
        except KeyError as exc:
            raise WebRequestError(str(exc), 404) from exc
        except ValueError as exc:
            raise WebRequestError(str(exc), 400) from exc
        return {"accepted": True, "command_id": command_id, "result": _json_value(result)}


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def create_app(controller: WebController, settings: WebSettings | None = None) -> Any:
    """Create an aiohttp app. aiohttp is imported lazily for tooling/tests."""
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - exercised on minimal hosts
        raise RuntimeError("the web interface requires aiohttp") from exc

    service = WebControlService(controller, settings)
    package_root = Path(__file__).parent
    template = (package_root / "templates" / "index.html").read_text(encoding="utf-8")
    stylesheet = (package_root / "static" / "app.css").read_text(encoding="utf-8")
    javascript = (package_root / "static" / "control.js").read_text(encoding="utf-8")

    @web.middleware
    async def local_network_only(request: Any, handler: Any) -> Any:
        if not service.client_allowed(request.remote):
            return web.json_response({"error": "client is outside allowed networks"}, status=403)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    async def index(_request: Any) -> Any:
        return web.Response(text=template, content_type="text/html")

    async def css(_request: Any) -> Any:
        return web.Response(text=stylesheet, content_type="text/css")

    async def js(_request: Any) -> Any:
        return web.Response(text=javascript, content_type="application/javascript")

    async def api_state(_request: Any) -> Any:
        return web.json_response(await service.snapshot())

    async def api_command(request: Any) -> Any:
        try:
            body = await request.json()
            result = await service.dispatch(
                request.match_info["target_id"],
                body,
                target_type=("blind" if request.match_info["target_type"] == "blinds" else "group"),
                supplied_token=request.headers.get("X-Graber-Control-Token"),
            )
            return web.json_response(result, status=202)
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        except WebRequestError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    app = web.Application(middlewares=[local_network_only])
    app[web.AppKey("graber_web_settings", WebSettings)] = service.settings
    app.router.add_get("/", index)
    app.router.add_get("/assets/app.css", css)
    app.router.add_get("/assets/control.js", js)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/{target_type:blinds|groups}/{target_id}/commands", api_command)
    return app


async def start_web(controller: WebController, settings: WebSettings | None = None) -> Any:
    """Start aiohttp and return its runner for graceful cleanup."""
    from aiohttp import web

    selected = settings or WebSettings()
    runner = web.AppRunner(create_app(controller, selected))
    await runner.setup()
    site = web.TCPSite(runner, selected.host, selected.port)
    await site.start()
    return runner
