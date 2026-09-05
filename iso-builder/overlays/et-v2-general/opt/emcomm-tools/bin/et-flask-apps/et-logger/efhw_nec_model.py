#!/usr/bin/env python3
"""
efhw_nec_model.py
==================
Outil de modelisation NEC (PyNEC) pour antennes End-Fed Half-Wave (EFHW)
en configuration sloper (fil incline, alimente a une extremite en hauteur,
extremite basse pres du sol).

Developpe pour VA2OPS - reutilisable pour n'importe quelle EFHW/sloper,
en changeant simplement les parametres d'entree.

INSTALLATION
------------
    pip install PyNEC --break-system-packages

UTILISATION RAPIDE
------------------
    python3 efhw_nec_model.py

Ou en important les fonctions dans un autre script / notebook :

    from efhw_nec_model import get_pattern, print_pattern, great_circle_bearing

Auteur: Sylvain Deguire (VA2OPS)
"""

import math

# PyNEC est chargé en différé — laisse et-logger démarrer sans PyNEC
# (utile sur les machines de dev). Les appels à get_pattern() lèveront
# alors ImportError, les routes /api/antennas/* doivent le gérer.
nec = None

def _load_nec():
    global nec
    if nec is None:
        import PyNEC as _mod
        nec = _mod
    return nec

FT2M = 0.3048
C_M_PER_US = 299.792458   # vitesse de la lumière (m par microseconde)


def circular_azimuth_distance(a_deg, b_deg):
    """Distance angulaire en degrés entre deux azimuts, sur le cercle 0-360.
    Utilise ceci partout où on cherche l'échantillon de patron le plus
    proche d'un azimut cible — sinon on se trompe près de la couture 0/360
    (ex: cible à 5°, patron à 355° et 5° : abs(355-5)=350, faux; correct=10)."""
    d = abs(a_deg - b_deg) % 360.0
    return min(d, 360.0 - d)


# ---------------------------------------------------------------------------
# 1. GEOMETRIE ET MODELE NEC
# ---------------------------------------------------------------------------

