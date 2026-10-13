# Receipts

Your friend says something confidently wrong in Discord. You hit **Ctrl+Alt+F**. A little window pops up
with what he just said, a verdict, the facts, a line you can read out loud, and sources.

How it works: Receipts sits in the system tray and keeps the last ~3 minutes of one audio source in memory
(for you, the **SteelSeries Sonar - Chat** output, which carries only Discord). When you press a hotkey it
takes the last 45 seconds, transcribes them, and has Claude or Grok (your pick) fact-check the claim with
live web search.

## Setup (Windows)

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/). In the installer, tick
   **"Add python.exe to PATH"**.
2. Get an API key for the AI you want to use:
   - **Claude**: [console.anthropic.com](https://console.anthropic.com) → API keys
   - **Grok**: [console.x.ai](https://console.x.ai) → API keys
3. Put this `Receipts` folder anywhere on your PC and double-click **`Start Receipts.bat`**.
   The first run installs everything into a `.venv` folder (a few minutes). After that it starts instantly.
4. The window pops up in the top-right corner. Click **⚙**, pick **Claude** or **Grok** at the top, paste that
   key, and Save. You can store both keys and switch whenever you like.
5. Check the audio. In ⚙ Settings, **Audio source** should be *Auto* (it picks "SteelSeries Sonar - Chat" by
   itself) or pick that output yourself. Have someone talk in Discord: the **Level** bar should bounce, and
   the dot next to the title turns green.

The first fact-check also downloads the speech model (~500 MB) once.

To start it with Windows, press `Win+R`, type `shell:startup`, and drop a shortcut to `Start Receipts.bat` there.

## Using it

| Hotkey | What it does |
|---|---|
| **Ctrl+Alt+F** | Fact-check: claim, ✅/❌/⚠️ verdict, key facts, a "Say this:" line |
| **Ctrl+Alt+D** | Argue back: debate-coach counterpoints, even if he's partly right |
| **Ctrl+Alt+R** | Roast: the verdict delivered as a (friendly) roast |
| **Ctrl+Alt+W** | Show / hide the window |

- The popup doesn't steal focus from your game or Discord. Click into it if you want to type.
- Type in the box to follow up ("source for the second point?", "what about X?"), or type a claim yourself.
- **Esc** or ✕ hides the window. Quit from the tray icon (the little gold receipt).
- Drag the window by its header. Resize it from the bottom-right corner. It remembers where you put it.

**Stream Deck:** add a *System → Hotkey* action and set it to the same keys (for example Ctrl+Alt+F).
That gives you a physical "FACT CHECK" button.

## Updates

Receipts checks GitHub for a new version when it starts. When one is out, a gold **⬆ Update** button appears
at the top of the window. Click it and Receipts downloads the new version, swaps it in, and restarts. Your
settings, keys and prompts are kept. You can also check by hand: tray icon → **Check for updates**, or
⚙ Settings → **Check for updates**. If an update needs new packages, a console window shows them installing
during the restart.

## Tweaking

Everything is in **⚙ Settings**:

- **Grab the last N seconds**: 45 by default. Shorter means less unrelated chatter in the transcript.
- **Speech to text**: *On this PC* is free and private. With an NVIDIA GPU, set *Run on* to CUDA and the model
  to `large-v3-turbo` for better accuracy (needs the CUDA 12 / cuDNN 9 libraries). On a slow PC, the
  *Groq API* option is very fast and has a free tier.
- **AI**: Claude or Grok. For Claude, `claude-opus-5-5` on *Low* effort is the default; `claude-sonnet-5-5` is
  cheaper and `claude-haiku-5-5` is the fastest and cheapest. For Grok, `grok-4.7` is the default and `grok-4.3`
  is cheaper.
- **Hotkeys**: any combination, written like `<ctrl>+<alt>+f` or `<ctrl>+<shift>+<f9>`.
- **Edit prompts…** opens `config.json` (in `%APPDATA%\Receipts`). Each mode's `prompt` is plain text, so
  change the tone, add a mode (give it a new `id`), or change what it focuses on. Receipts picks up the
  changes as soon as you save the file.

## Cost

Each check is one AI request plus a few web searches, so it costs a few cents. Cheaper models (Sonnet,
Haiku, grok-4.3) cost less. Local transcription is free.

## Troubleshooting

- **"Didn't hear anything"**: the wrong audio source is selected. Open ⚙ Settings and watch the Level bar while
  someone talks.
- **The window doesn't show over my game**: games in *exclusive* fullscreen draw over everything. Switch the
  game to *borderless windowed*.
- **A hotkey does nothing**: another app may already use that combination. Pick a different one in ⚙ Settings.
- Anything else: ⚙ Settings → **Open log**.

## Privacy

Audio only lives in memory (a rolling ~3-minute buffer) and is never saved to disk. Nothing leaves your PC
until you press a hotkey. Then the clip is transcribed (locally by default) and only the transcript text goes
to the AI you picked. It's still polite to let the group know you've got a fact-checker running.
