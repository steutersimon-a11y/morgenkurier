"""Generates a fresh Morgenkurier edition using Groq (free tier, no card
required). Real headlines are fetched from free public RSS feeds and handed
to the model as source material so it never has to invent facts.

The issue is built from FIVE separate, sequential Groq calls (one per
ressort group) instead of one big call. Reasons:
  - Each group gets its own full max_completion_tokens budget, so articles
    can actually be long-form instead of one-sentence summaries squeezed
    out of a single shared 6K-token budget.
  - Each group only sees the RSS material relevant to its own ressorts, so
    the model can't over-index on whatever the single most prominent
    headline of the day happens to be (previously: the same lead story
    bled into cover, USA, deep dive, etc.).
  - A running "covered topics" list is threaded through the calls so later
    groups actively avoid repeating what earlier groups already covered in
    depth.
A 65s pause between calls keeps every individual call comfortably inside
llama-3.3-70b-versatile's 12K-tokens-per-minute free-tier budget while
still allowing a much larger *total* token budget across the whole run
(~35K content tokens vs. ~6K before). Total runtime is ~6-7 minutes, which
is irrelevant for an unattended nightly cron job.

The model returns only structured JSON content (no HTML/CSS) which Python
renders into the static template.html to produce index.html.
Run daily via .github/workflows/daily-update.yml."""

import html
import json
import os
import re
import sys
import time
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
# groq/compound (an agentic wrapper model with built-in web search/code
# exec) proved unreliable for plain JSON generation - it returned a bare
# "413 request_too_large" even for a ~16K-char request, with no rate-limit
# detail in the error body. llama-3.3-70b-versatile is a plain,
# well-documented chat model and has behaved predictably since the switch.
MODEL = "llama-3.3-70b-versatile"
TEMPERATURE = 0.7
COOLDOWN_SECONDS = 65  # keeps successive calls in separate TPM windows

WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

# Freie, oeffentliche RSS-Feeds - keine Anmeldung, kein API-Key, keine Kosten.
# Nach Ressort-Gruppen sortiert, damit jede Gruppe eigenes, passendes
# Material bekommt statt eines gemeinsamen Topfs.
FEEDS = {
    "Deutschland (Tagesschau Inland)": "https://www.tagesschau.de/inland/index~rss2.xml",
    "Welt (Tagesschau Ausland)": "https://www.tagesschau.de/ausland/index~rss2.xml",
    "Wirtschaft (Tagesschau)": "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
    "Wirtschaft (n-tv)": "https://www.n-tv.de/wirtschaft/rss",
    "Wirtschaft (Spiegel)": "https://www.spiegel.de/wirtschaft/index.rss",
    "USA & Kanada (BBC)": "http://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "Welt (BBC World)": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Europa & Welt (Deutsche Welle)": "https://rss.dw.com/rdf/rss-de-all",
    "Europa (DW)": "https://rss.dw.com/rdf/rss-de-eu",
    "Asien (BBC)": "http://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "Technologie (Heise)": "https://www.heise.de/rss/heise-atom.xml",
    "Technologie (Golem)": "https://rss.golem.de/rss.php?feed=RSS2.0",
    "Wissenschaft (ScienceDaily)": "https://www.sciencedaily.com/rss/all.xml",
}
ITEMS_PER_FEED = 10
SNIPPET_LEN = 280

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


def fetch_all_feeds() -> dict[str, list[tuple[str, str]]]:
    all_items = {}
    for label, url in FEEDS.items():
        items = fetch_feed(url)
        if items:
            all_items[label] = items
        else:
            print(f"Hinweis: keine Eintraege von '{label}'.", file=sys.stderr)
    return all_items


