"""Local speech recognition by shelling out to a whisper binary.

Two families of command line are in the wild and both are common enough to be
worth supporting:

* **whisper.cpp** (``whisper-cli``, historically ``main``) — wants a 16 kHz mono
  WAV and a model file, writes ``<prefix>.json``.
* **OpenAI whisper** (``whisper``, and the ``faster-whisper`` CLI that copies its
  flags) — reads the media file directly and writes into an output directory.

Which one the user has is detected from the binary name, with the ``--help``
text as a tiebreak. Nothing is downloaded and nothing is installed: if no
binary is present the transcriber says exactly what to install and stops.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import transcripts
from ..audio import to_wav16k
from ..models import Transcript
from . import TranscriptionError

# Long meetings are slow to transcribe on CPU; an hour of audio can take longer
# than an hour. The cap is generous rather than tight so that a real meeting
# never dies half-way, but it exists so a wedged process cannot hang forever.
TIMEOUT_SECONDS = 6 * 60 * 60


class WhisperTranscriber:
    """Drive a locally installed whisper binary."""

    name = "whisper"

    def __init__(self, binary: str = "whisper-cli", model: str = "", language: str = "en") -> None:
        self.binary = binary or "whisper-cli"
        self.model = model
        self.language = language

    # -- discovery --------------------------------------------------------

    def resolve(self) -> str | None:
        """Absolute path to the binary, trying sensible alternates."""
        candidates = [self.binary, "whisper-cli", "whisper", "main", "faster-whisper"]
        for candidate in candidates:
            found = shutil.which(candidate) or (
                str(Path(candidate).expanduser())
                if Path(candidate).expanduser().is_file()
                else None
            )
            if found:
                return found
        return None

    def available(self) -> bool:
        return self.resolve() is not None

    # -- transcription ----------------------------------------------------

    def transcribe(self, audio: Path, *, language: str = "") -> Transcript:
        binary = self.resolve()
        if binary is None:
            raise TranscriptionError(
                f"no whisper binary found (looked for {self.binary!r}, 'whisper-cli', 'whisper'). "
                "Install whisper.cpp or openai-whisper, set transcriber.whisper_bin, "
                "or import a transcript file instead: minutely import meeting.vtt"
            )
        if not audio.exists():
            raise TranscriptionError(f"audio file not found: {audio}")

        lang = language or self.language
        with tempfile.TemporaryDirectory(prefix="minutely-asr-") as tmp:
            workdir = Path(tmp)
            if _is_cpp(binary):
                output = self._run_cpp(binary, audio, workdir, lang)
            else:
                output = self._run_openai(binary, audio, workdir, lang)
            transcript = transcripts.load(output)

        merged = transcripts.merge_utterances(transcript)
        merged.source = str(audio)
        merged.language = lang
        return merged

    def _run_cpp(self, binary: str, audio: Path, workdir: Path, language: str) -> Path:
        if not self.model:
            raise TranscriptionError(
                "whisper.cpp needs a model file — set it with "
                "`minutely config --whisper-model /path/to/ggml-base.en.bin`"
            )
        model = Path(self.model).expanduser()
        if not model.is_file():
            raise TranscriptionError(f"whisper model not found: {model}")

        wav = audio if audio.suffix.lower() == ".wav" else to_wav16k(audio, workdir / "audio.wav")
        prefix = workdir / "out"
        command = [
            binary,
            "-m", str(model),
            "-f", str(wav),
            "-oj",
            "-of", str(prefix),
        ]
        if language:
            command += ["-l", language]
        _run(command)
        produced = prefix.with_suffix(".json")
        if not produced.exists():
            raise TranscriptionError("whisper.cpp produced no JSON output")
        return produced

    def _run_openai(self, binary: str, audio: Path, workdir: Path, language: str) -> Path:
        command = [
            binary,
            str(audio),
            "--output_format", "vtt",
            "--output_dir", str(workdir),
        ]
        if self.model:
            command += ["--model", self.model]
        if language:
            command += ["--language", language]
        _run(command)
        produced = sorted(workdir.glob("*.vtt"))
        if not produced:
            raise TranscriptionError("whisper produced no VTT output")
        return produced[0]


def _is_cpp(binary: str) -> bool:
    stem = Path(binary).name.lower()
    if "whisper-cli" in stem or stem in {"main", "whisper-cpp", "whisper.cpp"}:
        return True
    if stem.startswith("whisper") and stem not in {"whisper", "whisper.exe"}:
        return True
    if stem in {"whisper", "whisper.exe", "faster-whisper"}:
        return False
    # Unknown name: whisper.cpp advertises its model flag in --help.
    try:
        result = subprocess.run(
            [binary, "--help"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "-m FNAME" in result.stdout or "--model FNAME" in result.stdout


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        raise TranscriptionError(f"cannot execute {command[0]}")
    except subprocess.TimeoutExpired:
        raise TranscriptionError("transcription timed out")
    except OSError as exc:
        raise TranscriptionError(f"transcription failed to start: {exc}")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise TranscriptionError(
            f"{Path(command[0]).name} exited {result.returncode}: {' / '.join(tail) or 'no output'}"
        )
