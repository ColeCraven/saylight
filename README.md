<p align="center">
  <img src="docs/images/icon.png" alt="" width="96">
</p>

<h1 align="center">Saylight</h1>

<p align="center"><b>Highlight it, and it says it.</b></p>

Highlight text anywhere on Windows, press <kbd>Ctrl</kbd> + <kbd>`</kbd>, and Saylight reads it in a natural voice
while each word lights up as it's spoken.

Saylight is built for dyslexic readers, especially anyone working through dense academic papers. It stays out of
the way until you need it, follows along with the voice so you never lose your place, and cleans up the mess that
text copied from PDFs usually brings with it.

<p align="center">
  <img src="docs/images/reading-light.png" alt="Saylight reading a paragraph, with the current sentence and word highlighted" width="49%">
  <img src="docs/images/reading-dark.png" alt="The same panel in dark mode" width="49%">
</p>

## Features

**One shortcut for everything.** Highlight text and press <kbd>Ctrl</kbd> + <kbd>`</kbd> to start reading. Press it
with nothing highlighted to show or hide the panel, or to pause and resume while it's reading.

**Type or paste your own text.** Click ✎ or the empty reading box, type or paste, then press **Read aloud**
(<kbd>Ctrl</kbd> + <kbd>Enter</kbd>). Highlight part of it and <kbd>Ctrl</kbd> + <kbd>`</kbd> reads just that part.
The box uses your reading font, size and background, so what you type looks like what you'll hear.

**Follow along as you listen.**
- A soft band marks the sentence being read, and a highlight follows the current word.
- Earlier sentences are dimmed slightly. Text you haven't reached is never dimmed, so you can read ahead of the voice.
- Scroll back to re-read anything; a *Back to current* button returns you to the voice.
- Click any sentence to read from there.

**Made for academic text.**
- Words split across PDF line breaks are rejoined ("compre- hension" becomes "comprehension").
- Ligatures (ﬁ, ﬂ), page headers and manuscript line numbers are cleaned up.
- Citations such as *(Smith et al., 2019)* and *[12]* can be skipped, and web links are read as "link".
- Sentence splitting understands *et al.*, *e.g.*, *Fig. 3*, decimals and statistics, so the highlight stays accurate.

**Reading display you can tune.** Choose the text size, font (Verdana, Segoe UI, Calibri or Sitka), letter and line
spacing, highlight style, and background (Cream, Peach, Soft blue, Soft dark, or automatic with your Windows theme).

<p align="center">
  <img src="docs/images/settings.png" alt="The Reading display settings page" width="49%">
  <img src="docs/images/expanded.png" alt="The larger reading view" width="49%">
</p>

**Simple controls.**
- Play and pause, previous and next sentence, and stop.
- **Clear** empties the reading box and anything you typed; Undo appears for 10 seconds.
- Speed from 0.5× to 2×; changes apply from the next sentence without distorting the voice.
- Volume control, and a larger reading view when you want more context.

**Unobtrusive by design.**
- The panel floats on top but never takes keyboard focus, so the app you're reading in stays active. Only the Type
  or paste box takes the keyboard, and only while it's open; it hands it back when you read or press <kbd>Esc</kbd>.
- A tray icon and a small status pill show what's happening while the panel is hidden.
- Follows Windows dark and light mode, high-contrast themes, text scaling and the *Animation effects* setting.

## Design choices

The reading display follows published guidance and research on dyslexia and reading, rather than guesswork:

| Choice | Why |
|---|---|
| Verdana at 18 px with 1.5 line spacing | Larger sizes help dyslexic readers ([Rello et al., 2013](https://doi.org/10.1145/2461121.2461125)). Verdana keeps I, l and 1 distinct, and it's on the [British Dyslexia Association](https://www.bdadyslexia.org.uk/advice/employers/creating-a-dyslexia-friendly-workplace/dyslexia-friendly-style-guide) recommended list. |
| Slightly wider letter *and* word spacing | Extra letter spacing helps some dyslexic readers ([Zorzi et al., 2012](https://pmc.ncbi.nlm.nih.gov/articles/PMC3396504)), but only when word spacing grows with it ([Galliussi et al., 2020](https://pmc.ncbi.nlm.nih.gov/articles/PMC7188700/)). |
| No "dyslexia font" by default | Specialist fonts show no benefit in controlled studies ([Wery & Diliberto, 2017](https://pmc.ncbi.nlm.nih.gov/articles/PMC5629233/); [Kuster et al., 2018](https://pmc.ncbi.nlm.nih.gov/articles/PMC5934461/)). |
| Text and audio together, with read-ahead | Dyslexic students read faster with synchronised text-to-speech, with no loss in comprehension ([Schneps et al., 2019](https://doi.org/10.1111/mbe.12180)). |
| Cream and warm backgrounds, never pure black on white | The BDA style guide recommends cream or pastel backgrounds, and warm backgrounds tested fastest in [Rello & Bigham (2017)](https://doi.org/10.1145/3132525.3132546). They're offered as comfort settings, not as a treatment. |
| Skipping citations | Mid-sentence citations separate the subject from its verb, which is hard to follow by ear. Skipped citations can be switched back on at any time. |

Every reading background keeps text contrast between 11:1 and 15:1 (WCAG AAA).

## Install

**Easiest:** download `Saylight.zip` from the [latest release](../../releases/latest), unzip it, and double-click
**Install Saylight.cmd**. It sets everything up, adds Saylight to the Start menu and starts it with Windows.
Run **Uninstall Saylight.cmd** to remove it.

**From source:**

```
pip install -r requirements.txt
python saylight_app.py --install
```

Requires Windows 10 or 11 and Python 3.10 or newer.

| Command | What it does |
|---|---|
| `python saylight_app.py --install` | Start with Windows, add a Start menu entry, and start now |
| `python saylight_app.py --uninstall` | Undo `--install` and quit |
| `python saylight_app.py --start` / `--quit` | Start or quit the running copy |

## Using it

| Action | How |
|---|---|
| Read highlighted text | <kbd>Ctrl</kbd> + <kbd>`</kbd> (the key left of <kbd>1</kbd>), or the ▶ button |
| Pause / resume | <kbd>Ctrl</kbd> + <kbd>`</kbd> with nothing highlighted, the ▶ button, or middle-click the tray icon |
| Show / hide the panel | <kbd>Ctrl</kbd> + <kbd>`</kbd> with nothing highlighted (when not reading), or click the tray icon |
| Jump to a sentence | Click it, or use the progress bar |
| Re-read | Scroll over the text, or press ⏮ to replay the sentence |
| Text size | <kbd>Ctrl</kbd> + scroll over the text, or the ⚙ Reading display page |
| Speed | − / + buttons, or scroll over the speed control |
| Media keys | Play/Pause, Next and Previous work while it's reading |
| Read your own text | Click ✎ or the empty reading box, type or paste, then <kbd>Ctrl</kbd> + <kbd>Enter</kbd> or **Read aloud**. Highlight part of it to hear just that part. <kbd>Esc</kbd> goes back and keeps your text |
| Clear the text | The eraser button, or *Clear text* in the tray menu. **Undo** appears for 10 seconds |

