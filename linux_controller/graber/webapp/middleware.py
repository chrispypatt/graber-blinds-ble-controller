"""Restrict portal access by network, session and CSRF token."""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

from aiohttp import web
from graber.security import AuthManager


def _request_client(request: web.Request, trusted_proxies: tuple[Any, ...]) -> str:
    """Resolve a client address without trusting forwarded headers from LAN peers."""
    try:
        peer = ipaddress.ip_address((request.remote or "").split("%", 1)[0])
    except ValueError:
        return ""
    if not any(peer in network for network in trusted_proxies):
        return str(peer)
    forwarded: list[Any] = []
    for value in request.headers.get("X-Forwarded-For", "").split(","):
        try:
            forwarded.append(ipaddress.ip_address(value.strip().split("%", 1)[0]))
        except ValueError:
            continue
    for candidate in reversed(forwarded):
        if not any(candidate in network for network in trusted_proxies):
            return str(candidate)
    return str(forwarded[0] if forwarded else peer)


def portal_middleware(allowed_networks: tuple[str, ...], trusted_proxies: tuple[str, ...],
                      auth_manager: AuthManager):
    networks = tuple(ipaddress.ip_network(value) for value in allowed_networks)
    proxy_networks = tuple(ipaddress.ip_network(value) for value in trusted_proxies)
    public = {
        ("GET", "/login"),
        ("GET", "/assets/auth.js"),
        ("GET", "/assets/app.css"),
        ("POST", "/api/auth/login"),
    }
    setup_routes = {
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/account"),
        ("POST", "/api/auth/logout"),
    }

    def security_headers(response: web.StreamResponse) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if auth_manager.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"

    @web.middleware
    async def hardened(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            security_headers(exc)
            raise
        security_headers(response)
        return response

    @web.middleware
    async def lan_only(request: web.Request, handler: Any) -> web.StreamResponse:
        client = _request_client(request, proxy_networks)
        request["graber_client"] = client
        try:
            address = ipaddress.ip_address(client)
        except ValueError:
            return web.json_response({"error": "client is outside allowed networks"}, status=403)
        if not any(address in network for network in networks):
            return web.json_response({"error": "client is outside allowed networks"}, status=403)
        return await handler(request)

    def unauthorized(request: web.Request, *, setup_required: bool = False) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            payload = {"error": "authentication required"}
            if setup_required:
                payload = {"error": "owner account setup required", "setup_required": True}
            return web.json_response(payload, status=403 if setup_required else 401)
        raise web.HTTPSeeOther("/login")

    @web.middleware
    async def authenticated(request: web.Request, handler: Any) -> web.StreamResponse:
        route = (request.method, request.path)
        if route in public:
            return await handler(request)
        token = request.cookies.get(auth_manager.cookie_name)
        session = auth_manager.get_session(token)
        if session is None:
            return unauthorized(request)
        request["graber_session"] = session
        request["graber_session_token"] = token
        if session.setup_required and route not in setup_routes:
            return unauthorized(request, setup_required=True)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session.csrf_token):
                return web.json_response({"error": "invalid CSRF token"}, status=403)
        return await handler(request)

    return [hardened, lan_only, authenticated]
