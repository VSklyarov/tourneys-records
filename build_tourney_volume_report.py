# -*- coding: utf-8 -*-
"""Pool tourney volume & duration by ChipEV limit keys (last ~2 years) -> HTML."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyodbc
import yaml

HERE = Path(__file__).resolve().parent
PARAMS = Path(r"D:\Job\Projects\HS_site\code\app\config\parameters.yml")
OUT_HTML = HERE / "pool-tourney-volume-record.html"
OUT_CHART_DATA_JS = HERE / "pool-tourney-volume-record-chart-data.js"
FONTS_DIR = HERE.parent / "gipsy_volatility" / "fonts"

# Match build_dashboard_data.py
ROOM = {
    1: "PokerStars",
    5: "Partypoker",
    6: "Winamax",
    7: "GGPoker",
    8: "iPoker",
    9: "Betclic",
    11: "ACR",
    12: "Unibet",
}
RES = {
    0: "COM",
    1: "FR",
    2: "IT",
    3: "ES",
    4: "RUSO",
    5: "NJ",
    6: "BE",
    7: "BG",
    8: "CZ",
    9: "DK",
    10: "EE",
    11: "EU",
    12: "GR",
    13: "PT",
    14: "RO",
    15: "SH",
    16: "UK",
    17: "EPT",
    18: "PA",
}
DROP_PLAYER_IDS = {13036}
DROP_RES_IDS = {19}

YEARS_BACK = 2
REG_FROM = date.today().replace(year=date.today().year - YEARS_BACK)

def _reg_from_effective() -> date:
    import os

    q = os.environ.get("QUICK_TEST_DAYS")
    if q and q.isdigit():
        from datetime import timedelta

        return date.today() - timedelta(days=int(q))
    return REG_FROM

POOL_FILTERS = """
  AND t.table_size = 3
  AND t.is_refunded != 1
  AND t.not_in_pool IS NULL
  AND sn.player_id NOT IN (13036)
  AND td.reservation NOT IN (19)
  AND (
        (t.DateTimeUnRegister IS NULL OR t.unregister_date IS NOT NULL)
        OR t.register_date > t.DateTimeUnRegister
        OR (t.summary_chip_net_showdown IS NOT NULL OR t.summary_chip_net_no_showdown IS NOT NULL)
  )
"""

SQL_TD = """
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT id, poker_room, reservation, buyin_currency, buyin_total_value
FROM tournament_desc WITH (NOLOCK);
"""

SQL_POOLS = """
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SELECT id, name FROM pool WITH (NOLOCK);
"""

SQL_COUNTS_MONTH = f"""
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

SELECT
    sn.player_id,
    t.pool_id AS pool_id,
    td.id AS td_id,
    t.stack_size AS stack,
    COUNT(*) AS n
FROM tournament t WITH (NOLOCK)
INNER JOIN tournament_desc td WITH (NOLOCK) ON t.td_id = td.id
INNER JOIN screen_name sn WITH (NOLOCK) ON t.sn_id = sn.id
WHERE t.register_date >= ?
  AND t.register_date < ?
  AND t.pool_id IS NOT NULL
{POOL_FILTERS}
GROUP BY sn.player_id, t.pool_id, td.id, t.stack_size;
"""

SQL_DURATION_MONTH = f"""
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

SELECT
    td.id AS td_id,
    t.stack_size AS stack,
    COUNT(*) AS n,
    AVG(CAST(DATEDIFF(second, t.register_date, t.unregister_date) AS float)) AS avg_sec,
    STDEV(CAST(DATEDIFF(second, t.register_date, t.unregister_date) AS float)) AS std_sec
FROM tournament t WITH (NOLOCK)
INNER JOIN tournament_desc td WITH (NOLOCK) ON t.td_id = td.id
INNER JOIN screen_name sn WITH (NOLOCK) ON t.sn_id = sn.id
WHERE t.register_date >= ?
  AND t.register_date < ?
{POOL_FILTERS}
  AND t.unregister_date IS NOT NULL
  AND t.unregister_date > t.register_date
GROUP BY td.id, t.stack_size;
"""

SQL_DURATION_BUCKETS_MONTH = f"""
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

SELECT
    td.id AS td_id,
    t.stack_size AS stack,
    (DATEDIFF(minute, t.register_date, t.unregister_date) / 2) * 2 AS dur_min_bin,
    COUNT(*) AS n
FROM tournament t WITH (NOLOCK)
INNER JOIN tournament_desc td WITH (NOLOCK) ON t.td_id = td.id
INNER JOIN screen_name sn WITH (NOLOCK) ON t.sn_id = sn.id
WHERE t.register_date >= ?
  AND t.register_date < ?
{POOL_FILTERS}
  AND t.unregister_date IS NOT NULL
  AND t.unregister_date > t.register_date