## Privacy

The voice is Microsoft's online neural voice (the same voices as Microsoft Edge's Read Aloud), accessed through the
open-source [edge-tts](https://github.com/rany2/edge-tts) library. That means **the text you have read aloud is sent
to Microsoft** to be turned into speech, so avoid using it on confidential material. Saylight itself stores no text:
it's kept in memory only while you're reading, and the log records counts, never content.

Text you type or paste stays in memory until you clear it or quit Saylight. It is never saved, and it is sent to
Microsoft only when you have it read aloud.

## Development

```
saylight_app.py          entry point and command-line options
saylight/
  academic_text.py       cleans copied text and splits sentences
  text_model.py          sentences, synthesis chunks and word timing
  engine.py              the reading state machine
  synth.py, player.py    speech generation (edge-tts) and playback (Windows MCI)
  input_thread.py        global shortcut and copying the selection
  settings.py, theme.py  saved settings, colours and fonts
  ui/                    the panel, controls, reading view, tray, status pill and tooltips
  ui/compose.py          the Type or paste box
scripts/build_release.py builds the installer zip into dist/
```

Run the test suite (about 900 tests) from the project folder:

```
python -m unittest discover -s saylight/tests -t .
```

## Acknowledgements

- [edge-tts](https://github.com/rany2/edge-tts) for access to the neural voices (LGPL-3.0)
- [Qt for Python (PySide6)](https://doc.qt.io/qtforpython-6/) for the interface (LGPL-3.0)
- Segoe Fluent Icons and Segoe UI Variable, which ship with Windows 11

Saylight is an independent project and is not affiliated with Microsoft.

## License

[MIT](LICENSE) © 2026 Cole Craven
