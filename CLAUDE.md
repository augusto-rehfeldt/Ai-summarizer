# AI Book Summarizer (Calibre plugin)

## What This Is
Calibre InterfaceAction plugin: summarizes selected books with an LLM and writes the
result to a custom column (`#summary`). Text comes out of the book file itself
(TXT/EPUB/MOBI/PDF/HTML), never from metadata.

## Non-Negotiables
- The plugin only ever writes the configured custom column (falling back to comments);
  nothing else in the library is touched.
- Never commit `ai_summarizer.json` or any API key. Calibre stores prefs (including
  pasted keys) in `%APPDATA%/calibre/plugins/ai_summarizer.json`, outside this repo.
- `providers.py` must stay free of `calibre`/`qt` imports — it is the only file the
  offline checks can import.

## Commands
- Checks: `python test_providers.py`
- Build zip: `python build.py`
- Build + install into Calibre: `python build.py --install` (Calibre must be closed, restart after)

## Layout
| File | Role |
|---|---|
| `__init__.py` | `InterfaceActionBase`, version, config widget hookup |
| `action.py` | Toolbar action: selection, key check, custom-column check, confirm |
| `config.py` | Qt config dialog (provider, key, base URL, model, context, prompt) |
| `jobs.py` | `SummarizerWorker` (extraction, chunking, retries) + progress dialog |
| `providers.py` | Provider table, key resolution, model listing, request/response shaping |

## Gotchas
- **A provider is a row in `providers.PROVIDERS`, not a code path.** Four request
  shapes exist (`style`): `openai` (`/chat/completions`), `anthropic` (`/messages`),
  `gemini` (`/models/{model}:generateContent`) and `cli` (no HTTP at all —
  `providers.run_cli()` drives a coding CLI). Adding a gateway means adding a
  row; nothing in `jobs.py` should learn a provider's name. `jobs._call_api()` has the
  single `style == 'cli'` branch and `build_request()` raises for that style rather
  than silently posting to `/chat/completions`. CLI invocation details (executable
  candidates, flags, how the neutral system prompt gets in) live per provider in
  `providers.CLI_SPECS`.
- **A model id in a row goes stale and only fails at request time.** Verified live on
  2026-08-30: `gemini-3.1-pro` and `google/gemini-3.1-pro` are 404s — Google publishes
  `gemini-3.1-pro-preview` and `gemini-3.5-flash`, OpenRouter publishes
  `google/gemini-3.1-pro-preview`. The default gemini model is now `gemini-3.5-flash`
  because the pro ids are quota-limited on a free key. Re-check with "Fetch models"
  before trusting anything in the static lists.
- **Every OpenAI-style request must carry a token cap.** Uncapped, OpenRouter prices
  the model's whole output budget up front and answers
  `402 … you requested up to 65536 tokens, but can only afford 1804` on an account
  with credit enough for the summary actually asked for. The cap is the same
  `max(4096, max_words * 2)` Anthropic already got. `token_param` on the row is the
  spelling: gateways take `max_tokens`, OpenAI's own API wants
  `max_completion_tokens` and 400s on the older name.
- **`needs_key: False` marks a provider that authenticates elsewhere** (`claude-cli`
  on the user's subscription, `command-code` on the Command Code plan, `openai-oauth`
  through the local proxy). `action.py`
  checks `P.needs_key()` before refusing to run, and the config dialog says so instead
  of nagging for a key that does not exist. `openai-oauth` talks to the local proxy on
  `127.0.0.1:10531`; `providers.ensure_openai_oauth_proxy()` starts it when nothing is
  listening (`npx --yes openai-oauth@latest --detach` — the same command book-writer's
  AIService runs, so one proxy serves both), and `jobs._call_api()` plus the config
  dialog's Check/Fetch buttons call it via the row's `proxy_start` flag.
- **Command Code's executable must never be resolved as `cmd`**: on Windows that is
  `cmd.exe`. `CLI_SPECS['command-code']['exe']` tries `cmdc`, `command-code`,
  `commandcode`, in that order. `cmd -p` has no `--append-system-prompt`, so
  `run_cli()` prepends the neutral instruction to the prompt instead; it also passes
  `--skip-onboarding --no-skills --no-session` and maps Command Code's distinct exit
  codes (3 auth, 5 rate limit, 10 credits) to readable errors.
- **`claude -p` inherits every CLAUDE.md and plugin rule it can find**, and they end
  up in the summary: measured 2026-08-30, a summary came back written in a plugin's
  caveman register. `run_cli()` runs in `tempfile.gettempdir()` so no project file is
  read, and appends `_CLI_SYSTEM` to override the global one, which nothing can
  unload. Re-check the register if that prompt is ever edited.
