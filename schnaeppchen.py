# -*- coding: utf-8 -*-
"""Taegliche Schnaeppchen-Suche fuer neue Dauerbetrieb-HDDs. Nur auf diesem Windows-PC ausfuehren."""

from __future__ import annotations

import argparse
import json
import random
import threading
import re
import time
import sys
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SEEN_PATH = ROOT / "gesehen.json"
CONFIG_PATH = ROOT / "config.json"
REF_PATH = ROOT / "referenz.json"
ROUTE_PATH = ROOT / "route.json"
GEO_CACHE_PATH = ROOT / "geo_cache.json"

NO_DEAL = "Heute keine neuen HDD-Schnaeppchen."
# Deal-Grenze (inkl. Versand/Einfuhr). Einzige Stelle; in config.json ueberschreibbar: "max_eur_per_tb": 32
MAX_EUR_PER_TB = 32.0
# Kandidatenfenster fuer Suche/Vorfilter: grosszuegig ueber der Grenze, damit nichts knapp darunter verloren geht
CANDIDATE_FACTOR = 1.15
CANDIDATE_EUR_PER_TB = MAX_EUR_PER_TB * CANDIDATE_FACTOR


def set_max_eur_per_tb(value) -> None:
    global MAX_EUR_PER_TB, CANDIDATE_EUR_PER_TB
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    if 10.0 <= v <= 100.0:
        MAX_EUR_PER_TB = v
        CANDIDATE_EUR_PER_TB = v * CANDIDATE_FACTOR
MIN_EUR_PER_TB = 15.0  # junk floor: <15 EUR/TB almost always wrong item/tray/GB noise
MIN_TB = 3.0
MAX_TB = 8.0
# Search price ceiling: 8 TB * ~38 EUR/TB plus buffer (bewusst weit, Filter passiert spaeter)
WATCH_CAP = 350
DETAIL_LIMIT = 14
TOP_N = 8

# Modus-abhaengige Einstellungen (Standard = taeglicher Lauf, --gruendlich = ausfuehrlich)
MODE = {
    "gruendlich": False,
    "pages": {"ebay": 1, "ebay_cap": 4, "kleinanzeigen": 1, "amazon": 1, "vinted": 1, "geizhals": 6},
    "detail_limit": DETAIL_LIMIT,  # pro Quelle (Detailphase laeuft parallel je Quelle)
    "verify_limit": 20,  # Vorschau-Deals pro Quelle, die zusaetzlich auf der Detailseite geprueft werden
    "top_n": TOP_N,
    "max_relaunches": 3,  # Browser-Neustarts pro Quelle
    "search_budget_s": 30 * 60,
    "detail_budget_s": 45 * 60,
}
GRUENDLICH_MODE = {
    "gruendlich": True,
    "pages": {"ebay": 1, "ebay_cap": 10, "kleinanzeigen": 3, "amazon": 3, "vinted": 1, "geizhals": 10},
    "detail_limit": 100,  # pro Quelle
    "verify_limit": 150,
    "top_n": 80,
    "max_relaunches": 6,
    "search_budget_s": 150 * 60,
    "detail_budget_s": 210 * 60,
}

# Mindestabstand (Sekunden, Start-zu-Start) zwischen zwei Seitenaufrufen DERSELBEN Seite.
# Frueher liefen die Quellen abwechselnd, dadurch lag pro Seite ~5-8 s dazwischen -> nicht schneller werden.
# eBay bewusst etwas langsamer (Blockseiten "Something went wrong").
PACE = {
    "ebay": (6.5, 9.5),
    "kleinanzeigen": (5.0, 7.0),
    "amazon": (5.0, 7.0),
    "vinted": (5.0, 7.0),
    "geizhals": (5.0, 7.0),
}
WORKER_ORDER = ("geizhals", "kleinanzeigen", "ebay", "amazon", "vinted")
VERIFY_SOURCES = ("ebay", "kleinanzeigen", "vinted", "amazon", "geizhals")
SOURCE_LABEL = {
    "ebay": "eBay",
    "kleinanzeigen": "Kleinanzeigen",
    "amazon": "Amazon",
    "vinted": "Vinted",
    "geizhals": "Geizhals",
}

# --gruendlich: Kapazitaet x Modell-Matrix + Teilenummern + generische Begriffe
G_CAPS = ("3TB", "4TB", "6TB", "8TB")
G_MODELS = (
    "WD Red Plus",
    "WD Red Pro",
    "WD Gold",
    "WD Ultrastar DC HC310",
    "WD Ultrastar DC HC320",
    "WD Ultrastar DC HC330",
    "HGST Ultrastar",
    "Seagate Exos 7E8",
    "Seagate Exos 7E10",
    "Seagate Exos X",
    "Seagate IronWolf",
    "Seagate IronWolf Pro",
    "Toshiba N300",
    "Toshiba MG04",
    "Toshiba MG06",
    "Toshiba MG07",
    "Toshiba MG08",
)
G_PARTNUMBERS = (
    "WD40EFPX", "WD60EFPX", "WD80EFPX", "WD40EFZZ", "WD60EFZX", "WD80EFZZ", "WD80EFBX",
    "WD4003FFBX", "WD4005FFBX", "WD6003FFBX", "WD8003FFBX", "WD8005FFBX",
    "WD4003FRYZ", "WD6003FRYZ", "WD8004FRYZ", "WD8005FRYZ",
    "HUS726T4TALA6L4", "HUS726T6TALE6L4", "HUS728T8TALE6L4", "HUH728080ALE600", "HUS726040ALE610",
    "ST4000NM000A", "ST6000NM021A", "ST8000NM000A", "ST8000NM017B", "ST4000NM002A", "ST6000NM019B",
    "ST4000VN006", "ST4000NE001", "ST6000NE000", "ST8000NE001", "ST8000VN004",
    "MG04ACA400E", "MG06ACA600E", "MG06ACA800E", "MG08ADA400E", "MG08ADA600E", "MG08ADA800E",
    "HDWG440", "HDWG460", "HDWG480",
)
G_GENERIC = (
    "NAS Festplatte neu 4TB", "NAS Festplatte neu 6TB", "NAS Festplatte neu 8TB", "NAS Festplatte neu",
    "enterprise HDD neu", "enterprise HDD neu 4TB", "enterprise HDD neu 8TB",
    "Festplatte 3TB Neu SATA", "Festplatte 4TB Neu SATA", "Festplatte 6TB Neu SATA", "Festplatte 8TB Neu SATA",
    "Server Festplatte neu 8TB", "3,5 Zoll Festplatte 8TB neu OVP",
)


def gruendlich_queries() -> list[str]:
    out: list[str] = []
    for model in G_MODELS:
        for cap in G_CAPS:
            out.append(f"{model} {cap}")
    out.extend(G_PARTNUMBERS)
    out.extend(G_GENERIC)
    seen: set[str] = set()
    uniq = []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq


def gruendlich_portal_queries() -> list[str]:
    out = [f"{m} {c}" for m in G_MODELS for c in G_CAPS]
    out += ["NAS Festplatte 8TB", "NAS Festplatte 6TB", "NAS Festplatte 4TB", "Enterprise HDD 8TB"]
    return out

HDD_QUERIES = [
    "HGST Ultrastar 3TB",
    "HGST Ultrastar 4TB",
    "HGST Ultrastar 6TB",
    "HGST Ultrastar 8TB",
    "Ultrastar 4TB Neu",
    "Ultrastar 6TB Neu",
    "Ultrastar 8TB Neu",
    "WD Gold 4TB",
    "WD Gold 6TB",
    "WD Gold 8TB",
    "WD Red Plus 4TB",
    "WD Red Plus 6TB",
    "WD Red Plus 8TB",
    "WD Red Pro 4TB",
    "WD Red Pro 6TB",
    "WD Red Pro 8TB",
    "Seagate Exos 4TB",
    "Seagate Exos 6TB",
    "Seagate Exos 8TB",
    "IronWolf Pro 4TB",
    "IronWolf Pro 6TB",
    "IronWolf Pro 8TB",
    "IronWolf 4TB Neu",
    "IronWolf 6TB Neu",
    "Toshiba N300 4TB",
    "Toshiba N300 6TB",
    "Toshiba N300 8TB",
    "Toshiba MG04",
    "Toshiba MG06",
    "Toshiba MG07",
    "Toshiba MG08",
    "Festplatte 4TB Neu SATA",
    "Festplatte 6TB Neu SATA",
    "Festplatte 8TB Neu SATA",
    "HDD 4TB Neu NAS",
    "HDD 6TB Neu NAS",
    "Enterprise HDD 4TB Neu",
    "Enterprise HDD 6TB Neu",
]


GEIZHALS_CAT_URL = ("https://geizhals.de/?cat=hde7s&xf=5704_3.5%22~5717_SATA+6Gb%2Fs~5717_SATA+3Gb%2Fs~5744_%3E%3D3TB"
                    "&sort=r&t=v&hloc=de&pagesize=100")

# eBay: wenige breite Suchen (Kategorie 56083, Preisband je Kapazitaet, 240/Seite, paginiert) ...
EBAY_CAP_QUERIES = ["3TB", "4TB", "5TB", "6TB", "8TB"]
# ... plus Modellfamilien ohne Kategorie (falls Angebote falsch einsortiert sind)
EBAY_FAMILY_QUERIES = [
    "Seagate Exos", "Ultrastar", "IronWolf", "WD Red Plus", "WD Red Pro", "WD Gold", "Toshiba MG", "Toshiba N300",
]

# Focused queries for Amazon / Geizhals / Vinted (retail search is stronger)
PORTAL_QUERIES = [
    "Ultrastar 4TB",
    "Ultrastar 6TB",
    "Ultrastar 8TB",
    "WD Gold 4TB",
    "WD Gold 6TB",
    "WD Gold 8TB",
    "WD Red Plus 4TB",
    "WD Red Plus 6TB",
    "WD Red Plus 8TB",
    "WD Red Pro 6TB",
    "WD Red Pro 8TB",
    "Seagate Exos 4TB",
    "Seagate Exos 6TB",
    "Seagate Exos 8TB",
    "IronWolf Pro 4TB",
    "IronWolf Pro 6TB",
    "IronWolf Pro 8TB",
    "IronWolf 4TB",
    "IronWolf 6TB",
    "Toshiba N300 4TB",
    "Toshiba N300 6TB",
    "Toshiba N300 8TB",
    "Toshiba MG07",
    "Toshiba MG08",
]

# Hard reject: used / refurbished / consumer SMR / wrong product class
REJECT = (
    "defekt",
    "bastler",
    "ersatzteil",
    "schrott",
    "fuer teile",
    "für teile",
    "refurbished",
    "remanufactured",
    "generalueberholt",
    "generalüberholt",
    "erneuert",
    "gepruefte ware",
    "geprüfte ware",
    "pulled drive",
    "hard drive pull",
    "hdd pull",
    "oem pull",
    "oem used",
    "used pull",
    "data recovery",
    "datenrettung",
    "second hand",
    "vorbesitzer",
    "betriebsstunden",
    "power on hours",
    "smart fehler",
    "smart error",
    "sektorenfehler",
    "bad sector",
    "usb festplatte",
    "externe festplatte",
    "external hdd",
    "enclosure",
    "gehaeuse leer",
    "gehäuse leer",
    "dockingstation",
    "barracuda",
    "wd blue",
    "wd green",
    "wd elements",
    "my passport",
    "expansion portable",
    "skyhawk",
    "surveillance",
    "videoaufzeichnung",
    "deskstar",
    "travelstar",
    "gelöscht",
    "geloescht",
    "rezertifiziert",
    "recertified",
    "recertifiziert",
    "renewed",
    "b-ware",
    "wie neu",
    "neuwertig",
    "white label",
    "whitelabel",
    "sshd",
    "wd purple",
    "purple",
    "wd av-gp",
    "wd av ",
    "this listing was deleted",
    "anzeige wurde gelöscht",
    "anzeige wurde geloescht",
)

# Word-boundary rejects (avoid matching unused/etc.)
REJECT_WORDS = (
    r"(?<!un)gebraucht",
    r"(?<!un)benutzt",
    r"\bused\b",
    r"\bpull\b",
    r"\bpulled\b",
    r"\bssd\b",
    r"\bnvme\b",
    r"\bm\.2\b",
    r"\b[pxs]300\b",  # Toshiba Desktop/Surveillance
    r"\bhdwd\d",  # Toshiba P300 Teilenummern
)

# Parts / empty trays / controllers (title+core) — not complete HDDs
# Paralleles SCSI (U160/U320, 68/80-pin) - nicht "Serial Attached SCSI"
SCSI_TITLE = r"(?<!attached )\bscsi\b|\bu(?:160|320)\b|\bultra\s*(?:160|320)\b|\b(?:68|80)[- ]?pin\b"

REJECT_PARTS = (
    r"\bpcb\b",
    r"\bcontroller\b",
    r"nur\s*(tray|caddy|rahmen|einschub)",
    r"ohne\s*(platte|festplatte|hdd)",
    r"\b(hdd\s*)?tray\b",
    r"\bcaddy\b",
)

# Preferred Dauerbetrieb families (CMR / enterprise / NAS 24/7)
PREFERRED = (
    (r"ultrastar|\bhgst\b|hitachi\s*ultrastar", "HGST/Ultrastar enterprise"),
    (r"\bwd\s*gold\b|western\s*digital\s*gold|wd\s*wd[a-z0-9]*g", "WD Gold enterprise"),
    (r"\bred\s*plus\b|wd\s*red\s*plus", "WD Red Plus NAS CMR"),
    (r"\bred\s*pro\b|wd\s*red\s*pro", "WD Red Pro NAS"),
    (r"\bexos\b|seagate\s*exos", "Seagate Exos enterprise"),
    (r"ironwolf\s*pro", "Seagate IronWolf Pro NAS"),
    (r"\bironwolf\b", "Seagate IronWolf NAS"),
    (r"toshiba\s*n300|\bn300\b", "Toshiba N300 NAS"),
    (r"toshiba\s*mg0[468]|toshiba\s*mg[0-9]{2}|\bmg0[468]\b|\bmg[0-9]{2}aca|\bmg[0-9]{2}ada", "Toshiba MG enterprise"),
)

# Explicit SMR / not-for-NAS consumer patterns when identifiable
SMR_OR_BAD = (
    r"\bsmr\b",
    r"wd\s*red(?!\s*(plus|pro))",  # older WD Red non-Plus often SMR
    r"barracuda",
    r"wd\s*blue",
    r"wd\s*green",
)


@dataclass
class Deal:
    model: str
    capacity_tb: float
    price: float
    shipping: float
    eur_per_tb: float
    condition: str
    url: str
    fazit: str
    score: float
    interface: str
    form: str
    dauerbetrieb: str
    source: str = ""
    import_fee: float = 0.0
    sas_only: bool = False
    geprueft: str = ""
    title: str = ""
    location: str = ""
    route_km: float | None = None
    on_route: bool = False
    pickup_price: float | None = None
    pickup_eur_per_tb: float | None = None
    status: str = "Neu"
    first_seen: str = ""
    pickup_only: bool = False   # qualifiziert nur bei Abholung (Abholpreis <= Limit bzw. 'nur Abholung')
    pickup_allowed: bool = True  # Kleinanzeigen: nicht 'nur Versand'

    def effective_ept(self) -> float:
        if self.on_route and self.pickup_eur_per_tb:
            return min(self.eur_per_tb, self.pickup_eur_per_tb)
        return self.eur_per_tb


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    data = json.loads(SEEN_PATH.read_text(encoding="utf-8-sig"))
    return set(data.get("urls", []))


def load_seen_meta() -> dict[str, dict]:
    """Zusatzinfos je URL (first_seen/last_seen/Preis/Standort). Alte Dateien ohne 'meta': erstes Sehen
    spaetestens am Aenderungsdatum der Datei."""
    if not SEEN_PATH.exists():
        return {}
    data = json.loads(SEEN_PATH.read_text(encoding="utf-8-sig"))
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    legacy = datetime.fromtimestamp(SEEN_PATH.stat().st_mtime).strftime("%Y-%m-%d")
    for u in data.get("urls", []):
        m = meta.setdefault(u, {})
        if not m.get("first_seen"):
            m["first_seen"] = legacy
            m["first_seen_note"] = "spaetestens"
    return meta


def save_seen(urls: set[str], meta: dict[str, dict] | None = None) -> None:
    data: dict = {"urls": sorted(urls)}
    if meta:
        data["meta"] = {u: meta[u] for u in sorted(meta) if u in urls}
    SEEN_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def seen_source(url: str) -> str:
    for dom, src in (("ebay.", "ebay"), ("kleinanzeigen.", "kleinanzeigen"), ("vinted.", "vinted"),
                     ("amazon.", "amazon"), ("geizhals.", "geizhals")):
        if dom in url:
            return src
    return ""


def seen_meta_entry(deal: Deal, old: dict | None, today: str) -> dict:
    m = dict(old or {})
    m.setdefault("first_seen", today)
    m["last_seen"] = today
    m.update({"title": deal.title, "model": deal.model, "source": deal.source, "price": deal.price,
              "shipping": deal.shipping, "capacity_tb": deal.capacity_tb, "eur_per_tb": round(deal.eur_per_tb, 2),
              "location": deal.location, "route_km": deal.route_km})
    return m


def norm_url(url: str) -> str:
    url = url.split("?")[0].split("#")[0]
    url = re.sub(r"/+$", "", url)
    return url


def parse_price(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,4}(?:\.\d{3})?)", text.replace("\xa0", " "))
    if not match:
        return None
    raw = match.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_shipping(text: str) -> float:
    """Extract shipping EUR; 0 if free/Abholung/unknown."""
    if not text:
        return 0.0
    low = text.lower().replace("\xa0", " ")
    # eBay-Karten/Detail: "+EUR 4,99 Lieferung" / "EUR 4,99 Versand" / "Versand: EUR 5,99"
    m_eb = re.search(r"\+\s*eur\s*(\d{1,3},\d{2})\s*(?:versand|lieferung)", low)
    if m_eb:
        return float(m_eb.group(1).replace(",", "."))
    if re.search(r"kostenloser?\s+(versand|lieferung)|(versand|lieferung)\s*gratis|gratis\s+(\d-\d\s+tage\s+)?lieferung|free\s*shipping|\+?\s*0[,.]0{1,2}\s*€?\s*versand", low):
        return 0.0
    m_eb2 = re.search(r"(?:versand|lieferung)[ \t]*:[ \t]*eur[ \t]*(\d{1,3},\d{2})", low)
    if m_eb2:
        return float(m_eb2.group(1).replace(",", "."))
    match = re.search(
        r"(?:\+|plus)?\s*(?:eur\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,3},\d{2}|\d{1,2})\s*€?\s*(?:versand|shipping)",
        low,
        re.I,
    )
    if match:
        raw = match.group(1)
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return 0.0
    match2 = re.search(r"versand(?:kosten)?\s*(?:ab|für|fuer)?\s*[:=]?\s*(?:eur\s*)?(\d{1,3},\d{2}|\d{1,2})(?!\s*(?:tb|gb|tage|werktage|-|x))", low)
    if match2:
        raw = match2.group(1).replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return 0.0


def capacity_tb(text: str) -> float | None:
    """Parse drive capacity in TB; ignore cache MB and RAM-like GB noise."""
    if not text:
        return None
    low = text.lower().replace(",", ".")
    # Explicit TB
    m = re.search(r"\b([3-8])(?:\.0)?\s*tb\b", low)
    if m:
        return float(m.group(1))
    # GB / GiB near HDD context (3000-8000)
    for m in re.finditer(r"\b([3-8]000)\s*(?:gb|gib)\b", low):
        gb = int(m.group(1))
        return gb / 1000.0
    # Model-ish: 4000GB without space
    m = re.search(r"\b([3-8])000gb\b", low)
    if m:
        return float(m.group(1))
    return None


def title_has_small_gb(title: str) -> float | None:
    """If title states capacity as GB < 3000 (e.g. 300GB, 600GB, 250GB), return that GB value."""
    if not title:
        return None
    low = title.lower().replace(",", ".")
    for m in re.finditer(r"\b(\d{1,4}(?:\.\d+)?)\s*(?:gb|gib)\b(?!\s*/\s*s)(?!it)", low):
        gb = float(m.group(1))
        if gb < 3000:
            return gb
    return None


