#!/usr/bin/env python3
"""
Auswertung von Leistungsmessungen mit MEHREREN Lastzyklen je Logdatei
für den Treibervergleich ALT / MITTEL / NEU.

Gilt für beide Messreihen des Projekts:
  * Ethernet-Treiber (iperf3 Download oder Upload), je Zyklus
        60 s Leerlauf -> 60 s Last -> 60 s Leerlauf
  * Grafiktreiber (FurMark), je Zyklus
        60 s Leerlauf -> 180 s Last -> 60 s Leerlauf
Je Treiber gibt es EINE lange Logdatei, in der alle (typisch 10) Zyklen
hintereinander stehen. Format wie vom Messgerät exportiert:

    Zeit;Wert 1-avg[W]
    2026-08-28 10:24:28.077581; 8.02956
    ...

Was das Programm macht
----------------------
 1. Datenqualität je Datei prüfen (Abtastrate, Lücken, Trennschärfe).
 2. Alle Lastphasen automatisch finden (Schwelle aus den Daten, Glättung,
    Mindestdauer, Flankenkorrektur auf der Rohkurve) und gegen den
    erwarteten Ablauf prüfen (Anzahl, Periodendauer, Lastdauer).
 3. Je Zyklus: Leerlauf davor, Last (festes, für alle gleich langes
    Fenster), Leerlauf danach, Delta, Energie, Einschwingspitze,
    Leerlaufspitzen.
 4. Je Treiber: Mittelwert, Standardabweichung, 95-%-Konfidenzintervall,
    Streuung zwischen den Zyklen, Trend über die Zyklen (Aufwärmen,
    thermische Drift), Ausreißer.
 5. Zwischen den Treibern: globaler Test (Kruskal-Wallis, Welch-ANOVA),
    paarweise Welch-t-Tests und Mann-Whitney-U mit Holm-Korrektur,
    Effektstärke (Hedges g), Differenz mit 95-%-Konfidenzintervall.
 6. Optional: Effizienz (Watt je Gbit/s bzw. Energie je Frame), wenn die
    CSV-Dateien der Benchmark-Skripte mit angegeben werden.
 7. Zyklusweiser Vergleich: je Zyklus ALT/MITTEL/NEU nebeneinander
    (Bild aller Zyklen, ein Bild je Zyklus mit Wertetabelle, Matrix im Bericht).
 8. Ausgabe: Textbericht (.log), Excel-Arbeitsmappe (Übersicht, Zyklusvergleich
    mit Formeln, Einzelwerte, Tests, Trend, Info), CSV-Dateien, drei
    LaTeX-Tabellen und sieben Diagramme plus ein Bild je Zyklus.

Benötigt: numpy, pandas, matplotlib, scipy, openpyxl
    pip install numpy pandas matplotlib scipy openpyxl

Beispiele
---------
Ethernet, Dateien heißen ALT_download.log, MITTEL_download.log, NEU_download.log:
    python analyze_driver_power.py --preset nic --logdir logs \\
        --pattern "{version}_download.log" --tag download

Ethernet mit Durchsatz aus den CSV-Dateien des Benchmark-Skripts:
    python analyze_driver_power.py --preset nic --logdir logs \\
        --pattern "{version}_download.log" --tag download \\
        --perf ALT=logs/ALT_download_20260901_101500.csv \\
        --perf MITTEL=logs/MITTEL_download_20260901_111500.csv \\
        --perf NEU=logs/NEU_download_20260901_121500.csv

FurMark, Dateien wie bisher benannt:
    python analyze_driver_power.py --preset furmark --logdir logs \\
        --pattern "Mess_Furmark_GRAKA_{version}.log" --tag furmark

Einzelne Dateien direkt angeben:
    python analyze_driver_power.py --preset nic \\
        --file ALT=a.log --file MITTEL=b.log --file NEU=c.log
"""

import argparse
import itertools
import logging
import math
import os
import sys

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from scipy import stats as sps
    HAVE_SCIPY = True
except ImportError:  # Programm läuft weiter, nur ohne p-Werte
    sps = None
    HAVE_SCIPY = False


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DEFAULT_VERSIONS = ["ALT", "MITTEL", "NEU"]

COLORS = {"ALT": "#d62728", "MITTEL": "#ff7f0e", "NEU": "#2ca02c"}
FALLBACK_COLORS = ["#1f77b4", "#9467bd", "#8c564b", "#e377c2", "#17becf"]

PRESETS = {
    "nic": dict(
        benchmark="iperf3 (Ethernet)", load=60.0, idle=60.0, cycles=10,
        smooth=3.0, skip_ramp=3.0,
        labels={"ALT": "ALT (12.19.1.32)", "MITTEL": "MITTEL (12.19.2.45)", "NEU": "NEU (12.19.2.64)"},
    ),
    "furmark": dict(
        benchmark="FurMark (Grafik)", load=180.0, idle=60.0, cycles=10,
        smooth=5.0, skip_ramp=10.0,
        labels={"ALT": "ALT (31.0.101.2115)", "MITTEL": "MITTEL (31.0.101.2135)", "NEU": "NEU (31.0.101.2141)"},
    ),
    "prime95": dict(
        benchmark="Prime95", load=180.0, idle=60.0, cycles=10,
        smooth=5.0, skip_ramp=10.0, labels={},
    ),
    "custom": dict(
        benchmark="Benchmark", load=60.0, idle=60.0, cycles=10,
        smooth=3.0, skip_ramp=3.0, labels={},
    ),
}

DT = 0.25  # Raster der Interpolation in s

logger = logging.getLogger("driver_power")


def setup_logging(outdir: str, tag: str) -> str:
    logfile = os.path.join(outdir, f"analyse_{tag}.log")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(message)s")
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(logfile, mode="w", encoding="utf-8")):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logfile


def log(msg: str = ""):
    logger.info(msg)


def heading(title: str, char: str = "="):
    log("")
    log(char * 100)
    log(title)
    log(char * 100)


def fmt(x, nd: int = 2, unit: str = "") -> str:
    """Zahl formatieren; None/NaN -> 'n/a'."""
    if x is None:
        return "n/a"
    try:
        if not np.isfinite(x):
            return "n/a"
    except TypeError:
        return str(x)
    return f"{x:.{nd}f}{unit}"


def fmt_p(p) -> str:
    if p is None or not np.isfinite(p):
        return "n/a"
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def color_of(version: str, versions: list) -> str:
    if version in COLORS:
        return COLORS[version]
    return FALLBACK_COLORS[versions.index(version) % len(FALLBACK_COLORS)]


# ---------------------------------------------------------------------------
# Einlesen und Datenqualität
# ---------------------------------------------------------------------------

def load_log(path: str) -> pd.DataFrame:
    """Liest eine Messgeräte-Logdatei. Kopfzeilen und kaputte Zeilen werden
    verworfen, Dezimalkomma wird akzeptiert."""
    raw = pd.read_csv(path, sep=";", header=None, names=["time", "power"],
                      engine="python", dtype=str, skipinitialspace=True,
                      encoding="utf-8-sig", on_bad_lines="skip")
    raw["time"] = pd.to_datetime(raw["time"].str.strip(), format="ISO8601", errors="coerce")
    raw["power"] = pd.to_numeric(
        raw["power"].str.strip().str.replace(",", ".", regex=False), errors="coerce")
    df = raw.dropna().sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(df) < 10:
        raise ValueError(f"zu wenige gültige Messwerte in {path}")
    df["seconds"] = (df["time"] - df["time"].iloc[0]).dt.total_seconds()
    return df[["time", "seconds", "power"]]


def data_quality(df: pd.DataFrame) -> dict:
    dts = np.diff(df["seconds"].values)
    med = float(np.median(dts))
    return {
        "n": len(df),
        "start": df["time"].iloc[0],
        "duration_s": float(df["seconds"].iloc[-1]),
        "dt_median": med,
        "dt_max": float(dts.max()),
        "gaps": int(np.sum(dts > max(3 * med, 3.0))),
        "p_min": float(df["power"].min()),
        "p_max": float(df["power"].max()),
    }


def resample(df: pd.DataFrame, dt: float = DT):
    t = np.arange(0.0, df["seconds"].iloc[-1], dt)
    p = np.interp(t, df["seconds"].values, df["power"].values)
    return t, p


def smooth(p: np.ndarray, window_s: float, dt: float = DT) -> np.ndarray:
    win = max(1, int(round(window_s / dt)))
    return pd.Series(p).rolling(win, center=True, min_periods=1).mean().values


# ---------------------------------------------------------------------------
# Lastphasen finden
# ---------------------------------------------------------------------------

def auto_threshold(p_smooth: np.ndarray):
    """Zwei-Klassen-Trennung (Leerlauf / Last) auf der geglätteten Kurve.
    Startwert Mitte zwischen 10.- und 90.-Perzentil, dann iterativ die Mitte
    zwischen den Medianen beider Klassen. Mediane statt Mittelwerte, damit
    kurze Hintergrundspitzen die Trennung nicht verschieben. Funktioniert
    unabhängig davon, wie groß der Lastanteil der Messung ist."""
    thr = 0.5 * (np.percentile(p_smooth, 10) + np.percentile(p_smooth, 90))
    lo = hi = float("nan")
    for _ in range(100):
        a, b = p_smooth[p_smooth < thr], p_smooth[p_smooth >= thr]
        if len(a) == 0 or len(b) == 0:
            break
        lo, hi = float(np.median(a)), float(np.median(b))
        new = 0.5 * (lo + hi)
        if abs(new - thr) < 1e-4:
            thr = new
            break
        thr = new
    # Trennschärfe: Abstand der Niveaus in Einheiten der robusten Streuung
    a, b = p_smooth[p_smooth < thr], p_smooth[p_smooth >= thr]
    spread = 1.4826 * np.median(np.abs(np.concatenate([a - lo, b - hi]))) if len(a) and len(b) else np.nan
    separation = (hi - lo) / spread if spread and spread > 0 else float("inf")
    return float(thr), lo, hi, float(separation)


def find_runs(mask: np.ndarray):
    runs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def detect_segments(t, p, thr, smooth_s, min_dur, merge_gap, refine_s=3.0):
    """Findet alle zusammenhängenden Lastphasen.

    1. Glätten, damit kurze Hintergrundspitzen nicht als Last zählen.
    2. Bereiche über der Schwelle suchen, kurze Einbrüche (< merge_gap)
       innerhalb einer Lastphase überbrücken.
    3. Nur Bereiche behalten, die mindestens min_dur lang sind.
    4. Flanken auf der Rohkurve nachführen: Anfang = erster Zeitpunkt, ab
       dem die Rohkurve refine_s lang durchgehend über der Schwelle liegt,
       Ende analog rückwärts. Sonst verschiebt die Glättung die Grenzen
       und zieht Leerlaufsekunden ins Lastfenster.
    """
    dt = t[1] - t[0]
    ps = smooth(p, smooth_s, dt)
    runs = find_runs(ps >= thr)

    merged = []
    for r in runs:
        if merged and (t[r[0]] - t[merged[-1][1]]) <= merge_gap:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    segs = [r for r in merged if t[r[1]] - t[r[0]] >= min_dur]

    k = max(1, int(round(refine_s / dt)))
    margin = int(round(2 * max(smooth_s, refine_s) / dt))
    above = p >= thr
    refined = []
    for idx, (s, e) in enumerate(segs):
        lower = refined[-1][1] + 1 if refined else 0
        upper = segs[idx + 1][0] - 1 if idx + 1 < len(segs) else len(p) - 1
        start = next((i for i in range(max(lower, s - margin), e + 1)
                      if above[i:i + k].all()), s)
        end = next((j for j in range(min(upper, e + margin), start, -1)
                    if above[max(start, j - k + 1):j + 1].all()), e)
        refined.append((start, end))
    return [(float(t[s]), float(t[e])) for s, e in refined], ps


# ---------------------------------------------------------------------------
# Kennzahlen je Zyklus
# ---------------------------------------------------------------------------

_trapz = getattr(np, "trapezoid", None) or np.trapz


def window(t, p, a, b):
    m = (t >= a) & (t <= b)
    return t[m], p[m]


def basic_stats(tw, pw):
    if len(pw) < 4:
        return None
    mean = float(pw.mean())
    std = float(pw.std(ddof=1))
    return {
        "mean": mean, "median": float(np.median(pw)), "std": std,
        "cv": std / mean * 100 if mean else float("nan"),
        "min": float(pw.min()), "max": float(pw.max()),
        "p95": float(np.percentile(pw, 95)),
        "energy_Wh": float(_trapz(pw, tw)) / 3600.0,
        "dur": float(tw[-1] - tw[0]),
    }


def count_spikes(pw, baseline, dt=DT, min_rise=3.0):
    """Zählt kurze Spitzen im Leerlauf (Ereignisse, nicht Messpunkte)."""
    if len(pw) < 4:
        return 0, float("nan")
    mad = 1.4826 * np.median(np.abs(pw - baseline))
    thr = baseline + max(min_rise, 4 * mad)
    above = pw > thr
    events = int(np.sum(above[1:] & ~above[:-1]) + (1 if above[0] else 0))
    return events, float(pw.max())


def analyse_cycles(t, p, segs, cfg, win_len):
    """Berechnet je Lastphase alle Kennzahlen."""
    idle_len = cfg["idle_window"]
    guard, ramp = cfg["guard"], cfg["skip_ramp"]
    t_end = t[-1]
    cycles = []
    for i, (ts, te) in enumerate(segs):
        prev_end = segs[i - 1][1] if i > 0 else -np.inf
        next_start = segs[i + 1][0] if i + 1 < len(segs) else np.inf

        pre_a = max(ts - guard - idle_len, prev_end + guard, 0.0)
        pre_b = ts - guard
        post_a = te + guard
        post_b = min(te + guard + idle_len, next_start - guard, t_end)

        complete = (te - ts) >= ramp + win_len - 0.5
        load_a = ts + ramp
        load_b = ts + ramp + win_len if complete else te

        s_load = basic_stats(*window(t, p, load_a, load_b))
        tw, pw_pre = window(t, p, pre_a, pre_b) if pre_b - pre_a >= 10 else (np.array([]), np.array([]))
        s_pre = basic_stats(tw, pw_pre)
        tw, pw_post = window(t, p, post_a, post_b) if post_b - post_a >= 10 else (np.array([]), np.array([]))
        s_post = basic_stats(tw, pw_post)

        idle_all = np.concatenate([pw_pre, pw_post])
        idle_mean = float(idle_all.mean()) if len(idle_all) >= 4 else float("nan")
        idle_median = float(np.median(idle_all)) if len(idle_all) >= 4 else float("nan")
        spikes, spike_max = count_spikes(idle_all, idle_median) if len(idle_all) >= 4 else (0, float("nan"))

        _, p_ramp = window(t, p, ts, ts + ramp + 2)
        rec = {
            "cycle": i + 1,
            "start_s": ts,
            "start_min": ts / 60.0,
            "load_dur_s": te - ts,
            "period_s": ts - segs[i - 1][0] if i > 0 else float("nan"),
            "complete": bool(complete),
            "idle_pre_mean": s_pre["mean"] if s_pre else float("nan"),
            "idle_pre_median": s_pre["median"] if s_pre else float("nan"),
            "idle_post_mean": s_post["mean"] if s_post else float("nan"),
            "idle_post_median": s_post["median"] if s_post else float("nan"),
            "idle_mean": idle_mean,
            "idle_median": idle_median,
            "idle_drift": (s_post["mean"] - s_pre["mean"]) if (s_pre and s_post) else float("nan"),
            "idle_std": float(idle_all.std(ddof=1)) if len(idle_all) >= 4 else float("nan"),
            "pre_a": pre_a if s_pre else float("nan"), "pre_b": pre_b if s_pre else float("nan"),
            "post_a": post_a if s_post else float("nan"), "post_b": post_b if s_post else float("nan"),
            "load_a": load_a if s_load else float("nan"), "load_b": load_b if s_load else float("nan"),
            "idle_spikes": spikes,
            "idle_spike_max": spike_max,
            "load_mean": s_load["mean"] if s_load else float("nan"),
            "load_median": s_load["median"] if s_load else float("nan"),
            "load_std": s_load["std"] if s_load else float("nan"),
            "load_cv": s_load["cv"] if s_load else float("nan"),
            "load_max": s_load["max"] if s_load else float("nan"),
            "load_p95": s_load["p95"] if s_load else float("nan"),
            "load_window_s": s_load["dur"] if s_load else float("nan"),
            "energy_load_Wh": s_load["energy_Wh"] if s_load else float("nan"),
            "ramp_peak": float(p_ramp.max()) if len(p_ramp) else float("nan"),
        }
        rec["delta_mean"] = rec["load_mean"] - rec["idle_mean"]
        rec["delta_robust"] = rec["load_median"] - rec["idle_median"]
        cycles.append(rec)
    return cycles


