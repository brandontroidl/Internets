"""Scholarly search - papers, dissertations, and researchers.  All KEYLESS.

    .papers <orcid|query>    works from OpenAlex - by author ORCID iD (bare or
                             orcid.org URL) or free-text topic search.
    .thesis <query>          dissertations/theses from OpenAlex by topic.
    .scholar <name|topic>    researchers via ORCID expanded search - name,
                             institutions, and their ORCID iD (feed it back
                             into .papers).

The ``-oa`` flag (positional, anywhere) restricts .papers/.thesis to
open-access works.  Result links prefer the open-access PDF, then the OA
landing page, then the DOI.  Every outbound call goes through
base.fetch_json (size-capped); both endpoints are keyless public APIs.
"""
from __future__ import annotations

import asyncio
import logging
import re

from .base import BotModule, cred, fetch_json, help_row, strip_ctrl

log = logging.getLogger("internets.scholar")

_MAX_INPUT = 200
_N_WORKS = 5
_N_PEOPLE = 3

_OPENALEX_WORKS = "https://api.openalex.org/works"
_ORCID_SEARCH = "https://pub.orcid.org/v3.0/expanded-search/"

# Trim OpenAlex work records to what the output needs; a full record carries
# abstracts + reference lists and 5 of them would blow the fetch_json cap.
_WORK_FIELDS = ("display_name,publication_year,doi,open_access,"
                "best_oa_location,authorships,primary_location")

_ORCID_RE = re.compile(
    r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])$")


def parse_orcid(s: str) -> str | None:
    """Return the bare ORCID iD from a bare iD or orcid.org URL, else None."""
    m = _ORCID_RE.match((s or "").strip())
    return m.group(1).upper() if m else None


def split_flags(arg: str) -> tuple[str, set[str]]:
    """Split ``-flag`` words (positional, anywhere) out of the query text."""
    words = (arg or "").split()
    flags = {w[1:].lower() for w in words if w.startswith("-") and len(w) > 1}
    query = " ".join(w for w in words if not w.startswith("-"))
    return query, flags


def _work_link(w: dict) -> str:
    """Best link for a work: OA PDF > OA landing page > DOI > OpenAlex id."""
    oa = w.get("best_oa_location") or {}
    return (oa.get("pdf_url") or oa.get("landing_page_url")
            or w.get("doi") or w.get("id") or "")


def _format_work(i: int, w: dict) -> str:
    title = strip_ctrl(w.get("display_name") or "(untitled)", 130)
    year = w.get("publication_year") or "?"
    auths = w.get("authorships") or []
    first = ((auths[0].get("author") or {}).get("display_name")
             if auths else "") or "?"
    author = strip_ctrl(first, 40) + (" et al." if len(auths) > 1 else "")
    src = ((w.get("primary_location") or {}).get("source") or {})
    venue = strip_ctrl(src.get("display_name") or "", 50)
    oa_mark = " [OA]" if (w.get("open_access") or {}).get("is_oa") else ""
    bits = [f"  {i}. \x02{title}\x02 ({year}){oa_mark}", author]
    if venue:
        bits.append(venue)
    link = strip_ctrl(_work_link(w), 200)
    if link:
        bits.append(link)
    return " :: ".join(bits)


def _works_query(arg: str) -> tuple[dict, str]:
    """Build OpenAlex query params + a header label from a .papers arg."""
    query, flags = split_flags(arg)
    params: dict = {"per-page": str(_N_WORKS), "select": _WORK_FIELDS}
    filters: list[str] = []
    orcid = parse_orcid(query)
    if orcid:
        filters.append(f"author.orcid:{orcid}")
        params["sort"] = "publication_date:desc"
        label = f"ORCID {orcid}"
    else:
        # `search=` (not filter=default.search) so commas in the query can't
        # be parsed as filter separators.
        params["search"] = query
        label = f"'{strip_ctrl(query, 60)}'"
    if "oa" in flags:
        filters.append("open_access.is_oa:true")
        label += " [open access]"
    if filters:
        params["filter"] = ",".join(filters)
    return params, label


def _run_works(params: dict, label: str, ua: str) -> list[str]:
    data = fetch_json(_OPENALEX_WORKS, ua=ua, params=params, timeout=10)
    if not isinstance(data, dict):
        return ["lookup failed"]
    results = data.get("results") or []
    if not results:
        return [f"no results for {label}"]
    count = (data.get("meta") or {}).get("count", len(results))
    lines = [f":: {label} - {count} works, top {len(results)} ::"]
    lines += [_format_work(i, w) for i, w in enumerate(results, 1)]
    return lines


