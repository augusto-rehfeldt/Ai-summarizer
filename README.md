# Calibre book summarizer

Calibre plugin that summarizes selected book text into a configured custom column
(`#summary` by default). Provider credentials remain in Calibre preferences or the
existing environment/provider stores.

- Check: `python test_providers.py`
- Build: `python build.py`
- Install explicitly: `python build.py --install` (close Calibre first).

The source files are canonical; the ZIP is a generated artifact.

## Providers

Configured as rows in `providers.PROVIDERS`; the config dialog shows the key
source for each. Supported:

- **Command Code CLI** and **Claude Code CLI** — run on your existing plan,
  no API key. The executable is looked up on PATH, with a fallback probe of
  standard Node install dirs (`%APPDATA%\npm`, `C:\nvm4w\nodejs`,
  `%LOCALAPPDATA%\nvm\*\nodejs`, `Program Files\nodejs`) so a stale GUI PATH
  does not break the run.
- **openai-oauth** — talks to a local proxy on `127.0.0.1:10531`; the plugin
  starts it (`npx openai-oauth@latest --detach`) when nothing is listening.
- **HTTP gateways** — Hyper (Charm), OpenCode Zen (incl. Go), OpenRouter,
  Anthropic, OpenAI, Google Gemini, xAI Grok, Groq, MiniMax. Keys come from
  the config dialog, the environment, or the gateway CLI's own stored login
  (`opencode` / `crush`).
- Free, no-cost models are available on Command Code (`poolside/laguna-s-2.1-free`,
  `inclusionai/ling-3.0-flash-sante:free`) and cheap flash tiers elsewhere.

## Browse summaries in Story Atlas

After summarizing books, run from `../book-watch`:

```powershell
python book_watch.py export-atlas --output data/atlas/library.csv
```

This reads the library configured in book-watch without changing it. It supports
both normalized text columns and direct comments columns, falling back to ordinary
book comments when a summary is absent. Use `--summary-column '#your_column'` for
a different plugin setting. Then run from `../semantic-story-atlas`:

```powershell
python backend/app.py --stories ../book-watch/data/atlas
```
