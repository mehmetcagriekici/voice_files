# voice-files

Read text and Markdown files aloud from the terminal, offline, with a
natural-sounding [Piper](https://github.com/OHF-Voice/piper1-gpl) neural voice.
DECtalk and eSpeak NG are available as alternative engines.

Markdown is detected by extension (`.md`, `.markdown`, …) or, for files with
other extensions, by its content. It is spoken as plain text: code blocks,
images, link URLs, HTML and formatting symbols are skipped.

## Install

Needs PipeWire (`pw-play`) or ALSA (`aplay`) for playback.

```sh
pipx install .          # or: pip install -e .
```

The default voice (`en_US-lessac-medium`, about 60 MB) is downloaded to
`~/.local/share/piper-voices` the first time you run the app.

Optional engines:

```sh
./install_dectalk.sh        # DECtalk into ~/.local/share/dectalk (no sudo)
sudo apt install espeak-ng  # eSpeak NG
```

`install_dectalk.sh` downloads the community build from
[github.com/dectalk/dectalk](https://github.com/dectalk/dectalk). That code
descends from leaked DEC source, so its licence status is unclear.

## Usage

```sh
voice-files notes.md                  # read aloud
voice-files a.txt b.md                # several files in order
voice-files notes.md -s 220           # speed in words per minute
voice-files notes.md -v en_US-ryan-high   # another Piper voice (downloads once)
voice-files notes.md -e dectalk -v paul   # DECtalk's Perfect Paul (Hawking voice)
voice-files notes.md -e espeak        # eSpeak NG
voice-files notes.md --print          # show the text that would be spoken
voice-files notes.md -t               # write notes.txt next to it
voice-files notes.md -o notes.wav     # save to a WAV file
```

While reading:

| Key       | Action                         |
|-----------|--------------------------------|
| space     | pause / resume                 |
| ← / →     | previous / next paragraph      |
| q, Ctrl+C | quit                           |

Piper voices are listed at
[huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main)
and named like `en_US-lessac-medium`. Piper supports speed and volume.
Pitch (`-p`) and pitch range (`--range`) need DECtalk (Hz and %) or eSpeak
(0-99); `-g` adds gaps between words with eSpeak. DECtalk voices: paul
(default), harry, frank, dennis, betty, ursula, wendy, rita, kit, val.
