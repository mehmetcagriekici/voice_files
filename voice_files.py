#!/usr/bin/env python3
"""Read text and Markdown files aloud from the terminal, offline.

Files can also be web URLs; GitHub file and repository links are fetched as
raw text, so https://github.com/owner/repo/blob/main/README.md works.

Speech comes from Piper, a natural-sounding neural voice. DECtalk and
eSpeak NG are available as alternative engines.

Markdown is detected (by extension, or by content for other files) and reduced
to plain prose first: code blocks, images, link URLs and formatting are dropped.

While reading: space pauses/resumes, left/right skip paragraphs, q quits.
"""

import argparse
import array
import ctypes
import ctypes.util
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
TEXT_SUFFIXES = {".txt", ".text"}

PIPER_VOICES = Path(os.environ.get("PIPER_VOICES", Path.home() / ".local/share/piper-voices"))
DECTALK_HOME = Path(os.environ.get("DECTALK_HOME", Path.home() / ".local/share/dectalk"))


# --------------------------------------------------------------------------
# Text preparation
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects readable text from rendered Markdown HTML."""

    BLOCKS = {
        "p", "div", "li", "blockquote", "tr", "dt", "dd", "table", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br", "figure", "details", "summary",
    }
    SKIP = {"pre", "script", "style"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "area", "col", "embed", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in self.VOID:
                self.skip += 1
            return
        if tag in self.SKIP:
            self.skip = 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag not in self.VOID:
                self.skip -= 1
            return
        if tag in ("td", "th"):
            self.parts.append(", ")
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data.replace("\n", " "))


def markdown_to_text(md: str) -> str:
    from markdown_it import MarkdownIt

    md = re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.DOTALL)  # front matter
    md = re.sub(r"\[\^[^\]\s]+\]:?", "", md)  # footnote markers
    html = MarkdownIt("commonmark").enable(["table", "strikethrough"]).render(md)
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*\[[ xX]\]\s+", "", text, flags=re.MULTILINE)  # task list boxes
    text = re.sub(r",\s*(\n|$)", r"\1", text)  # trailing table separators
    return _tidy(text)


def plain_to_text(text: str) -> str:
    """Re-join hard-wrapped lines so sentences are not broken by pauses."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        lines = []
        for line in (l.strip() for l in block.splitlines()):
            if not line:
                continue
            if lines and line[0].islower():
                lines[-1] += " " + line
            else:
                lines.append(line)
        paragraphs.append("\n".join(lines))
    return _tidy("\n\n".join(paragraphs))


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_MD_HINTS = re.compile(
    r"^(#{1,6}\s|```|~~~|>\s|\s*[-*+]\s+\S|\s*\d+\.\s+\S|\|.*\|\s*$)"
    r"|\[[^\]\n]+\]\([^)\n]+\)|\*\*[^*\n]+\*\*|`[^`\n]+`",
    re.MULTILINE,
)


def is_markdown(path: Path, text: str) -> bool:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return True
    if suffix in TEXT_SUFFIXES:
        return False
    return len(_MD_HINTS.findall(text)) >= 3


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def source_path(source: str) -> Path:
    """The local path, or for a URL the path part (used to detect Markdown)."""
    return Path(urllib.parse.urlparse(source).path) if is_url(source) else Path(source)


def raw_url(url: str) -> str:
    """Rewrite GitHub page links to the URL of the raw file."""
    parts = urllib.parse.urlparse(url)
    if parts.netloc.lower() not in ("github.com", "www.github.com"):
        return url
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) == 2:  # repository page: its README
        owner, repo = segments[0], segments[1].removesuffix(".git")
        return f"https://api.github.com/repos/{owner}/{repo}/readme"
    if len(segments) >= 5 and segments[2] in ("blob", "raw"):
        owner, repo, _, *rest = segments
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{'/'.join(rest)}"
    return url