def capacity_tb_prefer_title(text: str) -> float | None:
    """Prefer TITLE capacity. If title has GB < 3000, do NOT take TB from body/related noise."""
    if not text:
        return None
    title = text.split("\n", 1)[0]
    title_cap = capacity_tb(title)
    small_gb = title_has_small_gb(title)
    if small_gb is not None:
        # Title explicitly names a small drive — trust it (will fail MIN_TB) over body TB noise
        if title_cap is not None:
            return title_cap
        return small_gb / 1000.0
    if title_cap is not None:
        return title_cap
    mn = model_number_tb(title)
    if mn is not None:
        return mn
    # No capacity in title — fall back to core/body
    return capacity_tb(text)


def model_number_tb(text: str) -> float | None:
    """Kapazitaet aus bekannten Teilenummern (WD/Seagate/HGST/Toshiba) ableiten."""
    up = (text or "").upper()
    m = re.search(r"\bWD(\d{2,3})(?:EF|EZ|PU|FZ|FR|FF|FY|FB|VC|KR)[A-Z]{1,3}\b", up)
    if m:
        v = m.group(1)
        return float(v[0]) if len(v) == 2 else float(v[:2])
    m = re.search(r"\bWD(\d)00\d[EF][A-Z]{3}\b", up)
    if m:
        return float(m.group(1))
    m = re.search(r"\bST(\d{4,5})(?:NM|NE|VN|VX|DM|NT)[0-9A-Z]{3,5}\b", up)
    if m:
        return int(m.group(1)) / 1000.0
    m = re.search(r"\bHU[SH]72\dT(\d)TAL", up)
    if m:
        return float(m.group(1))
    m = re.search(r"\bHU[SH]72\d(0[1-9])0A", up)
    if m:
        return float(int(m.group(1)))
    m = re.search(r"\bMG0\d[A-Z]{3}(\d)00", up)
    if m:
        return float(m.group(1))
    m = re.search(r"\bHDW[GNQ]([14])(\d)0\b", up)
    if m:
        return float(m.group(2))
    return None


def title_capacities(title: str) -> set[float]:
    low = (title or "").lower().replace(",", ".")
    caps = set()
    for m in re.finditer(r"\b(\d{1,2}(?:\.\d)?)\s*tb\b", low):
        try:
            caps.add(float(m.group(1)))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{3,5})\s*(?:gb|gib)\b", low):
        gb = int(m.group(1))
        if gb >= 1000:
            caps.add(gb / 1000.0)
    return caps


def capacity_mismatch(title: str, cap: float | None) -> str | None:
    """Grund, falls Titel mehrere Kapazitaeten nennt oder Teilenummer nicht zur Kapazitaet passt."""
    caps = title_capacities(title)
    if len(caps) > 1:
        return f"Titel nennt mehrere Kapazitaeten {sorted(caps)}"
    if len(caps) == 1 and cap and abs(next(iter(caps)) - cap) > 0.01:
        return f"Titel nennt {next(iter(caps)):g}TB, nicht {cap:g}TB"
    mn = model_number_tb(title)
    if mn and cap and abs(mn - cap) > 0.01:
        return f"Teilenummer ({mn:g}TB) passt nicht zu {cap:g}TB"
    return None


def fold(text: str) -> str:
    return (
        text.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("\xa0", " ")
    )


# "nie geoeffnet, eingebaut oder benutzt" / "wurde nicht benutzt" ist ein Neu-Signal, kein Gebraucht-Signal
NEGATED_USE = r"\b(?:nicht|nie|niemals)\b(?:(?!\bneu)[^.\n]){0,50}?\b(?:benutzt|verwendet|gebraucht)\w*"


# Nicht-neu-Zustaende (Ruecklaeufer, Retoure, B-Ware, Austausch-/Ersatzgeraet, RMA, Refurb) - "keine Retoure" zaehlt nicht
NOT_NEW = (r"ruck\s*laeufer|rueck\s*laeufer|\bretoure\b|kundenretoure|retour(?:en)?ware|retoure(?:n)?geraet|"
           r"\bb[\s-]?ware\b|austauschgeraet|austausch[\s-]geraet|ersatzgeraet|\brma[\s-]*(?:ware|geraet|ersatz|austausch|"
           r"replacement|unit|drive|platte|festplatte)|\b(?:aus|von|als)\s+(?:einer\s+)?rma\b|refurb\w*|\brenewed\b|"
           r"instandgesetzt|wiederaufbereitet|recertified|rezertifiziert|\bre-?certified\b")
NEGATED_NOT_NEW = (r"\b(?:keine?n?|kein|nicht|no|not|ohne)\s+(?:\w+\s+){0,2}?(?:garantie[\s-]*|kunden)?"
                   r"(?:rue?ck\s*laeufer|retoure|retourware|b[\s-]?ware|rma|refurb\w*|austauschgeraet|ersatzgeraet)\w*")


def not_new_reason(text: str, title: str = "") -> str | None:
    low = re.sub(NEGATED_NOT_NEW, " ", fold(text or ""))
    m = re.search(NOT_NEW, low)
    if m:
        return low[max(0, m.start() - 25):m.end() + 25].strip()
    # RMA allein nur im Titel (in Beschreibungen meist Ruecksende-Hinweis)
    t = re.sub(NEGATED_NOT_NEW, " ", fold(title or ""))
    m = re.search(r"\brma\b", t)
    if m:
        return t[max(0, m.start() - 25):m.end() + 25].strip()
    return None


def rejected(text: str) -> bool:
    low = re.sub(NEGATED_NOT_NEW, " ", re.sub(NEGATED_USE, " ", fold(text)))
    if not_new_reason(text, "\n".join(text.splitlines()[:2])):
        return True
    if any(word in low for word in REJECT):
        return True
    if any(re.search(pat, low) for pat in REJECT_WORDS):
        return True
    if any(re.search(pat, low) for pat in REJECT_PARTS):
        return True
    # Suchanzeigen nur am Titel erkennen (Seitenkopf enthaelt "Suche im Umkreis ...")
    head = "\n".join(low.splitlines()[:2])
    if re.search(r"\b(gesucht|suche)\b", head) and "verkaufe" not in low:
        return True
    for pat in SMR_OR_BAD:
        if re.search(pat, low):
            return True
    return False


def is_new_condition(text: str) -> bool:
    low = fold(text)
    # Explicit used signals already rejected; require positive NEW signal
    new_ok = bool(
        re.search(
            r"\b(neu|neuware|unbenutzt|ungeoeffnet|ungeöffnet|versiegelt|sealed|ovp|originalverpackt|"
            r"new\s*\(other\)|neu\s*\(sonstige\)|brand\s*new|brandneu|factory\s*sealed)\b",
            low,
        )
    )
    if not new_ok:
        return False
    # Bulk "Neu (Sonstige)" ok only if unused/sealed language OR no open/tested language
    if re.search(r"neu\s*\(sonstige\)|new\s*\(other\)", low):
        if re.search(r"geoeffnet|geöffnet|opened|getestet|tested|ausgepackt|pull", low):
            return False
    return True


def condition_of(text: str) -> str:
    low = fold(text)
    if re.search(r"neu\s*\(sonstige\)|new\s*\(other\)", low):
        return "Neu (Sonstige)"
    if re.search(r"\b(neu|neuware|unbenutzt|versiegelt|sealed|ovp)\b", low):
        return "Neu"
    return "Unklar"


def detect_interface(text: str) -> str:
    low = fold(text)
    has_sas = bool(re.search(r"\bsas\b|12g\s*sas|6g\s*sas", low))
    has_sata = bool(re.search(r"\bsata\b|sata\s*iii|sata\s*3|sata\s*6", low))
    has_adapter = bool(re.search(r"adapter|sata.?adapter|sas.?zu.?sata|interposer", low))
    if has_sas and has_sata:
        return "SAS+SATA"
    if has_sas and has_adapter:
        return "SAS (Adapter dabei)"
    if has_sas:
        return "SAS"
    if has_sata:
        return "SATA"
    return "unbekannt"


def detect_form(text: str) -> str:
    title = fold(text.split("\n", 1)[0])
    low = fold(text)
    title_35 = bool(re.search(r"3[\.,]5\s*(\"|''|zoll|inch)?", title))
    title_25 = bool(re.search(r"2[\.,]5\s*(\"|''|zoll|inch)?", title))
    body_35 = bool(re.search(r"3[\.,]5\s*(\"|''|zoll|inch)?", low))
    # Title wins: explicit 2.5" in title is decisive (laptop/SFF pulls)
    if title_25 and not title_35:
        return '2.5"'
    if title_35 and not title_25:
        return '3.5"'
    if title_35 and title_25:
        return '3.5"'
    # No form in title: body 3.5 helps; do NOT infer 2.5 from related-item body noise
    if body_35:
        return '3.5"'
    return '3.5" (angenommen)'


def preferred_label(text: str) -> str | None:
    low = fold(text)
    for pat, label in PREFERRED:
        if re.search(pat, low):
            return label
    return None


def brand_of(text: str) -> str:
    low = fold(text or "")
    if re.search(r"seagate|\bexos\b|ironwolf|\bst\d{4,5}(?:nm|vn|ne|dm|vx)", low):
        return "seagate"
    if re.search(r"toshiba|\bmg0\d|\bn300\b|\bhdw[gnq]", low):
        return "toshiba"
    if re.search(r"western\s*digital|\bwd\b|\bwd\d|\bwd\s*(?:red|gold)|hgst|hitachi|ultrastar|\bhu[sh]72", low):
        return "wd"  # HGST gehoert zu WD
    return ""


def model_of(text: str, capacity: float) -> str:
    title = re.sub(r"\s+", " ", text.split("\n")[0]).strip()
    pref = preferred_label(title)
    if not pref:
        pref = preferred_label(text)
        # Label aus dem Seitentext nur, wenn es nicht einer anderen Marke als im Titel gehoert
        if pref and brand_of(title) and brand_of(pref) != brand_of(title):
            pref = None
    short = title[:80] if title else "HDD"
    if pref:
        return f"{pref} {capacity:g}TB"
    return f"{short}"


def dauerbetrieb_judgment(text: str, title: str = "") -> tuple[bool, str]:
    pref = preferred_label(title) if title else None
    if not pref:
        pref = preferred_label(text)
        # Label aus dem Seitentext nicht verwenden, wenn es zu einer anderen Marke als der Titel gehoert
        if pref and title and brand_of(title) and brand_of(pref) != brand_of(title):
            pref = None
    low = fold(text)
    if pref:
        rpm = ""
        if re.search(r"7200\s*(rpm|u/?min)", low):
            rpm = ", 7200rpm"
        return True, f"Ja ({pref}{rpm})"
    if re.search(r"dauerbetrieb|24\s*/\s*7|nas\s*geeignet|enterprise|cmr\b", low):
        return True, "Ja (explizit NAS/24/7/CMR)"
    if re.search(r"desktop\s*hdd|pc\s*festplatte|consumer", low):
        return False, "Nein (Consumer/Desktop)"
    return False, "Nein (kein Enterprise/NAS-Modell erkennbar)"


def eur_per_tb(price: float, shipping: float, capacity: float, extra: float = 0.0) -> float:
    return (price + shipping + extra) / capacity


# --- SAS-Erkennung ueber Teilenummern (Titel nennt oft "SAS/SATA", Teilenummer ist eindeutig) ---
SAS_PARTNUMBER = re.compile(
    r"\bHU[SH]72[0-9A-Z]{4,6}AL(?:S\d|[45]\d)"  # HGST/WD: ALS640, AL5204, AL4204 = SAS (ALE/ALA/ALN = SATA)
    r"|\bHU[SH]72[0-9A-Z]{4,6}SS\d"  # HGST ...SS600 = SAS
    r"|\bST\d{4,9}SS\b"  # Seagate ...SS = SAS
    r"|\bST\d{3,5}(?:NM00(?:14|25|34|45|65|75|95)|NM0125|NM0135|NM001[AB]|NM003A|NM004A|NM005A|NM029A|NM018B|NM020B|NM022B)\b"
    r"|\bMG\d\dSCA|\bAL1\d[SL]E"  # Toshiba MGxxSCA / AL14SE = SAS
    r"|\bWD\d{3,5}F[A-Z]{2}G\b",  # WD RE SAS (z.B. WD4001FYYG)
    re.I,
)
SATA_PARTNUMBER = re.compile(
    r"\bHU[SH]72[0-9A-Z]{4,6}AL[EAN]\d"  # HGST/WD Ultrastar SATA
    r"|\bST\d{3,5}(?:NM000[A-C]|NM002[A-C]|NM0035|NM0055|NM0024|NM0033|NM0105|NM0115|NM013A|NM017B|NM019B|NM021A|NM024B|NM001G"
    r"|NE\d{3,4}|VN\d{3,4}|VX\d{3,4}|DM\d{3,4}|NT\d{3,4})\b"  # Seagate SATA
    r"|\bMG\d\dA[CD]A|\bHDW[GNQ]\d|\bDT01ACA|\bHDWD\d"  # Toshiba SATA
    r"|\bWD\d{2,5}(?:EF|EZ|FF|FR|FY|PU|FB|KR|VC)[A-Z]{1,3}Z?\b",  # WD SATA (Red/Gold/Purple/RE-SATA ...Z)
    re.I,
)
# Diese Modellreihen gibt es nur mit SATA
SATA_ONLY_FAMILIES = r"\bred\s*plus\b|\bred\s*pro\b|\bwd\s*gold\b|ironwolf|\bn300\b|\bwd\s*red\b|\bpurple\b|barracuda|skyhawk"


def interface_of(core: str) -> str:
    iface = detect_interface(core)
    title = core.split("\n", 1)[0] if core else ""
    if SAS_PARTNUMBER.search(title):
        return "SAS (Teilenummer)"
    if iface in ("unbekannt", "SAS+SATA") and SATA_PARTNUMBER.search(title):
        return "SATA (Teilenummer)"
    if iface == "unbekannt":
        head = core[:2500]
        if SAS_PARTNUMBER.search(head):
            return "SAS (Teilenummer)"
        if SATA_PARTNUMBER.search(head):
            return "SATA (Teilenummer)"
        if re.search(SATA_ONLY_FAMILIES, fold(title)):
            return "SATA (Modellreihe)"
    return iface


def is_sas_only(iface: str) -> bool:
    return iface.startswith("SAS") and iface != "SAS+SATA"


# --- Nicht-EU-Verkaeufer: Einfuhrumsatzsteuer schaetzen ---
EU_COUNTRIES = (
    "deutschland", "oesterreich", "frankreich", "italien", "spanien", "niederlande", "belgien", "luxemburg",
    "polen", "tschechien", "tschechische republik", "slowakei", "ungarn", "slowenien", "kroatien", "rumaenien",
    "bulgarien", "griechenland", "zypern", "malta", "portugal", "irland", "daenemark", "schweden", "finnland",
    "estland", "lettland", "litauen", "germany", "austria", "france", "italy", "spain", "netherlands",
)
NON_EU_COUNTRIES = (
    "vereinigtes koenigreich", "grossbritannien", "united kingdom", "england", "schottland", "wales", "nordirland",
    "vereinigte staaten", "usa", "united states", "china", "hongkong", "hong kong", "kanada", "canada", "japan",
    "taiwan", "suedkorea", "korea", "singapur", "australien", "neuseeland", "schweiz", "norwegen", "island",
    "liechtenstein", "tuerkei", "ukraine", "serbien", "israel", "indien", "malaysia", "thailand", "vietnam",
    "russland", "belarus", "bosnien", "albanien", "nordmazedonien", "montenegro", "moldau", "philippinen",
    "indonesien", "mexiko", "brasilien", "vereinigte arabische emirate",
)
IMPORT_VAT = 0.19
FX_FALLBACK = {"eur": 1.0, "€": 1.0, "£": 1.16, "gbp": 1.16, "$": 0.86, "us $": 0.86, "usd": 0.86,
               "c $": 0.62, "cad": 0.62, "au $": 0.56, "aud": 0.56, "chf": 1.07}


def non_eu_country(location: str) -> str | None:
    """Liefert das Land, falls der Artikelstandort ausserhalb der EU liegt (sonst None)."""
    loc = fold(location or "").strip()
    if not loc:
        return None
    country = loc.split(",")[-1].strip()
    for c in EU_COUNTRIES:
        if c == country or country.endswith(c):
            return None
    for c in NON_EU_COUNTRIES:
        if re.search(r"\b" + re.escape(c) + r"\b", country):
            return country
    return None


def parse_amount(raw: str) -> float | None:
    s_ = (raw or "").strip().rstrip(".,")
    if not s_:
        return None
    if "," in s_ and "." in s_:
        if s_.rfind(",") > s_.rfind("."):
            s_ = s_.replace(".", "").replace(",", ".")
        else:
            s_ = s_.replace(",", "")
    elif "," in s_:
        s_ = s_.replace(",", ".") if re.search(r",\d{1,2}$", s_) else s_.replace(",", "")
    elif "." in s_ and not re.search(r"\.\d{1,2}$", s_):
        s_ = s_.replace(".", "")
    try:
        return float(s_)
    except ValueError:
        return None


def eur_price_from_text(text: str) -> float | None:
    """eBay-Preistext -> EUR (bevorzugt 'EUR x'/'ca. EUR x', sonst Fremdwaehrung grob umgerechnet)."""
    t = (text or "").replace("\xa0", " ")
    m = re.search(r"EUR\s*([\d.,]+)", t)
    if m:
        return parse_amount(m.group(1))
    m = re.search(r"(£|US\s*\$|C\s*\$|AU\s*\$|\$|CHF)\s*([\d.,]+)", t)
    if m:
        val = parse_amount(m.group(2))
        rate = FX_FALLBACK.get(re.sub(r"\s+", " ", m.group(1).lower()), 1.0)
        return round(val * rate, 2) if val else None
    return None


def import_estimate(price: float, shipping: float, shown_eur: float = 0.0) -> float:
    """19 % EUSt auf Ware+Versand (HDDs: 0 % Zoll); angezeigte Einfuhrabgaben, falls hoeher."""
    vat = IMPORT_VAT * (price + shipping)
    return round(max(vat, shown_eur or 0.0), 2)


def ebay_shown_import(body: str, info: dict | None) -> float:
    t = (body or "").replace("\xa0", " ")
    m = re.search(r"Einfuhrabgaben:?\s*(?:Ca\.?\s*)?(EUR|£|US\s*\$|C\s*\$|AU\s*\$|\$|CHF|€)\s*([\d.,]+)", t, re.I)
    if not m:
        return 0.0
    cur = re.sub(r"\s+", " ", m.group(1).lower())
    val = parse_amount(m.group(2)) or 0.0
    rate = FX_FALLBACK.get(cur, 1.0)
    # Genauer: Kurs aus "£50,00" + "Ca. EUR 58,96" der Seite ableiten
    if info and cur not in ("eur", "€"):
        p_orig = parse_amount((re.search(r"([\d.,]+)", info.get("price") or "") or [None, ""])[1]) if info.get("price") else None
        p_eur = eur_price_from_text(info.get("approx") or "")
        if p_orig and p_eur:
            rate = p_eur / p_orig
    return round(val * rate, 2)


def ebay_detail_shipping(ship_text: str) -> float | None:
    t = fold(ship_text or "")
    if not t:
        return None
    m = re.search(r"ca\.?\s*eur\s*([\d.,]+)", t)
    if m:
        return parse_amount(m.group(1))
    seg = t.split("standort")[0]
    if re.search(r"kostenlos|gratis", seg):
        return 0.0
    m = re.search(r"eur\s*([\d.,]+)", seg)
    if m:
        return parse_amount(m.group(1))
    return None


def ebay_location(body: str, info: dict | None) -> str:
    for src in ((info or {}).get("shipping") or "", (info or {}).get("location") or "", body or ""):
        m = re.search(r"Standort:\s*([^\n|]+)", src.replace("\xa0", " "))
        if m:
            return m.group(1).strip()
    return ""


