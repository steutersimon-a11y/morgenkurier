"""RSS-Beschaffung fuer den Morgenkurier.

Gemeinsame Bibliothek fuer fetch_material.py. Enthaelt die Feed-Liste und
das namespace-agnostische Parsen (RSS 2.0, RSS 1.0/RDF und Atom).

Eine kaputte Quelle darf den Lauf nie stoppen - sie liefert dann eben
weniger Schlagzeilen."""

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

# Freie, oeffentliche RSS-Feeds - keine Anmeldung, kein API-Key, keine Kosten.
FEEDS = {
    "Deutschland (Tagesschau Inland)": "https://www.tagesschau.de/inland/index~rss2.xml",
    "Welt (Tagesschau Ausland)": "https://www.tagesschau.de/ausland/index~rss2.xml",
    "Wirtschaft (Tagesschau)": "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
    "Wirtschaft (n-tv)": "https://www.n-tv.de/wirtschaft/rss",
    "Wirtschaft (Spiegel)": "https://www.spiegel.de/wirtschaft/index.rss",
    "USA & Kanada (BBC)": "http://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "Welt (BBC World)": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Europa & Welt (Deutsche Welle)": "https://rss.dw.com/rdf/rss-de-all",
    "Asien (BBC)": "http://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "Technologie (Heise)": "https://www.heise.de/rss/heise-atom.xml",
    "Technologie (Golem)": "https://rss.golem.de/rss.php?feed=RSS2.0",
    "Wissenschaft (ScienceDaily)": "https://www.sciencedaily.com/rss/all.xml",
}

# Claude bekommt das gesamte Material in einem Rutsch und hat mit 1M Kontext
# reichlich Luft - daher grosszuegiger als in der alten Groq-Pipeline, wo ein
# 12K-Token-pro-Minute-Limit die Menge diktiert hat.
ITEMS_PER_FEED = 14
SNIPPET_LEN = 320

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

USER_AGENT = "Mozilla/5.0 (compatible; MorgenkurierBot/1.0)"


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = HTML_TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def fetch_feed(url: str, limit: int = ITEMS_PER_FEED) -> list[tuple[str, str]]:
    """Returns a list of (title, description) tuples. Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
    all_items: dict[str, list[tuple[str, str]]] = {}
    for label, url in FEEDS.items():
        items = fetch_feed(url)
        if items:
            all_items[label] = items
        else:
            print(f"Hinweis: keine Eintraege von '{label}'.", file=sys.stderr)
    return all_items


def material_block(all_items: dict[str, list[tuple[str, str]]]) -> str:
    """Rendert das gesamte Rohmaterial als Markdown, nach Quelle gegliedert."""
    sections = []
    for label, items in all_items.items():
        lines = [f"## {label}"]
        for title, desc in items:
            snippet = f" — {desc[:SNIPPET_LEN]}" if desc else ""
            lines.append(f"- {title}{snippet}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "(Heute lag kein Material vor.)"