GROUP BY td.id, t.stack_size, (DATEDIFF(minute, t.register_date, t.unregister_date) / 2) * 2;
"""

CACHE_COUNTS = HERE / "_cache_player_pool_counts.parquet"
CACHE_LIMITS_JSON = HERE / "_cache_limits.json"


def fmt_stake(v: float) -> str:
    return f"{float(v):g}"


def typ_play(room: int, res: int, stack: float) -> str:
    stack = float(stack)
    if room == 8 and res in (1, 3):
        if stack == 200:
            return "flash"
        if stack == 500:
            return "regular"
    if room == 6:
        return "nitro" if stack == 300 else "regular"
    if room == 12:
        return "flash" if stack == 200 else "regular"
    return "all"


def slabel(cur: int, stake: float) -> str:
    sym = "€" if int(cur) == 2 else "$"
    return sym + fmt_stake(stake)


def rsr_key_from_parts(room: int, res: int, cur: int, stake: float, stack: float) -> str:
    rname = ROOM.get(room, f"Room{room}")
    res_lbl = RES.get(res, f"R{res}")
    tp = typ_play(room, res, stack)
    sl = slabel(cur, stake)
    return f"{rname}|{res_lbl}|{tp}|{sl}"


def attach_rsr(df: pd.DataFrame, td_map: dict[int, dict]) -> pd.DataFrame:
    rows = []
    for t in df.itertuples():
        td = td_map.get(int(t.td_id))
        if not td or td["res"] in DROP_RES_IDS:
            continue
        rsr = rsr_key_from_parts(td["room"], td["res"], td["cur"], td["stake"], float(t.stack))
        rows.append({**t._asdict(), "rsr": rsr, "room": td["room"], "res": td["res"], "cur": td["cur"], "stake": td["stake"]})
    return pd.DataFrame(rows)


def load_td_map(conn) -> dict[int, dict]:
    td = pd.read_sql(SQL_TD, conn)
    out = {}
    for r in td.itertuples():
        out[int(r.id)] = {
            "room": int(r.poker_room),
            "res": int(r.reservation),
            "cur": int(r.buyin_currency),
            "stake": float(r.buyin_total_value),
        }
    return out


def load_pool_names(conn) -> dict[int, str]:
    pools = pd.read_sql(SQL_POOLS, conn)
    return {int(r.id): str(r.name) for r in pools.itertuples()}


def describe_counts(arr: np.ndarray) -> dict:
    if len(arr) == 0:
        return {}
    return {
        "n": int(len(arr)),
        "sum": int(arr.sum()),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "p10": float(np.percentile(arr, 10)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "max": int(arr.max()),
    }


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) < end:
        ms = date(y, m, 1)
        me = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append((max(ms, start), min(me, end)))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def merge_duration_stats(parts: list[pd.DataFrame], td_map: dict[int, dict]) -> pd.DataFrame:
    if not parts:
        return pd.DataFrame()
    df = attach_rsr(pd.concat(parts, ignore_index=True), td_map)
    if df.empty:
        return pd.DataFrame()
    rows = []
    for rsr, sub in df.groupby("rsr"):
        n = sub["n"].to_numpy(dtype=np.float64)
        avg = sub["avg_sec"].to_numpy(dtype=np.float64)
        std = sub["std_sec"].fillna(0).to_numpy(dtype=np.float64)
        total = n.sum()
        if total <= 0:
            continue
        mean = float(np.sum(n * avg) / total)
        var = (
            float(np.sum(n * (std ** 2 + (avg - mean) ** 2)) / max(total - 1, 1))
            if total > 1
            else 0.0
        )
        row = sub.iloc[0].to_dict()
        row["rsr"] = rsr
        row["n"] = int(total)
        row["avg_sec"] = mean
        row["std_sec"] = math.sqrt(max(var, 0))
        rows.append(row)
    return pd.DataFrame(rows)


def merge_duration_buckets(parts: list[pd.DataFrame], td_map: dict[int, dict]) -> dict[str, list[dict]]:
    if not parts:
        return {}
    df = attach_rsr(pd.concat(parts, ignore_index=True), td_map)
    if df.empty:
        return {}
    merged: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
    for t in df.itertuples():
        merged[t.rsr][float(t.dur_min_bin)] += int(t.n)
    out: dict[str, list[dict]] = {}
    for rsr, bins in merged.items():
        out[rsr] = [
            {"lo": b, "hi": b + 2, "count": c}
            for b, c in sorted(bins.items())
        ]
    return out


def percentile_buckets(buckets: list[dict], pct: float) -> float | None:
    if not buckets:
        return None
    total = sum(b["count"] for b in buckets)
    if total <= 0:
        return None
    target = total * pct / 100.0
    acc = 0
    for b in buckets:
        acc += b["count"]
        if acc >= target:
            return (b["lo"] + b["hi"]) / 2.0
    return buckets[-1]["hi"]


def median_from_buckets(buckets: list[dict]) -> float | None:
    return percentile_buckets(buckets, 50)


def thin_buckets(buckets: list[dict], max_bins: int = 36) -> list[dict]:
    if len(buckets) <= max_bins:
        return buckets
    step = max(2, int(math.ceil(len(buckets) / max_bins)))
    out: list[dict] = []
    i = 0
    while i < len(buckets):
        chunk = buckets[i : i + step]
        out.append(
            {
                "lo": chunk[0]["lo"],
                "hi": chunk[-1]["hi"],
                "count": sum(c["count"] for c in chunk),
            }
        )
        i += step
    return out


def _nice_step(raw: float) -> float:
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    frac = raw / base
    if frac < 1.5:
        nice = 1.0
    elif frac < 3.5:
        nice = 2.0
    elif frac < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    return nice * base


def _round_preserve(counts: list[float]) -> list[int]:
    if not counts:
        return []
    floors = [math.floor(c) for c in counts]
    leftover = int(round(sum(counts))) - sum(floors)
    order = sorted(range(len(counts)), key=lambda i: (counts[i] - floors[i]), reverse=True)
    for i in range(max(0, leftover)):
        floors[order[i % len(order)]] += 1
    return floors


def _overlap_count(rows: list[dict], lo: float, hi: float) -> float:
    total = 0.0
    for b in rows:
        span = float(b["hi"]) - float(b["lo"])
        olo = max(float(b["lo"]), lo)
        ohi = min(float(b["hi"]), hi)
        if ohi > olo and span > 0:
            total += float(b["count"]) * (ohi - olo) / span
    return total


def _tail_edges(lo: float, hi: float, n: int) -> list[float]:
    if hi <= lo:
        return [lo, lo + 1.0]
    n = max(1, n)
    if n == 1 or lo <= 0 or hi / lo < 1.4:
        if n == 1:
            return [lo, hi]
        step = (hi - lo) / n
        return [lo + step * i for i in range(n)] + [hi]
    ratio = (hi / lo) ** (1.0 / n)
    if ratio < 1.25:
        return [lo, hi]
    edges = [lo]
    x = lo
    for _ in range(n - 1):
        x *= ratio
        edges.append(x)
    edges.append(hi)
    return edges


def adaptive_hist_from_bins(buckets: list[dict], mass: float = 0.98, target_bins: int = 28, max_tail: int = 5) -> list[list]:
    """Fine equal bins through the dense part, then a few wider tail bins.

    Each row is [lo, hi, count, tail]. Tail bars are drawn at the same
    on-screen width as body bars so a handful of multi-hour outliers
    cannot stretch the axis.
    """
    rows = [
        {"lo": float(b["lo"]), "hi": float(b["hi"]), "count": float(b["count"])}
        for b in buckets
        if float(b.get("count", 0)) > 0 and float(b["hi"]) > float(b["lo"])
    ]
    rows.sort(key=lambda b: b["lo"])
    if not rows:
        return []
    total = sum(b["count"] for b in rows)
    acc = 0.0
    cut = rows[-1]["hi"]
    for b in rows:
        acc += b["count"]
        if acc >= total * mass:
            cut = b["hi"]
            break
    width0 = min(b["hi"] - b["lo"] for b in rows)
    span = max(cut - min(0.0, rows[0]["lo"]), width0)
    step = max(_nice_step(span / target_bins), width0)
    body_hi = math.ceil(max(cut, step) / step) * step
    edges = [0.0]
    x = 0.0
    while x < body_hi - 1e-9:
        x = min(body_hi, x + step)
        edges.append(x)
    raw_counts = [_overlap_count(rows, edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    counts = _round_preserve(raw_counts)
    out = [[edges[i], edges[i + 1], counts[i], 0] for i in range(len(counts))]
    hi_max = rows[-1]["hi"]
    if hi_max > body_hi + 1e-6:
        n_tail = min(max_tail, sum(1 for b in rows if b["hi"] > body_hi))
        t_edges = _tail_edges(body_hi, hi_max, max(1, n_tail))
        raw_tail = [_overlap_count(rows, t_edges[i], t_edges[i + 1]) for i in range(len(t_edges) - 1)]
        tail_counts = _round_preserve(raw_tail)
        pending = t_edges[0]
        for i, c in enumerate(tail_counts):
            if c <= 0:
                continue
            out.append([pending, t_edges[i + 1], c, 1])
            pending = t_edges[i + 1]
    return out


def adaptive_hist_from_values(values: np.ndarray, mass: float = 0.98, target_bins: int = 28, max_tail: int = 5) -> list[list]:
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v) & (v >= 0)]
    if len(v) < 2:
        return []
    cut = float(np.quantile(v, mass))
    cut = max(cut, float(np.min(v)))
    span = max(cut, 1.0)
    step = max(_nice_step(span / target_bins), 1.0)
    body_hi = math.ceil(max(cut, step) / step) * step
    edges = np.arange(0.0, body_hi + step * 0.5, step)
    if len(edges) < 2:
        edges = np.array([0.0, body_hi])
    if edges[-1] < body_hi:
        edges = np.append(edges, body_hi)
    body = v[v <= body_hi]
    counts, edges = np.histogram(body, bins=edges)
    out = [[float(edges[i]), float(edges[i + 1]), int(counts[i]), 0] for i in range(len(counts))]
    tail_vals = v[v > body_hi]
    if len(tail_vals):
        hi_max = float(tail_vals.max())
        n_tail = min(max_tail, len(tail_vals))
        t_edges = _tail_edges(body_hi, hi_max, max(1, n_tail))
        # right edge inclusive
        t_edges[-1] = max(t_edges[-1], hi_max)
        t_counts, t_edges_arr = np.histogram(tail_vals, bins=np.asarray(t_edges, dtype=np.float64))
        pending = float(t_edges_arr[0])
        for i, c in enumerate(t_counts):
            if int(c) <= 0:
                continue
            out.append([pending, float(t_edges_arr[i + 1]), int(c), 1])
            pending = float(t_edges_arr[i + 1])
    return out


def histogram(arr: np.ndarray, bins: int = 24) -> list[dict]:
    if len(arr) == 0:
        return []
    lo, hi = int(arr.min()), int(arr.max())
    if lo == hi:
        return [{"lo": lo, "hi": hi + 1, "count": int(len(arr))}]
    edges = np.linspace(lo, hi + 1, bins + 1, dtype=int)
    edges = np.unique(edges)
    if len(edges) < 2:
        edges = np.array([lo, hi + 1])
    hist, edges = np.histogram(arr, bins=edges)
    out = []
    for i, c in enumerate(hist):
        if c <= 0:
            continue
        out.append({"lo": int(edges[i]), "hi": int(edges[i + 1]), "count": int(c)})
    return out


def kde_1d_sample(values: np.ndarray, n_grid: int = 96, max_samples: int = 8000) -> list[list[float]]:
    """Gaussian KDE (Silverman bandwidth), for chart tooltip + axes."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return []
    if len(v) > max_samples:
        rng = np.random.default_rng(42)
        v = rng.choice(v, size=max_samples, replace=False)
    n = len(v)
    lo, hi = max(0.0, float(np.min(v))), max(0.0, float(np.max(v)))
    hi = max(hi, lo)
    span = max(hi - lo, 1e-9)
    pad = 0.05 * span
    grid = np.linspace(lo, hi + pad, n_grid)
    sigma = float(np.std(v, ddof=1)) if n > 1 else span / 2
    if sigma <= 0:
        sigma = max(span / 4.0, 1e-6)
    h = 1.06 * sigma * (n ** (-0.2))
    if h <= 0:
        h = max(span / 25.0, 1e-6)
    inv = 1.0 / (math.sqrt(2 * math.pi) * h * n)
    z = (grid[:, None] - v[None, :]) / h
    dens = inv * np.exp(-0.5 * z * z).sum(axis=1)
    return [[float(x), float(y)] for x, y in zip(grid, dens)]


