"""BitgetAuth — Bitget V2 unified HMAC-SHA256 request signing.

Centralized and unit-tested against the algorithm (bybit-listener's ``BybitAuth``
discipline) so no adapter method hand-rolls a signature.

Algorithm (VERIFIED against live 40006/00000 responses via ``bitget_api_probe.py``):

    timestamp = str(int(time.time() * 1000))                 # ms, string
    prehash   = timestamp + METHOD + requestPath + body      # body "" for GET
    sign      = base64(hmac_sha256(secret, prehash))         # BASE64, not hex

``requestPath`` MUST include the query string exactly as sent (``/api/v2/mix/
account/accounts?productType=USDT-FUTURES``); for GET the body stays empty (the
query is NOT appended a second time).

Header names are ``ACCESS-KEY`` / ``ACCESS-SIGN`` / ``ACCESS-TIMESTAMP`` /
``ACCESS-PASSPHRASE`` — the analysis doc's ``ACcess-Key`` spelling was a typo.

**Passphrase:** sent as the RAW passphrase, NOT pre-signed. The analysis doc
claimed ``base64(hmac(secret, passphrase))``; that is the OKX-style variant and is
wrong for Bitget V2. ``passphrase_prehashed=True`` is available as an escape hatch.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class BitgetAuth:
    """Signs Bitget V2 REST requests and builds the WS ``login`` argument."""

    def __init__(self, key: str, secret: str, passphrase: str,
                 passphrase_prehashed: bool = False) -> None:
        if not key or not secret or not passphrase:
            raise ValueError("BitgetAuth requires key, secret and passphrase")
        self.key = key
        self._secret = secret
        self._passphrase_raw = passphrase
        self.passphrase = (self._sign(passphrase) if passphrase_prehashed else passphrase)

    # ------------------------------------------------------------------ signing
    def _sign(self, payload: str) -> str:
        """base64(hmac_sha256(secret, payload)) — the single signing primitive."""
        return base64.b64encode(
            hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).digest()
        ).decode()

    @staticmethod
    def timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def prehash(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Build the exact string that gets signed (unit-tested)."""
        return f"{timestamp}{method.upper()}{request_path}{body}"

    def sign_request(self, method: str, request_path: str, body: str = "",
                     timestamp: str | None = None) -> tuple[str, str]:
        """Return ``(signature, timestamp)`` for one request."""
        ts = timestamp or self.timestamp_ms()
        return self._sign(self.prehash(ts, method, request_path, body)), ts

    def headers(self, method: str, request_path: str, body: str = "",
                timestamp: str | None = None) -> dict[str, str]:
        """Full signed header set. ``request_path`` must include the query string."""
        sign, ts = self.sign_request(method, request_path, body, timestamp)
        return {
            "ACCESS-KEY": self.key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    # ----------------------------------------------------------------------- ws
    def ws_login_arg(self, timestamp: str | None = None) -> dict[str, str]:
        """One ``args`` entry for the WS ``{"op":"login"}`` frame.

        The WS prehash uses SECONDS (not ms) with method ``GET`` and the fixed
        path ``/user/verify``.
        """
        ts = timestamp or str(int(time.time()))
        return {
            "apiKey": self.key,
            "passphrase": self.passphrase,
            "timestamp": ts,
            "sign": self._sign(self.prehash(ts, "GET", "/user/verify")),
        }

    def __repr__(self) -> str:  # never leak the secret
        return f"BitgetAuth(key={self.key[:4]}...{'*' * 6})"
