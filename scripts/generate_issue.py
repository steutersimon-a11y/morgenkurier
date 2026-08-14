"""Generates a fresh Morgenkurier edition using Groq (free tier, no card
required). Real headlines are fetched from free public RSS feeds and
handed to the model as source material so it never has to invent facts.
Overwrites index.html. Run daily via .github/workflows/daily-update.yml."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.html"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# groq/compound has a much higher free-tier TPM budget (70K vs 12K for
# llama-3.3-70b-versatile), which the full request (news material +
# design reference + instructions) needs headroom for.
MODEL = "groq/compound"
MAX_COMPLETION_TOKENS = 20000

WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

# Freie, oeffentliche RSS-Feeds - keine Anmeldung, kein API-Key, keine Kosten.
FEEDS = {
    "Deutschland (Tagesschau Inland)": "https://www.tagesschau.de/inland/index~rss2.xml",
    "Welt (Tagesschau Ausland)": "https://www.tagesschau.de/ausland/index~rss2.xml",
    "Wirtschaft (Tagesschau)": "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
    "Welt (BBC World)": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Europa & Welt (Deutsche Welle)": "https://rss.dw.com/rdf/rss-de-all",
    "Technologie (Heise)": "https://www.heise.de/rss/heise-atom.xml",
}
ITEMS_PER_FEED = 12

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = HTML_TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def fetch_feed(url: str, limit: int = ITEMS_PER_FEED) -> list[tuple[str, str]]:
    """Returns a list of (title, description) tuples. Never raises -
    a broken feed just yields fewer headlines, it must not fail the run.
    Namespace-agnostic so it handles RSS 2.0, RSS 1.0/RDF, and Atom alike."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; MorgenkurierBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        root = ET.fromstring(data)

        items: list[tuple[str, str]] = []
        for el in root.iter():
            if local_name(el.tag) not in ("item", "entry"):
                continue
            title = None
            desc = None
            for child in el:
                ctag = local_name(child.tag)
                if ctag == "title" and title is None:
                    title = clean_text(child.text)
                elif ctag in ("description", "summary", "content") and desc is None:
                    desc = clean_text(child.text)
            if title:
                items.append((title, desc or ""))
            if len(items) >= limit:
                break
        return items
    except Exception as exc:  # noqa: BLE001 - eine kaputte Quelle darf den Lauf nicht stoppen
        print(f"Warnung: Feed konnte nicht geladen werden ({url}): {exc}", file=sys.stderr)
        return []