- **A Claude Code quota notice arrives on stdout with exit code 0**, looking exactly
  like a completion — and would be written straight into the book's `#summary`.
  `run_cli()` raises on a short output matching `_QUOTA`; the length guard is what
  keeps a book *about* limits from being thrown away.
- **Keys are resolved, not just read** (`resolve_key`): the config dialog's field, then
  `key_envs`, then the login `opencode`'s CLI stored in
  `~/.local/share/opencode/auth.json`, then the one Crush stored in `crush.json` under
  `%LOCALAPPDATA%`/`~/.local/share`/`~/.config`. Same order as book-watch, so Hyper and
  OpenCode Zen work with an empty key field. Zen and Go are separate rows with separate
  `opencode_auth` names — the zen row must not fall back to the `opencode-go` key, or a
  subscription token gets spent against the pay-as-you-go endpoint. Calibre's GUI does not read `.env` files,
  so an env var must be set for the user session, not just a shell.
- **Hyper's stored key is an OAuth access token with a one-hour life.** `crush_entry()`
  prefers `oauth.access_token` over `api_key` and reports `expired`; the config dialog
  says so. There is no refresh here — run `crush` to get a fresh one.
- **OpenCode Zen 403s a default urllib agent** (Cloudflare), so every request carries
  `BROWSER_UA`. Without it `/models` returns 403 and `list_models()` silently falls back
  to the small static list.
- **`list_models()` never raises**: a dead endpoint returns the static `models` list from
  the provider row, so the config dialog always has something to show. When the gateway
  does answer, static entries it no longer publishes are merged back in (first seen
  wins), and non-chat ids (`tts`, `whisper`, `dall-e`, `moderation`, `audio`, …) are
  filtered out. The model combo is
  editable — gateways add models faster than this table does. The dialog fetches
  automatically when it opens and when the provider changes (`fetch_models(auto=True)`),
  but the auto pass skips what would only hang or surprise: CLI rows, starting the
  openai-oauth proxy (that can open a browser sign-in), and providers with no
  resolvable key.
- **Static `models` lists were re-checked against each live API on 2026-09-22**
  (hyper, OpenCode Zen, OpenCode Go, OpenRouter, gemini, minimax with resolvable
  keys; openai-oauth against the running proxy; openai/grok additions corroborated
  by OpenRouter's catalog since no key resolved locally). Dead ids were dropped,
  current ones added. Hyper caps some families below the model's native window, so
  its row carries its own `model_contexts` (e.g. `minimax-m3` 512k, `glm-5.2` 1M).
  The openrouter and groq rows deliberately stay vendor-prefixed and window-less:
  their live `/models` publishes `context_length`, so the auto-fetch fills them
  in (OpenRouter's is even public — no key needed).
- **`validate_key()` is the cheap "does this key work" check** behind the config
  dialog's Check button: one GET on `/models` (never raises; 401/403 means a rejected
  key, anything else is a warning). CLI providers report "no key needed" without any
  network call.
- **Context window drives chunking, not the request.** `_check_context_split_needed()`
  splits a book into chunk summaries plus a synthesis pass when the text passes 80% of
  the window. Unknown models fall back to `DEFAULT_CONTEXT_WINDOW` (100k), which
  over-chunks a 1M-token model — the config dialog's "Model context" spinbox overrides
  it, and gets filled automatically when the fetched list publishes one
  (`context_length`/`limit.context`; OpenCode Zen publishes neither). The flat table
  was re-verified against OpenRouter's published `context_length` on 2026-09-22:
  `gpt-5.6-terra`/`gpt-5.6-luna`, the `deepseek-v4` family and `glm-5.2`/`glm-5.3`
  had been missing entirely (so openai-oauth chunked terra at 100k), and
  gpt-5.4/minimax/kimi/qwen/grok windows were stale. A row can carry
  its own `model_contexts` map (the flat `MODEL_CONTEXT_WINDOWS` is keyed by bare id
  and cannot tell Command Code's 1M `claude-sonnet-5` from the 200k one elsewhere).
- **Reasoning models leak their thinking into `content`.** `clean_text()` strips
  explicit `<think>`/`<thinking>` blocks and a leading `SUMMARY:` label only.
  Ordinary prose prefixes and in-body `SUMMARY:` text are preserved. Cleanup applies
  to every provider, because gateways route to whatever model they like.
- **Anthropic requires `max_tokens`**; it is derived from the configured summary length
  (`max_words * 2`, floor 4096). The old hardcoded 2048 truncated any summary over
  ~1000 words.
- **The source of truth is this folder, not the installed zip.** Calibre only ever sees
  `AI Book Summarizer.zip`; `build.py --install` copies it over, keeping a `.zip.bak`.
