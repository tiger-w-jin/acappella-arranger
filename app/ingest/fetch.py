"""Fetch a media file the user points at, without becoming a probe for the network.

A server that will fetch any URL a caller supplies is a server-side request
forgery tool. This one is reachable on a workstation's network interface, so
anyone who can reach it could otherwise use it to reach things they cannot:
loopback services, cloud metadata endpoints, hosts inside a corporate network.
That is the risk this module exists to contain, and it is the reason for nearly
every rule below.

What it will fetch: a plain http(s) URL that resolves to a public address and
returns an audio or video file. Your own hosting, a public-domain archive, a
Creative Commons recording.

What it will not: anything off a streaming platform. Extracting audio from
YouTube, Spotify and the like breaks their terms and, for commercial music,
copyright. Those hosts are named so the refusal explains itself instead of
looking like a bug.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .audio import AUDIO_SUFFIXES, VIDEO_SUFFIXES
from .score import SCORE_SUFFIXES

MAX_FETCH_BYTES = 40 * 1024 * 1024
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_REDIRECTS = 4

ALLOWED_SCHEMES = {"http", "https"}

# Platforms whose audio is not ours to take. Naming them turns a confusing
# failure into an explanation.
STREAMING_HOSTS = {
    "youtube.com", "youtu.be", "music.youtube.com", "m.youtube.com",
    "spotify.com", "open.spotify.com",
    "soundcloud.com", "music.apple.com", "itunes.apple.com",
    "tidal.com", "deezer.com", "pandora.com", "audiomack.com",
    "bilibili.com", "music.163.com", "y.qq.com", "kugou.com", "kkbox.com",
    "vimeo.com", "dailymotion.com", "tiktok.com", "douyin.com",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "twitch.tv",
    "netflix.com", "bandcamp.com", "mixcloud.com",
}

MEDIA_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES | SCORE_SUFFIXES

_MEDIA_CONTENT_TYPES = ("audio/", "video/", "application/octet-stream")
_SCORE_CONTENT_TYPES = ("xml", "musicxml", "midi", "application/zip")


class FetchError(ValueError):
    """Anything that stops a fetch, phrased for the person who typed the URL."""


@dataclass
class Fetched:
    path: str
    filename: str
    content_type: str
    size: int


def _registered_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _is_streaming_host(host: str) -> bool:
    host = host.lower().strip(".")
    return host in STREAMING_HOSTS or _registered_domain(host) in STREAMING_HOSTS


def _addresses_for(host: str) -> list[ipaddress._BaseAddress]:
    """Every address the host resolves to, so none of them can be a surprise."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise FetchError(f"Could not resolve “{host}”.") from error

    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not found:
        raise FetchError(f"Could not resolve “{host}”.")
    return found


def _reject_private(host: str) -> None:
    """Refuse anything that is not a public address.

    Every address the name resolves to is checked, not just the first: a name
    with both a public and a loopback record would otherwise slip through.
    """
    for address in _addresses_for(host):
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise FetchError(
                f"“{host}” resolves to {address}, which is a private or internal "
                "address. Only public addresses can be fetched."
            )
        # 169.254.169.254 and friends are covered by is_global, but call the
        # cloud metadata case out because it is the one that matters most.
        if str(address).startswith("169.254."):
            raise FetchError("That address is link-local and cannot be fetched.")


def validate_url(url: str) -> str:
    """Check a URL is fetchable, or say precisely why it is not."""
    url = (url or "").strip()
    if not url:
        raise FetchError("Enter a link first.")
    if len(url) > 2048:
        raise FetchError("That link is too long.")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError(
            f"Only http and https links can be fetched, not “{parsed.scheme or 'that'}”."
        )
    host = parsed.hostname
    if not host:
        raise FetchError("That does not look like a complete link.")

    if _is_streaming_host(host):
        raise FetchError(
            f"“{host}” is a streaming platform, and pulling audio out of one breaks "
            "its terms of use — and, for commercial music, copyright. Link to a file "
            "you host or have the rights to, or upload it directly. Recording the "
            "melody yourself works well and is the app's best input anyway."
        )

    _reject_private(host)
    return url


