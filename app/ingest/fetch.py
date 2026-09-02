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

# Whatever the platform, these two always apply, so every refusal ends with them.
_UNIVERSAL_STEPS = [
    "Sing or play the melody and record it — one clean line transcribes far "
    "better here than any full mix, so this is the app's best input.",
    "Or paste a direct link to a file you host or have the rights to.",
]

# How to legitimately obtain a file, per platform. Keyed by registered domain.
_PLATFORM_GUIDANCE: dict[str, tuple[str, list[str]]] = {
    "youtube.com": ("YouTube", [
        "If it is your own upload: YouTube Studio → Content → hover the video "
        "→ the ⋮ menu → Download. That gives you an MP4 you can drop straight in.",
    ]),
    "youtu.be": ("YouTube", [
        "If it is your own upload: YouTube Studio → Content → hover the video "
        "→ the ⋮ menu → Download. That gives you an MP4 you can drop straight in.",
    ]),
    "bandcamp.com": ("Bandcamp", [
        "Buying the track on Bandcamp includes an actual download — take the "
        "WAV or MP3 and upload it here. Some artists offer it free.",
    ]),
    "soundcloud.com": ("SoundCloud", [
        "Some tracks have a Download button the artist has enabled — if this "
        "one does, use it and upload the file.",
    ]),
    "music.apple.com": ("Apple Music", [
        "Apple Music is streaming only, but the iTunes Store sells the same "
        "tracks as downloads. A purchased M4A works here.",
    ]),
    "itunes.apple.com": ("the iTunes Store", [
        "A purchased track downloads as an M4A, which this app reads directly.",
    ]),
    "vimeo.com": ("Vimeo", [
        "If it is your own video, or the owner enabled downloads, Vimeo offers "
        "the file directly — then upload it here.",
    ]),
}

# Streaming-only services, where no legitimate file export exists at all.
_NO_EXPORT = {
    "spotify.com": "Spotify", "tidal.com": "TIDAL", "deezer.com": "Deezer",
    "pandora.com": "Pandora", "kkbox.com": "KKBOX", "music.163.com": "NetEase Music",
    "y.qq.com": "QQ Music", "kugou.com": "Kugou", "netflix.com": "Netflix",
    "mixcloud.com": "Mixcloud", "audiomack.com": "Audiomack",
    "bilibili.com": "Bilibili", "dailymotion.com": "Dailymotion",
    "tiktok.com": "TikTok", "douyin.com": "Douyin", "twitch.tv": "Twitch",
    "instagram.com": "Instagram", "facebook.com": "Facebook",
    "twitter.com": "X", "x.com": "X",
}


def _domain_keys(host: str) -> list[str]:
    """The host and every parent domain, most specific first.

    Lookups walk this rather than testing one "registered domain", because the
    interesting names are not all two labels: `music.163.com` and `y.qq.com`
    have their own entries, and a bare last-two-labels rule reduces them to
    `163.com` and `qq.com` and misses.
    """
    labels = host.lower().strip(".").split(".")
    return [".".join(labels[i:]) for i in range(len(labels))]


def _guidance_for(host: str) -> tuple[str, list[str]]:
    """Name the platform and say how to get the file legitimately."""
    host = host.lower().strip(".")

    # Most specific name wins, so a per-host entry beats its parent domain's.
    for key in _domain_keys(host):
        if key in _PLATFORM_GUIDANCE:
            platform, steps = _PLATFORM_GUIDANCE[key]
            return platform, [*steps, *_UNIVERSAL_STEPS]
        if key in _NO_EXPORT:
            name = _NO_EXPORT[key]
            return name, [
                f"{name} has no way to export a file, so there is no legitimate "
                "route from a link there. If you own the recording, upload it directly.",
                *_UNIVERSAL_STEPS,
            ]
    return host, list(_UNIVERSAL_STEPS)

_MEDIA_CONTENT_TYPES = ("audio/", "video/", "application/octet-stream")
_SCORE_CONTENT_TYPES = ("xml", "musicxml", "midi", "application/zip")


class FetchError(ValueError):
    """Anything that stops a fetch, phrased for the person who typed the URL."""


class StreamingSiteError(FetchError):
    """A refusal that comes with a route to what the person actually wanted.

    "No" on its own is a dead end when there is usually a legitimate way to get
    the same file — the site's own download for your own uploads, a purchase
    that includes one, or simply recording the tune.
    """

    def __init__(self, platform: str, steps: list[str]):
        self.platform = platform
        self.steps = steps
        super().__init__(
            f"{platform} does not allow its audio to be downloaded, and for "
            f"commercial music that is a copyright question too. Here is what does work."
        )


@dataclass
class Fetched:
    path: str
    filename: str
    content_type: str
    size: int


def _is_streaming_host(host: str) -> bool:
    return any(key in STREAMING_HOSTS for key in _domain_keys(host))


def _addresses_for(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
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
        platform, steps = _guidance_for(host)
        raise StreamingSiteError(platform, steps)

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
