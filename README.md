# AI Book Summarizer — Calibre Plugin

Summarizes books in your Calibre library using AI APIs (Gemini, OpenAI, Anthropic, MiniMax) and stores the result in a custom column.

## Features

- **Multi-Provider Support**: Use Google Gemini, OpenAI, Anthropic Claude, or MiniMax
- **Context-Aware Splitting**: Automatically splits large books when content exceeds 80% of model's context window
- **Summarizes** EPUB, TXT, HTML, and MOBI books
- **Configurable** model, prompt template, and word limit (default: 2000 words)
- **Writes summaries** to any custom Long Text column (default: `#summary`)
- **Batch-processes** multiple selected books with a progress dialog

---

## Installation

### 1. Install the plugin

In Calibre: **Preferences → Plugins → Load plugin from file** → select `AISummarizer.zip`

Restart Calibre.

### 2. Create the custom column

**Preferences → Add your own columns → Add column**

| Field | Value |
|-------|-------|
| Column id | `summary` |
| Column heading | `Summary` |
| Type | Long text / HTML |

Calibre stores it as `#summary`. Restart Calibre after adding.

### 3. Configure

**Preferences → Plugins → AI Book Summarizer → Customize plugin**

- Select your AI provider (Gemini, OpenAI, Anthropic, or MiniMax)
- Paste your API key
- Select model
- Verify column name is `#summary`

### 4. Use

Select one or more books → click **AI Summarize** in the toolbar.

---

## GitHub Releases — How to Release & Update

### Repository structure

```
AISummarizer/              ← this folder becomes the plugin source
├── __init__.py
├── action.py
├── config.py
├── jobs.py
├── summarizer.py
├── images/
│   └── icon.png
├── plugin-import-name-ai_summarizer.txt
└── README.md
.github/
└── workflows/
    └── release.yml        ← auto-builds the zip on every version tag
```

### One-time setup

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/calibre-ai-summarizer.git
git add .
git commit -m "Initial release"
git push -u origin main
```

### How to publish a new release

**1. Bump the version** in `__init__.py`:
```python
version = (2, 0, 0)   # change this
```

**2. Commit and tag:**
```bash
git add __init__.py
git commit -m "Release v2.0.0"
git tag v2.0.0
git push origin main --tags
```

**3. GitHub Actions automatically:**
- Zips the plugin source into `AISummarizer.zip`
- Creates a GitHub Release named `v2.0.0`
- Attaches the zip as a downloadable asset

Users can then download the zip directly from the **Releases** page.

---

## Providers and Models

### Google Gemini

| Model | Context Window | Release Date |
|-------|---------------|-------------|
| `gemini-3.1-flash` | 1M tokens | - |
| `gemini-3.1-pro` | 1M tokens | 2026-02-19 |

Get API key: [Google AI Studio](https://aistudio.google.com/app/apikey)

### OpenAI

| Model | Context Window |
|-------|---------------|
| `gpt-5.4` | 256k tokens |
| `gpt-5.4-mini` | 256k tokens |

Get API key: [OpenAI Platform](https://platform.openai.com/api-keys)

### Anthropic Claude

| Model | Context Window | Release Date |
|-------|---------------|-------------|
| `claude-opus-4.7` | 200k tokens | 2026-04-16 |
| `claude-sonnet-4.6` | 200k tokens | 2026-02-17 |
| `claude-haiku-4.5` | 200k tokens | 2025-10-15 |

Get API key: [Anthropic Console](https://console.anthropic.com/settings/keys)

### MiniMax

| Model | Context Window | Release Date |
|-------|---------------|-------------|
| `MiniMax-M2.7` | 204.8k tokens | 2026-03 |

Get API key: [MiniMax Platform](https://platform.minimaxi.com)

---

## Context Window and Text Splitting

When a book's text exceeds 80% of the selected model's context window, the plugin automatically:

1. Splits the text into chunks
2. Summarizes each chunk individually
3. Combines and re-summarizes the chunk summaries

This ensures comprehensive coverage even for very long books.

---

## License

Apache
