from __future__ import annotations

import struct
import wave
from pathlib import Path

from minutely import audio


def test_mime_types_map_to_extensions() -> None:
    assert audio.suffix_for("audio/webm;codecs=opus") == ".webm"
    assert audio.suffix_for("audio/mp4") == ".m4a"
    assert audio.suffix_for("audio/wav") == ".wav"
    # An unknown recorder still gets a container the rest of the app accepts.
    assert audio.suffix_for("audio/weird") == ".webm"
    assert audio.suffix_for("") == ".webm"


def test_safe_name_strips_paths_and_shell_surprises() -> None:
    assert audio.safe_name("../../etc/passwd") == "passwd"
    assert audio.safe_name("Q3 review; rm -rf.webm") == "Q3-review-rm-rf.webm"
    # A path is reduced to its basename, so a name cannot walk out of its folder.
    assert audio.safe_name("../../etc/shadow.wav") == "shadow.wav"
    assert audio.safe_name("") == "meeting"
    assert len(audio.safe_name("x" * 400)) <= 80


def test_wav_duration_is_read_from_the_header(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<8000h", *([0] * 8000)))
    assert audio.duration(path) == 0.5


def test_duration_of_a_missing_file_is_unknown(tmp_path: Path) -> None:
    assert audio.duration(tmp_path / "ghost.wav") is None


def test_duration_of_an_unreadable_container_is_unknown(tmp_path: Path) -> None:
    # ffprobe may or may not exist on the machine; either way this must not raise.
    path = tmp_path / "broken.wav"
    path.write_bytes(b"RIFFnot really a wav")
    assert audio.duration(path) is None
