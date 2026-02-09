import os
import requests
import polyline
import json
from flask import Flask, redirect, request, jsonify, session, render_template, url_for
from dotenv import load_dotenv
from datetime import datetime
from shapely.geometry import Point, Polygon, shape
from shapely.prepared import prep
from sqlalchemy import create_engine, text

# 1. Configuration initiale
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')

# --- CONFIGURATION DATABASE (SUPABASE) ---
# Sur Vercel, cette variable doit être définie dans les Settings
DB_URL = "postgresql://postgres:R61KlIcfrFKqADHK@db.jsfouzzuekmdyegslhmf.supabase.co:5432/postgres"

# Configuration Strava
CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
REDIRECT_URI = os.getenv('STRAVA_REDIRECT_URI', 'http://localhost:5000/callback')

SPORT_TRANSLATIONS = {
    'Run': 'Course à pied', 'Ride': 'Vélo', 'Hike': 'Randonnée', 'Walk': 'Marche',
    'AlpineSki': 'Ski Alpin', 'BackcountrySki': 'Ski de Rando', 'VirtualRide': 'Vélo Virtuel',
    'VirtualRun': 'Course Virtuelle', 'GravelRide': 'Gravel', 'TrailRun': 'Trail',
    'E-BikeRide': 'Vélo Électrique', 'Velomobile': 'Vélomobile', 'NordicSki': 'Ski de Fond',
    'Snowshoe': 'Raquettes'
}
GPS_SPORTS = list(SPORT_TRANSLATIONS.keys())

# --- CACHES EN MÉMOIRE (RAM uniquement) ---
RAW_DATA_CACHE = {}
API_RESULT_CACHE = {}

# --- FONCTIONS UTILITAIRES ---

def get_cells_from_polyline(pts, grid_size_deg):
    cells = set()
    if not pts: return cells
    prev_lat, prev_lon = pts[0]
    
    def to_key(lat, lon):
        return (round(round(lat/grid_size_deg)*grid_size_deg, 6), 
                round(round(lon/grid_size_deg)*grid_size_deg, 6))

    cells.add(to_key(prev_lat, prev_lon))

    for i in range(1, len(pts)):
        curr_lat, curr_lon = pts[i]
        dist = ((curr_lat - prev_lat)**2 + (curr_lon - prev_lon)**2)**0.5
        if dist > grid_size_deg * 0.7:
            num_steps = int(dist / (grid_size_deg * 0.5))
            for j in range(1, num_steps + 1):
                frac = j / (num_steps + 1)
                cells.add(to_key(prev_lat + (curr_lat - prev_lat) * frac, prev_lon + (curr_lon - prev_lon) * frac))
        cells.add(to_key(curr_lat, curr_lon))
        prev_lat, prev_lon = curr_lat, curr_lon
    return cells

def get_strava_activities_cached(token):
    """Charge les activités une seule fois."""
    if token in RAW_DATA_CACHE: return RAW_DATA_CACHE[token]
    
    all_activities = []
    headers = {'Authorization': f'Bearer {token}'}
    page = 1
    
    while True:
        try:
            r = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers, params={'per_page': 200, 'page': page}, timeout=10)
            if r.status_code != 200: break
            data = r.json()
            if not data: break
            all_activities.extend(data)
            page += 1
            if page > 10: break
        except: break
    
    cleaned_data = []
    for act in all_activities:
        if act.get('type') in GPS_SPORTS and act.get('map', {}).get('summary_polyline'):
            cleaned_data.append({
                'type': act['type'],
                'start_date_local': act['start_date_local'],
                'polyline': act['map']['summary_polyline'],
                'distance': act.get('distance', 0)
            })

    RAW_DATA_CACHE[token] = cleaned_data
    return cleaned_data

# --- ROUTES STANDARD ---

@app.route('/')
def index():
    if 'access_token' not in session: return redirect(url_for('login_page'))
    return render_template('index.html')

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/auth')
def auth():
    return redirect(f"https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={REDIRECT_URI}&approval_prompt=auto&scope=activity:read_all")

