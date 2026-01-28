import os
import requests
import polyline
import json
import pickle  # <--- AJOUT IMPORTANT
from flask import Flask, redirect, request, jsonify, session, render_template, url_for
from dotenv import load_dotenv
from datetime import datetime
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from concurrent.futures import ThreadPoolExecutor, as_completed

# 1. Configuration initiale
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')

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

# --- SYSTÈME DE CACHE À 3 NIVEAUX ---
# 1. Cache Disque (Villes) -> OPTIMISÉ EN PICKLE
CACHE_FILE = "cache_villes_opti.pkl"  # <--- FICHIER RAPIDE
MUNI_CACHE = {}

# 2. Cache Données Brutes (Activités Strava par Token)
RAW_DATA_CACHE = {}

# 3. Cache Résultats Calculés (JSON prêt à l'emploi par Token + Filtres)
API_RESULT_CACHE = {}

# CHARGEMENT ULTRA RAPIDE (PICKLE)
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'rb') as f:  # Mode lecture binaire 'rb'
            MUNI_CACHE = pickle.load(f)
        print(f"✅ Cache chargé : {len(MUNI_CACHE)} villes en mémoire.")
    except Exception as e:
        print(f"⚠️ Erreur chargement cache: {e}")
        MUNI_CACHE = {}
else:
    print("⚠️ Aucun fichier cache trouvé. Le démarrage sera lent si on doit tout télécharger.")

# Sauvegarde (optionnel, attention sur Render le disque n'est pas persistant)
def save_muni_cache():
    try:
        with open(CACHE_FILE, 'wb') as f:  # Mode écriture binaire 'wb'
            pickle.dump(MUNI_CACHE, f)
    except: pass

# --- FONCTIONS UTILITAIRES ---

def fetch_city_data(lat, lon):
    key = f"{round(lat, 3)}_{round(lon, 3)}"
    if key in MUNI_CACHE: return (key, MUNI_CACHE[key])

    url = f"https://geo.api.gouv.fr/communes?lat={lat}&lon={lon}&fields=nom,contour,surface&format=json&geometry=contour"
    try:
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            results = response.json()
            if results:
                city = results[0]
                data = {
                    "name": city.get('nom', 'Inconnue'),
                    "area_m2": city.get('surface', 0) * 10000,
                    "outline": [[p[1], p[0]] for p in city['contour']['coordinates'][0]] if 'contour' in city else []
                }
                return (key, data)
    except: pass
    return None

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

# --- ROUTES ---

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
        RAW_DATA_CACHE.pop(token, None) # Vide données brutes
        API_RESULT_CACHE.pop(token, None) # Vide résultats calculés
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

# --- API ---

@app.route('/api/stats_history')
def get_stats_history():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    # Paramètres de filtre
    grid_meters = int(request.args.get('grid_size', 100))
    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')

    # --- VÉRIFICATION DU CACHE DE RÉSULTAT ---
    cache_key = f"stats_{grid_meters}_{sel_year}_{sel_sport}"
    
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    
    if cache_key in API_RESULT_CACHE[token]:
        print(f"🚀 Cache hit: {cache_key}")
        return jsonify(API_RESULT_CACHE[token][cache_key])

    # --- CALCUL SI PAS EN CACHE ---
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

    # SAUVEGARDE EN CACHE
    API_RESULT_CACHE[token][cache_key] = result
    return jsonify(result)

@app.route('/api/activities')
def get_activities_route():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')
    grid_meters = int(request.args.get('grid_size', 100))
    
    # --- VÉRIFICATION DU CACHE DE RÉSULTAT ---
    cache_key = f"act_{grid_meters}_{sel_year}_{sel_sport}"
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    
    if cache_key in API_RESULT_CACHE[token]:
        print(f"🚀 Cache hit: {cache_key}")
        return jsonify(API_RESULT_CACHE[token][cache_key])

    # --- CALCUL ---
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

    # --- VILLES (AVEC CACHE OPTIMISÉ) ---
    if grid_store:
        sorted_locs = sorted(grid_store.items(), key=lambda x: x[1]['cnt'], reverse=True)
        scan_points = [k for k, v in sorted_locs[:600]]

        identified_cities = {}
        with ThreadPoolExecutor(max_workers=10) as exe:
            futures = {exe.submit(fetch_city_data, lat, lon): (lat, lon) for lat, lon in scan_points}
            for future in as_completed(futures):
                res = future.result()
                if res:
                    key, muni = res
                    MUNI_CACHE[key] = muni 
                    
                    if muni['name'] not in identified_cities and len(identified_cities) < 50:
                        try:
                            # PREP = x50 VITESSE
                            prepared_poly = prep(Polygon(muni['outline']))
                            count_inside = 0
                            muni_center = muni['outline'][0][0]
                            
                            for (clat, clon) in grid_store.keys():
                                if abs(clat - muni_center) < 0.15: 
                                    if prepared_poly.contains(Point(clat, clon)):
                                        count_inside += 1
                            
                            if count_inside > 0:
                                pct = (count_inside * (grid_meters**2) / muni['area_m2']) * 100
                                identified_cities[muni['name']] = {
                                    "name": muni['name'], "outline": muni['outline'],
                                    "stats": {"blocks": count_inside, "percent": round(min(pct, 100), 2)}
                                }
                        except: pass
        
        save_muni_cache()
        data["top_municipalities"] = sorted(list(identified_cities.values()), key=lambda x: x['stats']['blocks'], reverse=True)

    # SAUVEGARDE EN CACHE
    API_RESULT_CACHE[token][cache_key] = data
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True, port=5000)