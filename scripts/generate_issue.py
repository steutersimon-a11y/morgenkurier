"""Generates a fresh Morgenkurier edition using Groq (free tier, no card
required). Real headlines are fetched from free public RSS feeds and handed
to the model as source material so it never has to invent facts. The model
returns only structured JSON content (no HTML/CSS) which keeps the request
well inside the free tier's token limits; Python renders that content into
the static template.html to produce index.html.
Run daily via .github/workflows/daily-update.yml."""

import html
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
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# groq/compound is an agentic wrapper model (built-in web search/code exec)
# and proved unreliable for plain JSON generation - it returned a bare
# "413 request_too_large" even for a ~16K-char request, with no rate-limit
# detail in the error body, suggesting an internal payload issue unrelated
# to token counts. llama-3.3-70b-versatile is a plain, well-documented chat
# model; with the JSON-only content architecture the whole request (news
# material + schema instructions + completion) comfortably fits its 12K
# tokens-per-minute free-tier budget.
MODEL = "llama-3.3-70b-versatile"
MAX_COMPLETION_TOKENS = 6000

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
ITEMS_PER_FEED = 8
SNIPPET_LEN = 200

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
    """Returns a list of (title, description) tuples. Never raises - a
    broken feed just yields fewer headlines, it must not fail the run.
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


def build_news_material() -> tuple[str, list[str]]:
    sections = []
    used_labels = []
    for label, url in FEEDS.items():
        items = fetch_feed(url)
        if not items:
            continue
        used_labels.append(label)
        lines = [f"## {label}"]
        for title, desc in items:
            snippet = f" — {desc[:SNIPPET_LEN]}" if desc else ""
            lines.append(f"- {title}{snippet}")
        sections.append("\n".join(lines))
    material = "\n\n".join(sections) if sections else "(Keine Quellen erreichbar.)"
    return material, used_labels


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    if match:
        return int(match.group(1)) + 1
    return 1


SCHEMA_DESCRIPTION = """Antworte AUSSCHLIESSLICH mit einem einzigen validen JSON-Objekt (keine Erklaerung, kein Markdown, keine Code-Fences davor oder danach). Alle Texte sind reiner Klartext ohne HTML-Tags. Halte dich an genau dieses Schema (Kurzform der erwarteten Laenge in Klammern):

