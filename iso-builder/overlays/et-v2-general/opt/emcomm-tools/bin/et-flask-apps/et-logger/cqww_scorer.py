"""CQ WW DX Contest live-score engine for et-logger.

Implements the claimed-score algorithm from cqww_scoring_spec.md:
  score = total_qso_points * (zone_mults + country_mults)   (summed per band)

Country / continent / default-zone resolution uses the AD1C "Big CTY"
cty.dat (MIT-licensed, country-files.com). We ship a copy next to this
module and prefer an operator-updated copy at
~/.config/liaisonos/logger/cty.dat if present.

State is always rebuilt from the full log (see score_session), never
decremented — removing the first of two same-band QSOs turns the second
from a dupe into a scoring QSO, so incremental math is unsafe.
"""

import os
import re

# CQ WW scores only the six classic contest bands.
VALID_BANDS = {160, 80, 40, 20, 15, 10}

# Portable tokens that are NOT a location prefix (operating modifiers).
_MODIFIERS = {"P", "M", "MM", "AM", "QRP", "A", "LH", "R", "B", "J"}

_BUNDLED_CTY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cty.dat")
_OVERRIDE_CTY = os.path.expanduser("~/.config/liaisonos/logger/cty.dat")

# Override tokens embedded in cty.dat alias entries: (cqzone) [ituzone]
# {continent} <lat/lon> ~gmt~.  We only need CQ-zone and continent.
_OVR = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]|\{([^}]*)\}|<([^>]*)>|~([^~]*)~")