def fetch(url: str) -> str:
    request = urllib.request.Request(raw_url(url), headers={
        "User-Agent": "voice-files",
        "Accept": "application/vnd.github.raw, text/plain;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8")


def markdown_links(repo_url: str) -> list[str]:
    """Links to the Markdown files at the top of a GitHub repository."""
    import json

    contents = raw_url(repo_url).removesuffix("/readme") + "/contents/"
    request = urllib.request.Request(contents, headers={"User-Agent": "voice-files"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            entries = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return [e["html_url"] for e in entries
            if e.get("type") == "file" and Path(e["name"]).suffix.lower() in MARKDOWN_SUFFIXES]


def load_text(source: str) -> str:
    path = source_path(source)
    raw = fetch(source) if is_url(source) else path.read_text(encoding="utf-8")
    if is_url(source) and raw_url(source).endswith("/readme"):
        path = Path("README.md")  # GitHub repository READMEs are nearly always Markdown
    return markdown_to_text(raw) if is_markdown(path, raw) else plain_to_text(raw)


def chunk_text(text: str, limit: int = 500) -> list[str]:
    """One piece per paragraph; long paragraphs are split at sentence ends."""
    pieces = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= limit:
            pieces.append(line)
            continue
        current = ""
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(sentence[:cut])
                sentence = sentence[cut:].lstrip()
            if current and len(current) + len(sentence) + 1 > limit:
                pieces.append(current)
                current = ""
            current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)
    return pieces


# --------------------------------------------------------------------------
# eSpeak NG (libespeak-ng through ctypes)
# --------------------------------------------------------------------------

class _Event(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("unique_identifier", ctypes.c_uint),
        ("text_position", ctypes.c_int),
        ("length", ctypes.c_int),
        ("audio_position", ctypes.c_int),
        ("sample", ctypes.c_int),
        ("user_data", ctypes.c_void_p),
        ("id", ctypes.c_char * 8),
    ]


class _Voice(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("languages", ctypes.c_char_p),
        ("identifier", ctypes.c_char_p),
        ("gender", ctypes.c_ubyte),
        ("age", ctypes.c_ubyte),
        ("variant", ctypes.c_ubyte),
        ("xx1", ctypes.c_ubyte),
        ("score", ctypes.c_int),
        ("spare", ctypes.c_void_p),
    ]


_SYNTH_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.POINTER(_Event)
)


class Espeak:
    AUDIO_OUTPUT_SYNCHRONOUS = 2
    RATE, VOLUME, PITCH, RANGE, WORDGAP = 1, 2, 3, 4, 7
    EVENT_LIST_TERMINATED, EVENT_WORD = 0, 1
    CHARS_UTF8, ENDPAUSE = 0x1, 0x1000

    def __init__(self):
        name = ctypes.util.find_library("espeak-ng") or "libespeak-ng.so.1"
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            sys.exit("Error: eSpeak NG not found. Install it with: sudo apt install espeak-ng")
        lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.espeak_SetSynthCallback.argtypes = [_SYNTH_CALLBACK]
        lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        lib.espeak_SetParameter.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.espeak_ListVoices.argtypes = [ctypes.POINTER(_Voice)]
        lib.espeak_ListVoices.restype = ctypes.POINTER(ctypes.POINTER(_Voice))
        lib.espeak_Synth.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p,
        ]
        self.lib = lib
        self.sample_rate = lib.espeak_Initialize(self.AUDIO_OUTPUT_SYNCHRONOUS, 0, None, 0)
        if self.sample_rate <= 0:
            sys.exit("Error: could not initialise eSpeak NG.")
        self._callback = _SYNTH_CALLBACK(self._on_synth)  # keep a reference alive
        lib.espeak_SetSynthCallback(self._callback)
        self._pcm = bytearray()
        self._words = []

    def configure(self, voice: str, rate: int, pitch: int, pitch_range: int, gap: int, volume: int):
        if self.lib.espeak_SetVoiceByName(voice.encode()) != 0:
            sys.exit(f"Error: voice '{voice}' not found (see --list-voices).")
        for param, value in (
            (self.RATE, rate), (self.PITCH, pitch), (self.RANGE, pitch_range),
            (self.WORDGAP, gap), (self.VOLUME, volume),
        ):
            self.lib.espeak_SetParameter(param, value, 0)

    def _on_synth(self, wav, numsamples, events):
        if numsamples > 0:
            self._pcm += ctypes.string_at(wav, numsamples * 2)
        i = 0
        while events[i].type != self.EVENT_LIST_TERMINATED:
            if events[i].type == self.EVENT_WORD:
                self._words.append(events[i].audio_position * self.sample_rate // 1000 * 2)
            i += 1
        return 0

    def synth(self, text: str) -> tuple[bytes, list[int]]:
        """Return 16-bit mono PCM and the byte offset where each word starts."""
        self._pcm = bytearray()
        self._words = []
        data = text.encode("utf-8")
        err = self.lib.espeak_Synth(
            data, len(data) + 1, 0, 1, 0, self.CHARS_UTF8 | self.ENDPAUSE, None, None
        )
        if err != 0:
            raise RuntimeError(f"eSpeak NG synthesis failed (error {err})")
        return bytes(self._pcm), self._words

    def voices(self, variants: bool = False) -> list[tuple[str, str]]:
        spec = None
        if variants:
            spec = _Voice()
            spec.languages = b"variant"
        found = []
        array = self.lib.espeak_ListVoices(ctypes.byref(spec) if spec else None)
        i = 0
        while array[i]:
            v = array[i].contents
            ident = (v.identifier or b"").decode(errors="ignore")
            lang = (v.languages or b"")[1:].decode(errors="ignore")
            found.append((Path(ident).name if variants else lang, (v.name or b"").decode(errors="ignore")))
            i += 1
        return found


class Piper:
    """Piper neural TTS; voice models are downloaded on first use."""

    DEFAULT_VOICE = "en_US-danny-low"

    def __init__(self, voice: str, rate: int, volume: int):
        from piper import PiperVoice, SynthesisConfig

        model = PIPER_VOICES / f"{voice}.onnx"
        if not model.exists():
            from piper.download_voices import download_voice

            print(f"Downloading Piper voice '{voice}' (about 60-120 MB, one time only)...")
            PIPER_VOICES.mkdir(parents=True, exist_ok=True)
            try:
                download_voice(voice, PIPER_VOICES)
            except Exception as e:
                sys.exit(f"Error: could not download Piper voice '{voice}': {e}")
        self.voice = PiperVoice.load(model)
        self.sample_rate = self.voice.config.sample_rate
        # length_scale stretches speech; 175 wpm is Piper's natural pace.
        # Volume stays below 1.0 because Piper normalises audio to full scale.
        self.config = SynthesisConfig(length_scale=175 / rate, volume=0.9 * volume / 100)

    def synth(self, text: str) -> tuple[bytes, list[int]]:
        """Return 16-bit mono PCM; word positions are not reported."""
        pcm = b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text, self.config))
        return pcm, []

    @staticmethod
    def installed_voices() -> list[str]:
        return sorted(m.stem for m in PIPER_VOICES.glob("*.onnx"))


