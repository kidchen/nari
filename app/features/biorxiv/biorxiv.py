"""bioRxiv paper digest: new preprints matching a person's categories + keywords, summarized by Gemini.

Source: the bioRxiv JSON API (api.biorxiv.org/details/biorxiv/<from>/<to>/<cursor>), which returns every
paper posted in a date range with abstract, category, version and DOI, 30 per page. (The per-subject RSS
feeds only hold the latest 30 papers, which busy categories like neuroscience exceed in under a day.)
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

log = logging.getLogger(__name__)
API = "https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}"
MAX_PAGES = 400                # safety only: a 7-day catch-up is ~60 pages, 14 days ~120
SUMMARY_BATCH = 8              # papers per Gemini call


@dataclass
class Paper:
    doi: str
    title: str
    authors: str
    abstract: str
    category: str
    posted: str
    version: int = 1
    matched: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.biorxiv.org/content/{self.doi}v{self.version}"

    @property
    def first_author(self) -> str:
        names = [a.strip() for a in self.authors.split(";") if a.strip()]
        return (names[0] + (" et al." if len(names) > 1 else "")) if names else ""


def norm_category(c: str) -> str:
    return re.sub(r"[\s_]+", " ", c.strip().lower())


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-‐–—_/]", " ", s.lower()))


def label(keyword: str) -> str:
    """'zebrafish|danio' -> 'zebrafish' (the first spelling is the display name)."""
    return keyword.split("|", 1)[0].strip()


def match_keywords(p: Paper, keywords: list[str]) -> list[str]:
    """Labels of keywords found in title or abstract. A keyword may list alternative spellings separated by
    '|'; matching is case-insensitive, 'cell-cycle' == 'cell cycle', and a plural 's' is allowed ('NPCs')."""
    text = _norm_text(f"{p.title} {p.abstract}")
    hits = []
    for kw in keywords:
        for alt in kw.split("|"):
            k = _norm_text(alt).strip()
            if k and re.search(rf"(?<![a-z0-9]){re.escape(k)}s?(?![a-z0-9])", text):
                hits.append(label(kw))
                break
    return hits


def _get(url: str, timeout: float = 30, tries: int = 3) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nari-biorxiv/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            log.info("bioRxiv API retry after %s", e)
            time.sleep(3 * (i + 1))
    return {}


def fetch_papers(start: date, end: date, categories: list[str], include_revisions: bool = False,
                 get: Callable[[str], dict] = _get) -> list[Paper]:
    """All papers posted in [start, end] in the given categories (all if empty)."""
    cats = {norm_category(c) for c in categories}
    out, cursor = [], 0
    for _ in range(MAX_PAGES):
        data = get(API.format(start=start.isoformat(), end=end.isoformat(), cursor=cursor))
        coll = data.get("collection") or []
        for d in coll:
            if cats and norm_category(d.get("category", "")) not in cats:
                continue
            version = int(d.get("version") or 1)
            if version > 1 and not include_revisions:
                continue
            out.append(Paper(d["doi"], d.get("title", "").strip(), d.get("authors", ""), d.get("abstract", "").strip(),
                             d.get("category", ""), d.get("date", ""), version))
        msg = (data.get("messages") or [{}])[0]
        total = int(msg.get("total") or 0)
        cursor += int(msg.get("count") or len(coll))
        if not coll or cursor >= total:
            break
    else:
        log.warning("bioRxiv fetch hit the %d-page cap (%s..%s); newest papers may be missing", MAX_PAGES, start, end)
    seen, uniq = set(), []
    for p in out:                       # API can list a DOI more than once
        if p.doi not in seen:
            seen.add(p.doi)
            uniq.append(p)
    return uniq


def score(p: Paper, keywords: list[str]) -> int:
    """Keyword order = priority: the first keyword weighs most (n), the last least (1)."""
    labels = [label(k) for k in keywords]
    n = len(labels)
    return sum(n - labels.index(k) for k in p.matched if k in labels)


def select(papers: list[Paper], keywords: list[str], already: set[str],
           context: list[str] | None = None) -> list[Paper]:
    """Matching papers not sent before, highest priority score first (keyword order in the config).
    If `context` is set, a paper must ALSO mention at least one context term (e.g. zebrafish / danio):
    keeps ambiguous acronyms and off-focus uses of broad keywords out of a focused feed."""
    picked = []
    for p in papers:
        if p.doi in already:
            continue
        if context and not match_keywords(p, context):
            continue
        if hits := match_keywords(p, keywords):
            p.matched = hits
            picked.append(p)
    picked.sort(key=lambda p: p.posted, reverse=True)           # newest first ...
    return sorted(picked, key=lambda p: -score(p, keywords))   # ... within the same priority score


# ---- Gemini ---------------------------------------------------------------------------------

PROMPT = """You summarize biology preprints for a researcher. For EACH paper below:
Summarize this biology preprint into 3 concise bullet points for a researcher. Focus on:
1. Core question/goal, 2. Main methodology/technology, 3. Key finding. Keep it brief.

