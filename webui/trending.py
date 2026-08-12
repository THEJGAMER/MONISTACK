"""Predictive/trending monitoring (ROADMAP.md 3.4): stores periodic samples
of metrics the pollers already collect - optic Rx/Tx power + temperature,
PSU power draw, interface utilization and error/discard counts - and
evaluates simple, explainable threshold rules against the trend, rather
than a single point-in-time reading.

Samples are written by status_poller.py on its slow (transceiver) cadence,
reusing data the fast poll already fetched over SSH - no extra round trips
just to trend something. Storage is Postgres (see db.py's `metric_samples`
table), queried here rather than in app.py so the threshold/forecast math
has direct test coverage independent of any live SSH session, same
separation of concerns as topology.py.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.trending")

# Every metric this module knows how to record/label, plus the direction a
# "bad" trend moves in - used by the generic evaluate_decline/evaluate_rise
# helpers below instead of one bespoke function per metric.
METRIC_LABELS = {
    "optic_rx_power_dbm": "Rx power (dBm)",
    "optic_tx_power_dbm": "Tx power (dBm)",
    "optic_temp_c": "Optic temperature (°C)",
    "psu_power_watts": "PSU power draw (W)",
    "iface_input_mbps": "Input utilization (Mbps)",
    "iface_output_mbps": "Output utilization (Mbps)",
    "iface_input_errors": "Input errors (cumulative)",
    "iface_output_errors": "Output errors (cumulative)",
}


def record_samples(db, rows):
    """`rows`: iterable of (device_id, metric, port, value). `port` may be
    None. Skips the whole call gracefully if `db` is None (not configured
    yet) or a value is None (nothing to record) - callers don't need to
    filter first."""
    if db is None:
        return
    clean = [(d, m, p, v) for (d, m, p, v) in rows if v is not None]
    if not clean:
        return
    try:
        for device_id, metric, port, value in clean:
            db.execute(
                "INSERT INTO metric_samples (device_id, metric, port, value) VALUES (%s, %s, %s, %s)",
                (device_id, metric, port, float(value)),
            )
    except Exception:
        log.warning("could not record trend samples for %s", clean[0][0] if clean else "?", exc_info=True)


def prune_old_samples(db, keep_days=90):
    """Deletes samples older than `keep_days`.

    Scheduling now lives in retention.py, which owns this table alongside
    every other growing one - this function is kept for direct/ad-hoc use
    and is no longer wired to a loop of its own, so there is exactly one
    thing deciding when pruning happens.

    The docstring here used to claim it was "called once at startup and
    then daily". It wasn't: the loop that called it slept 24 hours before
    its first run and nothing called it at startup, so on a process
    redeployed several times a day it realistically never ran. The table
    was found at 2.03M rows / 493 MB as a result. Corrected rather than
    quietly deleted, because the gap between what a comment claims and
    what the code does is the thing that hid this for weeks.
    """
    if db is None:
        return
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        db.execute("DELETE FROM metric_samples WHERE recorded_at < %s", (cutoff,))
    except Exception:
        log.warning("could not prune old trend samples", exc_info=True)


def list_available_series(db, device_id):
    """Returns [{'metric','port'}] for every distinct series this device
    actually has samples for - drives the frontend's metric/port picker
    without it needing to know in advance which ports have optics or PSUs."""
    if db is None:
        return []
    rows = db.query(
        "SELECT DISTINCT metric, port FROM metric_samples WHERE device_id = %s ORDER BY metric, port",
        (device_id,),
    )
    return [{"metric": r["metric"], "port": r["port"]} for r in rows]


def get_samples(db, device_id, metric, port=None, hours=168):
    """Returns [{'recorded_at': iso str, 'value': float}] ascending by
    time, for the trailing `hours` (default 7 days)."""
    if db is None:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if port is None:
        rows = db.query(
            """SELECT recorded_at, value FROM metric_samples
               WHERE device_id = %s AND metric = %s AND port IS NULL AND recorded_at >= %s
               ORDER BY recorded_at""",
            (device_id, metric, since),
        )
    else:
        rows = db.query(
            """SELECT recorded_at, value FROM metric_samples
               WHERE device_id = %s AND metric = %s AND port = %s AND recorded_at >= %s
               ORDER BY recorded_at""",
            (device_id, metric, port, since),
        )
    return [{"recorded_at": r["recorded_at"].isoformat(), "value": r["value"]} for r in rows]


def evaluate_decline(samples, warn_by, unit=""):
    """Flags a sustained *decline* from the trend's own peak - the general
    shape of "optic Rx power slowly dropping" (ROADMAP 3.4): compares the
    latest sample against the highest value seen in the window, since a
    gradually failing optic's power reading trends down from wherever it
    started, not from some fixed absolute number every port should share
    (different fiber runs/attenuation legitimately sit at different
    baselines). Returns None if there's too little history to trust yet
    (fewer than 6 samples - roughly half a day at the 300s sample cadence)
    or the decline hasn't crossed `warn_by`.

    `warn_by` is a magnitude (e.g. 3.0 dB) - crossing it is the alert,
    matching how optics engineers actually reason about power budget
    margin (a 3dB drop is a halving of optical power, a standard early-
    warning threshold), not a value this app invented."""
    if len(samples) < 6:
        return None
    peak = max(s["value"] for s in samples)
    peak_sample = max(samples, key=lambda s: s["value"])
    current = samples[-1]["value"]
    decline = peak - current
    if decline < warn_by:
        return None
    return {
        "kind": "decline",
        "peak_value": peak,
        "peak_at": peak_sample["recorded_at"],
        "current_value": current,
        "current_at": samples[-1]["recorded_at"],
        "change": -decline,
        "unit": unit,
        "message": f"Down {decline:.1f}{unit} from its peak of {peak:.1f}{unit} ({peak_sample['recorded_at']}) to {current:.1f}{unit} now.",
    }


def evaluate_deviation(samples, warn_pct, unit=""):
    """Flags the latest sample being unusually far (either direction) from
    the trend's own trailing baseline - used for metrics with no single
    "good" direction (PSU power draw: both a sudden rise *and* a sudden
    drop can indicate a problem, unlike optic power where only decline
    matters). Baseline is the mean of the first half of the window, so a
    genuine gradual shift doesn't get compared against itself. Returns
    None with fewer than 6 samples or a zero baseline (can't compute a
    percentage against it)."""
    if len(samples) < 6:
        return None
    half = len(samples) // 2
    baseline_samples = samples[:half]
    baseline = sum(s["value"] for s in baseline_samples) / len(baseline_samples)
    current = samples[-1]["value"]
    if baseline == 0:
        return None
    pct_change = (current - baseline) / abs(baseline) * 100
    if abs(pct_change) < warn_pct:
        return None
    direction = "up" if pct_change > 0 else "down"
    return {
        "kind": "deviation",
        "baseline_value": baseline,
        "current_value": current,
        "pct_change": pct_change,
        "unit": unit,
        "message": f"{direction.capitalize()} {abs(pct_change):.0f}% from its recent baseline of {baseline:.1f}{unit} to {current:.1f}{unit} now.",
    }


def forecast_linear(samples, target_value, unit=""):
    """Simple linear-regression capacity forecast: fits a line through the
    window's samples and, if it's trending toward `target_value` (not away
    from it - e.g. utilization climbing toward link capacity, error count
    climbing toward "worth investigating"), projects when it'll get there
    at the current rate of change. Deliberately simple (least-squares
    through raw samples, no seasonality/smoothing) - a rough "worth a
    closer look" estimate, not a guarantee, and framed that way to the
    user rather than implying more precision than trending ~7-30 days of
    data actually supports. Returns None if there's too little history,
    the trend is flat, or it's moving away from the target."""
    if len(samples) < 6:
        return None
    t0 = datetime.fromisoformat(samples[0]["recorded_at"])
    xs = [(datetime.fromisoformat(s["recorded_at"]) - t0).total_seconds() for s in samples]
    ys = [s["value"] for s in samples]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    current = ys[-1]

    moving_toward_target = (slope > 0 and current < target_value) or (slope < 0 and current > target_value)
    if not moving_toward_target or slope == 0:
        return None

    seconds_to_target = (target_value - intercept) / slope - xs[-1]
    if seconds_to_target <= 0:
        return None
    days_to_target = seconds_to_target / 86400
    return {
        "kind": "forecast",
        "current_value": current,
        "target_value": target_value,
        "slope_per_day": slope * 86400,
        "days_to_target": days_to_target,
        "unit": unit,
        "message": (
            f"At its current trend ({slope * 86400:+.2f}{unit}/day), expected to reach "
            f"{target_value:.0f}{unit} in about {days_to_target:.0f} day(s)."
        ),
    }