def kde_from_histogram_bins(buckets: list[dict], n_grid: int = 96) -> list[list[float]]:
    """KDE from coarse bins (duration): bin centers weighted by count."""
    if not buckets:
        return []
    centers = []
    weights = []
    for b in buckets:
        c = int(b.get("count", 0))
        if c <= 0:
            continue
        lo = float(b["lo"])
        hi = float(b["hi"]) if b.get("hi") is not None else lo + 2.0
        w = max(hi - lo, 1e-6)
        centers.append((lo + hi) / 2.0)
        weights.append(float(c))
    if len(centers) < 2:
        return []
    w = np.asarray(weights, dtype=np.float64)
    w /= w.sum()
    pts = np.asarray(centers, dtype=np.float64)
    n = len(pts)
    lo, hi = max(0.0, float(pts.min())), max(0.0, float(pts.max()))
    hi = max(hi, lo)
    span = max(hi - lo, 1e-9)
    pad = 0.08 * span
    grid = np.linspace(lo, hi + pad, n_grid)
    sigma = float(np.sqrt(np.sum(w * (pts - np.sum(w * pts)) ** 2)))
    if sigma <= 0:
        sigma = span / max(n, 3)
    h = max(1.06 * sigma * (n ** (-0.2)), span / 80.0, 1e-6)
    inv = 1.0 / (math.sqrt(2 * math.pi) * h)
    dens = np.zeros_like(grid)
    for pi, wi in zip(pts, w):
        dens += wi * inv * np.exp(-0.5 * ((grid - pi) / h) ** 2)
    return [[float(x), float(y)] for x, y in zip(grid, dens)]