@app.route('/logout')
def logout():
    token = session.get('access_token')
    if token:
        RAW_DATA_CACHE.pop(token, None)
        API_RESULT_CACHE.pop(token, None)
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/callback')
def callback():
    code = request.args.get('code')
    res = requests.post("https://www.strava.com/oauth/token", data={'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'code': code, 'grant_type': 'authorization_code'})
    if res.status_code == 200:
        session['access_token'] = res.json().get('access_token')
        return redirect('/')
    return "Erreur auth"

@app.route('/stats')
def stats_page(): return render_template('stats.html') if 'access_token' in session else redirect(url_for('login_page'))

@app.route('/story')
def story_page(): return render_template('story.html') if 'access_token' in session else redirect(url_for('login_page'))

@app.route('/timelapse')
def timelapse_page(): return render_template('timelapse.html') if 'access_token' in session else redirect(url_for('login_page'))

# --- API ROUTES ---

@app.route('/api/stats_history')
def get_stats_history():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    grid_meters = int(request.args.get('grid_size', 100))
    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')

    cache_key = f"stats_{grid_meters}_{sel_year}_{sel_sport}"
    
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    if cache_key in API_RESULT_CACHE[token]:
        return jsonify(API_RESULT_CACHE[token][cache_key])

    activities = get_strava_activities_cached(token)
    grid_size_deg = grid_meters / 111320
    activities.sort(key=lambda x: x['start_date_local'])

    monthly_data = {}
    global_seen = set()
    available_years = set()
    available_sports = set()
    total_blocks = 0

    for act in activities:
        dt = datetime.strptime(act['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        y_str = str(dt.year)
        m_key = dt.strftime("%Y-%m")
        sport = act['type']

        available_years.add(y_str)
        available_sports.add(sport)

        if sel_year != 'all' and y_str != sel_year: continue
        if sel_sport != 'all' and sport != sel_sport: continue

        if m_key not in monthly_data: monthly_data[m_key] = {'new': 0, 'routine': 0}

        pts = polyline.decode(act['polyline'])
        blocks = get_cells_from_polyline(pts, grid_size_deg)

        for b in blocks:
            if b not in global_seen:
                global_seen.add(b)
                monthly_data[m_key]['new'] += 1
                total_blocks += 1
            else:
                monthly_data[m_key]['routine'] += 1

    labels = sorted(monthly_data.keys())
    conquest, explore, routine = [], [], []
    running = 0
    for m in labels:
        running += monthly_data[m]['new']
        conquest.append(running)
        explore.append(monthly_data[m]['new'])
        routine.append(monthly_data[m]['routine'])

    result = {
        "labels": labels, "conquest": conquest, "exploration": explore, "routine": routine,
        "total_blocks": total_blocks,
        "available_years": sorted(list(available_years), reverse=True),
        "available_sports": sorted(list(available_sports))
    }

    API_RESULT_CACHE[token][cache_key] = result
    return jsonify(result)

@app.route('/api/activities')
def get_activities_route():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')
    grid_meters = int(request.args.get('grid_size', 100))
    
    cache_key = f"act_{grid_meters}_{sel_year}_{sel_sport}"
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    
    if cache_key in API_RESULT_CACHE[token]:
        return jsonify(API_RESULT_CACHE[token][cache_key])

    activities = get_strava_activities_cached(token)
    grid_size_deg = grid_meters / 111320

    data = {
        "coords": [], "grid_cells": [], "grid_size_used": grid_size_deg,
        "available_years": set(), "available_sports": {}, "top_municipalities": [],
        "stats": { "total_distance": 0, "activity_count": 0, "cells_conquered": 0 }
    }
    
    grid_store = {}

    for act in activities:
        dt = datetime.strptime(act['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        y_str = str(dt.year)
        sport = act['type']
        
        data["available_years"].add(y_str)
        if sport not in data["available_sports"]:
            data["available_sports"][sport] = SPORT_TRANSLATIONS.get(sport, sport)

        if (sel_year == 'all' or sel_year == y_str) and (sel_sport == 'all' or sel_sport == sport):
            pts = polyline.decode(act['polyline'])
            data["coords"].append(pts)
            
            blocks = get_cells_from_polyline(pts, grid_size_deg)
            act_ym = dt.strftime("%Y-%m")

            for b in blocks:
                if b not in grid_store:
                    grid_store[b] = {'cnt': 0, 'first': act_ym, 'last': act_ym}
                
                grid_store[b]['cnt'] += 1
                if act_ym < grid_store[b]['first']: grid_store[b]['first'] = act_ym
                if act_ym > grid_store[b]['last']: grid_store[b]['last'] = act_ym
            
            data["stats"]["total_distance"] += act['distance'] / 1000
            data["stats"]["activity_count"] += 1

    data["grid_cells"] = [[k[0], k[1], v['cnt'], v['first'], v['last']] for k, v in grid_store.items()]
    data["stats"]["cells_conquered"] = len(grid_store)
    data["available_years"] = sorted(list(data["available_years"]), reverse=True)
    data["available_sports"] = dict(sorted(data["available_sports"].items(), key=lambda x: x[1]))

    # --- CALCUL DES VILLES OPTIMISÉ (BATCH REQUEST) ---
    # Nous utilisons une approche par lots (batch) pour interroger la base de données.
    # Au lieu de vérifier chaque bloc un par un (trop lent), on envoie des paquets de coordonnées.
    
    if grid_store and DB_URL:
        identified_cities = {}
        
        # 1. Création de "Sondes" (Probes)
        # On ne garde qu'un point unique tous les ~1km (arrondi 2 décimales)
        # Cela réduit drastiquement le nombre de points à vérifier (ex: 10 000 blocs -> 150 sondes)
        probe_points = set()
        for lat, lon in grid_store.keys():
            probe_points.add((round(lat, 2), round(lon, 2)))
        
        probe_list = list(probe_points)
        
        # 2. Interrogation par paquets (Batch)
        batch_size = 50 # On envoie 50 points d'un coup
        
        try:
            engine = create_engine(DB_URL)
            with engine.connect() as conn:
                for i in range(0, len(probe_list), batch_size):
                    batch = probe_list[i:i+batch_size]
                    
                    # Construction d'un MultiPoint WKT (Well Known Text) pour PostGIS
                    # IMPORTANT: WKT est en format "LON LAT"
                    points_str = ", ".join([f"{lon} {lat}" for lat, lon in batch])
                    wkt_multipoint = f"MULTIPOINT({points_str})"
                    
                    # Cette requête trouve TOUTES les communes qui intersectent nos points
                    query = text("""
                        SELECT DISTINCT nom_commune, 
                               ST_Area(geometry::geography) as area_m2, 
                               ST_AsGeoJSON(geometry) as outline
                        FROM communes
                        WHERE ST_Intersects(geometry, ST_GeomFromText(:wkt, 4326))
                    """)
                    
                    result_proxy = conn.execute(query, {"wkt": wkt_multipoint})
                    
                    # Traitement des résultats bruts
                    for row in result_proxy:
                        if row.nom_commune not in identified_cities:
                            
                            # Parsing GeoJSON
                            geojson_geom = json.loads(row.outline)
                            inverted_outline = []
                            # Inversion Lat/Lon pour Leaflet
                            if geojson_geom['type'] == 'Polygon':
                                inverted_outline = [[p[1], p[0]] for p in geojson_geom['coordinates'][0]]
                            elif geojson_geom['type'] == 'MultiPolygon':
                                inverted_outline = [[p[1], p[0]] for p in geojson_geom['coordinates'][0][0]]
                            
                            identified_cities[row.nom_commune] = {
                                "name": row.nom_commune,
                                "area_m2": row.area_m2,
                                "outline": inverted_outline,
                                "poly_obj": Polygon(inverted_outline) # Objet Shapely pour calculs précis
                            }
                    
                    # Sécurité : Si on a déjà plus de 70 villes, on arrête le scan DB pour ne pas surcharger
                    if len(identified_cities) >= 70: break

        except Exception as e:
            print(f"⚠️ Erreur Batch DB: {e}")

        # 3. Calcul précis des statistiques en Python (Rapide car en RAM)
        final_cities_list = []
        
        for city_name, city_data in identified_cities.items():
            try:
                # Préparation géométrique accélérée
                poly_geom = city_data['poly_obj']
                prepared_poly = prep(poly_geom)
                min_lat, min_lon, max_lat, max_lon = poly_geom.bounds
                
                count_inside = 0
                
                # On vérifie quels blocs sont réellement DANS cette ville
                for (clat, clon) in grid_store.keys():
                    # Check rapide (Bounding Box)
                    if min_lat <= clat <= max_lat and min_lon <= clon <= max_lon:
                        # Check précis (Point in Polygon)
                        if prepared_poly.contains(Point(clat, clon)):
                            count_inside += 1
                
                if count_inside > 0:
                    area_conquered_m2 = count_inside * (grid_meters**2)
                    pct = (area_conquered_m2 / city_data['area_m2']) * 100
                    
                    final_cities_list.append({
                        "name": city_name,
                        "outline": city_data['outline'],
                        "stats": {
                            "blocks": count_inside,
                            "percent": round(min(pct, 100), 2)
                        }
                    })
            except Exception as e:
                print(f"Erreur calcul stats ville {city_name}: {e}")

        # On trie pour avoir les villes les plus explorées en premier
        data["top_municipalities"] = sorted(final_cities_list, key=lambda x: x['stats']['blocks'], reverse=True)

    API_RESULT_CACHE[token][cache_key] = data
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=5000)