def material_block(all_items: dict[str, list[tuple[str, str]]], labels: list[str],
                    item_limit: int | None = None, snippet_len: int | None = None) -> str:
    snippet_len = snippet_len if snippet_len is not None else SNIPPET_LEN
    sections = []
    for label in labels:
        items = all_items.get(label, [])
        if item_limit is not None:
            items = items[:item_limit]
        if not items:
            continue
        lines = [f"## {label}"]
        for title, desc in items:
            snippet = f" — {desc[:snippet_len]}" if desc else ""
            lines.append(f"- {title}{snippet}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "(Zu diesen Quellen lag heute nichts vor.)"


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    if match:
        return int(match.group(1)) + 1
    return 1


STYLE_GUIDE = """STILVORGABE (sehr wichtig fuer die Qualitaet, gilt fuer alle Fliesstexte):
Schreibe wie ein erfahrener Wirtschafts-/Politikjournalist einer hochwertigen Tageszeitung (Niveau FAZ, NZZ, Zeit), nicht wie eine Nachrichtenagentur-Meldung oder eine Zusammenfassung.
- Variiere Satzlaenge und -bau bewusst: auf einen komplexen Satz mit Nebensatz kann ein kurzer, pointierter folgen.
- Nutze praezisen, konkreten Wortschatz statt Fuellwoertern und Plattitueden ("wichtige Entwicklung", "spielt eine Rolle", "vor diesem Hintergrund", "nicht zuletzt"). Jeder Satz liefert eine neue Information oder Einordnung - keine Wiederholung des vorigen Satzes in anderen Worten.
- Baue explizite Kausalitaet und Einordnung ein ("X fuehrt zu Y, weil...", "Damit steigt das Risiko, dass...", "Entscheidend ist weniger A als B") statt Fakten nur aneinanderzureihen.
- Schreibe die geforderte Laenge wirklich aus - lieber ein Thema gruendlich und mit mehreren Facetten (Ursache, Betroffene, Einordnung, moegliche Folgen) durchdringen, als mehrere Themen oberflaechlich antippen.
- Vorbild fuer den TON (der Inhalt hier ist frei erfunden, nur der Stil zaehlt als Vorlage): "Der Machtwechsel ist mehr als ein Wahlergebnis - er ist ein Stresstest fuer die Frage, ob sich in der Region politische Mehrheiten noch vorhersagen lassen. Fuer Unternehmen, die dort produzieren oder einkaufen, bedeutet das: Planungssicherheit wird zur knappen Ressource, nicht mehr zur Selbstverstaendlichkeit."
"""

BASE_SYSTEM_PROMPT = (
    "Du bist der Chefredakteur des 'Morgenkurier', eines taeglichen, "
    "redaktionell kuratierten Morgen-Briefings fuer die Geschaeftsfuehrung "
    "eines Schuh-Einzelhaendlers mit starkem Fokus auf die CSEE-Region "
    "(Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, "
    "Bosnien, Slowakei, Kroatien, Slowenien). Du bekommst echtes, aktuelles "
    "Rohmaterial (Schlagzeilen und Kurzbeschreibungen aus RSS-Feeds echter "
    "Nachrichtenquellen) und schreibst NUR auf Basis dieses Materials - "
    "erfinde niemals Fakten, Zahlen, Namen oder Zitate, die nicht im "
    "Material stehen oder sich nicht direkt daraus ableiten lassen. Du "
    "gibst deine Antwort ausschliesslich als JSON gemaess dem vorgegebenen "
    "Schema zurueck.\n\n" + STYLE_GUIDE
)


def covered_note(covered: list[str]) -> str:
    if not covered:
        return ""
    return (
        "\nBEREITS AUSFUEHRLICH IN ANDEREN RESSORTS BEHANDELTE THEMEN "
        "(nicht erneut zum Hauptthema eines Abschnitts machen, hoechstens "
        "kurz als Querverweis erwaehnen):\n- " + "\n- ".join(covered) + "\n"
    )


def call_groq(system_prompt: str, user_prompt: str, api_key: str, max_completion_tokens: int) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": max_completion_tokens,
        "temperature": TEMPERATURE,
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


def run_group(name: str, api_key: str, max_completion_tokens: int, material: str,
              schema: str, covered: list[str], extra_context: str = "") -> dict:
    print(f"Generiere Gruppe '{name}' ({max_completion_tokens} Tokens Budget)...")
    user_prompt = (
        f"ROHMATERIAL (deine einzige Faktenquelle fuer diese Gruppe):\n\n{material}\n"
        f"{covered_note(covered)}"
        f"{extra_context}\n"
        f"{schema}"
    )
    raw_text = call_groq(BASE_SYSTEM_PROMPT, user_prompt, api_key, max_completion_tokens)
    result = extract_json(raw_text)
    print(f"Gruppe '{name}' fertig ({len(raw_text)} Zeichen Antwort).")
    return result


# ---------------------------------------------------------------------------
# Gruppe A: Deutschland & Politik
# ---------------------------------------------------------------------------
SCHEMA_A = """Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (kein Markdown, keine Code-Fences), reiner Klartext ohne HTML-Tags, exakt in diesem Schema:
{
  "deutschland": {
    "headlines": [str] (6-8 kurze Ein-Satz-Meldungen aus Deutschland, ohne Gedankenstrich-Praefix),
    "article_title": str (Ueberschrift fuer einen ausfuehrlichen Hintergrundartikel zum wichtigsten deutschen Thema des Tages),
    "article_paragraphs": [str] (4-6 gut ausgearbeitete Absaetze, je 4-6 Saetze - insgesamt ein wirklich substantieller Artikel, keine Kurzmeldung),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "politik": {
    "leitartikel_desc": str (1 Satz: welches politische Leitthema wird behandelt - waehle nach Moeglichkeit ein anderes Thema als der Deutschland-Artikel, z.B. ein europaeisches oder internationales Politikthema aus dem Material),
    "ereignis": str (2-3 Saetze, was konkret passiert ist),
    "hintergrund": str (1 ausfuehrlicher Absatz, 4-6 Saetze),
    "akteure": str (1 ausfuehrlicher Absatz: wer will was, welche Interessen stehen sich gegenueber),
    "machtverhaeltnisse": str (1 Absatz),
    "konsequenzen": str (1 ausfuehrlicher Absatz: konkrete moegliche Szenarien),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "topics_covered": [str] (3-6 kurze Stichworte der Hauptthemen dieser beiden Abschnitte, z.B. ["Rentenreform","Landtagswahl Sachsen-Anhalt"])
}
Wenn zu einem Bereich nichts Ausreichendes im Material steht, schreibe das ehrlich kurz statt Fuellstoff zu erfinden."""


def group_a(all_items, api_key, covered) -> dict:
    material = material_block(all_items, [
        "Deutschland (Tagesschau Inland)", "Welt (Tagesschau Ausland)",
    ])
    return run_group("Deutschland & Politik", api_key, 8000, material, SCHEMA_A, covered)


# ---------------------------------------------------------------------------
# Gruppe B: Finanzen, Market Mechanics, USA
# ---------------------------------------------------------------------------
SCHEMA_B = """Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (kein Markdown, keine Code-Fences), reiner Klartext ohne HTML-Tags, exakt in diesem Schema:
{
  "finanzen": {
    "was_ist_passiert": [str] (2-3 ausfuehrliche Absaetze zu den wichtigsten Markt-/Unternehmensmeldungen des Tages),
    "eingepreist": str (1 Absatz: was war bereits erwartet worden, was war die eigentliche Ueberraschung),
    "gewinner": str (1-2 Saetze als Fliesstext),
    "verlierer": str (1-2 Saetze als Fliesstext),
    "auswirkungen": str (1 ausfuehrlicher Absatz zu Europa/Deutschland),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "mechanics": {
    "headline": str (eine erklaerende Frage, z.B. 'Warum...?', verankert an einer heutigen Meldung),
    "paragraphs": [str] (2-3 Absaetze, die ein wirtschaftliches Konzept laienverstaendlich, aber nicht oberflaechlich erklaeren),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "usa": {
    "washington": str (1 ausfuehrlicher Absatz zur US-Politik),
    "wallstreet": str (1 ausfuehrlicher Absatz zu US-Maerkten),
    "america": str (1 Absatz zu Gesellschaft/Technologie/Sonstiges aus den USA, falls Material vorhanden),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "topics_covered": [str] (3-6 kurze Stichworte, z.B. ["US-Inflationsdaten","Lufthansa-Gewinnwarnung"])
}
Wenn zu einem Bereich nichts Ausreichendes im Material steht, schreibe das ehrlich kurz statt Fuellstoff zu erfinden."""


def group_b(all_items, api_key, covered) -> dict:
    material = material_block(all_items, [
        "Wirtschaft (Tagesschau)", "Wirtschaft (n-tv)", "Wirtschaft (Spiegel)",
        "USA & Kanada (BBC)",
    ])
    return run_group("Finanzen & USA", api_key, 7000, material, SCHEMA_B, covered)


# ---------------------------------------------------------------------------
# Gruppe C: Welt & Geopolitik, CSEE Business Brief
# ---------------------------------------------------------------------------
SCHEMA_C = """Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (kein Markdown, keine Code-Fences), reiner Klartext ohne HTML-Tags, exakt in diesem Schema:
{
  "geopolitik": {
    "china": str (1 ausfuehrlicher Absatz),
    "ukraine_russland": str (1 ausfuehrlicher Absatz),
    "weitere": str (1 Absatz zu weiteren geopolitischen Entwicklungen, z.B. Nahost, Nordkorea, Asien),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "csee": [
    {"headline": str, "text": str (2-3 Saetze), "relevanz": str (1-2 Saetze: konkrete Relevanz fuer einen Schuh-Einzelhaendler mit Geschaeft/Beschaffung/Filialen in der Region)}
  ] (3-5 Eintraege NUR zu Oesterreich, Tschechien, Ungarn, Rumaenien, Bulgarien, Serbien, Bosnien, Slowakei, Kroatien oder Slowenien - durchsuche das Material gezielt danach; findest du zu einem Land nichts, nimm es nicht in die Liste auf; findest du insgesamt nichts, gib ein Array mit einem Eintrag zurueck, der das ehrlich sagt),
  "csee_toc_teaser": str (1 Satz fuers Inhaltsverzeichnis),
  "topics_covered": [str] (3-6 kurze Stichworte, z.B. ["Ukraine-Angriffe auf Oelinfrastruktur","Rumaenien Regierungskrise"])
}
Wenn zu einem Bereich nichts Ausreichendes im Material steht, schreibe das ehrlich kurz statt Fuellstoff zu erfinden."""


def group_c(all_items, api_key, covered) -> dict:
    material = material_block(all_items, [
        "Welt (BBC World)", "Europa & Welt (Deutsche Welle)", "Europa (DW)", "Asien (BBC)",
    ])
    return run_group("Geopolitik & CSEE", api_key, 7000, material, SCHEMA_C, covered)


# ---------------------------------------------------------------------------
# Gruppe D: Future & Technology, Wissen
# ---------------------------------------------------------------------------
SCHEMA_D = """Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (kein Markdown, keine Code-Fences), reiner Klartext ohne HTML-Tags, exakt in diesem Schema:
{
  "future": {
    "verfuegbar": str (1 ausfuehrlicher Absatz: KI/Tech, die bereits im produktiven Einsatz ist),
    "in_entwicklung": str (1 ausfuehrlicher Absatz),
    "experimentell": str (1 Absatz: experimentelle/spekulative Entwicklungen, mit angemessener Vorsicht formuliert),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "wissen": [
    {"kicker": str (Rubrik, z.B. 'Wissenschaft', 'Gesellschaft', 'Kultur'), "headline": str, "text": str (2-3 Saetze)}
  ] (5-7 Eintraege zu unterschiedlichen Themen aus dem Material - moeglichst breit gestreut, nicht mehrere Eintraege zum selben Thema),
  "wissen_toc_teaser": str (1 Satz fuers Inhaltsverzeichnis),
  "topics_covered": [str] (3-6 kurze Stichworte, z.B. ["Quantencomputing-Durchbruch","Neue Exoplaneten-Entdeckung"])
}
Wenn zu einem Bereich nichts Ausreichendes im Material steht, schreibe das ehrlich kurz statt Fuellstoff zu erfinden."""


def group_d(all_items, api_key, covered) -> dict:
    material = material_block(all_items, [
        "Technologie (Heise)", "Technologie (Golem)", "Wissenschaft (ScienceDaily)",
    ])
    return run_group("Future & Wissen", api_key, 7000, material, SCHEMA_D, covered)


# ---------------------------------------------------------------------------
# Gruppe E: Cover, Inhalt-Kontext, Index, Welt am Morgen, Signal, Deep Dive, Zitat
# ---------------------------------------------------------------------------
SCHEMA_E = """Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (kein Markdown, keine Code-Fences), reiner Klartext ohne HTML-Tags, exakt in diesem Schema:
{
  "cover_headline": str (eine Zeile, das wichtigste Thema der GESAMTEN heutigen Ausgabe - darf mit dem Leitthema eines Ressorts uebereinstimmen, wenn es wirklich das wichtigste Thema ist),
  "cover_dek": str (2-3 Saetze Einordnung des Leitthemas),
  "cover_items": [ {"title": str, "desc": str (1 Satz)} ] (genau 4 weitere wichtige Meldungen aus verschiedenen Ressorts, NICHT identisch mit der cover_headline),
  "index_table": [ {"area": str, "dot": "🟢"|"🟡"|"🟠"|"🔴", "note": str (1 Satz Einordnung)} ] (genau fuer diese 7 Bereiche in dieser Reihenfolge: "Deutschland", "USA", "Europa", "Geopolitik", "Finanzmärkte", "CSEE Retail", "Technologie"),
  "welt_am_morgen": [str] (8-10 kurze Ein-Satz-Meldungen quer durch alle Themenbereiche, je ein String ohne Gedankenstrich-Praefix, moeglichst divers),
  "signal": [ {"tag": str (z.B. 'Economic Signal', 'Political Signal', 'Future Signal'), "headline": str, "text": str (2-3 Saetze)} ] (genau 3 Eintraege: eine wirtschaftliche, eine politische, eine technologische Perspektive - zu Themen, die NICHT bereits die Hauptartikel anderer Ressorts sind),
  "deepdive": {
    "headline": str (ein groesseres Thema, das mehrere Faeden aus verschiedenen Ressorts zusammenfuehrt und tiefer einordnet als die einzelnen Ressort-Artikel - WICHTIG: waehle explizit ein anderes Thema als die cover_headline),
    "paragraphs": [str] (4-6 ausfuehrliche Absaetze, die das Thema wirklich durchdringen - Ursachen, Zusammenhaenge, Betroffene, moegliche Folgen),
    "toc_teaser": str (1 Satz fuers Inhaltsverzeichnis)
  },
  "quote": {"text": str (ein bekanntes, echtes, verifizierbares Zitat passend zum Hauptthema der Ausgabe), "author": str (Name und ggf. Quelle)},
  "gedanke": str (1 Absatz, redaktioneller Schlussgedanke passend zur Ausgabe)
}
Wenn zu einem Bereich nichts Ausreichendes im Material steht, schreibe das ehrlich kurz statt Fuellstoff zu erfinden."""


def group_e(all_items, api_key, covered, digest: str) -> dict:
    all_labels = list(all_items.keys())
    material = material_block(all_items, all_labels, item_limit=5, snippet_len=110)
    extra_context = (
        f"\nZUSAMMENFASSUNG DER BEREITS FERTIGGESTELLTEN RESSORTS (nutze das, "
        f"um cover_headline/deepdive bewusst zu unterscheiden und Doppelungen "
        f"zu vermeiden):\n{digest}\n"
    )
    return run_group("Cover & Synthese", api_key, 6500, material, SCHEMA_E, covered, extra_context)


def build_digest(result_a: dict, result_b: dict, result_c: dict, result_d: dict) -> str:
    d = result_a.get("deutschland", {})
    p = result_a.get("politik", {})
    f = result_b.get("finanzen", {})
    m = result_b.get("mechanics", {})
    u = result_b.get("usa", {})
    g = result_c.get("geopolitik", {})
    csee = result_c.get("csee", [])
    fut = result_d.get("future", {})
    wis = result_d.get("wissen", [])
    lines = [
        f"- Deutschland: {d.get('article_title', '')}",
        f"- Politik: {p.get('leitartikel_desc', '')}",
        f"- Finanzen: {(f.get('was_ist_passiert') or [''])[0][:200]}",
        f"- Market Mechanics: {m.get('headline', '')}",
        f"- USA: {u.get('washington', '')[:150]}",
        f"- Geopolitik: {g.get('ukraine_russland', '')[:150]}",
        f"- CSEE: {', '.join(item.get('headline', '') for item in csee[:3])}",
        f"- Future: {fut.get('verfuegbar', '')[:150]}",
        f"- Wissen: {', '.join(item.get('headline', '') for item in wis[:3])}",
    ]
    return "\n".join(lines)


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

    all_items = fetch_all_feeds()
    print(f"Rohmaterial gesammelt von {len(all_items)} Quellen.")

    covered: list[str] = []

    result_a = group_a(all_items, api_key, covered)
    covered += result_a.get("topics_covered", [])
    time.sleep(COOLDOWN_SECONDS)

    result_b = group_b(all_items, api_key, covered)
    covered += result_b.get("topics_covered", [])
    time.sleep(COOLDOWN_SECONDS)

    result_c = group_c(all_items, api_key, covered)
    covered += result_c.get("topics_covered", [])
    time.sleep(COOLDOWN_SECONDS)

    result_d = group_d(all_items, api_key, covered)
    covered += result_d.get("topics_covered", [])
    time.sleep(COOLDOWN_SECONDS)

    digest = build_digest(result_a, result_b, result_c, result_d)
    result_e = group_e(all_items, api_key, covered, digest)

    content = {
        "cover_headline": result_e.get("cover_headline"),
        "cover_dek": result_e.get("cover_dek"),
        "cover_items": result_e.get("cover_items"),
        "toc_teasers": {
            "deutschland": result_a.get("deutschland", {}).get("toc_teaser"),
            "politik": result_a.get("politik", {}).get("toc_teaser"),
            "finanzen": result_b.get("finanzen", {}).get("toc_teaser"),
            "mechanics": result_b.get("mechanics", {}).get("toc_teaser"),
            "usa": result_b.get("usa", {}).get("toc_teaser"),
            "geopolitik": result_c.get("geopolitik", {}).get("toc_teaser"),
            "csee": result_c.get("csee_toc_teaser"),
            "future": result_d.get("future", {}).get("toc_teaser"),
            "wissen": result_d.get("wissen_toc_teaser"),
            "deepdive": result_e.get("deepdive", {}).get("toc_teaser"),
        },
        "index_table": result_e.get("index_table"),
        "welt_am_morgen": result_e.get("welt_am_morgen"),
        "deutschland": result_a.get("deutschland"),
        "politik": result_a.get("politik"),
        "finanzen": result_b.get("finanzen"),
        "mechanics": result_b.get("mechanics"),
        "usa": result_b.get("usa"),
        "geopolitik": result_c.get("geopolitik"),
        "csee": result_c.get("csee"),
        "signal": result_e.get("signal"),
        "future": result_d.get("future"),
        "wissen": result_d.get("wissen"),
        "deepdive": result_e.get("deepdive"),
        "quote": result_e.get("quote"),
        "gedanke": result_e.get("gedanke"),
        "sources_used": sorted(all_items.keys()),
    }

    new_html = render_html(content, edition_number, date_str, time_str)

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"Ausgabe {edition_number} vom {date_str} geschrieben ({len(new_html)} Zeichen).")


if __name__ == "__main__":
    main()
