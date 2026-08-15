"""Schritt 3 der taeglichen Pipeline: HTML rendern.

Liest den von Claude geschriebenen redaktionellen Inhalt (build/content.json)
und rendert ihn deterministisch in scripts/template.html -> index.html.

Bewusste Arbeitsteilung: Claude liefert ausschliesslich Text (JSON), niemals
Markup. Damit kann sich das Layout von Ausgabe zu Ausgabe nicht veraendern -
alle HTML-Struktur, alle CSS-Klassen und das gesamte Escaping liegen hier.

Der Lauf bricht ab, wenn Pflichtfelder fehlen: eine halb leere Zeitung soll
nicht deployt werden. Unterschrittene Wortzahlen sind dagegen nur Warnungen -
lieber eine etwas kurze Ausgabe als gar keine.

Aufruf: python scripts/render_issue.py"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.html"
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"
BUILD_DIR = REPO_ROOT / "build"
CONTENT_PATH = BUILD_DIR / "content.json"
SOURCES_PATH = BUILD_DIR / "sources.json"

WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------

# (Pfad, Mindestanzahl Eintraege) - Pfad ist punktsepariert.
REQUIRED_LISTS = [
    ("cover_items", 3),
    ("index_table", 5),
    ("welt_am_morgen", 6),
    ("deutschland.headlines", 4),
    ("deutschland.article_paragraphs", 3),
    ("finanzen.was_ist_passiert", 1),
    ("mechanics.paragraphs", 2),
    ("csee", 1),
    ("signal", 2),
    ("wissen", 3),
    ("deepdive.paragraphs", 3),
]

REQUIRED_STRINGS = [
    "cover_headline", "cover_dek", "gedanke",
    "deutschland.article_title",
    "politik.leitartikel_desc", "politik.ereignis", "politik.hintergrund",
    "politik.akteure", "politik.machtverhaeltnisse", "politik.konsequenzen",
    "finanzen.eingepreist", "finanzen.gewinner", "finanzen.verlierer",
    "finanzen.auswirkungen",
    "mechanics.headline",
    "usa.washington", "usa.wallstreet",
    "geopolitik.china", "geopolitik.ukraine_russland",
    "future.verfuegbar", "future.in_entwicklung",
    "deepdive.headline",
    "quote.text", "quote.author",
]

# (Pfad, Ziel-Wortzahl) - nur Warnung, kein Abbruch.
WORD_TARGETS = [
    ("deutschland.article_paragraphs", 450),
    ("deepdive.paragraphs", 400),
    ("finanzen.was_ist_passiert", 250),
    ("mechanics.paragraphs", 180),
]


def dig(content: dict, path: str):
    node = content
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def count_words(value) -> int:
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, list):
        return sum(len(str(v).split()) for v in value)
    return 0


def total_word_count(node) -> int:
    """Zaehlt rekursiv alle Woerter - auch in verschachtelten Objekten."""
    if isinstance(node, str):
        return len(node.split())
    if isinstance(node, list):
        return sum(total_word_count(v) for v in node)
    if isinstance(node, dict):
        return sum(total_word_count(v) for v in node.values())
    return 0


def validate(content: dict) -> None:
    errors: list[str] = []

    for path, minimum in REQUIRED_LISTS:
        value = dig(content, path)
        if not isinstance(value, list):
            errors.append(f"'{path}' fehlt oder ist keine Liste.")
        elif len(value) < minimum:
            errors.append(f"'{path}' hat {len(value)} Eintraege, mindestens {minimum} erwartet.")

    for path in REQUIRED_STRINGS:
        value = dig(content, path)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"'{path}' fehlt oder ist leer.")

    if errors:
        print("Fehler: build/content.json ist unvollstaendig.", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    for path, target in WORD_TARGETS:
        actual = count_words(dig(content, path))
        if actual < target * 0.85:
            print(
                f"Warnung: '{path}' hat {actual} Woerter (Ziel {target}).",
                file=sys.stderr,
            )
        else:
            print(f"  OK: '{path}' {actual} Woerter (Ziel {target}).")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def paragraphs(items) -> str:
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"      <p>{esc(p)}</p>" for p in items or [] if str(p).strip())


def bullet_paragraphs(items) -> str:
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"      <p>— {esc(p)}</p>" for p in items or [] if str(p).strip())


def render_cover_items(items) -> str:
    return "\n".join(
        f'      <div class="ci"><h4>{esc(item.get("title"))}</h4>'
        f'<p>{esc(item.get("desc"))}</p></div>'
        for item in items or []
    )


def render_index_table(rows) -> str:
    return "\n".join(
        f'      <tr><td class="area">{esc(row.get("area"))}</td>'
        f'<td class="dot">{esc(row.get("dot"))}</td>'
        f'<td class="note">{esc(row.get("note"))}</td></tr>'
        for row in rows or []
    )


def render_csee_items(items) -> str:
    return "\n".join(
        f'    <div class="csee-item">\n'
        f'      <h4>{esc(item.get("headline"))}</h4>\n'
        f'      <p>{esc(item.get("text"))}</p>\n'
        f'      <p class="relevanz"><b>Relevanz für den Schuhretail:</b> '
        f'{esc(item.get("relevanz"))}</p>\n'
        f'    </div>'
        for item in items or []
    )


def render_signal_items(items) -> str:
    return "\n".join(
        f'      <div class="signal-item">\n'
        f'        <div class="tag">{esc(item.get("tag"))}</div>\n'
        f'        <h4>{esc(item.get("headline"))}</h4>\n'
        f'        <p>{esc(item.get("text"))}</p>\n'
        f'      </div>'
        for item in items or []
    )


def render_wissen_items(items) -> str:
    return "\n".join(
        f'    <div class="wissen-item">\n'
        f'      <div class="kicker">{esc(item.get("kicker"))}</div>\n'
        f'      <h3>{esc(item.get("headline"))}</h3>\n'
        f'      <p>{esc(item.get("text"))}</p>\n'
        f'    </div>'
        for item in items or []
    )


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    return int(match.group(1)) + 1 if match else 1


def render_html(content: dict, sources: list[str], edition_number: int,
                date_str: str, time_str: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    toc = content.get("toc_teasers") or {}
    deutschland = content.get("deutschland") or {}
    politik = content.get("politik") or {}
    finanzen = content.get("finanzen") or {}
    mechanics = content.get("mechanics") or {}
    usa = content.get("usa") or {}
    geopolitik = content.get("geopolitik") or {}
    future = content.get("future") or {}
    deepdive = content.get("deepdive") or {}
    quote = content.get("quote") or {}

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
        "§§SOURCES§§": esc(", ".join(sources) or "diverse RSS-Feeds"),
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    leftover = re.findall(r"§§[A-Z_]+§§", template)
    if leftover:
        print(
            f"Fehler: unersetzte Platzhalter im Template: {sorted(set(leftover))}",
            file=sys.stderr,
        )
        sys.exit(1)
    return template


def main() -> None:
    # --check validiert nur und schreibt nichts. Der Redaktionsschritt nutzt
    # das, um sein eigenes JSON zu pruefen und nachzubessern - ein echter
    # Render-Lauf wuerde dort die Ausgabennummer faelschlich hochzaehlen.
    check_only = "--check" in sys.argv

    if not CONTENT_PATH.exists():
        print(
            f"Fehler: {CONTENT_PATH} nicht gefunden. Hat der Redaktionsschritt "
            f"(Claude) die Datei geschrieben?",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        content = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Fehler: build/content.json ist kein gueltiges JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    validate(content)

    if check_only:
        print("build/content.json ist vollstaendig und valide.")
        return

    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.exists() else []

    previous_html = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else ""
    edition_number = next_edition_number(previous_html)

    now_berlin = datetime.now(ZoneInfo("Europe/Berlin"))
    new_html = render_html(
        content, sources, edition_number,
        german_date(now_berlin), now_berlin.strftime("%H:%M"),
    )

    INDEX_PATH.write_text(new_html, encoding="utf-8")
    total_words = total_word_count(content)
    print(
        f"Ausgabe {edition_number:03d} geschrieben "
        f"({len(new_html):,} Zeichen HTML, ~{total_words:,} Woerter Text)."
    )


if __name__ == "__main__":
    main()