# --- Verkaeuferbeschreibung (eBay-iframe): Gebraucht-/Rezertifiziert-Signale ---
DESC_USED_PATTERNS = (
    (r"(?<!un)gebraucht", "gebraucht"),
    (r"(?<!un)getestet", "getestet"),
    (r"recertified|rezertifiziert|recertifiziert", "rezertifiziert"),
    (r"refurbished|generalueberholt|wiederaufbereitet", "refurbished/generalueberholt"),
    (r"\b0\s*(?:hours?|hrs?|h|stunden)\b[^\n]{0,15}(?:tested|getestet)|(?:tested|getestet)[^\n]{0,15}\b0\s*(?:hours?|hrs?|stunden)\b|\b0\s*hrs\b", "0 hours tested"),
    (r"\bpulled\b|\b(?:server|oem|hdd|drive|system)\s*pulls?\b|\bpulls?\s+from\b|ausgebaut", "pulled/ausgebaut"),
    (r"so\s+gut\s+wie\s+nie\s+benutzt|kaum\s+benutzt|wenig\s+benutzt", "so gut wie nie benutzt"),
)
HOURS_KW = r"(?:betriebsstunden|betriebszeit|power[\s-]*on[\s-]*hours?|\bpoh\b)"
# Datenblatt-Kontext (MTBF, Stunden pro Jahr, Garantie) ist kein Gebraucht-Signal
HOURS_SPEC_CONTEXT = r"mtbf|mttf|ausfaell|ausfall|zwischen|pro\s+jahr|/\s*jahr|jahr|year|garantie|warranty|24\s*/\s*7|8760|bis\s+zu|workload|nennlast"


def _hours_used(low: str, m) -> bool:
    val = parse_amount(m.group(1)) or 0.0
    if val <= 0 or val >= 100000:
        return False
    window = low[max(0, m.start() - 60):m.end() + 40]
    return not re.search(HOURS_SPEC_CONTEXT, window)


# Rechtstexte in Verkaeuferbeschreibungen (Gewaehrleistung auf Gebrauchtware, Altoel-/Batterie-Ruecknahme)
# sind kein Hinweis auf einen gebrauchten Artikel -> vor der Pruefung entfernen
DESC_BOILERPLATE = (
    r"[^.\n]*\b(?:gewaehrleistung|garantie|haftung|verjaehr\w*)\b[^.\n]*gebraucht\w*[^.\n]*",
    r"[^.\n]*gebraucht\w*[^.\n]*\b(?:gewaehrleistung|garantie|verjaehr\w*)\b[^.\n]*",
    r"[^.\n]*\b(?:bei|fuer|auf)\s+gebrauchtwaren?\b[^.\n]*",
    r"[^.\n]*gebraucht\w*\s+(?:\w+\s+){0,2}(?:oel\w*|altoel\w*|batterie\w*|akku\w*|elektro\w*|altgeraet\w*|geraete|verpackung\w*)[^.\n]*",
    r"[^.\n]*\b(?:altoel\w*|batteriegesetz|batterieverordnung|battg|elektrog|elektroaltgeraet\w*)\b[^.\n]*",
)


def desc_used_reason(desc: str) -> str | None:
    low = fold(desc or "")[:8000]
    if not low.strip():
        return None
    for pat in DESC_BOILERPLATE:
        low = re.sub(pat, " ", low)
    for pat, label in DESC_USED_PATTERNS:
        m = re.search(pat, low)
        if m:
            return f"Beschreibung: {label} ('{low[max(0, m.start() - 30):m.end() + 30].strip()}')"
    nn = not_new_reason(low)
    if nn:
        return f"Beschreibung: nicht neu (Ruecklaeufer/Retoure/B-Ware/RMA/Refurb) ('{nn}')"
    for m in re.finditer(HOURS_KW + r"[^\d\n]{0,25}(\d[\d.,]*)", low):
        if _hours_used(low, m):
            return f"Beschreibung: Betriebsstunden {m.group(1)} ('{low[max(0, m.start() - 20):m.end() + 20].strip()}')"
    for m in re.finditer(r"(\d[\d.,]*)\s*(?:betriebsstunden|power[\s-]*on[\s-]*hours?|std\.?\s*laufzeit)", low):
        if _hours_used(low, m):
            return f"Beschreibung: {m.group(0)}"
    if re.search(r"(wenige|geringe|kaum)\s+" + HOURS_KW, low):
        return "Beschreibung: wenige Betriebsstunden"
    return None


def kleinanzeigen_shipping(text: str) -> float:
    """Kleinanzeigen: 'ab 0,99 EUR'/'Versand moeglich' ohne Betrag = unbekannt -> ~6 EUR."""
    low = fold(text or "")
    if re.search(r"kostenlose?r?\s+versand|versand\s+(?:ist\s+)?(?:kostenlos|gratis|inklusive)|inkl\.?\s*versand|versandkostenfrei", low):
        return 0.0
    m = re.search(r"(?:versand|dhl|hermes|dpd|paket)[^\n€]{0,40}?zzgl\.?\s*(\d{1,2}(?:[.,]\d{2})?)\s*(?:€|eur)", low)
    if m:
        return parse_amount(m.group(1)) or 6.0
    if re.search(r"ab\s*0[.,]99", low):
        return 6.0
    v = parse_shipping(text)
    if v >= 1.5:
        return v
    if re.search(r"nur\s+abholung", low) and not re.search(r"versand\s+moeglich", low):
        return 0.0
    return 6.0



def core_listing_text(text: str) -> str:
    """Title + main description; cut marketplace 'related items' noise."""
    if not text:
        return ""
    lines = text.splitlines()
    title = lines[0] if lines else ""
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    cut = re.search(
        r"\n\s*(aehnliche artikel|ähnliche artikel|mehr artikel von|sponsored|"
        r"wird oft zusammen|andere artikel|verwandte suchanzeigen|"
        r"people who viewed|similar sponsored|verkaeufer weitere|verkäufer weitere|"
        r"andere anzeigen des anbieters|alle anzeigen dieses anbieters|das k(?:ö|oe)nnte dich auch interessieren|"
        r"anzeige melden)\b",
        body,
        re.I,
    )
    if cut:
        body = body[: cut.start()]
    # Keep a bounded window of the main listing
    body = body[:4500]
    return (title + "\n" + body).strip()


def consider(text: str, price: float, shipping: float, url: str, source: str = "",
             import_fee: float = 0.0, import_note: str = "", max_ept: float | None = None) -> Deal | None:
    if price <= 0:
        return None
    # Amazon/Geizhals retail listings rarely say "Neu" in the title — treat as new unless used/refurb
    src = (source or "").lower()
    if src in ("amazon", "geizhals") and not is_new_condition(text):
        if not rejected(text) and not re.search(r"refurbished|generalueberholt|generalüberholt|gebraucht|\bused\b", fold(text)):
            text = text + "\nNeu OVP"
    core = core_listing_text(text)
    title = (core.split("\n", 1)[0] if core else "") or (text.split("\n", 1)[0] if text else "")
    # Parts/tray/PCB: check title + core
    if rejected(core) or rejected(title):
        return None
    if re.search(SCSI_TITLE, fold(title)):
        return None
    if not is_new_condition(core):
        return None
    # Title-first capacity; block body TB when title says e.g. 300GB / 600GB
    cap = capacity_tb_prefer_title(core)
    if cap is None:
        cap = capacity_tb_prefer_title(text)
    if cap is None:
        cap = model_number_tb(title)
    if cap is None or cap < MIN_TB or cap > MAX_TB:
        return None
    if capacity_mismatch(title, cap):
        return None
    ept = eur_per_tb(price, shipping, cap, import_fee)
    if ept > (max_ept or MAX_EUR_PER_TB):
        return None
    # Junk floor: <15 EUR/TB is almost never a real new 3.5" Dauerbetrieb drive
    if ept < MIN_EUR_PER_TB:
        return None
    ok, reason = dauerbetrieb_judgment(core, title)
    if not ok:
        return None
    iface = interface_of(core)
    form = detect_form(core)
    # User wants 3.5" Dauerbetrieb only — reject laptop/2.5" server pulls
    if form.startswith("2.5"):
        return None
    # Soft-reject pure SAS without adapter note — still include but flag
    fazit_bits = [reason, iface, form]
    if is_sas_only(iface):
        fazit_bits.append("Achtung: SAS-only — HBA noetig (nicht SATA)")
    if shipping > 0:
        fazit_bits.append("zzgl. " + f"{shipping:.2f}".replace(".", ",") + " EUR Versand")
    if import_fee > 0:
        fazit_bits.append("zzgl. ca. " + f"{import_fee:.2f}".replace(".", ",") + " EUR Einfuhr (19% EUSt" + (", " + import_note if import_note else "") + ")")
    elif src == "kleinanzeigen" and re.search(r"nur\s+abholung", text, re.I):
        fazit_bits.append("nur Abholung")
    score = (CANDIDATE_EUR_PER_TB - ept) * 10.0
    if "enterprise" in reason.lower() or "ultrastar" in reason.lower() or "exos" in reason.lower() or "gold" in reason.lower():
        score += 8
    if iface.startswith("SATA"):
        score += 3
    if form.startswith("3.5"):
        score += 1
    if condition_of(text) == "Neu":
        score += 2
    src_label = {
        "ebay": "eBay.de",
        "kleinanzeigen": "Kleinanzeigen",
        "amazon": "Amazon.de",
        "geizhals": "Geizhals",
        "vinted": "Vinted.de",
    }.get(src, source or "?")
    fazit_bits.insert(0, src_label)
    return Deal(
        model=model_of(text, cap),
        capacity_tb=cap,
        price=price,
        shipping=shipping,
        eur_per_tb=ept,
        condition=condition_of(text),
        url=norm_url(url),
        fazit="; ".join(fazit_bits),
        score=score,
        interface=iface,
        form=form,
        dauerbetrieb=reason,
        source=src_label,
        import_fee=import_fee,
        sas_only=is_sas_only(iface),
        title=re.sub(r"\s+", " ", title).strip()[:140],
    )


def format_deals(deals: list[Deal]) -> str:
    if not deals:
        return NO_DEAL
    blocks = []
    for deal in deals:
        price = f"{deal.price:.2f}".replace(".", ",")
        price = re.sub(r",00$", "", price)
        ept = f"{deal.eur_per_tb:.2f}".replace(".", ",")
        total = deal.price + deal.shipping + deal.import_fee
        total_s = f"{total:.2f}".replace(".", ",") + " EUR"
        if deal.shipping > 0:
            total_s += " inkl. " + f"{deal.shipping:.2f}".replace(".", ",") + " EUR Versand"
        if deal.import_fee > 0:
            total_s += (" und" if deal.shipping > 0 else " inkl.") + " ca. " + f"{deal.import_fee:.2f}".replace(".", ",") + " EUR Einfuhr"
        blocks.append(
            "\n".join(
                [
                    ("- [AUF DEM WEG" + (", NUR BEI ABHOLUNG UNTER LIMIT" if deal.pickup_only else "") + "] " if deal.on_route else "- ") + deal.model
                    + (f" | {deal.title}" if deal.title and deal.title not in deal.model else ""),
                    f"- {price} EUR (gesamt {total_s}) = {ept} EUR/TB, {deal.condition}, {deal.interface}",
                    f"- Quelle: {deal.source or '?'} | Standort: {deal.location or 'unbekannt'} | {route_label(deal)}"
                    + (f" | bekannt seit {deal.first_seen}" if deal.first_seen else ""),
                ]
                + ([f"- ABHOLUNG: {deal.pickup_price:.2f} EUR ohne Versand = {deal.pickup_eur_per_tb:.2f} EUR/TB".replace(".", ",")]
                   if deal.pickup_price else [])
                + [
                    f"- {deal.url}",
                    f"- {deal.fazit}" + (f" [{deal.geprueft}]" if deal.geprueft else ""),
                ]
            )
        )
    return "\n\n".join(blocks)


def search_url(source: str, query: str, cap: int, page: int = 1) -> str:
    if source == "geizhals_cat":
        # Kategorie HDD: 3.5", SATA 6Gb/s + 3Gb/s, ab 3TB; sortiert nach EUR/TB inkl. Versand, DE-Haendler
        url = GEIZHALS_CAT_URL
        if page > 1:
            url += f"&pg={page}"
        return url
    if source == "ebay_cap":
        # Breite Suche in Kategorie 56083 (Interne Festplatten), Neu/Neu-Sonstige, Sofortkauf, sortiert nach
        # Preis+Versand aufsteigend, 240 pro Seite, Preisband passend zur Kapazitaet (Kandidatenfenster ueber dem Limit)
        tb = float(re.sub(r"[^0-9.]", "", query) or 8)
        params = {
            "_nkw": query,
            "_sacat": "56083",
            "_udlo": str(int(tb * 12)),
            "_udhi": str(int(tb * CANDIDATE_EUR_PER_TB) + 10),
            "LH_BIN": "1",
            "LH_ItemCondition": "1000|1500",
            "_sop": "15",
            "_ipg": "240",
            "rt": "nc",
        }
        if page > 1:
            params["_pgn"] = str(page)
        return "https://www.ebay.de/sch/i.html?" + urllib.parse.urlencode(params)
    if source == "ebay":
        params = {
            "_nkw": query,
            "_udlo": "45",
            "_udhi": str(max(cap, WATCH_CAP)),
            "LH_BIN": "1",
            "_sop": "15",
            "rt": "nc",
            "LH_PrefLoc": "1",
            # Neu + Neu (Sonstige)
            "LH_ItemCondition": "1000|1500",
            "_ipg": "240",
        }
        params.pop("LH_PrefLoc", None)
        if page > 1:
            params["_pgn"] = str(page)
        return "https://www.ebay.de/sch/i.html?" + urllib.parse.urlencode(params)
    if source == "amazon":
        params = {
            "k": query,
            "s": "price-asc-rank",
        }
        if page > 1:
            params["page"] = str(page)
        return "https://www.amazon.de/s?" + urllib.parse.urlencode(params)
    if source == "geizhals":
        params = {
            "fs": query,
            "in": "",
            "sort": "r",  # Preis pro Einheit (EUR/TB)
            "t": "v",  # inkl. Versand
            "hloc": "de",
        }
        if page > 1:
            params["pg"] = str(page)
        return "https://geizhals.de/?" + urllib.parse.urlencode(params)
    if source == "vinted":
        params = {
            "search_text": query,
            "order": "price_low_to_high",
            "price_from": "45",  # < 45 EUR ist nie eine neue 3-8TB Dauerbetrieb-HDD (filtert Muell)
            "price_to": str(max(cap, WATCH_CAP)),
            "currency": "EUR",
        }
        if page > 1:
            params["page"] = str(page)
        return "https://www.vinted.de/catalog?" + urllib.parse.urlencode(params)
    slug = fold(query)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    seite = f"seite:{page}/" if page > 1 else ""
    return f"https://www.kleinanzeigen.de/s-anzeige:angebote/preis:1:{max(cap, WATCH_CAP)}/{seite}{slug}/k0"


