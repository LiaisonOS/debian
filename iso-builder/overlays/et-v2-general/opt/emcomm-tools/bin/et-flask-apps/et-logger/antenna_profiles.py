#!/usr/bin/env python3
"""
antenna_profiles.py
====================
Module de gestion de profils d'antennes + selection de la meilleure
antenne pour une cible donnee (indicatif ou site POTA), a integrer
dans et-logger (onglet carte).

Concept
-------
Tu configures tes antennes une fois (type, hauteurs, orientation, bandes
couvertes). Quand tu entends une station ou un POTA, l'app:
  1. Recupere les coordonnees de la cible (via ta DB indicatifs/POTA existante)
  2. Calcule l'azimut reel (grand cercle) depuis ton QTH/site vers la cible
  3. Regarde, pour chaque antenne configuree et disponible sur la bande
     du moment, le gain reel (calcule via NEC) dans cette direction
  4. Te dit laquelle utiliser -- ou si TOUTES tes antennes sont dans un
     null, te dit clairement "skip, chasse quelqu'un d'autre"

Depend de efhw_nec_model.py (le script NEC qu'on a construit ensemble).

INTEGRATION SQLite (suggestion de schema pour et-logger)
---------------------------------------------------------
CREATE TABLE antenna_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,              -- ex: "Ultimax 10-40 QTH"
    ant_type      TEXT NOT NULL,               -- 'sloper_efhw' | 'vertical_omni' | 'inverted_v_nvis'
    is_omni       INTEGER NOT NULL DEFAULT 0,  -- 1 pour verticales omnidirectionnelles
    feed_h_ft     REAL,                        -- hauteur au point d'alimentation
    far_h_ft      REAL,                        -- hauteur extremite eloignee (slopers)
    wire_len_ft   REAL,                        -- longueur du fil rayonnant (par bande si multi)
    azimuth_deg   REAL,                        -- orientation de l'extremite eloignee (slopers)
    bands_mhz     TEXT,                        -- JSON: {"20m": 14.150, "40m": 7.100, ...}
    site_lat      REAL,                        -- QTH fixe ou site POTA au moment du deploiement
    site_lon      REAL,
    active        INTEGER NOT NULL DEFAULT 1,  -- deployee actuellement?
    notes         TEXT
);

CREATE TABLE antenna_pattern_cache (
    profile_id    INTEGER NOT NULL,
    band_mhz      REAL NOT NULL,
    takeoff_deg   REAL NOT NULL,
    pattern_json  TEXT NOT NULL,   -- [[az, gain_dBi], ...] pas de 10 deg
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (profile_id, band_mhz, takeoff_deg),
    FOREIGN KEY (profile_id) REFERENCES antenna_profiles(id)
);
"""

import math
import json
from dataclasses import dataclass, field
from typing import Optional

from efhw_nec_model import (
    get_pattern,
    get_pattern_delta_loop,
    get_pattern_horizontal_loop,
    great_circle_bearing,
    circular_azimuth_distance,
)

EARTH_R_KM = 6371.0


# ---------------------------------------------------------------------------
# 1. MODELE DE PROFIL D'ANTENNE
# ---------------------------------------------------------------------------

@dataclass
class AntennaProfile:
    name: str
    ant_type: str                      # 'sloper_efhw' | 'vertical_omni' | 'inverted_v_nvis'
    bands_mhz: dict                    # ex: {"20m": 14.150, "40m": 7.100}
    site_lat: float
    site_lon: float
    is_omni: bool = False
    feed_h_ft: Optional[float] = None
    far_h_ft: Optional[float] = None
    wire_len_ft: Optional[float] = None
    azimuth_deg: Optional[float] = None   # ignore si is_omni=True
    notes: str = ""


# Exemple: tes antennes actuelles (a ajuster/sauver en DB dans et-logger)
SYLVAIN_ANTENNAS = [
    AntennaProfile(
        name="Ultimax 10-40 (QTH)",
        ant_type="sloper_efhw",
        bands_mhz={"20m": 14.150, "40m": 7.100},
        site_lat=45.5017, site_lon=-73.5673,   # QTH Montreal (a ajuster)
        feed_h_ft=35, far_h_ft=8, wire_len_ft=66, azimuth_deg=180,
        notes="Sloper fixe, oriente sud, transfo cote maison",
    ),
    AntennaProfile(
        name="DX Commander (QTH)",
        ant_type="vertical_omni",
        bands_mhz={"20m": 14.150, "15m": 21.200, "10m": 28.400},
        site_lat=45.5017, site_lon=-73.5673,
        is_omni=True,
        notes="Verticale multi-bande avec radians",
    ),
    AntennaProfile(
        name="Fleep-tenna (portable)",
        ant_type="sloper_efhw",
        bands_mhz={"20m": 14.150, "40m": 7.100, "15m": 21.200, "10m": 28.400},
        site_lat=45.5017, site_lon=-73.5673,   # a mettre a jour au site POTA du jour
        feed_h_ft=22.5, far_h_ft=3, wire_len_ft=32, azimuth_deg=180,
        notes="Sloper portable, orientation variable selon le site",
    ),
]


