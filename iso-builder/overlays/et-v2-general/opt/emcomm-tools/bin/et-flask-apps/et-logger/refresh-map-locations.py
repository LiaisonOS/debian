#!/usr/bin/env python3
#
# Author  : Sylvain Deguire (VA2OPS)
# Purpose : Backfill map coordinates on EXISTING et-logger QSOs.
#
#   Walks the QSO log, resolves each callsign through the normal tiers
#   (local US/CA DB -> your own growing DB -> QRZ.com), and fills in
#   qso_lat/qso_lon (plus grid/name/country) so every contact shows on the
#   Field Logger map. Also grows your own callsign DB as it goes.
#
#   QRZ must be configured + online to resolve DX not already in a local DB
#   (set it in et-logger: the gear icon). Stations only known by grid still
#   get mapped (grid -> lat/lon).
#
# Usage:
#   refresh-map-locations.py                 # only QSOs missing coordinates (all sessions)
#   refresh-map-locations.py --all           # re-resolve EVERY QSO (slower, more QRZ hits)
#   refresh-map-locations.py --session <id>  # just one session
#   refresh-map-locations.py --limit N       # cap how many to process (testing)
#
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_logger():
    """Import et-logger.py (dash in the name) as a module."""
    path = os.path.join(HERE, "et-logger.py")
    spec = importlib.util.spec_from_file_location("etlogger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description="Backfill et-logger map coordinates.")
    ap.add_argument("--all", action="store_true",
                    help="re-resolve every QSO, not just those missing coordinates")
    ap.add_argument("--session", type=int, default=None,
                    help="limit to one session id")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N QSOs")
    args = ap.parse_args()

    m = load_logger()
    m.init_db()  # ensure schema (adds worked_callsigns on old DBs)

    # Report QRZ status so the operator knows DX will/won't resolve.
    cfg = m.load_qrz_config()
    if cfg.get("enabled") and cfg.get("user"):
        print("QRZ: enabled (user %s)" % cfg.get("user"))
    else:
        print("QRZ: NOT configured — only locally-known calls + grids will map.")

    print("Scanning log (%s)%s ..." % (
        "ALL qsos" if args.all else "QSOs missing coordinates",
        " for session %d" % args.session if args.session else ""))

    res = m.refresh_qso_locations(session_id=args.session,
                                  only_missing=not args.all,
                                  limit=args.limit)
    print("\nScanned:       %d" % res["scanned"])
    print("Updated:       %d  (now on the map)" % res["updated"])
    print("Still missing: %d  (no DB/QRZ/grid/country match)" % res["still_missing"])
    miss = res.get("missing_calls") or []
    if miss:
        print("\nStill-missing callsigns:")
        for cstr in miss:
            print("   - %s" % cstr)
        print("\n(these resolved to NO location anywhere — often malformed /"
              " special-event / portable calls, or an empty/odd call field)")

    try:
        conn = m.get_db()
        n = conn.execute("SELECT COUNT(*) FROM worked_callsigns").fetchone()[0]
        conn.close()
        print("Your own DB now holds %d station(s)." % n)
    except Exception:
        pass

    print("\nDone. Open the Field Logger map to see them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