def accept_consent(page) -> None:
    for label in ("Alle akzeptieren", "Einverstanden", "Akzeptieren", "Zustimmen"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if button.count() and button.first.is_visible(timeout=1200):
                button.first.click(timeout=2000)
                return
        except Exception:
            continue


def _json_string(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def listing_price(block: str) -> str:
    found = []
    for match in re.finditer(r"(\d{1,4}(?:\.\d{3})*(?:,\d{2})?)\s*\u20ac", block):
        window = block[max(0, match.start() - 48) : match.start()].lower()
        if "aufpreis" in window or "extra" in window:
            continue
        raw = match.group(1).replace(".", "").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        found.append((value, match.group(0)))
    if not found:
        return ""
    serious = [item for item in found if item[0] >= 20]
    return (serious or found)[0][1]


def cards_from_html(html: str, source: str) -> list[dict]:
    cards: list[dict] = []
    if source == "kleinanzeigen":
        blocks = re.findall(r'<article\b[^>]*data-adid="\d+"[\s\S]*?</article>', html)
        for block in blocks:
            href_m = re.search(r'data-href="([^"]+)"', block)
            title_m = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', block)
            desc_m = re.search(r'"description"\s*:\s*"((?:\\.|[^"\\])*)"', block)
            price_text = listing_price(block)
            if not href_m or not price_text:
                continue
            href = href_m.group(1).replace("&amp;", "&")
            if href.startswith("/"):
                href = "https://www.kleinanzeigen.de" + href
            flat_k = re.sub(r"<[^>]+>", " | ", re.sub(r"<script[\s\S]*?</script>", " ", block))
            loc_k = re.search(r"\|\s*(\d{5}\s+[^|]{2,60}?)\s*\|", flat_k)
            cards.append(
                {
                    "location": re.sub(r"\s+", " ", loc_k.group(1)).strip() if loc_k else "",
                    "title": _json_string(title_m.group(1)) if title_m else "",
                    "snippet": _json_string(desc_m.group(1)) if desc_m else "",
                    "price": price_text,
                    "shipping_text": block[:3000],
                    "url": href,
                }
            )
        return cards

    if source == "amazon":
        # Search result cards by ASIN
        parts_a = re.split(r'data-asin="([A-Z0-9]{10})"', html)
        for index in range(1, len(parts_a), 2):
            asin = parts_a[index]
            chunk = parts_a[index + 1][:12000]
            if "a-price" not in chunk and "€" not in chunk and "&euro;" not in chunk.lower():
                continue
            title_m = re.search(r"<h2[^>]*>[\s\S]*?<span[^>]*>([^<]{8,200})</span>", chunk)
            if not title_m:
                title_m = re.search(r'alt="([^"]{8,200})"', chunk)
            title = title_m.group(1).strip() if title_m else ""
            if not title:
                continue
            price = ""
            off = re.search(r'class="a-offscreen"[^>]*>([^<]*€[^<]*)</span>', chunk)
            if not off:
                off = re.search(r'class="a-offscreen"[^>]*>([^<]*&euro;[^<]*)</span>', chunk, re.I)
            if off:
                price = off.group(1).replace("&euro;", "€")
            else:
                whole = re.search(r'a-price-whole[^>]*>([^<]+)', chunk)
                frac = re.search(r'a-price-fraction[^>]*>([^<]+)', chunk)
                if whole:
                    w = re.sub(r"[^\d]", "", whole.group(1))
                    f = re.sub(r"[^\d]", "", frac.group(1)) if frac else "00"
                    if w:
                        price = f"{w},{f} €"
            if not price:
                continue
            # Skip obviously unrelated (cases, docks) via reject later; keep card
            cards.append(
                {
                    "title": title,
                    "snippet": title + " Neu",
                    "price": price,
                    "shipping_text": chunk[:3000],
                    "url": f"https://www.amazon.de/dp/{asin}",
                }
            )
        return cards

    if source in ("geizhals", "geizhals_cat"):
        # Neues Svelte-Layout (2026): galleryview__item / listview__item mit "€ 196,75" und "(€ 49,188/TB)"
        for m in re.finditer(
            r'<(?:div|article|li)[^>]*class="(?:galleryview|listview)__item[^"]*"[^>]*>([\s\S]*?)(?=<(?:div|article|li)[^>]*class="(?:galleryview|listview)__item[ "]|<nav|$)',
            html,
        ):
            chunk = m.group(1)[:12000]
            name_m = re.search(r'__name-link"[^>]*title="([^"]+)"', chunk) or re.search(r'title="([^"]{8,220})"', chunk)
            href_m = re.search(r'href="([^"#]*-a\d+\.html)[^"]*"', chunk)
            price_m = re.search(r'class="price"[^>]*>(?:<!--[^>]*-->)*\s*(?:€|&euro;)\s*([\d.]+,\d{2})', chunk)
            if not (name_m and href_m and price_m):
                continue
            import html as _html
            title = re.sub(r"\s+", " ", _html.unescape(name_m.group(1))).strip()
            href = href_m.group(1).replace("&amp;", "&")
            if not href.startswith("http"):
                href = "https://geizhals.de/" + href.lstrip("./")
            ppu_m = re.search(r'__ppu"[^>]*>(?:<!--[^>]*-->)*\s*\((?:€|&euro;)\s*([\d.]+,\d+)\s*/\s*TB\)', chunk)
            offers_m = re.search(r'(\d+)\s*Angebot', chunk)
            avail_m = re.search(r'class="availability"[^>]*aria-label="([^"]+)"', chunk)
            cards.append(
                {
                    "title": title,
                    "snippet": title + " Neu",
                    "price": price_m.group(1) + " €",
                    "shipping_text": "Preis inkl. Versand (Geizhals t=v)",
                    "url": href.split("?")[0],
                    "ppu": ppu_m.group(1) if ppu_m else "",
                    "offers": offers_m.group(1) if offers_m else "",
                    "availability": avail_m.group(1) if avail_m else "",
                }
            )
        if cards:
            return cards
        # Altes Layout: Product rows: links + prices
        for m in re.finditer(
            r'<div[^>]*class="[^"]*(?:listitem|productlist__item|row)[^"]*"[\s\S]*?</div>\s*(?=<div[^>]*class="[^"]*(?:listitem|productlist__item|row)|$)',
            html,
            re.I,
        ):
            chunk = m.group(0)[:8000]
            href_m = re.search(r'href="(https?://geizhals\.de/[^"]+-a\d+\.html|/[^"]+-a\d+\.html)"', chunk)
            if not href_m:
                href_m = re.search(r'href="([^"]+-a\d+\.html)"', chunk)
            if not href_m:
                continue
            href = href_m.group(1).replace("&amp;", "&")
            if href.startswith("/"):
                href = "https://geizhals.de" + href
            title_m = re.search(r'(?:class="[^"]*productlist__name[^"]*"[^>]*>|itemprop="name"[^>]*>)([^<]{8,200})', chunk)
            if not title_m:
                title_m = re.search(r'alt="([^"]{8,200})"', chunk)
            if not title_m:
                title_m = re.search(r'>([A-Za-z0-9][^<]{10,120})</a>', chunk)
            title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
            if not title:
                continue
            price_text = listing_price(chunk)
            if not price_text:
                pm = re.search(r'([\d.]+,\d{2})\s*&?euro;|([\d.]+,\d{2})\s*€', chunk, re.I)
                if pm:
                    price_text = (pm.group(1) or pm.group(2)) + " €"
            if not price_text:
                continue
            cards.append(
                {
                    "title": title,
                    "snippet": title + " Neu",
                    "price": price_text,
                    "shipping_text": chunk[:3000],
                    "url": href.split("?")[0],
                }
            )
        if cards:
            return cards
        # Fallback: any product deep-link with nearby price
        for m in re.finditer(r'href="((?:https://geizhals\.de)?/[^"]+-a(\d+)\.html)"[^>]*>([^<]{8,180})', html):
            href = m.group(1)
            if href.startswith("/"):
                href = "https://geizhals.de" + href
            title = re.sub(r"\s+", " ", m.group(3)).strip()
            # look ahead for price
            window = html[m.end() : m.end() + 1500]
            price_text = listing_price(window)
            if not price_text:
                pm = re.search(r'([\d.]+,\d{2})\s*(?:&euro;|€)', window)
                if pm:
                    price_text = pm.group(1) + " €"
            if not price_text:
                continue
            url = href.split("?")[0]
            if any(c["url"] == url for c in cards):
                continue
            cards.append(
                {
                    "title": title,
                    "snippet": title + " Neu",
                    "price": price_text,
                    "shipping_text": window,
                    "url": url,
                }
            )
        return cards

    if source == "vinted":
        # Item links /items/<id>-...
        for m in re.finditer(
            r'href="((?:https://www\.vinted\.de)?/items/(\d+)[^"]*)"[\s\S]{0,2000}?(?:€|EUR|price)',
            html,
            re.I,
        ):
            href = m.group(1).replace("&amp;", "&")
            if href.startswith("/"):
                href = "https://www.vinted.de" + href
            chunk = html[max(0, m.start() - 200) : m.end() + 2500]
            title_m = re.search(r'(?:alt|title)="([^"]{5,180})"', chunk)
            if not title_m:
                title_m = re.search(r'newItem_title[^>]*>\s*([^<]{5,180})', chunk)
            title = title_m.group(1).strip() if title_m else ""
            if not title:
                # slug from URL
                slug = href.rstrip("/").split("/")[-1]
                title = slug.replace("-", " ")
            price = ""
            pm = re.search(r'([\d]+(?:[.,]\d{2})?)\s*€', chunk)
            if not pm:
                pm = re.search(r'"price"\s*:\s*"?(?:EUR\s*)?([\d.]+)"?', chunk)
            tot = re.search(r'([\d]+[.,]\d{2})\s*€\s*,\s*([\d]+[.,]\d{2})\s*€', title)
            if tot:
                price = tot.group(2).replace(".", ",") + " €"
            elif pm:
                raw = pm.group(1)
                if "." in raw and "," not in raw and re.search(r"\.\d{2}$", raw):
                    # 79.90 style
                    price = raw.replace(".", ",") + " €"
                else:
                    price = raw + " €"
            if not price:
                continue
            url = norm_url(href.split("?")[0])
            if any(c["url"] == url for c in cards):
                continue
            cards.append(
                {
                    "title": title,
                    "snippet": title,
                    "price": price,
                    "shipping_text": chunk[:2000],
                    "url": url,
                }
            )
        # JSON blob fallback often embedded in Vinted
        if not cards:
            for m in re.finditer(
                r'"id"\s*:\s*(\d{6,})[\s\S]{0,400}?"title"\s*:\s*"((?:\\.|[^"\\])*)"[\s\S]{0,400}?"price"\s*:\s*"?(?:EUR\s*)?([\d.]+)"?',
                html,
            ):
                item_id, title_raw, price_raw = m.group(1), m.group(2), m.group(3)
                title = _json_string(title_raw)
                price = price_raw.replace(".", ",") + " €" if "." in price_raw else price_raw + " €"
                url = f"https://www.vinted.de/items/{item_id}"
                if any(c["url"] == url for c in cards):
                    continue
                cards.append(
                    {
                        "title": title,
                        "snippet": title,
                        "price": price,
                        "shipping_text": "",
                        "url": url,
                    }
                )
        return cards

    parts = re.split(r'data-listingid="(\d+)"', html)
    for index in range(1, len(parts), 2):
        listing_id = parts[index]
        if len(listing_id) < 10:
            continue
        chunk = parts[index + 1][:15000]
        title_m = re.search(r'class="s-card__title[^"]*"[^>]*>(.*?)</', chunk, re.S)
        title = re.sub(r"<[^>]+>", " ", title_m.group(1)) if title_m else ""
        title = re.sub(r"\s+", " ", title).strip()
        if not title or "shop on ebay" in title.lower():
            continue
        price_m = re.search(r's-card__price[^>]*>(.*?)</span>', chunk, re.S)
        price = re.sub(r"<[^>]+>", " ", price_m.group(1)) if price_m else ""
        price = re.sub(r"\s+", " ", price).strip()
        if not price:
            fallback = re.search(r"EUR\s*[\d\.,]+", chunk)
            price = fallback.group(0) if fallback else ""
        href = f"https://www.ebay.de/itm/{listing_id}"
        flat = re.sub(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", " ", chunk)
        flat = re.sub(r"<[^>]+>", " | ", flat)
        flat = re.sub(r"(\s*\|\s*)+", " | ", flat)[:1500]
        cond_m = re.search(r"\|\s*(Brandneu|Neu \(Sonstige\)|Neu – Sonstige|Neu|Gebraucht|Generalüberholt|Gepr[üu]ft[^|]*|Als Ersatzteil[^|]*|Wie neu|Sehr gut|Gut|Akzeptabel)\s*\|", flat)
        cond = cond_m.group(1) if cond_m else ""
        ship_m = re.search(r"\|\s*([^|]*(?:Lieferung|Versand)[^|]*)\|", flat)
        ship_txt = ship_m.group(1).strip() if ship_m else ""
        loc_m = re.search(r"\|\s*aus ([^|]{3,60}?)\s*\|", flat)
        if " bis " in price:
            # Variantenangebot: Preisspanne -> unsicher, Detailseite muss pruefen
            cond = (cond + " Variante").strip()
        cards.append(
            {
                "title": title,
                "snippet": title + ("\nZustand: " + cond if cond else ""),
                "price": price,
                "shipping_text": ship_txt,
                "url": href,
                "price_range": " bis " in price,
                "location": loc_m.group(1).strip() if loc_m else "",
            }
        )
    return cards


def listing_cards(page, source: str) -> list[dict]:
    return cards_from_html(page.content(), source)


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8000)
    except Exception:
        return ""


STATS: dict[str, dict] = {}
STATS_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
STAT_KEYS = ("loads", "cards", "blocked", "block_hits", "errors", "relaunches", "ctx_resets", "detail_loads", "detail_blocked")


def stat_key(source: str) -> str:
    return {"geizhals_cat": "geizhals", "ebay_cap": "ebay"}.get(source, source)


def _stat(source: str, key: str, inc: int = 1) -> None:
    source = stat_key(source)
    with STATS_LOCK:
        STATS.setdefault(source, {k: 0 for k in STAT_KEYS})
        STATS[source][key] = STATS[source].get(key, 0) + inc


def say(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


BLOCK_MARKERS = (
    "tut uns leid", "sorry", "api-services-support@", "captcha", "robot check", "access denied",
    "403 forbidden", "something went wrong", "ich bin kein roboter", "zugriff verweigert",
)
HOMES = {
    "ebay": "https://www.ebay.de/",
    "kleinanzeigen": "https://www.kleinanzeigen.de/",
    "amazon": "https://www.amazon.de/",
    "geizhals": "https://geizhals.de/",
    "geizhals_cat": "https://geizhals.de/",
    "vinted": "https://www.vinted.de/",
    "ebay_cap": "https://www.ebay.de/",
}

# Fehler, nach denen Seite/Kontext/Browser unbrauchbar ist -> Worker-Browser neu starten
FATAL_MARKERS = (
    "frame was detached", "target page, context or browser has been closed", "target closed",
    "browser has been closed", "context has been closed", "page has been closed", "has been closed",
    "connection closed", "browser closed", "crashed", "page crashed", "pipe", "websocket",
    "playwright connection", "disconnected",
)


def is_fatal(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in FATAL_MARKERS)


def short_err(exc: BaseException) -> str:
    return re.sub(r"\s+", " ", str(exc).split("Call log:")[0]).strip()[:160]


def looks_blocked(page) -> bool:
    try:
        snip = page.locator("body").inner_text(timeout=3000)[:600].lower()
    except Exception:
        return False
    return any(x in snip for x in BLOCK_MARKERS)


def goto_retry(page, url: str, tries: int = 3) -> None:
    """Detailseite laden; bei eBay-Fehlerseite bis zu 'tries' mal neu laden."""
    for attempt in range(tries):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(700 + 800 * attempt)
        accept_consent(page)
        if not looks_blocked(page):
            return
        page.wait_for_timeout(1500 * (attempt + 1))


def collect(page, source: str, query: str, cap: int, page_no: int = 1, worker=None) -> list[dict]:
    url = search_url(source, query, cap, page_no)
    _stat(source, "loads")

    def P():
        return worker.page if worker is not None else page

    def nav(target: str, timeout: int = 45000, paced: bool = True) -> None:
        if worker is not None:
            if paced:
                worker.pace()
            else:
                worker.loads_on_page += 1
        P().goto(target, wait_until="domcontentloaded", timeout=timeout)

    try:
        nav(url)
    except Exception as exc:
        if is_fatal(exc):
            raise
        # soft retry once
        P().wait_for_timeout(1500)
        nav(url, 60000)
    slow = source in ("amazon", "geizhals", "geizhals_cat", "vinted")
    P().wait_for_timeout(1500 if slow else 1200)
    accept_consent(P())
    # Extra consent / continue buttons on Amazon / Geizhals / Vinted
    for label in ("Continue shopping", "Einkauf fortsetzen", "Alle ablehnen", "Accept all", "Alles akzeptieren"):
        try:
            button = P().get_by_role("button", name=re.compile(label, re.I))
            if button.count() and button.first.is_visible(timeout=800):
                button.first.click(timeout=2000)
                P().wait_for_timeout(600)
        except Exception as exc:
            if is_fatal(exc):
                raise
    P().wait_for_timeout(700 if slow else 500)
    # Blockseiten / sporadische Fehlerseiten (eBay "Something went wrong")
    is_ebay = source in ("ebay", "ebay_cap") and worker is not None
    max_attempts = 5 if is_ebay else 4
    for attempt in range(max_attempts):
        if not looks_blocked(P()):
            if worker is not None:
                worker.consec_blocks = 0
            break
        if attempt == 0:
            _stat(source, "blocked")
        _stat(source, "block_hits")
        if attempt == max_attempts - 1:
            _stat(source, "errors")
            if worker is not None:
                worker.block_failed()
                if not getattr(worker, "in_retry", False):
                    worker.failed_loads.append((source, query, cap, page_no))
            return []
        try:
            if is_ebay:
                # eBay-403 ("Something went wrong") tritt zufaellig auf (~40%) und ist beim naechsten Versuch meist weg.
                # Backoff: 1) 3-5 s, 2) 5-8 s, 3) Startseite neu aufwaermen + 8-12 s, 4) frischer Kontext + 10-15 s
                if attempt == 0:
                    time.sleep(random.uniform(3, 5))
                elif attempt == 1:
                    time.sleep(random.uniform(5, 8))
                elif attempt == 2:
                    time.sleep(random.uniform(4, 6))
                    nav(HOMES["ebay"], paced=False)
                    P().wait_for_timeout(1500)
                    accept_consent(P())
                    time.sleep(random.uniform(8, 12))
                else:
                    time.sleep(random.uniform(6, 9))
                    worker.reset_context("eBay-Block")
                    time.sleep(random.uniform(4, 6))
                nav(url, paced=False)
                lo, hi = PACE["ebay"]
                worker.next_ok = time.time() + random.uniform(lo, hi)
            else:
                if attempt == 0:
                    P().wait_for_timeout(1500)
                else:
                    # bounce via homepage then retry search (with backoff)
                    P().wait_for_timeout(3000 * attempt)
                    nav(HOMES.get(source, url))
                    P().wait_for_timeout(1500)
                    accept_consent(P())
                nav(url)
            P().wait_for_timeout(1800)
            accept_consent(P())
        except Exception as exc:
            if is_fatal(exc):
                raise
            return []
    # Kleinanzeigen leitet "seite:N" hinter der letzten Seite auf Seite 1 um
    if source == "kleinanzeigen" and page_no > 1 and "seite:" not in P().url:
        return []
    cards = listing_cards(P(), source)
    _stat(source, "cards", len(cards))
    return cards


def collect_pages(page, source: str, query: str, cap: int, max_pages: int, worker=None) -> list[dict]:
    """Mehrere Ergebnisseiten; stoppt, sobald eine Seite nichts Neues liefert."""
    out: list[dict] = []
    urls: set[str] = set()
    for page_no in range(1, max_pages + 1):
        cards = collect(page, source, query, cap, page_no, worker=worker)
        new = [c for c in cards if c.get("url") not in urls]
        for c in new:
            urls.add(c.get("url"))
        out.extend(new)
        if not new:
            break
        # eBay zeigt bis ~60 pro Seite; weniger = letzte Seite
        if source == "ebay" and len(cards) < 200:
            break
        if source == "ebay_cap" and len(cards) < 200:
            break
        if source == "kleinanzeigen" and len(cards) < 20:
            break
        if source == "amazon" and len(cards) < 10:
            break
        if source == "geizhals_cat":
            ppus = [parse_price(c.get("ppu", "")) for c in cards if c.get("ppu")]
            ppus = [p for p in ppus if p]
            # Liste ist nach EUR/TB sortiert: sobald Seite deutlich ueber Limit liegt, aufhoeren
            if ppus and min(ppus[-10:]) > CANDIDATE_EUR_PER_TB + 4:
                break
    return out


def warm_site(page, source: str, errors: list[str]) -> None:
    """Homepage-first warmup to reduce 403 / bot walls."""
    homes = {
        "ebay": "https://www.ebay.de/",
        "kleinanzeigen": "https://www.kleinanzeigen.de/",
        "amazon": "https://www.amazon.de/",
        "geizhals": "https://geizhals.de/",
        "vinted": "https://www.vinted.de/",
    }
    url = homes.get(source)
    if not url:
        return
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1000)
        accept_consent(page)
        page.wait_for_timeout(500)
    except Exception as exc:
        errors.append(f"{source} start: {short_err(exc)}")
        if is_fatal(exc):
            raise


EBAY_DETAIL_JS = r"""
() => {
  const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : null; };
  const ifr = document.querySelector('iframe#desc_ifr') || document.querySelector('iframe[src*="ebaydesc.com"]');
  return {
    title: t('h1.x-item-title__mainTitle') || t('h1'),
    price: t('.x-price-primary'),
    approx: t('.x-price-approx'),
    condition: t('.x-item-condition-text') || t('[data-testid="x-item-condition"]'),
    shipping: t('[data-testid="ux-labels-values--shipping"]') || t('.ux-labels-values--shipping') || t('[data-testid="d-shipping"]'),
    location: t('[data-testid="ux-labels-values--itemLocation"]') || t('.ux-labels-values--itemLocation'),
    specs: (t('[data-testid="x-about-this-item"]') || t('.x-about-this-item') || t('.ux-layout-section-evo--features') || '').slice(0, 3000),
    ended: t('.ux-layout-section--statusmessage') || t('[data-testid="d-statusmessage"]') || t('.d-statusmessage'),
    desc_src: ifr ? ifr.src : null,
  };
}
"""


KA_DETAIL_JS = r"""
() => {
  const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
  const m = s => { const e = document.querySelector(s); return e ? (e.getAttribute('content') || '').trim() : ''; };
  return {
    title: t('#viewad-title') || t('h1#viewad-title') || t('.boxedarticle--title') || t('main h1') || t('h1'),
    og_title: m('meta[property="og:title"]'),
    locality: t('#viewad-locality'),
    price: t('#viewad-price'),
  };
}
"""


AMAZON_DETAIL_JS = r"""
() => {
  const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim().slice(0, 600) : null; };
  return {
    title: t('#productTitle'),
    price: t('#corePrice_feature_div .a-offscreen') || t('#corePriceDisplay_desktop_feature_div .a-offscreen')
           || t('#buybox .a-price .a-offscreen') || t('#price_inside_buybox'),
    buybox: t('#buybox') || t('#desktop_buybox'),
    merchant: t('#merchantInfoFeature_feature_div') || t('#merchant-info') || t('#tabular-buybox') || t('#sellerProfileTriggerId'),
    availability: t('#availability'),
    condition: t('#renewedProgramDescriptionAtf_feature_div') || t('#usedAccordionRow') || '',
    specs: t('#productDetails_techSpec_section_1') || t('#productOverview_feature_div') || '',
  };
}
"""


def geizhals_offers(html: str) -> list[dict]:
    """Angebotsliste einer Geizhals-Produktseite (t=v: Preis inkl. Versand)."""
    import html as _html
    offers = []
    for m in re.finditer(r'<div class="offer offer--available[^"]*"[^>]*id="offer-index-\d+"[^>]*>([\s\S]*?)(?=<div class="offer offer--|<div class="offerlist-disclaimer|$)', html):
        chunk = m.group(1)[:20000]
        pm = re.search(r'class="gh_price">\s*(?:€|&euro;)\s*([\d.]+,\d{2})', chunk)
        if not pm:
            continue
        shop_m = re.search(r"/OUTSHOP/([^'\"\\]+)", chunk)
        link_m = re.search(r'offer__clickout[\s\S]{0,6000}?\shref="([^"]+)"', chunk)
        det_m = re.search(r'class="offer__details[^"]*"[^>]*>([\s\S]{0,1500}?)</div>', chunk)
        link = _html.unescape(link_m.group(1)) if link_m else ""
        if link.startswith("/"):
            link = "https://geizhals.de" + link
        details = re.sub(r"<[^>]+>", " ", det_m.group(1)) if det_m else ""
        offers.append({
            "total": parse_amount(pm.group(1)),
            "shop": _html.unescape(shop_m.group(1)).strip() if shop_m else "?",
            "link": link,
            "details": re.sub(r"\s+", " ", _html.unescape(details)).strip()[:200],
        })
    return offers


class WorkerDead(Exception):
    pass


class Progress:
    """Fortschrittszeilen pro Quelle + grobe Gesamt-ETA (langsamste Quelle bestimmt die Laufzeit)."""

    def __init__(self, t0: float, every: int):
        self.t0 = t0
        self.every = every
        self.workers: list["SourceWorker"] = []

    def eta_search(self) -> float:
        rest = 0.0
        for w in self.workers:
            rest = max(rest, w.search_eta())
        return rest

    def report(self, w: "SourceWorker", force: bool = False) -> None:
        if not force and w.done % self.every and w.done != len(w.jobs):
            return
        st = STATS.get(w.src, {})
        say(
            f"  [{SOURCE_LABEL[w.src]:<13}] Suche {w.done}/{len(w.jobs)} | {len(w.cards)} Karten | "
            f"Aufrufe {st.get('loads', 0)} | blockiert {st.get('blocked', 0)} | Fehler {st.get('errors', 0)} | "
            f"Neustarts {st.get('relaunches', 0)} | {fmt_dur(time.time() - w.t_start)}"
            f" | Rest Quelle ~{fmt_dur(w.search_eta())} | Gesamt-ETA Suche ~{fmt_dur(self.eta_search())}"
            f" (gesamt {fmt_dur(time.time() - self.t0)})"
        )


class SourceWorker(threading.Thread):
    """Eine Quelle = ein Thread mit eigenem Playwright, Browser, Kontext und Tab.
    Innerhalb der Quelle strikt sequentiell mit Mindestabstand (PACE); stirbt der Browser,
    wird er neu gestartet (max. MODE['max_relaunches']) und die Anfrage einmal wiederholt."""

    def __init__(self, src: str, jobs: list[tuple[str, str, int]], progress: Progress, t0: float):
        super().__init__(name=f"worker-{src}", daemon=True)
        self.src = src
        self.jobs = jobs
        self.progress = progress
        self.t0 = t0
        self.t_start = time.time()
        self.cards: list[dict] = []
        self.errors: list[str] = []
        self.detail_jobs: list[dict] = []
        self.detail_results: list[dict] = []
        self.search_done = threading.Event()
        self.detail_ready = threading.Event()
        self.finished = threading.Event()
        self.done = 0
        self.detail_done = 0
        self.search_seconds = 0.0
        self.detail_seconds = 0.0
        self.relaunches = 0
        self.dead = False
        self.consec_blocks = 0
        self.failed_loads: list[tuple[str, str, int, int]] = []
        self.in_retry = False
        self.next_ok = 0.0
        self.loads_on_page = 0
        self.pw = None
        self.persistent = False
        self.no_persist = False
        self.browser = None
        self.context = None
        self.page = None
        self.ua = ""

    # ---------- Browser-Lebenszyklus ----------
    def _new_context(self) -> None:
        self.context = self.browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1280, "height": 900},
            user_agent=self.ua,
        )
        self.page = self.context.new_page()
        self.loads_on_page = 0
        self.pace()
        warm_site(self.page, self.src, self.errors)
        if self.src == "ebay":
            time.sleep(random.uniform(2.0, 3.5))

    def _launch(self) -> None:
        # UA passend zur echten Chromium-Version (ohne "HeadlessChrome")
        self.ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chromium_major()}.0.0.0 Safari/537.36"
        )
        if self.src in PERSISTENT_SOURCES and not self.no_persist:
            try:
                kwargs = dict(headless=True, args=BROWSER_ARGS, locale="de-DE", timezone_id="Europe/Berlin",
                              viewport={"width": 1280, "height": 900}, user_agent=self.ua)
                exe = chromium_executable()
                if exe:
                    kwargs["executable_path"] = exe
                PROFILE_DIR.mkdir(exist_ok=True)
                self.context = self.pw.chromium.launch_persistent_context(str(PROFILE_DIR / self.src), **kwargs)
                self.browser = None
                self.persistent = True
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                for extra in self.context.pages[1:]:
                    try:
                        extra.close()
                    except Exception:
                        pass
                self.loads_on_page = 0
                self.pace()
                warm_site(self.page, self.src, self.errors)
                time.sleep(random.uniform(2.0, 3.5))
                return
            except Exception as exc:
                self.errors.append(f"{self.src} Profil nicht nutzbar ({short_err(exc)}) - ohne Profil weiter")
                self.no_persist = True
                try:
                    if self.context is not None:
                        self.context.close()
                except Exception:
                    pass
        self.persistent = False
        self.browser = launch_browser(self.pw)
        self._new_context()

    def _close(self) -> None:
        for obj in (self.context, self.browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None

    def page_dead(self) -> bool:
        try:
            return (self.page is None or self.page.is_closed()
                    or (self.browser is not None and not self.browser.is_connected()))
        except Exception:
            return True

    def relaunch(self, reason: str) -> bool:
        if self.relaunches >= MODE["max_relaunches"]:
            self.dead = True
            say(f"  [{SOURCE_LABEL[self.src]}] Neustart-Limit ({MODE['max_relaunches']}) erreicht ({reason}) - Quelle wird beendet")
            return False
        self.relaunches += 1
        _stat(self.src, "relaunches")
        say(f"  [{SOURCE_LABEL[self.src]}] Browser-Neustart {self.relaunches}/{MODE['max_relaunches']}: {reason[:100]}")
        self._close()
        time.sleep(5 * self.relaunches)
        try:
            self._launch()
            return True
        except Exception as exc:
            self.errors.append(f"{self.src} Neustart: {short_err(exc)}")
            if self.relaunches < MODE["max_relaunches"]:
                return self.relaunch(f"Neustart fehlgeschlagen: {short_err(exc)}")
            self.dead = True
            return False

    def reset_context(self, reason: str) -> None:
        """Frischer Kontext (neue Cookies) im selben Browser + Startseite neu aufwaermen."""
        _stat(self.src, "ctx_resets")
        if self.persistent:
            # Persistentes Profil: Cookies leeren, neuer Tab, Startseite neu aufwaermen
            try:
                self.context.clear_cookies()
                old = self.page
                self.page = self.context.new_page()
                old.close()
            except Exception as exc:
                if is_fatal(exc):
                    raise
            self.loads_on_page = 0
            self.pace()
            warm_site(self.page, self.src, self.errors)
            time.sleep(random.uniform(2.0, 3.5))
            return
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        self._new_context()

    def pace(self) -> None:
        """Mindestabstand zwischen Seitenaufrufen derselben Seite einhalten."""
        wait = self.next_ok - time.time()
        if wait > 0:
            time.sleep(wait)
        lo, hi = PACE.get(self.src, (5.0, 7.0))
        self.next_ok = time.time() + random.uniform(lo, hi)
        self.loads_on_page += 1

    def block_failed(self) -> None:
        self.consec_blocks += 1
        if self.consec_blocks >= 2:
            cool = random.uniform(45, 75) * min(self.consec_blocks - 1, 3)
            say(f"  [{SOURCE_LABEL[self.src]}] {self.consec_blocks} Blockseiten in Folge - Pause {cool:.0f}s")
            self.next_ok = max(self.next_ok, time.time() + cool)

    def _recycle_page(self) -> None:
        # Langlaeufer: Tab alle ~60 Aufrufe erneuern (Cookies bleiben im Kontext)
        if self.loads_on_page < 60 or self.page_dead():
            return
        try:
            old = self.page
            self.page = self.context.new_page()
            old.close()
            self.loads_on_page = 0
        except Exception:
            pass

    def _guarded(self, fn, label: str):
        """fn() ausfuehren; bei toter Seite/Browser neu starten und genau einmal wiederholen."""
        for attempt in range(2):
            if self.dead:
                raise WorkerDead(f"{self.src} beendet (Neustart-Limit)")
            if self.page_dead() and not self.relaunch("Seite/Browser geschlossen"):
                raise WorkerDead(f"{self.src} beendet (Neustart-Limit)")
            try:
                return fn()
            except WorkerDead:
                raise
            except Exception as exc:
                fatal = is_fatal(exc) or self.page_dead()
                if fatal and attempt == 0 and self.relaunch(f"{label}: {short_err(exc)}"):
                    continue
                if fatal and self.dead:
                    raise WorkerDead(f"{self.src} beendet: {short_err(exc)}")
                raise

    # ---------- Phasen ----------
    def search_eta(self) -> float:
        if self.search_done.is_set():
            return 0.0
        el = time.time() - self.t_start
        if self.done == 0:
            lo, hi = PACE.get(self.src, (5.0, 7.0))
            return len(self.jobs) * (lo + hi) / 2
        return el / self.done * (len(self.jobs) - self.done)

    def _search_phase(self) -> None:
        deadline = self.t0 + MODE["search_budget_s"]
        for i, (source, query, pages) in enumerate(self.jobs, 1):
            if time.time() > deadline:
                rest = len(self.jobs) - i + 1
                self.errors.append(f"{self.src}: Zeitbudget Suche erschoepft, {rest} Suchen uebersprungen")
                _stat(self.src, "errors", rest)
                break
            if self.dead:
                rest = len(self.jobs) - i + 1
                self.errors.append(f"{self.src}: Quelle nach Neustart-Limit beendet, {rest} Suchen uebersprungen")
                _stat(self.src, "errors", rest)
                break
            self._recycle_page()
            try:
                cards = self._guarded(
                    lambda: collect_pages(self.page, source, query, WATCH_CAP, pages, worker=self),
                    query,
                )
                for card in cards:
                    card["query"] = query
                    card["source"] = stat_key(source)
                    self.cards.append(card)
            except WorkerDead as exc:
                self.errors.append(f"{source} / {query}: {exc}")
                _stat(self.src, "errors")
            except Exception as exc:
                self.errors.append(f"{source} / {query}: {short_err(exc)}")
                _stat(self.src, "errors")
            self.done = i
            self.progress.report(self)
        # Endgueltig blockierte Seiten am Ende der Suche noch einmal versuchen (Abdeckung vor Laufzeit)
        failed, self.failed_loads = list(self.failed_loads), []
        if failed and not self.dead:
            self.in_retry = True
            ok = 0
            for source, query, cap, page_no in failed:
                if time.time() > deadline or self.dead:
                    break
                time.sleep(random.uniform(8, 14))
                pg = page_no
                while True:
                    try:
                        cards = self._guarded(lambda: collect(self.page, source, query, cap, pg, worker=self), query)
                    except Exception as exc:
                        self.errors.append(f"{source} / {query} (Wiederholung): {short_err(exc)}")
                        break
                    if cards and pg == page_no:
                        ok += 1
                        _stat(self.src, "errors", -1)
                    for card in cards:
                        card["query"] = query
                        card["source"] = stat_key(source)
                        self.cards.append(card)
                    # Folgeseiten, die wegen der Blockade uebersprungen wurden
                    if source in ("ebay", "ebay_cap") and len(cards) >= 200 and pg < MODE["pages"].get(source, 1):
                        pg += 1
                        continue
                    break
            self.in_retry = False
            say(f"  [{SOURCE_LABEL[self.src]}] Wiederholung blockierter Seiten: {ok}/{len(failed)} nachgeholt")

    def _goto_detail(self, url: str) -> bool:
        """Detailseite laden mit Pacing + Block-Backoff. True = Seite ok."""
        for attempt in range(3):
            self.pace()
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_timeout(900 + 600 * attempt)
            accept_consent(self.page)
            if not looks_blocked(self.page):
                self.consec_blocks = 0
                return True
            if attempt == 0:
                _stat(self.src, "detail_blocked")
            if self.src == "ebay":
                if attempt == 0:
                    time.sleep(random.uniform(8, 12))
                else:
                    time.sleep(random.uniform(10, 15))
                    self.reset_context("eBay-Block (Detail)")
            else:
                self.page.wait_for_timeout(1500 * (attempt + 1))
        self.block_failed()
        return False

    def _fetch_detail(self, url: str) -> dict:
        _stat(self.src, "detail_loads")
        if self.src == "geizhals":
            url = url + ("&" if "?" in url else "?") + "hloc=de&t=v"
        if not self._goto_detail(url):
            return {"blocked": True}
        out: dict = {"final_url": self.page.url}
        try:
            out["page_title"] = self.page.title()
        except Exception:
            out["page_title"] = ""
        info: dict = {}
        if self.src == "ebay":
            try:
                self.page.mouse.wheel(0, 2500)
                self.page.wait_for_timeout(900)
            except Exception as exc:
                if is_fatal(exc):
                    raise
            try:
                info = self.page.evaluate(EBAY_DETAIL_JS) or {}
            except Exception as exc:
                if is_fatal(exc):
                    raise
            out["ebay"] = info
        if self.src == "kleinanzeigen":
            try:
                out["ka"] = self.page.evaluate(KA_DETAIL_JS) or {}
            except Exception as exc:
                if is_fatal(exc):
                    raise
        if self.src == "amazon":
            try:
                out["amazon"] = self.page.evaluate(AMAZON_DETAIL_JS) or {}
            except Exception as exc:
                if is_fatal(exc):
                    raise
        if self.src == "geizhals":
            try:
                out["gh_offers"] = geizhals_offers(self.page.content())
            except Exception as exc:
                if is_fatal(exc):
                    raise
        out["body"] = page_text(self.page)
        if self.src == "ebay":
            desc = ""
            try:
                desc = self.page.frame_locator("iframe#desc_ifr").locator("body").inner_text(timeout=8000)
            except Exception as exc:
                if is_fatal(exc):
                    raise
            if not desc.strip() and info.get("desc_src"):
                # Beschreibung liegt auf itm.ebaydesc.com (anderer Host) -> direkt laden
                try:
                    time.sleep(random.uniform(1.0, 2.0))
                    self.page.goto(info["desc_src"], wait_until="domcontentloaded", timeout=30000)
                    self.page.wait_for_timeout(700)
                    desc = page_text(self.page)
                except Exception as exc:
                    if is_fatal(exc):
                        raise
                    out["desc_error"] = short_err(exc)
            out["desc"] = (desc or "")[:10000]
        return out

    def _detail_phase(self) -> None:
        deadline = self.t0 + MODE["detail_budget_s"]
        t_d = time.time()
        for i, card in enumerate(self.detail_jobs, 1):
            rec: dict = {"card": card}
            if time.time() > deadline:
                rec["error"] = "Zeitbudget Detailphase erschoepft"
            else:
                try:
                    rec.update(self._guarded(lambda: self._fetch_detail(card["url"]), "Detail"))
                except WorkerDead as exc:
                    rec["error"] = str(exc)
                except Exception as exc:
                    rec["error"] = short_err(exc)
                    _stat(self.src, "errors")
            self.detail_results.append(rec)
            self.detail_done = i
            if i % 5 == 0 or i == len(self.detail_jobs):
                el = time.time() - t_d
                say(f"  [{SOURCE_LABEL[self.src]:<13}] Detail {i}/{len(self.detail_jobs)} | {fmt_dur(el)} | "
                    f"Rest ~{fmt_dur(el / i * (len(self.detail_jobs) - i))} (gesamt {fmt_dur(time.time() - self.t0)})")

    def run(self) -> None:
        try:
            with sync_playwright() as pw:
                self.pw = pw
                self.t_start = time.time()
                try:
                    self._launch()
                except Exception as exc:
                    self.errors.append(f"{self.src} Start: {short_err(exc)}")
                    if not self.relaunch(f"Start fehlgeschlagen: {short_err(exc)}"):
                        self.dead = True
                self._search_phase()
                self.search_seconds = time.time() - self.t_start
                self.search_done.set()
                say(f"  [{SOURCE_LABEL[self.src]:<13}] Suche fertig in {fmt_dur(self.search_seconds)}: {len(self.cards)} Karten")
                self.detail_ready.wait()
                if self.detail_jobs:
                    t_d = time.time()
                    if self.dead:
                        # tote Quelle fuer die Detailphase genau einmal zusaetzlich neu starten
                        say(f"  [{SOURCE_LABEL[self.src]}] Extra-Neustart fuer die Detailphase")
                        _stat(self.src, "relaunches")
                        self._close()
                        time.sleep(5)
                        try:
                            self._launch()
                            self.dead = False
                        except Exception as exc:
                            self.errors.append(f"{self.src} Detail-Neustart: {short_err(exc)}")
                    self._detail_phase()
                    self.detail_seconds = time.time() - t_d
                self._close()
        except Exception as exc:
            self.errors.append(f"{self.src} Worker: {short_err(exc)}")
        finally:
            self.search_done.set()
            self.finished.set()


# --- Standort, Geocoding (Nominatim + Cache) und Pendelroute Furtwangen <-> Herrenberg ---
ROUTE_MAX_KM = 20.0
NET_UA = "schnaeppchen-hdd/1.0 (privates HDD-Preisskript; einmal taeglich, wenige Anfragen, mit Cache)"
ROUTE_START, ROUTE_END = "Furtwangen im Schwarzwald", "Herrenberg"
# Uebliche Strecke: Furtwangen - Voehrenbach - Villingen-Schwenningen - A81 - Herrenberg (OSRM-Via-Punkte)
ROUTE_VIA = (("Furtwangen im Schwarzwald", 48.0512, 8.2074), ("Voehrenbach", 48.0458, 8.3039),
             ("Villingen-Schwenningen", 48.0632, 8.4930), ("Herrenberg", 48.5964, 8.8702))
# Fallback ohne OSRM: Handwegpunkte entlang L173/B523/A81 (grob, Abstand zur echten Strasse < ~3 km)
ROUTE_FALLBACK = ((48.0512, 8.2074), (48.0458, 8.3039), (48.0632, 8.4930), (48.0900, 8.5500), (48.1580, 8.6060),
                  (48.2350, 8.6150), (48.2950, 8.6350), (48.3650, 8.6600), (48.4350, 8.7200), (48.4900, 8.7600),
                  (48.5550, 8.8250), (48.5964, 8.8702))
_NET_LOCK = threading.Lock()
_LAST_NOMINATIM = [0.0]
_GEO_CACHE: dict | None = None
_ROUTE: dict | None = None


def http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": NET_UA, "Accept-Language": "de"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _geo_cache() -> dict:
    global _GEO_CACHE
    if _GEO_CACHE is None:
        try:
            _GEO_CACHE = json.loads(GEO_CACHE_PATH.read_text(encoding="utf-8-sig")) if GEO_CACHE_PATH.exists() else {}
        except Exception:
            _GEO_CACHE = {}
    return _GEO_CACHE


def save_geo_cache() -> None:
    if _GEO_CACHE is not None:
        GEO_CACHE_PATH.write_text(json.dumps(_GEO_CACHE, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def nominatim(params: dict) -> tuple[float, float] | None:
    """Hoeflich: max. 1 Anfrage/s, eigener User-Agent; Aufrufer cacht."""
    with _NET_LOCK:
        wait = 1.1 - (time.time() - _LAST_NOMINATIM[0])
        if wait > 0:
            time.sleep(wait)
        try:
            q = urllib.parse.urlencode({**params, "format": "json", "limit": 1})
            res = http_json("https://nominatim.openstreetmap.org/search?" + q)
        finally:
            _LAST_NOMINATIM[0] = time.time()
    if res:
        return float(res[0]["lat"]), float(res[0]["lon"])
    return None


VERSANDHANDEL = "Versandhandel"


def location_key(loc: str) -> str:
    return re.sub(r"\s+", " ", fold(loc or "")).strip(" ,-")


def geocode(loc: str) -> tuple[float, float] | None:
    """PLZ+Ort / Ort, Land -> (lat, lon). Ergebnisse (auch Fehlschlaege) landen in geo_cache.json."""
    if not loc or loc.startswith(VERSANDHANDEL) or "unbekannt" in loc.lower():
        return None
    key = location_key(loc)
    cache = _geo_cache()
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    result = None
    try:
        m = re.match(r"\s*(\d{5})\s+(.*)", loc)
        if m:
            result = nominatim({"postalcode": m.group(1), "countrycodes": "de"})
            if result is None and m.group(2).strip():
                result = nominatim({"q": f"{m.group(2).strip()}, Deutschland"})
        else:
            q = loc.strip()
            result = nominatim({"q": q})
    except Exception as exc:
        say(f"  Geocoding fehlgeschlagen fuer '{loc}': {short_err(exc)}")
        return None  # Netzfehler nicht cachen
    cache[key] = [round(result[0], 5), round(result[1], 5)] if result else None
    return result


def _simplify(points: list[list[float]], min_km: float = 0.15) -> list[list[float]]:
    out = [points[0]]
    for p in points[1:-1]:
        if haversine_km(out[-1], p) >= min_km:
            out.append(p)
    out.append(points[-1])
    return out


def haversine_km(a, b) -> float:
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def build_route() -> dict:
    """Einmalige Einrichtung: OSRM-Route (Via Voehrenbach/Villingen-Schwenningen) als Polylinie in route.json."""
    coords = ";".join(f"{lon:.5f},{lat:.5f}" for _, lat, lon in ROUTE_VIA)
    j = http_json(f"https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson", timeout=40)
    r = j["routes"][0]
    pts = [[round(lat, 5), round(lon, 5)] for lon, lat in r["geometry"]["coordinates"]]
    route = {
        "quelle": "OSRM router.project-osrm.org (driving), via " + " - ".join(n for n, _, _ in ROUTE_VIA),
        "erstellt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "laenge_km": round(r["distance"] / 1000, 1),
        "dauer_min": round(r["duration"] / 60),
        "punkte_roh": len(pts),
        "points": _simplify(pts),
    }
    ROUTE_PATH.write_text(json.dumps(route, ensure_ascii=False), encoding="utf-8")
    return route


def load_route(rebuild: bool = False) -> dict:
    global _ROUTE
    if _ROUTE is not None and not rebuild:
        return _ROUTE
    if ROUTE_PATH.exists() and not rebuild:
        try:
            _ROUTE = json.loads(ROUTE_PATH.read_text(encoding="utf-8-sig"))
            if _ROUTE.get("points"):
                return _ROUTE
        except Exception:
            pass
    try:
        _ROUTE = build_route()
        say(f"Route erstellt: {_ROUTE['quelle']}, {_ROUTE['laenge_km']} km, {len(_ROUTE['points'])} Punkte -> {ROUTE_PATH.name}")
    except Exception as exc:
        # Fallback nur im Speicher (naechster Lauf versucht OSRM erneut)
        pts = [list(p) for p in ROUTE_FALLBACK]
        length = sum(haversine_km(a, b) for a, b in zip(pts, pts[1:]))
        _ROUTE = {"quelle": f"Fallback-Wegpunkte (OSRM nicht erreichbar: {short_err(exc)})", "laenge_km": round(length, 1),
                  "points": pts}
        say("Route: " + _ROUTE["quelle"])
    return _ROUTE


def route_distance_km(pt: tuple[float, float], route: dict) -> float:
    """Minimaler Abstand Punkt -> Polylinie (lokale equirektangulaere Projektion, genau genug fuer < 100 km)."""
    import math
    lat0 = math.radians(pt[0])
    kx, ky = 111.32 * math.cos(lat0), 110.57
    px, py = pt[1] * kx, pt[0] * ky
    best = float("inf")
    pts = route["points"]
    for a, b in zip(pts, pts[1:]):
        ax, ay, bx, by = a[1] * kx, a[0] * ky, b[1] * kx, b[0] * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best:
            best = d
    return best


def ka_location(raw: str) -> str:
    """'72072 Baden-Wuerttemberg - Tuebingen' -> '72072 Tuebingen'."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    m = re.match(r"(\d{5}) (.+?) - (.+)$", raw)
    if m:
        return f"{m.group(1)} {m.group(3).strip()}"
    m = re.match(r"(\d{5}) (.+)$", raw)
    return f"{m.group(1)} {m.group(2).strip()}" if m else raw


def card_location(card: dict) -> str:
    src = card.get("source", "")
    if src in ("amazon", "geizhals"):
        return VERSANDHANDEL
    loc = (card.get("location") or "").strip()
    if src == "kleinanzeigen":
        return ka_location(loc) if loc else "unbekannt"
    if src == "ebay":
        return loc if loc else "unbekannt (vermutlich Deutschland)"
    return loc or "unbekannt"


def annotate_location(deals: list) -> None:
    """Abstand zur Pendelroute, Flag 'auf dem Weg', Abholpreis (Kleinanzeigen)."""
    if not deals:
        return
    try:
        route = load_route()
    except Exception as exc:
        say(f"Route nicht verfuegbar: {short_err(exc)}")
        return
    for d in deals:
        if not d.location:
            d.location = "unbekannt"
        pt = geocode(d.location)
        if pt is None:
            continue
        d.route_km = round(route_distance_km(pt, route), 1)
        d.on_route = d.route_km <= ROUTE_MAX_KM
        if d.on_route and d.source == "Kleinanzeigen" and d.pickup_allowed:
            d.pickup_price = d.price
            d.pickup_eur_per_tb = round(d.price / d.capacity_tb, 2) if d.capacity_tb else None
    try:
        save_geo_cache()
    except Exception:
        pass


def route_label(d) -> str:
    if d.location.startswith(VERSANDHANDEL):
        return "Versand"
    if d.route_km is None:
        return "Abstand unbekannt"
    return f"{d.route_km:.1f}".replace(".", ",") + " km zur Route" + (" - AUF DEM WEG" if d.on_route else "")



def send_telegram(text: str, config: dict) -> None:
    token = (config.get("telegram_bot_token") or "").strip()
    chat_id = (config.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def write_log(text: str, test: bool = False) -> Path:
    """Tageslog logs/YYYY-MM-DD.txt; Testlaeufe schreiben getrennt nach logs/YYYY-MM-DD_test.txt."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = LOG_DIR / (datetime.now().strftime("%Y-%m-%d") + ("_test" if test else "") + ".txt")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}]\n{text}\n\n")
    return path


def chromium_executable() -> str | None:
    root = Path.home() / "AppData" / "Local" / "ms-playwright"
    matches = sorted(root.glob("chromium-*/chrome-win64/chrome.exe"))
    if matches:
        return str(matches[-1])
    return None


# Persistentes Profil (user-data-dir) getestet: brachte bei eBay keine weniger Blockseiten (58% statt ~40%),
# daher aus. Zum Wiederaktivieren z.B. ("ebay",) eintragen.
PERSISTENT_SOURCES: tuple[str, ...] = ()
PROFILE_DIR = ROOT / "browser_profile"
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]


def chromium_major() -> str:
    exe = chromium_executable()
    try:
        if exe:
            for d in Path(exe).parent.iterdir():
                m = re.fullmatch(r"(\d+)\.\d+\.\d+\.\d+(?:\.manifest)?", d.name)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "131"


def launch_browser(playwright):
    exe = chromium_executable()
    if exe:
        return playwright.chromium.launch(
            headless=True,
            executable_path=exe,
            args=["--disable-blink-features=AutomationControlled"],
        )
    return playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )


