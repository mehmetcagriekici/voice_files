# voice-files

Read text and Markdown files aloud from the terminal, offline, in DECtalk's
"Perfect Paul" voice (the Stephen Hawking voice), or eSpeak NG as a fallback.

Markdown is detected by extension (`.md`, `.markdown`, …) or, for files with
other extensions, by its content. It is spoken as plain text: code blocks,
images, link URLs, HTML and formatting symbols are skipped.

## Install

Needs PipeWire (`pw-play`) or ALSA (`aplay`) for playback.

```sh
pipx install .          # or: pip install -e .
./install_dectalk.sh    # DECtalk into ~/.local/share/dectalk (no sudo)
```

`install_dectalk.sh` downloads the community build from
[github.com/dectalk/dectalk](https://github.com/dectalk/dectalk). That code
descends from leaked DEC source, so its licence status is unclear. Without
DECtalk the app falls back to eSpeak NG (`sudo apt install espeak-ng`).

## Usage

```sh
voice-files notes.md                  # read aloud
voice-files a.txt b.md                # several files in order
voice-files notes.md -s 220           # speed in words per minute
voice-files notes.md -p 100           # lower pitch (Hz; Paul is 122)
voice-files notes.md --range 40       # flatter, more monotone
voice-files notes.md -v harry         # another voice (see --list-voices)
voice-files notes.md -e espeak        # use eSpeak NG instead
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

DECtalk voices: paul (default), harry, frank, dennis, betty, ursula, wendy,
rita, kit, val. With `-e espeak`, `-v` takes eSpeak voices such as
`en-us+klatt3` (default), `-p` is 0-99, and `-g` adds gaps between words.
