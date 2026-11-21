"""Build a manual-install package: dist/fishing.zip (+ dist/fishing-card.js).

HACS does not need this (it installs straight from the repository); it is for copying to a machine by hand.

    python scripts/package.py            # writes into ./dist

The zip's top-level folder is `fishing/`, so it unzips straight into `/config/custom_components/`.
The Lovelace card lives inside the integration (`custom_components/fishing/www/fishing-card.js`), so a HACS
install and this zip carry it in the same place, which is where `card.py` serves it from. Test files, caches
and dev environments are never included.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "fishing"
CARD = INTEGRATION / "www" / "fishing-card.js"


def files() -> list[tuple[Path, str]]:
    """(source, path inside the zip) for everything that ships."""
    return [
        (p, f"fishing/{p.relative_to(INTEGRATION).as_posix()}")
        for p in sorted(INTEGRATION.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]


def build(out_dir: Path | None = None) -> Path:
    out_dir = out_dir or ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "fishing.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for source, name in files():
            z.write(source, name)
    (out_dir / "fishing-card.js").write_bytes(CARD.read_bytes())
    return target


if __name__ == "__main__":
    path = build(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    with zipfile.ZipFile(path) as z:
        print(f"{len(z.namelist())} files -> {path} ({path.stat().st_size / 1024:.0f} KB)")
