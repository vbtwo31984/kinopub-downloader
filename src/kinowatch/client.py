from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://kino.watch"
STATE_PATH = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "kinowatch" / "session.json"


class KinoWatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    subtitle: str = ""
    original_title: str = ""


@dataclass(frozen=True)
class MediaSource:
    url: str
    label: str = "Download"
    kind: str = "file"
    referer: str = ""
    duration: float = 0.0
    estimated_size: int = 0
    audio_url: str = ""
    tracks: tuple["MediaTrack", ...] = ()


@dataclass(frozen=True)
class MediaTrack:
    url: str
    kind: str
    language: str
    name: str
    default: bool = False

    @property
    def label(self) -> str:
        default = " (default)" if self.default else ""
        return f"{self.kind.title():<9} {self.language:<5} {self.name}{default}"


@dataclass(frozen=True)
class TwoFactorChallenge:
    action: str
    fields: dict[str, str]
    code_field: str
    prompt: str = "Enter the code sent to your email"


class KinoWatchClient:
    def __init__(self, state_path: Path = STATE_PATH) -> None:
        self.state_path = state_path
        self.http = httpx.Client(
            base_url=BASE_URL,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "KinoWatchTUI/0.1 (+local authorized client)"},
        )
        self._load_cookies()

    def close(self) -> None:
        self.http.close()

    @property
    def is_logged_in(self) -> bool:
        response = self.http.get("/item/search", params={"query": ""})
        return "/user/login" not in str(response.url)

    def _load_cookies(self) -> None:
        try:
            saved: dict[str, str] = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.http.cookies.update(saved)

    def _save_cookies(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {cookie.name: cookie.value for cookie in self.http.cookies.jar}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    def login(self, username: str, password: str) -> TwoFactorChallenge | None:
        page = self.http.get("/user/login")
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        csrf = soup.select_one('input[name="_csrf"]')
        if not csrf or not csrf.get("value"):
            raise KinoWatchError("The login page did not provide a CSRF token.")
        response = self.http.post(
            "/user/login",
            data={
                "_csrf": csrf["value"],
                "login-form[login]": username,
                "login-form[password]": password,
                "login-form[rememberMe]": "1",
            },
        )
        response.raise_for_status()
        challenge = parse_two_factor_challenge(response.text, str(response.url))
        if challenge:
            return challenge
        if "/user/login" in str(response.url):
            raise KinoWatchError("Login was rejected. Check your username or password.")
        self._save_cookies()
        return None

    def confirm_two_factor(self, challenge: TwoFactorChallenge, code: str) -> None:
        data = dict(challenge.fields)
        data[challenge.code_field] = code.strip()
        response = self.http.post(challenge.action, data=data)
        response.raise_for_status()
        if parse_two_factor_challenge(response.text, str(response.url)) or not self.is_logged_in:
            raise KinoWatchError("The verification code was rejected or expired.")
        self._save_cookies()

    def search(self, query: str) -> list[SearchResult]:
        response = self.http.get("/item/search", params={"query": query})
        response.raise_for_status()
        if "/user/login" in str(response.url):
            raise KinoWatchError("Your saved session has expired. Please log in again.")
        return parse_search_results(response.text)

    def sources(self, item_url: str) -> list[MediaSource]:
        response = self.http.get(item_url)
        response.raise_for_status()
        if "/user/login" in str(response.url):
            raise KinoWatchError("Your saved session has expired. Please log in again.")
        sources = parse_media_sources(response.text, str(response.url))
        expanded: list[MediaSource] = []
        for source in sources:
            expanded.extend(self.hls_variants(source) if source.kind == "hls" else [source])
        return expanded

    def hls_variants(self, source: MediaSource) -> list[MediaSource]:
        """Expand an HLS master playlist into its selectable quality streams."""
        response = self.http.get(source.url, headers={"Referer": source.referer} if source.referer else {})
        response.raise_for_status()
        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        tracks_by_group: dict[str, list[MediaTrack]] = {}
        for line in lines:
            if not line.startswith("#EXT-X-MEDIA:"):
                continue
            track_kind = _hls_attribute(line, "TYPE").lower()
            if track_kind not in {"audio", "subtitles"}:
                continue
            group = _hls_attribute(line, "GROUP-ID")
            track_uri = _hls_attribute(line, "URI")
            if group and track_uri:
                tracks_by_group.setdefault(group, []).append(MediaTrack(
                    urljoin(source.url, track_uri),
                    "subtitle" if track_kind == "subtitles" else "audio",
                    _hls_attribute(line, "LANGUAGE") or "und",
                    _hls_attribute(line, "NAME") or "Unnamed",
                    _hls_attribute(line, "DEFAULT") == "YES",
                ))
        variants: list[MediaSource] = []
        for index, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            stream_url = next((candidate for candidate in lines[index + 1:] if not candidate.startswith("#")), "")
            if not stream_url:
                continue
            resolution = re.search(r"RESOLUTION=\d+x(\d+)", line)
            name = re.search(r'(?:NAME|VIDEO)="?([^,"]+)', line)
            bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
            audio_group = _hls_attribute(line, "AUDIO")
            subtitle_group = _hls_attribute(line, "SUBTITLES")
            quality = f"{resolution.group(1)}p" if resolution else (name.group(1) if name else "Video")
            estimated_size = int(int(bandwidth.group(1)) * source.duration / 8) if bandwidth and source.duration else 0
            tracks = tuple(tracks_by_group.get(audio_group, []) + tracks_by_group.get(subtitle_group, []))
            default_audio = next((track.url for track in tracks if track.kind == "audio" and track.default), "")
            variants.append(MediaSource(
                urljoin(source.url, stream_url), quality, "hls", source.referer,
                source.duration, estimated_size, default_audio, tracks,
            ))
        return variants or [source]

    def download(self, source: MediaSource, destination: Path, progress: Any) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self.http.stream("GET", source.url, headers=headers) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0)) + offset
            mode = "ab" if offset and response.status_code == 206 else "wb"
            if mode == "wb":
                offset = 0
            with partial.open(mode) as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 256):
                    output.write(chunk)
                    offset += len(chunk)
                    progress(offset, total)
        partial.replace(destination)

    def download_hls(self, source: MediaSource, destination: Path, progress: Any, tracks: list[MediaTrack] | None = None) -> None:
        """Remux the signed HLS stream the page player exposes into an MP4 file."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        duration = self._hls_duration(source)
        command = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1",
        ]
        selected_tracks = tracks if tracks is not None else [track for track in source.tracks if track.kind == "audio" and track.default]
        input_options = ["-user_agent", self.http.headers["User-Agent"]]
        if source.referer:
            input_options.extend(["-referer", source.referer])
        command.extend(input_options + ["-i", source.url])
        for input_index, track in enumerate(selected_tracks, start=1):
            command.extend(input_options + ["-i", track.url])
        command.extend(["-map", "0:v:0"])
        for input_index, track in enumerate(selected_tracks, start=1):
            command.extend(["-map", f"{input_index}:{'a' if track.kind == 'audio' else 's'}:0?"])
        command.extend(["-c:v", "copy", "-c:a", "copy"])
        if any(track.kind == "subtitle" for track in selected_tracks):
            command.extend(["-c:s", "mov_text"])
        command.append(str(temporary))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        speed = ""
        assert process.stdout is not None
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "speed":
                speed = value
            elif key in {"out_time_us", "out_time_ms"}:
                try:
                    progress(int(value) / 1_000_000, duration, speed)
                except ValueError:
                    pass
        stderr = process.stderr.read() if process.stderr else ""
        if process.wait() != 0:
            raise KinoWatchError(f"FFmpeg could not save the player stream: {stderr.strip()[-300:]}")
        temporary.replace(destination)

    def _hls_duration(self, source: MediaSource) -> float:
        command = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            "-user_agent", self.http.headers["User-Agent"],
        ]
        if source.referer:
            command.extend(["-referer", source.referer])
        command.extend([source.url])
        probe = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            return float(probe.stdout.strip())
        except ValueError:
            return 0.0


def parse_search_results(html: str) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[SearchResult] = []
    seen: set[str] = set()
    # kino.watch search results use an empty poster link followed by one or more
    # title links inside a `.item` card. Parse the card as a whole so the empty
    # poster does not suppress the actual title as a duplicate.
    for card in soup.select(".item"):
        links = card.select('a[href*="/item/view/"]')
        if not links:
            continue
        url = urljoin(BASE_URL, links[0].get("href", ""))
        if url in seen:
            continue
        names = list(dict.fromkeys(link.get_text(" ", strip=True) for link in links if link.get_text(" ", strip=True)))
        if not names:
            continue
        seen.add(url)
        title = names[0]
        original_title = names[1] if len(names) > 1 else ""
        text = card.get_text(" ", strip=True)
        subtitle = text
        for name in names[:2]:
            subtitle = subtitle.replace(name, "", 1)
        found.append(SearchResult(title, url, subtitle.strip(" -|•")[:160], original_title))

    # Keep a fallback for pages that don't use the normal search-result card.
    if found:
        return found
    for anchor in soup.select('a[href*="/item/view/"]'):
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        url = urljoin(BASE_URL, anchor.get("href", ""))
        if url not in seen:
            seen.add(url)
            found.append(SearchResult(title, url))
    return found


def parse_two_factor_challenge(html: str, page_url: str) -> TwoFactorChallenge | None:
    """Extract an email/OTP form presented after the initial login request."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        fields: dict[str, str] = {}
        code_field: str | None = None
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if not name:
                continue
            input_type = input_tag.get("type", "text").lower()
            value = input_tag.get("value", "")
            fields[name] = value
            normalized = name.lower()
            if input_type in {"text", "tel", "number", "password"} and re.search(r"(?:code|otp|auth|token|verify|confirm)", normalized):
                code_field = name
        if code_field:
            action = urljoin(page_url, form.get("action") or page_url)
            fields.pop(code_field, None)
            text = form.get_text(" ", strip=True)
            prompt = text[:160] if text else "Enter the code sent to your email"
            return TwoFactorChallenge(action, fields, code_field, prompt)
    return None