def _papers_sync(arg: str, ua: str) -> list[str]:
    try:
        params, label = _works_query(arg)
        return _run_works(params, label, ua)
    except Exception as e:  # noqa: BLE001 - requests/json/size-cap errors
        log.warning(f"papers lookup: {e}")
        return ["lookup failed"]


def _thesis_sync(arg: str, ua: str) -> list[str]:
    try:
        query, flags = split_flags(arg)
        filters = ["type:dissertation"]
        if "oa" in flags:
            filters.append("open_access.is_oa:true")
        params = {"per-page": str(_N_WORKS), "select": _WORK_FIELDS,
                  "search": query, "filter": ",".join(filters)}
        label = f"dissertations: '{strip_ctrl(query, 60)}'"
        if "oa" in flags:
            label += " [open access]"
        return _run_works(params, label, ua)
    except Exception as e:  # noqa: BLE001
        log.warning(f"thesis lookup: {e}")
        return ["lookup failed"]


def _scholar_sync(arg: str, ua: str) -> list[str]:
    try:
        data = fetch_json(
            _ORCID_SEARCH, ua=ua,
            params={"q": arg.strip(), "rows": str(_N_PEOPLE)},
            headers={"Accept": "application/json"}, timeout=10)
        if not isinstance(data, dict):
            return ["lookup failed"]
        hits = data.get("expanded-result") or []
        if not hits:
            return [f"no ORCID researchers matching '{strip_ctrl(arg, 60)}'"]
        found = data.get("num-found", len(hits))
        lines = [f":: ORCID researchers matching '{strip_ctrl(arg, 60)}' "
                 f"- {found} found, top {len(hits)} ::"]
        for i, h in enumerate(hits, 1):
            name = strip_ctrl(
                " ".join(p for p in (h.get("given-names"),
                                     h.get("family-names")) if p) or "?", 60)
            inst = strip_ctrl("; ".join((h.get("institution-name") or [])[:2]), 90)
            oid = strip_ctrl(h.get("orcid-id") or "?", 20)
            bits = [f"  {i}. \x02{name}\x02"]
            if inst:
                bits.append(inst)
            bits.append(f"https://orcid.org/{oid}")
            lines.append(" :: ".join(bits))
        return lines
    except Exception as e:  # noqa: BLE001
        log.warning(f"scholar lookup: {e}")
        return ["lookup failed"]


class ScholarModule(BotModule):
    """`.papers` / `.thesis` / `.scholar` - keyless scholarly search."""

    COMMANDS: dict[str, str] = {
        "papers": "cmd_papers",
        "thesis": "cmd_thesis",
        "scholar": "cmd_scholar",
    }

    def on_load(self) -> None:
        self._ua: str = cred(self.bot.cfg, "weather_user_agent",
                             "weather", "user_agent", "Internets/1.0")

    def is_configured(self) -> bool:
        return True

    def _gate(self, nick: str) -> bool:
        if self.bot.rate_limited(nick):
            self.bot.notice(nick, f"{nick}: slow down - try again in a few seconds")
            return False
        return True

    async def _run(self, nick: str, reply_to: str, arg: str | None,
                   worker, usage: str) -> None:
        if not self._gate(nick):
            return
        query, _flags = split_flags(arg or "")
        if not query:
            p = self.bot.cfg["bot"]["command_prefix"]
            self.bot.privmsg(reply_to, f"{nick}: {p}{usage}")
            return
        for line in await asyncio.to_thread(worker, arg[:_MAX_INPUT], self._ua):
            self.bot.privmsg(reply_to, line)

    async def cmd_papers(self, nick: str, reply_to: str, arg: str | None) -> None:
        await self._run(nick, reply_to, arg, _papers_sync,
                        "papers <orcid|query> [-oa]")

    async def cmd_thesis(self, nick: str, reply_to: str, arg: str | None) -> None:
        await self._run(nick, reply_to, arg, _thesis_sync,
                        "thesis <query> [-oa]")

    async def cmd_scholar(self, nick: str, reply_to: str, arg: str | None) -> None:
        await self._run(nick, reply_to, arg, _scholar_sync,
                        "scholar <name|topic>")

    def help_lines(self, prefix: str) -> list[str]:
        return [
            help_row(prefix, "papers <orcid|query> [-oa]",
                     "Papers by ORCID iD or topic (OpenAlex); -oa = open-access only"),
            help_row(prefix, "thesis <query> [-oa]",
                     "Dissertations/theses by topic (OpenAlex); -oa = open-access only"),
            help_row(prefix, "scholar <name|topic>",
                     "Find researchers + ORCID iDs"),
        ]


def setup(bot: object) -> ScholarModule:
    return ScholarModule(bot)  # type: ignore[arg-type]
