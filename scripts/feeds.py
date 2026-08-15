"""RSS-Beschaffung fuer den Morgenkurier.

Gemeinsame Bibliothek fuer fetch_material.py. Enthaelt die Feed-Liste und
das namespace-agnostische Parsen (RSS 2.0, RSS 1.0/RDF und Atom).

WICHTIG - Reichweite dieser Liste: Ob eine URL hier tatsaechlich erreichbar
ist, haengt von der Netzwerk-Policy der Cloud-Umgebung ab, in der die
Routine laeuft (siehe claude.ai/code/routines -> Environment -> Network
access). Mit "Trusted"/"Custom" sind nur wenige Entwickler-Domains frei;
die grosse Mehrheit dieser ~110 Nachrichten-Domains braucht "Full" Network
Access, um ueberhaupt eine Chance zu haben. Mit einer Domain-Allowlist von
Hand zu pflegen ist bei dieser Groessenordnung nicht praktikabel.

Deshalb ist RSS hier bewusst NICHT der primaere Rechercheweg, sondern ein
Bonus: Jede kaputte/blockierte Quelle liefert einfach keine Eintraege statt
den Lauf zu stoppen (siehe fetch_feed). Die eigentliche, verlaessliche
Recherche macht das Sprachmodell selbst per WebSearch/WebFetch - die laufen
serverseitig ueber Anthropic und sind von dieser Netzwerk-Policy nicht
betroffen. Siehe editorial_brief.md, Schritt 0 und 2."""

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

