from __future__ import annotations

import getpass
import time
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from tqdm import tqdm

from .client import KinoWatchClient, KinoWatchError, MediaSource, MediaTrack, SearchResult, TwoFactorChallenge


def _message(title: str, text: str) -> None:
    print(f"\n{title}: {text}")


def _confirm(text: str) -> bool:
    return input(f"{text} [y/N] ").strip().lower() in ("y", "yes")


def _selector(
    title: str,
    values: list[tuple[object, str]],
    page_size: int = 18,
    multiple: bool = False,
    preselected: set[object] | None = None,
) -> object | list[object] | None:
    """Minimal keyboard selector, matching Soap4Downloader's interaction style."""
    if not values:
        return [] if multiple else None
    selected_index = 0
    selected_values = set(preselected or ())
    bindings = KeyBindings()

    def move(delta: int) -> None:
        nonlocal selected_index
        selected_index = min(len(values) - 1, max(0, selected_index + delta))

    def render() -> list[tuple[str, str]]:
        start = max(0, min(selected_index - page_size // 2, len(values) - page_size))
        end = min(len(values), start + page_size)
        output: list[tuple[str, str]] = [
            ("class:title", f"{title}\n"),
            ("", "Up/down or j/k to move. Space toggles, Enter continues, q cancels.\n\n" if multiple else "Up/down or j/k to move. Enter selects. q cancels.\n\n"),
        ]
        for index in range(start, end):
            _, label = values[index]
            marker = "[x] " if multiple and values[index][0] in selected_values else ("[ ] " if multiple else "")
            output.append(("reverse" if index == selected_index else "", f"{'>' if index == selected_index else ' '} {marker}{label}\n"))
        if len(values) > page_size:
            output.append(("", f"\nShowing {start + 1}-{end} of {len(values)}"))
        return output

    @bindings.add("up")
    @bindings.add("k")
    def up(event) -> None:
        move(-1)

    @bindings.add("down")
    @bindings.add("j")
    def down(event) -> None:
        move(1)

    @bindings.add("pageup")
    def page_up(event) -> None:
        move(-page_size)

    @bindings.add("pagedown")
    def page_down(event) -> None:
        move(page_size)

    @bindings.add("home")
    def home(event) -> None:
        nonlocal selected_index
        selected_index = 0

    @bindings.add("end")
    def end(event) -> None:
        nonlocal selected_index
        selected_index = len(values) - 1

    @bindings.add("enter")
    def enter(event) -> None:
        get_app().exit(result=[value for value, _ in values if value in selected_values] if multiple else values[selected_index][0])

    @bindings.add(" ")
    def toggle(event) -> None:
        if not multiple:
            return
        value = values[selected_index][0]
        if value in selected_values:
            selected_values.remove(value)
        else:
            selected_values.add(value)

    @bindings.add("q")
    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event) -> None:
        get_app().exit(result=None)

    return Application(
        layout=Layout(Window(content=FormattedTextControl(render, focusable=True), dont_extend_height=True)),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
    ).run()


def _login(client: KinoWatchClient) -> bool:
    if not _confirm("You are not logged in. Login now?"):
        _message("Abort", "Login is required.")
        return False
    username = input("kino.watch login or email: ").strip()
    password = getpass.getpass("kino.watch password: ")
    if not username or not password:
        _message("Login failed", "A login and password are required.")
        return False
    try:
        challenge = client.login(username, password)
        if isinstance(challenge, TwoFactorChallenge):
            code = input("Verification code from email: ").strip()
            if not code:
                _message("Login failed", "A verification code is required.")
                return False
            client.confirm_two_factor(challenge, code)
    except KinoWatchError as exc:
        _message("Login failed", str(exc))
        return False
    _message("Success", f"Logged in as {username}")
    return True


def _download(client: KinoWatchClient, source: MediaSource, title: str, tracks: list[MediaTrack]) -> None:
    suggested = "".join(char if char.isalnum() or char in " ._-" else "_" for char in title).strip() + ".mp4"
    filename = input(f"Save as [downloads/{suggested}]: ").strip() or suggested
    destination = Path.cwd() / "downloads" / Path(filename).name
    if source.kind == "hls":
        temporary = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        total_text = f" / {_format_size(source.estimated_size)}" if source.estimated_size else ""
        started = time.monotonic()
        with tqdm(
            total=100,
            desc=f"{destination.name} — 0 MB{total_text}",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {postfix}",
            mininterval=0.5,
        ) as bar:
            last_percent = 0.0

            def hls_progress(done: float, total: float, speed: str) -> None:
                nonlocal last_percent
                percent = (done / total * 100) if total else 0
                bar.update(max(0, percent - last_percent))
                last_percent = percent
                try:
                    bytes_written = temporary.stat().st_size
                except FileNotFoundError:
                    bytes_written = 0
                bar.set_description(f"{destination.name} — {_format_size(bytes_written)}{total_text}")
                elapsed = max(time.monotonic() - started, 0.001)
                rate = bytes_written / elapsed
                remaining = (source.estimated_size - bytes_written) / rate if source.estimated_size and rate else 0
                eta = _format_eta(remaining) if source.estimated_size and rate else "--:--"
                bar.set_postfix_str(f"{_format_size(int(rate))}/s ETA {eta}")

            client.download_hls(source, destination, hls_progress, tracks)
        _message("Done", f"Saved {destination}")
        return
    with tqdm(unit="B", unit_scale=True, unit_divisor=1024, desc=destination.name, mininterval=0.5) as bar:
        last = 0

        def progress(done: int, total: int) -> None:
            nonlocal last
            if total:
                bar.total = total
            bar.update(done - last)
            last = done

        client.download(source, destination, progress)
    _message("Done", f"Saved {destination}")


def _format_size(value: int) -> str:
    mib = 1024 * 1024
    gib = 1024 * mib
    if value < gib:
        return f"{value / mib:.0f} MB"
    return f"{value / gib:.2f} GB"


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def run_tui() -> None:
    client = KinoWatchClient()
    try:
        if not client.is_logged_in and not _login(client):
            return
        while True:
            query = input("\nSearch kino.watch (blank to quit): ").strip()
            if not query:
                return
            results = client.search(query)
            if not results:
                _message("No results", f'Nothing was found for "{query}". Try another search.')
                continue
            chosen = _selector(
                "Search results\nTitle                                      Original title                         Details",
                [
                    (result, f"{result.title[:42]:<42} {result.original_title[:38]:<38} {result.subtitle}")
                    for result in results
                ],
            )
            if not isinstance(chosen, SearchResult):
                continue
            sources = client.sources(chosen.url)
            source = _selector("Available qualities", [(item, item.label or item.url) for item in sources])
            if not isinstance(source, MediaSource):
                _message("No file", "The selected page did not expose a direct downloadable video file.")
                continue
            tracks = _selector(
                "Audio and subtitles to include",
                [(track, track.label) for track in source.tracks],
                multiple=True,
                preselected={track for track in source.tracks if track.kind == "audio" and track.default},
            )
            if not isinstance(tracks, list):
                continue
            _download(client, source, chosen.original_title or chosen.title, tracks)
    except KinoWatchError as exc:
        _message("Error", str(exc))
    except KeyboardInterrupt:
        print()
    finally:
        client.close()


def main() -> None:
    run_tui()


if __name__ == "__main__":
    main()
