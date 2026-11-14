"""Screen reconstruction: first time text was visible, across redraws and chunk boundaries."""

from __future__ import annotations

from zerg.qa.pty_reader import TimedChunk
from zerg.qa.terminal_screen import ScreenReplay
from zerg.qa.terminal_screen import first_visible

ROWS, COLS = 10, 40
CSI = b"\x1b["


def _at(chunks, *needles, rows=ROWS, cols=COLS):
    return {r.needle: r.t for r in first_visible(chunks, needles, rows=rows, columns=cols)}


def test_plain_output_is_dated_to_the_chunk_that_delivered_it() -> None:
    chunks = [(1.0, b"hello\r\n"), (2.5, b"MARK0001 and more\r\n"), (4.0, b"tail\r\n")]
    assert _at(chunks, "hello", "MARK0001", "tail", "never") == {"hello": 1.0, "MARK0001": 2.5, "tail": 4.0, "never": None}


def test_a_marker_split_across_two_chunks_is_dated_to_the_chunk_that_completes_it() -> None:
    chunks = [(1.0, b"abc MARK00"), (3.0, b"02 def")]
    assert _at(chunks, "MARK0002") == {"MARK0002": 3.0}


def test_an_escape_sequence_split_across_chunks_does_not_leak_into_the_text() -> None:
    chunks = [(1.0, b"\x1b[3"), (1.5, b"1mred\x1b[0m MARK0003")]
    seen = _at(chunks, "red MARK0003", "[31m")
    assert seen == {"red MARK0003": 1.5, "[31m": None}


def test_spinner_line_redrawn_in_place_is_seen_even_though_it_is_gone_from_the_final_screen() -> None:
    frames = [b"\r| thinking", b"\r/ thinking", b"\r- working " + CSI + b"K", b"\r" + CSI + b"2KDone MARK0004\r\n"]
    chunks = [(1.0 + i * 0.25, frame) for i, frame in enumerate(frames)]
    seen = _at(chunks, "| thinking", "/ thinking", "- working", "Done MARK0004", "thinking MARK")
    assert seen == {"| thinking": 1.0, "/ thinking": 1.25, "- working": 1.5, "Done MARK0004": 1.75, "thinking MARK": None}
    replay = ScreenReplay([], rows=ROWS, columns=COLS)
    for t, frame in chunks:
        replay.feed(t, frame)
    assert replay.display[0] == "Done MARK0004"  # the spinner text really was overwritten


def test_bottom_input_box_redrawn_with_cursor_addressing() -> None:
    def box(text: str) -> bytes:
        return b"\x1b7" + CSI + f"{ROWS};1H".encode() + CSI + b"2K" + f"> {text}".encode() + b"\x1b8"

    chunks = [
        (1.0, b"output line one\r\n"),
        (1.2, box("typ")),
        (1.4, box("typed MARK0005")),
        (1.6, b"output line two\r\n"),
        (1.8, box("")),
    ]
    seen = _at(chunks, "> typ", "typed MARK0005", "output line two")
    assert seen == {"> typ": 1.2, "typed MARK0005": 1.4, "output line two": 1.6}
    replay = ScreenReplay([], rows=ROWS, columns=COLS)
    for t, data in chunks:
        replay.feed(t, data)
    assert replay.display[-1] == ">"
    assert "MARK0005" not in "\n".join(replay.display)


def test_text_that_scrolled_off_within_one_chunk_still_counts_as_seen() -> None:
    body = b"".join(f"line {i:03d}\r\n".encode() for i in range(40)) + b"final\r\n"
    chunks = [(1.0, b"start\r\n"), (2.0, body)]
    seen = _at(chunks, "line 000", "line 039", "final", "start")
    assert seen == {"line 000": 2.0, "line 039": 2.0, "final": 2.0, "start": 1.0}