Rules: start the bullets with "Goal: ", "Method: ", "Finding: " respectively; use only the title and abstract
given; each bullet <= 25 words. Return ONLY JSON:
{"papers": [{"id": "<paper id>", "bullets": ["Goal: ...", "Method: ...", "Finding: ..."]}]}

"""


def build_prompt(papers: list[Paper]) -> str:
    parts = [PROMPT]
    for p in papers:
        parts.append(f"### id: {p.doi}\nTitle: {p.title}\nAuthors: {p.authors[:300]}\nAbstract: {p.abstract[:2500]}\n")
    return "\n".join(parts)


def parse_summaries(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip())
    except json.JSONDecodeError:
        log.warning("bioRxiv summary: non-JSON reply")
        return {}
    out = {}
    for item in data.get("papers", []) if isinstance(data, dict) else []:
        bullets = [str(b).strip() for b in item.get("bullets") or [] if str(b).strip()]
        if item.get("id") and bullets:
            out[str(item["id"]).strip()] = bullets[:3]
    return out


def summarize(papers: list[Paper], llm: Callable[[str], str | None] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not llm:
        return out
    for i in range(0, len(papers), SUMMARY_BATCH):
        out.update(parse_summaries(llm(build_prompt(papers[i:i + SUMMARY_BATCH]))))
    return out


# ---- formatting -----------------------------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def format_digest(papers: list[Paper], summaries: dict[str, list[str]], extra: list[Paper],
                  start: date, end: date) -> str:
    span = f"{start:%m/%d}" if start == end else f"{start:%m/%d}–{end:%m/%d}"
    n = len(papers) + len(extra)
    lines = [f"📚 <b>New on bioRxiv</b> · {span} · {n} paper{'s' if n != 1 else ''}"]
    for i, p in enumerate(papers, 1):
        lines += ["", f'<b>{i}. <a href="{html.escape(p.url)}">{_esc(p.title)}</a></b>',
                  f"<i>{_esc(p.first_author)} · {_esc(p.category)} · matched: {_esc(', '.join(p.matched))}</i>"]
        if bullets := summaries.get(p.doi):
            lines += [f"• {_esc(b)}" for b in bullets]
        else:
            lines.append(_esc(p.abstract[:280].rsplit(" ", 1)[0] + "…"))
    if extra:
        lines += ["", f"<b>More matches ({len(extra)})</b>"]
        lines += [f'• <a href="{html.escape(p.url)}">{_esc(p.title)}</a> <i>({_esc(", ".join(p.matched))})</i>'
                  for p in extra]
    if papers and not summaries:
        lines += ["", "<i>(AI summaries unavailable; showing the start of each abstract.)</i>"]
    return "\n".join(lines)


def format_recap(sent: list[tuple[str, str]], near: list[tuple[Paper, list[str]]], days: int, quiet_days: int,
                 keywords: list[str], context: list[str]) -> str:
    """After a quiet week: what was sent in the window (title, url) + near misses (matched a keyword but
    no context term), or a plain 'nothing matched' so the person knows the digest still works."""
    if not sent and not near:
        lines = [f"📚 <b>bioRxiv</b>: nothing matched your topics in the past {days} days.",
                 "<i>Keywords:</i> " + _esc(", ".join(label(k) for k in keywords))]
        if context:
            lines.append("<i>Must also mention:</i> " + _esc(", ".join(label(k) for k in context)))
        lines.append("<i>Widen it with /papers add &lt;term&gt;.</i>")
        return "\n".join(lines)
    lines = [f"📚 <b>bioRxiv recap</b> · past {days} days",
             f"<i>Nothing new matched in {quiet_days} days, so here's the recent picture.</i>"]
    if sent:
        lines += ["", f"<b>Sent to you ({len(sent)})</b>"]
        lines += [f'• <a href="{html.escape(url)}">{_esc(title)}</a>' if url else f"• {_esc(title)}" for title, url in sent]
    if near:
        lines += ["", f"<b>Near misses ({len(near)})</b> <i>— matched a keyword, but none of: "
                  f"{_esc(', '.join(label(k) for k in context))}</i>"]
        lines += [f'• <a href="{html.escape(p.url)}">{_esc(p.title)}</a> <i>({_esc(", ".join(hits))})</i>' for p, hits in near]
    return "\n".join(lines)
