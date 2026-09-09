#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carte.py — Génère une carte HTML autonome des points de passage de vacances.

Chaîne : liste de villes/sites  ->  géocodage (Nominatim/OpenStreetMap)
         ->  vérification point par point  ->  carte Leaflet multi-fonds
             (OSM, satellite Esri, IGN Plan/Carte/Ortho, OpenTopoMap...).

Tout est gratuit et sans clé d'API. Aucune dépendance : stdlib uniquement.

Usage :
    python3 carte.py points.txt                  # géocode + génère carte.html
    python3 carte.py points.txt --verifier       # validation interactive de chaque point
    python3 carte.py points.txt -o bretagne.html --titre "Bretagne 2026"
    python3 carte.py points.txt --gpx trace.gpx --geojson points.geojson
"""

import argparse
import csv
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Géocodage
# --------------------------------------------------------------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim exige un User-Agent identifiable et max 1 requête/seconde.
USER_AGENT = "mapspoints/1.0 (carte de voyage personnelle)"
DELAI_MIN = 1.1  # secondes entre deux requêtes

_dernier_appel = [0.0]
_dernier_osrm = [0.0]


def _throttle(etat=_dernier_appel, delai=DELAI_MIN):
    """Espace les appels : ces services publics sont gratuits, on les ménage."""
    ecart = time.time() - etat[0]
    if ecart < delai:
        time.sleep(delai - ecart)
    etat[0] = time.time()


def nominatim_recherche(requete, limite=5, langue="fr", pays=None):
    """Renvoie une liste de candidats [{lat, lon, nom, type, ...}]."""
    params = {
        "q": requete,
        "format": "jsonv2",
        "limit": str(limite),
        "addressdetails": "1",
        "accept-language": langue,
    }
    if pays:
        params["countrycodes"] = pays
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=20) as rep:
            brut = json.loads(rep.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"    ! erreur HTTP {e.code} sur « {requete} »", file=sys.stderr)
        return []
    except Exception as e:  # réseau, timeout, JSON...
        print(f"    ! erreur réseau sur « {requete} » : {e}", file=sys.stderr)
        return []

    candidats = []
    for r in brut:
        adr = r.get("address", {})
        candidats.append({
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "adresse": r.get("display_name", ""),
            "type": f"{r.get('category', r.get('class', ''))}/{r.get('type', '')}",
            "importance": r.get("importance", 0),
            "pays": adr.get("country", ""),
            "region": adr.get("state", adr.get("county", "")),
            "source": "nominatim",
        })
    return candidats


# --------------------------------------------------------------------------
# Itinéraires routiers (OSRM public, gratuit et sans clé)
# --------------------------------------------------------------------------

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"


def itineraire_routier(a, b):
    """
    Route conduite entre deux (lat, lon). Renvoie {"pts", "km", "min"},
    ou None si aucune route n'existe (île sans bac, point en pleine nature...).
    """
    url = (f"{OSRM_URL}{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
           "?overview=simplified&geometries=geojson")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    _throttle(_dernier_osrm, 1.0)
    try:
        with urllib.request.urlopen(req, timeout=30) as rep:
            data = json.loads(rep.read().decode("utf-8"))
    except Exception as e:
        print(f"    ! routage indisponible ({e})", file=sys.stderr)
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    r = data["routes"][0]
    return {
        "pts": [[lat, lon] for lon, lat in r["geometry"]["coordinates"]],
        "km": round(r["distance"] / 1000, 1),
        "min": round(r["duration"] / 60),
    }


def calculer_troncons(points, cache, router=False):
    """
    Un tronçon par couple d'étapes consécutives : tracé routier si possible,
    sinon segment droit (signalé comme tel). Renvoie (troncons, nb_droits).
    """
    troncons, droits = [], 0
    for n, (a, b) in enumerate(zip(points, points[1:]), 1):
        depart, arrivee = (a["lat"], a["lon"]), (b["lat"], b["lon"])
        route = None
        if router:
            cle = f"{depart[0]:.5f},{depart[1]:.5f}|{arrivee[0]:.5f},{arrivee[1]:.5f}"
            if cle in cache:
                route = cache[cle]
            else:
                route = itineraire_routier(depart, arrivee)
                cache[cle] = route          # None mémorisé aussi : inutile de réessayer
            if route:
                print(f"  {n:2d}→{n+1:<2d} {a['nom'][:28]:<28} "
                      f"{route['km']:>7.1f} km  {duree(route['min'])}")
        if route:
            troncons.append({**route, "route": True})
        else:
            droits += 1
            if router:
                print(f"  {n:2d}→{n+1:<2d} {a['nom'][:28]:<28}   pas de route "
                      "→ ligne droite")
            troncons.append({"pts": [list(depart), list(arrivee)],
                             "km": round(haversine(depart, arrivee), 1),
                             "min": None, "route": False})
    return troncons, droits


def duree(minutes):
    if minutes is None:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d}" if h else f"{m} min"


# --------------------------------------------------------------------------
# Lecture de la liste de points
# --------------------------------------------------------------------------

RE_COORD = re.compile(r"@\s*(-?\d+[.,]?\d*)\s*[,;/]\s*(-?\d+[.,]?\d*)\s*$")

# noms de colonnes acceptés dans un CSV, quelle que soit la casse ou l'accent
COLONNES = {
    "nom":     ("nom", "lieu", "name", "etape", "site", "destination", "titre", "ville"),
    "date":    ("date", "jour", "quand"),
    "note":    ("note", "commentaire", "description", "remarque", "detail", "infos"),
    "requete": ("requete", "recherche", "adresse", "query"),
    "lat":     ("lat", "latitude"),
    "lon":     ("lon", "lng", "long", "longitude"),
}
ALIAS = {alias: champ for champ, alias_du_champ in COLONNES.items()
         for alias in alias_du_champ}


def sans_accent(s):
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def nombre(v):
    """Accepte « 48.8566 » comme « 48,8566 » (décimale française)."""
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


def lire_texte(chemin):
    """Décode le fichier en tolérant le BOM d'Excel et l'ancien encodage Windows."""
    brut = Path(chemin).read_bytes()
    for encodage in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return brut.decode(encodage)
        except UnicodeDecodeError:
            continue
    return brut.decode("utf-8", errors="replace")


def detecter_separateur(entete):
    """Excel exporte en « ; » en France, en « , » ailleurs ; parfois tabulation."""
    try:
        return csv.Sniffer().sniff(entete, delimiters=";,\t").delimiter
    except csv.Error:
        return max(";,\t", key=entete.count)


def est_entete_csv(ligne, sep):
    champs = [sans_accent(c) for c in ligne.split(sep)]
    return len(champs) > 1 and all(c in ALIAS for c in champs if c)


def lire_csv(texte):
    lignes = [l for l in texte.splitlines() if l.strip()]
    sep = detecter_separateur(lignes[0])
    points = []
    for brute in csv.DictReader(lignes, delimiter=sep):
        # on renomme les colonnes reconnues, on ignore les autres
        ligne = {}
        for cle, val in brute.items():
            champ = ALIAS.get(sans_accent(cle))
            if champ:
                ligne[champ] = (val or "").strip()
        nom = ligne.get("nom", "")
        if not nom or nom.startswith("#"):
            continue
        points.append({
            "nom": nom,
            "date": ligne.get("date", ""),
            "note": ligne.get("note", ""),
            "requete": ligne.get("requete") or nom,
            "lat": nombre(ligne.get("lat")),
            "lon": nombre(ligne.get("lon")),
        })
    return points


def lire_liste(texte):
    points = []
    for brute in texte.splitlines():
        ligne = brute.strip()
        if not ligne or ligne.startswith("#"):
            continue
        champs = [c.strip() for c in ligne.split("|")]
        nom = champs[0]
        lat = lon = None
        # coordonnées explicites « @lat,lon » : on court-circuite le géocodage
        m = RE_COORD.search(nom)
        if m:
            lat = nombre(m.group(1))
            lon = nombre(m.group(2))
            nom = nom[:m.start()].strip()
        points.append({
            "nom": nom,
            "date": champs[1] if len(champs) > 1 else "",
            "note": champs[2] if len(champs) > 2 else "",
            "requete": champs[3] if len(champs) > 3 else nom,
            "lat": lat,
            "lon": lon,
        })
    return points


def lire_points(chemin):
    """
    Deux formats, reconnus automatiquement.

    1) Texte simple, une ligne par point, champs séparés par « | » :
           Pointe du Raz | 2026-07-14 | Coucher de soleil
           Locronan
           Bivouac du lac @45.1234, 6.5678
       Lignes vides et lignes commençant par # ignorées.

    2) CSV / tableur, avec une ligne d'en-têtes parmi :
           nom (ou lieu, ville, étape...), date, note, requete, lat, lon
       Séparateur « , », « ; » ou tabulation ; export Excel français accepté.
    """
    texte = lire_texte(chemin)
    lignes = [l for l in texte.splitlines() if l.strip()]
    if not lignes:
        return []

    est_csv = Path(chemin).suffix.lower() in (".csv", ".tsv")
    if not est_csv:  # un .txt peut aussi contenir un tableau : on regarde l'en-tête
        sep = detecter_separateur(lignes[0])
        est_csv = est_entete_csv(lignes[0], sep)
    return lire_csv(texte) if est_csv else lire_liste(texte)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def charger_cache(chemin):
    p = Path(chemin)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"! cache illisible ({chemin}), il sera recréé", file=sys.stderr)
    return {}


def sauver_cache(chemin, cache):
    Path(chemin).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def cle_cache(requete, pays):
    base = unicodedata.normalize("NFKD", requete.lower().strip())
    base = "".join(c for c in base if not unicodedata.combining(c))
    return f"{base}|{pays or ''}"


# --------------------------------------------------------------------------
# Résolution : géocodage + vérification point par point
# --------------------------------------------------------------------------

def afficher_candidats(candidats):
    for i, c in enumerate(candidats, 1):
        print(f"      [{i}] {c['adresse']}")
        print(f"          {c['lat']:.5f}, {c['lon']:.5f}   ({c['type']})")


def resoudre(points, cache, pays=None, interactif=False, forcer=False,
             revoir=frozenset()):
    """Complète chaque point avec lat/lon + statut. Renvoie (points, nb_problemes)."""
    problemes = 0

    for n, pt in enumerate(points, 1):
        etiquette = f"{n:2d}. {pt['nom']}"
        # ce point est-il à re-poser, et la question peut-elle être posée ?
        demander = interactif or n in revoir

        # 1. coordonnées fournies à la main dans la liste
        if pt["lat"] is not None and pt["lon"] is not None:
            pt["adresse"] = "coordonnées fournies"
            pt["statut"] = "ok"
            print(f"{etiquette:<40} ✓ {pt['lat']:.5f}, {pt['lon']:.5f}  (manuel)")
            continue

        cle = cle_cache(pt["requete"], pays)

        # 2. cache — sauté si le point est explicitement à revoir, ou s'il est
        #    douteux alors qu'on est justement en train de vérifier
        c = cache.get(cle)
        rejouer = forcer or n in revoir or (
            interactif and c is not None and c.get("statut", "ok") != "ok")
        if c is not None and not rejouer:
            pt.update(lat=c["lat"], lon=c["lon"], adresse=c["adresse"],
                      statut=c.get("statut", "ok"))
            marque = "✓" if pt["statut"] == "ok" else "⚠"
            print(f"{etiquette:<40} {marque} {pt['lat']:.5f}, {pt['lon']:.5f}  (cache)")
            continue

        # 3. géocodage
        candidats = nominatim_recherche(pt["requete"], limite=5, pays=pays)

        if not candidats:
            pt.update(lat=None, lon=None, adresse="", statut="introuvable")
            problemes += 1
            print(f"{etiquette:<40} ✗ INTROUVABLE")
            if demander:
                saisie = input("      Nouvelle recherche, « lat,lon », ou Entrée pour ignorer : ").strip()
                if saisie:
                    m = re.match(r"^(-?\d+[.,]?\d*)\s*[,;]\s*(-?\d+[.,]?\d*)$", saisie)
                    if m:
                        pt.update(lat=float(m.group(1).replace(",", ".")),
                                  lon=float(m.group(2).replace(",", ".")),
                                  adresse="saisie manuelle", statut="ok")
                        problemes -= 1
                    else:
                        candidats = nominatim_recherche(saisie, limite=5, pays=pays)
            if not candidats and pt["lat"] is None:
                continue

        if candidats:
            choix = 0
            # ambigu = plusieurs résultats dans des régions/pays différents
            ambigu = len(candidats) > 1 and (
                candidats[0]["pays"] != candidats[1]["pays"]
                or candidats[0]["region"] != candidats[1]["region"])

            if demander and (ambigu or len(candidats) > 1):
                print(f"{etiquette:<40} ? {len(candidats)} candidat(s) :")
                afficher_candidats(candidats)
                saisie = input("      Numéro [1], « lat,lon », ou « r » pour rechercher autrement : ").strip()
                if saisie.lower() == "r":
                    nouvelle = input("      Requête : ").strip()
                    if nouvelle:
                        candidats = nominatim_recherche(nouvelle, limite=5, pays=pays) or candidats
                elif re.match(r"^-?\d+[.,]?\d*\s*[,;]\s*-?\d+[.,]?\d*$", saisie):
                    a, b = re.split(r"\s*[,;]\s*", saisie)
                    candidats = [{"lat": float(a.replace(",", ".")),
                                  "lon": float(b.replace(",", ".")),
                                  "adresse": "saisie manuelle", "type": "manuel",
                                  "pays": "", "region": "", "source": "manuel"}]
                elif saisie.isdigit() and 1 <= int(saisie) <= len(candidats):
                    choix = int(saisie) - 1

            c = candidats[choix]
            statut = "ok" if (demander or not ambigu) else "ambigu"
            if statut == "ambigu":
                problemes += 1
            pt.update(lat=c["lat"], lon=c["lon"], adresse=c["adresse"], statut=statut)
            cache[cle] = {"lat": c["lat"], "lon": c["lon"],
                          "adresse": c["adresse"], "statut": statut}
            marque = "✓" if statut == "ok" else "⚠"
            print(f"{etiquette:<40} {marque} {pt['lat']:.5f}, {pt['lon']:.5f}")
            print(f"      {c['adresse']}")
            if statut == "ambigu":
                print(f"      ⚠ autre candidat possible : {candidats[1]['adresse']}")

    return points, problemes


# --------------------------------------------------------------------------
# Distances
# --------------------------------------------------------------------------

def choisir_points(points, expression):
    """Traduit « 9,Takayama » en numéros de points (1 = premier de la liste)."""
    choisis = set()
    for jeton in (j.strip() for j in expression.split(",")):
        if not jeton:
            continue
        if jeton.isdigit():
            n = int(jeton)
            if 1 <= n <= len(points):
                choisis.add(n)
            else:
                print(f"! --revoir : pas de point n°{n}", file=sys.stderr)
            continue
        cible = sans_accent(jeton)
        trouves = {n for n, p in enumerate(points, 1)
                   if cible in sans_accent(p["nom"])}
        if trouves:
            choisis |= trouves
        else:
            print(f"! --revoir : aucun point ne correspond à « {jeton} »",
                  file=sys.stderr)
    return choisis


def haversine(a, b):
    """Distance en km entre deux (lat, lon)."""
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def format_km(km):
    return f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km".replace(".", ",")


def rapport_distances(points, tout=False):
    """
    Distances entre étapes consécutives, et repérage des étapes douteuses.

    Un long trajet n'est pas suspect en soi (une journée de train peut faire
    200 km). Ce qui trahit un lieu mal géocodé, c'est le *détour* : l'étape est
    loin de ses deux voisines alors que celles-ci sont proches l'une de l'autre.
    """
    if len(points) < 2:
        return
    coord = lambda p: (p["lat"], p["lon"])
    troncons = [haversine(coord(a), coord(b)) for a, b in zip(points, points[1:])]
    mediane = sorted(troncons)[len(troncons) // 2]

    suspects = {}  # indice du point -> raison
    for i in range(1, len(points) - 1):
        detour = troncons[i - 1] + troncons[i] - haversine(coord(points[i - 1]),
                                                           coord(points[i + 1]))
        if detour > max(6 * mediane, 150):
            suspects[i] = f"détour de {format_km(detour)} par rapport aux étapes voisines"
    for i, d in enumerate(troncons):
        if d < 0.05:
            suspects[i + 1] = f"confondue avec l'étape {i + 1}"

    if tout:
        print("\nDistances entre étapes consécutives :")
        for i, d in enumerate(troncons):
            marque = "⚠" if (i in suspects or i + 1 in suspects) else " "
            trajet = f"{points[i]['nom']} → {points[i+1]['nom']}"
            print(f"  {marque} {i+1:2d}→{i+2:<3d} {trajet:<46} {format_km(d):>9}")

    print(f"\nTrajet : {format_km(sum(troncons))} au total, "
          f"étape la plus longue {format_km(max(troncons))}.")

    if suspects:
        print("\n⚠ Localisation à vérifier :")
        for i, raison in sorted(suspects.items()):
            print(f"    {i+1:2d}. {points[i]['nom']} — {raison}")
        numeros = ",".join(str(i + 1) for i in sorted(suspects))
        print(f"    Pour les revoir :  --revoir {numeros}")


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------

def trace_complete(points, troncons):
    """Points du tracé : géométrie routière si elle existe, sinon les étapes."""
    if not troncons:
        return [[p["lat"], p["lon"]] for p in points]
    suite = []
    for t in troncons:
        for pt in t["pts"]:
            if not suite or pt != suite[-1]:
                suite.append(pt)
    return suite


def export_geojson(points, chemin, troncons=None):
    fc = {"type": "FeatureCollection", "features": []}
    for i, p in enumerate(points, 1):
        fc["features"].append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
            "properties": {"ordre": i, "nom": p["nom"], "date": p["date"],
                           "note": p["note"], "adresse": p.get("adresse", "")},
        })
    fc["features"].append({
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[lon, lat]
                                     for lat, lon in trace_complete(points, troncons)]},
        "properties": {"nom": "Itinéraire",
                       "parRoute": bool(troncons and any(t["route"] for t in troncons))},
    })
    Path(chemin).write_text(json.dumps(fc, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def echap_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def export_gpx(points, chemin, titre, troncons=None):
    lignes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<gpx version="1.1" creator="carte.py" '
              'xmlns="http://www.topografix.com/GPX/1/1">',
              f"  <metadata><name>{echap_xml(titre)}</name></metadata>"]
    for i, p in enumerate(points, 1):
        desc = " — ".join(x for x in (p["date"], p["note"]) if x)
        lignes.append(f'  <wpt lat="{p["lat"]:.6f}" lon="{p["lon"]:.6f}">')
        lignes.append(f"    <name>{i}. {echap_xml(p['nom'])}</name>")
        if desc:
            lignes.append(f"    <desc>{echap_xml(desc)}</desc>")
        lignes.append("  </wpt>")
    lignes.append(f"  <trk><name>{echap_xml(titre)}</name><trkseg>")
    for lat, lon in trace_complete(points, troncons):
        lignes.append(f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
    lignes.append("  </trkseg></trk>\n</gpx>")
    Path(chemin).write_text("\n".join(lignes), encoding="utf-8")


# --------------------------------------------------------------------------
# Génération de la carte
# --------------------------------------------------------------------------

GABARIT = Path(__file__).with_name("gabarit.html")


# apparence du tracé et des marqueurs, ajustable ensuite dans la carte
STYLE_DEFAUT = {
    "taille": 28,        # diamètre des marqueurs en pixels ; 0 = masqués
    "auto": True,        # réduire les marqueurs quand on dézoome
    "epaisseur": 3,      # épaisseur du trait ; 0 = masqué
    "couleur": "#c1440e",
    "pointille": True,
    "dessus": False,     # trait par-dessus les marqueurs
    "degrade": True,     # couleur suivant l'avancement, du départ à l'arrivée
    "degradeDebut": "#275fce",
    "degradeFin": "#bc2431",
    "decalage": False,   # décaler chaque tronçon à droite (sépare aller/retour)
}

# emprises approximatives des fonds de carte nationaux
EMPRISES = {
    "fr": (41.0, 51.6, -5.5, 9.9),    # France métropolitaine
    "jp": (24.0, 46.0, 122.0, 146.5),  # Japon
}
FOND_PAR_ZONE = {"fr": "Plan IGN"}   # ailleurs : fond mondial à toponymie latine


def zones_couvertes(points):
    """Zones dont les fonds nationaux sont pertinents pour ces points."""
    zones = []
    for code, (lat_min, lat_max, lon_min, lon_max) in EMPRISES.items():
        if any(lat_min <= p["lat"] <= lat_max and lon_min <= p["lon"] <= lon_max
               for p in points):
            zones.append(code)
    zones.append("monde")
    return zones


def generer_carte(points, chemin, titre, tracer=True, fond_defaut=None, style=None,
                  troncons=None):
    if troncons is None:
        troncons = [{"pts": [[a["lat"], a["lon"]], [b["lat"], b["lon"]]],
                     "km": round(haversine((a["lat"], a["lon"]),
                                           (b["lat"], b["lon"])), 1),
                     "min": None, "route": False}
                    for a, b in zip(points, points[1:])]
    cumul = [0.0]
    for t in troncons:
        cumul.append(cumul[-1] + t["km"])

    donnees = []
    for i, p in enumerate(points):
        donnees.append({
            "kmEtape": troncons[i - 1]["km"] if i else None,
            "minEtape": troncons[i - 1]["min"] if i else None,
            "parRoute": troncons[i - 1]["route"] if i else None,
            "n": i + 1, "nom": p["nom"], "date": p["date"], "note": p["note"],
            "adresse": p.get("adresse", ""), "lat": p["lat"], "lon": p["lon"],
            "statut": p.get("statut", "ok"),
            "km": round(cumul[i], 1),
        })

    zones = zones_couvertes(points)
    if not fond_defaut:  # fond national si les points sont dans une zone couverte
        fond_defaut = next((FOND_PAR_ZONE[z] for z in zones if z in FOND_PAR_ZONE),
                           "Carte du monde (Esri)")

    config = {
        "titre": titre,
        "tracer": tracer,
        "zones": zones,
        "fondDefaut": fond_defaut,
        "style": style or STYLE_DEFAUT,
        "troncons": troncons,
        "parRoute": any(t["route"] for t in troncons),
        "minutes": sum(t["min"] for t in troncons if t["min"]) or None,
        # calque de noms latins : proposé dans le sélecteur, utile
        # par-dessus le satellite ou un fond national non latin
        "etiquettes": False,
        "totalKm": round(cumul[-1], 1),
        "points": donnees,
    }
    html_sortie = GABARIT.read_text(encoding="utf-8").replace(
        "/*__DONNEES__*/null",
        json.dumps(config, ensure_ascii=False))
    Path(chemin).write_text(html_sortie, encoding="utf-8")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Carte HTML des points de passage d'un voyage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("liste", help="fichier .txt ou .csv des lieux")
    ap.add_argument("-o", "--sortie", default="carte.html", help="carte HTML produite")
    ap.add_argument("-t", "--titre", default="Mes points de passage")
    ap.add_argument("--verifier", action="store_true",
                    help="validation interactive de chaque point (recommandé la 1re fois)")
    ap.add_argument("--pays", default=None,
                    help="restreint le géocodage (ex : fr, fr,es)")
    ap.add_argument("--cache", default="cache_geocodage.json")
    ap.add_argument("--revoir", default="",
                    help="re-vérifie seulement ces points : numéros et/ou "
                         "morceaux de nom séparés par des virgules (ex : "
                         "--revoir 9,Takayama)")
    ap.add_argument("--forcer", action="store_true",
                    help="ignore le cache et re-géocode TOUS les points")
    ap.add_argument("--route", action="store_true",
                    help="suit les routes réelles entre les étapes (OSRM public) "
                         "au lieu de tracer des lignes droites ; donne aussi le "
                         "kilométrage et le temps de conduite")
    ap.add_argument("--cache-routes", default="cache_routes.json")
    ap.add_argument("--distances", action="store_true",
                    help="détaille la distance entre chaque étape consécutive")
    ap.add_argument("--sans-trace", action="store_true",
                    help="ne pas relier les points par une ligne")

    apparence = ap.add_argument_group(
        "apparence (ajustable ensuite dans la carte, bouton ⚙)")
    apparence.add_argument("--taille", type=int, metavar="PX",
                           help="diamètre des marqueurs (défaut 28 ; "
                                "moins de 20 = pastilles sans numéro ; 0 = masqués)")
    apparence.add_argument("--marqueurs", choices=("auto", "fixe"),
                           help="auto (défaut) : les marqueurs rapetissent quand "
                                "on dézoome, ce qui dégage le tracé")
    apparence.add_argument("--epaisseur", type=int, metavar="PX",
                           help="épaisseur du trait (défaut 3 ; 0 = masqué)")
    apparence.add_argument("--couleur", metavar="COULEUR",
                           help="couleur unie du tracé et des marqueurs "
                                "(#1d4e89, darkgreen...) ; sans elle, le tracé "
                                "est dégradé du départ à l'arrivée")
    apparence.add_argument("--degrade", nargs=2, metavar=("DEPART", "ARRIVEE"),
                           help="couleurs de début et de fin du dégradé "
                                "(ex : --degrade \"#275fce\" \"#bc2431\", ou "
                                "--degrade teal orange)")
    apparence.add_argument("--separer-aller-retour", action="store_true",
                           help="décale chaque tronçon à droite du sens de "
                                "marche : sur une route empruntée deux fois, "
                                "aller et retour deviennent deux voies")
    apparence.add_argument("--trait", choices=("pointille", "plein"),
                           help="style du trait (défaut pointille)")
    apparence.add_argument("--trace-dessus", action="store_true",
                           help="dessine le trait par-dessus les marqueurs")
    ap.add_argument("--fond", default=None,
                    help="fond affiché au départ (par défaut : le fond national de la zone)")
    ap.add_argument("--gpx", help="exporte aussi un fichier GPX")
    ap.add_argument("--geojson", help="exporte aussi un fichier GeoJSON")
    args = ap.parse_args()

    points = lire_points(args.liste)
    if not points:
        sys.exit("Aucun point dans la liste.")
    print(f"{len(points)} point(s) à localiser.\n")

    revoir = choisir_points(points, args.revoir)
    cache = charger_cache(args.cache)
    points, problemes = resoudre(points, cache, pays=args.pays,
                                 interactif=args.verifier, forcer=args.forcer,
                                 revoir=revoir)
    sauver_cache(args.cache, cache)

    valides = [p for p in points if p.get("lat") is not None]
    ignores = len(points) - len(valides)
    print()
    if ignores:
        print(f"⚠ {ignores} point(s) non localisé(s), exclus de la carte.")
    if problemes and not args.verifier:
        print("⚠ Points douteux ou introuvables : relancez avec --verifier ; "
              "seuls ces points-là vous seront reposés.")
    if not problemes and not args.verifier:
        print("Pour corriger un point déjà validé : --revoir \"nom du lieu\"")
    if not valides:
        sys.exit("Aucun point localisé, pas de carte générée.")

    rapport_distances(valides, tout=args.distances)

    troncons = None
    if args.route and len(valides) > 1:
        print("\nCalcul des itinéraires routiers :")
        cache_routes = charger_cache(args.cache_routes)
        troncons, droits = calculer_troncons(valides, cache_routes, router=True)
        sauver_cache(args.cache_routes, cache_routes)
        km = sum(t["km"] for t in troncons)
        minutes = sum(t["min"] for t in troncons if t["min"])
        if droits < len(troncons):
            print(f"\nPar la route : {km:.0f} km, {duree(minutes)} de conduite.")
        else:
            print(f"\nAucun tronçon routable : {km:.0f} km à vol d'oiseau.")
        if droits:
            print(f"⚠ {droits} tronçon(s) sans route : tracés en ligne droite "
                  "(île sans bac, point hors du réseau routier...).")
    print()

    style = dict(STYLE_DEFAUT)
    if args.taille is not None:
        style["taille"] = max(0, args.taille)
    if args.marqueurs:
        style["auto"] = args.marqueurs == "auto"
    if args.epaisseur is not None:
        style["epaisseur"] = max(0, args.epaisseur)
    if args.couleur:                      # couleur explicite = tracé uni
        style["couleur"] = args.couleur
        style["degrade"] = False
    if args.degrade:
        style["degradeDebut"], style["degradeFin"] = args.degrade
        style["degrade"] = True
    if args.separer_aller_retour:
        style["decalage"] = True
    if args.trait:
        style["pointille"] = args.trait == "pointille"
    if args.trace_dessus:
        style["dessus"] = True

    generer_carte(valides, args.sortie, args.titre,
                  tracer=not args.sans_trace, fond_defaut=args.fond, style=style,
                  troncons=troncons)
    print(f"→ Carte : {Path(args.sortie).resolve()}")

    if args.gpx:
        export_gpx(valides, args.gpx, args.titre, troncons)
        print(f"→ GPX   : {Path(args.gpx).resolve()}")
    if args.geojson:
        export_geojson(valides, args.geojson, troncons)
        print(f"→ GeoJSON: {Path(args.geojson).resolve()}")


if __name__ == "__main__":
    main()