def get_pattern(freq_mhz, feed_h_ft, far_h_ft, wire_len_ft, azimuth_deg,
                 takeoff_deg=20.0, wire_radius_m=0.0008,
                 ground_eps=13.0, ground_sig=0.005,
                 coax_radiator=None):
    """
    Calcule le patron de rayonnement azimutal (0-360 deg, pas de 10 deg)
    d'une EFHW en sloper, a un angle d'elevation (takeoff) donne.

    Parametres
    ----------
    freq_mhz : float
        Frequence d'operation en MHz (ex: 14.150 pour le 20m).
    feed_h_ft : float
        Hauteur du point d'alimentation (unun) en pieds.
    far_h_ft : float
        Hauteur de l'extremite eloignee du fil, en pieds.
    wire_len_ft : float
        Longueur physique totale du fil rayonnant, en pieds.
    azimuth_deg : float
        Azimut (0=Nord, 90=Est, 180=Sud, 270=Ouest) vers lequel pointe
        l'extremite eloignee du fil, depuis le point d'alimentation.
    takeoff_deg : float
        Angle d'elevation (angle de radiation) auquel evaluer le patron.
        20 deg est un choix courant pour du DX; utiliser 30-40 deg pour
        du NVIS / trafic regional.
    wire_radius_m : float
        Rayon du fil rayonnant, en metres (defaut ~ #14-18 AWG).
    ground_eps, ground_sig : float
        Constante dielectrique et conductivite du sol (defaut = "sol moyen").
        Sol tres conducteur (mer, terre humide riche): eps~80, sig~5
        Sol tres pauvre (sable sec, roche): eps~5, sig~0.001
    coax_radiator : dict ou None
        Si fourni, ajoute un second fil representant le coax non isole
        (ou partiellement isole) partant du point d'alimentation.
        Cles attendues:
            'dx_m', 'dy_m', 'dz_m' : deplacement (metres) du bout du coax
                                       par rapport au point d'alimentation
            'radius_m'             : rayon du conducteur exterieur du coax
            'choke_R', 'choke_X'   : impedance serie (ohms) du choke a
                                       inserer pres du point d'alimentation
                                       (0, 0 = pas de choke / connexion directe;
                                       quelques milliers d'ohms = bon choke)

    Retour
    ------
    list de tuples (azimut_deg, gain_dBi)
    """
    feed_h = feed_h_ft * FT2M
    far_h = far_h_ft * FT2M
    wire_len = wire_len_ft * FT2M

    dz = feed_h - far_h
    horiz = math.sqrt(max(wire_len ** 2 - dz ** 2, 0.0))
    # CONVENTION (fix 2026-07-14): NEC mesure phi depuis l'axe +X en sens
    # antihoraire, et les appelants lisent phi comme un cap compas (0=Nord,
    # sens horaire). Ces deux conventions sont des images miroir l'une de
    # l'autre. On place donc le fil a l'angle NEC phi = azimuth_deg
    # (dx=cos, dy=sin) : le modele est le miroir du fil physique, et la
    # lecture phi-comme-compas (elle-meme un miroir) restitue exactement
    # le patron physique. Meme principe que le fix delta_loop 2026-07-13.
    az = math.radians(azimuth_deg)
    dx = horiz * math.cos(az)
    dy = horiz * math.sin(az)

    # Segments proportionnels à la fréquence : au moins ~25 segments/λ pour
    # que NEC reste précis. À 14 MHz sur 20 m ≈ 1λ, 21 segments suffisent.
    # À 28 MHz même antenne = 2λ, il en faut ~50.
    wavelength_m = C_M_PER_US / freq_mhz     # c / f ; freq en MHz donne λ en m
    main_segs = max(21, int(wire_len / wavelength_m * 25))
    if main_segs % 2 == 0:
        main_segs += 1                        # NEC préfère un nombre impair

    _load_nec()
    context = nec.nec_context()
    geo = context.get_geometry()

    # Fil rayonnant principal (tag 1), alimente au segment 1 (extremite haute)
    geo.wire(1, main_segs, 0.0, 0.0, feed_h, dx, dy, far_h, wire_radius_m, 1.0, 1.0)

    if coax_radiator is not None:
        cx = coax_radiator.get('dx_m', 0.0)   # deplacement physique vers l'EST
        cy = coax_radiator.get('dy_m', 5.0)   # deplacement physique vers le NORD
        cz = coax_radiator.get('dz_m', 0.3)
        crad = coax_radiator.get('radius_m', 0.00545)  # ~LMR-400
        coax_len = math.sqrt(cx * cx + cy * cy + (cz - feed_h) ** 2)
        coax_segs = max(11, int(coax_len / wavelength_m * 25))
        if coax_segs % 2 == 0:
            coax_segs += 1
        # Miroir physique->modele (voir note de convention ci-dessus):
        # un point physique (Est, Nord) se place en (Nord, Est) dans NEC.
        geo.wire(2, coax_segs, 0.0, 0.0, feed_h, cy, cx, cz, crad, 1.0, 1.0)

    context.geometry_complete(0)
    context.gn_card(2, 0, ground_eps, ground_sig, 0, 0, 0, 0)
    context.fr_card(0, 1, freq_mhz, 0)
    context.ex_card(0, 1, 1, 0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    if coax_radiator is not None:
        choke_R = coax_radiator.get('choke_R', 0.0)
        choke_X = coax_radiator.get('choke_X', 0.0)
        # Impedance serie (type 4) appliquee au 1er segment du coax (tag 2),
        # pres du point d'alimentation -- c'est la ou un vrai choke serait installe.
        context.ld_card(4, 2, 1, 1, choke_R, choke_X, 0.0)

    theta = 90.0 - takeoff_deg  # NEC mesure theta depuis le zenith
    context.rp_card(0, 1, 37, 0, 5, 0, 0, theta, 0.0, 0.0, 10.0, 0.0, 0.0)

    rp = context.get_radiation_pattern(0)
    gains = rp.get_gain()
    phis = rp.get_phi_angles()
    return list(zip(phis, gains[0]))


def _finish_and_sample(context, freq_mhz, feed_tag, feed_seg,
                        takeoff_deg, ground_eps, ground_sig):
    """Boilerplate after geometry: ground, freq, excitation, RP card, return
    the pattern. Extracted so each get_pattern_* function stays readable."""
    context.geometry_complete(0)
    context.gn_card(2, 0, ground_eps, ground_sig, 0, 0, 0, 0)
    context.fr_card(0, 1, freq_mhz, 0)
    context.ex_card(0, feed_tag, feed_seg, 0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    theta = 90.0 - takeoff_deg
    context.rp_card(0, 1, 37, 0, 5, 0, 0, theta, 0.0, 0.0, 10.0, 0.0, 0.0)
    rp = context.get_radiation_pattern(0)
    return list(zip(rp.get_phi_angles(), rp.get_gain()[0]))


def get_pattern_delta_loop(freq_mhz, apex_height_ft, base_corner_ft,
                            perimeter_ft, azimuth_deg,
                            takeoff_deg=20.0, wire_radius_m=0.0008,
                            ground_eps=13.0, ground_sig=0.005):
    """Inverted delta loop, APEX DOWN (fed at the bottom apex, base at top).
    Models Chameleon CHA TDL and similar tactical delta loops.

    Geometry: closed isoceles triangle, 3 wires:
        - apex (feed point, near the ground) → each top corner (2 legs)
        - top corner → top corner (base wire at the top)

    Parameters
    ----------
    apex_height_ft   : apex/feed height above ground.
    base_corner_ft   : HALF-width of the top base — distance from the
                       centerline to each top corner. Top base length =
                       2 × base_corner_ft.
    perimeter_ft     : full wire length (2 legs + base).
    azimuth_deg      : bearing (0=N, 90=E) of the MAIN LOBE (broadside).
                       Loop is bidirectional above ~10 MHz — the other
                       lobe is at azimuth_deg + 180°. Nulls sit ±90° from
                       the main lobe.

    Retour: list de (azimut_deg, gain_dBi).

    Fix corrigé 2026-07-13 : le patron NEC retourné utilise phi=0 sur
    l'axe +X, alors que les appelants traitent phi comme un cap compas
    (0=Nord). Sans correction, le lobe visé apparaissait décalé de 90°
    (Kentucky sous le bon lobe donnait un gain négatif, la France dans
    le null E-O gagnait au lieu de perdre). La rotation appliquée ci-
    dessous fait que `azimuth_deg` représente directement la direction
    du lobe principal, telle qu'un opérateur radioamateur la comprend.
    """
    _load_nec()

    apex_h = apex_height_ft * FT2M
    base_corner = base_corner_ft * FT2M
    perimeter = perimeter_ft * FT2M

    base_full = 2 * base_corner
    leg_len = (perimeter - base_full) / 2.0
    if leg_len <= base_corner:
        raise ValueError("Géométrie impossible: périmètre trop court pour "
                          "ce base_corner (la jambe doit être plus longue "
                          "que le demi-base).")

    corner_rise = math.sqrt(leg_len ** 2 - base_corner ** 2)
    corner_h = apex_h + corner_rise    # sommets du haut

    # Base initialement le long de +X → broadside naturel le long de +Y.
    # NEC retourne phi avec 0° = +X, mais l'appelant traite phi comme cap
    # compas (0° = Nord). Décalage de -90° pour que azimuth_deg = direction
    # du LOBE principal.
    rotation_deg = (azimuth_deg - 90.0) % 360.0
    az = math.radians(rotation_deg)
    ca, sa = math.cos(az), math.sin(az)

    def rotate(x0, y0):
        return x0 * ca - y0 * sa, x0 * sa + y0 * ca

    c1x, c1y = rotate(-base_corner, 0.0)
    c2x, c2y = rotate( base_corner, 0.0)

    # Segments proportionnels à λ (≥25 seg/λ) — précision NEC.
    wavelength_m = C_M_PER_US / freq_mhz
    def nseg(length_m, floor=11):
        n = max(floor, int(length_m / wavelength_m * 25))
        return n + (0 if n % 2 else 1)   # préfère impair

    n_leg = nseg(leg_len)
    n_base = nseg(base_full, floor=15)

    context = nec.nec_context()
    geo = context.get_geometry()
    # jambe 1 : apex → coin 1  (FEED sur segment 1)
    geo.wire(1, n_leg, 0.0, 0.0, apex_h, c1x, c1y, corner_h,
             wire_radius_m, 1.0, 1.0)
    # jambe 2 : apex → coin 2
    geo.wire(2, n_leg, 0.0, 0.0, apex_h, c2x, c2y, corner_h,
             wire_radius_m, 1.0, 1.0)
    # base : coin 1 → coin 2
    geo.wire(3, n_base, c1x, c1y, corner_h, c2x, c2y, corner_h,
             wire_radius_m, 1.0, 1.0)

    return _finish_and_sample(context, freq_mhz,
                                feed_tag=1, feed_seg=1,
                                takeoff_deg=takeoff_deg,
                                ground_eps=ground_eps, ground_sig=ground_sig)


def get_pattern_horizontal_loop(freq_mhz, height_ft, perimeter_ft,
                                 takeoff_deg=20.0, wire_radius_m=0.0008,
                                 ground_eps=13.0, ground_sig=0.005):
    """Full-wave horizontal loop (square), a.k.a. "skywarmer" / "cloud burner".

    Very broad azimuth pattern (nearly omni). Elevation peaks straight
    up when the loop is < ~0.15 λ high, drops toward the horizon as
    height increases. Great for NVIS and short-skip.

    Parameters
    ----------
    height_ft   : uniform height above ground of all four sides.
    perimeter_ft: total wire length (4 sides), in feet.

    azimuth is meaningless for a horizontal square — result is polled
    over 0-360° like the sloper for uniformity of API.
    """
    _load_nec()

    h = height_ft * FT2M
    p = perimeter_ft * FT2M
    side = p / 4.0

    wavelength_m = C_M_PER_US / freq_mhz
    n_side = max(11, int(side / wavelength_m * 25))
    if n_side % 2 == 0:
        n_side += 1

    # Four corners of a square centered at origin
    hs = side / 2.0
    c1 = (-hs, -hs, h)
    c2 = ( hs, -hs, h)
    c3 = ( hs,  hs, h)
    c4 = (-hs,  hs, h)

    context = nec.nec_context()
    geo = context.get_geometry()
    geo.wire(1, n_side, *c1, *c2, wire_radius_m, 1.0, 1.0)   # south side (feed)
    geo.wire(2, n_side, *c2, *c3, wire_radius_m, 1.0, 1.0)
    geo.wire(3, n_side, *c3, *c4, wire_radius_m, 1.0, 1.0)
    geo.wire(4, n_side, *c4, *c1, wire_radius_m, 1.0, 1.0)

    return _finish_and_sample(context, freq_mhz,
                                feed_tag=1, feed_seg=(n_side + 1) // 2,
                                takeoff_deg=takeoff_deg,
                                ground_eps=ground_eps, ground_sig=ground_sig)


# ---------------------------------------------------------------------------
# 2. AFFICHAGE / ANALYSE
# ---------------------------------------------------------------------------

def print_pattern(data, label=""):
    """Affiche le patron en texte avec max/min identifies."""
    best = max(data, key=lambda t: t[1])
    worst = min(data, key=lambda t: t[1])
    print(f"--- {label} ---")
    for phi, g in data:
        bar = "#" * int(max(g - worst[1], 0) * 2)
        print(f"  az {phi:5.1f} deg   gain {g:6.2f} dBi   {bar}")
    print(f"  MAX: az={best[0]:.1f} deg  gain={best[1]:.2f} dBi")
    print(f"  MIN (null): az={worst[0]:.1f} deg  gain={worst[1]:.2f} dBi")
    print(f"  Ecart max-min: {best[1] - worst[1]:.2f} dB")
    print()


def gain_at_azimuth(data, target_az):
    """Retourne le gain (dBi) au point du patron le plus proche de target_az.
    Utilise la distance circulaire pour ne pas se planter à la couture 0/360."""
    closest = min(data, key=lambda t: circular_azimuth_distance(t[0], target_az))
    return closest[1]


# ---------------------------------------------------------------------------
# 3. AZIMUT REEL VERS UNE CIBLE (GRAND CERCLE)
# ---------------------------------------------------------------------------

def great_circle_bearing(lat1, lon1, lat2, lon2):
    """
    Calcule l'azimut initial (en degres, 0-360) du grand cercle entre deux
    points geographiques (latitude/longitude en degres decimales).

    Rappel important: sur de longues distances (transatlantique, transpacifique),
    ce n'est PAS l'azimut "intuitif" d'une carte plate (Mercator). Toujours
    calculer, ne jamais deviner a l'oeil sur une carte.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360) % 360


# ---------------------------------------------------------------------------
# 4. EXEMPLES D'UTILISATION (tes antennes)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 70)
    print("EXEMPLE 1 - Ultimax 10-40 @ QTH VA2OPS (66 pi, 35->8 pi, sud/180)")
    print("=" * 70)
    data_ultimax = get_pattern(
        freq_mhz=14.150, feed_h_ft=35, far_h_ft=8,
        wire_len_ft=66, azimuth_deg=180, takeoff_deg=20
    )
    print_pattern(data_ultimax, "Ultimax 10-40, 20m, takeoff 20 deg")

    print("=" * 70)
    print("EXEMPLE 2 - Meme antenne, coax LMR-400 SANS choke")
    print("=" * 70)
    data_no_choke = get_pattern(
        freq_mhz=14.150, feed_h_ft=35, far_h_ft=8,
        wire_len_ft=66, azimuth_deg=180, takeoff_deg=20,
        coax_radiator={'dx_m': 0.0, 'dy_m': 5.0, 'dz_m': 0.3,
                        'radius_m': 0.00545, 'choke_R': 0.0, 'choke_X': 0.0}
    )
    print_pattern(data_no_choke, "Ultimax 10-40 + LMR-400 sans choke")

    print("=" * 70)
    print("EXEMPLE 3 - Meme antenne, coax AVEC bon choke (MFJ-915 ~3000+j2000 ohms)")
    print("=" * 70)
    data_choke = get_pattern(
        freq_mhz=14.150, feed_h_ft=35, far_h_ft=8,
        wire_len_ft=66, azimuth_deg=180, takeoff_deg=20,
        coax_radiator={'dx_m': 0.0, 'dy_m': 5.0, 'dz_m': 0.3,
                        'radius_m': 0.00545, 'choke_R': 3000.0, 'choke_X': 2000.0}
    )
    print_pattern(data_choke, "Ultimax 10-40 + LMR-400 avec choke")

    print("=" * 70)
    print("EXEMPLE 4 - Fleep-tenna N9SAB, section 20m (32 pi, 22.5->3 pi)")
    print("=" * 70)
    data_fleep = get_pattern(
        freq_mhz=14.150, feed_h_ft=22.5, far_h_ft=3,
        wire_len_ft=32, azimuth_deg=180, takeoff_deg=20
    )
    print_pattern(data_fleep, "Fleep-tenna 20m sloper, takeoff 20 deg")

    print("=" * 70)
    print("EXEMPLE 5 - Azimut reel Montreal -> Paris, et gain a cet azimut")
    print("=" * 70)
    mtl = (45.5017, -73.5673)
    paris = (48.8566, 2.3522)
    brg = great_circle_bearing(*mtl, *paris)
    print(f"Azimut grand cercle Montreal -> Paris: {brg:.1f} deg")
    print(f"Gain de l'Ultimax a cet azimut (20 deg takeoff): "
          f"{gain_at_azimuth(data_ultimax, brg):.2f} dBi")
    print()
    print("Astuce: remplace (mtl) et (paris) par tes propres coordonnees")
    print("(QTH et cible DX) pour verifier n'importe quel autre trajet.")
