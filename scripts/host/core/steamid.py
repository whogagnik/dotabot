from __future__ import annotations

import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Mapping
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from steam.client import SteamClient
from steam.enums import EResult

STEAMID64_BASE = 76561197960265728
STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
_OPENID_CLAIMED_ID_RE = re.compile(
    r"^https?://steamcommunity\.com/openid/id/(\d{17})/?$"
)


class SteamCredentialsError(RuntimeError):
    """Steam rejected credentials or did not return an account SteamID."""


def get_steamid3(username: str, password: str):
    steamid64 = get_steamid64(username, password)
    return steamid64_to_steamid3(steamid64)

def steamid64_to_steamid3(steamid64: str | int) -> str:
    steamid64 = int(steamid64)
    account_id = steamid64 - STEAMID64_BASE

    if account_id <= 0:
        raise ValueError("Некорректный steamid64")
    return str(account_id)
def get_steamid64(username: str, password: str) -> str:
    """Return SteamID64 through Steam's current credentials-auth endpoint.

    The endpoint returns the account's SteamID as soon as it has verified the
    encrypted password.  We deliberately do not poll for a token: a token is
    unnecessary for this lookup and may require Steam Guard confirmation.
    """
    if not username or not password:
        raise ValueError("Steam username and password are required")

    # qr_loger already contains the current RSA-password implementation used by
    # the host's normal Steam authentication flow.  Reusing it keeps both flows
    # on the same Steam API and avoids the obsolete CM-client login.
    from scripts.host.core import qr_loger

    modulus, exponent, timestamp = qr_loger._get_password_rsa_key_json(username)
    encrypted_password = qr_loger._rsa_encrypt_password(
        password, modulus, exponent
    )
    payload = {
        "account_name": username,
        "encrypted_password": encrypted_password,
        "encryption_timestamp": str(timestamp),
        "remember_login": True,
        "persistence": 1,
        "platform_type": 2,
        "website_id": "Community",
        "device_friendly_name": "dotabot-steamid-lookup",
    }

    endpoint = qr_loger.BASE_URL + "BeginAuthSessionViaCredentials/v1/"

    # Steam intermittently answers x-eresult=5 to the JSON representation of
    # this endpoint.  qr_loger's production auth flow therefore retries the
    # exact same request as form-urlencoded; the ID lookup must do so as well.
    response = qr_loger.http_post_json(endpoint, payload)

    def response_details(candidate):
        try:
            return candidate.json().get("response") or {}
        except (AttributeError, ValueError):
            return {}

    details = response_details(response)
    if not details.get("steamid"):
        form_payload = {
            key: (str(value).lower() if isinstance(value, bool) else str(value))
            for key, value in payload.items()
        }
        form_response = qr_loger.http_post_form(endpoint, form_payload, accept_json=True)
        form_details = response_details(form_response)
        if form_response.ok or form_details:
            response, details = form_response, form_details

    steamid64 = details.get("steamid")
    if response.ok and steamid64:
        steamid64 = str(steamid64)
        steamid64_to_steamid3(steamid64)
        return steamid64

    error = str(details.get("extended_error_message") or "").strip()
    result = response.headers.get("x-eresult") or str(response.status_code)
    suffix = f": {error}" if error else ""
    raise SteamCredentialsError(f"Steam credentials lookup failed ({result}){suffix}")


def get_steamid64_legacy_cm(username: str, password: str) -> str:
    """Legacy CM implementation, retained only for diagnostics/compatibility."""
    client = SteamClient()

    result = client.cli_login(username=username, password=password)

    if result != EResult.OK:
        client.logout()
        raise RuntimeError(f"Login failed: {result}")

    steamid64 = str(int(client.steam_id))

    client.logout()

    return steamid64


