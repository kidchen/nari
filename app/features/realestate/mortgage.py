"""Module B (part 2): Freddie Mac PMMS mortgage rates via FRED's public CSV (no API key)."""
from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
LABELS = {"MORTGAGE30US": "30-yr fixed", "MORTGAGE15US": "15-yr fixed"}


@dataclass(frozen=True)
class RatePoint:
    series: str
    date: str
    rate: float
    prev_rate: float | None
    year_ago_rate: float | None


def parse_fred_csv(series: str, text: str) -> RatePoint:
    rows = [r for r in csv.reader(io.StringIO(text))][1:]
    vals = [(d, float(v)) for d, v in rows if v not in ("", ".")]
    if not vals:
        raise ValueError(f"No data for {series}")
    date, rate = vals[-1]
    prev = vals[-2][1] if len(vals) > 1 else None
    year_ago = vals[-53][1] if len(vals) > 53 else None  # weekly series
    return RatePoint(series, date, rate, prev, year_ago)


def _default_fetch(url: str) -> str:
    import urllib.request

    # stdlib HTTP/1.1 client: FRED resets curl_cffi's HTTP/2 streams
    req = urllib.request.Request(url, headers={"User-Agent": "nari-digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def fetch_rates(series_list: list[str], fetch: Callable[[str], str] = _default_fetch) -> list[RatePoint]:
    return [parse_fred_csv(s, fetch(FRED_CSV.format(series=s))) for s in series_list]


def format_rates(points: list[RatePoint]) -> str:
    lines = ["🏦 <b>Mortgage rates</b> (Freddie Mac weekly survey)"]
    for p in points:
        parts = [f"{LABELS.get(p.series, p.series)}: <b>{p.rate:.2f}%</b>"]
        if p.prev_rate is not None:
            parts.append(f"wk {p.rate - p.prev_rate:+.2f}")
        if p.year_ago_rate is not None:
            parts.append(f"yr {p.rate - p.year_ago_rate:+.2f}")
        lines.append("• " + " · ".join(parts) + f" <i>(as of {p.date})</i>")
    return "\n".join(lines)