def vinted_card_ok(preview: str, price: float) -> bool:
    """Vinted-Vorfilter: nur echte 3-8TB-HDD-Karten mit plausiblem Preis belegen Detailplaetze."""
    title = preview.split("\n", 1)[0]
    cap = capacity_tb(title) or model_number_tb(title)
    if cap is None or cap < MIN_TB or cap > MAX_TB:
        return False
    if not (preferred_label(title) or re.search(r"festplatte|\bhdd\b|\bnas\b|enterprise|ultrastar|exos", fold(title))):
        return False
    return price >= cap * 12


def preview_ok(preview: str, price: float) -> bool:
    if price <= 0 or price > WATCH_CAP:
        return False
    if rejected(preview):
        return False
    cap = capacity_tb(preview)
    if cap is not None and (cap < MIN_TB or cap > MAX_TB):
        return False
    # Allow missing capacity/condition on card — detail pass may fill in
    return True


EBAY_ENDED = r"dieses angebot wurde (?:vom verkaeufer )?beendet|angebot (?:ist )?beendet|der verkaeufer hat dieses angebot beendet|artikel ist nicht mehr verfuegbar|nicht mehr verfuegbar"


VINTED_SCAM = (
    r"message me|contact me|write me|text me|dm me|whats\s*app|telegram|"
    r"nicht\s+(?:ueber|uber|via|auf)\s+(?:diese|die|dieser)\s+(?:plattform|app|seite)|"
    r"not\s+(?:through|via|on)\s+(?:this|the)\s+(?:platform|app|site)|"
    r"don'?t\s+sell\s+(?:through|on|via)|do\s+not\s+sell\s+(?:through|on|via)|i\s+dont\s+sell|"
    r"ausserhalb\s+(?:von\s+)?vinted|outside\s+(?:of\s+)?vinted|paypal\s+(?:freunde|friends)|"
    r"(?:schreib|kontaktier)\w*\s+mir\s+(?:per|ueber|eine)\s+(?:mail|e-mail|whatsapp)"
)
EMAIL_RE = r"[\w.+-]+@[\w-]+\.(?:de|com|net|org|eu|info|at|ch)\b"
PHONE_RE = r"(?:\+|00)\s?49[\s\d/-]{8,}|\b01[5-7]\d[\s/-]?\d{6,8}\b"