def duration_from_agg(rsr: str, dur_stats: pd.DataFrame, dur_buckets: dict[str, list[dict]]) -> tuple[dict, list[dict]]:
    buckets = dur_buckets.get(rsr, [])
    row = dur_stats.loc[dur_stats["rsr"] == rsr]
    if row.empty and not buckets:
        return {}, []
    n = int(row["n"].iloc[0]) if not row.empty else sum(b["count"] for b in buckets)
    mean_min = float(row["avg_sec"].iloc[0] / 60.0) if not row.empty else None
    std_min = float(row["std_sec"].iloc[0] / 60.0) if not row.empty else None
    dstats = {
        "n": n,
        "mean_min": mean_min,
        "median_min": median_from_buckets(buckets),
        "std_min": std_min,
        "p10_min": percentile_buckets(buckets, 10),
        "p90_min": percentile_buckets(buckets, 90),
    }
    return dstats, thin_buckets(buckets)


def build_limits(
    counts: pd.DataFrame,
    dur_stats: pd.DataFrame,
    dur_buckets: dict[str, list[dict]],
) -> list[dict]:
    gb_cols = ["rsr", "player_id", "pool_id", "pool_name"]
    if "username" in counts.columns:
        counts = counts.drop(columns=["username"], errors="ignore")
    agg = counts.groupby(gb_cols, dropna=False)["n"].sum().reset_index()

    limits: list[dict] = []
    for rsr, sub in agg.groupby("rsr"):
        parts = rsr.split("|", 3)
        if len(parts) != 4:
            continue
        room, res, typ, sl = parts
        n_arr = sub["n"].to_numpy(dtype=np.int64)
        stats = describe_counts(n_arr)
        if not stats or stats["n"] < 5:
            continue

        top = sub.nlargest(30, "n")
        top_rows = [
            {
                "rank": i + 1,
                "pool_id": int(r.pool_id),
                "pool_name": r.pool_name,
                "n": int(r.n),
            }
            for i, r in enumerate(top.itertuples())
        ]

        raw_dur_buckets = dur_buckets.get(rsr, [])
        dstats, _ = duration_from_agg(rsr, dur_stats, dur_buckets)
        kde_tourney = kde_1d_sample(n_arr.astype(np.float64))
        kde_duration = kde_from_histogram_bins(raw_dur_buckets) if len(raw_dur_buckets) >= 3 else []
        hist_tourney = adaptive_hist_from_values(n_arr.astype(np.float64))
        hist_duration = adaptive_hist_from_bins(raw_dur_buckets) if raw_dur_buckets else []

        limits.append(
            {
                "rsr": rsr,
                "room": room,
                "res": res,
                "typ": typ,
                "stake": sl,
                "stats": stats,
                "kde_tourney": kde_tourney,
                "kde_duration": kde_duration,
                "hist_tourney": hist_tourney,
                "hist_duration": hist_duration,
                "top30": top_rows,
                "duration": dstats,
                "players_pools": int(len(sub)),
            }
        )

    limits.sort(key=lambda x: (-x["stats"]["sum"], x["rsr"]))
    return limits


def load_css() -> str:
    ref = HERE.parent / "gipsy_volatility" / "gipsy-october-weights.html"
    text = ref.read_text(encoding="utf-8")
    start = text.find("<style>")
    end = text.find("</style>")
    css = text[start + 7 : end]
    # Fonts live next to the generated HTML (self-contained for static hosting).
    css = css.replace('url("fonts/', 'url("fonts/')
    extra = """
.filter-toolbar {
  margin-top: 24px;
  padding: 20px 22px;
  background: var(--sheet);
  color: var(--sheet-ink);
  border-radius: 10px;
}
.filter-toolbar h2 { margin: 0 0 12px; font-size: 1.15rem; }
.filter-row {
  display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end;
}
.filter-field { display: flex; flex-direction: column; gap: 6px; min-width: 140px; }
.filter-field label { font-size: 0.78rem; color: var(--sheet-soft); font-weight: 500; }
.filter-field select {
  height: 40px; padding: 0 12px; border-radius: 6px;
  border: 1px solid var(--sheet-line); background: #FFFBF5; color: var(--sheet-ink);
  font: inherit; min-width: 160px;
}
.filter-hint { margin: 12px 0 0; font-size: 0.88rem; color: var(--sheet-soft); max-width: 62ch; }
.stake-empty {
  margin: 28px 0 0;
  padding: 22px;
  background: var(--sheet);
  color: var(--sheet-soft);
  border-radius: 10px;
}
.stake-empty.is-hidden { display: none; }
.limit-grid { display: flex; flex-direction: column; gap: 40px; margin-top: 28px; }
.limit-card { scroll-margin-top: 80px; }
.limit-card.is-hidden { display: none !important; }
.hist-label { font-size: 0.82rem; color: var(--sheet-soft); margin: 4px 0 0; }
.stat-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px 16px; margin: 16px 0 0;
}
.stat-grid div { min-width: 0; }
.stat-grid dt { margin: 0; font-size: 0.78rem; color: var(--sheet-soft); }
.stat-grid dd { margin: 4px 0 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 16px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
.echart-box {
  width: 100%; height: 260px; margin: 12px 0 0;
  border-radius: 8px;
  background: linear-gradient(180deg, rgba(26,21,16,0.06) 0%, rgba(26,21,16,0.02) 100%);
}
"""
    return css + extra


