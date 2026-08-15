"""Schritt 3 der taeglichen Pipeline: HTML rendern.

Liest den von Claude geschriebenen redaktionellen Inhalt (build/content.json)
und rendert ihn deterministisch in scripts/template.html -> index.html.

Bewusste Arbeitsteilung: Claude liefert ausschliesslich Text (JSON), niemals
Markup. Damit kann sich das Layout von Ausgabe zu Ausgabe nicht veraendern -
alle HTML-Struktur, alle CSS-Klassen und das gesamte Escaping liegen hier.

Architektur "ressorts": Statt fuer jedes Ressort ein eigenes, fest verdrahtetes
Feld-Set zu pflegen (frueherer Stand: deutschland/politik/finanzen/usa/...
als Einzelfelder), ist der Inhalt jetzt eine Liste variabler Ressort-Objekte
(siehe RessortSchema in editorial_brief.md). Ein Ressort kann einen
Hauptartikel, mehrere kleine Artikel, eine optionale Erklaerbox und optionale
Kurzmeldungen haben. Das ist der einzige Weg, wie "18 FAS-artige Ressorts,
jedes mit Haupt- und Kurzartikeln" wartbar bleibt, ohne fuer jedes Ressort
erneut eigene render_*()-Funktionen und Platzhalter zu schreiben.

Der Lauf bricht ab, wenn Pflichtfelder fehlen: eine halb leere Zeitung soll
nicht deployt werden. Unterschrittene Wortzahlen sind dagegen nur Warnungen -
lieber eine etwas kurze Ausgabe als gar keine. Es gibt bewusst KEINE
Obergrenze fuer Artikellaenge.

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

# Feste Positionen VOR den Ressorts (Sektionsnummer im Heft).
SECNUM_INHALT = 2
SECNUM_INDEX = 3
SECNUM_WELT_AM_MORGEN = 4
SECNUM_WATCHLIST = 5
SECNUM_ERSTES_RESSORT = 6

# Weicher Richtwert fuer Hauptartikel - keine Obergrenze, nur eine Warnung
# nach unten. Bei "Umfang soll enorm steigen" ist die Untergrenze bewusst
# grosszuegig gehalten statt eng, damit kurze, aber vollstaendige Ressorts
# (z.B. an material-armen Tagen) nicht staendig als Fehler markiert werden.
HAUPTARTIKEL_WORD_TARGET = 350
DEEPDIVE_WORD_TARGET = 400


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------

REQUIRED_TOP_LISTS = [
    ("cover_items", 3),
    ("index_table", 5),
    ("welt_am_morgen", 6),
    ("finanzmarkt_watchlist", 3),
    ("ressorts", 6),
    ("signal", 3),
]

REQUIRED_TOP_STRINGS = [
    "cover_headline", "cover_dek", "gedanke",
    "deepdive.headline",
    "quote.text", "quote.author",
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


def validate_ressorts(ressorts) -> list[str]:
    errors: list[str] = []
    if not isinstance(ressorts, list):
        return ["'ressorts' fehlt oder ist keine Liste."]

    seen_ids: set[str] = set()
    for i, r in enumerate(ressorts):
        label = f"ressorts[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{label} ist kein Objekt.")
            continue

        rid = r.get("id")
        if not rid or not isinstance(rid, str):
            errors.append(f"{label}: 'id' fehlt oder ist kein String.")
        elif rid in seen_ids:
            errors.append(f"{label}: 'id' \"{rid}\" ist nicht eindeutig.")
        else:
            seen_ids.add(rid)

        tag = rid or label
        if not r.get("name"):
            errors.append(f"ressort '{tag}': 'name' fehlt.")
        if not r.get("toc_teaser"):
            errors.append(f"ressort '{tag}': 'toc_teaser' fehlt.")

        hauptartikel = r.get("hauptartikel")
        kleine = r.get("kleine_artikel") or []
        has_haupt = (
            isinstance(hauptartikel, dict)
            and hauptartikel.get("title")
            and len(hauptartikel.get("paragraphs") or []) >= 3
        )
        has_kleine = isinstance(kleine, list) and len(kleine) >= 2

        if not has_haupt and not has_kleine:
            errors.append(
                f"ressort '{tag}': braucht entweder einen Hauptartikel "
                f"(Titel + mind. 3 Absaetze) oder mind. 2 kleine Artikel."
            )
        for j, item in enumerate(kleine):
            if not isinstance(item, dict) or not item.get("title") or not item.get("text"):
                errors.append(f"ressort '{tag}' kleine_artikel[{j}]: 'title' oder 'text' fehlt.")

    return errors


def validate_watchlist(entries) -> list[str]:
    errors: list[str] = []
    if not isinstance(entries, list):
        return ["'finanzmarkt_watchlist' fehlt oder ist keine Liste."]
    for i, entry in enumerate(entries):
        for field in ("ticker", "name", "kurs_kontext", "entwicklung", "einordnung"):
            if not isinstance(entry, dict) or not entry.get(field):
                errors.append(f"finanzmarkt_watchlist[{i}]: '{field}' fehlt.")
    return errors


def validate(content: dict) -> None:
    errors: list[str] = []

    for path, minimum in REQUIRED_TOP_LISTS:
        value = dig(content, path)
        if not isinstance(value, list):
            errors.append(f"'{path}' fehlt oder ist keine Liste.")
        elif len(value) < minimum:
            errors.append(f"'{path}' hat {len(value)} Eintraege, mindestens {minimum} erwartet.")

    for path in REQUIRED_TOP_STRINGS:
        value = dig(content, path)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"'{path}' fehlt oder ist leer.")

    errors += validate_ressorts(content.get("ressorts"))
    errors += validate_watchlist(content.get("finanzmarkt_watchlist"))

    if len(dig(content, "deepdive.paragraphs") or []) < 3:
        errors.append("'deepdive.paragraphs' hat weniger als 3 Absaetze.")

    if errors:
        print("Fehler: build/content.json ist unvollstaendig.", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    for r in content.get("ressorts") or []:
        haupt = r.get("hauptartikel")
        rid = r.get("id", "?")
        if haupt and haupt.get("paragraphs"):
            words = count_words(haupt["paragraphs"])
            if words < HAUPTARTIKEL_WORD_TARGET * 0.5:
                print(
                    f"Warnung: Hauptartikel '{rid}' hat nur {words} Woerter "
                    f"(Richtwert ab ~{HAUPTARTIKEL_WORD_TARGET}, kein Maximum).",
                    file=sys.stderr,
                )
            else:
                print(f"  OK: Hauptartikel '{rid}': {words} Woerter.")

    dd_words = count_words(dig(content, "deepdive.paragraphs"))
    if dd_words < DEEPDIVE_WORD_TARGET * 0.85:
        print(f"Warnung: Deep Dive hat {dd_words} Woerter (Ziel {DEEPDIVE_WORD_TARGET}).", file=sys.stderr)
    else:
        print(f"  OK: Deep Dive {dd_words} Woerter (Ziel {DEEPDIVE_WORD_TARGET}).")


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


def render_signal_items(items) -> str:
    return "\n".join(
        f'      <div class="signal-item">\n'
        f'        <div class="tag">{esc(item.get("tag"))}</div>\n'
        f'        <h4>{esc(item.get("headline"))}</h4>\n'
        f'        <p>{esc(item.get("text"))}</p>\n'
        f'      </div>'
        for item in items or []
    )


def render_watchlist(entries) -> str:
    return "\n".join(
        f'      <div class="watch-item">\n'
        f'        <div class="watch-ticker">{esc(entry.get("ticker"))}</div>\n'
        f'        <h4>{esc(entry.get("name"))}</h4>\n'
        f'        <p class="watch-kurs">{esc(entry.get("kurs_kontext"))}</p>\n'
        f'        <p>{esc(entry.get("entwicklung"))}</p>\n'
        f'        <p class="watch-einordnung">{esc(entry.get("einordnung"))}</p>\n'
        f'      </div>'
        for entry in entries or []
    )


def render_headlines_block(items) -> str:
    if not items:
        return ""
    return (
        '<h3 class="dept">Kurz notiert</h3>\n'
        '    <div class="cols">\n'
        + bullet_paragraphs(items) + "\n"
        '    </div>'
    )


def render_hauptartikel_block(article) -> str:
    if not article or not article.get("paragraphs"):
        return ""
    return (
        f'<h3 class="dept">{esc(article.get("title"))}</h3>\n'
        f'    <div class="cols">\n'
        + paragraphs(article.get("paragraphs")) + "\n"
        f'    </div>'
    )


def render_kleine_artikel_block(items) -> str:
    if not items:
        return ""
    parts = ['<h3 class="dept">Weitere Meldungen</h3>']
    for item in items:
        block = [
            '    <div class="mini-item">',
            f'      <h4>{esc(item.get("title"))}</h4>',
            f'      <p>{esc(item.get("text"))}</p>',
        ]
        relevanz = item.get("relevanz")
        if relevanz:
            block.append(f'      <p class="relevanz"><b>Relevanz:</b> {esc(relevanz)}</p>')
        block.append('    </div>')
        parts.append("\n".join(block))
    return "\n".join(parts)


def render_erklaerbox_block(box) -> str:
    if not box or not box.get("paragraphs"):
        return ""
    return (
        '<div class="explainer">\n'
        f'      <div class="tag">{esc(box.get("tag") or "Erklärt")}</div>\n'
        f'      <h4>{esc(box.get("headline"))}</h4>\n'
        + paragraphs(box.get("paragraphs")) + "\n"
        '    </div>'
    )


def render_ressort_section(ressort: dict, secnum: int) -> str:
    rid = esc(ressort.get("id"))
    name = esc(ressort.get("name"))
    parts = [
        f'  <section id="{rid}">',
        f'    <div class="secnum">{secnum:02d}</div>',
        f'    <h2 class="sectitle">{name}</h2>',
    ]
    if ressort.get("kicker"):
        parts.append(f'    <div class="secdesc">{esc(ressort.get("kicker"))}</div>')

    for block in (
        render_headlines_block(ressort.get("headlines")),
        render_erklaerbox_block(ressort.get("erklaerbox")),
        render_hauptartikel_block(ressort.get("hauptartikel")),
        render_kleine_artikel_block(ressort.get("kleine_artikel")),
    ):
        if block:
            parts.append("    " + block)

    parts.append('    <a class="backtotoc" href="#inhalt">↑ Zurück zum Inhalt</a>')
    parts.append('  </section>')
    return "\n".join(parts)


def render_ressort_sections(ressorts: list) -> str:
    return "\n\n".join(
        render_ressort_section(r, SECNUM_ERSTES_RESSORT + i)
        for i, r in enumerate(ressorts or [])
    )


def render_nav_links(ressorts: list) -> str:
    links = ['<a class="brand" href="#top">MORGENKURIER</a>', '<a href="#inhalt">Inhalt</a>']
    for r in ressorts or []:
        links.append(f'<a href="#{esc(r.get("id"))}">{esc(r.get("name"))}</a>')
    links.append('<a href="#deepdive">Deep Dive</a>')
    return "\n    ".join(links)


def render_ressort_ticker(ressorts: list) -> str:
    return " · ".join(esc(r.get("name")) for r in ressorts or [])


def render_toc_ressort_rows(ressorts: list) -> str:
    rows = []
    for i, r in enumerate(ressorts or []):
        num = SECNUM_ERSTES_RESSORT + i
        rows.append(
            f'<a href="#{esc(r.get("id"))}"><div class="toc-row"><div class="num">{num:02d}</div>'
            f'<div class="body"><h4>{esc(r.get("name"))}</h4><p>{esc(r.get("toc_teaser"))}</p></div></div></a>'
        )
    return "\n    ".join(rows)


def german_date(dt: datetime) -> str:
    return f"{WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {MONTHS_DE[dt.month - 1]} {dt.year}"


def next_edition_number(previous_html: str) -> int:
    match = re.search(r"Ausgabe\s+0*(\d+)", previous_html)
    return int(match.group(1)) + 1 if match else 1


def render_html(content: dict, sources: list[str], edition_number: int,
                date_str: str, time_str: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    ressorts = content.get("ressorts") or []
    n = len(ressorts)
    secnum_signal = SECNUM_ERSTES_RESSORT + n
    secnum_deepdive = secnum_signal + 1
    secnum_zitat = secnum_deepdive + 1

    deepdive = content.get("deepdive") or {}
    quote = content.get("quote") or {}

    edition_label = f"Ausgabe {edition_number:03d}"
    replacements = {
        "§§EYEBROW§§": esc(edition_label),
        "§§EDITION_LABEL§§": esc(edition_label),
        "§§DATE_TIME§§": esc(f"{date_str} · {time_str} Uhr"),
        "§§RESSORT_TICKER§§": render_ressort_ticker(ressorts),
        "§§NAV_LINKS§§": render_nav_links(ressorts),
        "§§COVER_HEADLINE§§": esc(content.get("cover_headline")),
        "§§COVER_DEK§§": esc(content.get("cover_dek")),
        "§§COVER_LIST§§": render_cover_items(content.get("cover_items")),
        "§§TOC_RESSORT_ROWS§§": render_toc_ressort_rows(ressorts),
        "§§NUM_SIGNAL§§": f"{secnum_signal:02d}",
        "§§NUM_DEEPDIVE§§": f"{secnum_deepdive:02d}",
        "§§NUM_ZITAT§§": f"{secnum_zitat:02d}",
        "§§INDEX_TABLE§§": render_index_table(content.get("index_table")),
        "§§WELT_AM_MORGEN§§": bullet_paragraphs(content.get("welt_am_morgen")),
        "§§WATCHLIST_ITEMS§§": render_watchlist(content.get("finanzmarkt_watchlist")),
        "§§RESSORT_SECTIONS§§": render_ressort_sections(ressorts),
        "§§SIGNAL_ITEMS§§": render_signal_items(content.get("signal")),
        "§§DEEPDIVE_HEADLINE§§": esc(deepdive.get("headline")),
        "§§DEEPDIVE_PARAGRAPHS§§": paragraphs(deepdive.get("paragraphs")),
        "§§QUOTE_TEXT§§": esc(quote.get("text")),
        "§§QUOTE_AUTHOR§§": esc(quote.get("author")),
        "§§GEDANKE§§": esc(content.get("gedanke")),
        "§§SOURCES§§": esc(", ".join(sources) or "diverse RSS-Feeds und Websuche"),
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
        f"({len(new_html):,} Zeichen HTML, ~{total_words:,} Woerter Text, "
        f"{len(content.get('ressorts') or [])} Ressorts)."
    )


if __name__ == "__main__":
    main()