def vinted_scam_reason(body: str, price: float, cap: float | None) -> str | None:
    """Typische Vinted-Betrugsmuster (Kontakt ausserhalb der Plattform, 0 Bewertungen + Dumpingpreis)."""
    # nur Anzeigenbereich pruefen (vor dem Footer)
    cut = re.split(r"Für deine Bestellungen bei anderen|Sicher einkaufen und verkaufen", body or "")[0]
    low = fold(cut)
    m = re.search(VINTED_SCAM, low)
    if m:
        return f"Vinted-Betrugsverdacht: '{low[max(0, m.start() - 25):m.end() + 25].strip()}'"
    if re.search(EMAIL_RE, cut, re.I):
        return "Vinted-Betrugsverdacht: E-Mail-Adresse in der Beschreibung"
    if re.search(PHONE_RE, cut):
        return "Vinted-Betrugsverdacht: Telefonnummer in der Beschreibung"
    rev = re.search(r"\n(\d+)\n(?:Häufige Uploads|Zuletzt online|Schneller Versand|Aktive[rs]? |Verifiziert)", cut)
    no_reviews = bool(re.search(r"noch keine bewertungen|keine bewertungen", low)) or (rev is not None and rev.group(1) == "0")
    if no_reviews and cap and price / cap < 25:
        return f"Vinted-Betrugsverdacht: 0 Bewertungen und unplausibel billig ({price / cap:.1f} EUR/TB)"
    return None


VINTED_SHIP_DEFAULT = 5.0
VINTED_SHIP_MIN = 4.0  # 3.5"-HDD ~0,7 kg: Paket S/M


def vinted_price_ship(body: str, card_price: float) -> tuple[float, float, str]:
    """Preis inkl. Kaeuferschutz (Vinted-Gebuehr) + geschaetzter Versand."""
    low = fold(body or "")
    price = card_price
    note = ""
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)\s*€\s*\n?\s*inkl\.?\s*(?:vinted-gebuehr|kaeuferschutz)", low)
    if m:
        price = parse_amount(m.group(1)) or card_price
        note = "inkl. Kaeuferschutz"
    else:
        price = round(card_price * 1.05 + 0.70, 2)
        note = "inkl. ca. 5%+0,70 Kaeuferschutz (geschaetzt)"
    sm = re.search(r"versand\s*\n?\s*ab\s*(\d{1,2}(?:[.,]\d{2})?)\s*€", low)
    if sm:
        ship = max(parse_amount(sm.group(1)) or 0.0, VINTED_SHIP_MIN)
    else:
        ship = VINTED_SHIP_DEFAULT
    ship_s = f"{ship:.2f}".replace(".", ",")
    return price, ship, note + f", Versand ca. {ship_s} EUR (geschaetzt)"


AMAZON_NOT_NEW = r"instandgesetzt|renewed|generalueberholt|refurbished|gebraucht|amazon warehouse|warehouse deals|wie neu|b-ware|zustand:\s*(?:gut|sehr gut|akzeptabel)"


def specs_capacity(specs: str) -> float | None:
    """Speicherkapazitaet aus eBay-Artikelmerkmalen (z.B. 'Speicherkapazitaet\n4 TB')."""
    m = re.search(r"(?:speicher|festplatten)?kapazitaet[^\n:]{0,20}[:\n]\s*(\d+(?:[.,]\d+)?)\s*(tb|gb)", fold(specs or ""))
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if m.group(2) == "tb" else v / 1000.0


KA_SHIP_ONLY = r"\bnur\s+versand\b|keine?\s+abholung|abholung\s+(?:ist\s+)?(?:leider\s+)?nicht\s+moeglich|kein\s+selbstabholer"
KA_GONE = r"anzeige (?:ist )?(?:leider )?(?:nicht mehr verfuegbar|wurde (?:geloescht|deaktiviert|entfernt))|diese anzeige existiert nicht|die gewuenschte anzeige ist nicht mehr"
KA_BAD_TITLE = r"zum inhalt springen|^kleinanzeigen|willkommen bei|^hallo|melde dich|einloggen|registrieren"


def ka_real_title(rec: dict) -> str:
    """Echter Anzeigentitel (#viewad-title/h1, sonst og:title/<title>), nie Skip-Link/Kopfzeile."""
    info = rec.get("ka") or {}
    for raw in (info.get("title"), info.get("og_title"), rec.get("page_title")):
        t = re.sub(r"\s+", " ", raw or "").strip()
        t = re.sub(r"^(?:(?:reserviert|gel(?:ö|oe)scht)\s*[•·|-]\s*)+", "", t, flags=re.I)
        t = re.sub(r"\s*\|\s*kleinanzeigen.*$", "", t, flags=re.I)
        t = re.sub(r"\s+in\s+[^|]{2,60}\s-\s[^|]{2,60}$", "", t)  # "... in Bundesland - Ort"
        if len(t) >= 6 and not re.search(KA_BAD_TITLE, fold(t)):
            return t
    return ""


def evaluate_detail(card: dict, rec: dict) -> tuple[Deal | None, str | None]:
    """Detailseite bewerten: bestehende Filter (consider) + quellenspezifische Pruefungen."""
    ka_reserved = False
    if card.get("source") == "kleinanzeigen":
        ad_id = re.search(r"/(\d{6,})-\d+-\d+", card.get("url", ""))
        final = rec.get("final_url") or ""
        if ad_id and final and ad_id.group(1) not in final:
            return None, "Anzeige nicht mehr online (Weiterleitung)"
        if re.search(KA_GONE, fold((rec.get("body") or "")[:3000])):
            return None, "Anzeige nicht mehr online"
        ka_reserved = bool(re.search(r"^\s*reserviert", fold(((rec.get("ka") or {}).get("title") or ""))))
        real = ka_real_title(rec)
        if not real:
            return None, "Kleinanzeigen: kein echter Anzeigentitel lesbar (uebersprungen)"
        card["title"] = real
        card["snippet"] = real
        if card.get("_recheck"):
            p_ka = parse_price(((rec.get("ka") or {}).get("price") or "").replace("VB", ""))
            if p_ka:
                card["price_value"] = p_ka
    if card.get("_recheck"):
        if card.get("source") == "ebay":
            info_r = rec.get("ebay") or {}
            if info_r.get("title"):
                card["title"] = card["snippet"] = info_r["title"]
            p_e = (eur_price_from_text(info_r.get("approx") or "") if info_r.get("approx")
                   else parse_price((info_r.get("price") or "").replace("EUR", "€")))
            if p_e:
                card["price_value"] = p_e
        elif card.get("source") == "vinted" and not card.get("title"):
            card["title"] = card["snippet"] = (rec.get("page_title") or "").split("|")[0].strip()
        if not card.get("price_value"):
            return None, "Wiederholungspruefung: kein Preis lesbar"
    preview = (card.get("title") or "") + "\n" + (card.get("snippet") or "")
    body = rec.get("body") or ""
    src = card.get("source", "")
    text = preview + "\n" + body
    fee, note = card.get("import_fee", 0.0), card.get("import_note", "")
    desc = rec.get("desc") or ""
    price = card["price_value"]
    extra_fazit: list[str] = []
    if src == "ebay":
        info = rec.get("ebay") or {}
        if re.search(EBAY_ENDED, fold((info.get("ended") or "") + "\n" + body[:2500])):
            return None, "Angebot beendet/nicht verfuegbar"
        reason = desc_used_reason(desc)
        if reason:
            return None, reason
        ship = ebay_detail_shipping(info.get("shipping") or "")
        if ship is None:
            ship = parse_shipping(text)
            if ship <= 0:
                ship = card["shipping_value"]
        loc = ebay_location(body, info) or card.get("location", "")
        country = non_eu_country(loc)
        if country:
            fee = import_estimate(price, ship, ebay_shown_import(body, info))
            note = f"Standort {loc}"
        else:
            fee, note = 0.0, ""
    elif src == "kleinanzeigen":
        ship = kleinanzeigen_shipping(core_listing_text(text))
    elif src == "vinted":
        cap_guess = capacity_tb_prefer_title(core_listing_text(text)) or model_number_tb(preview)
        scam = vinted_scam_reason(body, price, cap_guess)
        if scam:
            return None, scam
        price, ship, vnote = vinted_price_ship(body, price)
        extra_fazit.append(vnote)
    elif src == "amazon":
        info = rec.get("amazon") or {}
        # nur Kopf der Buybox (ausgewaehltes Angebot) - weiter unten stehen oft "Gebraucht ab ..."-Zusatzangebote
        bb = fold(str(info.get("buybox") or "")[:160] + " " + str(info.get("merchant") or "") + " " + str(info.get("condition") or "")[:300])
        m = re.search(AMAZON_NOT_NEW, bb)
        if m:
            return None, f"Amazon: Angebot ist keine Neuware ('{bb[max(0, m.start() - 20):m.end() + 20].strip()}')"
        p_amz = parse_price((info.get("price") or "").replace("€", " €"))
        if not p_amz:
            return None, "Amazon: kein Neupreis in der Buybox (nicht verfuegbar / nur Drittangebote)"
        price = p_amz
        ship = parse_shipping(info.get("buybox") or "")
        merchant = re.sub(r"\s+", " ", (info.get("merchant") or "")).strip()
        if merchant:
            extra_fazit.append("Verkaeufer: " + merchant[:80])
        if info.get("title"):
            text = info["title"] + "\n" + (info.get("specs") or "") + "\n" + body
    elif src == "geizhals":
        offers = [o for o in (rec.get("gh_offers") or []) if o.get("total") and not rejected(o.get("details", ""))]
        if offers:
            best_o = min(offers, key=lambda o: o["total"])
            price = best_o["total"]
            extra_fazit.append(f"Shop: {best_o['shop']} (guenstigstes Angebot inkl. Versand, {len(offers)} Angebote)")
        ship = 0.0
        # Nur Produktname/Specs aus der Liste werten: die Detailseite enthaelt Werbung/aehnliche Produkte
        text = preview
    else:
        ship = parse_shipping(text)
        if ship <= 0:
            ship = card["shipping_value"]
    deal = consider(text, price, ship, card["url"], src, fee, note)
    if src == "kleinanzeigen":
        core_k = fold(core_listing_text(text))
        ship_only = bool(re.search(KA_SHIP_ONLY, core_k))
        only_pickup = bool(re.search(r"nur\s+abholung", core_k)) and not re.search(r"versand\s+moeglich", core_k)
        if deal is None and not ship_only:
            # Abholpreis (ohne Versand) <= Limit: qualifiziert nur, wenn der Standort 'auf dem Weg' liegt (Pruefung in main)
            deal = consider(text, price, 0.0, card["url"], src, fee, note)
            if deal:
                deal.shipping = ship
                deal.eur_per_tb = eur_per_tb(price, ship, deal.capacity_tb, fee)
                deal.pickup_only = True
                deal.fazit += "; nur bei Abholung unter Limit"
        elif deal and only_pickup:
            deal.pickup_only = True  # 'Nur Abholung': nur sinnvoll, wenn auf dem Weg
        if deal:
            deal.pickup_allowed = not ship_only
            if ship_only:
                deal.fazit += "; nur Versand (keine Abholung)"
    if deal and src == "ebay":
        info = rec.get("ebay") or {}
        t_ = (card.get("title") or "") + " " + (info.get("title") or "")
        cap_t = capacity_tb(t_) or model_number_tb(t_)
        cap_s = specs_capacity(info.get("specs") or "")
        if cap_s and abs(cap_s - deal.capacity_tb) > 0.01:
            return None, f"Artikelmerkmal Speicherkapazitaet {cap_s:g}TB passt nicht zu {deal.capacity_tb:g}TB"
        if not cap_t and not cap_s:
            return None, "Kapazitaet weder im Titel noch in den Artikelmerkmalen (nur Seitentext)"
    if deal and deal.interface == "unbekannt" and desc:
        iface_d = interface_of((card.get("title") or "") + "\n" + desc)
        if iface_d != "unbekannt":
            deal.interface = (iface_d[:-1] + ", Beschreibung)") if iface_d.endswith(")") else (iface_d + " (Beschreibung)")
            deal.sas_only = is_sas_only(iface_d)
            bits = deal.fazit.split("; ")
            if "unbekannt" in bits:
                bits[bits.index("unbekannt")] = deal.interface
            deal.fazit = "; ".join(bits)
    if deal:
        deal.location = detail_location(src, card, rec)
        if ka_reserved:
            extra_fazit.append("Achtung: als reserviert markiert")
    if deal and extra_fazit:
        deal.fazit += "; " + "; ".join(extra_fazit)
    return deal, (None if deal else "Detail: " + filter_reason(text, price, ship, fee))