{
  "cover_headline": str (eine Zeile, das wichtigste Thema der Ausgabe, GROSSBUCHSTABEN nicht noetig),
  "cover_dek": str (1-2 Saetze Einordnung des Leitthemas),
  "cover_items": [ {"title": str, "desc": str (1 Satz)} ] (genau 4 weitere wichtige Meldungen),
  "toc_teasers": {"deutschland": str, "politik": str, "finanzen": str, "mechanics": str, "usa": str, "geopolitik": str, "csee": str, "future": str, "wissen": str, "deepdive": str} (je ein kurzer Teaser-Satz fuer das Inhaltsverzeichnis),
  "index_table": [ {"area": str, "dot": "🟢"|"🟡"|"🟠"|"🔴", "note": str (1 Satz Einordnung)} ] (genau fuer diese 7 Bereiche in dieser Reihenfolge: "Deutschland", "USA", "Europa", "Geopolitik", "Finanzmärkte", "CSEE Retail", "Technologie"),
  "welt_am_morgen": [str] (6-8 kurze Ein-Satz-Meldungen aus aller Welt, je ein String ohne Gedankenstrich-Praefix),
  "deutschland": {"headlines": [str] (5-6 kurze Ein-Satz-Meldungen ohne Praefix), "article_title": str (Ueberschrift fuer einen Hintergrundartikel), "article_paragraphs": [str] (2-3 Absaetze)},
  "politik": {"leitartikel_desc": str (1 Satz, welches Thema der Leitartikel behandelt), "ereignis": str (1-2 Saetze, was ist passiert), "hintergrund": str (1 Absatz), "akteure": str (1 Absatz, wer will was), "machtverhaeltnisse": str (1 Absatz), "konsequenzen": str (1 Absatz, moegliche Szenarien)},
  "finanzen": {"was_ist_passiert": [str] (1-2 Absaetze als Array), "eingepreist": str (1 Absatz), "gewinner": str (1 Satz/Aufzaehlung als Fliesstext), "verlierer": str (1 Satz/Aufzaehlung als Fliesstext), "auswirkungen": str (1 Absatz zu Europa/Deutschland)},
  "mechanics": {"headline": str (eine erklaerende Frage, z.B. 'Warum...?'), "paragraphs": [str] (2 Absaetze, die ein wirtschaftliches Konzept laienverstaendlich erklaeren, verankert an einer heutigen Meldung)},
  "usa": {"washington": str (1 Absatz), "wallstreet": str (1 Absatz), "america": str (1 Absatz)},
  "geopolitik": {"china": str (1 Absatz), "ukraine_russland": str (1 Absatz), "weitere": str (1 Absatz)},
  "csee": [ {"headline": str, "text": str (1-2 Saetze), "relevanz": str (1 Satz: konkrete Relevanz fuer einen Schuh-Einzelhaendler mit Geschaeft/Beschaffung in der Region)} ] (2-4 Eintraege NUR zu Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, Bosnien, Slowakei, Kroatien oder Slowenien; wenn im Material nichts zu diesen Laendern steht, gib ein Array mit einem Eintrag zurueck, der das ehrlich sagt),
  "signal": [ {"tag": str (z.B. 'Economic Signal'), "headline": str, "text": str (1-2 Saetze)} ] (genau 3 Eintraege, unterschiedliche Perspektiven: wirtschaftlich, politisch, technologisch),
  "future": {"verfuegbar": str (1 Absatz: KI/Tech bereits im Einsatz), "in_entwicklung": str (1 Absatz), "experimentell": str (1 Absatz)},
  "wissen": [ {"kicker": str (Rubrik, z.B. 'Wissenschaft'), "headline": str, "text": str (1-2 Saetze)} ] (4-6 Eintraege zu Wissenschaft/Kultur/Gesellschaft/Technik aus dem Material),
  "deepdive": {"headline": str, "paragraphs": [str] (2-3 Absaetze, die ein groesseres Thema des Tages einordnen)},
  "quote": {"text": str (ein bekanntes, echtes, verifizierbares Zitat passend zum Hauptthema), "author": str (Name und ggf. Quelle)},
  "gedanke": str (1 Absatz, redaktioneller Schlussgedanke passend zur Ausgabe),
  "sources_used": [str] (Liste der Feed-Bezeichnungen aus dem Rohmaterial, die du tatsaechlich verwendet hast)
}

Wenn zu einem Bereich nichts im Rohmaterial steht, schreibe das ehrlich kurz in das jeweilige Feld statt Fuellstoff zu erfinden. Erfinde niemals Fakten, Zahlen, Namen oder Zitate, die nicht im Material stehen oder sich nicht direkt daraus ableiten lassen (Ausnahme: das bekannte, echte Zitat des Tages)."""


def build_prompt(edition_number: int, date_str: str, time_str: str, news_material: str) -> tuple[str, str]:
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
        "dieses Materials. Du gibst deine Antwort ausschliesslich als JSON "
        "gemaess dem vorgegebenen Schema zurueck."
    )

    user_prompt = f"""Erstelle den Inhalt fuer die heutige Ausgabe {edition_number} des Morgenkurier fuer {date_str}, {time_str} Uhr.

ROHMATERIAL (echte, aktuelle Schlagzeilen aus RSS-Feeds - deine einzige Faktenquelle):

{news_material}

