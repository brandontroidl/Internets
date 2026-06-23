"""Reference lookups — Wikipedia, DOI, ISBN, Stack Overflow, RFC, arXiv, elements.

All KEYLESS.  Every outbound HTTP call goes through base.fetch_json (size-capped)
except .arxiv, whose endpoint returns ATOM XML — that path uses defusedxml plus a
size-capped raw requests fetch.

    .wiki <query>            Wikipedia summary (first sentence + URL)
    .doi <doi>               Crossref work metadata (title/authors/journal/year)
    .isbn <isbn>             Open Library book (title/authors/year/publisher)
    .so <query>              top Stack Overflow question (title/score/answered/link)
    .rfc <number>            RFC metadata (title/status/date)
    .arxiv <id|query>        arXiv paper (title/authors/date/link) — ATOM XML
    .element <name|sym|Z>    periodic-table entry (offline, no network)
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

from defusedxml import ElementTree  # arXiv ATOM is 3rd-party XML — defuse XXE/billion-laughs.

from .base import (
    BotModule,
    ResponseTooLarge,
    cred,
    fetch_json,
    help_row,
    strip_ctrl,
)

log = logging.getLogger("internets.reflookup")

_MAX_INPUT = 200
# arXiv ATOM feeds for a single result are small, but a query can return a verbose
# entry; cap the raw body well under the JSON default before defusedxml parses it.
_MAX_XML_BYTES = 256 * 1024
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


# ── periodic table (offline) ──────────────────────────────────────────────
# (symbol, Z, name, atomic mass, group, period, category)
_ELEMENTS: list[tuple[str, int, str, float, int, int, str]] = [
    ("H", 1, "Hydrogen", 1.008, 1, 1, "nonmetal"),
    ("He", 2, "Helium", 4.0026, 18, 1, "noble gas"),
    ("Li", 3, "Lithium", 6.94, 1, 2, "alkali metal"),
    ("Be", 4, "Beryllium", 9.0122, 2, 2, "alkaline earth metal"),
    ("B", 5, "Boron", 10.81, 13, 2, "metalloid"),
    ("C", 6, "Carbon", 12.011, 14, 2, "nonmetal"),
    ("N", 7, "Nitrogen", 14.007, 15, 2, "nonmetal"),
    ("O", 8, "Oxygen", 15.999, 16, 2, "nonmetal"),
    ("F", 9, "Fluorine", 18.998, 17, 2, "halogen"),
    ("Ne", 10, "Neon", 20.180, 18, 2, "noble gas"),
    ("Na", 11, "Sodium", 22.990, 1, 3, "alkali metal"),
    ("Mg", 12, "Magnesium", 24.305, 2, 3, "alkaline earth metal"),
    ("Al", 13, "Aluminium", 26.982, 13, 3, "post-transition metal"),
    ("Si", 14, "Silicon", 28.085, 14, 3, "metalloid"),
    ("P", 15, "Phosphorus", 30.974, 15, 3, "nonmetal"),
    ("S", 16, "Sulfur", 32.06, 16, 3, "nonmetal"),
    ("Cl", 17, "Chlorine", 35.45, 17, 3, "halogen"),
    ("Ar", 18, "Argon", 39.948, 18, 3, "noble gas"),
    ("K", 19, "Potassium", 39.098, 1, 4, "alkali metal"),
    ("Ca", 20, "Calcium", 40.078, 2, 4, "alkaline earth metal"),
    ("Sc", 21, "Scandium", 44.956, 3, 4, "transition metal"),
    ("Ti", 22, "Titanium", 47.867, 4, 4, "transition metal"),
    ("V", 23, "Vanadium", 50.942, 5, 4, "transition metal"),
    ("Cr", 24, "Chromium", 51.996, 6, 4, "transition metal"),
    ("Mn", 25, "Manganese", 54.938, 7, 4, "transition metal"),
    ("Fe", 26, "Iron", 55.845, 8, 4, "transition metal"),
    ("Co", 27, "Cobalt", 58.933, 9, 4, "transition metal"),
    ("Ni", 28, "Nickel", 58.693, 10, 4, "transition metal"),
    ("Cu", 29, "Copper", 63.546, 11, 4, "transition metal"),
    ("Zn", 30, "Zinc", 65.38, 12, 4, "transition metal"),
    ("Ga", 31, "Gallium", 69.723, 13, 4, "post-transition metal"),
    ("Ge", 32, "Germanium", 72.630, 14, 4, "metalloid"),
    ("As", 33, "Arsenic", 74.922, 15, 4, "metalloid"),
    ("Se", 34, "Selenium", 78.971, 16, 4, "nonmetal"),
    ("Br", 35, "Bromine", 79.904, 17, 4, "halogen"),
    ("Kr", 36, "Krypton", 83.798, 18, 4, "noble gas"),
    ("Rb", 37, "Rubidium", 85.468, 1, 5, "alkali metal"),
    ("Sr", 38, "Strontium", 87.62, 2, 5, "alkaline earth metal"),
    ("Y", 39, "Yttrium", 88.906, 3, 5, "transition metal"),
    ("Zr", 40, "Zirconium", 91.224, 4, 5, "transition metal"),
    ("Nb", 41, "Niobium", 92.906, 5, 5, "transition metal"),
    ("Mo", 42, "Molybdenum", 95.95, 6, 5, "transition metal"),
    ("Tc", 43, "Technetium", 98.0, 7, 5, "transition metal"),
    ("Ru", 44, "Ruthenium", 101.07, 8, 5, "transition metal"),
    ("Rh", 45, "Rhodium", 102.91, 9, 5, "transition metal"),
    ("Pd", 46, "Palladium", 106.42, 10, 5, "transition metal"),
    ("Ag", 47, "Silver", 107.87, 11, 5, "transition metal"),
    ("Cd", 48, "Cadmium", 112.41, 12, 5, "transition metal"),
    ("In", 49, "Indium", 114.82, 13, 5, "post-transition metal"),
    ("Sn", 50, "Tin", 118.71, 14, 5, "post-transition metal"),
    ("Sb", 51, "Antimony", 121.76, 15, 5, "metalloid"),
    ("Te", 52, "Tellurium", 127.60, 16, 5, "metalloid"),
    ("I", 53, "Iodine", 126.90, 17, 5, "halogen"),
    ("Xe", 54, "Xenon", 131.29, 18, 5, "noble gas"),
    ("Cs", 55, "Caesium", 132.91, 1, 6, "alkali metal"),
    ("Ba", 56, "Barium", 137.33, 2, 6, "alkaline earth metal"),
    ("La", 57, "Lanthanum", 138.91, 3, 6, "lanthanide"),
    ("Ce", 58, "Cerium", 140.12, 0, 6, "lanthanide"),
    ("Pr", 59, "Praseodymium", 140.91, 0, 6, "lanthanide"),
    ("Nd", 60, "Neodymium", 144.24, 0, 6, "lanthanide"),
    ("Pm", 61, "Promethium", 145.0, 0, 6, "lanthanide"),
    ("Sm", 62, "Samarium", 150.36, 0, 6, "lanthanide"),
    ("Eu", 63, "Europium", 151.96, 0, 6, "lanthanide"),
    ("Gd", 64, "Gadolinium", 157.25, 0, 6, "lanthanide"),
    ("Tb", 65, "Terbium", 158.93, 0, 6, "lanthanide"),
    ("Dy", 66, "Dysprosium", 162.50, 0, 6, "lanthanide"),
    ("Ho", 67, "Holmium", 164.93, 0, 6, "lanthanide"),
    ("Er", 68, "Erbium", 167.26, 0, 6, "lanthanide"),
    ("Tm", 69, "Thulium", 168.93, 0, 6, "lanthanide"),
    ("Yb", 70, "Ytterbium", 173.05, 0, 6, "lanthanide"),
    ("Lu", 71, "Lutetium", 174.97, 3, 6, "lanthanide"),
    ("Hf", 72, "Hafnium", 178.49, 4, 6, "transition metal"),
    ("Ta", 73, "Tantalum", 180.95, 5, 6, "transition metal"),
    ("W", 74, "Tungsten", 183.84, 6, 6, "transition metal"),
    ("Re", 75, "Rhenium", 186.21, 7, 6, "transition metal"),
    ("Os", 76, "Osmium", 190.23, 8, 6, "transition metal"),
    ("Ir", 77, "Iridium", 192.22, 9, 6, "transition metal"),
    ("Pt", 78, "Platinum", 195.08, 10, 6, "transition metal"),
    ("Au", 79, "Gold", 196.97, 11, 6, "transition metal"),
    ("Hg", 80, "Mercury", 200.59, 12, 6, "transition metal"),
    ("Tl", 81, "Thallium", 204.38, 13, 6, "post-transition metal"),
    ("Pb", 82, "Lead", 207.2, 14, 6, "post-transition metal"),
    ("Bi", 83, "Bismuth", 208.98, 15, 6, "post-transition metal"),
    ("Po", 84, "Polonium", 209.0, 16, 6, "post-transition metal"),
    ("At", 85, "Astatine", 210.0, 17, 6, "halogen"),
    ("Rn", 86, "Radon", 222.0, 18, 6, "noble gas"),
    ("Fr", 87, "Francium", 223.0, 1, 7, "alkali metal"),
    ("Ra", 88, "Radium", 226.0, 2, 7, "alkaline earth metal"),
    ("Ac", 89, "Actinium", 227.0, 3, 7, "actinide"),
    ("Th", 90, "Thorium", 232.04, 0, 7, "actinide"),
    ("Pa", 91, "Protactinium", 231.04, 0, 7, "actinide"),
    ("U", 92, "Uranium", 238.03, 0, 7, "actinide"),
    ("Np", 93, "Neptunium", 237.0, 0, 7, "actinide"),
    ("Pu", 94, "Plutonium", 244.0, 0, 7, "actinide"),
    ("Am", 95, "Americium", 243.0, 0, 7, "actinide"),
    ("Cm", 96, "Curium", 247.0, 0, 7, "actinide"),
    ("Bk", 97, "Berkelium", 247.0, 0, 7, "actinide"),
    ("Cf", 98, "Californium", 251.0, 0, 7, "actinide"),
    ("Es", 99, "Einsteinium", 252.0, 0, 7, "actinide"),
    ("Fm", 100, "Fermium", 257.0, 0, 7, "actinide"),
    ("Md", 101, "Mendelevium", 258.0, 0, 7, "actinide"),
    ("No", 102, "Nobelium", 259.0, 0, 7, "actinide"),
    ("Lr", 103, "Lawrencium", 266.0, 3, 7, "actinide"),
    ("Rf", 104, "Rutherfordium", 267.0, 4, 7, "transition metal"),
    ("Db", 105, "Dubnium", 268.0, 5, 7, "transition metal"),
    ("Sg", 106, "Seaborgium", 269.0, 6, 7, "transition metal"),
    ("Bh", 107, "Bohrium", 270.0, 7, 7, "transition metal"),
    ("Hs", 108, "Hassium", 269.0, 8, 7, "transition metal"),
    ("Mt", 109, "Meitnerium", 278.0, 9, 7, "unknown"),
    ("Ds", 110, "Darmstadtium", 281.0, 10, 7, "unknown"),
    ("Rg", 111, "Roentgenium", 282.0, 11, 7, "unknown"),
    ("Cn", 112, "Copernicium", 285.0, 12, 7, "transition metal"),
    ("Nh", 113, "Nihonium", 286.0, 13, 7, "unknown"),
    ("Fl", 114, "Flerovium", 289.0, 14, 7, "unknown"),
    ("Mc", 115, "Moscovium", 290.0, 15, 7, "unknown"),
    ("Lv", 116, "Livermorium", 293.0, 16, 7, "unknown"),
    ("Ts", 117, "Tennessine", 294.0, 17, 7, "unknown"),
    ("Og", 118, "Oganesson", 294.0, 18, 7, "unknown"),
]
_BY_SYMBOL = {e[0].lower(): e for e in _ELEMENTS}
_BY_NAME = {e[2].lower(): e for e in _ELEMENTS}
_BY_Z = {e[1]: e for e in _ELEMENTS}


def element_lookup(query: str) -> str:
    """Pure offline periodic-table lookup by symbol, name, or atomic number."""
    q = (query or "").strip()
    if not q:
        return "usage: .element <name|symbol|Z>"
    e = None
    if q.isdigit():
        e = _BY_Z.get(int(q))
    if e is None:
        e = _BY_SYMBOL.get(q.lower()) or _BY_NAME.get(q.lower())
    if e is None:
        return f"no element matching '{strip_ctrl(q, 40)}'"
    symbol, z, name, mass, group, period, category = e
    grp = f"group {group}" if group else "group —"
    return (f"\x02{name}\x02 ({symbol}) :: Z={z} :: mass {mass} :: "
            f"{grp} :: period {period} :: {category}")


# ── Wikipedia ─────────────────────────────────────────────────────────────
def _wiki_summary(title: str, ua: str) -> dict | None:
    """REST summary for an exact (URL-encoded) title, or None on 404."""
    data = fetch_json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
        ua=ua, timeout=10, allow_404=True,
    )
    return data if (data and isinstance(data, dict)) else None


def _wiki_search_title(query: str, ua: str) -> str | None:
    """Resolve free text to the best article title via opensearch (forgiving of
    case/punctuation), or None.  Returns [term, [titles], [descs], [urls]]."""
    data = fetch_json(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "opensearch", "search": query, "limit": "1",
                "namespace": "0", "format": "json"},
        ua=ua, timeout=10, allow_404=True,
    )
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list) and data[1]:
        return data[1][0]
    return None


def _wiki_sync(query: str, ua: str) -> str:
    try:
        q = query.strip()
        title = quote(q.replace(" ", "_"), safe="")
        data = _wiki_summary(title, ua)
        if data is None:
            # Exact-title miss (wrong case/punctuation): resolve via search.
            resolved = _wiki_search_title(q, ua)
            if resolved:
                data = _wiki_summary(quote(resolved.replace(" ", "_"), safe=""), ua)
        if data is None:
            return f"no Wikipedia article for '{strip_ctrl(query, 60)}'"
        page_title = strip_ctrl(data.get("title", query), 120)
        url = strip_ctrl(
            data.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://en.wikipedia.org/wiki/{title}",
            200,
        )
        if data.get("type") == "disambiguation":
            return f"\x02{page_title}\x02 (disambiguation — be more specific) — {url}"
        extract = strip_ctrl(data.get("extract", "").strip(), 300)
        if not extract:
            return f"\x02{page_title}\x02 — {url}"
        return f"\x02{page_title}\x02: {extract} — {url}"
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"wiki lookup: {e}")
        return "lookup failed"
    except Exception as e:  # requests.RequestException + json errors
        log.warning(f"wiki lookup: {e}")
        return "lookup failed"


# ── DOI (Crossref) ────────────────────────────────────────────────────────
def _doi_sync(doi: str, ua: str) -> str:
    try:
        d = doi.strip()
        data = fetch_json(
            f"https://api.crossref.org/works/{quote(d, safe='/')}",
            ua=ua,
            timeout=10,
            allow_404=True,
        )
        if not data or not isinstance(data, dict):
            return f"no Crossref record for '{strip_ctrl(d, 60)}'"
        msg = data.get("message", {})
        titles = msg.get("title") or []
        title = strip_ctrl(titles[0] if titles else "(untitled)", 200)
        authors = []
        for a in (msg.get("author") or [])[:3]:
            fam = a.get("family") or a.get("name") or ""
            given = a.get("given", "")
            authors.append(f"{given} {fam}".strip() if given else fam)
        author_str = ", ".join(strip_ctrl(a, 60) for a in authors if a) or "?"
        containers = msg.get("container-title") or []
        journal = strip_ctrl(containers[0], 100) if containers else ""
        year = ""
        parts = (msg.get("issued") or {}).get("date-parts") or []
        if parts and parts[0]:
            year = str(parts[0][0])
        bits = [f"\x02{title}\x02", author_str]
        if journal:
            bits.append(journal)
        if year:
            bits.append(year)
        return " :: ".join(bits)
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"doi lookup: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f"doi lookup: {e}")
        return "lookup failed"


# ── ISBN (Open Library) ───────────────────────────────────────────────────
def _isbn_sync(isbn: str, ua: str) -> str:
    try:
        n = isbn.strip().replace("-", "").replace(" ", "")
        key = f"ISBN:{n}"
        data = fetch_json(
            "https://openlibrary.org/api/books",
            ua=ua,
            params={"bibkeys": key, "format": "json", "jscmd": "data"},
            timeout=10,
            allow_404=True,
        )
        if not data or not isinstance(data, dict) or key not in data:
            return f"no book found for ISBN {strip_ctrl(n, 30)}"
        book = data[key]
        title = strip_ctrl(book.get("title", "(untitled)"), 180)
        authors = ", ".join(
            strip_ctrl(a.get("name", ""), 60)
            for a in (book.get("authors") or [])[:3]
            if a.get("name")
        ) or "?"
        year = strip_ctrl(book.get("publish_date", ""), 20)
        publishers = ", ".join(
            strip_ctrl(p.get("name", ""), 60)
            for p in (book.get("publishers") or [])[:2]
            if p.get("name")
        )
        bits = [f"\x02{title}\x02", authors]
        if year:
            bits.append(year)
        if publishers:
            bits.append(publishers)
        return " :: ".join(bits)
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"isbn lookup: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f"isbn lookup: {e}")
        return "lookup failed"


# ── Stack Overflow ────────────────────────────────────────────────────────
def _so_sync(query: str, ua: str) -> str:
    try:
        data = fetch_json(
            "https://api.stackexchange.com/2.3/search/advanced",
            ua=ua,
            params={
                "order": "desc",
                "sort": "relevance",
                "site": "stackoverflow",
                "q": query.strip(),
            },
            timeout=10,
        )
        if not data or not isinstance(data, dict):
            return "lookup failed"
        items = data.get("items") or []
        if not items:
            return f"no Stack Overflow results for '{strip_ctrl(query, 60)}'"
        q = items[0]
        title = strip_ctrl(q.get("title", "(untitled)"), 200)
        score = q.get("score", 0)
        answered = "answered" if q.get("is_answered") else "unanswered"
        link = strip_ctrl(q.get("link", ""), 200)
        return f"\x02{title}\x02 :: score {score} :: {answered} :: {link}"
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"so lookup: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f"so lookup: {e}")
        return "lookup failed"


# ── RFC ───────────────────────────────────────────────────────────────────
def _rfc_by_number(n: int, ua: str) -> str | None:
    """Format one RFC from the rfc-editor JSON, or None if it does not exist."""
    data = fetch_json(
        f"https://www.rfc-editor.org/rfc/rfc{n}.json",
        ua=ua, timeout=10, allow_404=True,
    )
    if not data or not isinstance(data, dict):
        return None
    title = strip_ctrl(data.get("title", "(untitled)"), 200)
    status = strip_ctrl(data.get("status", ""), 60)
    month = strip_ctrl(data.get("month", ""), 20)
    year = strip_ctrl(str(data.get("year", "")), 10)
    date = " ".join(p for p in (month, year) if p)
    bits = [f"\x02RFC {n}\x02: {title}"]
    if status:
        bits.append(status)
    if date:
        bits.append(date)
    return " :: ".join(bits)


def _rfc_search_number(query: str, ua: str) -> int | None:
    """Resolve a title/keyword to the best-matching RFC number via the IETF
    datatracker, or None.  Mirrors the .wiki search fallback: rank an exact
    title match over a prefix match over a substring match, then shorter title."""
    data = fetch_json(
        "https://datatracker.ietf.org/api/v1/doc/document/",
        params={"name__startswith": "rfc", "title__icontains": query,
                "format": "json", "limit": "20"},
        ua=ua, timeout=12, allow_404=True,
    )
    objs = data.get("objects", []) if isinstance(data, dict) else []
    ql = query.strip().lower()
    best = None  # (rank, number)
    for o in objs:
        num = o.get("rfc")
        if not num:
            nm = o.get("name", "")
            num = int(nm[3:]) if nm.startswith("rfc") and nm[3:].isdigit() else None
        if not num:
            continue
        tl = (o.get("title") or "").strip().lower()
        rank = (tl == ql, tl.startswith(ql), ql in tl, -len(tl))
        if best is None or rank > best[0]:
            best = (rank, int(num))
    return best[1] if best else None


def _rfc_sync(arg: str, ua: str) -> str:
    try:
        a = arg.strip()
        if a.isdigit():
            return _rfc_by_number(int(a), ua) or f"no RFC {strip_ctrl(a, 20)}"
        num = _rfc_search_number(a, ua)
        if num is None:
            return f"no RFC matching '{strip_ctrl(arg, 60)}'"
        return _rfc_by_number(num, ua) or f"no RFC matching '{strip_ctrl(arg, 60)}'"
    except (ResponseTooLarge, KeyError, ValueError, TypeError) as e:
        log.warning(f"rfc lookup: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f"rfc lookup: {e}")
        return "lookup failed"


# ── rtfm (tldr-pages: Unix / BSD / Linux command reference) ────────────────
_TLDR_BASE = "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages"
_TLDR_PLATFORMS = ("common", "linux", "osx", "freebsd", "openbsd", "netbsd")
_RTFM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]*$")


def _http_text(url: str, ua: str, max_bytes: int = 65536) -> str | None:
    """Size-capped raw text GET for a trusted host; None on 404."""
    import requests  # noqa: PLC0415 — lazy import matches base.fetch_json
    with requests.get(url, headers={"User-Agent": ua}, timeout=10, stream=True) as r:
        if r.status_code == 404:
            return None
        r.raise_for_status()
        raw = r.raw.read(max_bytes + 1, decode_content=True)
        if len(raw) > max_bytes:
            raise ResponseTooLarge("tldr page too large")
        return raw.decode("utf-8", "replace")


def _rtfm_sync(query: str, ua: str) -> str:
    """tldr-pages summary for a command (host is hardcoded/trusted; only the
    command name is user input and is whitelisted to a safe charset)."""
    try:
        name = query.strip().lower().replace(" ", "-")
        if not _RTFM_NAME_RE.match(name) or len(name) > 40:
            return "usage: .rtfm <command>  e.g. .rtfm tar"
        text = plat = None
        for p in _TLDR_PLATFORMS:
            raw = _http_text(f"{_TLDR_BASE}/{p}/{name}.md", ua)
            if raw:
                text, plat = raw, p
                break
        if not text:
            return (f"no tldr page for '{strip_ctrl(name, 40)}' "
                    f"(try the full page: man {strip_ctrl(name, 40)})")

        def _ub(t: str) -> str:
            return re.sub(r"`([^`]*)`", r"\1", t)

        desc = ""
        examples: list[tuple[str, str]] = []
        pending = None
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(">"):
                d = s.lstrip(">").strip()
                if d and not desc and not d.lower().startswith(("more info", "see also")):
                    desc = d
            elif s.startswith("- "):
                pending = s[2:].strip().rstrip(":")
            elif s.startswith("`") and pending:
                cmd = s.strip("`").strip().replace("{{", "").replace("}}", "")
                examples.append((pending, cmd))
                pending = None
        head = f"\x02{name}\x02 ({plat})"
        if desc:
            head += f": {_ub(desc)}"
        parts = [head] + [f"{_ub(d)}: {c}" for d, c in examples[:3]]
        return strip_ctrl(" :: ".join(parts), 420)
    except (ResponseTooLarge, ValueError, TypeError) as e:
        log.warning(f"rtfm lookup: {e}")
        return "lookup failed"
    except Exception as e:
        log.warning(f"rtfm lookup: {e}")
        return "lookup failed"


# ── arXiv (ATOM XML) ──────────────────────────────────────────────────────
def _arxiv_fetch_xml(query: str, ua: str) -> str:
    """Size-capped raw fetch of the arXiv ATOM feed — returns decoded text.

    Not via fetch_json: the endpoint returns XML, not JSON.  The body is
    capped before buffering so a hostile/huge feed can't OOM the process;
    defusedxml then guards the parse against XXE / billion-laughs.
    """
    import requests  # noqa: PLC0415 — lazy import matches base.fetch_json
    # id_list for a bare arXiv id (e.g. 2101.00001 or hep-th/9901001), else a
    # full-text search.  Heuristic: ids contain a dot+digits or a slash.
    q = query.strip()
    is_id = ("/" in q) or (
        "." in q and q.replace(".", "").replace("v", "").isdigit()
    )
    params = (
        {"id_list": q, "max_results": "1"}
        if is_id
        else {"search_query": f"all:{q}", "max_results": "1"}
    )
    with requests.get(
        "http://export.arxiv.org/api/query",
        params=params,
        headers={"User-Agent": ua},
        timeout=10,
        stream=True,
    ) as r:
        r.raise_for_status()
        body = r.raw.read(_MAX_XML_BYTES + 1, decode_content=True)
    if len(body) > _MAX_XML_BYTES:
        raise ResponseTooLarge("arXiv response too large")
    return body.decode("utf-8", errors="replace")


def _arxiv_sync(query: str, ua: str, fetch=_arxiv_fetch_xml) -> str:
    try:
        text = fetch(query, ua)
        root = ElementTree.fromstring(text)
        entry = root.find("a:entry", _ATOM_NS)
        if entry is None:
            return f"no arXiv result for '{strip_ctrl(query, 60)}'"
        title = strip_ctrl((entry.findtext("a:title", "", _ATOM_NS) or "").strip(), 200)
        authors = [
            strip_ctrl((a.findtext("a:name", "", _ATOM_NS) or "").strip(), 60)
            for a in entry.findall("a:author", _ATOM_NS)[:3]
        ]
        author_str = ", ".join(a for a in authors if a) or "?"
        published = strip_ctrl((entry.findtext("a:published", "", _ATOM_NS) or "")[:10], 12)
        link = strip_ctrl((entry.findtext("a:id", "", _ATOM_NS) or "").strip(), 200)
        if not title:
            return f"no arXiv result for '{strip_ctrl(query, 60)}'"
        bits = [f"\x02{title}\x02", author_str]
        if published:
            bits.append(published)
        if link:
            bits.append(link)
        return " :: ".join(bits)
    except ResponseTooLarge as e:
        log.warning(f"arxiv lookup: {e}")
        return "lookup failed"
    except ElementTree.ParseError as e:
        log.warning(f"arxiv parse: {e}")
        return "lookup failed"
    except (KeyError, ValueError, TypeError) as e:
        log.warning(f"arxiv lookup: {e}")
        return "lookup failed"
    except Exception as e:  # requests.RequestException
        log.warning(f"arxiv lookup: {e}")
        return "lookup failed"


class RefLookupModule(BotModule):
    """Reference lookups: wiki / doi / isbn / so / rfc / arxiv / element."""

    COMMANDS: dict[str, str] = {
        "wiki": "cmd_wiki",
        "doi": "cmd_doi",
        "isbn": "cmd_isbn",
        "so": "cmd_so",
        "rfc": "cmd_rfc",
        "rtfm": "cmd_rtfm",
        "arxiv": "cmd_arxiv",
        "element": "cmd_element",
    }

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down — try again in a few seconds")
            return False
        return True

    async def cmd_wiki(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}wiki <query>")
            return
        result = await asyncio.to_thread(_wiki_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_doi(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}doi <doi>  e.g. {p}doi 10.1038/nature12373")
            return
        result = await asyncio.to_thread(_doi_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_isbn(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}isbn <isbn>")
            return
        result = await asyncio.to_thread(_isbn_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_so(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}so <query>")
            return
        result = await asyncio.to_thread(_so_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_rfc(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rfc <number|title>  e.g. {p}rfc 2616  {p}rfc hypertext transfer protocol")
            return
        result = await asyncio.to_thread(_rfc_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_arxiv(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}arxiv <id|query>")
            return
        result = await asyncio.to_thread(_arxiv_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    async def cmd_element(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}element <name|symbol|Z>")
            return
        self.bot.privmsg(reply_to, strip_ctrl(element_lookup(arg[:_MAX_INPUT])))

    async def cmd_rtfm(self, nick: str, reply_to: str, arg: str | None) -> None:
        if not self._gate(nick):
            return
        if not arg or not arg.strip():
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}rtfm <command>  e.g. {p}rtfm tar")
            return
        result = await asyncio.to_thread(_rtfm_sync, arg[:_MAX_INPUT], self._ua)
        self.bot.privmsg(reply_to, result)

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "wiki <query>", "Wikipedia summary + link"),
            help_row(prefix, "doi <doi>", "Crossref work metadata"),
            help_row(prefix, "isbn <isbn>", "Open Library book lookup"),
            help_row(prefix, "so <query>", "Top Stack Overflow question"),
            help_row(prefix, "rfc <number|title>", "RFC by number or title search"),
            help_row(prefix, "rtfm <command>", "Unix/Linux/BSD command reference (tldr)"),
            help_row(prefix, "arxiv <id|query>", "arXiv paper lookup"),
            help_row(prefix, "element <name|symbol|Z>", "Periodic-table entry (offline)"),
        ]


def setup(bot: object) -> RefLookupModule:
    return RefLookupModule(bot)  # type: ignore[arg-type]