class DECtalk:
    """DECtalk through its `say` program, which writes raw PCM to stdout."""

    sample_rate = 11025
    VOICES = {
        "paul": "Perfect Paul (the Hawking voice)", "harry": "Huge Harry", "frank": "Frail Frank",
        "dennis": "Doctor Dennis", "betty": "Beautiful Betty", "ursula": "Uppity Ursula",
        "wendy": "Whispering Wendy", "rita": "Rough Rita", "kit": "Kit the Kid", "val": "Variable Val",
    }

    def __init__(self, voice: str, rate: int, pitch: int | None, pitch_range: int | None, volume: int):
        self.say = DECTALK_HOME / "say"
        if not self.say.exists():
            sys.exit(f"Error: DECtalk not found in {DECTALK_HOME}. Run ./install_dectalk.sh")
        voice = voice.lower()
        if voice not in self.VOICES:
            sys.exit(f"Error: unknown DECtalk voice '{voice}' (choose from: {', '.join(self.VOICES)}).")
        # [:np] selects Paul, [:nh] Harry, ...; [:dv ...] adjusts the voice
        prefix = f"[:n{voice[0]}]"
        settings = [f"ap {pitch}" if pitch is not None else "", f"pr {pitch_range}" if pitch_range is not None else ""]
        if any(settings):
            prefix += f" [:dv {' '.join(filter(None, settings))}]"
        self.cmd = [str(self.say), "-r", str(rate), "-v", str(volume), "-pre", prefix]

    def synth(self, text: str) -> tuple[bytes, list[int]]:
        """Return 16-bit mono PCM; DECtalk does not report word positions."""
        # [ ] and { } would be read as DECtalk phoneme/command syntax
        text = text.translate(str.maketrans("[]{}", "()()"))
        result = subprocess.run(self.cmd + ["-fo", "stdout:raw", "-a", text], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"DECtalk failed: {result.stderr.decode(errors='ignore').strip()}")
        return result.stdout[: len(result.stdout) // 2 * 2], []


def dectalk_available() -> bool:
    return (DECTALK_HOME / "say").exists()


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------

def write_wav(path, pcm: bytes, rate: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class Player:
    """Plays PCM through pw-play (PipeWire) or aplay; can be cut off instantly."""

    LATENCY = 0.15  # seconds between starting the player and hearing sound

    def __init__(self, rate: int, workdir: str):
        self.rate = rate
        self.file = str(Path(workdir) / "chunk.wav")
        if shutil.which("pw-play"):
            self.cmd = ["pw-play", self.file]
        elif shutil.which("aplay"):
            self.cmd = ["aplay", "-q", self.file]
        else:
            sys.exit("Error: no audio player found (need pw-play or aplay).")
        self.proc = None
        self.started = 0.0
        self.offset = 0

    def start(self, pcm: bytes, offset: int = 0):
        self.stop()
        self.offset = offset
        write_wav(self.file, pcm[offset:], self.rate)
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.monotonic()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> int:
        """Stop playback and return the approximate byte offset reached."""
        if self.proc is None:
            return self.offset
        elapsed = max(0.0, time.monotonic() - self.started - self.LATENCY)
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        self.proc = None
        return self.offset + int(elapsed * self.rate) * 2


class Keys:
    """Single-key input from the terminal, without echo or Enter."""

    def __init__(self):
        import termios
        import tty

        self.fd = sys.stdin.fileno()
        self._termios = termios
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)

    def close(self):
        self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self.saved)

    def read(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        data = os.read(self.fd, 16)
        if data.startswith(b"\x1b[C"):
            return "right"
        if data.startswith(b"\x1b[D"):
            return "left"
        return data[:1].decode(errors="ignore").lower()


def pause_before(pcm: bytes, pos: int, rate: int) -> int:
    """Byte offset just after the last short silence before pos (a word boundary)."""
    samples = array.array("h", pcm[: pos - pos % 2])
    frame = rate // 100  # 10 ms
    quiet_run = 0
    gap_end = 0
    i = len(samples) - frame
    while i >= max(0, len(samples) - int(rate * 1.5)):
        window = samples[i : i + frame]
        if max(window) < 800 and min(window) > -800:
            if quiet_run == 0:
                gap_end = i + frame
            quiet_run += 1
            if quiet_run == 3:
                return gap_end * 2
        else:
            quiet_run = 0
        i -= frame
    return max(0, len(samples) - rate // 2) * 2


def _status(index: int, total: int, text: str, paused: bool):
    width = shutil.get_terminal_size((80, 20)).columns
    icon = "⏸ " if paused else "▶ "
    help_text = "  [space] pause  [←/→] skip  [q] quit"
    head = f"{icon}{index + 1}/{total} "
    room = max(0, width - len(head) - len(help_text) - 1)
    preview = text if len(text) <= room else text[: max(0, room - 1)] + "…"
    sys.stdout.write(f"\r\033[K{head}{preview.ljust(room)}{help_text}")
    sys.stdout.flush()


def read_aloud(engine, player: Player, chunks: list[str], keys: Keys | None) -> bool:
    """Speak chunks in order. Returns False if the user quit."""
    # One worker does all synthesis, rendering the next paragraph while
    # the current one plays so there is no gap between them.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return _read_chunks(engine, player, chunks, keys, pool)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _read_chunks(engine, player, chunks, keys, pool) -> bool:
    ahead = None  # (index, future) of the prefetched paragraph
    i = 0
    while i < len(chunks):
        if ahead and ahead[0] == i:
            pcm, words = ahead[1].result()
        else:
            pcm, words = pool.submit(engine.synth, chunks[i]).result()
        ahead = (i + 1, pool.submit(engine.synth, chunks[i + 1])) if i + 1 < len(chunks) else None
        paused = False
        resume_at = 0
        if keys:
            _status(i, len(chunks), chunks[i], paused)
        player.start(pcm)
        while True:
            key = keys.read(0.05) if keys else None
            if not keys:
                time.sleep(0.05)
            if not paused and not player.running():
                i += 1
                break
            if key == " ":
                if paused:
                    player.start(pcm, resume_at)
                else:
                    reached = player.stop()
                    # resume from the start of the word being spoken
                    if words:
                        resume_at = max((w for w in words if w <= reached), default=0)
                    else:
                        resume_at = pause_before(pcm, reached, player.rate)
                paused = not paused
                _status(i, len(chunks), chunks[i], paused)
            elif key == "right":
                player.stop()
                i += 1
                break
            elif key == "left":
                player.stop()
                i = max(i - 1, 0)
                break
            elif key == "q":
                player.stop()
                return False
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="voice-files",
        description="Read text and Markdown files aloud (Markdown is spoken as plain text).",
        epilog="While reading: space = pause/resume, ←/→ = previous/next paragraph, q = quit.",
    )
    p.add_argument("files", nargs="*", help="text or Markdown files or URLs to read (GitHub links work)")
    p.add_argument("-e", "--engine", choices=["piper", "dectalk", "espeak"], default="piper",
                   help="speech engine (default: piper)")
    p.add_argument("-v", "--voice",
                   help=f"Piper: e.g. en_US-ryan-high (default: {Piper.DEFAULT_VOICE}); "
                        "DECtalk: paul, harry, ... (default: paul); "
                        "eSpeak: e.g. en-us+klatt3 (default)")
    p.add_argument("-s", "--speed", type=int, default=175, help="words per minute (default: 175)")
    p.add_argument("-p", "--pitch", type=int,
                   help="not for Piper. DECtalk: average pitch in Hz, 50-350 (Paul: 122); eSpeak: 0-99 (default: 40)")
    p.add_argument("--range", type=int, dest="pitch_range",
                   help="not for Piper. Pitch variation, lower = more monotone. DECtalk: 0-250 %% (default: 100); "
                        "eSpeak: 0-99 (default: 50)")
    p.add_argument("-g", "--gap", type=int, default=0,
                   help="eSpeak only: extra pause between words, in 10 ms units (default: 0)")
    p.add_argument("-a", "--volume", type=int, default=100, help="volume, 0-100 (default: 100)")
    p.add_argument("--list-voices", action="store_true", help="list the available voices, then exit")
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="print the text that would be spoken instead of speaking it")
    p.add_argument("-o", "--output", type=Path, help="save speech to a WAV file instead of playing it")
    p.add_argument("-t", "--to-txt", action="store_true",
                   help="write each input's plain text to a .txt file next to it, then exit")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    args = p.parse_args(argv)

    if not args.list_voices and not args.files:
        p.error("at least one file is required")
    if args.engine == "piper":
        for name, value in (("--pitch", args.pitch), ("--range", args.pitch_range), ("--gap", args.gap or None)):
            if value is not None:
                p.error(f"{name} is not supported by Piper (use -e dectalk or -e espeak)")
    if args.engine == "dectalk":
        limits = {"--speed": (75, 600), "--pitch": (50, 350), "--range": (0, 250)}
    else:
        limits = {"--speed": (80, 450), "--pitch": (0, 99), "--range": (0, 99)}
    limits |= {"--gap": (0, 100), "--volume": (0, 100)}
    for name, value in (
        ("--speed", args.speed), ("--pitch", args.pitch), ("--range", args.pitch_range),
        ("--gap", args.gap), ("--volume", args.volume),
    ):
        low, high = limits[name]
        if value is not None and not low <= value <= high:
            p.error(f"{name} must be between {low} and {high} for {args.engine}")
    return args


