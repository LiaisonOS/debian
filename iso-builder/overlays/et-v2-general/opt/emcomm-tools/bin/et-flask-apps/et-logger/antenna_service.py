"""
antenna_service.py — DB-backed antenna profiles + pattern recommendation
for the et-logger Flask app.

Wraps efhw_nec_model.py and antenna_profiles.py:
- CRUD on antenna_profiles + antenna_pattern_cache (same SQLite DB as QSOs)
- Synchronous pattern precomputation on create/update (~200–500 ms per band)
- best-for lookup: from a target lat/lon and band, ranks configured antennas
  by gain toward that bearing, flags nulls, returns JSON-ready dicts

Uses only stdlib + the two sibling modules — no Flask deps here so the
service stays testable in isolation.

Auteur: Sylvain Deguire (VA2OPS)
"""

import json
import sqlite3
from datetime import datetime, timezone

from efhw_nec_model import (
    great_circle_bearing,
    circular_azimuth_distance,
)
from antenna_profiles import (
    AntennaProfile,
    haversine_km,
    suggested_takeoff_angle,
    NULL_THRESHOLD_DBI,
    get_cached_pattern as _compute_pattern_uncached,
)


# ---------------------------------------------------------------------------
# Schema — called from et-logger.init_db()
# ---------------------------------------------------------------------------

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS antenna_profiles (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        ant_type      TEXT NOT NULL,
        is_omni       INTEGER NOT NULL DEFAULT 0,
        feed_h_ft     REAL,
        far_h_ft      REAL,
        wire_len_ft   REAL,
        azimuth_deg   REAL,
        bands_mhz     TEXT NOT NULL,          -- JSON: {"20m": 14.150, ...}
        site_lat      REAL,
        site_lon      REAL,
        active        INTEGER NOT NULL DEFAULT 1,
        notes         TEXT DEFAULT '',
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS antenna_pattern_cache (
        profile_id    INTEGER NOT NULL,
        band_name     TEXT NOT NULL,
        band_mhz      REAL NOT NULL,
        takeoff_deg   REAL NOT NULL,
        pattern_json  TEXT NOT NULL,          -- [[az, gain_dBi], ...]
        computed_at   TEXT NOT NULL,
        PRIMARY KEY (profile_id, band_name, takeoff_deg),
        FOREIGN KEY (profile_id) REFERENCES antenna_profiles(id) ON DELETE CASCADE
    )
    """,
]


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.execute("PRAGMA foreign_keys = ON")


# ---------------------------------------------------------------------------
# Row ↔ AntennaProfile marshalling
# ---------------------------------------------------------------------------

def _row_to_profile(row: sqlite3.Row) -> AntennaProfile:
    return AntennaProfile(
        name=row["name"],
        ant_type=row["ant_type"],
        bands_mhz=json.loads(row["bands_mhz"]),
        site_lat=row["site_lat"] or 0.0,
        site_lon=row["site_lon"] or 0.0,
        is_omni=bool(row["is_omni"]),
        feed_h_ft=row["feed_h_ft"],
        far_h_ft=row["far_h_ft"],
        wire_len_ft=row["wire_len_ft"],
        azimuth_deg=row["azimuth_deg"],
        notes=row["notes"] or "",
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if isinstance(d.get("bands_mhz"), str):
        d["bands_mhz"] = json.loads(d["bands_mhz"])
    d["is_omni"] = bool(d.get("is_omni", 0))
    d["active"] = bool(d.get("active", 1))
    return d


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM antenna_profiles ORDER BY active DESC, name"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_profile(conn: sqlite3.Connection, profile_id: int) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM antenna_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def _validate_payload(payload: dict) -> tuple[bool, str]:
    if not payload.get("name"):
        return False, "name is required"
    if not payload.get("ant_type"):
        return False, "ant_type is required"
    bands = payload.get("bands_mhz") or {}
    if not isinstance(bands, dict) or not bands:
        return False, "bands_mhz must be a non-empty {band_name: mhz} dict"
    ant_type = (payload.get("ant_type") or "").lower()
    if payload.get("is_omni"):
        pass    # verticale omni — nominal-gain approximation, no geometry
    elif ant_type == "horizontal_loop":
        # Skywarmer: needs height + perimeter, no azimuth or far-end
        for f in ("feed_h_ft", "wire_len_ft"):
            if payload.get(f) in (None, ""):
                return False, f"{f} is required for horizontal_loop"
    elif ant_type == "horizontal_efhw":
        # Horizontal EFHW: single height + length + wire direction.
        # far_h_ft is unused (both ends at feed_h_ft).
        for f in ("feed_h_ft", "wire_len_ft", "azimuth_deg"):
            if payload.get(f) in (None, ""):
                return False, f"{f} is required for horizontal_efhw"
    else:
        # Sloper EFHW / Inverted-V / delta_loop — heights + wire + azimuth
        for f in ("feed_h_ft", "far_h_ft", "wire_len_ft", "azimuth_deg"):
            if payload.get(f) in (None, ""):
                return False, f"{f} is required for ant_type={ant_type!r}"
    return True, ""


def create_profile(conn: sqlite3.Connection, payload: dict) -> tuple[int, list[str]]:
    """Insert profile + precompute pattern cache. Returns (id, warnings)."""
    ok, err = _validate_payload(payload)
    if not ok:
        raise ValueError(err)

    cur = conn.execute(
        """
        INSERT INTO antenna_profiles
          (name, ant_type, is_omni, feed_h_ft, far_h_ft, wire_len_ft,
           azimuth_deg, bands_mhz, site_lat, site_lon, active, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload["ant_type"],
            1 if payload.get("is_omni") else 0,
            payload.get("feed_h_ft"),
            payload.get("far_h_ft"),
            payload.get("wire_len_ft"),
            payload.get("azimuth_deg"),
            json.dumps(payload["bands_mhz"]),
            payload.get("site_lat"),
            payload.get("site_lon"),
            1 if payload.get("active", True) else 0,
            payload.get("notes", ""),
        ),
    )
    profile_id = cur.lastrowid
    conn.commit()

    # Patterns are computed LAZILY on first best-for/map view. Precomputing
    # here would block the HTTP response for 5–30 s per multi-band antenna
    # (each band requires a full NEC solve), and any PyNEC hiccup would
    # hang the save request indefinitely. Cost of laziness: first pattern
    # view takes ~500 ms instead of instant.
    return profile_id, []