VINTED_LOC_RE = r"(?:Standort|Ort|Location)\s*\n\s*([^\n]{3,60})"


def detail_location(src: str, card: dict, rec: dict) -> str:
    body = rec.get("body") or ""
    if src == "kleinanzeigen":
        loc = ((rec.get("ka") or {}).get("locality") or "").strip()
        if not loc:
            m = re.search(r"\n(\d{5} [^\n]{2,60})\n", body[:4000])
            loc = m.group(1) if m else card.get("location", "")
        return ka_location(loc) if loc else "unbekannt"
    if src == "ebay":
        loc = ebay_location(body, rec.get("ebay"))
        return loc or card_location(card)
    if src == "vinted":
        m = re.search(VINTED_LOC_RE, body)
        if m and not re.search(r"\d+\s*(?:€|eur)", m.group(1), re.I):
            return m.group(1).strip()
        return "unbekannt (Vinted zeigt keinen Ort)"
    if src == "amazon":
        merchant = fold((rec.get("amazon") or {}).get("merchant") or "")
        if not merchant or "verkauf durch amazon" in merchant or re.search(r"verkaeufer\s*\n?\s*amazon", merchant):
            return VERSANDHANDEL + " (Amazon.de)"
        m = re.search(r"verk(?:ae|a)u(?:f|fer)\s*(?:durch)?\s*\n?\s*([^\n]{2,40})", (rec.get("amazon") or {}).get("merchant") or "", re.I)
        return VERSANDHANDEL + (f" (Haendler: {m.group(1).strip()})" if m else " (Drittanbieter)")
    if src == "geizhals":
        offers = [o for o in (rec.get("gh_offers") or []) if o.get("total") and not rejected(o.get("details", ""))]
        if offers:
            return VERSANDHANDEL + f" (Shop: {min(offers, key=lambda o: o['total'])['shop']})"
        return VERSANDHANDEL
    return card_location(card)


def filter_reason(text: str, price: float, shipping: float, fee: float = 0.0) -> str:
    """Grund, warum consider() abgelehnt hat (nur fuer die Ausgabe)."""
    core = core_listing_text(text)
    title = (core.split("\n", 1)[0] if core else "") or (text.split("\n", 1)[0] if text else "")
    if rejected(core) or rejected(title):
        nn = not_new_reason(core, "\n".join(core.splitlines()[:2]))
        if nn:
            return f"nicht neu: Ruecklaeufer/Retoure/B-Ware/RMA/Refurb ('{nn}')"
        return "Ausschlusswort (gebraucht/defekt/Bundle/SSD o.ae.)"
    if re.search(SCSI_TITLE, fold(title)):
        return "paralleles SCSI"
    if not is_new_condition(core):
        return "Zustand nicht eindeutig neu"
    cap = capacity_tb_prefer_title(core) or capacity_tb_prefer_title(text) or model_number_tb(title)
    if cap is None or cap < MIN_TB or cap > MAX_TB:
        return "Kapazitaet nicht 3-8 TB / nicht erkannt"
    mm = capacity_mismatch(title, cap)
    if mm:
        return mm
    ept = eur_per_tb(price, shipping, cap, fee)
    if ept > MAX_EUR_PER_TB:
        return f"{ept:.2f} EUR/TB (Preis {price:.2f} + Versand {shipping:.2f}) > {MAX_EUR_PER_TB:g}"
    if ept < MIN_EUR_PER_TB:
        return f"{ept:.2f} EUR/TB unplausibel niedrig"
    ok, reason = dauerbetrieb_judgment(core)
    if not ok:
        return reason
    if detect_form(core).startswith("2.5"):
        return "2,5 Zoll"
    return "Filter nicht bestanden"


def known_reference(rec: dict, known_url: str) -> tuple[str, Deal | None]:
    if rec.get("error") or rec.get("blocked"):
        return f"Bekannte Referenz {known_url}: Abruf fehlgeschlagen ({rec.get('error') or 'blockiert'})", None
    body = rec.get("body") or ""
    info = rec.get("ebay") or {}
    title = rec.get("page_title") or ""
    text = title + "\n" + body
    price_m = re.search(r"(EUR|€)\s*([\d.]+,\d{2})", body) or re.search(r"([\d.]+,\d{2})\s*€", body)
    price_val = parse_price(price_m.group(0)) if price_m else None
    ship = ebay_detail_shipping(info.get("shipping") or "")
    if ship is None:
        ship = parse_shipping(body)
    cap = capacity_tb(text)
    if not (price_val and cap):
        return f"Bekannte Referenz {known_url}: Preis/Kapazitaet nicht sicher gelesen.", None
    ept = eur_per_tb(price_val, ship, cap)
    ok, reason = dauerbetrieb_judgment(text)
    cond = condition_of(text) if is_new_condition(text) else ("Gebraucht/Unklar — " + condition_of(text))
    desc_reason = desc_used_reason(rec.get("desc") or "")
    iface = interface_of(core_listing_text(text))
    qual = (ept <= MAX_EUR_PER_TB and is_new_condition(core_listing_text(text)) and ok
            and not rejected(core_listing_text(text)) and not desc_reason)
    note = (
        f"Bekannte Referenz {known_url}:\n"
        f"  Titel/Seite: {title[:100]}\n"
        f"  Preis {price_val:.2f} + Versand {ship:.2f} = {ept:.2f} EUR/TB bei {cap:g}TB\n"
        f"  Zustand: {cond}; Dauerbetrieb: {reason}; "
        f"Interface: {iface}; Form: {detect_form(text)}\n"
        + (f"  {desc_reason}\n" if desc_reason else "")
        + f"  Qualifiziert bis {MAX_EUR_PER_TB:g} EUR/TB + Neu + Dauerbetrieb: {'JA' if qual else 'NEIN'}"
        + (" (aber SAS-only, nicht in SATA-Liste)" if is_sas_only(iface) else "")
    )
    deal = None if desc_reason else consider(text, price_val, ship, known_url, "ebay")
    if deal:
        deal.geprueft = "Detail geprueft"
    return note, deal


CAP_BUCKETS = (3.0, 4.0, 5.0, 6.0, 8.0)


def geizhals_market(candidates: list[dict]) -> dict[float, dict]:
    """Guenstigste Neuware je Kapazitaet laut Geizhals (inkl. Versand, auch ueber dem Limit) als Marktreferenz."""
    out: dict[float, dict] = {}
    for c in candidates:
        if c.get("source") != "geizhals":
            continue
        title = c.get("title") or ""
        if rejected(title) or re.search(r"bundle|\d+er-pack|\bpack\b", title, re.I):
            continue
        cap = capacity_tb(title)
        price = parse_price(c.get("price", ""))
        if not cap or not price or cap not in CAP_BUCKETS:
            continue
        ok, _ = dauerbetrieb_judgment(title)
        if not ok:
            continue
        ept = price / cap
        if cap not in out or ept < out[cap]["ept"]:
            out[cap] = {"ept": ept, "price": price, "title": title, "url": c.get("url", "")}
    return out


def market_overview(best: list[Deal], candidates: list[dict]) -> str:
    lines = [f"Marktueberblick (qualifizierte Deals bis {MAX_EUR_PER_TB:g} EUR/TB, SATA/unbekannt; Abholpreis nur 'auf dem Weg'):"]
    for cap in CAP_BUCKETS:
        ds = [d for d in best if abs(d.capacity_tb - cap) < 0.01]
        if ds:
            d = ds[0]
            lines.append(f"  {cap:g} TB: {d.eur_per_tb:.2f} EUR/TB ({d.price + d.shipping + d.import_fee:.2f} EUR, {d.source}) {d.url}")
        else:
            lines.append(f"  {cap:g} TB: kein Deal bis {MAX_EUR_PER_TB:g} EUR/TB")
    by_src: dict[str, Deal] = {}
    for d in best:
        if d.source not in by_src:
            by_src[d.source] = d
    lines.append("  Bester Deal je Quelle:")
    for src_l in ("eBay.de", "Kleinanzeigen", "Amazon.de", "Geizhals", "Vinted.de"):
        d = by_src.get(src_l)
        lines.append(f"    {src_l}: " + (f"{d.eur_per_tb:.2f} EUR/TB {d.model} {d.url}" if d else "-"))
    gm = geizhals_market(candidates)
    if gm:
        lines.append(f"  Marktreferenz Geizhals (guenstigste gelesene Neuware 3.5\" SATA inkl. Versand, Einzelstueck, auch ueber {MAX_EUR_PER_TB:g}):")
        for cap in CAP_BUCKETS:
            g = gm.get(cap)
            if g:
                lines.append(f"    {cap:g} TB: {g['ept']:.2f} EUR/TB ({g['price']:.2f} EUR) {g['title'][:60]} {g['url']}")
    return "\n".join(lines)