# ---------------------------------------------------------------------------
# Leistungsdaten der Benchmark-Skripte (optional)
# ---------------------------------------------------------------------------

PERF_METRICS = [
    # Spalte, Anzeigename, Einheit Effizienz, Umrechnung (delta_W, wert, lastdauer_s) -> Effizienz
    ("Mbps", "Durchsatz [Mbit/s]", "W je Gbit/s", lambda d, v, L: d / (v / 1000.0)),
    ("FPS", "Bildrate [FPS]", "mJ je Frame", lambda d, v, L: d / v * 1000.0),
    # Score = Punkte über die gesamte Benchmarkdauer -> Mehrenergie je Punkt in Joule
    ("Score", "FurMark-Score", "J je Score-Punkt", lambda d, v, L: d * L / v),
]


def load_perf_csv(path: str):
    """Liest die CSV eines Benchmark-Skripts und gibt {Zyklus: Wert} zurück."""
    df = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    if "Iter" not in df.columns:
        raise ValueError(f"Spalte 'Iter' fehlt in {path}")
    for col, name, unit, fn in PERF_METRICS:
        if col not in df.columns:
            continue
        vals = pd.to_numeric(df[col].str.strip().str.replace(",", ".", regex=False), errors="coerce")
        it = pd.to_numeric(df["Iter"], errors="coerce")
        ok = vals.notna() & it.notna() & (vals > 0)
        if ok.sum() == 0:
            continue
        per_cycle = pd.Series(vals[ok].values, index=it[ok].astype(int).values).groupby(level=0).mean()
        return {"column": col, "name": name, "unit": unit, "fn": fn,
                "values": per_cycle.to_dict()}
    raise ValueError(f"keine auswertbare Leistungsspalte (Mbps/FPS/Score) in {path}")


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------

_T_TABLE = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
            8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
            25: 2.060, 30: 2.042, 60: 2.000, 120: 1.980}


def t_crit(df: float) -> float:
    if not np.isfinite(df) or df <= 0:
        return float("nan")
    if HAVE_SCIPY:
        return float(sps.t.ppf(0.975, df))
    for k in sorted(_T_TABLE, reverse=True):  # konservativ abrunden
        if df >= k:
            return _T_TABLE[k]
    return _T_TABLE[1]


def clean(values):
    x = np.asarray(values, dtype=float)
    return x[np.isfinite(x)]


def describe(values, with_cv: bool = True) -> dict:
    x = clean(values)
    n = len(x)
    d = {"n": n, "mean": np.nan, "sd": np.nan, "sem": np.nan, "ci_lo": np.nan,
         "ci_hi": np.nan, "median": np.nan, "min": np.nan, "max": np.nan, "cv": np.nan}
    if n == 0:
        return d
    d.update(mean=float(x.mean()), median=float(np.median(x)),
             min=float(x.min()), max=float(x.max()))
    if n >= 2:
        sd = float(x.std(ddof=1))
        sem = sd / math.sqrt(n)
        tc = t_crit(n - 1)
        d.update(sd=sd, sem=sem, ci_lo=d["mean"] - tc * sem, ci_hi=d["mean"] + tc * sem,
                 cv=sd / abs(d["mean"]) * 100 if (with_cv and d["mean"]) else np.nan)
    return d


def compare(a, b) -> dict:
    """Welch-t-Test, Mann-Whitney-U, Hedges g und 95-%-KI der Differenz b - a."""
    a, b = clean(a), clean(b)
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = a.mean(), b.mean()
    va, vb = a.var(ddof=1), b.var(ddof=1)
    diff = mb - ma
    se = math.sqrt(va / na + vb / nb)
    if se > 0:
        df = (va / na + vb / nb) ** 2 / (
            ((va / na) ** 2 / (na - 1) if na > 1 else 0) + ((vb / nb) ** 2 / (nb - 1) if nb > 1 else 0))
        tstat = diff / se
        p_welch = float(2 * sps.t.sf(abs(tstat), df)) if HAVE_SCIPY else float("nan")
        tc = t_crit(df)
        ci = (diff - tc * se, diff + tc * se)
    else:
        df, p_welch, ci = float("nan"), (0.0 if diff != 0 else 1.0), (diff, diff)
    p_mw = float("nan")
    if HAVE_SCIPY:
        try:
            p_mw = float(sps.mannwhitneyu(a, b, alternative="two-sided").pvalue)
        except ValueError:
            pass
    sp = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)) if na + nb > 2 else 0
    g = (diff / sp) * (1 - 3 / (4 * (na + nb) - 9)) if sp > 0 else float("nan")
    return {"n_a": na, "n_b": nb, "mean_a": ma, "mean_b": mb, "diff": diff,
            "rel": diff / ma * 100 if ma else float("nan"),
            "ci_lo": ci[0], "ci_hi": ci[1], "df": df,
            "p_welch": p_welch, "p_mw": p_mw, "g": g}


def holm(pvals):
    """Holm-Bonferroni-Korrektur; NaN bleibt NaN."""
    idx = [i for i, p in enumerate(pvals) if np.isfinite(p)]
    adj = [float("nan")] * len(pvals)
    order = sorted(idx, key=lambda i: pvals[i])
    m, running = len(order), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def effect_label(g) -> str:
    if not np.isfinite(g):
        return "n/a"
    a = abs(g)
    return ("vernachlässigbar" if a < 0.2 else "klein" if a < 0.5
            else "mittel" if a < 0.8 else "groß")


def global_tests(groups):
    groups = [clean(g) for g in groups]
    out = {"kruskal": float("nan"), "anova": float("nan"), "anova_name": "n/a"}
    if not HAVE_SCIPY or len(groups) < 2 or any(len(g) < 2 for g in groups):
        return out
    try:
        out["kruskal"] = float(sps.kruskal(*groups).pvalue)
    except ValueError:
        pass
    try:
        out["anova"] = float(sps.alexandergovern(*groups).pvalue)
        out["anova_name"] = "Alexander-Govern (Welch-Typ)"
    except Exception:
        try:
            out["anova"] = float(sps.f_oneway(*groups).pvalue)
            out["anova_name"] = "einfaktorielle ANOVA"
        except Exception:
            pass
    return out