def update_profile(conn: sqlite3.Connection, profile_id: int,
                    payload: dict) -> list[str]:
    ok, err = _validate_payload(payload)
    if not ok:
        raise ValueError(err)
    conn.execute(
        """
        UPDATE antenna_profiles SET
          name=?, ant_type=?, is_omni=?, feed_h_ft=?, far_h_ft=?,
          wire_len_ft=?, azimuth_deg=?, bands_mhz=?, site_lat=?, site_lon=?,
          active=?, notes=?
        WHERE id=?
        """,
        (
            payload["name"],
            payload["ant_type"],
            1 if payload.get("is_omni") else 0,
            payload.get("feed_h_ft"),
            payload.get("far_h_ft"),
            payload.get("wire_len_ft"),
            payload.get("azimuth_deg"),
            json.dumps(payload["bands_mhz"]),
            payload.get("site_lat"),
            payload.get("site_lon"),
            1 if payload.get("active", True) else 0,
            payload.get("notes", ""),
            profile_id,
        ),
    )
    # Geometry may have changed — invalidate BOTH caches (in-memory keyed
    # by name, on-disk keyed by profile_id). Patterns recompute lazily on
    # next best-for/pattern view. Precomputing here was blocking PUT for
    # 5–30 s per multi-band antenna (or hanging entirely on PyNEC hiccups).
    import antenna_profiles as _ap
    _ap._pattern_cache.clear()
    conn.execute("DELETE FROM antenna_pattern_cache WHERE profile_id = ?",
                 (profile_id,))
    conn.commit()
    return []    # patterns compute lazily on next best-for/pattern view