def write_html_report(new_list: list[Deal], still_list: list[Deal], sas_list: list[Deal], overview: str,
                      candidates: list[dict], stats: dict, runtime: float, test: bool, gruendlich: bool,
                      route: dict | None = None) -> Path:
    import html as _h
    out_dir = ROOT / "berichte"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / (datetime.now().strftime("%Y-%m-%d") + ".html")
    def esc(x) -> str:
        return _h.escape(str(x))

    def table(deals: list[Deal], tid: str, empty: str) -> str:
        rows = []
        for d in deals:
            total = d.price + d.shipping + d.import_fee
            rk = d.route_km if d.route_km is not None else 9999
            pick = (f"{d.pickup_price:.2f}<br><span class='small'>{d.pickup_eur_per_tb:.2f} EUR/TB</span>"
                    if d.pickup_price else "-")
            rows.append(
                f"<tr class='{'weg' if d.on_route else ''}'>"
                f"<td>{'<b>AUF DEM WEG</b>' if d.on_route else ''}{'<br>nur bei Abholung unter Limit' if d.pickup_only else ''}</td>"
                f"<td data-v='{d.eur_per_tb:.4f}'>{d.eur_per_tb:.2f}</td><td data-v='{total:.2f}'>{total:.2f}</td>"
                f"<td data-v='{d.pickup_eur_per_tb or 9999}'>{pick}</td>"
                f"<td data-v='{d.capacity_tb:g}'>{d.capacity_tb:g} TB</td><td>{esc(d.model)}<br><span class='small'>{esc(d.title)}</span></td>"
                f"<td>{esc(d.source)}</td><td>{esc(d.location or 'unbekannt')}</td>"
                f"<td data-v='{rk}'>{'' if d.route_km is None else f'{d.route_km:.1f} km'}</td>"
                f"<td>{esc(d.condition)}</td><td>{esc(d.interface)}</td><td data-v='{d.price:.2f}'>{d.price:.2f}</td>"
                f"<td data-v='{d.shipping:.2f}'>{d.shipping:.2f}</td><td data-v='{d.import_fee:.2f}'>{d.import_fee:.2f}</td>"
                f"<td>{esc(d.geprueft or '-')}</td><td>{esc(d.first_seen or '-')}</td><td><a href='{esc(d.url)}' target='_blank'>Link</a></td>"
                f"<td class='f'>{esc(d.fazit)}</td></tr>")
        head = ("<tr><th>Weg</th><th>EUR/TB</th><th>Gesamt EUR</th><th>Abholung EUR</th><th>Kap.</th><th>Modell</th><th>Quelle</th>"
                "<th>Standort</th><th>Abstand Route</th><th>Zustand</th><th>Interface</th><th>Preis</th><th>Versand</th><th>Einfuhr</th>"
                "<th>Geprueft</th><th>Bekannt seit</th><th>Link</th><th>Fazit</th></tr>")
        return (f"<table class='sortable' id='{tid}'><thead>{head}</thead><tbody>"
                + ("".join(rows) or f"<tr><td colspan='18'>{empty}</td></tr>") + "</tbody></table>")

    sas_rows = "".join(
        f"<li>{d.eur_per_tb:.2f} EUR/TB - {esc(d.model)} - <a href='{esc(d.url)}' target='_blank'>{esc(d.url)}</a></li>"
        for d in sas_list[:15])
    stat_rows = "".join(
        f"<tr><td>{esc(SOURCE_LABEL.get(k, k))}</td>" + "".join(f"<td>{v.get(x, 0)}</td>" for x in STAT_KEYS) + "</tr>"
        for k, v in stats.items())
    route = route or {}
    route_txt = (f"Pendelroute {ROUTE_START} &harr; {ROUTE_END}: {route.get('laenge_km', '?')} km, Quelle: {esc(route.get('quelle', 'unbekannt'))}. "
                 f"<b>AUF DEM WEG</b> (gruen) = Standort hoechstens {ROUTE_MAX_KM:g} km von der Strecke; bei Kleinanzeigen zusaetzlich Abholpreis ohne Versand."
                 if route.get("points") else "Pendelroute nicht verfuegbar.")
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>HDD-Schnaeppchen {datetime.now():%Y-%m-%d}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:20px;color:#222}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}} th{{background:#eef;cursor:pointer;position:sticky;top:0}}
tr:nth-child(even){{background:#f8f8f8}} tr.weg{{background:#d9f5d9 !important}} td.f{{font-size:11px;color:#555;max-width:420px}} pre{{background:#f4f4f4;padding:8px;white-space:pre-wrap}}
.small{{font-size:12px;color:#666}}</style></head><body>
<h1>HDD-Schnaeppchen {datetime.now():%d.%m.%Y %H:%M}{' (TEST)' if test else ''}</h1>
<p class="small">Neue 3,5"-HDDs 3-8 TB fuer 24/7/NAS, SATA oder Interface unbekannt, bis {MAX_EUR_PER_TB:g} EUR/TB inkl. Versand
(und ggf. Einfuhrabgaben); Kleinanzeigen 'auf dem Weg' auch, wenn der Abholpreis ohne Versand bis {MAX_EUR_PER_TB:g} EUR/TB liegt. Modus: {'gruendlich' if gruendlich else 'Standard'}, Laufzeit {fmt_dur(runtime)}, {len(candidates)} Rohkandidaten.
Spaltenkopf anklicken zum Sortieren.</p>
<p class="small">{route_txt}</p>
<h2>Neu ({len(new_list)})</h2>
{table(new_list, 't_neu', 'Heute keine neuen qualifizierten Deals.')}
<h2>Weiterhin verfuegbar ({len(still_list)})</h2>
<p class="small">Bereits frueher gemeldet (gesehen.json) und in diesem Lauf erneut online gefunden und geprueft.</p>
{table(still_list, 't_alt', 'Keine.')}
<h2>Marktueberblick</h2><pre>{esc(overview)}</pre>
<h2>SAS-only (ausgeschlossen, nur Info)</h2><ul>{sas_rows or '<li>keine</li>'}</ul>
<h2>Quellen-Statistik</h2><table><tr><th>Quelle</th>{''.join(f'<th>{x}</th>' for x in STAT_KEYS)}</tr>{stat_rows}</table>
<script>
document.querySelectorAll('table.sortable').forEach(tbl => tbl.querySelectorAll('th').forEach((th, i) => th.addEventListener('click', () => {{
  const tb = tbl.querySelector('tbody'); const rows = Array.from(tb.rows);
  const asc = th.dataset.asc !== '1'; th.dataset.asc = asc ? '1' : '0';
  const val = r => {{ const c = r.cells[i]; if (!c) return ''; const v = c.dataset.v; return v !== undefined ? parseFloat(v) : c.innerText.toLowerCase(); }};
  rows.sort((a, b) => {{ const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); }});
  rows.forEach(r => tb.appendChild(r));
}})));
</script></body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="HDD-Schnaeppchen-Suche")
    ap.add_argument("--gruendlich", action="store_true",
                    help="Ausfuehrlicher Lauf: Kapazitaet x Modell-Matrix, Teilenummern, mehrere Seiten, mehr Detailseiten (~1-2 h)")
    ap.add_argument("--ignore-seen", action="store_true", help="gesehen.json beim Filtern ignorieren (wird trotzdem aktualisiert)")
    ap.add_argument("--json", default="", help="Alle qualifizierten Deals zusaetzlich als JSON hierhin schreiben")
    ap.add_argument("--max-queries", type=int, default=0, help="Nur fuer Tests: Anzahl Suchbegriffe begrenzen")
    ap.add_argument("--route-neu", action="store_true",
                    help="Pendelroute Furtwangen-Herrenberg neu von OSRM holen (route.json ueberschreiben)")
    ap.add_argument("--test", action="store_true",
                    help="Testlauf: gesehen.json und referenz.json werden NICHT geschrieben")
    return ap.parse_args(argv)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.gruendlich:
        MODE.update(GRUENDLICH_MODE)
    hdd_queries = gruendlich_queries() if MODE["gruendlich"] else HDD_QUERIES
    portal_queries = gruendlich_portal_queries() if MODE["gruendlich"] else PORTAL_QUERIES
    ebay_cap_queries = list(EBAY_CAP_QUERIES)
    ebay_kw_queries = list(EBAY_FAMILY_QUERIES) + (list(G_PARTNUMBERS) if MODE["gruendlich"] else [])
    if args.max_queries:
        hdd_queries = hdd_queries[: args.max_queries]
        portal_queries = portal_queries[: args.max_queries]
        ebay_cap_queries = ebay_cap_queries[: args.max_queries]
        ebay_kw_queries = ebay_kw_queries[: max(1, args.max_queries // 2)]
    pages = MODE["pages"]
    config = load_config()
    set_max_eur_per_tb(config.get("max_eur_per_tb", MAX_EUR_PER_TB))
    t0 = time.time()
    print(f"Modus: {'GRUENDLICH' if MODE['gruendlich'] else 'Standard'}{' [TEST - kein Schreiben von gesehen/referenz]' if args.test else ''}; "
          f"{len(hdd_queries)} Kleinanzeigen-Suchen, eBay {len(ebay_cap_queries)} Kapazitaets- + {len(ebay_kw_queries)} Modellsuchen, "
          f"{len(portal_queries)} Portal-Suchen, Seiten {pages}", flush=True)
    print(f"Limit: {MAX_EUR_PER_TB:g} EUR/TB inkl. Versand (Kandidatenfenster bis {CANDIDATE_EUR_PER_TB:.1f}); "
          f"Kleinanzeigen auf dem Weg (<= {ROUTE_MAX_KM:g} km): Abholpreis bis {MAX_EUR_PER_TB:g}", flush=True)
    seen = load_seen()
    seen_meta = load_seen_meta()
    seen_filter = set() if args.ignore_seen else seen
    if args.route_neu:
        load_route(rebuild=True)
    candidates: list[dict] = []
    errors: list[str] = []
    known_url = "https://www.ebay.de/itm/236859451862"
    known_note = ""

    # --- 1) Suche: eine Quelle = ein Worker-Thread mit eigenem Browser, parallel ---
    progress = Progress(t0, every=10 if MODE["gruendlich"] else 5)
    jobs = {
        "geizhals": [("geizhals_cat", "Kategorie HDD 3.5 nach EUR/TB", pages["geizhals"])],
        "kleinanzeigen": [("kleinanzeigen", q, pages["kleinanzeigen"]) for q in hdd_queries],
        "ebay": ([("ebay_cap", q, pages["ebay_cap"]) for q in ebay_cap_queries]
                 + [("ebay", q, pages["ebay"]) for q in ebay_kw_queries]),
        "amazon": [("amazon", q, pages.get("amazon", 1)) for q in portal_queries],
        "vinted": [("vinted", q, pages.get("vinted", 1)) for q in portal_queries],
    }
    workers = {src: SourceWorker(src, jobs[src], progress, t0) for src in WORKER_ORDER}
    progress.workers = list(workers.values())
    for src in WORKER_ORDER:
        _stat(src, "loads", 0)
    print("Starte Quellen parallel (je eigener Browser, innerhalb der Quelle sequentiell): "
          + ", ".join(f"{SOURCE_LABEL[s_]} {len(jobs[s_])} Suchen" for s_ in WORKER_ORDER), flush=True)
    ranked: list[dict] = []
    deals: list[Deal] = []
    cheapest_ept: float | None = None
    detail_rejects: list[str] = []
    try:
        for w in workers.values():
            w.start()
            time.sleep(3.0)  # Browserstarts versetzen (Lastspitzen vermeiden)
        search_wait_until = t0 + MODE["search_budget_s"] + 300
        for w in workers.values():
            w.search_done.wait(timeout=max(1.0, search_wait_until - time.time()))
        hung = [src for src, w in workers.items() if not w.search_done.is_set()]
        if hung:
            errors.append("Quelle(n) reagieren nicht, weiter ohne: " + ", ".join(hung))
        print(f"Suche abgeschlossen nach {fmt_dur(time.time() - t0)}", flush=True)
        for src in WORKER_ORDER:
            candidates.extend(list(workers[src].cards))

        # --- 2) Zusammenfuehren + bestehende Filter/Ranking ---
        seen_urls: set[str] = set()
        for card in candidates:
            if card.get("source") == "ebay" and re.search(r"£|\$|CHF", card.get("price", "")):
                price = eur_price_from_text(card.get("price", ""))
            else:
                price = parse_price(card.get("price", ""))
            if price is None or price <= 0:
                continue
            url = norm_url(card["url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            card["url"] = url
            ship_src = (card.get("shipping_text") or "") + "\n" + (card.get("price") or "") + "\n" + (card.get("snippet") or "")
            if card.get("source") == "kleinanzeigen":
                ship = kleinanzeigen_shipping(ship_src)
            else:
                ship = parse_shipping(ship_src)
            preview = (card.get("title") or "") + "\n" + (card.get("snippet") or "")
            if not preview_ok(preview, price):
                continue
            if card.get("source") == "vinted" and not vinted_card_ok(preview, price):
                continue
            # Bereits gemeldete Deals NICHT ausfiltern: sie werden erneut geprueft und unter "Weiterhin verfuegbar" gelistet
            card["_seen"] = url in seen_filter
            card["price_value"] = price
            card["shipping_value"] = ship
            country = non_eu_country(card.get("location", ""))
            card["import_fee"] = import_estimate(price, ship) if country else 0.0
            card["import_note"] = f"Standort {card.get('location')}" if country else ""
            cap = capacity_tb(preview)
            if cap and is_new_condition(preview) and not rejected(preview):
                ept = eur_per_tb(price, ship, cap, card["import_fee"])
                if ept < MAX_EUR_PER_TB:
                    ok, _ = dauerbetrieb_judgment(preview)
                    if ok and (cheapest_ept is None or ept < cheapest_ept):
                        cheapest_ept = ept
            ranked.append(card)

        need_detail: list[dict] = []
        preview_deals: list[tuple[Deal, dict]] = []
        for card in ranked:
            preview = (card.get("title") or "") + "\n" + (card.get("snippet") or "")
            deal = consider(preview, card["price_value"], card["shipping_value"], card["url"], card.get("source", ""),
                            card.get("import_fee", 0.0), card.get("import_note", ""))
            if deal is None and card.get("source") == "kleinanzeigen":
                # Abhol-Kandidat: ohne Versand unter dem Limit -> Detailseite (Standort) entscheidet
                deal = consider(preview, card["price_value"], 0.0, card["url"], "kleinanzeigen")
                if deal:
                    card["_pickup_cand"] = True
            if deal:
                # eBay/Kleinanzeigen-Treffer zusaetzlich auf der Detailseite pruefen (Beschreibung, Standort, Versand)
                if not deal.sas_only and card.get("source") in VERIFY_SOURCES:
                    preview_deals.append((deal, card))
                else:
                    deals.append(deal)
            else:
                # Detail if capacity or newness unclear but not hard-rejected
                if rejected(preview):
                    continue
                t_caps = title_capacities(card.get("title") or "")
                if t_caps and not any(MIN_TB <= c_ <= MAX_TB for c_ in t_caps):
                    continue  # Titel nennt nur Kapazitaeten ausserhalb 3-8 TB (z.B. 1TB, 12TB)
                if capacity_tb(preview) is None or not is_new_condition(preview) or preferred_label(preview):
                    need_detail.append(card)

        def est_ept(card: dict) -> float:
            prev = (card.get("title") or "") + "\n" + (card.get("snippet") or "")
            cap = capacity_tb(prev) or model_number_tb(prev) or capacity_tb(card.get("query") or "") or 8.0
            # Kleinanzeigen: ohne Versand schaetzen (Abholung auf dem Weg moeglich)
            ship_e = 0.0 if card.get("source") == "kleinanzeigen" else card["shipping_value"]
            return (card["price_value"] + ship_e + card.get("import_fee", 0.0)) / cap

        # Nur Kandidaten, die nach Schaetzung im (weiten) Kandidatenfenster liegen; guenstigste EUR/TB zuerst
        need_detail = [c for c in need_detail if MIN_EUR_PER_TB * 0.8 <= est_ept(c) <= CANDIDATE_EUR_PER_TB]
        need_detail.sort(key=est_ept)

        def pv_key(dc) -> float:
            d_, c_ = dc
            if c_.get("source") == "kleinanzeigen":
                return (c_["price_value"] + c_.get("import_fee", 0.0)) / d_.capacity_tb  # Preis/TB ohne Versand
            return d_.eur_per_tb
        preview_deals.sort(key=pv_key)
        verify = []
        v_count: dict[str, int] = {}
        for deal, card in preview_deals:
            src_v = card.get("source", "")
            if card.get("_seen"):
                verify.append((deal, card))  # bekannte Deals immer pruefen (zaehlen nicht gegen das Limit)
            elif v_count.get(src_v, 0) < MODE["verify_limit"]:
                v_count[src_v] = v_count.get(src_v, 0) + 1
                verify.append((deal, card))
            elif src_v == "vinted" or card.get("_pickup_cand"):
                continue  # Vinted nie ungeprueft melden (Betrugsrisiko); Abhol-Kandidaten brauchen den Standort
            else:
                deal.geprueft = "Detail ungeprueft"
                deal.location = card_location(card)
                deals.append(deal)
        # Limit pro Quelle: sonst belegen billige Vinted-Karten (ohne Kapazitaet = 8 TB geschaetzt) alle Plaetze
        per_src: dict[str, int] = {}
        detail_cards = []
        for c in need_detail:
            src_c = c.get("source", "")
            if c.get("_seen"):
                detail_cards.append(c)
            elif per_src.get(src_c, 0) < MODE["detail_limit"]:
                per_src[src_c] = per_src.get(src_c, 0) + 1
                detail_cards.append(c)
        for deal, card in verify:
            card["_preview_deal"] = deal
            detail_cards.append(card)
        print(f"Detailseiten: {len(detail_cards)} von {len(need_detail)} Kandidaten (max {MODE['detail_limit']} pro Quelle)"
              f" + {len(verify)} Vorschau-Deals zur Pruefung", flush=True)
        for card in detail_cards:
            src = card.get("source", "")
            if src in workers:
                workers[src].detail_jobs.append(card)
        # Bekannte Deals, die die Suche diesmal nicht gezeigt hat: Detailseite direkt erneut pruefen (noch online?)
        rechecks = 0
        for u in sorted(seen_filter - seen_urls):
            src_r = seen_source(u)
            m_r = seen_meta.get(u, {})
            if src_r not in workers or (src_r == "geizhals" and not m_r.get("title")):
                continue
            workers[src_r].detail_jobs.append({
                "url": u, "source": src_r, "_recheck": True, "_seen": True, "title": m_r.get("title", ""),
                "snippet": m_r.get("title", ""), "price_value": float(m_r.get("price") or 0.0),
                "shipping_value": float(m_r.get("shipping") or 0.0), "import_fee": 0.0, "import_note": "",
                "location": ""})
            rechecks += 1
        if rechecks:
            print(f"  Bekannte Deals (gesehen.json) nicht in der Suche, direkt nachgeprueft: {rechecks}", flush=True)
        # Bekannte HGST-Referenz immer mitpruefen (eBay-Worker)
        workers["ebay"].detail_jobs.append({"url": known_url, "source": "ebay", "_known": True, "title": "", "snippet": ""})
        print("  Detail pro Quelle: " + ", ".join(
            f"{SOURCE_LABEL[s_]} {len(workers[s_].detail_jobs)}" for s_ in WORKER_ORDER if workers[s_].detail_jobs), flush=True)
    finally:
        for w in workers.values():
            w.detail_ready.set()

    # --- 3) Detailphase laeuft pro Quelle parallel (im Worker, mit dessen warmem Browser) ---
    detail_wait_until = t0 + MODE["detail_budget_s"] + 300
    for w in workers.values():
        w.finished.wait(timeout=max(1.0, detail_wait_until - time.time()))
    hung = [src for src, w in workers.items() if not w.finished.is_set()]
    if hung:
        errors.append("Detailphase haengt bei: " + ", ".join(hung))
    for src in WORKER_ORDER:
        errors.extend(workers[src].errors)
        for rec in list(workers[src].detail_results):
            card = rec["card"]
            if card.get("_known"):
                known_note, known_deal = known_reference(rec, known_url)
                if known_deal and known_url not in {d.url for d in deals}:
                    deals.append(known_deal)
                continue
            pdeal = card.get("_preview_deal")
            if rec.get("error") or rec.get("blocked"):
                errors.append(f"Detail {'blockiert' if rec.get('blocked') else rec.get('error')}: {card['url']}")
                if pdeal and src != "vinted" and not card.get("_pickup_cand"):
                    pdeal.geprueft = "Detail nicht pruefbar"
                    pdeal.location = card_location(card)
                    deals.append(pdeal)
                continue
            deal, why = evaluate_detail(card, rec)
            if deal:
                deal.geprueft = "Detail geprueft"
                deals.append(deal)
            elif pdeal or card.get("_seen") or (why and why.startswith("Beschreibung")):
                tag = " [bekannt aus gesehen.json, qualifiziert nicht mehr]" if card.get("_seen") else ""
                detail_rejects.append(f"{card['url']}: {why or 'Detail: Filter nicht bestanden'}{tag}")

    if not candidates and errors:
        message = "Suche fehlgeschlagen, heute keine zuverlaessige Aussage.\n" + "\n".join(errors[:8])
        write_log(message, args.test)
        print(message)
        return 1

    unique: dict[str, Deal] = {}
    for deal in deals:
        current = unique.get(deal.url)
        if current is None or deal.score > current.score:
            unique[deal.url] = deal
    all_sorted = sorted(unique.values(), key=lambda d: d.eur_per_tb)
    # Nutzer will nur SATA: SAS-only separat (nur Log), nicht in der Deal-Liste
    sas_list = [d for d in all_sorted if d.sas_only]
    sata = [d for d in all_sorted if not d.sas_only]
    annotate_location(sata)
    kept = []
    for d in sata:
        if d.pickup_only and not (d.on_route and d.pickup_allowed and d.pickup_eur_per_tb is not None
                                  and d.pickup_eur_per_tb <= MAX_EUR_PER_TB):
            where = "Abstand unbekannt" if d.route_km is None else f"{d.route_km:.1f} km zur Route"
            detail_rejects.append(f"{d.url}: nur bei Abholung unter Limit, aber nicht auf dem Weg ({d.location}, {where})")
            continue
        kept.append(d)
    sata = sorted(kept, key=lambda d: d.effective_ept())
    new_list = [d for d in sata if d.url not in seen_filter][: MODE["top_n"]]
    still_list = [d for d in sata if d.url in seen_filter][: MODE["top_n"]]
    for d in still_list:
        m_s = seen_meta.get(d.url, {})
        d.status = "Weiterhin verfuegbar"
        d.first_seen = (m_s.get("first_seen_note", "") + " " + m_s.get("first_seen", "")).strip()
    for d in new_list:
        d.status = "Neu"
    route_info = (_ROUTE or {})
    # Auf-dem-Weg-Deals zuerst, sonst nach EUR/TB
    order = lambda d: (not d.on_route, d.effective_ept())  # noqa: E731
    new_list.sort(key=order)
    still_list.sort(key=order)
    best = sorted(new_list + still_list, key=lambda d: d.effective_ept())
    message = (f"NEU ({len(new_list)}):\n" + format_deals(new_list)) if new_list else NO_DEAL
    if not new_list and cheapest_ept is not None and not still_list:
        shown = f"{cheapest_ept:.2f}".replace(".", ",")
        message += f"\nGuenstigstes gesehenes EUR/TB (vor Finalfilter): {shown}"
    if still_list:
        message += f"\n\nWEITERHIN VERFUEGBAR (bereits gemeldet, noch online) ({len(still_list)}):\n" + format_deals(still_list)
    if route_info.get("points"):
        message += (f"\n\nRoute {ROUTE_START} <-> {ROUTE_END}: {route_info.get('laenge_km')} km ({route_info.get('quelle', '')[:60]}); "
                    f"'AUF DEM WEG' = Standort <= {ROUTE_MAX_KM:g} km von der Strecke")
    if known_note:
        message += "\n\n" + known_note
    if errors:
        message += "\n\nHinweis, einzelne Suchen sind fehlgeschlagen:\n" + "\n".join(errors[:6])

    if REF_PATH.exists():
        ref = json.loads(REF_PATH.read_text(encoding="utf-8-sig"))
    else:
        ref = {}
    # Keep parallel historical keys if present; update hdd reference
    notes = []
    # Referenz nur aus detailgeprueften, unverdaechtigen SATA/unbekannt-Deals mit plausiblem Gesamtpreis
    ref_ok = [d for d in best if d.geprueft == "Detail geprueft" and not d.sas_only and not d.pickup_only
              and d.price + d.shipping >= d.capacity_tb * MIN_EUR_PER_TB]
    if ref_ok:
        top = ref_ok[0]
        current = ref.get("hdd")
        payload = {
            "model": top.model,
            "capacity_tb": top.capacity_tb,
            "price": top.price,
            "shipping": top.shipping,
            "eur_per_tb": round(top.eur_per_tb, 2),
            "url": top.url,
            "condition": top.condition,
            "dauerbetrieb": top.dauerbetrieb,
        }
        if current is None or top.eur_per_tb < float(current.get("eur_per_tb", 999)):
            ref["hdd"] = payload
            if current and top.eur_per_tb <= float(current.get("eur_per_tb", 999)) - 3:
                notes.append(
                    f"Super Schnaeppchen: {top.model} liegt klar unter der Referenz von "
                    f"{current.get('eur_per_tb')} EUR/TB"
                )
        if top.eur_per_tb > MAX_EUR_PER_TB - 4:
            notes.append(f"Grenzwertig: {top.model} bei {top.eur_per_tb:.2f} EUR/TB (Ziel klar unter {MAX_EUR_PER_TB:g})")
    if not args.test:
        REF_PATH.write_text(json.dumps(ref, indent=2, ensure_ascii=False), encoding="utf-8")
    if notes:
        message += "\n\n" + "\n".join(notes)

    overview = market_overview(best, candidates)
    message += "\n\n" + overview
    try:
        rpath = write_html_report(new_list, still_list, sas_list, overview, candidates, STATS, time.time() - t0, args.test,
                                  MODE["gruendlich"], route_info)
        print(f"HTML-Bericht: {rpath}")
    except Exception as exc:
        errors.append(f"HTML-Bericht: {short_err(exc)}")
    extra = ""
    if sas_list:
        extra += "\n\nSAS-only ausgeschlossen (nur Info, nicht SATA):\n" + "\n".join(
            f"  {d.eur_per_tb:.2f} EUR/TB  {d.model}  {d.price + d.shipping + d.import_fee:.2f} EUR  {d.url}" for d in sas_list[:10])
    if detail_rejects:
        extra += f"\n\nAuf Detailseite verworfen ({len(detail_rejects)}):\n" + "\n".join("  " + r for r in detail_rejects[:15])
    path = write_log(message + extra, args.test)
    print(message + extra)
    print(f"\nLog: {path}")
    sources_seen = sorted({c.get("source","?") for c in candidates})
    print(f"Kandidaten roh: {len(candidates)}, ranked: {len(ranked)}, Deals: {len(best)}, Quellen: {sources_seen}")
    per_src_deals: dict[str, int] = {}
    for d in best:
        per_src_deals[d.source] = per_src_deals.get(d.source, 0) + 1
    timings = {src: {"suche_s": round(workers[src].search_seconds), "detail_s": round(workers[src].detail_seconds),
                     "suchen": f"{workers[src].done}/{len(workers[src].jobs)}", "details": len(workers[src].detail_jobs)}
               for src in WORKER_ORDER}
    print("Quellen-Statistik:")
    print(f"  {'Quelle':<14}{'Suchen':>8}{'Aufrufe':>9}{'Karten':>8}{'Blockiert':>10}{'403-Hits':>9}{'Fehler':>8}{'Neustarts':>10}"
          f"{'Ktx-Reset':>10}{'Detail':>8}{'D-Block':>8}{'Suche':>9}{'Detail':>9}")
    for src in WORKER_ORDER:
        st = STATS.get(src, {})
        tm = timings[src]
        print(f"  {SOURCE_LABEL[src]:<14}{tm['suchen']:>8}{st.get('loads', 0):>9}{st.get('cards', 0):>8}{st.get('blocked', 0):>10}{st.get('block_hits', 0):>9}"
              f"{st.get('errors', 0):>8}{st.get('relaunches', 0):>10}{st.get('ctx_resets', 0):>10}{st.get('detail_loads', 0):>8}"
              f"{st.get('detail_blocked', 0):>8}{fmt_dur(tm['suche_s']):>9}{fmt_dur(tm['detail_s']):>9}")
    print("Quellen-Statistik (json): " + json.dumps(STATS, ensure_ascii=False))
    print("Deals pro Quelle: " + json.dumps(per_src_deals, ensure_ascii=False) + f"; SAS-only ausgeschlossen: {len(sas_list)}")
    print(f"Laufzeit: {time.time()-t0:.0f}s ({fmt_dur(time.time()-t0)})")
    json_path = args.json or (str(LOG_DIR / ("gruendlich_" + datetime.now().strftime("%Y%m%d_%H%M") + ".json")) if MODE["gruendlich"] else "")
    if json_path:
        Path(json_path).write_text(
            json.dumps(
                {
                    "deals": [d.__dict__ for d in new_list + still_list],
                    "neu": [d.url for d in new_list],
                    "weiterhin_verfuegbar": [d.url for d in still_list],
                    "route": {k: v for k, v in route_info.items() if k != "points"},
                    "sas_only": [d.__dict__ for d in sas_list],
                    "detail_rejects": detail_rejects,
                    "timings": timings,
                    "runtime_s": round(time.time() - t0),
                    "stats": STATS,
                    "deals_per_source": per_src_deals,
                    "errors": errors,
                    "candidates": len(candidates),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"JSON: {json_path}")
    if args.test:
        print("TEST-Modus: gesehen.json / referenz.json unveraendert, kein Telegram.")
        return 0
    try:
        send_telegram(message, config)
    except Exception as exc:
        print(f"Telegram fehlgeschlagen: {exc}", file=sys.stderr)
    if best:
        today = datetime.now().strftime("%Y-%m-%d")
        for deal in best:
            seen_meta[deal.url] = seen_meta_entry(deal, seen_meta.get(deal.url), today)
        seen.update(deal.url for deal in best)
        save_seen(seen, seen_meta)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
