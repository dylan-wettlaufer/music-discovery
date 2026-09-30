"""Spotify authorization-code flow.

Scopes stay in development mode. The redirect is localhost. Tokens are encrypted
before they are written to ``users``.
"""

import html
import re
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from music_discovery.config import Settings, get_settings
from music_discovery.crypto import decrypt_token, encrypt_token
from music_discovery.db import session_scope
from music_discovery.models import User

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PROFILE_URL = "https://api.spotify.com/v1/me"

SCOPES: tuple[str, ...] = (
    "user-read-private",
    "user-read-recently-played",
    "user-top-read",
    "user-library-read",
    "playlist-modify-private",
)

_SIGN_IN_TIMEOUT_SECONDS = 180.0
_ACCESS_TOKEN_SKEW = timedelta(minutes=1)
_SAFE_ERROR = re.compile(r"[a-z0-9_]+")
SessionFactory = Callable[[], AbstractContextManager[Session]]


class AuthError(Exception):
    """The user can fix this by checking .env, Postgres, or the Spotify app."""


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str | None = None
    scope: str = ""


class SpotifyProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    country: str | None = None
    display_name: str | None = None


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def run_oauth(
    *,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    session_factory: SessionFactory | None = None,
    open_url: Callable[[str], None] | None = None,
    receive_code: Callable[[str], str] | None = None,
    echo: Callable[[str], None] | None = None,
) -> None:
    """Bind the localhost redirect, exchange the code, and store the refresh token.

    Run this on the host. The browser redirects to 127.0.0.1, which is not the
    app container.
    """
    settings = settings or get_settings()
    echo = echo or print
    _require_credentials(settings)
    _require_database(session_factory)

    state = secrets.token_urlsafe(32)
    url = build_authorize_url(
        client_id=settings.spotify_client_id,
        redirect_uri=settings.spotify_redirect_uri,
        state=state,
    )

    def announce() -> None:
        if open_url is None:
            _open_browser(url, echo)
        else:
            open_url(url)

    if receive_code is None:
        code = wait_for_authorization_code(
            redirect_uri=settings.spotify_redirect_uri,
            state=state,
            on_ready=announce,
        )
    else:
        announce()
        code = receive_code(state)

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=30)
    try:
        tokens = exchange_authorization_code(
            client,
            code=code,
            redirect_uri=settings.spotify_redirect_uri,
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
        )
        if not tokens.refresh_token:
            raise AuthError("Spotify did not return a refresh token.")
        profile = fetch_profile(client, tokens.access_token)
    finally:
        if owns_client:
            client.close()

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in)
    try:
        with _sessions(session_factory) as session:
            save_connected_user(
                session,
                profile=profile,
                refresh_token=tokens.refresh_token,
                access_token=tokens.access_token,
                expires_at=expires_at,
                encryption_key=settings.token_encryption_key,
            )
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc

    if profile.display_name:
        echo(f"Signed in as {profile.display_name} ({profile.id}).")
    else:
        echo(f"Signed in as {profile.id}.")


def refresh_access_token(
    *,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    session_factory: SessionFactory | None = None,
) -> None:
    """Exchange the stored refresh token. Spotify rotates it; persist the new one."""
    settings = settings or get_settings()
    _require_credentials(settings)
    try:
        with _sessions(session_factory) as session:
            user = session.scalar(select(User).order_by(User.id).limit(1))
            if user is None:
                raise AuthError("Sign in with `music-discovery auth` before refreshing.")
            user_id = user.id
            try:
                refresh_token = decrypt_token(
                    user.refresh_token_encrypted,
                    settings.token_encryption_key,
                )
            except ValueError as exc:
                raise AuthError("Stored refresh token could not be decrypted. Sign in again.") from exc
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=30)
    try:
        tokens = exchange_refresh_token(
            client,
            refresh_token=refresh_token,
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
        )
    finally:
        if owns_client:
            client.close()

    try:
        with _sessions(session_factory) as session:
            user = session.get(User, user_id)
            if user is None:
                raise AuthError("Sign in with `music-discovery auth` before refreshing.")
            _store_tokens(
                user,
                refresh_token=tokens.refresh_token or refresh_token,
                access_token=tokens.access_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in),
                encryption_key=settings.token_encryption_key,
            )
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc


