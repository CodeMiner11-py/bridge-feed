#!/usr/bin/env python3
"""BRIDGE real-time delays: 511 GTFS-RT TripUpdates -> delays.json.

Downloads the current feed pack (for tripmap.json, which maps 511's
original trip ids to the pack's short T-ids), fetches the regional
TripUpdates protobuf, and republishes only the trips that are delayed
or canceled, keyed by short trip id:

  {"generated_at": ..., "trips": {"T123": {"c": 1}}}            canceled
  {"s": [[seq, stop_id, arr_delay, dep_delay], ...]}           delayed

Only stop rows with a nonzero delay are kept, so the file stays small.
"""
import io, json, os, sys, urllib.request, zipfile
from datetime import datetime, timezone

API_KEY = os.environ.get("API_511_KEY", "")
PACK_URL = "https://raw.githubusercontent.com/CodeMiner11-py/bridge-feed/pack/bridge-feed.zip"
RT_URL = "https://api.511.org/transit/tripupdates?api_key=%s&agency=RG"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bridge-rt"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(outdir, exist_ok=True)
    if not API_KEY:
        print("API_511_KEY is not set", file=sys.stderr)
        sys.exit(1)
    from google.transit import gtfs_realtime_pb2

    print("Downloading the feed pack for tripmap.json ...")
    zf = zipfile.ZipFile(io.BytesIO(fetch(PACK_URL)))
    tripmap = json.loads(zf.read("tripmap.json").decode("utf-8"))
    print("Trip map entries: %d" % len(tripmap))

    print("Fetching 511 TripUpdates ...")
    raw = fetch(RT_URL % API_KEY)
    print("TripUpdates bytes: %d" % len(raw))
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)

    trips = {}
    n_updates = 0
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        short = tripmap.get(tu.trip.trip_id)
        if not short:
            continue
        n_updates += 1
        # TripDescriptor schedule_relationship: 3 = CANCELED
        if tu.trip.schedule_relationship == 3:
            trips[short] = {"c": 1}
            continue
        rows = []
        for stu in tu.stop_time_update:
            a = stu.arrival.delay if stu.HasField("arrival") else 0
            d = stu.departure.delay if stu.HasField("departure") else 0
            if a or d:
                rows.append([stu.stop_sequence, stu.stop_id, a, d])
        if rows:
            rows.sort(key=lambda r: r[0])
            trips[short] = {"s": rows}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "trips": trips}
    with open(os.path.join(outdir, "delays.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("Entities: %d, matched updates: %d, delayed/canceled trips: %d" % (len(feed.entity), n_updates, len(trips)))

if __name__ == "__main__":
    main()