def steam_openid_login_url(return_to: str) -> str:
    """Build a Steam OpenID login URL for a local callback URL."""
    parsed_return_to = urlparse(return_to)
    if parsed_return_to.scheme != "http" or not parsed_return_to.netloc:
        raise ValueError("OpenID return_to must be an absolute HTTP URL")
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": f"{parsed_return_to.scheme}://{parsed_return_to.netloc}/",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID_ENDPOINT}?{urlencode(params)}"


def _steamid64_from_claimed_id(claimed_id: str) -> str:
    match = _OPENID_CLAIMED_ID_RE.fullmatch(str(claimed_id))
    if match is None:
        raise ValueError("Steam OpenID response has an invalid claimed ID")

    steamid64 = match.group(1)
    # Also validates that the value belongs to an individual Steam account.
    steamid64_to_steamid3(steamid64)
    return steamid64


def _verify_steam_openid_response(params: Mapping[str, str]) -> str:
    """Verify Steam's OpenID assertion and return the authenticated SteamID64."""
    signed = {
        str(key): str(value)
        for key, value in params.items()
        if str(key).startswith("openid.")
    }
    if signed.get("openid.mode") != "id_res":
        raise ValueError("Steam OpenID login was cancelled or did not complete")

    verification = dict(signed)
    verification["openid.mode"] = "check_authentication"
    response = requests.post(
        STEAM_OPENID_ENDPOINT,
        data=verification,
        timeout=15,
    )
    response.raise_for_status()
    if "is_valid:true" not in response.text.replace(" ", "").lower():
        raise ValueError("Steam rejected the OpenID response signature")

    return _steamid64_from_claimed_id(signed.get("openid.claimed_id", ""))


def get_steamid64_openid(*, timeout_sec: float = 300.0, open_browser: bool = True) -> str:
    """Get a SteamID64 through a one-time, browser-based Steam OpenID login.

    No account password is passed to this application. Steam and Steam Guard are
    handled on Steam's own login page. The function returns only after the
    signed callback has been validated with Steam.
    """
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")

    token = secrets.token_urlsafe(24)
    received: dict[str, object] = {}
    completed = threading.Event()

    class OpenIdCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTPServer callback name
            parsed = urlparse(self.path)
            if parsed.path != f"/{token}":
                self.send_error(404)
                return

            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                received["steamid64"] = _verify_steam_openid_response(query)
            except Exception as error:
                received["error"] = error
            finally:
                completed.set()

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "error" in received:
                body = "<h2>Steam login was not accepted. You can close this tab.</h2>"
            else:
                body = "<h2>Steam account linked. You can close this tab.</h2>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), OpenIdCallbackHandler)
    port = int(server.server_port)
    return_to = f"http://127.0.0.1:{port}/{token}"
    login_url = steam_openid_login_url(return_to)

    listener = threading.Thread(target=server.handle_request, daemon=True)
    listener.start()
    try:
        if open_browser:
            webbrowser.open(login_url)
        else:
            print(login_url)

        if not completed.wait(timeout_sec):
            raise TimeoutError("Steam OpenID login timed out")
    finally:
        server.server_close()

    error = received.get("error")
    if isinstance(error, Exception):
        raise error
    steamid64 = received.get("steamid64")
    if not isinstance(steamid64, str):
        raise RuntimeError("Steam OpenID callback returned no SteamID")
    return steamid64


def get_steamid3_openid(*, timeout_sec: float = 300.0, open_browser: bool = True) -> str:
    """Browser/OpenID variant of :func:`get_steamid3`."""
    return steamid64_to_steamid3(
        get_steamid64_openid(timeout_sec=timeout_sec, open_browser=open_browser)
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Get SteamID64")
    parser.add_argument(
        "--openid",
        action="store_true",
        help="get the ID through a browser-based Steam OpenID login",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="OpenID login timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the OpenID URL instead of opening the browser",
    )
    args = parser.parse_args()

    if args.openid:
        print(
            get_steamid64_openid(
                timeout_sec=args.timeout,
                open_browser=not args.print_url,
            )
        )
    else:
        login = "xxrcs99219"
        password = "fqqdw70662"
        print(get_steamid64(login, password))
