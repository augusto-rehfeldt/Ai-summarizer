from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "ai_summarizer.zip"

FILES = [
    "__init__.py",
    "action.py",
    "config.py",
    "jobs.py",
    "summarizer.py",
    "plugin-import-name-ai_summarizer.txt",
    "icon.png",
    "images/icon.png",
]


def main() -> None:
    missing = [name for name in FILES if not (ROOT / name).exists()]
    if missing:
        raise FileNotFoundError(
            "Cannot build zip because these files are missing: " + ", ".join(missing)
        )

    if OUTPUT.exists():
        OUTPUT.unlink()

    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED) as zf:
        for name in FILES:
            src = ROOT / name
            arcname = Path(name).as_posix()
            zf.write(src, arcname=arcname)

    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
