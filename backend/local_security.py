"""Security helpers for a localhost-only paper reader."""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit


class UnsafeRemoteURLError(ValueError):
    """Raised when a remote URL could reach a non-public network."""


class DownloadTooLargeError(ValueError):
    """Raised when a remote response exceeds the configured byte limit."""


Resolver = Callable[..., list[tuple[object, ...]]]


def validate_remote_url(url: str, resolver: Resolver | None = None) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRemoteURLError("Only http:// and https:// URLs are allowed.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeRemoteURLError("Remote URL must contain a hostname and no credentials.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeRemoteURLError("Localhost URLs are not allowed.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolver = resolver or socket.getaddrinfo
    try:
        addresses = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise UnsafeRemoteURLError(f"Could not resolve remote host: {hostname}") from error
    if not addresses:
        raise UnsafeRemoteURLError(f"Could not resolve remote host: {hostname}")
    for address in addresses:
        raw_ip = str(address[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as error:
            raise UnsafeRemoteURLError("Remote host resolved to an invalid address.") from error
        if not ip.is_global:
            raise UnsafeRemoteURLError("Remote host resolves to a private or reserved address.")
    return parsed.geturl()


class ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        validated = validate_remote_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, validated)


def read_limited(response, max_bytes: int, chunk_size: int = 1024 * 1024) -> bytes:  # noqa: ANN001
    length = response.headers.get("Content-Length")
    if length:
        try:
            declared_length = int(length)
        except ValueError:
            declared_length = 0
        if declared_length > max_bytes:
            raise DownloadTooLargeError(f"Remote file exceeds the {max_bytes} byte limit.")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DownloadTooLargeError(f"Remote file exceeds the {max_bytes} byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def download_remote_bytes(
    url: str,
    *,
    accept: str,
    max_bytes: int,
    timeout: float,
    retries: int = 2,
) -> bytes:
    validated = validate_remote_url(url)
    opener = urllib.request.build_opener(ValidatingRedirectHandler())
    request = urllib.request.Request(
        validated,
        headers={"Accept": accept, "User-Agent": "PaperParallelReader/1.0"},
    )
    last_error: Exception | None = None
    attempts = 0
    deadline = time.monotonic() + timeout
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            with opener.open(request, timeout=remaining) as response:
                validate_remote_url(response.geturl())
                return read_limited(response, max_bytes)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                delay = min(2**attempt, 2, max(0, deadline - time.monotonic()))
                if delay <= 0:
                    break
                time.sleep(delay)
    raise RuntimeError(f"Remote download failed after {attempts} attempts: {last_error}") from last_error
