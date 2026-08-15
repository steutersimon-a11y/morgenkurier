"""Storyline-Kontinuitaet ueber mehrere Ausgaben hinweg.

Die Routine startet jeden Morgen mit einem frischen Checkout und hat sonst
kein Gedaechtnis an gestern. Damit ein Thema, das laenger laeuft (z.B. ein
Waldbrand ueber mehrere Tage), als Fortsetzung erkennbar bleibt statt jeden
Tag neu aufgerollt zu werden, wird eine kurze Liste laufender Geschichten
versioniert im Repo abgelegt: data/story_ledger.json (bewusst NICHT unter
build/, das ist gitignored und ueberlebt keinen neuen Checkout).

Ablauf:
  1. fetch_material.py liest die Liste (load_ledger/format_ledger_markdown)
     und zeigt sie Claude vor der Recherche.
  2. Claude vergibt in Ressorts mit fortlaufendem Thema eine stabile
     story_id (wiederverwendet aus der Liste) und schreibt optional eine
     ueberleitende vorgeschichte-Zeile.
  3. render_issue.py liest nach erfolgreichem Rendern die neuen story_ids
     aus content.json und schreibt die Liste fort (update_ledger).

Format je Eintrag: {"story_id": str, "headline": str, "ressort": str,
"date": "YYYY-MM-DD"}."""

import json
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "data" / "story_ledger.json"

RETENTION_DAYS = 10
MAX_ENTRIES = 40


def load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def format_ledger_markdown(ledger: list[dict]) -> str:
    if not ledger:
        return (
            "(Noch keine laufenden Geschichten erfasst - entweder die erste "
            "Ausgabe, oder bisher hat kein Artikel eine story_id vergeben.)"
        )
    rows = sorted(ledger, key=lambda e: e.get("date", ""), reverse=True)
    return "\n".join(
        f"- [{e.get('date', '?')}] {e.get('ressort', '?')} — "
        f"\"{e.get('headline', '?')}\" (story_id: `{e.get('story_id', '?')}`)"
        for e in rows
    )


def extract_todays_entries(content: dict, date_iso: str) -> list[dict]:
    entries = []
    for r in content.get("ressorts") or []:
        haupt = r.get("hauptartikel") or {}
        story_id = haupt.get("story_id")
        title = haupt.get("title")
        if story_id and title:
            entries.append({
                "story_id": str(story_id),
                "headline": str(title),
                "ressort": r.get("name") or r.get("id") or "?",
                "date": date_iso,
            })
    return entries


def update_ledger(content: dict, date_iso: str) -> int:
    """Schreibt die Story-Liste fort. Gibt die Anzahl heute erfasster
    Geschichten zurueck (fuer die Abschlussmeldung)."""
    ledger = load_ledger()
    todays = extract_todays_entries(content, date_iso)
    ledger.extend(todays)

    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    ledger = [e for e in ledger if e.get("date", "") >= cutoff]
    ledger = ledger[-MAX_ENTRIES:]

    LEDGER_PATH.parent.mkdir(exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(todays)