def trend(xs, ys) -> dict:
    x, y = np.asarray(xs, float), np.asarray(ys, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 3:
        return {"slope": np.nan, "p": np.nan, "r2": np.nan, "total": np.nan}
    if HAVE_SCIPY:
        r = sps.linregress(x, y)
        slope, p, r2 = r.slope, r.pvalue, r.rvalue ** 2
    else:
        slope = np.polyfit(x, y, 1)[0]
        p, r2 = np.nan, np.corrcoef(x, y)[0, 1] ** 2
    return {"slope": float(slope), "p": float(p), "r2": float(r2),
            "total": float(slope * (x.max() - x.min()))}


def robust_z(values):
    x = np.asarray(values, float)
    med = np.nanmedian(x)
    mad = np.nanmedian(np.abs(x - med))
    if not np.isfinite(mad) or mad == 0:
        return np.zeros_like(x)
    return 0.6745 * (x - med) / mad


# ---------------------------------------------------------------------------
# Diagramme
# ---------------------------------------------------------------------------

def plot_timeline(res, versions, labels, cfg, path):
    fig, axes = plt.subplots(len(versions), 1, figsize=(14, 2.8 * len(versions)),
                             sharey=True, squeeze=False)
    for ax, v in zip(axes[:, 0], versions):
        r = res[v]
        ax.plot(r["t"] / 60, r["p"], lw=0.5, color=color_of(v, versions))
        ax.axhline(r["thr"], ls="--", lw=0.8, color="black", alpha=0.5)
        for c, (ts, te) in zip(r["cycles"], r["segs"]):
            ax.axvspan(ts / 60, te / 60, color="#bbbbbb" if c["complete"] else "#f4a3a3",
                       alpha=0.35, lw=0)
            ax.text((ts + te) / 120, 1.0, str(c["cycle"]), transform=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=7)
        ax.set_title(f"{labels[v]}: {len(r['segs'])} von {cfg['cycles']} erwarteten Lastphasen "
                     f"(Schwelle {r['thr']:.1f} W)", fontsize=10, pad=12)
        ax.set_ylabel("Leistung [W]")
        ax.grid(alpha=0.3)
    axes[-1, 0].set_xlabel("Zeit seit Messbeginn [min]")
    fig.suptitle(f"{cfg['benchmark']}: Gesamtverlauf mit erkannten Lastphasen "
                 f"(grau = vollständig, rot = unvollständig)", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def cycle_profiles(r, cfg):
    rel = np.arange(-cfg["idle"], cfg["load"] + cfg["idle"], DT)
    prof = np.array([np.interp(ts + rel, r["t"], r["p"], left=np.nan, right=np.nan)
                     for ts, _ in r["segs"]])
    return rel, prof


def plot_overlay(res, versions, labels, cfg, path):
    fig, axes = plt.subplots(1, len(versions), figsize=(5.2 * len(versions), 4.3),
                             sharey=True, squeeze=False)
    for ax, v in zip(axes[0], versions):
        rel, prof = cycle_profiles(res[v], cfg)
        cmap = plt.get_cmap("viridis")
        n = len(prof)
        for i, row in enumerate(prof):
            ax.plot(rel, row, lw=0.6, color=cmap(i / max(n - 1, 1)), alpha=0.8)
        if n:
            ax.plot(rel, np.nanmean(prof, axis=0), lw=1.8, color="black", label="Mittel")
        ax.axvspan(0, cfg["load"], color="#dddddd", alpha=0.4, lw=0)
        ax.set_title(labels[v], fontsize=10)
        ax.set_xlabel("Zeit relativ zum Lastbeginn [s]")
        ax.grid(alpha=0.3)
    axes[0, 0].set_ylabel("Leistung [W]")
    sm = plt.cm.ScalarMappable(cmap="viridis",
                               norm=plt.Normalize(1, max(cfg["cycles"], 2)))
    fig.colorbar(sm, ax=axes[0, -1], label="Zyklus")
    fig.suptitle(f"{cfg['benchmark']}: alle Zyklen übereinandergelegt", fontsize=11)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_mean_profile(res, versions, labels, cfg, path):
    fig, ax = plt.subplots(figsize=(12, 5))
    for v in versions:
        rel, prof = cycle_profiles(res[v], cfg)
        if not len(prof):
            continue
        # robust: Median und 10.–90. Perzentil über die Zyklen. Ein symmetrisches ±SD-Band
        # würde bei einzelnen Hintergrundspitzen physikalisch unmögliche Werte < 0 W zeigen.
        m = np.nanmedian(prof, axis=0)
        lo, hi = np.nanpercentile(prof, 10, axis=0), np.nanpercentile(prof, 90, axis=0)
        ax.plot(rel, m, lw=1.4, color=color_of(v, versions), label=labels[v])
        ax.fill_between(rel, lo, hi, color=color_of(v, versions), alpha=0.18, lw=0)
    ax.axvspan(0, cfg["load"], color="#dddddd", alpha=0.35, lw=0)
    ax.set_xlabel("Zeit relativ zum Lastbeginn [s]")
    ax.set_ylabel("Leistung [W]")
    ax.set_title(f"{cfg['benchmark']}: Zyklusprofil je Treiber "
                 f"(Linie = Median, Band = 10.–90. Perzentil über die Zyklen)", fontsize=11)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_distribution(table, versions, labels, cfg, path):
    metrics = [("delta", "Mehrverbrauch unter Last [W]"),
               ("load_mean", "Last Ø [W]"), ("idle_mean", "Leerlauf Ø [W]")]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    rng = np.random.default_rng(0)
    for ax, (col, title) in zip(axes, metrics):
        data = [clean(table.loc[(table.version == v) & table.used, col]) for v in versions]
        ax.boxplot(data, widths=0.5, showfliers=False)
        for i, (v, d) in enumerate(zip(versions, data), start=1):
            ax.scatter(i + rng.uniform(-0.12, 0.12, len(d)), d, s=18,
                       color=color_of(v, versions), zorder=3)
        ax.set_xticks(range(1, len(versions) + 1))
        ax.set_xticklabels([labels[v].replace(" (", "\n(") for v in versions], fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.grid(axis="y", alpha=0.3)
    fig.suptitle(f"{cfg['benchmark']}: Verteilung der Zykluswerte je Treiber", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_trend(table, versions, labels, cfg, path):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    for ax, (col, title) in zip(axes, [("load_mean", "Last Ø [W]"),
                                       ("delta", "Mehrverbrauch unter Last [W]")]):
        for v in versions:
            sub = table[table.version == v]
            ax.plot(sub.cycle, sub[col], "-o", ms=4, lw=1, color=color_of(v, versions),
                    label=labels[v])
            bad = sub[~sub.used]
            ax.scatter(bad.cycle, bad[col], marker="x", s=70, color="black", zorder=4)
        ax.set_xlabel("Zyklus")
        ax.set_title(title, fontsize=10)
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.suptitle(f"{cfg['benchmark']}: Verlauf über die Zyklen "
                 f"(× = nicht in der Statistik)", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_ci(summary, versions, labels, cfg, path, eff_unit=None):
    panels = [("delta", "Mehrverbrauch unter Last [W]")]
    panels.append(("eff", f"Effizienz [{eff_unit}]") if eff_unit else ("load_mean", "Last Ø [W]"))
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    for ax, (key, title) in zip(axes, panels):
        xs = np.arange(len(versions))
        for i, v in enumerate(versions):
            d = summary[v].get(key)
            if not d or not np.isfinite(d["mean"]):
                continue
            err = [[d["mean"] - d["ci_lo"]], [d["ci_hi"] - d["mean"]]] if np.isfinite(d["ci_lo"]) else None
            ax.bar(i, d["mean"], color=color_of(v, versions), alpha=0.85, width=0.6)
            if err:
                ax.errorbar(i, d["mean"], yerr=err, color="black", capsize=6, lw=1.2)
            top = d["ci_hi"] if np.isfinite(d["ci_hi"]) else d["mean"]
            ax.annotate(f"{d['mean']:.2f}", (i, top), xytext=(0, 4), textcoords="offset points",
                        ha="center", va="bottom", fontsize=9)
        ax.set_xticks(xs)
        ax.set_xticklabels([labels[v].replace(" (", "\n(") for v in versions], fontsize=9)
        ax.set_title(title, fontsize=10)
        ax.margins(y=0.12)
        ax.grid(axis="y", alpha=0.3)
    fig.suptitle(f"{cfg['benchmark']}: Mittelwerte mit 95-%-Konfidenzintervall", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Zyklusweiser Treibervergleich: Bilder
# ---------------------------------------------------------------------------

def cycle_keys(eff_meta):
    """Kennzahlen für den zyklusweisen Vergleich (Tabelle, Excel)."""
    keys = [
        ("idle_pre_mean", "Leerlauf vor [W]", 2), ("load_mean", "Last Ø [W]", 2),
        ("idle_post_mean", "Leerlauf nach [W]", 2), ("idle_mean", "Leerlauf Ø [W]", 2),
        ("delta", "Mehrverbrauch Delta [W]", 2), ("energy_load_Wh", "Energie Lastfenster [Wh]", 4),
        ("load_cv", "CV innerhalb Last [%]", 2), ("load_std", "Schwankung in der Last, SD [W]", 3),
        ("idle_std", "Schwankung im Leerlauf, SD [W]", 3), ("load_max", "Lastspitze [W]", 1),
        ("ramp_peak", "Einschwingspitze [W]", 1),
    ]
    if eff_meta:
        keys += [("perf", eff_meta["name"], 2), ("eff", f"Effizienz [{eff_meta['unit']}]", 3)]
    return keys


def cycle_row(table, v, k):
    sub = table[(table.version == v) & (table.cycle == k)]
    return sub.iloc[0] if len(sub) else None


def status_of(row):
    if row is None:
        return "fehlt"
    if not row.complete:
        return "unvollständig"
    if not row.used:
        return "ausgeschlossen"
    return "Ausreißer" if row.outlier else "ok"


def phase_shading(ax, cfg, with_labels=False):
    """Auswertefenster hinterlegen: Leerlauf vor, Lastfenster, Leerlauf nach
    (nominale Lage relativ zum Lastbeginn)."""
    g, iw = cfg["guard"], cfg["idle_window"]
    spans = [(-(g + iw), -g, "#cfe3f5", "Leerlauf vor"),
             (cfg["skip_ramp"], cfg["skip_ramp"] + cfg["win_len"], "#e6e6e6", "Lastfenster"),
             (cfg["load"] + g, cfg["load"] + g + iw, "#cfe3f5", "Leerlauf nach")]
    for a, b, col, name in spans:
        ax.axvspan(a, b, color=col, alpha=0.7, lw=0, zorder=0)
        if with_labels:
            ax.text((a + b) / 2, 1.01, name, transform=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=9, color="#444444")
    ax.axvline(0, color="#888888", lw=0.6, ls=":")


def plot_cycle_grid(res, table, versions, labels, cfg, path):
    """Alle Zyklen nebeneinander, in jedem Feld ALT/MITTEL/NEU übereinander."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    n = max(len(res[v]["segs"]) for v in versions)
    if n == 0:
        return
    ncols = min(5, n)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.1 * ncols, 3.2 * nrows + 0.8),
                             sharex=True, sharey=True, squeeze=False)
    for k in range(1, n + 1):
        ax = axes[(k - 1) // ncols][(k - 1) % ncols]
        phase_shading(ax, cfg)
        lines = []
        for v in versions:
            row = cycle_row(table, v, k)
            if row is None:
                continue
            prof = res[v]["prof"][k - 1]
            ax.plot(res[v]["rel"], prof, lw=0.9, ls="-" if row.used else "--",
                    color=color_of(v, versions), zorder=2)
            st = status_of(row)
            lines.append((v, f"{v}: Δ {row.delta:.2f} W" + ("" if st in ("ok", "Ausreißer") else f" ({st})")))
        for i, (v, txt) in enumerate(lines):
            ax.text(0.03, 0.96 - 0.11 * i, txt, transform=ax.transAxes, fontsize=7.5, va="top",
                    color=color_of(v, versions),
                    bbox=dict(facecolor="white", edgecolor="none", alpha=0.75, pad=0.8))
        ax.set_title(f"Zyklus {k}", fontsize=10)
        ax.grid(alpha=0.25)
    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Zeit relativ zum Lastbeginn [s]", fontsize=8)
    for row_axes in axes:
        row_axes[0].set_ylabel("Leistung [W]", fontsize=8)
    handles = [Line2D([], [], color=color_of(v, versions), lw=1.5, label=labels[v]) for v in versions]
    handles += [Patch(color="#cfe3f5", label="Leerlauffenster"), Patch(color="#e6e6e6", label="Lastfenster"),
                Line2D([], [], color="black", ls="--", lw=1, label="nicht verwertet")]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False, fontsize=9)
    fig.suptitle(f"{cfg['benchmark']}: Treibervergleich je Zyklus "
                 f"(am Lastbeginn ausgerichtet, Δ = Mehrverbrauch unter Last)", fontsize=12)
    fig.tight_layout(rect=[0, 0.05, 1, 0.97])
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_cycle_single(res, table, versions, labels, cfg, k, path, ylim=None, with_table=True):
    """Ein Zyklus groß, darunter (optional) die Kennzahlen je Treiber als Tabelle.
    ylim: gemeinsame y-Achse, damit sich mehrere Zyklusbilder nebeneinander vergleichen lassen."""
    if with_table:
        fig = plt.figure(figsize=(11.5, 7.2))
        gs = fig.add_gridspec(2, 1, height_ratios=[3.0, 1.3], hspace=0.32)
        ax = fig.add_subplot(gs[0])
    else:
        fig, ax = plt.subplots(figsize=(11.5, 5.2))
    phase_shading(ax, cfg, with_labels=True)
    cells, rlabels, rcolors, used_flags = [], [], [], []
    for v in versions:
        row = cycle_row(table, v, k)
        if row is None:
            continue
        ax.plot(res[v]["rel"], res[v]["prof"][k - 1], lw=1.3, ls="-" if row.used else "--",
                color=color_of(v, versions), label=labels[v], zorder=2)
        used_flags.append(bool(row.used))
        cells.append([fmt(row.idle_pre_mean), fmt(row.load_mean), fmt(row.idle_post_mean),
                      fmt(row.delta), fmt(row.energy_load_Wh, 4), fmt(row.load_cv, 1),
                      fmt(row.load_max, 1), status_of(row)])
        rlabels.append(labels[v])
        rcolors.append(color_of(v, versions))
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_xlim(-cfg["idle"], cfg["load"] + cfg["idle"])
    ax.set_xlabel("Zeit relativ zum Lastbeginn [s]")
    ax.set_ylabel("Leistung [W]")
    ax.set_title(f"{cfg['benchmark']}: Zyklus {k} – Treiber im Vergleich", fontsize=12, pad=18)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3)
    if not with_table:
        fig.subplots_adjust(left=0.08, right=0.97, top=0.88, bottom=0.12)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return
    tax = fig.add_subplot(gs[1])
    tax.axis("off")
    if cells:
        tbl = tax.table(cellText=cells, rowLabels=rlabels, loc="center", cellLoc="center",
                        colLabels=["Leerlauf\nvor [W]", "Last Ø\n[W]", "Leerlauf\nnach [W]", "Delta\n[W]",
                                   "Energie\n[Wh]", "CV Last\n[%]", "Max\n[W]", "Status"])
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1, 1.6)
        for c in range(8):
            tbl[(0, c)].set_height(tbl[(0, c)].get_height() * 1.6)
        for i, c in enumerate(rcolors, start=1):
            tbl[(i, -1)].get_text().set_color(c)
            tbl[(i, -1)].get_text().set_fontweight("bold")
        # kleinsten Mehrverbrauch hervorheben
        deltas = [float(c[3]) if (c[3] != "n/a" and u) else np.inf for c, u in zip(cells, used_flags)]
        if np.isfinite(min(deltas)):
            tbl[(int(np.argmin(deltas)) + 1, 3)].set_facecolor("#d8f0d8")
    fig.subplots_adjust(left=0.20, right=0.97, top=0.92, bottom=0.03)
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Zyklusweiser Treibervergleich: Textbericht
# ---------------------------------------------------------------------------

def report_cycle_matrix(table, versions, labels, baseline, eff_meta):
    heading("3b. Zyklusweiser Treibervergleich (Zeile = Zyklus, Spalte = Treiber)", "-")
    log("Werte in Klammern: Zyklus nicht verwertet (unvollständig bzw. ausgeschlossen). "
        "'kleinster' = Treiber mit dem niedrigsten Wert in diesem Zyklus.")
    keys = [("idle_pre_mean", "Leerlauf vor [W]", 2), ("load_mean", "Last Ø [W]", 2),
            ("idle_post_mean", "Leerlauf nach [W]", 2), ("delta", "Mehrverbrauch Delta [W]", 2),
            ("energy_load_Wh", "Energie Lastfenster [Wh]", 4)]
    if eff_meta:
        keys.append(("eff", f"Effizienz [{eff_meta['unit']}]", 3))
    others = [v for v in versions if v != baseline]
    n = int(table.cycle.max()) if len(table) else 0
    for key, name, nd in keys:
        log("")
        log(name)
        head = f"  {'Zyklus':>6}" + "".join(f"{v:>12}" for v in versions) + f"{'kleinster':>12}"
        head += "".join(f"{v + '−' + baseline:>16}" for v in others)
        log(head)
        wins = {v: 0 for v in versions}
        for k in range(1, n + 1):
            vals, used_vals = {}, {}
            for v in versions:
                row = cycle_row(table, v, k)
                if row is None or not np.isfinite(row[key]):
                    vals[v] = "--"
                elif row.used:
                    vals[v] = fmt(row[key], nd)
                    used_vals[v] = float(row[key])
                else:
                    vals[v] = f"({fmt(row[key], nd)})"
            best = min(used_vals, key=used_vals.get) if used_vals else "--"
            if best in wins:
                wins[best] += 1
            line = f"  {k:>6}" + "".join(f"{vals[v]:>12}" for v in versions) + f"{best:>12}"
            for v in others:
                d = (used_vals[v] - used_vals[baseline]) if (v in used_vals and baseline in used_vals) else np.nan
                line += f"{fmt(d, nd):>16}"
            log(line)
        for stat in ("Mittel", "SD"):
            line = f"  {stat:>6}"
            for v in versions:
                d = describe(table.loc[(table.version == v) & table.used, key])
                line += f"{fmt(d['mean'] if stat == 'Mittel' else d['sd'], nd):>12}"
            log(line)
        log("  Zyklen mit kleinstem Wert: " + ", ".join(f"{labels[v]} {wins[v]}×" for v in versions))


# ---------------------------------------------------------------------------
# Excel-Arbeitsmappe
# ---------------------------------------------------------------------------

def write_excel(path, table, versions, labels, cfg, baseline, eff_meta, comp_rows,
                global_res, trend_rows, res, win_txt, stab_tests=()):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.formatting.rule import FormulaRule
        from openpyxl.utils import get_column_letter as L
    except ImportError:
        log("HINWEIS: openpyxl nicht installiert – keine Excel-Datei erzeugt (pip install openpyxl).")
        return False

    HDR = PatternFill("solid", fgColor="DCE6F1")
    AGG = PatternFill("solid", fgColor="F2F2F2")
    GREEN = PatternFill("solid", fgColor="C6EFCE")
    thin = Side(style="thin", color="BFBFBF")
    BOX = Border(top=thin, bottom=thin, left=thin, right=thin)
    bold = Font(bold=True)
    wb = Workbook()
    ws_over = wb.active
    ws_over.title = "Übersicht"
    ws = wb.create_sheet("Zyklusvergleich")

    def nf(nd):
        return "0" if nd == 0 else "0." + "0" * nd

    def hdr(sheet, r, c, text):
        cell = sheet.cell(r, c, text)
        cell.font = bold
        cell.fill = HDR
        cell.border = BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        return cell

    # ---------------- Blatt Zyklusvergleich ----------------
    keys = cycle_keys(eff_meta)
    others = [v for v in versions if v != baseline]
    nv = len(versions)
    c_min = 2 + nv
    c_diff = c_min + 1
    c_note = c_diff + len(others)
    n_cyc = int(table.cycle.max()) if len(table) else 0
    ws.cell(1, 1, f"{cfg['benchmark']}: zyklusweiser Treibervergleich").font = Font(bold=True, size=13)
    ws.cell(2, 1, "Leere Zelle = Zyklus nicht verwertet (Grund in 'Hinweis'). Die grau hinterlegten "
                  "Kennzahlen sind Formeln über die verwerteten Zyklen; grün = kleinster Wert im Zyklus.")
    agg = {}
    r = 4
    for key, name, nd in keys:
        ws.cell(r, 1, name).font = Font(bold=True, size=12)
        r += 1
        hr = r
        hdr(ws, r, 1, "Zyklus")
        for j, v in enumerate(versions):
            hdr(ws, r, 2 + j, labels[v])
        hdr(ws, r, c_min, "Kleinster Wert")
        for i, v in enumerate(others):
            hdr(ws, r, c_diff + i, f"{labels[v]} − {labels[baseline]}")
        hdr(ws, r, c_note, "Hinweis")
        r += 1
        first = r
        dr = f"${L(2)}${hr}:${L(1 + nv)}${hr}"
        for k in range(1, n_cyc + 1):
            ws.cell(r, 1, k).border = BOX
            notes = []
            for j, v in enumerate(versions):
                row = cycle_row(table, v, k)
                cell = ws.cell(r, 2 + j)
                cell.border = BOX
                if row is not None and row.used and np.isfinite(row[key]):
                    cell.value = round(float(row[key]), 6)
                    cell.number_format = nf(nd)
                else:
                    notes.append(f"{v}: {status_of(row)}")
            rng = f"{L(2)}{r}:{L(1 + nv)}{r}"
            ws.cell(r, c_min, f'=IF(COUNT({rng})=0,"",INDEX({dr},MATCH(MIN({rng}),{rng},0)))').border = BOX
            cb = L(2 + versions.index(baseline))
            for i, v in enumerate(others):
                cv = L(2 + versions.index(v))
                cell = ws.cell(r, c_diff + i, f'=IF(OR({cv}{r}="",{cb}{r}=""),"",{cv}{r}-{cb}{r})')
                cell.number_format = nf(nd)
                cell.border = BOX
            ws.cell(r, c_note, "; ".join(notes))
            r += 1
        last = r - 1
        if last >= first:
            a0 = f"{L(2)}{first}"
            ws.conditional_formatting.add(
                f"{L(2)}{first}:{L(1 + nv)}{last}",
                FormulaRule(formula=[f"AND(ISNUMBER({a0}),{a0}=MIN(${L(2)}{first}:${L(1 + nv)}{first}))"],
                            fill=GREEN))
        stats = ["n", "Mittelwert", "Standardabweichung", "Varianz", "Spannweite", "95-%-KI ±", "KI unten",
                 "KI oben", "Median", "Minimum", "Maximum", "CV zw. Zyklen [%]", "Zyklen mit kleinstem Wert"]
        srow = {}
        for s in stats:
            srow[s] = r
            ws.cell(r, 1, s).font = bold
            ws.cell(r, 1).fill = AGG
            r += 1
        cols = [(2 + j, versions[j]) for j in range(nv)] + [(c_diff + i, None) for i in range(len(others))]
        for c, v in cols:
            cl = L(c)
            rg = f"{cl}{first}:{cl}{max(last, first)}"
            nr, mr, sr, hwr = (f"{cl}{srow[s]}" for s in ("n", "Mittelwert", "Standardabweichung", "95-%-KI ±"))
            f = {
                "n": f"=COUNT({rg})",
                "Mittelwert": f'=IF({nr}=0,"",AVERAGE({rg}))',
                "Standardabweichung": f'=IF({nr}<2,"",STDEV({rg}))',
                "Varianz": f'=IF({nr}<2,"",VAR({rg}))',
                "Spannweite": f'=IF({nr}=0,"",MAX({rg})-MIN({rg}))',
                "95-%-KI ±": f'=IF({nr}<2,"",TINV(0.05,{nr}-1)*{sr}/SQRT({nr}))',
                "KI unten": f'=IF({nr}<2,"",{mr}-{hwr})',
                "KI oben": f'=IF({nr}<2,"",{mr}+{hwr})',
                "Median": f'=IF({nr}=0,"",MEDIAN({rg}))',
                "Minimum": f'=IF({nr}=0,"",MIN({rg}))',
                "Maximum": f'=IF({nr}=0,"",MAX({rg}))',
            }
            if v is not None:
                f["CV zw. Zyklen [%]"] = f'=IF(OR({nr}<2,{mr}=0),"",{sr}/ABS({mr})*100)'
                f["Zyklen mit kleinstem Wert"] = (f"=COUNTIF(${L(c_min)}${first}:${L(c_min)}${max(last, first)},"
                                                  f"{cl}${hr})")
            for s, formula in f.items():
                cell = ws.cell(srow[s], c, formula)
                cell.fill = AGG
                cell.border = BOX
                cell.number_format = ("0" if s in ("n", "Zyklen mit kleinstem Wert") else
                                      nf(min(nd + 2, 6)) if s == "Varianz" else
                                      "0.0" if s == "CV zw. Zyklen [%]" else nf(nd))
            if v is not None:
                for s in stats:
                    agg[(key, v, s)] = f"Zyklusvergleich!${cl}${srow[s]}"
        r += 2
    ws.freeze_panes = "B4"
    ws.column_dimensions["A"].width = 24
    for c in range(2, c_note):
        ws.column_dimensions[L(c)].width = 16
    ws.column_dimensions[L(c_note)].width = 42

    # ---------------- Blatt Übersicht ----------------
    wo = ws_over
    wo.cell(1, 1, f"{cfg['benchmark']}: Gesamtauswertung je Treiber").font = Font(bold=True, size=13)
    wo.cell(2, 1, f"Mittelwerte über alle verwerteten Zyklen; Lastfenster {win_txt}. "
                  f"Alle Zahlen sind Verweise auf das Blatt 'Zyklusvergleich'.")
    stat_cols = ["Mittelwert", "Standardabweichung", "Varianz", "CV zw. Zyklen [%]", "Spannweite",
                 "KI unten", "KI oben", "Median", "n"]
    short = {"Mittelwert": "Mittel", "Standardabweichung": "SD", "Varianz": "Varianz",
             "CV zw. Zyklen [%]": "CV [%]", "Spannweite": "Spannweite", "KI unten": "95-%-KI unten",
             "KI oben": "95-%-KI oben", "Median": "Median", "n": "n"}
    hdr(wo, 4, 1, "Kennzahl")
    wo.merge_cells(start_row=4, start_column=1, end_row=5, end_column=1)
    for j, v in enumerate(versions):
        c0 = 2 + j * len(stat_cols)
        hdr(wo, 4, c0, labels[v])
        wo.merge_cells(start_row=4, start_column=c0, end_row=4, end_column=c0 + len(stat_cols) - 1)
        for i, s in enumerate(stat_cols):
            hdr(wo, 5, c0 + i, short[s])
    r = 6
    for key, name, nd in keys:
        wo.cell(r, 1, name).border = BOX
        for j, v in enumerate(versions):
            for i, s in enumerate(stat_cols):
                cell = wo.cell(r, 2 + j * len(stat_cols) + i, f"={agg[(key, v, s)]}")
                cell.number_format = ("0" if s == "n" else nf(min(nd + 2, 6)) if s == "Varianz"
                                      else "0.0" if s == "CV zw. Zyklen [%]" else nf(nd))
                cell.border = BOX
        r += 1
    r += 2
    wo.cell(r, 1, "Rangfolge nach Mehrverbrauch unter Last (1 = sparsamster Treiber)").font = Font(bold=True, size=12)
    r += 1
    for c, t in enumerate(["Treiber", "Delta Mittel [W]", "Rang", "Abstand zum sparsamsten [W]",
                           "Zyklen mit kleinstem Delta"], start=1):
        hdr(wo, r, c, t)
    r += 1
    r0, r1 = r, r + nv - 1
    for v in versions:
        wo.cell(r, 1, labels[v]).border = BOX
        wo.cell(r, 2, f"={agg[('delta', v, 'Mittelwert')]}").number_format = "0.00"
        wo.cell(r, 3, f'=IF(B{r}="","",RANK(B{r},$B${r0}:$B${r1},1))')
        wo.cell(r, 4, f'=IF(B{r}="","",B{r}-MIN($B${r0}:$B${r1}))').number_format = "0.00"
        wo.cell(r, 5, f"={agg[('delta', v, 'Zyklen mit kleinstem Wert')]}")
        for c in range(2, 6):
            wo.cell(r, c).border = BOX
        r += 1
    r += 2
    wo.cell(r, 1, "Erläuterung der Spalten").font = Font(bold=True, size=12)
    legend = [
        ("Mittel", "Mittelwert über alle verwerteten Zyklen."),
        ("SD", "Standardabweichung: typische Abweichung eines einzelnen Zyklus vom Mittelwert, in der "
               "Einheit der Kennzahl. Klein = Treiber verhält sich von Zyklus zu Zyklus gleich."),
        ("Varianz", "Quadrat der SD (Einheit zum Quadrat, z. B. W²). Gleiche Aussage wie die SD, aber "
                    "schwerer zu deuten; Grundlage vieler statistischer Tests."),
        ("CV [%]", "Variationskoeffizient = SD / Mittelwert × 100. Relative Streuung; vergleichbar "
                   "zwischen Treibern und Kennzahlen mit unterschiedlichem Niveau."),
        ("Spannweite", "Größter minus kleinster Zykluswert: die gesamte beobachtete Schwankungsbreite."),
        ("95-%-KI unten/oben", "Konfidenzintervall des MITTELWERTS: Bereich, in dem der wahre Mittelwert "
                               "mit 95 % Sicherheit liegt. Beschreibt die Genauigkeit des Mittelwerts, nicht "
                               "die Schwankung der Einzelwerte. Überlappen sich die Intervalle zweier Treiber "
                               "nicht, unterscheiden sie sich deutlich."),
        ("Median", "mittlerer Wert der sortierten Zykluswerte, unempfindlich gegen Ausreißer."),
        ("n", "Anzahl verwerteter Zyklen."),
    ]
    for term, text in legend:
        r += 1
        wo.cell(r, 1, term).font = bold
        wo.cell(r, 2, text)
    wo.column_dimensions["A"].width = 30
    for c in range(2, 2 + nv * len(stat_cols)):
        wo.column_dimensions[L(c)].width = 11
    wo.freeze_panes = "B6"

    # ---------------- Blatt Stabilität ----------------
    wsb = wb.create_sheet("Stabilität", 1)
    wsb.cell(1, 1, f"{cfg['benchmark']}: Stabilität der Treiber").font = Font(bold=True, size=13)
    wsb.cell(2, 1, "Innerhalb = wie ruhig die Leistung während einer Phase ist (Mittel über die Zyklen). "
                   "Zwischen = wie gut sich das Zyklusergebnis wiederholt. Kleinere Werte = stabiler. "
                   "Alle Zahlen sind Verweise auf 'Zyklusvergleich'.")
    scols = [("Treiber", None, None),
             ("SD in der Last [W]", "load_std", "Mittelwert"), ("CV in der Last [%]", "load_cv", "Mittelwert"),
             ("SD im Leerlauf [W]", "idle_std", "Mittelwert"),
             ("Last Ø: SD zw. Zyklen [W]", "load_mean", "Standardabweichung"),
             ("Last Ø: Varianz [W²]", "load_mean", "Varianz"),
             ("Last Ø: CV zw. Zyklen [%]", "load_mean", "CV zw. Zyklen [%]"),
             ("Last Ø: Spannweite [W]", "load_mean", "Spannweite"),
             ("Delta: SD zw. Zyklen [W]", "delta", "Standardabweichung"),
             ("Delta: Varianz [W²]", "delta", "Varianz"),
             ("Delta: CV zw. Zyklen [%]", "delta", "CV zw. Zyklen [%]"),
             ("Delta: Spannweite [W]", "delta", "Spannweite"),
             ("Rang Ruhe in der Last (1 = ruhigste)", None, None),
             ("Rang Wiederholbarkeit Last (1 = stabilste)", None, None)]
    for c, (t, _, _) in enumerate(scols, start=1):
        hdr(wsb, 4, c, t)
        wsb.column_dimensions[L(c)].width = 14
    wsb.column_dimensions["A"].width = 22
    wsb.row_dimensions[4].height = 48
    r0, r1 = 5, 4 + nv
    for i, v in enumerate(versions):
        r = 5 + i
        wsb.cell(r, 1, labels[v]).border = BOX
        for c, (t, key, st) in enumerate(scols[1:12], start=2):
            cell = wsb.cell(r, c, f"={agg[(key, v, st)]}")
            cell.border = BOX
            cell.number_format = "0.0000" if "Varianz" in t else "0.00" if "[%]" in t else "0.000"
        wsb.cell(r, 13, f'=IF(C{r}="","",RANK(C{r},$C${r0}:$C${r1},1))').border = BOX
        wsb.cell(r, 14, f'=IF(G{r}="","",RANK(G{r},$G${r0}:$G${r1},1))').border = BOX
    r = r1 + 3
    wsb.cell(r, 1, "Unterscheidet sich die Stabilität signifikant?").font = Font(bold=True, size=12)
    wsb.cell(r + 1, 1, "Berechnet in Python mit scipy. Innerhalb: Kruskal-Wallis und Welch-Test auf die SD je "
                       "Zyklus. Zwischen: Brown-Forsythe-Test (Levene mit Median) auf Gleichheit der Streuung. "
                       "Paarweise p-Werte Holm-korrigiert.")
    r += 3
    for c, t in enumerate(["Prüfgröße", "Test", "p global", "Vergleich", "Kennwert", "p (Holm)",
                           "signifikant (5 %)"], start=1):
        hdr(wsb, r, c, t)
    r += 1
    for t in stab_tests:
        for a, b, q, p in t["pairs"]:
            vals = [t["name"], t["test"], t["p_global"], f"{labels[b]} vs. {labels[a]}", q, p]
            for c, val in enumerate(vals, start=1):
                if isinstance(val, (float, np.floating)):
                    val = None if not np.isfinite(val) else float(val)
                cell = wsb.cell(r, c, val)
                if c in (3, 6):
                    cell.number_format = "0.0000"
                elif c == 5:
                    cell.number_format = "0.000"
            wsb.cell(r, 7, f'=IF(F{r}="","",IF(F{r}<0.05,"ja","nein"))')
            r += 1
    r += 1
    wsb.cell(r, 1, "Kennwert: bei 'innerhalb' die Differenz der mittleren SD (B − A, in W); bei "
                   "'Wiederholbarkeit' das Verhältnis der SD (B / A, 2 = doppelt so starke Streuung).")

    # ---------------- Blatt Zyklen_Detail ----------------
    wd = wb.create_sheet("Zyklen_Detail")
    cols = [("version", "Treiber", None), ("cycle", "Zyklus", 0), ("start_min", "Lastbeginn [min]", 2),
            ("load_dur_s", "Lastdauer [s]", 1), ("period_s", "Abstand zum Vorzyklus [s]", 1),
            ("complete", "vollständig", None), ("outlier", "Ausreißer", None), ("used", "verwertet", None),
            ("idle_pre_mean", "Leerlauf vor Ø [W]", 3), ("idle_pre_median", "Leerlauf vor Median [W]", 3),
            ("idle_post_mean", "Leerlauf nach Ø [W]", 3), ("idle_post_median", "Leerlauf nach Median [W]", 3),
            ("idle_mean", "Leerlauf Ø [W]", 3), ("idle_median", "Leerlauf Median [W]", 3),
            ("idle_drift", "Leerlauf nach − vor [W]", 3), ("idle_spikes", "Leerlaufspitzen [Anzahl]", 0),
            ("idle_spike_max", "größte Leerlaufspitze [W]", 1),
            ("load_mean", "Last Ø [W]", 3), ("load_median", "Last Median [W]", 3), ("load_std", "Last SD [W]", 3),
            ("load_cv", "CV Last [%]", 2), ("load_max", "Last Max [W]", 2), ("load_p95", "Last P95 [W]", 2),
            ("ramp_peak", "Einschwingspitze [W]", 2), ("load_window_s", "Lastfenster [s]", 1),
            ("energy_load_Wh", "Energie Lastfenster [Wh]", 5), ("delta_mean", "Delta (Mittelwerte) [W]", 3),
            ("delta_robust", "Delta (Mediane) [W]", 3)]
    if eff_meta:
        cols += [("perf", eff_meta["name"], 2), ("eff", f"Effizienz [{eff_meta['unit']}]", 4)]
    for c, (_, title, _) in enumerate(cols, start=1):
        hdr(wd, 1, c, title)
        wd.column_dimensions[L(c)].width = 14
    for ri, (_, row) in enumerate(table.iterrows(), start=2):
        for c, (key, _, nd) in enumerate(cols, start=1):
            val = row[key]
            if key == "version":
                val = labels[val]
            elif key in ("complete", "outlier", "used"):
                val = "ja" if bool(val) else "nein"
            elif isinstance(val, (float, np.floating)):
                val = None if not np.isfinite(val) else round(float(val), 6)
            elif isinstance(val, (np.integer,)):
                val = int(val)
            cell = wd.cell(ri, c, val)
            if nd is not None:
                cell.number_format = nf(nd)
    wd.freeze_panes = "C2"
    wd.row_dimensions[1].height = 45

    # ---------------- Blatt Tests ----------------
    wt = wb.create_sheet("Tests")
    wt.cell(1, 1, "Statistische Tests zwischen den Treibern").font = Font(bold=True, size=13)
    wt.cell(2, 1, "Berechnet in Python mit scipy: Kruskal-Wallis, Alexander-Govern (Welch-ANOVA), "
                  "Welch-t-Test, Mann-Whitney-U; paarweise p-Werte Holm-korrigiert. "
                  "KI der Differenz nach Welch-Satterthwaite. Hedges g = Effektstärke.")
    r = 4
    for c, t in enumerate(["Kennzahl", "Kruskal-Wallis p", "Welch-ANOVA p"], start=1):
        hdr(wt, r, c, t)
    r += 1
    for key, gt in global_res.items():
        wt.cell(r, 1, key)
        wt.cell(r, 2, gt["kruskal"] if np.isfinite(gt["kruskal"]) else None).number_format = "0.0000"
        wt.cell(r, 3, gt["anova"] if np.isfinite(gt["anova"]) else None).number_format = "0.0000"
        r += 1
    r += 1
    tcols = [("metric", "Kennzahl", None), ("a", "Treiber A", None), ("b", "Treiber B", None),
             ("n_a", "n A", 0), ("n_b", "n B", 0), ("mean_a", "Mittel A", 4), ("mean_b", "Mittel B", 4),
             ("diff", "Differenz B − A", 4), ("rel", "relativ [%]", 2), ("ci_lo", "KI unten", 4),
             ("ci_hi", "KI oben", 4), ("p_welch_holm", "p Welch (Holm)", 4), ("p_mw_holm", "p MWU (Holm)", 4),
             ("g", "Hedges g", 2), ("effect", "Effekt", None)]
    for c, (_, t, _) in enumerate(tcols, start=1):
        hdr(wt, r, c, t)
        wt.column_dimensions[L(c)].width = 14
    hdr(wt, r, len(tcols) + 1, "signifikant (5 %)")
    r += 1
    for cr in comp_rows:
        for c, (key, _, nd) in enumerate(tcols, start=1):
            val = cr[key]
            if key in ("a", "b"):
                val = labels[val]
            elif isinstance(val, (float, np.floating)):
                val = None if not np.isfinite(val) else float(val)
            cell = wt.cell(r, c, val)
            if nd is not None:
                cell.number_format = nf(nd)
        pc = L(12)
        wt.cell(r, len(tcols) + 1, f'=IF({pc}{r}="","",IF({pc}{r}<0.05,"ja","nein"))')
        r += 1

    # ---------------- Blatt Trend ----------------
    wr = wb.create_sheet("Trend")
    wr.cell(1, 1, "Entwicklung über die Zyklen (lineare Regression je Treiber)").font = Font(bold=True, size=13)
    wr.cell(2, 1, "Berechnet in Python (scipy.stats.linregress). p < 0,05 und merkliche Summe über den Lauf "
                  "deuten auf Aufwärm- oder Temperatureffekte.")
    for c, t in enumerate(["Treiber", "Größe", "Steigung je Zyklus [W]", "Summe über Lauf [W]", "R²", "p",
                           "Zyklus 1 − übrige [W]"], start=1):
        hdr(wr, 4, c, t)
        wr.column_dimensions[L(c)].width = 16
    for ri, tr in enumerate(trend_rows, start=5):
        vals = [labels[tr["version"]], tr["name"], tr["slope"], tr["total"], tr["r2"], tr["p"], tr["warm"]]
        for c, val in enumerate(vals, start=1):
            if isinstance(val, (float, np.floating)):
                val = None if not np.isfinite(val) else float(val)
            cell = wr.cell(ri, c, val)
            if c >= 3:
                cell.number_format = "0.0000"

    # ---------------- Blatt Info ----------------
    wi = wb.create_sheet("Info")
    wi.cell(1, 1, "Messaufbau, Parameter und Datenqualität").font = Font(bold=True, size=13)
    info = [("Benchmark", cfg["benchmark"]), ("Erwartete Lastdauer [s]", cfg["load"]),
            ("Erwarteter Leerlauf je Seite [s]", cfg["idle"]), ("Erwartete Zyklen", cfg["cycles"]),
            ("Lastfenster", win_txt), ("Einschwingzeit übersprungen [s]", cfg["skip_ramp"]),
            ("Abstand Leerlauffenster zu Flanken [s]", cfg["guard"]),
            ("Länge Leerlauffenster [s]", cfg["idle_window"]), ("Vergleichsbasis", labels[baseline])]
    for i, (k, v) in enumerate(info, start=3):
        wi.cell(i, 1, k).font = bold
        wi.cell(i, 2, v)
    r = len(info) + 5
    for c, t in enumerate(["Treiber", "Datei", "Messwerte", "Dauer [min]", "Δt Median [s]", "größte Lücke [s]",
                           "Schwelle [W]", "Leerlaufniveau [W]", "Lastniveau [W]", "Trennschärfe",
                           "Lastphasen erkannt"], start=1):
        hdr(wi, r, c, t)
        wi.column_dimensions[L(c)].width = 16
    wi.column_dimensions["A"].width = 38
    wi.column_dimensions["B"].width = 34
    r += 1
    for v in versions:
        q = res[v]["q"]
        vals = [labels[v], os.path.basename(res[v]["path"]), q["n"], q["duration_s"] / 60, q["dt_median"],
                q["dt_max"], res[v]["thr"], res[v]["lo"], res[v]["hi"], res[v]["sep"], len(res[v]["segs"])]
        for c, val in enumerate(vals, start=1):
            cell = wi.cell(r, c, float(val) if isinstance(val, (np.floating,)) else val)
            if isinstance(val, float):
                cell.number_format = "0.00"
        r += 1
    r += 1
    wi.cell(r, 1, "Hinweis").font = bold
    wi.cell(r, 2, "Die Zyklen eines Treibers stammen aus einer Sitzung und bilden die Streuung von Zyklus "
                  "zu Zyklus ab, nicht die zwischen Messtagen oder Neuinstallationen.")

    # Schrift einheitlich Arial
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                f = cell.font
                cell.font = Font(name="Arial", bold=f.bold, italic=f.italic,
                                 size=f.size if f.size else 10, color=f.color)
    for sheet in wb.worksheets:  # Druck: Querformat, auf Seitenbreite skaliert
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
    wb.calculation.fullCalcOnLoad = True
    wb.save(path)
    return True


# ---------------------------------------------------------------------------
# LaTeX: Gesamttabelle und Zyklustabelle
# ---------------------------------------------------------------------------

def latex_name(name):
    return name.replace(" Ø", "").replace("Ø ", "").replace("%", r"\%").replace("−", "--")


def write_latex_full(path, summary, versions, labels, cfg, keys):
    lines = [r"% benötigt \usepackage{booktabs}", r"\begin{table}[htbp]", r"\centering", r"\small",
             rf"\begin{{tabular}}{{l{'r' * len(versions)}}}", r"\toprule",
             "Kennzahl & " + " & ".join(labels[v] for v in versions) + r" \\", r"\midrule"]
    for key, name, nd in keys:
        cells = []
        for v in versions:
            d = summary[v].get(key)
            if not d or not np.isfinite(d["mean"]):
                cells.append("--")
            elif np.isfinite(d["sd"]):
                cells.append(f"{de(d['mean'], nd)} $\\pm$ {de(d['sd'], nd)}")
            else:
                cells.append(de(d["mean"], nd))
        lines.append(latex_name(name) + " & " + " & ".join(cells) + r" \\")
    n = [summary[v]["delta"]["n"] for v in versions]
    lines += [r"\midrule", "verwertete Zyklen & " + " & ".join(str(x) for x in n) + r" \\",
              r"\bottomrule", r"\end{tabular}",
              rf"\caption{{{cfg['benchmark']}: Gesamtauswertung je Treiber "
              rf"(Mittelwert $\pm$ Standardabweichung über die verwerteten Zyklen)}}", r"\end{table}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_latex_cycles(path, table, summary, versions, cfg):
    nv = len(versions)
    n = int(table.cycle.max()) if len(table) else 0
    lines = [r"% benötigt \usepackage{booktabs}", r"\begin{table}[htbp]", r"\centering", r"\small",
             rf"\begin{{tabular}}{{r{'r' * nv}{'r' * nv}}}", r"\toprule",
             rf" & \multicolumn{{{nv}}}{{c}}{{Last [W]}} & \multicolumn{{{nv}}}{{c}}{{Mehrverbrauch Delta [W]}} \\",
             rf"\cmidrule(lr){{2-{1 + nv}}}\cmidrule(lr){{{2 + nv}-{1 + 2 * nv}}}",
             "Zyklus & " + " & ".join(versions) + " & " + " & ".join(versions) + r" \\", r"\midrule"]
    for k in range(1, n + 1):
        cells = []
        for key in ("load_mean", "delta"):
            for v in versions:
                row = cycle_row(table, v, k)
                if row is None or not np.isfinite(row[key]):
                    cells.append("--")
                elif row.used:
                    cells.append(de(row[key]))
                else:
                    cells.append(f"({de(row[key])})")
        lines.append(f"{k} & " + " & ".join(cells) + r" \\")
    lines.append(r"\midrule")
    for stat, lab in (("mean", "Mittel"), ("sd", "SD")):
        cells = [de(summary[v][key][stat]) for key in ("load_mean", "delta") for v in versions]
        lines.append(f"{lab} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}",
              rf"\caption{{{cfg['benchmark']}: Last und Mehrverbrauch je Zyklus und Treiber. "
              r"Werte in Klammern wurden nicht verwertet (unvollständige Lastphase).}", r"\end{table}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")



# ---------------------------------------------------------------------------
# Stabilität: innerhalb der Lastphase und zwischen den Zyklen
# ---------------------------------------------------------------------------

def spread_stats(values):
    """describe() ergänzt um Varianz und Spannweite."""
    d = describe(values)
    d["var"] = d["sd"] ** 2 if np.isfinite(d["sd"]) else np.nan
    d["range"] = d["max"] - d["min"] if d["n"] >= 1 else np.nan
    return d


def stability_analysis(table, versions):
    """Kennzahlen und Tests zur Stabilität der Treiber."""
    stab = {}
    for v in versions:
        sub = table[(table.version == v) & table.used]
        stab[v] = {
            "sd_in_load": float(np.nanmean(sub.load_std)) if len(sub) else np.nan,
            "cv_in_load": float(np.nanmean(sub.load_cv)) if len(sub) else np.nan,
            "sd_in_idle": float(np.nanmean(sub.idle_std)) if len(sub) else np.nan,
            "load": spread_stats(sub.load_mean),
            "delta": spread_stats(sub.delta),
            "idle": spread_stats(sub.idle_mean),
        }
    tests = []
    pairs = list(itertools.combinations(versions, 2))
    groups = lambda col: [clean(table.loc[(table.version == v) & table.used, col]) for v in versions]

    # 1) Schwankt die Leistung INNERHALB der Lastphase bei einem Treiber stärker?
    g = groups("load_std")
    p_glob = float("nan")
    if HAVE_SCIPY and all(len(x) >= 2 for x in g):
        try:
            p_glob = float(sps.kruskal(*g).pvalue)
        except ValueError:
            pass
    res = [compare(table.loc[(table.version == a) & table.used, "load_std"],
                   table.loc[(table.version == b) & table.used, "load_std"]) for a, b in pairs]
    adj = holm([r["p_welch"] if r else np.nan for r in res])
    tests.append({"name": "Schwankung innerhalb der Last (SD je Zyklus)", "test": "Kruskal-Wallis / Welch",
                  "p_global": p_glob,
                  "pairs": [(a, b, r["diff"] if r else np.nan, p) for (a, b), r, p in zip(pairs, res, adj)]})

    # 2) Wiederholt sich das Ergebnis bei einem Treiber schlechter? (Gleichheit der Varianzen)
    for col, name in (("load_mean", "Wiederholbarkeit Last Ø zwischen den Zyklen"),
                      ("delta", "Wiederholbarkeit Mehrverbrauch zwischen den Zyklen")):
        g = groups(col)
        p_glob = float("nan")
        pair_p = []
        if HAVE_SCIPY and all(len(x) >= 2 for x in g):
            try:
                p_glob = float(sps.levene(*g, center="median").pvalue)
            except ValueError:
                pass
            for a, b in pairs:
                ga = clean(table.loc[(table.version == a) & table.used, col])
                gb = clean(table.loc[(table.version == b) & table.used, col])
                try:
                    pair_p.append(float(sps.levene(ga, gb, center="median").pvalue))
                except ValueError:
                    pair_p.append(np.nan)
        else:
            pair_p = [np.nan] * len(pairs)
        adj = holm(pair_p)
        ratio = []
        for a, b in pairs:
            sa, sb = stab[a][("load" if col == "load_mean" else "delta")]["sd"], \
                     stab[b][("load" if col == "load_mean" else "delta")]["sd"]
            ratio.append(sb / sa if (np.isfinite(sa) and sa > 0 and np.isfinite(sb)) else np.nan)
        tests.append({"name": name, "test": "Brown-Forsythe (Levene, Median)", "p_global": p_glob,
                      "pairs": [(a, b, q, p) for (a, b), q, p in zip(pairs, ratio, adj)]})
    return stab, tests


def report_stability(stab, tests, versions, labels):
    heading("4b. Stabilität der Treiber", "-")
    log("Zwei Ebenen: (1) wie ruhig ist die Leistung INNERHALB einer Lastphase, (2) wie gut "
        "WIEDERHOLT sich das Ergebnis von Zyklus zu Zyklus.")
    log("SD = Standardabweichung [W]; Varianz = SD² [W²]; CV = SD / Mittelwert [%] (vergleichbar "
        "zwischen Treibern); Spannweite = größter minus kleinster Zykluswert [W].")
    log("")
    log("(1) Schwankung innerhalb der Phasen (Mittel über die Zyklen):")
    log(f"  {'Treiber':<24}{'SD in der Last':>16}{'CV in der Last':>16}{'SD im Leerlauf':>16}")
    for v in versions:
        s = stab[v]
        log(f"  {labels[v]:<24}{fmt(s['sd_in_load'], 3, ' W'):>16}{fmt(s['cv_in_load'], 2, ' %'):>16}"
            f"{fmt(s['sd_in_idle'], 3, ' W'):>16}")
    for key, title in (("load", "Last Ø"), ("delta", "Mehrverbrauch Delta")):
        log("")
        log(f"(2) Wiederholbarkeit zwischen den Zyklen – {title}:")
        log(f"  {'Treiber':<24}{'n':>4}{'SD':>10}{'Varianz':>12}{'CV':>10}{'Spannweite':>12}{'Min':>10}{'Max':>10}")
        for v in versions:
            d = stab[v][key]
            log(f"  {labels[v]:<24}{d['n']:>4}{fmt(d['sd'], 3):>10}{fmt(d['var'], 4):>12}"
                f"{fmt(d['cv'], 2, ' %'):>10}{fmt(d['range'], 3):>12}{fmt(d['min'], 2):>10}{fmt(d['max'], 2):>10}")
    log("")
    log("Tests: Unterscheidet sich die Stabilität der Treiber signifikant? (paarweise Holm-korrigiert)")
    for t in tests:
        log(f"  {t['name']}  [{t['test']}]  global p = {fmt_p(t['p_global'])}")
        for a, b, q, p in t["pairs"]:
            what = ("Differenz der SD" if "innerhalb" in t["name"] else "Verhältnis der SD")
            unit = " W" if "innerhalb" in t["name"] else "×"
            sig = " *" if np.isfinite(p) and p < 0.05 else ""
            log(f"      {labels[b]} vs. {labels[a]}: {what} {fmt(q, 3)}{unit}, p = {fmt_p(p)}{sig}")
    ok = [v for v in versions if np.isfinite(stab[v]["cv_in_load"])]
    if ok:
        calm = min(ok, key=lambda v: stab[v]["cv_in_load"])
        rep = min(ok, key=lambda v: stab[v]["load"]["cv"] if np.isfinite(stab[v]["load"]["cv"]) else np.inf)
        log("")
        log(f"Ruhigste Leistungsaufnahme innerhalb der Last: {labels[calm]} "
            f"(CV {fmt(stab[calm]['cv_in_load'], 2)} %).")
        log(f"Beste Wiederholbarkeit der Last zwischen den Zyklen: {labels[rep]} "
            f"(CV {fmt(stab[rep]['load']['cv'], 2)} %).")


def write_latex_stability(path, stab, versions, labels, cfg):
    lines = [r"% benötigt \usepackage{booktabs}", r"\begin{table}[htbp]", r"\centering", r"\small",
             r"\begin{tabular}{lrrrrrrr}", r"\toprule",
             r" & \multicolumn{2}{c}{innerhalb der Last} & \multicolumn{3}{c}{Last zwischen Zyklen} "
             r"& \multicolumn{2}{c}{Delta zwischen Zyklen} \\",
             r"\cmidrule(lr){2-3}\cmidrule(lr){4-6}\cmidrule(lr){7-8}",
             r"Treiber & SD [W] & CV [\%] & SD [W] & CV [\%] & Spannw. [W] & SD [W] & Spannw. [W] \\",
             r"\midrule"]
    for v in versions:
        s = stab[v]
        lines.append(f"{labels[v]} & {de(s['sd_in_load'], 3)} & {de(s['cv_in_load'])} & "
                     f"{de(s['load']['sd'], 3)} & {de(s['load']['cv'])} & {de(s['load']['range'])} & "
                     f"{de(s['delta']['sd'], 3)} & {de(s['delta']['range'])}" + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}",
              rf"\caption{{{cfg['benchmark']}: Stabilität der Treiber. Links die Schwankung der Leistung "
              r"innerhalb der Lastphase (Mittel über die Zyklen), rechts die Streuung der Zyklusmittelwerte "
              r"(Wiederholbarkeit).}", r"\end{table}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Zwei Zyklen im direkten Vergleich
# ---------------------------------------------------------------------------

def _ylim(res, versions, cycles):
    vals = [res[v]["prof"][k - 1] for v in versions for k in cycles if k - 1 < len(res[v]["prof"])]
    if not vals:
        return None
    allv = np.concatenate([x[np.isfinite(x)] for x in vals])
    return (max(0, np.nanmin(allv) - 2), np.nanmax(allv) + 4)


def plot_cycle_pair(res, table, versions, labels, cfg, cycles, path):
    """Zyklus A und Zyklus B untereinander, in jedem Feld alle Treiber übereinander."""
    fig = plt.figure(figsize=(12, 11))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1.05, 3, 1.05], hspace=0.45)
    ylim = _ylim(res, versions, cycles)
    for i, k in enumerate(cycles):
        ax = fig.add_subplot(gs[2 * i])
        phase_shading(ax, cfg, with_labels=True)
        cells, rl, rc = [], [], []
        for v in versions:
            row = cycle_row(table, v, k)
            if row is None:
                continue
            ax.plot(res[v]["rel"], res[v]["prof"][k - 1], lw=1.25, ls="-" if row.used else "--",
                    color=color_of(v, versions), label=labels[v], zorder=2)
            cells.append([fmt(row.idle_pre_mean), fmt(row.load_mean), fmt(row.idle_post_mean), fmt(row.delta),
                          fmt(row.load_std, 3), fmt(row.load_cv, 2), status_of(row)])
            rl.append(labels[v])
            rc.append(color_of(v, versions))
        if ylim:
            ax.set_ylim(*ylim)
        ax.set_ylabel("Leistung [W]")
        ax.set_title(f"Zyklus {k}", fontsize=12, fontweight="bold", pad=18)
        ax.grid(alpha=0.3)
        ax.legend(loc="center right", fontsize=9)
        if i == len(cycles) - 1:
            ax.set_xlabel("Zeit relativ zum Lastbeginn [s]")
        tax = fig.add_subplot(gs[2 * i + 1])
        tax.axis("off")
        if cells:
            tbl = tax.table(cellText=cells, rowLabels=rl, loc="center", cellLoc="center",
                            colLabels=["Leerlauf vor [W]", "Last Ø [W]", "Leerlauf nach [W]", "Delta [W]",
                                       "SD Last [W]", "CV Last [%]", "Status"])
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.scale(1, 1.35)
            for j, c in enumerate(rc, start=1):
                tbl[(j, -1)].get_text().set_color(c)
                tbl[(j, -1)].get_text().set_fontweight("bold")
    fig.suptitle(f"{cfg['benchmark']}: Zyklus {cycles[0]} und Zyklus {cycles[1]} im Vergleich\n"
                 f"(alle Treiber übereinander, gleiche Achsen)", fontsize=13)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def plot_cycle_pair_per_driver(res, table, versions, labels, cfg, cycles, path):
    """Je Treiber Zyklus A gegen Zyklus B – zeigt, wie stabil ein Treiber über den Lauf ist."""
    fig, axes = plt.subplots(1, len(versions), figsize=(5.2 * len(versions), 4.6), sharey=True, squeeze=False)
    ylim = _ylim(res, versions, cycles)
    for ax, v in zip(axes[0], versions):
        phase_shading(ax, cfg)
        texts = []
        for k, ls, alpha in ((cycles[0], "-", 1.0), (cycles[1], "-", 0.55)):
            row = cycle_row(table, v, k)
            if row is None:
                continue
            col = color_of(v, versions) if k == cycles[0] else "#333333"
            ax.plot(res[v]["rel"], res[v]["prof"][k - 1], lw=1.1, ls=ls, color=col, alpha=alpha,
                    label=f"Zyklus {k}", zorder=2)
            texts.append((col, f"Zyklus {k}: Last {row.load_mean:.2f} W, Δ {row.delta:.2f} W"))
        for i, (col, t) in enumerate(texts):
            ax.text(0.02, 0.95 - 0.08 * i, t, transform=ax.transAxes, fontsize=8.5, ha="left", va="top",
                    color=col, bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=1.5))
        if ylim:  # oben Platz für die Wertezeilen lassen
            ax.set_ylim(ylim[0], ylim[1] + 0.3 * (ylim[1] - ylim[0]))
        ax.set_title(labels[v], fontsize=11, color=color_of(v, versions), fontweight="bold")
        ax.set_xlabel("Zeit relativ zum Lastbeginn [s]")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
    axes[0, 0].set_ylabel("Leistung [W]")
    fig.suptitle(f"{cfg['benchmark']}: je Treiber Zyklus {cycles[0]} gegen Zyklus {cycles[1]}", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)



# ---------------------------------------------------------------------------
# Kurztabellen für die schriftliche Arbeit: Leerlauf vor / Last / Leerlauf nach
# ---------------------------------------------------------------------------

PHASES = [("pre", "Leerlauf vor", "pre_a", "pre_b"),
          ("load", "Last (Plateau)", "load_a", "load_b"),
          ("post", "Leerlauf nach", "post_a", "post_b")]


def phase_pool(r, rows, a_key, b_key):
    """Kennzahlen über ALLE Messpunkte einer Phase aus allen verwerteten Zyklen.
    Mittel/Median/Std/Min/Max aus den Rohwerten des Messgeräts, Energie je Zyklus aus
    dem Integral über das jeweilige Auswertefenster (Mittel über die Zyklen)."""
    ts, ps = r["df"]["seconds"].values, r["df"]["power"].values
    vals, energies, durs = [], [], []
    for _, x in rows.iterrows():
        a, b = x[a_key], x[b_key]
        if not (np.isfinite(a) and np.isfinite(b)) or b - a < 10:
            continue
        m = (ts >= a) & (ts <= b)
        vals.append(ps[m])
        tw, pw = window(r["t"], r["p"], a, b)
        if len(pw) >= 4:
            energies.append(float(_trapz(pw, tw)) / 3600.0)
            durs.append(b - a)
    if not vals:
        return None
    v = np.concatenate(vals)
    mean = float(v.mean())
    std = float(v.std(ddof=1)) if len(v) > 1 else float("nan")
    return {"mean": mean, "median": float(np.median(v)), "std": std,
            "cv": std / mean * 100 if mean else float("nan"),
            "min": float(v.min()), "max": float(v.max()),
            "energy": float(np.mean(energies)) if energies else float("nan"),
            "n_cycles": len(energies), "dur": float(np.mean(durs)) if durs else float("nan"),
            "n_samples": int(len(v))}


def phase_tables(res, table, versions):
    out = {}
    for key, name, a, b in PHASES:
        out[key] = {}
        for v in versions:
            rows = table[(table.version == v) & table.used]
            out[key][v] = phase_pool(res[v], rows, a, b)
    return out


def _row_de(label, s, e_nd=3):
    if s is None:
        return f"{label} & -- & -- & -- & -- & -- & -- & --"
    return (f"{label} & {de(s['mean'])} & {de(s['median'])} & {de(s['std'])} & {de(s['cv'])} & "
            f"{de(s['min'])} & {de(s['max'])} & {de(s['energy'], e_nd)}")


def kern(pt, v):
    """Kernaussage aus der Phasentabelle: Leerlauf = alle Messpunkte vor und nach der Last,
    damit beide Tabellen exakt zusammenpassen."""
    pre, post, load = pt["pre"][v], pt["post"][v], pt["load"][v]
    parts = [x for x in (pre, post) if x]
    if not parts or not load:
        return None
    n = sum(x["n_samples"] for x in parts)
    idle = sum(x["mean"] * x["n_samples"] for x in parts) / n
    return {"idle": idle, "load": load["mean"], "delta": load["mean"] - idle, "energy": load["energy"]}


def write_latex_short_tables(path, pt, summary, versions, labels, cfg, eff_meta):
    """Zwei Tabellen: (1) Leistung je Phase, (2) Kernaussage zum Energieverbrauch."""
    n = max((pt["load"][v]["n_cycles"] for v in versions if pt["load"][v]), default=0)
    win = {k: next((pt[k][v]["dur"] for v in versions if pt[k][v]), float("nan")) for k, *_ in PHASES}
    L = [r"% Kurztabellen: " + cfg["benchmark"],
         r"% benötigt in der Präambel: \usepackage{booktabs} und \usepackage{makecell}", ""]
    L += [r"\begin{table}[htbp]", r"\centering", r"\small", r"\setlength{\tabcolsep}{4pt}",
          r"\begin{tabular}{lrrrrrrr}", r"\toprule",
          r"Treiber & \makecell{Mittel\\{[}W{]}} & \makecell{Median\\{[}W{]}} & Std [W] & CV [\%] & "
          r"Min [W] & Max [W] & \makecell{Energie je\\Zyklus {[}Wh{]}} \\"]
    for key, name, *_ in PHASES:
        L += [r"\midrule", rf"\multicolumn{{8}}{{l}}{{\textit{{{name}}}}} \\"]
        for v in versions:
            s = pt[key][v]
            cells = ("-- & " * 6 + "--") if s is None else (
                f"{de(s['mean'])} & {de(s['median'])} & {de(s['std'])} & {de(s['cv'], 1)} & "
                f"{de(s['min'])} & {de(s['max'])} & {de(s['energy'], 3)}")
            L.append(f"{labels[v]} & {cells}" + r" \\")
    L += [r"\bottomrule", r"\end{tabular}",
          rf"\caption{{{cfg['benchmark']}: Leistungsaufnahme je Phase über {n} Zyklen je Treiber "
          rf"(Fenster je Zyklus: Leerlauf je {de(win['pre'], 0)}~s, Last {de(win['load'], 0)}~s).}}",
          r"\end{table}", ""]
    cols = "lrrrr" + ("r" if eff_meta else "")
    head = (r"Treiber & \makecell{Leerlauf\\{[}W{]}} & \makecell{Last\\{[}W{]}} & "
            r"\makecell{Mehrverbrauch\\{[}W{]}} & \makecell{Energie Last\\je Zyklus {[}Wh{]}}")
    if eff_meta:
        head += rf" & \makecell{{Effizienz\\{{[}}{eff_meta['unit']}{{]}}}}"
    L += [r"\begin{table}[htbp]", r"\centering", r"\small", rf"\begin{{tabular}}{{{cols}}}", r"\toprule",
          head + r" \\", r"\midrule"]
    for v in versions:
        k = kern(pt, v)
        if k is None:
            continue
        row = (f"{labels[v]} & {de(k['idle'])} & {de(k['load'])} & {de(k['delta'])} & "
               f"{de(k['energy'], 3)}")
        if eff_meta:
            row += f" & {de(summary[v]['eff']['mean'], 2)}"
        L.append(row + r" \\")
    L += [r"\bottomrule", r"\end{tabular}",
          rf"\caption{{{cfg['benchmark']}: Kernaussage zum Energieverbrauch, Mittelwerte über {n} Zyklen. "
          r"Mehrverbrauch = Last minus Leerlauf.}", r"\end{table}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def report_short_tables(pt, summary, versions, labels):
    heading("8. Kurztabellen (für die schriftliche Arbeit)", "-")
    log("Über alle Messpunkte der verwerteten Zyklen im jeweiligen Auswertefenster; "
        "Energie = Mittel je Zyklus für dieses Fenster.")
    log(f"  {'Phase':<15}{'Treiber':<24}{'Mittel':>8}{'Median':>8}{'Std':>7}{'CV %':>7}{'Min':>8}{'Max':>8}{'Wh/Zyklus':>11}")
    for key, name, *_ in PHASES:
        for j, v in enumerate(versions):
            s = pt[key][v]
            if s is None:
                continue
            log(f"  {name if j == 0 else '':<15}{labels[v]:<24}{s['mean']:>8.2f}{s['median']:>8.2f}{s['std']:>7.2f}"
                f"{s['cv']:>7.1f}{s['min']:>8.2f}{s['max']:>8.2f}{s['energy']:>11.3f}")
    log("")
    log(f"  {'Treiber':<24}{'Leerlauf':>10}{'Last':>9}{'Mehrverbrauch':>15}{'Wh Last/Zyklus':>16}")
    for v in versions:
        k = kern(pt, v)
        if k:
            log(f"  {labels[v]:<24}{k['idle']:>10.2f}{k['load']:>9.2f}{k['delta']:>15.2f}{k['energy']:>16.3f}")


def check_short_tables(pt, res, versions, labels):
    """Plausibilitätsprüfung: jeder Tabellenwert muss physikalisch zur Messdatei passen."""
    problems = []
    for key, name, *_ in PHASES:
        for v in versions:
            st = pt[key][v]
            if st is None:
                continue
            lo, hi = float(res[v]["df"]["power"].min()), float(res[v]["df"]["power"].max())
            for k in ("mean", "median", "min", "max"):
                if not (lo - 1e-9 <= st[k] <= hi + 1e-9):
                    problems.append(f"{name}/{labels[v]}: {k} = {st[k]:.2f} W außerhalb des Messbereichs {lo:.2f}–{hi:.2f} W")
            if not (st["min"] <= st["median"] <= st["max"] and st["min"] <= st["mean"] <= st["max"]):
                problems.append(f"{name}/{labels[v]}: Mittel/Median nicht zwischen Min und Max")
            expect = st["mean"] * st["dur"] / 3600.0
            if expect > 0 and abs(st["energy"] - expect) > 0.03 * expect:
                problems.append(f"{name}/{labels[v]}: Energie {st['energy']:.4f} Wh passt nicht zu "
                                f"Mittel × Dauer = {expect:.4f} Wh")
        loads = [pt["load"][v]["median"] for v in versions if pt["load"][v] and pt["pre"][v]]
        idles = [pt["pre"][v]["median"] for v in versions if pt["load"][v] and pt["pre"][v]]
    for v in versions:
        if pt["load"][v] and pt["pre"][v] and pt["load"][v]["median"] <= pt["pre"][v]["median"]:
            problems.append(f"{labels[v]}: Last-Median nicht über Leerlauf-Median – Erkennung prüfen")
    log("")
    if problems:
        log("PLAUSIBILITÄTSPRÜFUNG: PROBLEME GEFUNDEN")
        for x in problems:
            log("  - " + x)
    else:
        log("Plausibilitätsprüfung bestanden: alle Werte liegen im gemessenen Bereich, Median zwischen "
            "Min und Max, Energie passt zu Mittelwert × Fensterdauer.")
    return not problems


def write_short_xlsx(path, pt, summary, versions, labels, cfg, eff_meta, sources):
    """Kurzauswertung als Excel: echte Zahlen statt Text, daher unabhängig von Spracheinstellungen."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        log("HINWEIS: openpyxl fehlt – keine Kurzauswertung als Excel (pip install openpyxl).")
        return
    HDR = PatternFill("solid", fgColor="DCE6F1")
    thin = Side(style="thin", color="BFBFBF")
    BOX = Border(top=thin, bottom=thin, left=thin, right=thin)
    F = lambda **k: Font(name="Arial", size=k.pop("size", 10), **k)
    wb = Workbook()
    ws = wb.active
    ws.title = "Kurzauswertung"
    n = max((pt["load"][v]["n_cycles"] for v in versions if pt["load"][v]), default=0)
    win = {k: next((pt[k][v]["dur"] for v in versions if pt[k][v]), float("nan")) for k, *_ in PHASES}
    ws.cell(1, 1, f"{cfg['benchmark']}: Kurzauswertung").font = F(bold=True, size=13)
    ws.cell(2, 1, f"{n} Zyklen je Treiber. Auswertefenster je Zyklus: Leerlauf vor und nach je "
                  f"{win['pre']:.0f} s, Last {win['load']:.0f} s. Werte aus den Rohdaten des Messgeräts: "
                  + ", ".join(sources) + ".").font = F(size=9)

    def header(r, titles):
        for c, t in enumerate(titles, start=1):
            cell = ws.cell(r, c, t)
            cell.font = F(bold=True)
            cell.fill = HDR
            cell.border = BOX
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[r].height = 30

    r = 4
    ws.cell(r, 1, "Leistung je Phase").font = F(bold=True, size=11)
    r += 1
    header(r, ["Phase", "Treiber", "Mittel [W]", "Median [W]", "Std [W]", "CV [%]", "Min [W]", "Max [W]",
               "Energie je Zyklus [Wh]"])
    fmts = ["0.00", "0.00", "0.00", "0.0", "0.00", "0.00", "0.000"]
    for key, name, *_ in PHASES:
        for j, v in enumerate(versions):
            r += 1
            s = pt[key][v]
            ws.cell(r, 1, name if j == 0 else None).font = F(bold=True)
            ws.cell(r, 2, labels[v]).font = F()
            vals = [] if s is None else [s["mean"], s["median"], s["std"], s["cv"], s["min"], s["max"], s["energy"]]
            for c, (val, nfmt) in enumerate(zip(vals, fmts), start=3):
                cell = ws.cell(r, c, round(float(val), 4))
                cell.number_format = nfmt
                cell.font = F()
            for c in range(1, 10):
                ws.cell(r, c).border = BOX
    r += 3
    ws.cell(r, 1, "Kernaussage zum Energieverbrauch").font = F(bold=True, size=11)
    r += 1
    titles = ["Treiber", "Leerlauf [W]", "Last [W]", "Mehrverbrauch [W]", "Energie Last je Zyklus [Wh]"]
    if eff_meta:
        titles.append(f"Effizienz [{eff_meta['unit']}]")
    header(r, titles)
    for v in versions:
        r += 1
        k = kern(pt, v)
        ws.cell(r, 1, labels[v]).font = F()
        if k:
            ws.cell(r, 2, round(float(k["idle"]), 4)).number_format = "0.00"
            ws.cell(r, 3, round(float(k["load"]), 4)).number_format = "0.00"
            ws.cell(r, 4, f"=C{r}-B{r}").number_format = "0.00"
            ws.cell(r, 5, round(float(k["energy"]), 5)).number_format = "0.000"
        if eff_meta:
            ws.cell(r, 6, round(float(summary[v]["eff"]["mean"]), 4)).number_format = "0.00"
        for c in range(1, len(titles) + 1):
            ws.cell(r, c).border = BOX
            ws.cell(r, c).font = F()
    r += 2
    notes = [
        "Mittel, Median, Std, Min, Max: über alle Messpunkte (1 je Sekunde) der Phase aus allen Zyklen.",
        "CV = Std / Mittel × 100. Im Leerlauf hoch, weil kurze Windows-Hintergrundspitzen die Streuung prägen; "
        "daher dort besser den Median zitieren.",
        "Max im Leerlauf = einzelne Hintergrundspitzen, nicht Treiberverhalten.",
        "Kernaussage: Leerlauf = alle Messpunkte vor und nach der Last, Last = Plateau aus der Tabelle oben; "
        "Mehrverbrauch = Last minus Leerlauf (Formel). Energie = Integral der Leistung über das Fenster, "
        "Mittel je Zyklus.",
    ]
    for t in notes:
        ws.cell(r, 1, t).font = F(size=9, italic=True)
        r += 1
    widths = [16, 22, 11, 11, 10, 9, 10, 10, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.calculation.fullCalcOnLoad = True
    wb.save(path)



def parse_kv(items, what):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--{what} erwartet VERSION=WERT, nicht '{it}'")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Auswertung von Leistungslogs mit mehreren Lastzyklen je Treiber",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Beispiele")[-1])
    ap.add_argument("--preset", choices=PRESETS.keys(), default="nic",
                    help="Voreinstellung für Ablauf und Parameter (Default: nic)")
    ap.add_argument("--logdir", default=".", help="Ordner mit den Logdateien")
    ap.add_argument("--pattern", default="{version}.log",
                    help="Dateinamensmuster, z. B. '{version}_download.log'")
    ap.add_argument("--file", action="append", metavar="VERSION=PFAD",
                    help="Logdatei direkt angeben (mehrfach möglich)")
    ap.add_argument("--versions", nargs="+", default=DEFAULT_VERSIONS,
                    help="Treiberkennungen in Anzeigereihenfolge (Default: ALT MITTEL NEU)")
    ap.add_argument("--label", action="append", metavar="VERSION=TEXT",
                    help="Anzeigename, z. B. --label ALT='ALT (25.5)'")
    ap.add_argument("--perf", action="append", metavar="VERSION=CSV",
                    help="CSV des Benchmark-Skripts mit Mbps/FPS/Score je Zyklus")
    ap.add_argument("--outdir", default="auswertung", help="Ausgabeordner")
    ap.add_argument("--tag", default=None, help="Kürzel für Ausgabedateien (Default: Preset)")
    ap.add_argument("--title", default=None, help="Bezeichnung des Benchmarks in Bericht und Plots")
    ap.add_argument("--load-seconds", type=float, help="erwartete Lastdauer je Zyklus [s]")
    ap.add_argument("--idle-seconds", type=float, help="Leerlauf vor und nach der Last [s]")
    ap.add_argument("--cycles", type=int, help="erwartete Zyklen je Datei")
    ap.add_argument("--skip-ramp", type=float, help="Einschwingzeit am Lastbeginn, wird übersprungen [s]")
    ap.add_argument("--guard", type=float, default=5.0, help="Abstand der Leerlauffenster zu den Lastflanken [s]")
    ap.add_argument("--smooth", type=float, help="Glättungsfenster für die Lasterkennung [s]")
    ap.add_argument("--min-load", type=float, help="Mindestdauer einer Lastphase [s] (Default: halbe Lastdauer)")
    ap.add_argument("--merge-gap", type=float, default=5.0, help="kürzere Einbrüche innerhalb der Last überbrücken [s]")
    ap.add_argument("--window", default="auto", help="Länge des Lastfensters [s] oder 'auto'")
    ap.add_argument("--threshold", type=float, default=None, help="feste Schwelle [W] statt automatisch")
    ap.add_argument("--delta", choices=["mean", "robust"], default="mean",
                    help="Mehrverbrauch aus Mittelwerten (Default) oder Medianen (robust gegen Spitzen)")
    ap.add_argument("--baseline", default=None, help="Vergleichsbasis (Default: erste Version)")
    ap.add_argument("--kompakt", action="store_true",
                    help="nur das Wesentliche ausgeben: Bericht, Kurzauswertung (.xlsx/.tex), zwei Bilder")
    ap.add_argument("--compare-cycles", type=int, nargs=2, metavar=("A", "B"),
                    help="zwei Zyklen im direkten Vergleich darstellen (Default: erster und letzter)")
    ap.add_argument("--exclude-outliers", action="store_true",
                    help="Ausreißerzyklen aus Statistik und Tests herausnehmen")
    args = ap.parse_args()

    pre = PRESETS[args.preset]
    cfg = {
        "benchmark": args.title or pre["benchmark"],
        "load": args.load_seconds or pre["load"],
        "idle": args.idle_seconds or pre["idle"],
        "cycles": args.cycles or pre["cycles"],
        "skip_ramp": args.skip_ramp if args.skip_ramp is not None else pre["skip_ramp"],
        "smooth": args.smooth or pre["smooth"],
        "guard": args.guard,
    }
    cfg["min_load"] = args.min_load or 0.5 * cfg["load"]
    cfg["idle_window"] = max(10.0, cfg["idle"] - 2 * cfg["guard"])
    versions = args.versions
    labels = {v: pre["labels"].get(v, v) for v in versions}
    labels.update(parse_kv(args.label, "label"))
    files = parse_kv(args.file, "file")
    perf_files = parse_kv(args.perf, "perf")
    baseline = args.baseline or versions[0]
    tag = args.tag or args.preset

    os.makedirs(args.outdir, exist_ok=True)
    logfile = setup_logging(args.outdir, tag)

    heading(f"Treibervergleich {cfg['benchmark']} – Mehrfachzyklen")
    log(f"Erwarteter Ablauf je Zyklus: {cfg['idle']:.0f} s Leerlauf -> {cfg['load']:.0f} s Last "
        f"-> {cfg['idle']:.0f} s Leerlauf, {cfg['cycles']} Zyklen je Datei")
    log(f"Parameter: Glättung {cfg['smooth']} s, Mindestlast {cfg['min_load']:.0f} s, "
        f"Einschwingzeit {cfg['skip_ramp']} s, Abstand zu Flanken {cfg['guard']} s, "
        f"Leerlauffenster {cfg['idle_window']:.0f} s")
    log(f"Mehrverbrauch (Delta) berechnet aus: "
        f"{'Mittelwerten' if args.delta == 'mean' else 'Medianen (robust)'}")
    if not HAVE_SCIPY:
        log("HINWEIS: scipy nicht installiert – p-Werte werden nicht berechnet.")

    # ------------------------------------------------------------------ Laden
    res = {}
    heading("1. Datenqualität je Datei", "-")
    log(f"{'Treiber':<24}{'Datei':<34}{'Werte':>7}{'Dauer':>10}{'Δt Median':>11}"
        f"{'Δt max':>9}{'Lücken':>8}{'Schwelle':>10}{'Idle-Niv.':>10}{'Last-Niv.':>10}{'Trennung':>10}")
    for v in versions:
        path = files.get(v) or os.path.join(args.logdir, args.pattern.format(version=v))
        if not os.path.exists(path):
            log(f"{labels[v]:<24}FEHLT: {path}")
            continue
        df = load_log(path)
        q = data_quality(df)
        t, p = resample(df)
        ps = smooth(p, cfg["smooth"])
        thr, lo, hi, sep = auto_threshold(ps)
        if args.threshold is not None:
            thr = args.threshold
        segs, _ = detect_segments(t, p, thr, cfg["smooth"], cfg["min_load"], args.merge_gap)
        res[v] = {"path": path, "df": df, "q": q, "t": t, "p": p, "thr": thr,
                  "lo": lo, "hi": hi, "sep": sep, "segs": segs}
        log(f"{labels[v]:<24}{os.path.basename(path)[:33]:<34}{q['n']:>7}"
            f"{q['duration_s'] / 60:>8.1f} m{q['dt_median']:>10.2f}s{q['dt_max']:>8.1f}s"
            f"{q['gaps']:>8}{thr:>9.2f}W{lo:>9.2f}W{hi:>9.2f}W{sep:>10.1f}")
    missing = [v for v in versions if v not in res]
    versions = [v for v in versions if v in res]
    if len(versions) == 0:
        log("Keine Logdatei gefunden – Abbruch.")
        sys.exit(1)
    if baseline not in versions:
        baseline = versions[0]
    log("")
    log("Trennung = Abstand Leerlauf-/Lastniveau in Einheiten der robusten Streuung. "
        "Unter 5 ist die automatische Erkennung unsicher.")
    for v in versions:
        if res[v]["sep"] < 5:
            log(f"WARNUNG {labels[v]}: geringe Trennschärfe ({res[v]['sep']:.1f}) – "
                f"Erkennung prüfen, ggf. --threshold setzen.")
        if res[v]["q"]["gaps"]:
            log(f"WARNUNG {labels[v]}: {res[v]['q']['gaps']} Lücke(n) in den Messdaten, "
                f"größte {res[v]['q']['dt_max']:.1f} s.")

    # ------------------------------------------- Fensterlänge und Zyklen
    all_durs = [te - ts for v in versions for ts, te in res[v]["segs"]]
    if str(args.window).lower() == "auto":
        med = float(np.median(all_durs)) if all_durs else cfg["load"]
        win_len = math.floor(min(cfg["load"], med) - cfg["skip_ramp"] - 1.0)
        win_txt = f"{win_len:.0f} s (automatisch aus der typischen Lastdauer)"
    else:
        win_len = float(args.window)
        win_txt = f"{win_len:.0f} s (vorgegeben)"

    for v in versions:
        r = res[v]
        r["cycles"] = analyse_cycles(r["t"], r["p"], r["segs"], cfg, win_len)
    cfg["win_len"] = win_len
    for v in versions:
        res[v]["rel"], res[v]["prof"] = cycle_profiles(res[v], cfg)

    heading("2. Zyklenerkennung und Ablaufprüfung", "-")
    log(f"Lastfenster für alle Treiber: {win_txt}, beginnend {cfg['skip_ramp']:.0f} s nach Lastbeginn.")
    exp_period = cfg["load"] + 2 * cfg["idle"]
    log(f"Erwartete Periode Lastbeginn -> Lastbeginn: ca. {exp_period:.0f} s")
    log("")
    log(f"{'Treiber':<24}{'gefunden':>9}{'erwartet':>9}{'Lastdauer Ø':>13}{'min':>8}{'max':>8}"
        f"{'Periode Ø':>11}{'Periode σ':>11}{'unvollst.':>11}")
    for v in versions:
        c = res[v]["cycles"]
        durs = [x["load_dur_s"] for x in c]
        per = clean([x["period_s"] for x in c])
        inc = sum(not x["complete"] for x in c)
        log(f"{labels[v]:<24}{len(c):>9}{cfg['cycles']:>9}{fmt(np.mean(durs) if durs else np.nan, 1, 's'):>13}"
            f"{fmt(min(durs) if durs else np.nan, 1, 's'):>8}{fmt(max(durs) if durs else np.nan, 1, 's'):>8}"
            f"{fmt(per.mean() if len(per) else np.nan, 1, 's'):>11}{fmt(per.std() if len(per) > 1 else np.nan, 2, 's'):>11}"
            f"{inc:>11}")
    for v in versions:
        c = res[v]["cycles"]
        if len(c) != cfg["cycles"]:
            log(f"WARNUNG {labels[v]}: {len(c)} statt {cfg['cycles']} Lastphasen erkannt – "
                f"Gesamtverlauf-Diagramm prüfen.")
        per = [x for x in c if np.isfinite(x["period_s"])]
        if len(per) >= 2:
            med_p = np.median([x["period_s"] for x in per])
            by_nr = {x["cycle"]: x for x in c}
            for x in per:
                if abs(x["period_s"] - med_p) > 0.1 * med_p:
                    prev = by_nr.get(x["cycle"] - 1)
                    why = ("Folge der verkürzten Lastphase in Zyklus " f"{prev['cycle']}"
                           if prev and not prev["complete"] else "Aussetzer oder Fehlerkennung?")
                    log(f"WARNUNG {labels[v]}: Zyklus {x['cycle']} beginnt {x['period_s']:.0f} s nach "
                        f"dem vorherigen (typisch {med_p:.0f} s) – {why}")
        for x in c:
            if not x["complete"]:
                log(f"WARNUNG {labels[v]}: Zyklus {x['cycle']} hat nur {x['load_dur_s']:.0f} s Last – "
                    f"wird als unvollständig markiert und nicht in die Statistik übernommen.")
    if missing:
        log(f"Nicht gefunden und übersprungen: {', '.join(missing)}")

    # ------------------------------------------------ Tabelle aller Zyklen
    rows = []
    for v in versions:
        for x in res[v]["cycles"]:
            rows.append({"version": v, **x})
    table = pd.DataFrame(rows)
    table["delta"] = table["delta_mean"] if args.delta == "mean" else table["delta_robust"]

    # ------------------------------------------------ Leistungsdaten
    eff_meta = None
    table["perf"] = np.nan
    table["eff"] = np.nan
    for v, path in perf_files.items():
        if v not in versions:
            continue
        try:
            meta = load_perf_csv(path)
        except Exception as e:
            log(f"WARNUNG Leistungsdaten {v}: {e}")
            continue
        if eff_meta and meta["column"] != eff_meta["column"]:
            log(f"WARNUNG {v}: andere Leistungsgröße ({meta['column']}) als bei den übrigen – ignoriert.")
            continue
        eff_meta = eff_meta or meta
        mask = table.version == v
        table.loc[mask, "perf"] = table.loc[mask, "cycle"].map(meta["values"])
        n_found = int(table.loc[mask, "perf"].notna().sum())
        if n_found != mask.sum():
            log(f"HINWEIS {labels[v]}: Leistungswerte für {n_found} von {int(mask.sum())} Zyklen gefunden.")
    if eff_meta:
        fn = eff_meta["fn"]
        table["eff"] = [fn(d, pv, cfg["load"]) if np.isfinite(d) and np.isfinite(pv) and pv > 0 else np.nan
                        for d, pv in zip(table["delta"], table["perf"])]

    # ------------------------------------------------ Ausreißer
    table["outlier"] = False
    for v in versions:
        m = (table.version == v) & table.complete
        for col in ("delta", "load_mean"):
            vals = table.loc[m, col].values
            z = robust_z(vals)
            med = np.nanmedian(vals) if len(vals) else np.nan
            relevant = np.abs(vals - med) > 0.03 * abs(med)  # mind. 3 % Abweichung
            table.loc[m, "outlier"] |= (np.abs(z) > 3.5) & relevant
    table["used"] = table.complete & ~(table.outlier & args.exclude_outliers)

    # ------------------------------------------------ Bericht je Zyklus
    heading("3. Kennzahlen je Zyklus", "-")
    log("Leerlauf = Mittel aus den Fenstern vor und nach der Last; Delta = Last Ø - Leerlauf Ø; "
        "CV = Schwankung innerhalb des Lastfensters.")
    for v in versions:
        log("")
        log(f"--- {labels[v]} ---")
        log(f"{'Nr':>3}{'Beginn':>9}{'Last':>7}{'Leerl. vor':>12}{'Last Ø':>9}{'Leerl. nach':>13}"
            f"{'Delta':>8}{'CV':>7}{'Max':>8}{'Energie':>10}"
            f"{'  ' + eff_meta['column'] if eff_meta else '':>10}  Hinweise")
        for _, x in table[table.version == v].iterrows():
            notes = []
            if not x.complete:
                notes.append("unvollständig")
            if x.outlier:
                notes.append("Ausreißer")
            if x.idle_spikes:
                notes.append(f"{int(x.idle_spikes)} Leerlaufspitze(n) bis {x.idle_spike_max:.0f} W")
            log(f"{int(x.cycle):>3}{x.start_min:>7.1f} m{x.load_dur_s:>6.0f}s{fmt(x.idle_pre_mean):>11}W"
                f"{fmt(x.load_mean):>8}W{fmt(x.idle_post_mean):>12}W{fmt(x.delta):>7}W"
                f"{fmt(x.load_cv, 1):>6}%{fmt(x.load_max, 1):>7}W{fmt(x.energy_load_Wh, 4):>10}"
                f"{fmt(x.perf, 1) if eff_meta else '':>10}  {', '.join(notes)}")

    report_cycle_matrix(table, versions, labels, baseline, eff_meta)

    # ------------------------------------------------ Aggregat je Treiber
    metrics = [
        ("idle_mean", "Leerlauf Ø [W]", 2), ("idle_median", "Leerlauf Median [W]", 2),
        ("load_mean", "Last Ø [W]", 2), ("delta", "Mehrverbrauch Delta [W]", 2),
        ("load_cv", "CV innerhalb Last [%]", 2), ("load_std", "Schwankung in der Last, SD [W]", 3),
        ("idle_std", "Schwankung im Leerlauf, SD [W]", 3), ("load_max", "Lastspitze [W]", 1),
        ("ramp_peak", "Einschwingspitze [W]", 1), ("energy_load_Wh", "Energie Lastfenster [Wh]", 4),
        ("idle_drift", "Leerlauf nach - vor [W]", 2),
    ]
    if eff_meta:
        metrics += [("perf", eff_meta["name"], 2), ("eff", f"Effizienz [{eff_meta['unit']}]", 3)]

    NO_CV = {"idle_drift"}  # Mittelwert um null -> CV ohne Aussage
    summary = {v: {} for v in versions}
    heading("4. Zusammenfassung je Treiber über alle verwertbaren Zyklen", "-")
    log("Ø ± Standardabweichung, [95-%-Konfidenzintervall des Mittelwerts], CV zw. Zyklen = "
        "Wiederholbarkeit von Zyklus zu Zyklus.")
    for key, name, nd in metrics:
        log("")
        log(f"{name}")
        log(f"  {'Treiber':<24}{'n':>4}{'Mittel':>11}{'± SD':>10}{'95-%-KI':>24}{'Median':>11}"
            f"{'Min':>10}{'Max':>10}{'CV zw. Zyklen':>15}")
        for v in versions:
            d = describe(table.loc[(table.version == v) & table.used, key],
                         with_cv=key not in NO_CV)
            summary[v][key] = d
            ci = f"[{fmt(d['ci_lo'], nd)} ; {fmt(d['ci_hi'], nd)}]"
            log(f"  {labels[v]:<24}{d['n']:>4}{fmt(d['mean'], nd):>11}{fmt(d['sd'], nd):>10}{ci:>24}"
                f"{fmt(d['median'], nd):>11}{fmt(d['min'], nd):>10}{fmt(d['max'], nd):>10}"
                f"{fmt(d['cv'], 1, ' %'):>15}")

    stab, stab_tests = stability_analysis(table, versions)
    report_stability(stab, stab_tests, versions, labels)

    # ------------------------------------------------ Stabilität und Trend
    heading("5. Stabilität über die Zyklen: Trend, Aufwärmeffekt, Ausreißer", "-")
    log("Steigung = lineare Änderung je Zyklus; p < 0,05 deutet auf eine systematische Drift "
        "(z. B. Erwärmung) statt Zufallsstreuung.")
    log(f"  {'Treiber':<24}{'Größe':<12}{'Steigung/Zyklus':>17}{'Summe über Lauf':>17}{'R²':>7}{'p':>8}"
        f"{'Zyklus 1 vs. Rest':>20}")
    trends = {}
    trend_rows = []
    for v in versions:
        sub = table[(table.version == v) & table.used]
        for key, name in (("load_mean", "Last Ø"), ("delta", "Delta"), ("idle_mean", "Leerlauf Ø")):
            tr = trend(sub.cycle, sub[key])
            trends[(v, key)] = tr
            first = sub.loc[sub.cycle == sub.cycle.min(), key]
            rest = sub.loc[sub.cycle != sub.cycle.min(), key]
            warm = (float(first.iloc[0]) - float(rest.mean())) if len(first) and len(rest) else np.nan
            trend_rows.append({"version": v, "name": name, **tr, "warm": warm})
            log(f"  {labels[v]:<24}{name:<12}{fmt(tr['slope'], 3, ' W'):>17}{fmt(tr['total'], 2, ' W'):>17}"
                f"{fmt(tr['r2'], 2):>7}{fmt_p(tr['p']):>8}{fmt(warm, 2, ' W'):>20}")
    flagged = table[table.outlier]
    log("")
    if len(flagged):
        log("Ausreißerzyklen (robuster z-Wert > 3,5 und mind. 3 % Abweichung vom Median bei Delta oder Last Ø):")
        for _, x in flagged.iterrows():
            log(f"  {labels[x.version]} Zyklus {int(x.cycle)}: Last Ø {fmt(x.load_mean)} W, "
                f"Delta {fmt(x.delta)} W"
                f"{' – aus der Statistik genommen' if args.exclude_outliers else ' – bleibt in der Statistik (--exclude-outliers zum Entfernen)'}")
    else:
        log("Keine Ausreißerzyklen gefunden.")

    # ------------------------------------------------ Treibervergleich
    heading("6. Treibervergleich: statistische Tests", "-")
    log("Globaler Test: Unterscheidet sich mindestens ein Treiber? Paarweise: Welch-t-Test "
        "(ungleiche Varianzen) und Mann-Whitney-U, p-Werte Holm-korrigiert.")
    log("Hedges g: < 0,2 vernachlässigbar, < 0,5 klein, < 0,8 mittel, sonst groß.")
    comp_keys = [("delta", "Mehrverbrauch Delta"), ("load_mean", "Last Ø"), ("idle_mean", "Leerlauf Ø")]
    if eff_meta:
        comp_keys.append(("eff", f"Effizienz ({eff_meta['unit']})"))
    comp_rows = []
    global_res = {}
    pairs = list(itertools.combinations(versions, 2))
    for key, name in comp_keys:
        groups = [table.loc[(table.version == v) & table.used, key] for v in versions]
        gt = global_tests(groups)
        global_res[name] = gt
        log("")
        if np.isfinite(gt["kruskal"]) or np.isfinite(gt["anova"]):
            log(f"{name}:  Kruskal-Wallis p = {fmt_p(gt['kruskal'])}   {gt['anova_name']} p = {fmt_p(gt['anova'])}")
        else:
            log(f"{name}:  globaler Test nicht möglich (je Treiber mind. 2 verwertbare Zyklen nötig)")
        results = [compare(table.loc[(table.version == a) & table.used, key],
                           table.loc[(table.version == b) & table.used, key]) for a, b in pairs]
        adj_w = holm([r["p_welch"] if r else np.nan for r in results])
        adj_m = holm([r["p_mw"] if r else np.nan for r in results])
        log(f"  {'Vergleich':<30}{'Differenz':>11}{'relativ':>10}{'95-%-KI der Differenz':>26}"
            f"{'p Welch':>9}{'p MWU':>8}{'Hedges g':>10}  Effekt")
        for (a, b), r, pw, pm in zip(pairs, results, adj_w, adj_m):
            label = f"{labels[b]} − {labels[a]}"
            if r is None:
                log(f"  {label:<30}  zu wenige Zyklen für einen Test (je Treiber mind. 2 nötig)")
                continue
            ci = f"[{fmt(r['ci_lo'], 3)} ; {fmt(r['ci_hi'], 3)}]"
            sig = " *" if np.isfinite(pw) and pw < 0.05 else ""
            log(f"  {label:<30}{fmt(r['diff'], 3):>11}{fmt(r['rel'], 1, ' %'):>10}{ci:>26}"
                f"{fmt_p(pw):>9}{fmt_p(pm):>8}{fmt(r['g'], 2):>10}  {effect_label(r['g'])}{sig}")
            comp_rows.append({"metric": key, "a": a, "b": b, **r,
                              "p_welch_holm": pw, "p_mw_holm": pm, "effect": effect_label(r["g"])})
    log("")
    log("* = nach Holm-Korrektur signifikant auf dem 5-%-Niveau (Welch-Test).")

    # ------------------------------------------------ Bewertung
    heading("7. Einordnung", "-")
    ok = [v for v in versions if np.isfinite(summary[v]["delta"]["mean"])]
    if ok:
        ranked = sorted(ok, key=lambda v: summary[v]["delta"]["mean"])
        log("Rangfolge nach Mehrverbrauch unter Last (kleiner = sparsamer):")
        for i, v in enumerate(ranked, 1):
            d = summary[v]["delta"]
            ci = (f"95-%-KI {fmt(d['ci_lo'])} bis {fmt(d['ci_hi'])} W, " if np.isfinite(d["ci_lo"]) else "")
            log(f"  {i}. {labels[v]:<24}{fmt(d['mean'])} W  ({ci}n = {d['n']})")
        best, worst = ranked[0], ranked[-1]
        if best != worst:
            r = next((c for c in comp_rows if c["metric"] == "delta"
                      and {c["a"], c["b"]} == {best, worst}), None)
            diff = summary[worst]["delta"]["mean"] - summary[best]["delta"]["mean"]
            log("")
            if r and np.isfinite(r["p_welch_holm"]):
                verdict = ("statistisch abgesichert" if r["p_welch_holm"] < 0.05
                           else "statistisch NICHT abgesichert")
                log(f"Der Abstand zwischen sparsamstem und verbrauchsstärkstem Treiber beträgt {diff:.2f} W "
                    f"und ist {verdict} (p = {fmt_p(r['p_welch_holm'])}, Effekt {effect_label(r['g'])}).")
            else:
                log(f"Der Abstand zwischen sparsamstem und verbrauchsstärkstem Treiber beträgt {diff:.2f} W; "
                    f"für einen Test fehlen Wiederholungen.")
        cvs = {v: summary[v]["load_cv"]["mean"] for v in ok}
        steady = min(cvs, key=lambda v: cvs[v] if np.isfinite(cvs[v]) else np.inf)
        log(f"Gleichmäßigste Leistungsaufnahme innerhalb der Lastphase: {labels[steady]} "
            f"(mittlerer CV {fmt(cvs[steady], 2)} %).")
        for v in ok:
            level = summary[v]["load_mean"]["mean"]
            for key, name in (("load_mean", "Last"), ("delta", "Mehrverbrauch")):
                tr = trends.get((v, key))
                if (tr and np.isfinite(tr["p"]) and tr["p"] < 0.05
                        and abs(tr["total"]) > 0.01 * abs(level)):
                    log(f"Achtung {labels[v]}: {name} ändert sich systematisch über die Zyklen "
                        f"({tr['slope']:+.3f} W je Zyklus, {tr['total']:+.2f} W über den Lauf) – "
                        f"Aufwärm- oder Temperatureffekt; Pausen verlängern oder Einlaufzyklus vorschalten.")
            sub = table[(table.version == v) & table.used].sort_values("cycle")
            if len(sub) >= 4:
                rest = sub["load_mean"].iloc[1:]
                dev = sub["load_mean"].iloc[0] - rest.mean()
                if rest.std(ddof=1) > 0 and abs(dev) > 2 * rest.std(ddof=1) and abs(dev) > 0.01 * abs(level):
                    log(f"Hinweis {labels[v]}: Der erste Zyklus weicht um {dev:+.2f} W vom Rest ab "
                        f"(> 2 Standardabweichungen) – typischer Einlaufeffekt. Prüfen, ob die "
                        f"Aussage ohne Zyklus 1 gleich bleibt.")
        for v in ok:
            d = summary[v]["idle_drift"]
            idle = summary[v]["idle_mean"]["mean"]
            if (np.isfinite(d["ci_lo"]) and (d["ci_lo"] > 0 or d["ci_hi"] < 0)
                    and abs(d["mean"]) > 0.05 * abs(idle)):
                log(f"Hinweis {labels[v]}: Leerlauf nach der Last liegt im Mittel {d['mean']:+.2f} W "
                    f"{'über' if d['mean'] > 0 else 'unter'} dem Leerlauf davor (Nachlauf, z. B. Lüfter "
                    f"oder Nachheizen). Das Leerlaufniveau hängt dann vom Abstand zur Last ab; "
                    f"längere Leerlaufphasen oder --guard erhöhen.")
    log("")
    log("Methodische Hinweise:")
    log("  * Die Zyklen eines Treibers stammen aus EINER Sitzung. Sie erfassen die Streuung von "
        "Zyklus zu Zyklus, nicht die zwischen Neuinstallationen oder Messtagen. Die Tests sind "
        "deshalb eher zu optimistisch; für belastbare Aussagen die Messreihe an einem zweiten "
        "Tag bzw. nach Neuinstallation wiederholen.")
    if not eff_meta:
        log("  * Ohne Durchsatz- bzw. Leistungswerte bleibt offen, ob ein sparsamerer Treiber "
            "einfach weniger Arbeit verrichtet hat. Mit --perf lassen sich die CSV-Dateien der "
            "Benchmark-Skripte einbinden.")
    log("  * Leerlaufspitzen stammen meist aus Hintergrundaktivität des Betriebssystems. Wenn "
        "Mittelwert- und Median-Delta deutlich auseinanderliegen, --delta robust verwenden.")
    for v in ok:
        dm = summary[v]["delta"]["mean"]
        dr = describe(table.loc[(table.version == v) & table.used, "delta_robust"])["mean"]
        if np.isfinite(dm) and np.isfinite(dr) and abs(dm - dr) > 0.1 * abs(dm):
            log(f"    -> bei {labels[v]} weichen Mittelwert-Delta ({dm:.2f} W) und Median-Delta "
                f"({dr:.2f} W) um mehr als 10 % voneinander ab.")

    # ------------------------------------------------ Dateien
    base = os.path.join(args.outdir, tag)
    pt = phase_tables(res, table, versions)
    report_short_tables(pt, summary, versions, labels)
    check_short_tables(pt, res, versions, labels)
    write_latex_short_tables(f"{base}_kurztabellen.tex", pt, summary, versions, labels, cfg, eff_meta)
    write_short_xlsx(f"{base}_kurzauswertung.xlsx", pt, summary, versions, labels, cfg, eff_meta,
                     [os.path.basename(res[v]["path"]) for v in versions])
    plot_timeline(res, versions, labels, cfg, f"{base}_01_gesamtverlauf.png")
    plot_cycle_grid(res, table, versions, labels, cfg, f"{base}_07_zyklusvergleich.png")
    cyc_dir = None

    if not args.kompakt:
        # (keine CSV-Dateien mehr: alle Daten stehen in der Excel-Datei, ohne Probleme mit Dezimaltrennern)
        write_latex(f"{base}_tabelle.tex", summary, versions, labels, cfg, eff_meta)
        full_keys = [(k, n_, d) for k, n_, d in metrics if k != "idle_drift"]
        write_latex_full(f"{base}_tabelle_gesamt.tex", summary, versions, labels, cfg, full_keys)
        write_latex_cycles(f"{base}_tabelle_zyklen.tex", table, summary, versions, cfg)
        write_excel(f"{base}_auswertung.xlsx", table, versions, labels, cfg, baseline, eff_meta,
                    comp_rows, global_res, trend_rows, res, win_txt, stab_tests)
        write_latex_stability(f"{base}_tabelle_stabilitaet.tex", stab, versions, labels, cfg)
        n_max = int(table.cycle.max()) if len(table) else 1
        ca, cb = args.compare_cycles if args.compare_cycles else (1, n_max)
        if 1 <= ca <= n_max and 1 <= cb <= n_max:
            plot_cycle_pair(res, table, versions, labels, cfg, (ca, cb), f"{base}_08_zyklus_{ca}_und_{cb}.png")
            plot_cycle_pair_per_driver(res, table, versions, labels, cfg, (ca, cb),
                                       f"{base}_09_zyklus_{ca}_und_{cb}_je_treiber.png")
        cyc_dir = f"{base}_zyklusbilder"
        os.makedirs(os.path.join(cyc_dir, "ohne_tabelle"), exist_ok=True)
        n_cyc = int(table.cycle.max()) if len(table) else 0
        vals = [res[v]["prof"][int(x.cycle) - 1] for v in versions
                for _, x in table[(table.version == v) & table.used & ~table.outlier].iterrows()]
        if vals:
            allv = np.concatenate([a_[np.isfinite(a_)] for a_ in vals])
            ylim_all = (max(0, np.nanmin(allv) - 2), np.nanmax(allv) + 4)
        else:
            ylim_all = _ylim(res, versions, range(1, n_cyc + 1))
        for k in range(1, n_cyc + 1):
            plot_cycle_single(res, table, versions, labels, cfg, k,
                              os.path.join(cyc_dir, f"zyklus_{k:02d}.png"), ylim=ylim_all)
            plot_cycle_single(res, table, versions, labels, cfg, k,
                              os.path.join(cyc_dir, "ohne_tabelle", f"zyklus_{k:02d}.png"),
                              ylim=ylim_all, with_table=False)
        plot_overlay(res, versions, labels, cfg, f"{base}_02_zyklen_overlay.png")
        plot_mean_profile(res, versions, labels, cfg, f"{base}_03_mittleres_profil.png")
        plot_distribution(table, versions, labels, cfg, f"{base}_04_verteilung.png")
        plot_trend(table, versions, labels, cfg, f"{base}_05_trend.png")
        plot_ci(summary, versions, labels, cfg, f"{base}_06_mittelwerte_ki.png",
                eff_meta["unit"] if eff_meta else None)

    log("")
    heading("9. Ausgabedateien", "-")
    for f in sorted(os.listdir(args.outdir)):
        if f.startswith(tag):
            log(f"  {os.path.join(args.outdir, f)}")
    log(f"  {logfile}")
    if cyc_dir:
        log(f"  {cyc_dir}{os.sep}zyklus_NN.png  (ein Bild je Zyklus)")


def de(x, nd=2):
    s = fmt(x, nd)
    return s.replace(".", ",") if s != "n/a" else "--"


def write_latex(path, summary, versions, labels, cfg, eff_meta):
    """Kompakte booktabs-Tabelle zum Einbinden in den Bericht."""
    cols = "lrrrrr" + ("r" if eff_meta else "")
    head = (r"Treiber & $n$ & Leerlauf [W] & Last [W] & "
            r"Delta [W] & 95-\%-KI Delta [W]")
    if eff_meta:
        head += f" & {eff_meta['unit']}"
    lines = [
        r"% benötigt \usepackage{booktabs}",
        r"\begin{table}[htbp]", r"\centering", r"\small",
        rf"\begin{{tabular}}{{{cols}}}", r"\toprule", head + r" \\", r"\midrule",
    ]
    for v in versions:
        s = summary[v]
        row = (f"{labels[v]} & {s['delta']['n']} & {de(s['idle_mean']['mean'])} & "
               f"{de(s['load_mean']['mean'])} & {de(s['delta']['mean'])} & "
               f"{de(s['delta']['ci_lo'])} bis {de(s['delta']['ci_hi'])}")
        if eff_meta:
            row += f" & {de(s['eff']['mean'], 3)}"
        lines.append(row.replace("%", r"\%") + r" \\")
    lines += [
        r"\bottomrule", r"\end{tabular}",
        rf"\caption{{{cfg['benchmark']}: Mittelwerte über alle verwertbaren Zyklen "
        rf"(Lastfenster je Zyklus gleich lang, Delta = Last minus Leerlauf)}}",
        r"\end{table}",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