def fmt_num(x, digits=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, int):
        return f"{x:,}".replace(",", " ")
    return f"{x:,.{digits}f}".replace(",", " ")


def render_html(limits: list[dict], meta: dict) -> str:
    css = load_css()
    chart_payload: dict[str, dict] = {}
    for lim in limits:
        sid = lim["rsr"].replace("|", "--")
        chart_payload[sid] = {
            "tourney": lim.get("kde_tourney") or [],
            "duration": lim.get("kde_duration") or [],
            "tourneyHist": lim.get("hist_tourney") or [],
            "durationHist": lim.get("hist_duration") or [],
        }

    cards = []
    for lim in limits:
        sid = lim["rsr"].replace("|", "--")
        st = lim["stats"]
        du = lim.get("duration") or {}
        top_rows = "".join(
            f"<tr><td>{r['rank']}</td><td>{r['pool_name']}</td>"
            f"<td class='num'>{fmt_num(r['n'], 0)}</td></tr>"
            for r in lim["top30"]
        )
        cards.append(
            f"""
<section class="sheet limit-card is-hidden" id="{sid}" data-room="{lim['room']}" data-reservation="{lim['res']}"
         data-discipline="{lim['typ']}" data-stake="{lim['stake']}">
  <div class="sheet-head">
    <h2>{lim['room']} · {lim['res']} · {lim['typ']} · {lim['stake']}</h2>
    <span class="hist-label">{lim['players_pools']} player×pool pairs · {fmt_num(st['sum'], 0)} tournaments total</span>
  </div>
  <div class="two-col">
    <div>
      <h3 style="margin:0;font-size:1rem;">Volume per pool period</h3>
      <p class="hist-label">Distribution across (player, pool) pairs with ≥1 tournament on this stake</p>
      <dl class="stat-grid">
        <div><dt>N</dt><dd>{fmt_num(st['n'], 0)}</dd></div>
        <div><dt>Mean</dt><dd>{fmt_num(st['mean'])}</dd></div>
        <div><dt>Median</dt><dd>{fmt_num(st['median'])}</dd></div>
        <div><dt>σ</dt><dd>{fmt_num(st['std'])}</dd></div>
        <div><dt>P10 / P90</dt><dd>{fmt_num(st['p10'])} / {fmt_num(st['p90'])}</dd></div>
        <div><dt>Max</dt><dd>{fmt_num(st['max'], 0)}</dd></div>
      </dl>
      <div class="echart-box" id="chart-tourney-{sid}" data-chart-role="tourney"></div>
      <p class="hist-label">{'Share of player×pool pairs. Gold bars are equal steps through the dense part; orange bars are a coarser tail, drawn at the same width.' if lim.get('hist_tourney') else 'KDE (Gaussian, Silverman bandwidth) of tournaments per pool'}</p>
    </div>
    <div>
      <h3 style="margin:0;font-size:1rem;">Tournament duration</h3>
      <p class="hist-label">Completed: unregister_date − register_date (minutes)</p>
      <dl class="stat-grid">
        <div><dt>Tournaments</dt><dd>{fmt_num(du.get('n'), 0) if du else '—'}</dd></div>
        <div><dt>Mean</dt><dd>{fmt_num(du.get('mean_min')) if du else '—'} min</dd></div>
        <div><dt>Median</dt><dd>{fmt_num(du.get('median_min')) if du else '—'} min</dd></div>
        <div><dt>σ</dt><dd>{fmt_num(du.get('std_min')) if du else '—'} min</dd></div>
        <div><dt>P10 / P90</dt><dd>{fmt_num(du.get('p10_min')) if du else '—'} / {fmt_num(du.get('p90_min')) if du else '—'} min</dd></div>
      </dl>
      <div class="echart-box" id="chart-dur-{sid}" data-chart-role="duration"></div>
      <p class="hist-label">{'Share of tournaments. Gold bars are equal steps through the dense part; orange bars are a coarser tail, drawn at the same width.' if lim.get('hist_duration') else 'KDE from 2-minute duration bins (approx.) — sparse for very fast formats'}</p>
    </div>
  </div>
  <h3 style="margin:24px 0 8px;font-size:1rem;">Top 30 (single pool period)</h3>
  <div class="table-wrap">
    <table>
      <thead><tr><th>#</th><th>Pool</th><th>Tournaments</th></tr></thead>
      <tbody>{top_rows}</tbody>
    </table>
  </div>
</section>"""
        )

    chart_json_embed = json.dumps(chart_payload, separators=(",", ":"), ensure_ascii=True)
    # Optional: keep a .js copy for debugging (same folder as HTML); page uses embedded JSON so file:// works.
    OUT_CHART_DATA_JS.write_text(
        "window.__POOL_REPORT_CHART_DATA = " + chart_json_embed + ";\n", encoding="utf-8"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pool · volume & duration by stake</title>
<style>{css}
td.num, th {{ text-align: right; }}
td.num {{ font-variant-numeric: tabular-nums; }}
thead th:first-child, tbody td:first-child {{ text-align: left; }}
tbody td:nth-child(2) {{ text-align: left; }}
</style>
</head>
<body>
<header class="rail">
  <h1>Tournaments · records per pool period</h1>
  <span>{meta['reg_from']} — {meta['reg_to']}</span>
</header>
<main>
  <section class="filter-toolbar">
    <h2>Browse stakes ({len(limits)})</h2>
    <div class="filter-row">
      <div class="filter-field">
        <label for="filter-room">Room</label>
        <select id="filter-room" aria-label="Filter by room"></select>
      </div>
      <div class="filter-field">
        <label for="filter-reservation">Reservation</label>
        <select id="filter-reservation" aria-label="Filter by reservation"></select>
      </div>
      <div class="filter-field">
        <label for="filter-discipline">Discipline</label>
        <select id="filter-discipline" aria-label="Filter by discipline"></select>
      </div>
      <div class="filter-field">
        <label for="filter-stake">Stake</label>
        <select id="filter-stake" aria-label="Filter by stake"></select>
      </div>
    </div>
    <p class="filter-hint">Each list only shows values that still exist for the selections above. A list with a single value is filled in automatically. The window opens when one stake remains.</p>
  </section>
  <p class="stake-empty" id="stake-empty">Pick a stake to open its window.</p>
  <div class="limit-grid">{''.join(cards)}</div>
</main>
<script type="application/json" id="pool-report-chart-data">{chart_json_embed}</script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script>
(function () {{
  try {{
    var _jd = document.getElementById("pool-report-chart-data");
    window.__POOL_REPORT_CHART_DATA = _jd ? JSON.parse(_jd.textContent) : {{}};
  }} catch (e) {{
    console.error("pool-report: chart JSON parse failed", e);
    window.__POOL_REPORT_CHART_DATA = {{}};
  }}
  const ACCENT = "#E0B15A";
  const AXIS = "#5E5146";
  const GRID = "#DDD2C3";
  const INK = "#1C1612";
  function prepareKde(series) {{
    if (!series || !series.length) return [];
    const raw = [];
    for (let i = 0; i < series.length; i++) {{
      const x = +series[i][0];
      const y = Math.max(0, +series[i][1]);
      if (x < 0) {{
        const nxt = series[i + 1];
        if (nxt && +nxt[0] > 0) {{
          const x2 = +nxt[0];
          const y2 = Math.max(0, +nxt[1]);
          const t = (0 - x) / (x2 - x);
          raw.push([0, Math.max(0, y + t * (y2 - y))]);
        }}
        continue;
      }}
      if (raw.length && raw[raw.length - 1][0] === x) continue;
      raw.push([x, y]);
    }}
    if (raw.length < 2) return raw.map(function (p) {{ return [p[0], p[1], 0]; }});
    let area = 0;
    for (let i = 1; i < raw.length; i++) {{
      area += (raw[i][0] - raw[i - 1][0]) * (raw[i][1] + raw[i - 1][1]) / 2;
    }}
    const scale = area > 0 ? 1 / area : 1;
    const out = [[raw[0][0], raw[0][1] * scale, 0]];
    let acc = 0;
    for (let i = 1; i < raw.length; i++) {{
      const y0 = raw[i - 1][1] * scale;
      const y1 = raw[i][1] * scale;
      acc += (raw[i][0] - raw[i - 1][0]) * (y0 + y1) / 2;
      out.push([raw[i][0], y1, Math.min(1, acc)]);
    }}
    out[out.length - 1][2] = 1;
    return out;
  }}
  function fmtPct(p) {{
    return (Math.round(Math.max(0, Math.min(1, p)) * 1000) / 10).toFixed(1) + "%";
  }}
  function makeKdeOption(seriesData, xName) {{
    const prepared = prepareKde(seriesData);
    const hasData = prepared.length > 1;
    return {{
      animation: false,
      backgroundColor: "#FFFBF5",
      textStyle: {{ color: INK }},
      grid: {{ left: 56, right: 20, top: 28, bottom: 44, containLabel: false }},
      tooltip: {{
        trigger: "axis",
        formatter: function (p) {{
          const pt = p[0];
          if (!pt || !pt.data) return "";
          const x = pt.data[0];
          const left = pt.data[2] || 0;
          const right = 1 - left;
          return xName + ": <b>" + (Math.round(x * 100) / 100) + "</b><br/>Left " + fmtPct(left) + " · Right " + fmtPct(right);
        }}
      }},
      xAxis: {{
        type: "value",
        min: 0,
        name: xName,
        nameLocation: "middle",
        nameGap: 30,
        nameTextStyle: {{ color: AXIS, fontSize: 11 }},
        axisLine: {{ lineStyle: {{ color: GRID }} }},
        splitLine: {{ lineStyle: {{ color: GRID, opacity: 0.6 }} }},
        axisLabel: {{ color: AXIS }}
      }},
      yAxis: {{
        type: "value",
        name: "Density",
        nameTextStyle: {{ color: AXIS, fontSize: 11 }},
        axisLine: {{ lineStyle: {{ color: GRID }} }},
        splitLine: {{ lineStyle: {{ color: GRID, opacity: 0.6 }} }},
        axisLabel: {{ color: AXIS }},
        scale: true
      }},
      series: hasData ? [{{
        type: "line",
        smooth: true,
        showSymbol: false,
        lineStyle: {{ width: 2, color: ACCENT }},
        areaStyle: {{ color: "rgba(224,177,90,0.22)" }},
        emphasis: {{ focus: "series" }},
        data: prepared
      }}] : []
    }};
  }}
  function fmtInt(n) {{
    return String(Math.round(n)).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, " ");
  }}
  function fmtEdge(x) {{
    if (!isFinite(x)) return "";
    const ax = Math.abs(x);
    if (ax >= 1000) {{
      const v = Math.round(x / 100) / 10;
      return String(v) + "k";
    }}
    if (Math.abs(x - Math.round(x)) < 0.05) return String(Math.round(x));
    return String(Math.round(x * 10) / 10);
  }}
  function makeHistOption(bins, xName, unit) {{
    const total = bins.reduce(function (s, b) {{ return s + b[2]; }}, 0) || 1;
    const fineCount = bins.filter(function (b) {{ return !b[3]; }}).length || bins.length;
    const labelEvery = Math.max(1, Math.ceil(fineCount / 8));
    const labels = [];
    const data = [];
    let cdf = 0;
    let seenTail = false;
    let fineIndex = 0;
    bins.forEach(function (b) {{
      const tail = !!b[3];
      if (tail && !seenTail) {{
        labels.push(" ");
        data.push({{ value: null }});
        seenTail = true;
      }}
      const share = b[2] / total;
      const left = cdf;
      cdf += share;
      const wide = tail && (b[1] - b[0]) > Math.max(b[0], 1);
      let label = "";
      if (tail) label = wide ? fmtEdge(b[0]) + "+" : fmtEdge(b[0]) + "–" + fmtEdge(b[1]);
      else if (fineIndex % labelEvery === 0) label = fmtEdge(b[0]);
      labels.push(label);
      data.push({{
        value: share * 100,
        lo: b[0],
        hi: b[1],
        n: b[2],
        left: left,
        right: Math.max(0, 1 - cdf),
        wide: wide,
        itemStyle: {{ color: tail ? "rgba(240,120,72,0.9)" : "rgba(224,177,90,0.92)" }}
      }});
      if (!tail) fineIndex += 1;
    }});
    return {{
      animation: false,
      backgroundColor: "#FFFBF5",
      textStyle: {{ color: INK }},
      grid: {{ left: 52, right: 12, top: 28, bottom: 48 }},
      tooltip: {{
        trigger: "item",
        formatter: function (p) {{
          const d = p.data;
          if (!d || d.value == null) return "";
          const range = d.wide ? fmtEdge(d.lo) + "+" : fmtEdge(d.lo) + "–" + fmtEdge(d.hi);
          return xName + ": <b>" + range + "</b><br/>"
            + fmtPct(d.value / 100) + " · " + fmtInt(d.n) + " " + unit
            + "<br/>Left " + fmtPct(d.left) + " · Right " + fmtPct(d.right);
        }}
      }},
      xAxis: {{
        type: "category",
        data: labels,
        name: xName,
        nameLocation: "middle",
        nameGap: 32,
        nameTextStyle: {{ color: AXIS, fontSize: 11 }},
        axisLine: {{ lineStyle: {{ color: GRID }} }},
        axisTick: {{ show: false }},
        splitLine: {{ show: false }},
        axisLabel: {{ color: AXIS, interval: 0, hideOverlap: true }}
      }},
      yAxis: {{
        type: "value",
        min: 0,
        name: "%",
        nameTextStyle: {{ color: AXIS, fontSize: 11 }},
        axisLine: {{ lineStyle: {{ color: GRID }} }},
        splitLine: {{ lineStyle: {{ color: GRID, opacity: 0.6 }} }},
        axisLabel: {{ color: AXIS }}
      }},
      series: [{{
        type: "bar",
        barCategoryGap: "28%",
        data: data
      }}]
    }};
  }}
  function renderChartsForVisible() {{
    document.querySelectorAll(".limit-card:not(.is-hidden)").forEach(function (card) {{
      const id = card.id;
      const payload = window.__POOL_REPORT_CHART_DATA[id];
      if (!payload) return;
      const elT = card.querySelector('[data-chart-role="tourney"]');
      const elD = card.querySelector('[data-chart-role="duration"]');
      if (elT && elT.__chart) {{ elT.__chart.dispose(); elT.__chart = null; }}
      if (elD && elD.__chart) {{ elD.__chart.dispose(); elD.__chart = null; }}
      if (elT && typeof echarts !== "undefined") {{
        const c = echarts.init(elT);
        if (payload.tourneyHist && payload.tourneyHist.length > 1) {{
          c.setOption(makeHistOption(payload.tourneyHist, "Tournaments / pool", "pairs"));
        }} else {{
          c.setOption(makeKdeOption(payload.tourney, "Tournaments / pool"));
        }}
        elT.__chart = c;
      }}
      if (elD && typeof echarts !== "undefined") {{
        const c = echarts.init(elD);
        const d = payload.duration;
        if (payload.durationHist && payload.durationHist.length > 1) {{
          c.setOption(makeHistOption(payload.durationHist, "Minutes", "tournaments"));
        }} else if (d && d.length > 1) {{
          c.setOption(makeKdeOption(d, "Minutes"));
        }} else {{
          c.setOption({{
            graphic: [{{
              type: "text",
              left: "center",
              top: "middle",
              style: {{ text: "Not enough data for KDE", fill: AXIS, font: "14px sans-serif" }}
            }}]
          }});
        }}
        elD.__chart = c;
      }}
      requestAnimationFrame(function () {{
        if (elT && elT.__chart) elT.__chart.resize();
        if (elD && elD.__chart) elD.__chart.resize();
      }});
    }});
  }}
  const cards = Array.from(document.querySelectorAll(".limit-card"));
  function uniqSorted(arr) {{
    return Array.from(new Set(arr)).filter(Boolean).sort(function (a, b) {{
      return String(a).localeCompare(String(b));
    }});
  }}
  function stakeAmount(s) {{
    const m = String(s).replace(/\\s/g, "").match(/-?\\d+(?:[.,]\\d+)?/);
    return m ? parseFloat(m[0].replace(",", ".")) : Number.POSITIVE_INFINITY;
  }}
  function sortStakes(arr) {{
    return Array.from(new Set(arr)).filter(Boolean).sort(function (a, b) {{
      const da = stakeAmount(a);
      const db = stakeAmount(b);
      if (da !== db) return da - db;
      return String(a).localeCompare(String(b));
    }});
  }}
  function matchingCards() {{
    const r = document.getElementById("filter-room").value;
    const v = document.getElementById("filter-reservation").value;
    const d = document.getElementById("filter-discipline").value;
    const s = document.getElementById("filter-stake").value;
    return cards.filter(function (el) {{
      if (r && el.dataset.room !== r) return false;
      if (v && el.dataset.reservation !== v) return false;
      if (d && el.dataset.discipline !== d) return false;
      if (s && el.dataset.stake !== s) return false;
      return true;
    }});
  }}
  function fillSelect(sel, values) {{
    const was = sel.value;
    sel.innerHTML = "";
    if (values.length !== 1) {{
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "";
      sel.appendChild(blank);
    }}
    values.forEach(function (v) {{
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      sel.appendChild(o);
    }});
    if (values.length === 1) sel.value = values[0];
    else if (values.indexOf(was) >= 0) sel.value = was;
    else sel.value = "";
  }}
  function coerceFilterSelections() {{
    const roomSel = document.getElementById("filter-room");
    const resSel = document.getElementById("filter-reservation");
    const disSel = document.getElementById("filter-discipline");
    const stSel = document.getElementById("filter-stake");
    for (let i = 0; i < 8; i++) {{
      const r = roomSel.value;
      const v = resSel.value;
      const d = disSel.value;
      const s = stSel.value;
      const rooms = uniqSorted(cards.map(function (c) {{ return c.dataset.room; }}));
      const b0 = r ? cards.filter(function (c) {{ return c.dataset.room === r; }}) : cards;
      const reservations = uniqSorted(b0.map(function (c) {{ return c.dataset.reservation; }}));
      const b1 = v ? b0.filter(function (c) {{ return c.dataset.reservation === v; }}) : b0;
      const disciplines = uniqSorted(b1.map(function (c) {{ return c.dataset.discipline; }}));
      const b2 = d ? b1.filter(function (c) {{ return c.dataset.discipline === d; }}) : b1;
      const stakes = sortStakes(b2.map(function (c) {{ return c.dataset.stake; }}));
      fillSelect(roomSel, rooms);
      fillSelect(resSel, reservations);
      fillSelect(disSel, disciplines);
      fillSelect(stSel, stakes);
      if (roomSel.value !== r || resSel.value !== v || disSel.value !== d || stSel.value !== s) continue;
      break;
    }}
  }}
  function applyFilter() {{
    coerceFilterSelections();
    const matched = matchingCards();
    const only = matched.length === 1 ? matched[0] : null;
    cards.forEach(function (el) {{
      el.classList.toggle("is-hidden", el !== only);
    }});
    const empty = document.getElementById("stake-empty");
    if (empty) {{
      empty.classList.toggle("is-hidden", !!only);
      if (!only) {{
        const r = document.getElementById("filter-room").value;
        const v = document.getElementById("filter-reservation").value;
        const d = document.getElementById("filter-discipline").value;
        const s = document.getElementById("filter-stake").value;
        if (!r && !v && !d && !s) {{
          empty.textContent = "Pick a stake to open its window.";
        }} else if (!matched.length) {{
          empty.textContent = "No stake matches these filters.";
        }} else {{
          empty.textContent = matched.length + " stakes match. Narrow the lists until one remains.";
        }}
      }}
    }}
    if (only) renderChartsForVisible();
  }}
  ["filter-room","filter-reservation","filter-discipline","filter-stake"].forEach(function (id) {{
    document.getElementById(id).addEventListener("change", applyFilter);
  }});
  applyFilter();
  window.addEventListener("resize", function () {{
    document.querySelectorAll(".echart-box").forEach(function (el) {{
      if (el.__chart) el.__chart.resize();
    }});
  }});
}})();
</script>
</body>
</html>"""


def main():
    import os

    if CACHE_LIMITS_JSON.is_file() and os.environ.get("REFRESH_DATA") != "1":
        payload = json.loads(CACHE_LIMITS_JSON.read_text(encoding="utf-8"))
        html = render_html(payload["limits"], payload["meta"])
        OUT_HTML.write_text(html, encoding="utf-8")
        print(f"Wrote {OUT_HTML} from {CACHE_LIMITS_JSON.name} (set REFRESH_DATA=1 to re-query DB)", flush=True)
        return

    p = yaml.safe_load(PARAMS.read_text(encoding="utf-8"))["parameters"]
    cs = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={p['database_host']},{p['database_port']};"
        f"DATABASE={p['database_name']};UID={p['database_user']};"
        f"PWD={p['database_password']};TrustServerCertificate=yes;Encrypt=no"
    )
    reg_from = _reg_from_effective()
    reg_to = date.today()
    print(f"Connecting test DB, register_date >= {reg_from}", flush=True)

    td_map: dict[int, dict] = {}
    pool_names: dict[int, str] = {}
    dur_stat_parts: list[pd.DataFrame] = []
    dur_bucket_parts: list[pd.DataFrame] = []

    if CACHE_COUNTS.is_file() and os.environ.get("REFETCH_COUNTS") != "1":
        print(f"Using cache {CACHE_COUNTS.name}", flush=True)
        counts = pd.read_parquet(CACHE_COUNTS)
        conn = pyodbc.connect(cs, timeout=900)
        td_map = load_td_map(conn)
        for i, (m0, m1) in enumerate(month_ranges(reg_from, reg_to)):
            print(f"  duration month {m0} .. {m1} ({i + 1})", flush=True)
            dur_stat_parts.append(pd.read_sql(SQL_DURATION_MONTH, conn, params=[m0, m1]))
            dur_bucket_parts.append(pd.read_sql(SQL_DURATION_BUCKETS_MONTH, conn, params=[m0, m1]))
        conn.close()
    else:
        conn = pyodbc.connect(cs, timeout=900)
        print("Loading td + pool dims...", flush=True)
        td_map = load_td_map(conn)
        pool_names = load_pool_names(conn)
        count_parts: list[pd.DataFrame] = []
        for i, (m0, m1) in enumerate(month_ranges(reg_from, reg_to)):
            print(f"  month {m0} .. {m1} ({i + 1})", flush=True)
            count_parts.append(pd.read_sql(SQL_COUNTS_MONTH, conn, params=[m0, m1]))
            dur_stat_parts.append(pd.read_sql(SQL_DURATION_MONTH, conn, params=[m0, m1]))
            dur_bucket_parts.append(pd.read_sql(SQL_DURATION_BUCKETS_MONTH, conn, params=[m0, m1]))
        conn.close()

        counts = attach_rsr(pd.concat(count_parts, ignore_index=True), td_map)
        counts["pool_name"] = counts["pool_id"].map(lambda x: pool_names.get(int(x), f"Pool {x}"))
        cg = ["player_id", "pool_id", "pool_name", "rsr"]
        if "username" in counts.columns:
            counts = counts.drop(columns=["username"], errors="ignore")
        counts = counts.groupby(cg, dropna=False)["n"].sum().reset_index()
        counts.to_parquet(CACHE_COUNTS, index=False)
        print(f"  cached {len(counts):,} player×pool×limit rows", flush=True)

    if not td_map:
        conn = pyodbc.connect(cs, timeout=900)
        td_map = load_td_map(conn)
        conn.close()

    dur_stats = merge_duration_stats(dur_stat_parts, td_map)
    dur_buckets = merge_duration_buckets(dur_bucket_parts, td_map)

    limits = build_limits(counts, dur_stats, dur_buckets)
    meta = {
        "reg_from": reg_from.isoformat(),
        "reg_to": reg_to.isoformat(),
        "n_limits": len(limits),
    }
    CACHE_LIMITS_JSON.write_text(
        json.dumps({"meta": meta, "limits": limits}, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    html = render_html(limits, meta)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_HTML} ({len(limits)} limits) + {CACHE_LIMITS_JSON.name}", flush=True)


if __name__ == "__main__":
    main()
