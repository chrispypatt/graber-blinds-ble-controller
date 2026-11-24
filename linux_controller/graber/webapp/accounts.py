"""Handle portal login, account setup, password changes, and session cookies."""

from __future__ import annotations

import asyncio
import json
import time
from email.utils import formatdate
from typing import Mapping

from aiohttp import web
from graber.security import AuthError, AuthManager, MAXIMUM_PASSWORD_LENGTH, Session
from . import ASSET_ROOT


def session_payload(session: Session) -> dict[str, object]:
    return {
        "authenticated": True,
        "username": session.username,
        "role": session.role,
        "csrf_token": session.csrf_token,
        "setup_required": session.setup_required,
    }


async def auth_javascript(_request: web.Request) -> web.Response:
    value = (ASSET_ROOT / "static" / "auth.js").read_text(encoding="utf-8")
    return web.Response(text=value, content_type="application/javascript")


async def stylesheet(_request: web.Request) -> web.Response:
    value = (ASSET_ROOT / "static" / "app.css").read_text(encoding="utf-8")
    return web.Response(text=value, content_type="text/css")


class AccountRoutes:
    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth = auth_manager
        self.password_checks = asyncio.Semaphore(2)

    def set_session_cookie(self, response: web.StreamResponse, token: str) -> None:
        response.set_cookie(
            self.auth.cookie_name, token,
            max_age=int(self.auth.session_ttl_s),
            expires=formatdate(time.time() + self.auth.session_ttl_s, usegmt=True), path="/",
            secure=self.auth.secure_cookie, httponly=True, samesite="Strict",
        )

    def clear_session_cookie(self, response: web.StreamResponse) -> None:
        response.set_cookie(
            self.auth.cookie_name, "", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT", path="/",
            secure=self.auth.secure_cookie, httponly=True, samesite="Strict",
        )

    async def login_page(self, request: web.Request) -> web.Response:
        session = self.auth.get_session(request.cookies.get(self.auth.cookie_name))
        if session is not None and not session.setup_required:
            raise web.HTTPSeeOther("/")
        template = (ASSET_ROOT / "templates" / "auth.html").read_text(encoding="utf-8")
        return web.Response(text=template, content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        retry_after = self.auth.limiter.retry_after(request["graber_client"])
        if retry_after:
            return web.json_response(
                {"error": "too many login attempts"}, status=429,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        if not isinstance(body, Mapping):
            return web.json_response({"error": "invalid request"}, status=400)
        username, password = str(body.get("username", "")), str(body.get("password", ""))
        if not 1 <= len(username) <= 64 or not 1 <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
            return web.json_response({"error": "invalid username or password"}, status=401)
        try:
            await asyncio.wait_for(self.password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            result = await asyncio.to_thread(
                self.auth.login, request["graber_client"], username, password
            )
        finally:
            self.password_checks.release()
        if result is None:
            return web.json_response({"error": "invalid username or password"}, status=401)
        token, session = result
        response = web.json_response(session_payload(session))
        self.set_session_cookie(response, token)
        return response

    async def auth_session(self, request: web.Request) -> web.Response:
        return web.json_response(session_payload(request["graber_session"]))

    async def logout(self, request: web.Request) -> web.Response:
        await asyncio.to_thread(self.auth.sessions.remove, request["graber_session_token"])
        response = web.json_response({"authenticated": False})
        self.clear_session_cookie(response)
        return response

    async def change_password(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                raise TypeError
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        current_password = body.get("current_password")
        new_password = str(body.get("new_password", ""))
        if current_password is not None:
            current_password = str(current_password)
        if len(new_password) > MAXIMUM_PASSWORD_LENGTH or (
            current_password is not None and len(current_password) > MAXIMUM_PASSWORD_LENGTH
        ):
            return web.json_response({"error": "invalid password"}, status=400)
        try:
            await asyncio.wait_for(self.password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            token, session = await asyncio.to_thread(
                self.auth.change_password, request["graber_session"],
                current_password, new_password,
            )
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            self.password_checks.release()
        response = web.json_response(session_payload(session))
        self.set_session_cookie(response, token)
        return response

    async def configure_account(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                raise TypeError
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid request"}, status=400)
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if not 1 <= len(username) <= 64 or len(password) > MAXIMUM_PASSWORD_LENGTH:
            return web.json_response({"error": "invalid account"}, status=400)
        try:
            await asyncio.wait_for(self.password_checks.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return web.json_response({"error": "authentication is busy"}, status=503)
        try:
            token, session = await asyncio.to_thread(
                self.auth.configure_owner, request["graber_session"], username, password
            )
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            self.password_checks.release()
        response = web.json_response(session_payload(session))
        self.set_session_cookie(response, token)
        return response

    def register(self, app: web.Application) -> None:
        app.router.add_get("/login", self.login_page)
        app.router.add_get("/assets/auth.js", auth_javascript)
        app.router.add_get("/assets/app.css", stylesheet)
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_get("/api/auth/session", self.auth_session)
        app.router.add_post("/api/auth/logout", self.logout)
        app.router.add_post("/api/auth/password", self.change_password)
        app.router.add_post("/api/auth/account", self.configure_account)