# ---------------------------------------------------------------------------
# 2. GEOMETRIE: DISTANCE + AZIMUT VERS LA CIBLE
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Distance grand cercle en km entre deux points GPS."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def suggested_takeoff_angle(distance_km):
    """
    Heuristique simple liant distance et angle d'elevation typique.
    (approximation grossiere -- a raffiner plus tard avec VOACAP/hop count)
        < 300 km   -> NVIS,        angle ~50 deg
        300-1500km -> 1 saut,      angle ~20-25 deg
        > 1500 km  -> DX/multi-saut, angle ~10-15 deg
    """
    if distance_km < 300:
        return 50.0
    elif distance_km < 1500:
        return 22.0
    else:
        return 12.0


# ---------------------------------------------------------------------------
# 3. CACHE DE PATRON PAR ANTENNE/BANDE
# ---------------------------------------------------------------------------

_pattern_cache = {}  # (name, band, takeoff) -> [(az, gain), ...]  -- remplacer par SQLite en prod


def get_cached_pattern(profile: AntennaProfile, band_name: str, takeoff_deg: float):
    """Retourne (et met en cache) le patron NEC pour un profil+bande+angle donnes."""
    if band_name not in profile.bands_mhz:
        return None  # cette antenne ne couvre pas cette bande

    key = (profile.name, band_name, round(takeoff_deg, 1))
    if key in _pattern_cache:
        return _pattern_cache[key]

    freq_mhz = profile.bands_mhz[band_name]

    ant_type = (profile.ant_type or "").lower()

    if ant_type == "horizontal_efhw":
        # Horizontal EFHW (NVIS-style): both ends at the same height. Reuses
        # the sloper model with feed_h == far_h — dz=0 → wire lies flat.
        # Field reuse: feed_h_ft → uniform height, wire_len_ft → length,
        # azimuth_deg → wire direction. far_h_ft is ignored.
        h = profile.feed_h_ft
        pattern = get_pattern(
            freq_mhz=freq_mhz,
            feed_h_ft=h,
            far_h_ft=h,
            wire_len_ft=profile.wire_len_ft,
            azimuth_deg=profile.azimuth_deg,
            takeoff_deg=takeoff_deg,
        )
    elif ant_type == "delta_loop":
        # Inverted delta loop (CHA TDL style — apex at bottom, base at top).
        # Field reuse (with NEW semantics after the 2026-07-13 rotation fix):
        #   feed_h_ft   → apex height (bottom, feed point near unun)
        #   far_h_ft    → base HALF-width (centerline → each top corner)
        #   wire_len_ft → full perimeter (~ 1 λ)
        #   azimuth_deg → MAIN LOBE direction (0=N, 90=E, ...)
        pattern = get_pattern_delta_loop(
            freq_mhz=freq_mhz,
            apex_height_ft=profile.feed_h_ft,
            base_corner_ft=profile.far_h_ft,
            perimeter_ft=profile.wire_len_ft,
            azimuth_deg=profile.azimuth_deg,
            takeoff_deg=takeoff_deg,
        )
    elif ant_type == "horizontal_loop":
        # Full-wave horizontal square loop (skywarmer / NVIS).
        # Field reuse:  feed_h_ft → loop height   wire_len_ft → perimeter.
        pattern = get_pattern_horizontal_loop(
            freq_mhz=freq_mhz,
            height_ft=profile.feed_h_ft,
            perimeter_ft=profile.wire_len_ft,
            takeoff_deg=takeoff_deg,
        )
    elif profile.is_omni:
        # Verticale omni: gain approx constant en azimut. Valeur nominale
        # raisonnable pour une verticale avec bons radians a cet angle --
        # a affiner plus tard avec un vrai modele NEC de la verticale.
        nominal_gain = 1.5 if takeoff_deg <= 25 else -1.0
        pattern = [(az, nominal_gain) for az in range(0, 360, 10)]
    else:
        # Sloper EFHW / Inverted-V — original single-wire model.
        pattern = get_pattern(
            freq_mhz=freq_mhz,
            feed_h_ft=profile.feed_h_ft,
            far_h_ft=profile.far_h_ft,
            wire_len_ft=profile.wire_len_ft,
            azimuth_deg=profile.azimuth_deg,
            takeoff_deg=takeoff_deg,
        )

    _pattern_cache[key] = pattern
    return pattern


