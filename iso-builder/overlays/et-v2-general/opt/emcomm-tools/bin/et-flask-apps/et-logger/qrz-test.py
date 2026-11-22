#!/usr/bin/env python3
#
# Author  : Sylvain Deguire (VA2OPS)
# Purpose : Diagnose QRZ.com XML lookups for et-logger. Logs in, then looks up
#           a callsign, and prints the RAW QRZ XML response + the parsed result
#           so you can see the ACTUAL error (e.g. "subscription required") and
#           whether grid/lat/lon come back.
#
# Usage:
#   qrz-test.py <CALLSIGN>        # look up one call (e.g. a DX you worked)
#   qrz-test.py                   # defaults to W1AW
#
import importlib.util
import sys
import urllib.parse
import urllib.request

EL = "/opt/emcomm-tools/bin/et-flask-apps/et-logger/et-logger.py"


def load():
    s = importlib.util.spec_from_file_location("etlogger", EL)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def raw_get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def main():
    call = (sys.argv[1] if len(sys.argv) > 1 else "W1AW").upper().strip()
    m = load()
    cfg = m.load_qrz_config()

    print("=" * 60)
    print("QRZ config file : %s" % m.QRZ_CONFIG)
    print("  user          : %r" % cfg.get("user"))
    print("  has password  : %s" % bool(cfg.get("password")))
    print("  enabled       : %s" % bool(cfg.get("enabled")))
    print("=" * 60)

    if not (cfg.get("user") and cfg.get("password")):
        print("\n[!] No QRZ username/password saved — set them in et-logger (gear icon).")
        return 1

    # ---- 1. LOGIN (raw) ----
    login_url = m.QRZ_API + "?" + urllib.parse.urlencode({
        "username": cfg["user"], "password": cfg["password"],
        "agent": "liaisonos-etlogger1.0",
    })
    print("\n--- 1. LOGIN REQUEST ---")
    safe = login_url.replace(urllib.parse.quote_plus(cfg["password"]), "********") \
                    .replace(cfg["password"], "********")
    print(safe)
    try:
        login_raw = raw_get(login_url)
    except Exception as e:
        print("\n[!] LOGIN request FAILED (network/offline?): %s" % e)
        return 1
    print("\n--- LOGIN RAW RESPONSE ---")
    print(login_raw.strip())

    sess = m._qrz_parse(login_raw.encode()).get("session", {})
    key = sess.get("key")
    if not key:
        print("\n[!] No session key. QRZ said: %r" % sess.get("error", sess))
        return 1
    print("\n[ok] session key: %s..." % key[:10])

    # ---- 2. CALLSIGN LOOKUP (raw) ----
    look_url = m.QRZ_API + "?" + urllib.parse.urlencode({"s": key, "callsign": call})
    print("\n--- 2. LOOKUP REQUEST (%s) ---" % call)
    print(look_url.replace(key, "<SESSIONKEY>"))
    try:
        look_raw = raw_get(look_url)
    except Exception as e:
        print("\n[!] LOOKUP request FAILED: %s" % e)
        return 1
    print("\n--- LOOKUP RAW RESPONSE ---")
    print(look_raw.strip())

    parsed = m._qrz_parse(look_raw.encode())
    print("\n--- PARSED ---")
    print("  session block :", parsed.get("session"))
    c = parsed.get("callsign")
    if c:
        print("  callsign found: YES")
        for k in ("call", "fname", "name", "grid", "lat", "lon",
                  "cqzone", "country", "state", "addr2"):
            print("     %-8s: %r" % (k, c.get(k)))
    else:
        print("  callsign found: NO  ->  this is why nothing maps")

    # ---- 3. VERDICT ----
    print("\n" + "=" * 60)
    err = (parsed.get("session", {}) or {}).get("error", "")
    if c and (c.get("lat") or c.get("grid")):
        print("VERDICT: QRZ returns location data — lookups should work.")
    elif err and ("subscription" in err.lower() or "not authorized" in err.lower()):
        print("VERDICT: QRZ XML SUBSCRIPTION REQUIRED.")
        print("  Your QRZ account can log in but is NOT authorized to pull")
        print("  callsign data from the XML API. You need the paid 'XML")
        print("  Logbook Data' / XML subscription on qrz.com. (Account issue,")
        print("  not a LiaisonOS bug.)")
    elif c:
        print("VERDICT: QRZ returns the call but WITHOUT grid/lat/lon for your")
        print("  account tier — can't map from QRZ. (We could fall back to")
        print("  DXCC/prefix centroid if you want approximate pins.)")
    else:
        print("VERDICT: QRZ returned no callsign and this error: %r" % err)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