def delete_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM antenna_pattern_cache WHERE profile_id = ?",
                 (profile_id,))
    conn.execute("DELETE FROM antenna_profiles WHERE id = ?", (profile_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Pattern precomputation + cache lookup
# ---------------------------------------------------------------------------

# For precompute we pick ONE takeoff angle per band that's most likely to be
# useful at operator's typical distances — 22° covers 1-hop HF, which is the
# workhorse. best-for can request other takeoffs on demand (cached lazily).
DEFAULT_TAKEOFF_DEG = 22.0


def _precompute_patterns(conn: sqlite3.Connection, profile_id: int) -> list[str]:
    """Compute + cache the default-takeoff pattern for every configured band.
    Returns a list of warning strings (e.g. bands that failed to compute)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM antenna_profiles WHERE id = ?",
                       (profile_id,)).fetchone()
    if row is None:
        return [f"profile {profile_id} not found"]

    profile = _row_to_profile(row)
    warnings = []
    for band_name in profile.bands_mhz:
        try:
            _ensure_cached_pattern(conn, profile_id, profile, band_name,
                                    DEFAULT_TAKEOFF_DEG)
        except Exception as e:
            warnings.append(f"{band_name}: {type(e).__name__}: {e}")
    return warnings


def _ensure_cached_pattern(conn: sqlite3.Connection, profile_id: int,
                             profile: AntennaProfile, band_name: str,
                             takeoff_deg: float):
    """Look up (or compute) and cache a pattern. Raises on compute failure."""
    takeoff_key = round(float(takeoff_deg), 1)
    conn.row_factory = sqlite3.Row
    hit = conn.execute(
        """SELECT pattern_json FROM antenna_pattern_cache
           WHERE profile_id = ? AND band_name = ? AND takeoff_deg = ?""",
        (profile_id, band_name, takeoff_key),
    ).fetchone()
    if hit:
        return json.loads(hit["pattern_json"])

    # Delegate to antenna_profiles.get_cached_pattern for the actual NEC call.
    # (Its in-memory dict cache is fine here — SQLite is the durable cache.)
    pattern = _compute_pattern_uncached(profile, band_name, takeoff_key)
    if pattern is None:
        raise RuntimeError(f"band {band_name!r} not configured on profile")

    # Store — force list of [az, gain] pairs (JSON-safe).
    payload = json.dumps([[float(az), float(g)] for az, g in pattern])
    conn.execute(
        """INSERT OR REPLACE INTO antenna_pattern_cache
           (profile_id, band_name, band_mhz, takeoff_deg, pattern_json, computed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profile_id, band_name, profile.bands_mhz[band_name],
         takeoff_key, payload,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    return json.loads(payload)


def get_pattern_json(conn: sqlite3.Connection, profile_id: int,
                       band_name: str, takeoff_deg: float) -> list | None:
    """Public accessor for the map overlay. Returns [[az, gain_dBi], ...]."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM antenna_profiles WHERE id = ?",
                       (profile_id,)).fetchone()
    if row is None:
        return None
    profile = _row_to_profile(row)
    if band_name not in profile.bands_mhz:
        return None
    return _ensure_cached_pattern(conn, profile_id, profile, band_name,
                                   takeoff_deg)


# ---------------------------------------------------------------------------
# Recommendation: best antenna for a target lat/lon on a band
# ---------------------------------------------------------------------------

def best_for(conn: sqlite3.Connection, target_lat: float, target_lon: float,
              band_name: str,
              qth_lat: float | None = None, qth_lon: float | None = None,
              takeoff_deg: float | None = None) -> list[dict]:
    """For a target and band, rank ACTIVE antennas by gain toward the target.

    If (qth_lat, qth_lon) is provided (e.g. from the current session's
    my_lat/my_lon), use that as the operator's location. Otherwise fall
    back to each profile's own site_lat/site_lon — useful when profiles
    are per-site (portable POTA setups)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM antenna_profiles WHERE active = 1"
    ).fetchall()

    results = []
    for row in rows:
        profile = _row_to_profile(row)
        if band_name not in profile.bands_mhz:
            continue

        # QTH: session coords override profile's site coords when available
        lat0 = qth_lat if qth_lat is not None else profile.site_lat
        lon0 = qth_lon if qth_lon is not None else profile.site_lon
        if lat0 is None or lon0 is None or (lat0 == 0.0 and lon0 == 0.0):
            continue    # unknown QTH — can't recommend (0,0 = golfe de Guinee)

        distance_km = haversine_km(lat0, lon0, target_lat, target_lon)
        azimuth = great_circle_bearing(lat0, lon0, target_lat, target_lon)
        actual_takeoff = float(takeoff_deg) if takeoff_deg is not None \
            else suggested_takeoff_angle(distance_km)

        try:
            pattern = _ensure_cached_pattern(conn, row["id"], profile,
                                              band_name, actual_takeoff)
        except Exception:
            pattern = None

        if not pattern:
            gain = None
        else:
            closest = min(pattern,
                          key=lambda t: circular_azimuth_distance(t[0], azimuth))
            gain = closest[1]

        results.append({
            "id":            row["id"],
            "name":          profile.name,
            "gain_dBi":      round(gain, 2) if gain is not None else None,
            "azimuth_deg":   round(azimuth, 1),
            "distance_km":   round(distance_km, 0),
            "takeoff_deg":   round(actual_takeoff, 1),
            "is_null":       gain is not None and gain < NULL_THRESHOLD_DBI,
            "band_name":     band_name,
            "qth_used":      {"lat": lat0, "lon": lon0,
                              "source": "session" if qth_lat is not None else "profile"},
        })

    results.sort(key=lambda r: (r["gain_dBi"] is None,
                                 -(r["gain_dBi"] if r["gain_dBi"] is not None else -99)))
    return results