def _looks_like_media(url: str, content_type: str) -> bool:
    """Judge on the URL's own suffix and the declared type, nothing derived.

    Deciding this from a filename that has already had a fallback extension
    attached is circular: the fallback would make every response look like
    media, which is how an HTML error page got through.
    """
    path_name = unquote(Path(urlparse(url).path).name).lower()
    if any(path_name.endswith(suffix) for suffix in MEDIA_SUFFIXES):
        return True

    kind = (content_type or "").lower()
    if not kind:
        return False
    if kind.startswith(("text/", "application/json", "application/xhtml")):
        return False
    return kind.startswith(_MEDIA_CONTENT_TYPES) or any(t in kind for t in _SCORE_CONTENT_TYPES)


def _filename_from(url: str, content_type: str) -> str:
    name = unquote(Path(urlparse(url).path).name) or "download"
    name = re.sub(r"[^\w.\- ]", "_", name)[:120]
    if any(name.lower().endswith(s) for s in MEDIA_SUFFIXES):
        return name

    kind = (content_type or "").lower()
    for marker, suffix in (
        ("mpeg", ".mp3"), ("wav", ".wav"), ("flac", ".flac"), ("ogg", ".ogg"),
        ("mp4", ".mp4"), ("webm", ".webm"), ("quicktime", ".mov"),
        ("midi", ".mid"), ("xml", ".musicxml"),
    ):
        if marker in kind:
            return f"{name}{suffix}"
    return f"{name}.mp3"


def fetch_media(url: str) -> Fetched:
    """Download a media file to a temp path. The caller owns and removes it."""
    import httpx

    current = validate_url(url)
    seen: list[str] = []

    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        headers={"User-Agent": "acappella-arranger/1.0"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            seen.append(current)
            try:
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("That link redirected to nowhere.")
                        # Re-validate every hop: a public URL redirecting inward
                        # is the ordinary way an SSRF filter gets bypassed.
                        current = validate_url(str(response.url.join(location)))
                        continue

                    if response.status_code >= 400:
                        raise FetchError(
                            f"That link returned {response.status_code} "
                            f"{response.reason_phrase or ''}".strip() + "."
                        )

                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_FETCH_BYTES:
                        raise FetchError(
                            f"That file is larger than {MAX_FETCH_BYTES // (1024 * 1024)} MB."
                        )

                    # Judged before a filename is derived, so the fallback
                    # extension cannot vouch for the content.
                    if not _looks_like_media(current, content_type):
                        raise FetchError(
                            f"That link returned “{content_type or 'unknown content'}”, "
                            "which is not audio, video or a score file."
                        )
                    filename = _filename_from(current, content_type)

                    handle, path = tempfile.mkstemp(suffix=Path(filename).suffix or ".bin")
                    size = 0
                    try:
                        with os.fdopen(handle, "wb") as target:
                            for chunk in response.iter_bytes(64 * 1024):
                                size += len(chunk)
                                # Enforce on the stream too: content-length can
                                # lie, or be absent entirely.
                                if size > MAX_FETCH_BYTES:
                                    raise FetchError(
                                        "That file is larger than "
                                        f"{MAX_FETCH_BYTES // (1024 * 1024)} MB."
                                    )
                                target.write(chunk)
                    except BaseException:
                        Path(path).unlink(missing_ok=True)
                        raise

                    if size == 0:
                        Path(path).unlink(missing_ok=True)
                        raise FetchError("That link returned an empty file.")

                    return Fetched(path=path, filename=filename,
                                   content_type=content_type, size=size)
            except FetchError:
                raise
            except httpx.TimeoutException as error:
                raise FetchError("That link took too long to respond.") from error
            except httpx.HTTPError as error:
                raise FetchError(f"Could not fetch that link: {type(error).__name__}.") from error

    raise FetchError(f"That link redirected too many times ({len(seen)} hops).")
