"""Generates a fresh Morgenkurier edition using Google Gemini (free tier)
with Google Search grounding and overwrites index.html. Run daily via
.github/workflows/daily-update.yml."""

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.html"

MODEL = "gemini-flash-latest"

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
        "AUSSCHLIESSLICH echte, aktuelle Ereignisse ueber die Google-Suche und "
        "erfindest niemals Fakten, Zahlen oder Zitate."
    )

    user_prompt = f"""Erstelle die heutige Ausgabe {edition_number} des Morgenkurier fuer {date_str}, {time_str} Uhr.

Nutze die Google-Suche, um dir einen aktuellen Ueberblick ueber die wichtigsten Ereignisse von heute und den letzten 24-48 Stunden zu verschaffen, in diesen Bereichen:
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

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Fehler: Umgebungsvariable GEMINI_API_KEY ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    previous_html = INDEX_PATH.read_text(encoding="utf-8")
    edition_number = next_edition_number(previous_html)

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    date_str = german_date(now_berlin)
    time_str = now_berlin.strftime("%H:%M")

    system_prompt, user_prompt = build_prompt(previous_html, edition_number, date_str, time_str)

    client = genai.Client(api_key=api_key)
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[grounding_tool],
        max_output_tokens=32768,
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=user_prompt,
        config=config,
    )

    if not response.candidates:
        raise RuntimeError("Keine Antwort von Gemini erhalten (evtl. durch Sicherheitsfilter blockiert).")

    raw_text = response.text or ""
    if not raw_text.strip():
        finish_reason = response.candidates[0].finish_reason
        raise RuntimeError(f"Leere Antwort von Gemini (finish_reason={finish_reason}).")

    new_html = extract_html(raw_text)

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Ausgabe {edition_number} vom {date_str} geschrieben ({len(new_html)} Zeichen).")


if __name__ == "__main__":
    main()
