# Calibre book summarizer

Calibre plugin that summarizes selected book text into a configured custom column
(`#summary` by default). Provider credentials remain in Calibre preferences or the
existing environment/provider stores.

- Check: `python test_providers.py`
- Build: `python build.py`
- Install explicitly: `python build.py --install` (close Calibre first).

The source files are canonical; the ZIP is a generated artifact.

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