class CtyLookup:
    """Prefix / exact-call resolver built from a cty.dat file."""

    def __init__(self, path=None):
        self.exact = {}    # "W1AW"  -> (country, continent, cqzone)
        self.prefix = {}   # "VE"    -> (country, continent, cqzone)
        self._maxlen = 1
        self.path = path or self._pick_path()
        self.ok = False
        if self.path and os.path.isfile(self.path):
            try:
                self._parse(self.path)
                self.ok = True
            except Exception:
                self.ok = False

    @staticmethod
    def _pick_path():
        if os.path.isfile(_OVERRIDE_CTY):
            return _OVERRIDE_CTY
        if os.path.isfile(_BUNDLED_CTY):
            return _BUNDLED_CTY
        return None

    def _parse(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        header = None
        aliases = ""
        for raw in text.splitlines():
            if not raw.strip():
                continue
            if raw[0] != " " and ":" in raw:
                header = raw
                aliases = ""
            else:
                aliases += raw.strip()
            if header and aliases.endswith(";"):
                self._record(header, aliases[:-1])
                header = None
                aliases = ""

    def _record(self, header, alias_blob):
        # Name : CQ : ITU : Cont : Lat : Lon : GMT : PrimaryPrefix :
        parts = header.split(":")
        if len(parts) < 8:
            return
        country = parts[0].strip()
        try:
            base_zone = int(parts[1].strip())
        except ValueError:
            base_zone = 0
        base_cont = parts[3].strip()
        primary = parts[7].strip()

        # Primary prefix is a valid prefix too (if simple / no slash).
        if primary and "/" not in primary:
            self._add_prefix(primary, country, base_cont, base_zone)

        for tok in alias_blob.split(","):
            tok = tok.strip()
            if not tok:
                continue
            zone, cont = base_zone, base_cont
            for m in _OVR.finditer(tok):
                if m.group(1):          # (cqzone)
                    try:
                        zone = int(m.group(1))
                    except ValueError:
                        pass
                elif m.group(3):        # {continent}
                    cont = m.group(3).strip()
            key = _OVR.sub("", tok).strip()
            exact = key.startswith("=")
            if exact:
                key = key[1:]
            if not key:
                continue
            if exact:
                self.exact[key.upper()] = (country, cont, zone)
            else:
                self._add_prefix(key.upper(), country, cont, zone)

    def _add_prefix(self, key, country, cont, zone):
        self.prefix[key] = (country, cont, zone)
        if len(key) > self._maxlen:
            self._maxlen = len(key)

    @staticmethod
    def _location_part(call):
        """Pick the part of a portable call that indicates the location.

        Pragmatic field-logger rule (good enough for a claimed score):
        drop pure modifiers (/P /M /MM...) and bare area digits, then take
        the SHORTEST remaining part (the appended DX prefix, e.g. the 'VE2'
        in W1ABC/VE2 or the 'VE3' in VE3/W1ABC).
        """
        parts = [p for p in call.split("/")
                 if p and p not in _MODIFIERS and not p.isdigit()]
        if not parts:
            return call.replace("/", "")
        if len(parts) == 1:
            return parts[0]
        return min(parts, key=len)

    def resolve(self, call):
        """Return (country, continent, cqzone, is_mm).

        country is None for maritime/aeronautical mobile (no country mult).
        """
        call = (call or "").upper().strip()
        if not call:
            return (None, None, 0, False)

        is_mm = call.endswith("/MM") or call.endswith("/AM")

        # Exact-call override wins (covers =CALL entries in cty.dat).
        hit = self.exact.get(call)
        if hit:
            country, cont, zone = hit
            return (None if is_mm else country, cont, zone, is_mm)

        base = self._location_part(call)
        hit = self.exact.get(base)
        if not hit:
            # Longest-prefix match.
            for n in range(min(len(base), self._maxlen), 0, -1):
                hit = self.prefix.get(base[:n])
                if hit:
                    break
        if not hit:
            return (None, None, 0, is_mm)
        country, cont, zone = hit
        return (None if is_mm else country, cont, zone, is_mm)


# Module-level cached lookup (parsed once).
_LOOKUP = None


def get_lookup():
    global _LOOKUP
    if _LOOKUP is None:
        _LOOKUP = CtyLookup()
    return _LOOKUP


def reload_lookup():
    """Drop the cached lookup (call after the operator updates cty.dat)."""
    global _LOOKUP
    _LOOKUP = None
    return get_lookup()


def band_to_int(band):
    """'20m' -> 20. Returns 0 for anything not a CQ WW contest band."""
    if band is None:
        return 0
    m = re.match(r"\s*(\d+)", str(band))
    if not m:
        return 0
    n = int(m.group(1))
    return n if n in VALID_BANDS else 0


# MHz edges of the six CQ WW contest bands (no WARC/VHF — they score nothing).
_BAND_EDGES = [
    (1.8, 2.0, 160), (3.5, 4.0, 80), (7.0, 7.3, 40),
    (14.0, 14.35, 20), (21.0, 21.45, 15), (28.0, 29.7, 10),
]


def freq_to_band_int(freq_mhz):
    """14.25 -> 20. Fallback when a QSO has a frequency but no band string."""
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return 0
    for lo, hi, b in _BAND_EDGES:
        if lo <= f <= hi:
            return b
    return 0


def resolve_band(band, freq_mhz=None):
    """Band int from the band string, falling back to the frequency."""
    return band_to_int(band) or freq_to_band_int(freq_mhz)


def is_cqww(contest):
    return (contest or "").upper().startswith("CQ-WW")


class CqwwScorer:
    """Accumulates points + per-band multipliers for one station's log."""

    def __init__(self, my_call, my_zone, lookup=None):
        self.lookup = lookup or get_lookup()
        c = self.lookup.resolve(my_call)
        self.my_country = c[0]
        self.my_continent = c[1]
        self.my_zone = my_zone
        self.worked = set()        # {(call, band)}
        self.zones = {}            # band -> set(zone)
        self.countries = {}        # band -> set(country)
        self.qso_points = 0
        self.counted = 0           # non-dupe, valid-band QSOs

    def add_qso(self, call, band_int, zone_rcvd):
        """Returns dict(points, dupe, new_zone, new_country, country, continent)."""
        call = (call or "").upper().strip()
        country, continent, _dz, is_mm = self.lookup.resolve(call)
        out = {"points": 0, "dupe": False, "new_zone": False,
               "new_country": False, "country": country,
               "continent": continent, "mm": is_mm}

        if band_int not in VALID_BANDS:
            return out
        key = (call, band_int)
        if key in self.worked:
            out["dupe"] = True
            return out
        self.worked.add(key)
        self.counted += 1

        # QSO points — based on location of the station worked.
        if continent and self.my_continent and continent != self.my_continent:
            pts = 3
        elif country and self.my_country and country == self.my_country:
            pts = 0
        elif self.my_continent == "NA":
            pts = 2
        else:
            pts = 1
        self.qso_points += pts
        out["points"] = pts

        # Zone multiplier (operator-entered received zone).
        if zone_rcvd:
            bz = self.zones.setdefault(band_int, set())
            if zone_rcvd not in bz:
                out["new_zone"] = True
                bz.add(zone_rcvd)

        # Country multiplier — never for /MM or unresolved.
        if not is_mm and country:
            bc = self.countries.setdefault(band_int, set())
            if country not in bc:
                out["new_country"] = True
                bc.add(country)
        return out

    @property
    def zone_mults(self):
        return sum(len(s) for s in self.zones.values())

    @property
    def country_mults(self):
        return sum(len(s) for s in self.countries.values())

    @property
    def score(self):
        return self.qso_points * (self.zone_mults + self.country_mults)

    def summary(self):
        return {
            "qsos": self.counted,
            "points": self.qso_points,
            "zones": self.zone_mults,
            "countries": self.country_mults,
            "mults": self.zone_mults + self.country_mults,
            "score": self.score,
        }