def test_a_word_wrapped_at_the_right_edge_is_one_word() -> None:
    chunks = [(1.0, b"x" * (COLS - 4) + b"MARK0006 tail")]
    assert _at(chunks, "MARK0006") == {"MARK0006": 1.0}


def test_accepts_timed_chunks_and_unicode() -> None:
    chunks = [TimedChunk(5.0, 0, "café → MARK0007".encode())]
    assert _at(chunks, "café → MARK0007") == {"café → MARK0007": 5.0}


def test_clear_screen_removes_text_but_first_seen_stays() -> None:
    chunks = [(1.0, b"MARK0008"), (2.0, CSI + b"2J" + CSI + b"H" + b"MARK0009")]
    assert _at(chunks, "MARK0008", "MARK0009") == {"MARK0008": 1.0, "MARK0009": 2.0}


def test_rendering_only_the_changed_lines_shows_what_rendering_every_line_shows() -> None:
    """The search renders only lines the emulator marks changed (2026-11-13: every line after every chunk was most of a shard's CPU)."""
    import random

    rng = random.Random(20261113)
    words = [b"plain text ", b"MARK0009 ", b"x" * 55, b"\t", b"\r\n", b"\r", b"\n", b"\x08"]
    moves = [
        lambda: CSI + f"{rng.randint(1, ROWS)};{rng.randint(1, COLS)}H".encode(),  # cursor to
        lambda: CSI + rng.choice([b"K", b"1K", b"2K", b"J", b"1J", b"2J"]),  # erase in line, in display
        lambda: CSI + f"{rng.randint(1, 3)}".encode() + rng.choice([b"L", b"M", b"P", b"@", b"X", b"A", b"B", b"S", b"T"]),
        lambda: rng.choice(
            [b"\x1bM", b"\x1bD", b"\x1bE", b"\x1b7", b"\x1b8", b"\x1bc"]
        ),  # reverse index, index, next line, save, restore, reset
        lambda: CSI + f"{rng.randint(1, 4)};{rng.randint(5, ROWS)}r".encode(),  # scroll region
        lambda: CSI + b"1;31m",
    ]
    replay = ScreenReplay(["a needle that never comes"], rows=ROWS, columns=COLS)
    for step in range(3000):
        chunk = b"".join(rng.choice(words) if rng.random() < 0.6 else rng.choice(moves)() for _ in range(rng.randint(1, 6)))
        cut = rng.randint(0, len(chunk))  # sequences split across chunks too
        for part in (chunk[:cut], chunk[cut:]):
            replay.feed(float(step), part)
            assert replay._lines == replay.display, f"step {step}: the cached lines differ from the screen after {part!r}"


def test_a_chunk_that_changes_nothing_on_screen_is_not_searched_again() -> None:
    replay = ScreenReplay(["MARK0010"], rows=ROWS, columns=COLS)
    replay.feed(1.0, b"hello")
    assert replay._haystacks() is None  # nothing new since the search inside feed
    replay.feed(2.0, CSI + b"0m")  # an attribute change draws nothing
    assert replay._haystacks() is None
    replay.feed(3.0, b" MARK0010")
    assert {r.needle: r.t for r in replay.result(["MARK0010"])} == {"MARK0010": 3.0}


def test_wide_characters_render_as_the_emulator_renders_them_and_an_orphaned_stub_is_not_an_error() -> None:
    replay = ScreenReplay(["never"], rows=ROWS, columns=COLS)
    for t, data in enumerate(["日本語 MARK0011\r\n".encode(), "ab中cd".encode(), b"\r\n" * 12, "漢字 x".encode()]):
        replay.feed(float(t), data)
        assert replay._lines == replay.display
    # Erase the wide character and leave its stub: pyte's own display raises IndexError here, and the search must not.
    replay.feed(9.0, CSI + b"1;1H" + "日".encode() + CSI + b"1;1H" + CSI + b"1X" + b" MARK0012")
    assert "MARK0012" in "".join(replay._lines)