# Kuratierte, oeffentlich bekannte RSS-Feeds, nach Kategorie geordnet.
# Deutsche/CSEE-Kernquellen sind bewusst noch enthalten (Deutschland- und
# CSEE-Ressort brauchen sie inhaltlich), aber international stark
# aufgestockt - der Nutzer wollte "mindestens 100 internationale Quellen",
# weil die deutschen Quellen beim ersten Lauf schlecht durchkamen.
FEED_CATEGORIES: dict[str, dict[str, str]] = {
    "Weltnachrichten": {
        "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
        "BBC Europe": "http://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "BBC US & Canada": "http://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
        "BBC Asia": "http://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "BBC Africa": "http://feeds.bbci.co.uk/news/world/africa/rss.xml",
        "BBC Latin America": "http://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
        "BBC Middle East": "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "The Guardian World": "https://www.theguardian.com/world/rss",
        "The Guardian International": "https://www.theguardian.com/international/rss",
        "NYT World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "NPR World": "https://feeds.npr.org/1004/rss.xml",
        "NPR News": "https://feeds.npr.org/1001/rss.xml",
        "France24 English": "https://www.france24.com/en/rss",
        "DW English": "https://rss.dw.com/xml/rss-en-all",
        "euronews": "https://www.euronews.com/rss",
        "CNN World": "http://rss.cnn.com/rss/cnn_world.rss",
        "CNN Top Stories": "http://rss.cnn.com/rss/cnn_topstories.rss",
        "Reuters World (Google-News-Spiegel)": "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com&hl=en-US&gl=US&ceid=US:en",
    },
    "USA & Nordamerika": {
        "NYT US": "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
        "NPR Politics": "https://feeds.npr.org/1014/rss.xml",
        "Politico": "https://rss.politico.com/politics-news.xml",
        "The Hill": "https://thehill.com/feed/",
        "CNN Politics": "http://rss.cnn.com/rss/cnn_allpolitics.rss",
        "CBC Top Stories (Kanada)": "https://www.cbc.ca/cmlink/rss-topstories",
        "Axios (Google-News-Spiegel)": "https://news.google.com/rss/search?q=when:24h+allinurl:axios.com&hl=en-US&gl=US&ceid=US:en",
    },
    "Europa": {
        "The Guardian UK": "https://www.theguardian.com/uk/rss",
        "The Guardian Europe": "https://www.theguardian.com/world/europe-news/rss",
        "Politico Europe": "https://www.politico.eu/feed/",
        "EURACTIV": "https://www.euractiv.com/feed/",
        "Spiegel International": "https://www.spiegel.de/international/index.rss",
        "Kyiv Independent": "https://kyivindependent.com/feed/",
        "The Moscow Times": "https://www.themoscowtimes.com/rss/news",
        "The Local Germany": "https://www.thelocal.de/feed/",
        "Politico (EU-Wirtschaft)": "https://www.politico.eu/feed/?post_type=article&category=economy",
    },
    "Asien & Pazifik": {
        "SCMP (Hongkong)": "https://www.scmp.com/rss/91/feed",
        "Japan Times": "https://www.japantimes.co.jp/feed/",
        "Times of India": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "The Hindu": "https://www.thehindu.com/news/national/feeder/default.rss",
        "Straits Times Asia": "https://www.straitstimes.com/news/asia/rss.xml",
        "The Diplomat": "https://thediplomat.com/feed/",
        "Bangkok Post": "https://www.bangkokpost.com/rss/data/topstories.xml",
        "Korea Herald": "http://www.koreaherald.com/rss/020000000000.xml",
        "ABC News Australia": "https://www.abc.net.au/news/feed/51120/rss.xml",
        "Nikkei Asia (Google-News-Spiegel)": "https://news.google.com/rss/search?q=when:24h+allinurl:asia.nikkei.com&hl=en-US&gl=US&ceid=US:en",
    },
    "Naher Osten & Afrika": {
        "Times of Israel": "https://www.timesofisrael.com/feed/",
        "Al-Monitor": "https://www.al-monitor.com/rss.xml",
        "Middle East Eye": "https://www.middleeasteye.net/rss",
        "Arab News": "https://www.arabnews.com/rss.xml",
        "Africanews": "https://www.africanews.com/feed/",
        "AllAfrica Headlines": "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
    },
    "Wirtschaft & Maerkte": {
        "CNBC Top News": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "CNBC Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "CNBC Finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "MarketWatch Top Stories": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "MarketWatch Realtime": "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
        "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
        "Investing.com": "https://www.investing.com/rss/news.rss",
        "Business Insider": "https://feeds.businessinsider.com/custom/all",
        "Fortune": "https://fortune.com/feed/",
        "Forbes Business": "https://www.forbes.com/business/feed/",
        "Fox Business": "https://feeds.foxnews.com/foxbusiness/latest",
        "Seeking Alpha Market Currents": "https://seekingalpha.com/market_currents.xml",
        "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "CoinTelegraph": "https://cointelegraph.com/rss",
        "The Economist (Google-News-Spiegel)": "https://news.google.com/rss/search?q=when:24h+allinurl:economist.com&hl=en-US&gl=US&ceid=US:en",
    },
    "Technologie": {
        "TechCrunch": "https://techcrunch.com/feed/",
        "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
        "The Verge": "https://www.theverge.com/rss/index.xml",
        "Wired": "https://www.wired.com/feed/rss",
        "Engadget": "https://www.engadget.com/rss.xml",
        "VentureBeat": "https://venturebeat.com/feed/",
        "MIT Technology Review": "https://www.technologyreview.com/feed/",
        "ZDNet": "https://www.zdnet.com/news/rss.xml",
        "TechRadar": "https://www.techradar.com/rss",
        "9to5Mac": "https://9to5mac.com/feed/",
        "Android Authority": "https://www.androidauthority.com/feed/",
        "Dark Reading (IT-Sicherheit)": "https://www.darkreading.com/rss.xml",
        "Heise": "https://www.heise.de/rss/heise-atom.xml",
        "Golem": "https://rss.golem.de/rss.php?feed=RSS2.0",
    },
    "Wissenschaft": {
        "ScienceDaily": "https://www.sciencedaily.com/rss/all.xml",
        "Nature News": "https://www.nature.com/nature.rss",
        "Science Magazine News": "https://www.science.org/rss/news_current.xml",
        "Scientific American": "http://rss.sciam.com/ScientificAmerican-Global",
        "Space.com": "https://www.space.com/feeds/all",
        "Phys.org": "https://phys.org/rss-feed/",
        "New Scientist": "https://www.newscientist.com/feed/home/",
    },
    "Kultur & Feuilleton": {
        "The Atlantic": "https://www.theatlantic.com/feed/all/",
        "New Yorker": "https://www.newyorker.com/feed/everything",
        "Vox": "https://www.vox.com/rss/index.xml",
        "Slate": "https://slate.com/feeds/all.rss",
        "The Art Newspaper": "https://www.theartnewspaper.com/rss",
        "Smithsonian Magazine": "https://www.smithsonianmag.com/rss/latest_articles/",
    },
    "Unterhaltung & Medien": {
        "Variety": "https://variety.com/feed/",
        "Hollywood Reporter": "https://www.hollywoodreporter.com/feed/",
        "Deadline": "https://deadline.com/feed/",
        "IndieWire": "https://www.indiewire.com/feed/",
        "Rolling Stone": "https://www.rollingstone.com/feed/",
        "Pitchfork": "https://pitchfork.com/rss/news/",
        "Billboard": "https://www.billboard.com/feed/",
    },
    "Sport": {
        "BBC Sport": "http://feeds.bbci.co.uk/sport/rss.xml",
        "ESPN": "https://www.espn.com/espn/rss/news",
        "Sky Sports": "https://www.skysports.com/rss/12040",
        "Marca (englisch)": "https://www.marca.com/en/rss.xml",
    },
    "Reise, Karriere & Wohnen": {
        "Conde Nast Traveler": "https://www.cntraveler.com/feed/rss",
        "Lonely Planet": "https://www.lonelyplanet.com/news/feed",
        "Fast Company": "https://www.fastcompany.com/technology/rss",
        "Inc.": "https://www.inc.com/rss",
        "Harvard Business Review": "https://hbr.org/rss/home",
    },
    "CSEE & Deutschland": {
        "Tagesschau Inland": "https://www.tagesschau.de/inland/index~rss2.xml",
        "Tagesschau Ausland": "https://www.tagesschau.de/ausland/index~rss2.xml",
        "Tagesschau Wirtschaft": "https://www.tagesschau.de/wirtschaft/index~rss2.xml",
        "n-tv Wirtschaft": "https://www.n-tv.de/wirtschaft/rss",
        "Spiegel Wirtschaft": "https://www.spiegel.de/wirtschaft/index.rss",
        "The Local Austria": "https://www.thelocal.at/feed/",
        "Radio Prague International (CZ)": "https://english.radio.cz/rss/",
    },
}

# Flach als {Anzeigename: URL}, Kategorie als Praefix - so bleibt
# material_block() (Gruppierung nach Ueberschrift) unveraendert nutzbar.
FEEDS: dict[str, str] = {
    f"{category} — {label}": url
    for category, feeds in FEED_CATEGORIES.items()
    for label, url in feeds.items()
}

# Mit ~110 Feeds statt vormals 12 werden Einzelwerte bewusst konservativ
# gehalten, damit ein Lauf mit vielen kaputten/blockierten Quellen nicht
# ausufert: kurzer Timeout, wenige Eintraege pro Feed, kurze Snippets.
ITEMS_PER_FEED = 6
SNIPPET_LEN = 200
FETCH_TIMEOUT_SECONDS = 8

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
    """Returns a list of (title, description) tuples. Never raises - eine
    kaputte oder von der Netzwerk-Policy blockierte Quelle liefert einfach
    eine leere Liste, statt den gesamten Lauf zu stoppen."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
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
