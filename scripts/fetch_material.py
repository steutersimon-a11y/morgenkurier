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

from feeds import FEEDS, fetch_all_feeds, material_block
from storylines import RETENTION_DAYS, format_ledger_markdown, load_ledger

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
        # Kein harter Abbruch: bei ~110 Feeds ist es plausibel, dass die
        # Netzwerk-Policy der Cloud-Umgebung ALLE blockiert (z.B. bei
        # "Trusted" statt "Full" Network Access). Die Redaktion faellt in
        # diesem Fall auf WebSearch zurueck (editorial_brief.md, Schritt 0).
        print(
            "Warnung: keine einzige RSS-Quelle hat geantwortet - vermutlich "
            "blockiert die Netzwerk-Policy der Umgebung externe Domains. "
            "Weiter mit leerem Rohmaterial, Recherche laeuft dann komplett "
            "ueber WebSearch.",
            file=sys.stderr,
        )

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    header = (
        f"# Rohmaterial fuer den Morgenkurier\n\n"
        f"Redaktionsschluss: {german_date(now_berlin)}, "
        f"{now_berlin.strftime('%H:%M')} Uhr (Europe/Berlin)\n"
        f"Quellen, die geantwortet haben: {len(all_items)} von {len(FEEDS)}\n\n"
        f"---\n\n"
        f"## Laufende Geschichten (letzte {RETENTION_DAYS} Tage)\n\n"
        f"Themen, die in juengeren Ausgaben schon liefen. Nutze dieselbe "
        f"story_id, wenn ein heutiges Thema eine dieser Geschichten "
        f"fortsetzt (siehe editorial_brief.md).\n\n"
        f"{format_ledger_markdown(load_ledger())}\n\n"
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
