"""Generates a fresh Morgenkurier edition using Claude with web search
and overwrites index.html. Run daily via .github/workflows/daily-update.yml."""

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.html"

MODEL = "claude-opus-5"

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


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    if match:
        return int(match.group(1)) + 1
    return 1


def extract_html(raw_text: str) -> str:
    start = raw_text.lower().find("<!doctype")
    end = raw_text.lower().rfind("</html>")
    if start == -1 or end == -1:
        raise RuntimeError(
            "Antwort enthaelt kein vollstaendiges HTML-Dokument:\n" + raw_text[:2000]
        )
    return raw_text[start : end + len("</html>")]


def build_prompt(previous_html: str, edition_number: int, date_str: str, time_str: str) -> tuple[str, str]:
    system_prompt = (
        "Du bist der Chefredakteur des 'Morgenkurier', eines taeglichen, "
        "redaktionell kuratierten Morgen-Briefings fuer die Geschaeftsfuehrung "
        "eines Schuh-Einzelhaendlers mit starkem Fokus auf die CSEE-Region "
        "(Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, "
        "Bosnien, Slowakei, Kroatien, Slowenien). Der Ton ist sachlich, "
        "praezise und einordnend - wie eine hochwertige Wirtschaftszeitung, "
        "nicht wie eine Nachrichtenagentur-Auflistung. Du recherchierst "
        "AUSSCHLIESSLICH echte, aktuelle Ereignisse ueber die Websuche und "
        "erfindest niemals Fakten, Zahlen oder Zitate."
    )

    user_prompt = f"""Erstelle die heutige Ausgabe {edition_number} des Morgenkurier fuer {date_str}, {time_str} Uhr.

Nutze die Websuche, um dir einen aktuellen Ueberblick ueber die wichtigsten Ereignisse von heute und den letzten 24-48 Stunden zu verschaffen, in diesen Bereichen:
- Deutschland (Politik, Wirtschaft, Gesellschaft)
- Politik / Europa (mit Fokus auf ein zentrales Leitartikel-Thema des Tages)
- Finanzen & Wirtschaft (Wall Street, DAX, Fed, EZB, wichtige Unternehmenszahlen)
- USA (Washington, Wall Street, America)
- Welt & Geopolitik (China, Russland/Ukraine, Nahost, weitere Krisenherde)
- CSEE Business Brief (Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, Bosnien, Slowakei, Kroatien, Slowenien - mit "Relevanz fuer den Schuhretail" Einordnung je Meldung)
- Future & Technology (KI, Robotik, Quantencomputing, Raumfahrt)
- Wissen (rotierendes Ressort: waehle 4-6 Themen aus Cybersecurity, Sport, Astrophysik, Longevity, Architektur, Wissenschaft, Kultur, Musik - je nachdem was heute wirklich Neues gibt)
- Ein bis zwei Deep Dives zu einem groesseren Wirtschafts- oder Markt-Thema des Tages

ANFORDERUNGEN AN DAS ERGEBNIS:
1. Gib AUSSCHLIESSLICH ein vollstaendiges HTML-Dokument zurueck, beginnend mit <!DOCTYPE html> und endend mit </html>. Keine Erklaerungen, kein Markdown, keine Code-Fences davor oder danach.
2. Uebernimm das komplette CSS (den <style>-Block) und die HTML-Struktur/Klassen/IDs 1:1 aus der untenstehenden Referenzausgabe - Layout, Sektionen, Anker-IDs (#inhalt, #deutschland, #politik, #finanzen, #mechanics, #usa, #geopolitik, #csee, #signal, #future, #wissen, #deepdive, #zitat etc.) und Navigation bleiben identisch.
3. Aktualisiere: Titel-Tag und "Ausgabe {edition_number}" ueberall, das Datum/die Uhrzeit im meta-row, das Inhaltsverzeichnis (Titel/Teaser passend zu den heutigen Themen) und saemtliche Fliesstexte, Schlagzeilen und den Morgenkurier-Index (Ampel-Bewertung pro Bereich) mit echten, heutigen Inhalten.
4. Jede Schlagzeile und jede inhaltliche Aussage muss auf tatsaechlich recherchierten, aktuellen Informationen beruhen. Keine erfundenen Fakten, Namen, Zahlen oder Zitate.
5. Behalte den redaktionellen Aufbau jeder Sektion bei (z.B. "Was ist passiert" / "Gewinner & Verlierer" im Finanzressort, "Ereignis / Hintergrund / Interessen & Akteure / Machtverhaeltnisse / Konsequenzen & Szenarien" im Politikressort), fuelle sie aber mit den heutigen Themen.
6. Das Zitat des Tages am Ende soll thematisch zur Ausgabe passen und einer echten, verifizierbaren Person zugeordnet sein.
7. Wenn du zu einem Bereich an einem bestimmten Tag nichts wirklich Berichtenswertes findest, schreibe das ehrlich kurz statt Fuellstoff zu erfinden.

REFERENZAUSGABE (gestrige Ausgabe, als Stil- und Struktur-Vorlage - NICHT den Inhalt uebernehmen, nur Design/Struktur):

{previous_html}
"""
    return system_prompt, user_prompt


def main() -> None:
    if not INDEX_PATH.exists():
        print(f"Fehler: {INDEX_PATH} nicht gefunden.", file=sys.stderr)
        sys.exit(1)

    previous_html = INDEX_PATH.read_text(encoding="utf-8")
    edition_number = next_edition_number(previous_html)

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    date_str = german_date(now_berlin)
    time_str = now_berlin.strftime("%H:%M")

    system_prompt, user_prompt = build_prompt(previous_html, edition_number, date_str, time_str)

    client = anthropic.Anthropic(timeout=1200.0)

    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=system_prompt,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 30}],
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        final_message = stream.get_final_message()

    if final_message.stop_reason == "refusal":
        raise RuntimeError("Anfrage wurde von Claude abgelehnt (stop_reason=refusal).")

    raw_text = "\n".join(
        block.text for block in final_message.content if block.type == "text"
    )
    new_html = extract_html(raw_text)

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Ausgabe {edition_number} vom {date_str} geschrieben ({len(new_html)} Zeichen).")


if __name__ == "__main__":
    main()