def build_news_material() -> str:
    sections = []
    for label, url in FEEDS.items():
        items = fetch_feed(url)
        if not items:
            continue
        lines = [f"## {label}"]
        for title, desc in items:
            snippet = f" — {desc[:280]}" if desc else ""
            lines.append(f"- {title}{snippet}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "(Keine Quellen erreichbar.)"


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    if match:
        return int(match.group(1)) + 1
    return 1


def extract_design_reference(previous_html: str) -> str:
    """Returns just the CSS + navigation markup instead of the full previous
    edition. Sending the whole prior HTML blew the free-tier per-minute
    token budget; the model only needs the design, not yesterday's content,
    to reproduce the layout - the section-by-section spec in the prompt
    covers structure."""
    style_match = re.search(r"<style>.*?</style>", previous_html, re.S)
    nav_match = re.search(r'<nav class="sticky">.*?</nav>', previous_html, re.S)
    parts = [m.group(0) for m in (style_match, nav_match) if m]
    return "\n\n".join(parts) if parts else previous_html[:20000]


def extract_html(raw_text: str) -> str:
    start = raw_text.lower().find("<!doctype")
    end = raw_text.lower().rfind("</html>")
    if start == -1 or end == -1:
        raise RuntimeError(
            "Antwort enthaelt kein vollstaendiges HTML-Dokument:\n" + raw_text[:2000]
        )
    return raw_text[start : end + len("</html>")]


def build_prompt(
    design_reference: str, edition_number: int, date_str: str, time_str: str, news_material: str
) -> tuple[str, str]:
    system_prompt = (
        "Du bist der Chefredakteur des 'Morgenkurier', eines taeglichen, "
        "redaktionell kuratierten Morgen-Briefings fuer die Geschaeftsfuehrung "
        "eines Schuh-Einzelhaendlers mit starkem Fokus auf die CSEE-Region "
        "(Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, "
        "Bosnien, Slowakei, Kroatien, Slowenien). Der Ton ist sachlich, "
        "praezise und einordnend - wie eine hochwertige Wirtschaftszeitung, "
        "nicht wie eine Nachrichtenagentur-Auflistung. Du bekommst echtes, "
        "aktuelles Rohmaterial (Schlagzeilen und Kurzbeschreibungen aus "
        "RSS-Feeds echter Nachrichtenquellen) und schreibst NUR auf Basis "
        "dieses Materials. Du erfindest niemals Fakten, Zahlen, Namen oder "
        "Zitate, die nicht im Material stehen oder sich nicht direkt daraus "
        "ableiten lassen. Wenn zu einem Themenbereich nichts im Material "
        "steht, schreibst du das ehrlich kurz, statt Fuellstoff zu erfinden."
    )

    user_prompt = f"""Erstelle die heutige Ausgabe {edition_number} des Morgenkurier fuer {date_str}, {time_str} Uhr.

ROHMATERIAL (echte, aktuelle Schlagzeilen aus RSS-Feeds - deine einzige Faktenquelle):

{news_material}

AUFGABE:
Ordne das Rohmaterial diesen Ressorts zu und schreibe daraus die Ausgabe (nicht jedes Ressort braucht Material aus jedem Feed - waehle passend zu):
- Deutschland (Politik, Wirtschaft, Gesellschaft)
- Politik / Europa (ein zentrales Leitartikel-Thema des Tages, falls das Material eins hergibt)
- Finanzen & Wirtschaft (Maerkte, Unternehmen, Notenbanken)
- USA (falls im Material vorhanden)
- Welt & Geopolitik (China, Russland/Ukraine, Nahost, weitere Krisenherde)
- CSEE Business Brief (Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, Bosnien, Slowakei, Kroatien, Slowenien - durchsuche das Material gezielt nach diesen Laendern; findest du nichts, schreibe das ehrlich, erfinde nichts. Wo vorhanden: "Relevanz fuer den Schuhretail" Einordnung je Meldung.)
- Future & Technology (KI, Robotik, Wissenschaft, falls im Material vorhanden)
- Wissen (rotierendes Ressort: nutze passende Themen aus dem Material - Wissenschaft, Kultur, Gesellschaft etc.)
- Ein Deep Dive zu einem groesseren Thema, falls das Material genug Substanz fuer eine Einordnung hergibt

ANFORDERUNGEN AN DAS ERGEBNIS:
1. Gib AUSSCHLIESSLICH ein vollstaendiges HTML-Dokument zurueck, beginnend mit <!DOCTYPE html> und endend mit </html>. Keine Erklaerungen, kein Markdown, keine Code-Fences davor oder danach.
2. Uebernimm das komplette CSS (den <style>-Block) und die Navigations-/Anker-Struktur 1:1 aus der untenstehenden Design-Referenz - Layout, Sektionen, Anker-IDs (#inhalt, #deutschland, #politik, #finanzen, #mechanics, #usa, #geopolitik, #csee, #signal, #future, #wissen, #deepdive, #zitat etc.) bleiben identisch zu dem, was die Navigation vorgibt. Ressorts, fuer die es heute nichts Berichtenswertes gibt, duerfen kurz ausfallen oder auf das Naechstliegende ausweichen - aber die Struktur bleibt erhalten.
3. Aktualisiere: Titel-Tag und "Ausgabe {edition_number}" ueberall, das Datum/die Uhrzeit im meta-row, das Inhaltsverzeichnis (Titel/Teaser passend zu den heutigen Themen) und saemtliche Fliesstexte, Schlagzeilen und den Morgenkurier-Index (Ampel-Bewertung pro Bereich) mit echten, heutigen Inhalten aus dem Rohmaterial.
4. Jede Aussage muss sich auf das gelieferte Rohmaterial stuetzen. Keine erfundenen Fakten, Namen, Zahlen oder Zitate.
5. Behalte den redaktionellen Aufbau jeder Sektion bei (z.B. "Was ist passiert" / "Gewinner & Verlierer" im Finanzressort, "Ereignis / Hintergrund / Interessen & Akteure / Machtverhaeltnisse / Konsequenzen & Szenarien" im Politikressort), soweit das Material das hergibt.
6. Das Zitat des Tages am Ende soll thematisch zur Ausgabe passen und einer echten, verifizierbaren Person zugeordnet sein - falls im Material kein passendes Zitat vorkommt, waehle ein bekanntes, echtes Zitat, das zum Hauptthema der Ausgabe passt, und kennzeichne es klar als solches statt es zu erfinden.

DESIGN-REFERENZ (CSS und Navigation der Vorausgabe - uebernimm dieses Design 1:1, fuelle es aber mit den heutigen Inhalten gemaess der obigen Ressort-Vorgaben; die Anker-IDs im nav-Block zeigen dir die erwarteten Section-IDs):

{design_reference}
"""
    return system_prompt, user_prompt


def call_groq(system_prompt: str, user_prompt: str, api_key: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "temperature": 0.6,
    }
    req = urllib.request.Request(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; MorgenkurierBot/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq-API-Fehler {exc.code}: {body}") from exc

    choice = data["choices"][0]
    finish_reason = choice.get("finish_reason")
    content = choice["message"]["content"] or ""
    if not content.strip():
        raise RuntimeError(f"Leere Antwort von Groq (finish_reason={finish_reason}).")
    return content


def main() -> None:
    if not INDEX_PATH.exists():
        print(f"Fehler: {INDEX_PATH} nicht gefunden.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Fehler: Umgebungsvariable GROQ_API_KEY ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    previous_html = INDEX_PATH.read_text(encoding="utf-8")
    edition_number = next_edition_number(previous_html)

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    date_str = german_date(now_berlin)
    time_str = now_berlin.strftime("%H:%M")

    news_material = build_news_material()
    print(f"Rohmaterial gesammelt ({len(news_material)} Zeichen).")

    design_reference = extract_design_reference(previous_html)

    system_prompt, user_prompt = build_prompt(
        design_reference, edition_number, date_str, time_str, news_material
    )

    raw_text = call_groq(system_prompt, user_prompt, api_key)
    new_html = extract_html(raw_text)

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Ausgabe {edition_number} vom {date_str} geschrieben ({len(new_html)} Zeichen).")


if __name__ == "__main__":
    main()