def make_engine(args):
    if args.engine == "piper":
        return Piper(args.voice or Piper.DEFAULT_VOICE, args.speed, args.volume)
    if args.engine == "dectalk":
        return DECtalk(args.voice or "paul", args.speed, args.pitch, args.pitch_range, args.volume)
    engine = Espeak()
    engine.configure(
        args.voice or "en-us+klatt3", args.speed,
        40 if args.pitch is None else args.pitch,
        50 if args.pitch_range is None else args.pitch_range,
        args.gap, args.volume,
    )
    return engine


def list_voices():
    print(f"Piper voices installed in {PIPER_VOICES} (default engine):")
    for name in Piper.installed_voices():
        print(f"  {name}")
    print("  Others download on first use with -v, e.g. -v en_GB-alan-medium. Full list:")
    print("  https://huggingface.co/rhasspy/piper-voices/tree/main")
    print()
    if dectalk_available():
        print("DECtalk voices, use with --engine dectalk:")
        for name, desc in DECtalk.VOICES.items():
            print(f"  {name:<12} {desc}")
        print()
    engine = Espeak()
    print("eSpeak NG voices (language code, name), use with --engine espeak:")
    for lang, name in sorted(engine.voices()):
        print(f"  {lang:<12} {name}")
    print("\nVariants (append with +, e.g. en-us+klatt3):")
    print("  " + ", ".join(sorted(v for v, _ in engine.voices(variants=True))))