{SCHEMA_DESCRIPTION}"""
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
        "response_format": {"type": "json_object"},
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


def extract_json(raw_text: str) -> dict:
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("Antwort enthaelt kein JSON-Objekt:\n" + raw_text[:2000])
    return json.loads(raw_text[start : end + 1])


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def paragraphs(items) -> str:
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"      <p>{esc(p)}</p>" for p in items if str(p).strip())


def bullet_paragraphs(items) -> str:
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"      <p>— {esc(p)}</p>" for p in items if str(p).strip())


def render_cover_items(items) -> str:
    rows = []
    for item in items or []:
        rows.append(
            f'      <div class="ci"><h4>{esc(item.get("title"))}</h4>'
            f'<p>{esc(item.get("desc"))}</p></div>'
        )
    return "\n".join(rows)


def render_index_table(rows) -> str:
    out = []
    for row in rows or []:
        out.append(
            f'      <tr><td class="area">{esc(row.get("area"))}</td>'
            f'<td class="dot">{esc(row.get("dot"))}</td>'
            f'<td class="note">{esc(row.get("note"))}</td></tr>'
        )
    return "\n".join(out)


def render_csee_items(items) -> str:
    out = []
    for item in items or []:
        out.append(
            f'    <div class="csee-item">\n'
            f'      <h4>{esc(item.get("headline"))}</h4>\n'
            f'      <p>{esc(item.get("text"))}</p>\n'
            f'      <p class="relevanz"><b>Relevanz für den Schuhretail:</b> {esc(item.get("relevanz"))}</p>\n'
            f'    </div>'
        )
    return "\n".join(out)


def render_signal_items(items) -> str:
    out = []
    for item in items or []:
        out.append(
            f'      <div class="signal-item">\n'
            f'        <div class="tag">{esc(item.get("tag"))}</div>\n'
            f'        <h4>{esc(item.get("headline"))}</h4>\n'
            f'        <p>{esc(item.get("text"))}</p>\n'
            f'      </div>'
        )
    return "\n".join(out)


def render_wissen_items(items) -> str:
    out = []
    for item in items or []:
        out.append(
            f'    <div class="wissen-item">\n'
            f'      <div class="kicker">{esc(item.get("kicker"))}</div>\n'
            f'      <h3>{esc(item.get("headline"))}</h3>\n'
            f'      <p>{esc(item.get("text"))}</p>\n'
            f'    </div>'
        )
    return "\n".join(out)


def render_html(content: dict, edition_number: int, date_str: str, time_str: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    toc = content.get("toc_teasers", {}) or {}
    deutschland = content.get("deutschland", {}) or {}
    politik = content.get("politik", {}) or {}
    finanzen = content.get("finanzen", {}) or {}
    mechanics = content.get("mechanics", {}) or {}
    usa = content.get("usa", {}) or {}
    geopolitik = content.get("geopolitik", {}) or {}
    future = content.get("future", {}) or {}
    deepdive = content.get("deepdive", {}) or {}
    quote = content.get("quote", {}) or {}

    edition_label = f"Ausgabe {edition_number:03d}"
    replacements = {
        "§§EYEBROW§§": esc(edition_label),
        "§§EDITION_LABEL§§": esc(edition_label),
        "§§DATE_TIME§§": esc(f"{date_str} · {time_str} Uhr"),
        "§§COVER_HEADLINE§§": esc(content.get("cover_headline")),
        "§§COVER_DEK§§": esc(content.get("cover_dek")),
        "§§COVER_LIST§§": render_cover_items(content.get("cover_items")),
        "§§TOC_DEUTSCHLAND§§": esc(toc.get("deutschland")),
        "§§TOC_POLITIK§§": esc(toc.get("politik")),
        "§§TOC_FINANZEN§§": esc(toc.get("finanzen")),
        "§§TOC_MECHANICS§§": esc(toc.get("mechanics")),
        "§§TOC_USA§§": esc(toc.get("usa")),
        "§§TOC_GEOPOLITIK§§": esc(toc.get("geopolitik")),
        "§§TOC_CSEE§§": esc(toc.get("csee")),
        "§§TOC_FUTURE§§": esc(toc.get("future")),
        "§§TOC_WISSEN§§": esc(toc.get("wissen")),
        "§§TOC_DEEPDIVE§§": esc(toc.get("deepdive")),
        "§§INDEX_TABLE§§": render_index_table(content.get("index_table")),
        "§§WELT_AM_MORGEN§§": bullet_paragraphs(content.get("welt_am_morgen")),
        "§§DEUTSCHLAND_HEADLINES§§": bullet_paragraphs(deutschland.get("headlines")),
        "§§DEUTSCHLAND_ARTICLE_TITLE§§": esc(deutschland.get("article_title")),
        "§§DEUTSCHLAND_ARTICLE§§": paragraphs(deutschland.get("article_paragraphs")),
        "§§POLITIK_LEITARTIKEL§§": esc(politik.get("leitartikel_desc")),
        "§§POLITIK_EREIGNIS§§": esc(politik.get("ereignis")),
        "§§POLITIK_HINTERGRUND§§": esc(politik.get("hintergrund")),
        "§§POLITIK_AKTEURE§§": esc(politik.get("akteure")),
        "§§POLITIK_MACHTVERHAELTNISSE§§": esc(politik.get("machtverhaeltnisse")),
        "§§POLITIK_KONSEQUENZEN§§": esc(politik.get("konsequenzen")),
        "§§FINANZEN_WAS_IST_PASSIERT§§": paragraphs(finanzen.get("was_ist_passiert")),
        "§§FINANZEN_EINGEPREIST§§": esc(finanzen.get("eingepreist")),
        "§§FINANZEN_GEWINNER§§": esc(finanzen.get("gewinner")),
        "§§FINANZEN_VERLIERER§§": esc(finanzen.get("verlierer")),
        "§§FINANZEN_AUSWIRKUNGEN§§": esc(finanzen.get("auswirkungen")),
        "§§MECHANICS_HEADLINE§§": esc(mechanics.get("headline")),
        "§§MECHANICS_PARAGRAPHS§§": paragraphs(mechanics.get("paragraphs")),
        "§§USA_WASHINGTON§§": esc(usa.get("washington")),
        "§§USA_WALLSTREET§§": esc(usa.get("wallstreet")),
        "§§USA_AMERICA§§": esc(usa.get("america")),
        "§§GEOPOLITIK_CHINA§§": esc(geopolitik.get("china")),
        "§§GEOPOLITIK_UKRAINE§§": esc(geopolitik.get("ukraine_russland")),
        "§§GEOPOLITIK_WEITERE§§": esc(geopolitik.get("weitere")),
        "§§CSEE_ITEMS§§": render_csee_items(content.get("csee")),
        "§§SIGNAL_ITEMS§§": render_signal_items(content.get("signal")),
        "§§FUTURE_VERFUEGBAR§§": esc(future.get("verfuegbar")),
        "§§FUTURE_ENTWICKLUNG§§": esc(future.get("in_entwicklung")),
        "§§FUTURE_EXPERIMENTELL§§": esc(future.get("experimentell")),
        "§§WISSEN_ITEMS§§": render_wissen_items(content.get("wissen")),
        "§§DEEPDIVE_HEADLINE§§": esc(deepdive.get("headline")),
        "§§DEEPDIVE_PARAGRAPHS§§": paragraphs(deepdive.get("paragraphs")),
        "§§QUOTE_TEXT§§": esc(quote.get("text")),
        "§§QUOTE_AUTHOR§§": esc(quote.get("author")),
        "§§GEDANKE§§": esc(content.get("gedanke")),
        "§§SOURCES§§": esc(", ".join(content.get("sources_used") or []) or "diverse RSS-Feeds"),
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


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

    news_material, _ = build_news_material()
    print(f"Rohmaterial gesammelt ({len(news_material)} Zeichen).")

    system_prompt, user_prompt = build_prompt(edition_number, date_str, time_str, news_material)

    raw_text = call_groq(system_prompt, user_prompt, api_key)
    content = extract_json(raw_text)

    new_html = render_html(content, edition_number, date_str, time_str)

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Ausgabe {edition_number} vom {date_str} geschrieben ({len(new_html)} Zeichen).")


if __name__ == "__main__":
    main()