def parse_media_sources(html: str, page_url: str) -> list[MediaSource]:
    """Find direct, downloadable media URLs exposed by the authenticated page.

    This intentionally does not bypass DRM, access controls, or challenges. It only
    uses sources the site presents to the signed-in browser session.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str, str, float]] = []
    # The visible <video> element on these pages is the trailer. The full player
    # playlist is placed in a JavaScript assignment by the authenticated page.
    for script in soup.select("script"):
        body = script.string or script.get_text()
        match = re.search(r"window\.PLAYER_PLAYLIST\s*=\s*(\[.*?\])\s*;", body, re.S)
        if not match:
            continue
        try:
            playlist = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for entry in playlist:
            manifest = entry.get("manifest")
            if not isinstance(manifest, str):
                continue
            title = entry.get("title") or "Video"
            episode = entry.get("episode_title")
            label = f"{title} — {episode}" if episode else title
            candidates.append((urljoin(page_url, manifest), label, "hls", float(entry.get("duration") or 0)))
    if candidates:
        return _media_sources(candidates, page_url)

    # Fallback for pages that expose the full item directly in a media element.
    for tag in soup.select("video[src], video source[src], a[href]"):
        candidate = tag.get("src") or tag.get("href")
        if not candidate:
            continue
        absolute = urljoin(page_url, candidate)
        is_hls = tag.name in {"video", "source"} and ("mpegurl" in tag.get("type", "").lower() or ".m3u8" in absolute.lower())
        if is_hls:
            candidates.append((absolute, tag.get("title", "Player stream (MP4 via FFmpeg)"), "hls", 0))
        elif re.search(r"\.(?:mp4|mkv|webm|m4v)(?:[?#].*)?$", absolute, re.I):
            candidates.append((absolute, tag.get_text(" ", strip=True) or tag.get("title", "Download"), "file", 0))
    # Some players put normal URLs in JSON-like script attributes.
    for match in re.finditer(r'''["'](https?://[^"'\\]+?\.(?:mp4|mkv|webm|m4v)(?:\?[^"'\\]*)?)["']''', html, re.I):
        candidates.append((match.group(1), "Video file", "file", 0))
    return _media_sources(candidates, page_url)


def _media_sources(candidates: list[tuple[str, str, str, float]], page_url: str) -> list[MediaSource]:
    results: list[MediaSource] = []
    seen: set[str] = set()
    for url, label, kind, duration in candidates:
        if url not in seen:
            seen.add(url)
            results.append(MediaSource(url, label, kind, page_url, duration))
    return results


def _hls_attribute(line: str, name: str) -> str:
    match = re.search(rf'(?:^|[,:]){re.escape(name)}=(?:"([^"]*)"|([^,]*))', line)
    return (match.group(1) or match.group(2)).strip() if match else ""
