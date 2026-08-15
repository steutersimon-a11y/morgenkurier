"""Schritt 1 der taeglichen Pipeline: Rohmaterial beschaffen.

Laedt alle RSS-Feeds und legt zwei Dateien in build/ ab:
  - material.md   das gesamte Rohmaterial als Markdown (Claudes Faktenbasis)
  - sources.json  die Liste der Quellen, die heute wirklich geantwortet haben

Die Quellenliste wird bewusst hier festgehalten und nicht von Claude
zurueckgemeldet: die Quellenangabe im Impressum soll ein gemessener Fakt
sein, kein Modell-Output.

Aufruf: python scripts/fetch_material.py"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from feeds import fetch_all_feeds, material_block

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "build"

WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def main() -> None:
    BUILD_DIR.mkdir(exist_ok=True)

    all_items = fetch_all_feeds()
    if not all_items:
        print("Fehler: keine einzige Quelle hat geantwortet.", file=sys.stderr)
        sys.exit(1)

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    header = (
        f"# Rohmaterial fuer den Morgenkurier\n\n"
        f"Redaktionsschluss: {german_date(now_berlin)}, "
        f"{now_berlin.strftime('%H:%M')} Uhr (Europe/Berlin)\n"
        f"Quellen, die geantwortet haben: {len(all_items)} von {len(all_items)}\n\n"
        f"---\n\n"
    )
    material = header + material_block(all_items)

    (BUILD_DIR / "material.md").write_text(material, encoding="utf-8")
    (BUILD_DIR / "sources.json").write_text(
        json.dumps(sorted(all_items.keys()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    total_items = sum(len(v) for v in all_items.values())
    print(
        f"Rohmaterial geschrieben: {len(all_items)} Quellen, {total_items} Meldungen, "
        f"{len(material):,} Zeichen -> build/material.md"
    )


if __name__ == "__main__":
    main()