def gain_toward(profile: AntennaProfile, band_name: str, target_azimuth_deg: float,
                 takeoff_deg: float):
    """Gain (dBi) de ce profil vers un azimut cible, à un angle d'élévation donné.
    Distance angulaire circulaire — sinon on se trompe près de la couture 0/360."""
    pattern = get_cached_pattern(profile, band_name, takeoff_deg)
    if pattern is None:
        return None
    closest = min(pattern,
                  key=lambda t: circular_azimuth_distance(t[0], target_azimuth_deg))
    return closest[1]


# ---------------------------------------------------------------------------
# 4. SELECTION DE LA MEILLEURE ANTENNE POUR UNE CIBLE
# ---------------------------------------------------------------------------

NULL_THRESHOLD_DBI = -6.0  # en dessous de ca, on considere que c'est un "null" a eviter


def best_antenna_for_target(profiles, target_lat, target_lon, band_name,
                              takeoff_deg=None):
    """
    Pour une cible (callsign ou POTA deja resolu en lat/lon), retourne
    la liste des antennes disponibles sur cette bande, triees du meilleur
    gain au pire, avec un flag "null" si le gain est sous le seuil.

    Retour: liste de dicts:
        {name, gain_dBi, azimuth_deg, distance_km, takeoff_deg, is_null}
    """
    results = []
    for profile in profiles:
        if band_name not in profile.bands_mhz:
            continue

        distance_km = haversine_km(profile.site_lat, profile.site_lon,
                                     target_lat, target_lon)
        azimuth = great_circle_bearing(profile.site_lat, profile.site_lon,
                                         target_lat, target_lon)
        actual_takeoff = takeoff_deg or suggested_takeoff_angle(distance_km)
        gain = gain_toward(profile, band_name, azimuth, actual_takeoff)

        results.append({
            "name": profile.name,
            "gain_dBi": round(gain, 2) if gain is not None else None,
            "azimuth_deg": round(azimuth, 1),
            "distance_km": round(distance_km, 0),
            "takeoff_deg": actual_takeoff,
            "is_null": (gain is not None and gain < NULL_THRESHOLD_DBI),
        })

    results.sort(key=lambda r: (r["gain_dBi"] is None, -(r["gain_dBi"] or -99)))
    return results


def print_recommendation(results, target_label=""):
    print(f"--- Recommandation d'antenne pour {target_label} ---")
    if not results:
        print("  Aucune antenne configuree ne couvre cette bande.")
        return
    for r in results:
        flag = "  <-- NULL, EVITE" if r["is_null"] else ""
        print(f"  {r['name']:<28} gain={r['gain_dBi']:>6} dBi  "
              f"az={r['azimuth_deg']:>5.1f} deg  dist={r['distance_km']:>6.0f} km  "
              f"takeoff={r['takeoff_deg']:.0f} deg{flag}")
    best = results[0]
    if best["is_null"]:
        print(f"  >>> ATTENTION: meme ta meilleure antenne ({best['name']}) est faible "
              f"({best['gain_dBi']} dBi). Envisage de chasser une autre cible.")
    else:
        print(f"  >>> Utilise: {best['name']} ({best['gain_dBi']} dBi)")
    print()


# ---------------------------------------------------------------------------
# 5. EXEMPLE D'UTILISATION
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Exemple 1: une station au Kentucky (approx Lexington, KY)
    kentucky = (38.0406, -84.5037)
    res = best_antenna_for_target(SYLVAIN_ANTENNAS, *kentucky, band_name="20m")
    print_recommendation(res, "station au Kentucky (20m)")

    # Exemple 2: un DX vers la France (Paris)
    paris = (48.8566, 2.3522)
    res2 = best_antenna_for_target(SYLVAIN_ANTENNAS, *paris, band_name="20m")
    print_recommendation(res2, "DX France (20m)")

    # Exemple 3: un POTA proche pour du NVIS (ex: Estrie, QC ~150km)
    estrie = (45.40, -71.90)
    res3 = best_antenna_for_target(SYLVAIN_ANTENNAS, *estrie, band_name="40m")
    print_recommendation(res3, "POTA Estrie, proche (40m, probable NVIS)")