def to_txt(texts, force: bool) -> int:
    status = 0
    for source, text in texts:
        path = source_path(source)
        # files from the web are written to the current directory
        target = Path(path.name or "index").with_suffix(".txt") if is_url(source) else path.with_suffix(".txt")
        if not is_url(source) and target.resolve() == path.resolve():
            print(f"Skipping '{path}': it is already a .txt file.", file=sys.stderr)
            continue
        if target.exists() and not force:
            print(f"Skipping '{source}': '{target}' exists (use --force to overwrite).", file=sys.stderr)
            status = 1
            continue
        target.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {target}")
    return status


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_voices:
        list_voices()
        return 0

    texts = []
    for path in args.files:
        try:
            text = load_text(path)
        except FileNotFoundError:
            print(f"Error: file '{path}' not found.", file=sys.stderr)
            continue
        except IsADirectoryError:
            print(f"Error: '{path}' is a directory.", file=sys.stderr)
            continue
        except UnicodeDecodeError:
            print(f"Skipping '{path}': not a UTF-8 text file.", file=sys.stderr)
            continue
        except urllib.error.HTTPError as e:
            if e.code == 404 and raw_url(path).endswith("/readme"):
                links = markdown_links(path)
                if links:
                    print(f"Error: '{path}' has no README. Markdown files in it:",
                          *(f"  {link}" for link in links), sep="\n", file=sys.stderr)
                else:
                    print(f"Error: no README found at '{path}' (the repository may be private or not exist).",
                          file=sys.stderr)
                continue
            print(f"Error: could not fetch '{path}': HTTP {e.code} {e.reason}.", file=sys.stderr)
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"Error: could not fetch '{path}': {getattr(e, 'reason', e)}.", file=sys.stderr)
            continue
        if not text:
            print(f"Skipping '{path}': nothing to read.", file=sys.stderr)
            continue
        texts.append((path, text))

    if not texts:
        return 1

    if args.print_only:
        for path, text in texts:
            if len(texts) > 1:
                print(f"=== {path} ===")
            print(text)
        return 0

    if args.to_txt:
        return to_txt(texts, args.force)

    engine = make_engine(args)

    if args.output:
        if args.output.exists() and not args.force:
            sys.exit(f"Error: '{args.output}' exists (use --force to overwrite).")
        pcm = b"".join(engine.synth(c)[0] for _, text in texts for c in chunk_text(text))
        write_wav(args.output, pcm, engine.sample_rate)
        print(f"Saved {len(pcm) / 2 / engine.sample_rate / 60:.1f} min of audio to {args.output}")
        return 0

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    keys = Keys() if interactive else None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            player = Player(engine.sample_rate, tmp)
            try:
                for path, text in texts:
                    print(f"Reading {path}")
                    finished = read_aloud(engine, player, chunk_text(text), keys)
                    if keys:
                        print()
                    if not finished:
                        break
            except KeyboardInterrupt:
                player.stop()
                print("\nStopped.")
                return 130
    finally:
        if keys:
            keys.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