def load_access_token(
    *,
    settings: Settings | None = None,
    http_client: httpx.Client | None = None,
    session_factory: SessionFactory | None = None,
    force_refresh: bool = False,
) -> str:
    """Return a usable access token, refreshing when it expires within a minute."""
    settings = settings or get_settings()
    _require_credentials(settings)
    if force_refresh or _token_needs_refresh(session_factory):
        refresh_access_token(
            settings=settings,
            http_client=http_client,
            session_factory=session_factory,
        )
    return _decrypt_access_token(settings, session_factory)


def exchange_authorization_code(
    client: httpx.Client,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> TokenResponse:
    return _request_token(
        client,
        client_id=client_id,
        client_secret=client_secret,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


def exchange_refresh_token(
    client: httpx.Client,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> TokenResponse:
    return _request_token(
        client,
        client_id=client_id,
        client_secret=client_secret,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def fetch_profile(client: httpx.Client, access_token: str) -> SpotifyProfile:
    response = client.get(
        PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code != 200:
        raise AuthError(f"Spotify profile request failed ({response.status_code}).")
    try:
        return SpotifyProfile.model_validate(response.json())
    except ValidationError as exc:
        raise AuthError("Spotify returned an unexpected profile.") from exc


def wait_for_authorization_code(
    *,
    redirect_uri: str,
    state: str,
    timeout: float = _SIGN_IN_TIMEOUT_SECONDS,
    on_ready: Callable[[], None] | None = None,
) -> str:
    """Accept one localhost redirect and return the authorization code."""
    host, port, path = _redirect_endpoint(redirect_uri)
    holder = _Callback()
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != path:
                _send_html(self, 404, "Not found.")
                return
            params = parse_qs(parsed.query)
            got_state = params.get("state", [""])[0]
            if not secrets.compare_digest(got_state, state):
                _send_html(self, 400, "Sign-in failed. Close this tab and run auth again.")
                holder.error = "Spotify sign-in failed: state did not match."
                done.set()
                return
            error = params.get("error", [""])[0]
            if error:
                safe = error if _SAFE_ERROR.fullmatch(error) else "unknown"
                _send_html(self, 400, "Spotify did not grant access. You can close this tab.")
                holder.error = f"Spotify sign-in failed ({safe})."
                done.set()
                return
            code = params.get("code", [""])[0]
            if not code:
                _send_html(self, 400, "Spotify did not return a code. You can close this tab.")
                holder.error = "Spotify sign-in failed: missing code."
                done.set()
                return
            _send_html(self, 200, "Signed in. You can close this tab and return to the terminal.")
            holder.code = code
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = _CallbackServer((host, port), Handler)
    except OSError as exc:
        raise AuthError(
            f"Could not listen on {host}:{port}. "
            "Stop the other process or change SPOTIFY_REDIRECT_URI."
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        _wait_until_listening(host, port)
        if on_ready is not None:
            on_ready()
        if not done.wait(timeout):
            raise AuthError("Timed out waiting for Spotify sign-in.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if holder.error is not None:
        raise AuthError(holder.error)
    if holder.code is None:
        raise AuthError("Spotify sign-in failed: missing code.")
    return holder.code


def save_connected_user(
    session: Session,
    *,
    profile: SpotifyProfile,
    refresh_token: str,
    access_token: str,
    expires_at: datetime,
    encryption_key: str,
) -> User:
    """Insert the single v1 user, or refresh tokens on a later sign-in."""
    user = session.scalar(select(User).where(User.spotify_user_id == profile.id))
    if user is None:
        user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        user = User(spotify_user_id=profile.id, refresh_token_encrypted="")
        session.add(user)
    user.spotify_user_id = profile.id
    user.country = _country(profile.country)
    _store_tokens(
        user,
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=expires_at,
        encryption_key=encryption_key,
    )
    return user


def _request_token(
    client: httpx.Client,
    *,
    client_id: str,
    client_secret: str,
    data: dict[str, str],
) -> TokenResponse:
    response = client.post(
        TOKEN_URL,
        data=data,
        auth=(client_id, client_secret),
    )
    if response.status_code != 200:
        detail = _spotify_error(response)
        if data.get("grant_type") == "refresh_token" and _oauth_error(response) == "invalid_grant":
            raise AuthError(
                f"Spotify revoked access: {detail}. Sign in with `music-discovery auth` again."
            )
        raise AuthError(f"Spotify token request failed ({response.status_code}): {detail}")
    try:
        return TokenResponse.model_validate(response.json())
    except ValidationError as exc:
        raise AuthError("Spotify returned an unexpected token response.") from exc


def _store_tokens(
    user: User,
    *,
    refresh_token: str,
    access_token: str,
    expires_at: datetime,
    encryption_key: str,
) -> None:
    user.refresh_token_encrypted = encrypt_token(refresh_token, encryption_key)
    user.access_token_encrypted = encrypt_token(access_token, encryption_key)
    user.access_token_expires_at = expires_at


def _token_needs_refresh(session_factory: SessionFactory | None) -> bool:
    try:
        with _sessions(session_factory) as session:
            user = session.scalar(select(User).order_by(User.id).limit(1))
            if user is None:
                raise AuthError("Sign in with `music-discovery auth` before polling.")
            expires_at = user.access_token_expires_at
            has_token = bool(user.access_token_encrypted)
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc
    if not has_token or expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc) + _ACCESS_TOKEN_SKEW


def _decrypt_access_token(settings: Settings, session_factory: SessionFactory | None) -> str:
    try:
        with _sessions(session_factory) as session:
            user = session.scalar(select(User).order_by(User.id).limit(1))
            if user is None or not user.access_token_encrypted:
                raise AuthError("Sign in with `music-discovery auth` before polling.")
            try:
                return decrypt_token(user.access_token_encrypted, settings.token_encryption_key)
            except ValueError as exc:
                raise AuthError("Stored access token could not be decrypted. Sign in again.") from exc
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc


def _require_credentials(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", settings.spotify_client_id),
            ("SPOTIFY_CLIENT_SECRET", settings.spotify_client_secret),
            ("TOKEN_ENCRYPTION_KEY", settings.token_encryption_key),
        )
        if not value
    ]
    if missing:
        raise AuthError(f"Set {', '.join(missing)} in .env before signing in.")
    try:
        Fernet(settings.token_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise AuthError("TOKEN_ENCRYPTION_KEY must be a Fernet key.") from exc


def _require_database(session_factory: SessionFactory | None) -> None:
    try:
        with _sessions(session_factory) as session:
            session.execute(text("SELECT 1"))
    except OperationalError as exc:
        raise AuthError(f"Database unavailable: {exc.orig}") from exc


def _redirect_endpoint(redirect_uri: str) -> tuple[str, int, str]:
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        raise AuthError(
            "SPOTIFY_REDIRECT_URI must be http://127.0.0.1:<port>/<path>, "
            "and that URI must be allowlisted on the Spotify app."
        )
    return "127.0.0.1", parts.port, parts.path or "/"


def _country(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return None


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return ""


def _spotify_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "request rejected"
    if not isinstance(body, dict):
        return "request rejected"
    description = body.get("error_description") or body.get("error")
    if isinstance(description, str) and description:
        return description
    return "request rejected"


def _open_browser(url: str, echo: Callable[[str], None]) -> None:
    echo("Opening a browser to sign in with Spotify.")
    echo(url)
    webbrowser.open(url)


def _wait_until_listening(host: str, port: int) -> None:
    deadline = datetime.now(timezone.utc).timestamp() + 2
    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise AuthError(f"Could not listen on {host}:{port}.")


@contextmanager
def _sessions(session_factory: SessionFactory | None) -> Iterator[Session]:
    factory = session_factory or session_scope
    with factory() as session:
        yield session


def _send_html(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    body = f"<!DOCTYPE html><html><body><p>{html.escape(message)}</p></body></html>".encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


class _Callback:
    code: str | None = None
    error: str | None = None


class _CallbackServer(ThreadingHTTPServer):
    allow_reuse_address = True
