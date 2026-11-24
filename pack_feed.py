#!/usr/bin/env python3
"""BRIDGE feed packer.

Downloads 511's Regional GTFS feed (operator_id=RG), trims it down to
exactly what the BRIDGE planner reads, and writes:

  bridge-feed.zip   - the packed feed (same 7 text files, same format)
  feed-meta.json    - build stamp + sizes

The trims, all safe for the planner:
  * shapes.txt and every other unneeded file are dropped.
  * trips.txt keeps only route_id, service_id, trip_id, trip_headsign,
    and only trips whose service runs on the build day or the day before
    (America/Los_Angeles) - the planner only ever plans today, with a
    yesterday offset for after-midnight trips.
  * stop_times.txt keeps only trip_id, stop_id, arrival_time,
    departure_time, stop_sequence, and only rows for kept trips.
    Columns are located BY HEADER NAME: 511's stop_times.txt does not
    use the standard GTFS column order.
  * trip_id values are rewritten to short ids (T1, T2, ...) consistently
    in trips.txt and stop_times.txt. The planner treats trip_id as an
    opaque key, so this changes nothing it can see.
  * agency.txt, stops.txt, routes.txt, calendar.txt and
    calendar_dates.txt are copied verbatim (they are small).

The 511 API key comes from the API_511_KEY environment variable (a
GitHub Actions secret in production). For local runs it falls back to
the TX_511_KEY constant in the BRIDGE app source. The key is never
printed or written to any output file.
"""

import io
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEED_URL = "https://api.511.org/transit/datafeeds?operator_id=RG&api_key="
KEEP_FILES = ["agency.txt", "stops.txt", "routes.txt", "trips.txt",
              "calendar.txt", "calendar_dates.txt", "stop_times.txt"]
PT = ZoneInfo("America/Los_Angeles")
DOW = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def get_key():
    key = os.environ.get("API_511_KEY", "").strip()
    if key:
        return key
    # Local fallback: read the constant out of the BRIDGE app source.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "bridge-edit", "bridge-project", "index.html"),
        os.path.join(here, "index.html"),
    ]
    for path in candidates:
        if os.path.exists(path):
            m = re.search(r"TX_511_KEY\s*=\s*'([^']+)'", open(path).read())
            if m:
                return m.group(1)
    sys.exit("No API key: set API_511_KEY or run next to the BRIDGE app source.")


def yyyymmdd(d):
    return d.year * 10000 + d.month * 100 + d.day


def parse_csv(text):
    """Small CSV reader for the small GTFS files (handles quoted fields)."""
    import csv
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    out = []
    for row in rows[1:]:
        if not row or all(not c for c in row):
            continue
        out.append({head[i]: (row[i] if i < len(row) else "") for i in range(len(head))})
    return out


def active_services(texts, days):
    """Service ids that run on at least one of the given dates,
    mirroring the planner's calendar + calendar_dates logic."""
    daynums = {yyyymmdd(d) for d in days}
    added, removed = set(), set()
    for r in parse_csv(texts["calendar_dates.txt"]):
        try:
            d = int(r.get("date", "0"))
        except ValueError:
            continue
        if d in daynums:
            if r.get("exception_type") == "1":
                added.add(r["service_id"])
            elif r.get("exception_type") == "2":
                removed.add(r["service_id"])
    active = set()
    for r in parse_csv(texts["calendar.txt"]):
        try:
            start, end = int(r["start_date"]), int(r["end_date"])
        except (KeyError, ValueError):
            continue
        for d in days:
            dn = yyyymmdd(d)
            if start <= dn <= end and r.get(DOW[d.weekday()], "0") == "1":
                active.add(r["service_id"])
                break
    return (active | added) - removed


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(outdir, exist_ok=True)
    key = get_key()

    print("Downloading the 511 Regional feed...")
    req = urllib.request.Request(FEED_URL + key, headers={"User-Agent": "bridge-feed-packer"})
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
        with urllib.request.urlopen(req, timeout=300) as resp:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                tmp.write(chunk)
    src_bytes = os.path.getsize(tmp_path)
    print("Downloaded %.1f MB." % (src_bytes / 1048576))

    zf = zipfile.ZipFile(tmp_path)
    names = {os.path.basename(n): n for n in zf.namelist()}
    texts = {}
    for name in KEEP_FILES:
        if name in names and name not in ("trips.txt", "stop_times.txt"):
            texts[name] = zf.read(names[name]).decode("utf-8-sig")

    now_pt = datetime.now(PT)
    days = [now_pt.date(), now_pt.date() - timedelta(days=1)]
    services = active_services(texts, days)
    print("Services running today/yesterday (PT): %d" % len(services))

    # ── trips.txt: trim columns, drop inactive services, shorten ids ──
    raw_trips = zf.read(names["trips.txt"]).decode("utf-8-sig")
    trip_rows = parse_csv(raw_trips)
    trip_id_map = {}
    trip_lines = ["route_id,service_id,trip_id,trip_headsign"]
    for r in trip_rows:
        if r.get("service_id") not in services:
            continue
        new_id = "T%d" % (len(trip_id_map) + 1)
        trip_id_map[r["trip_id"]] = new_id
        headsign = (r.get("trip_headsign") or "").replace(",", " ").replace("\n", " ")
        trip_lines.append(",".join([r.get("route_id", ""), r["service_id"], new_id, headsign]))
    texts["trips.txt"] = "\n".join(trip_lines) + "\n"
    print("Trips kept: %d of %d" % (len(trip_id_map), len(trip_rows)))

    # ── stop_times.txt: stream, trim columns by header name ──
    out_lines = ["trip_id,stop_id,arrival_time,departure_time,stop_sequence"]
    kept_rows = 0
    with zf.open(names["stop_times.txt"]) as f:
        header = f.readline().decode("utf-8-sig").strip().split(",")
        header = [h.strip() for h in header]
        i_trip = header.index("trip_id")
        i_stop = header.index("stop_id")
        i_arr = header.index("arrival_time")
        i_dep = header.index("departure_time")
        i_seq = header.index("stop_sequence")
        need = max(i_trip, i_stop, i_arr, i_dep, i_seq)
        for raw in f:
            parts = raw.decode("utf-8", "replace").rstrip("\n").split(",")
            if len(parts) <= need:
                continue
            new_id = trip_id_map.get(parts[i_trip])
            if new_id is None:
                continue
            out_lines.append(",".join([new_id, parts[i_stop], parts[i_arr], parts[i_dep], parts[i_seq]]))
            kept_rows += 1
    texts["stop_times.txt"] = "\n".join(out_lines) + "\n"
    print("Stop-time rows kept: %d" % kept_rows)

    # ── Write the pack ──
    pack_path = os.path.join(outdir, "bridge-feed.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name in KEEP_FILES:
            out.writestr(name, texts[name])
    pack_bytes = os.path.getsize(pack_path)

    meta = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "pt_date": now_pt.date().isoformat(),
        "source_bytes": src_bytes,
        "pack_bytes": pack_bytes,
        "trips_kept": len(trip_id_map),
        "stop_times_rows_kept": kept_rows,
    }
    with open(os.path.join(outdir, "feed-meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    os.unlink(tmp_path)
    print("Pack written: %.1f MB (source was %.1f MB)." % (pack_bytes / 1048576, src_bytes / 1048576))


if __name__ == "__main__":
    main()
