"""Spotify authorization-code flow.

Scopes stay in development mode. The redirect is localhost. Tokens are encrypted
before they are written to ``users``.
"""

from urllib.parse import urlencode

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES: tuple[str, ...] = (
    "user-read-private",
    "user-read-recently-played",
    "user-top-read",
    "user-library-read",
    "playlist-modify-private",
)


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


def run_oauth() -> None:
    """Bind the localhost redirect, exchange the code, and store the refresh token."""
    raise NotImplementedError(
        "Spotify OAuth is not implemented. "
        "Bind the localhost redirect, exchange the code, and store the encrypted refresh token."
    )


def refresh_access_token() -> None:
    """Exchange the stored refresh token. Spotify rotates it; persist the new one."""
    raise NotImplementedError("Spotify token refresh is not implemented.")
