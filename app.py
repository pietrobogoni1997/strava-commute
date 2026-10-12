import os
import json
import math
import statistics
from datetime import datetime
from functools import wraps
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import (Flask, render_template, redirect, request,
                   session, jsonify, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# Necessario su Railway (e qualsiasi hosting con reverse proxy):
# fa sì che Flask veda l'URL reale con https:// invece di http://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

STRAVA_CLIENT_ID     = os.environ.get('STRAVA_CLIENT_ID', '')
STRAVA_CLIENT_SECRET = os.environ.get('STRAVA_CLIENT_SECRET', '')
STRAVA_AUTH_URL  = 'https://www.strava.com/oauth/authorize'
STRAVA_TOKEN_URL = 'https://www.strava.com/oauth/token'
STRAVA_API      = 'https://www.strava.com/api/v3'
CACHE_FILE = os.path.join(os.path.dirname(__file__), 'activities_cache.json')


# ─── Cache helpers ────────────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_cache(data):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f)


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'access_token' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def refresh_if_needed():
    exp = session.get('expires_at', 0)
    if exp and exp < datetime.now().timestamp() + 60:
        r = requests.post(STRAVA_TOKEN_URL, data={
            'client_id':     STRAVA_CLIENT_ID,
            'client_secret': STRAVA_CLIENT_SECRET,
            'grant_type':    'refresh_token',
            'refresh_token': session['refresh_token'],
        })
        d = r.json()
        session['access_token']  = d['access_token']
        session['refresh_token'] = d['refresh_token']
        session['expires_at']    = d['expires_at']

def strava(endpoint, params=None):
    refresh_if_needed()
    h = {'Authorization': f'Bearer {session["access_token"]}'}
    r = requests.get(f'{STRAVA_API}/{endpoint}', headers=h, params=params)
    r.raise_for_status()
    return r.json()


# ─── Geometry helpers ─────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distanza in metri tra due punti GPS."""
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'access_token' in session:
        return redirect(url_for('dashboard'))
    configured = bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET)
    return render_template('index.html', configured=configured)

@app.route('/login')
def login():
    cb = url_for('callback', _external=True)
    params = {
        'client_id':       STRAVA_CLIENT_ID,
        'response_type':   'code',
        'redirect_uri':    cb,
        'approval_prompt': 'auto',
        'scope':           'activity:read_all',
    }
    return redirect(f'{STRAVA_AUTH_URL}?{urlencode(params)}')

@app.route('/callback')
def callback():
    code  = request.args.get('code')
    error = request.args.get('error')
    if error or not code:
        return render_template('index.html', error='Autorizzazione negata.', configured=True)

    r = requests.post(STRAVA_TOKEN_URL, data={
        'client_id':     STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code':          code,
        'grant_type':    'authorization_code',
    })
    d = r.json()
    if 'access_token' not in d:
        return render_template('index.html', error='Errore token Strava.', configured=True)

    session['access_token']  = d['access_token']
    session['refresh_token'] = d['refresh_token']
    session['expires_at']    = d['expires_at']
    session['athlete']       = d.get('athlete', {})
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', athlete=session.get('athlete', {}))


# ─── API: lista attività ───────────────────────────────────────────────────────

@app.route('/api/activities')
@login_required
def api_activities():
    cache         = load_cache()
    athlete_id    = str(session['athlete'].get('id', 'unknown'))
    cache_key     = f'activities_{athlete_id}'
    force_refresh = request.args.get('refresh') == 'true'

    if cache_key not in cache or force_refresh:
        activities, page = [], 1
        while True:
            batch = strava('athlete/activities', {'per_page': 200, 'page': page})
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < 200:
                break
            page += 1
        cache[cache_key] = {'activities': activities, 'fetched_at': datetime.now().isoformat()}
        save_cache(cache)

    activities = cache[cache_key]['activities']

    # Filtri
    if request.args.get('commute') == 'true':
        activities = [a for a in activities if a.get('commute')]
    if typ := request.args.get('type'):
        activities = [a for a in activities if a.get('type') == typ]

    # Risposta leggera (senza polyline completi)
    out = []
    for a in activities:
        out.append({
            'id':           a['id'],
            'name':         a['name'],
            'type':         a['type'],
            'commute':      a.get('commute', False),
            'start_date':   a['start_date'],
            'elapsed_time': a['elapsed_time'],
            'moving_time':  a['moving_time'],
            'distance':     a['distance'],
            'avg_speed':    round(a.get('average_speed', 0) * 3.6, 1),
            'max_speed':    round(a.get('max_speed', 0) * 3.6, 1),
            'start_latlng': a.get('start_latlng', []),
            'end_latlng':   a.get('end_latlng', []),
            'polyline':     a.get('map', {}).get('summary_polyline', ''),
        })
    return jsonify(out)


# ─── API: stream GPS di un'attività ───────────────────────────────────────────

@app.route('/api/activity/<int:aid>/stream')
@login_required
def api_stream(aid):
    cache = load_cache()
    key   = f'stream_{aid}'

    if key not in cache:
        try:
            data = strava(
                f'activities/{aid}/streams',
                {'keys': 'latlng,time,velocity_smooth,altitude', 'key_by_type': 'true'}
            )
            cache[key] = data
            save_cache(cache)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    return jsonify(cache[key])


# ─── API: tempo tra due punti ─────────────────────────────────────────────────

@app.route('/api/analyze/time_between_points', methods=['POST'])
@login_required
def api_time_between_points():
    body         = request.json
    point_a      = body.get('point_a')       # [lat, lon]
    point_b      = body.get('point_b')       # [lat, lon]
    radius       = body.get('radius', 50)    # metri
    activity_ids = body.get('activity_ids', [])

    cache   = load_cache()
    results = []

    for aid in activity_ids:
        key = f'stream_{aid}'
        if key not in cache:
            try:
                data = strava(
                    f'activities/{aid}/streams',
                    {'keys': 'latlng,time', 'key_by_type': 'true'}
                )
                cache[key] = data
            except:
                continue

        s = cache[key]
        if not s or 'latlng' not in s or 'time' not in s:
            continue

        coords = s['latlng']['data']
        times  = s['time']['data']

        time_a = time_b = None
        for pos, t in zip(coords, times):
            if time_a is None and haversine(pos[0], pos[1], point_a[0], point_a[1]) < radius:
                time_a = t
            if time_a is not None and time_b is None and \
               haversine(pos[0], pos[1], point_b[0], point_b[1]) < radius:
                time_b = t
                break

        if time_a is not None and time_b is not None and time_b > time_a:
            secs = time_b - time_a
            results.append({'activity_id': aid, 'seconds': secs,
                            'minutes': round(secs / 60, 1)})

    save_cache(cache)

    if not results:
        return jsonify({'error': 'Nessuna attività attraversa entrambi i punti.', 'results': []})

    secs_list = [r['seconds'] for r in results]
    return jsonify({
        'results': sorted(results, key=lambda x: x['seconds']),
        'stats': {
            'count':       len(secs_list),
            'avg_min':     round(statistics.mean(secs_list) / 60, 1),
            'median_min':  round(statistics.median(secs_list) / 60, 1),
            'min_min':     round(min(secs_list) / 60, 1),
            'max_min':     round(max(secs_list) / 60, 1),
            'stdev_sec':   round(statistics.stdev(secs_list), 0) if len(secs_list) > 1 else 0,
        }
    })


# ─── API: analisi soste e velocità ────────────────────────────────────────────

@app.route('/api/analyze/stops', methods=['POST'])
@login_required
def api_stops():
    body      = request.json
    aid       = body.get('activity_id')
    threshold = body.get('speed_threshold', 0.5)   # m/s ≈ 1.8 km/h
    min_dur   = body.get('min_duration', 5)         # secondi minimi per considerarla una sosta

    cache = load_cache()
    key   = f'stream_{aid}'
    if key not in cache:
        try:
            data = strava(
                f'activities/{aid}/streams',
                {'keys': 'latlng,time,velocity_smooth', 'key_by_type': 'true'}
            )
            cache[key] = data
            save_cache(cache)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    s      = cache[key]
    coords = s['latlng']['data']
    times  = s['time']['data']
    vels   = s.get('velocity_smooth', {}).get('data', [0] * len(times))

    stops      = []
    in_stop    = False
    stop_start = stop_t0 = stop_lat = stop_lon = None

    for pos, t, v in zip(coords, times, vels):
        if v < threshold:
            if not in_stop:
                in_stop = True
                stop_start = t
                stop_lat, stop_lon = pos
        else:
            if in_stop:
                dur = t - stop_start
                if dur >= min_dur:
                    stops.append({
                        'lat':      stop_lat,
                        'lon':      stop_lon,
                        'duration': int(dur),
                    })
                in_stop = False

    # Speed profile per heatmap
    speed_profile = [
        {'lat': c[0], 'lon': c[1], 'speed': round(v * 3.6, 1)}
        for c, v in zip(coords, vels)
    ]

    return jsonify({
        'stops':          stops,
        'total_stops':    len(stops),
        'total_stop_time': sum(s['duration'] for s in stops),
        'speed_profile':  speed_profile,
    })


# ─── API: statistiche aggregate commute ───────────────────────────────────────

@app.route('/api/analyze/commute_stats')
@login_required
def api_commute_stats():
    cache      = load_cache()
    athlete_id = str(session['athlete'].get('id', 'unknown'))
    key        = f'activities_{athlete_id}'

    if key not in cache:
        return jsonify({'error': 'Attività non ancora caricate.'}), 400

    activities = [a for a in cache[key]['activities'] if a.get('commute')]
    if not activities:
        return jsonify({'error': 'Nessuna attività commute trovata.'}), 404

    by_type = {}
    for a in activities:
        t = a.get('type', 'Unknown')
        by_type.setdefault(t, []).append(a)

    summary = {}
    for t, acts in by_type.items():
        times  = [a['moving_time'] for a in acts]
        dists  = [a['distance'] for a in acts]
        speeds = [a.get('average_speed', 0) * 3.6 for a in acts]
        summary[t] = {
            'count':        len(acts),
            'avg_time_min': round(statistics.mean(times) / 60, 1),
            'avg_dist_km':  round(statistics.mean(dists) / 1000, 2),
            'avg_speed_kmh': round(statistics.mean(speeds), 1),
            'total_dist_km': round(sum(dists) / 1000, 1),
            'total_time_h':  round(sum(times) / 3600, 1),
        }

    # Timeline mensile
    monthly = {}
    for a in activities:
        mo = a['start_date'][:7]
        monthly.setdefault(mo, 0)
        monthly[mo] += 1

    return jsonify({'by_type': summary, 'monthly': dict(sorted(monthly.items()))})


# ─── API: pre-caricamento stream GPS ─────────────────────────────────────────
#
# Scarica TUTTI gli stream GPS delle attività commute in un colpo solo
# e li salva nella cache locale. Dopo, l'analisi è istantanea.
# Rispetta il rate limit Strava: 95 req/15min (lascia margine per le altre call).

import threading
import time as time_mod

_precache_state = {'running': False, 'done': 0, 'total': 0, 'errors': 0, 'log': []}

def _precache_worker(activity_ids, token, cache):
    global _precache_state
    _precache_state.update({'running': True, 'done': 0, 'total': len(activity_ids),
                            'errors': 0, 'log': []})
    headers = {'Authorization': f'Bearer {token}'}
    batch_count = 0

    for aid in activity_ids:
        key = f'stream_{aid}'
        if key in cache:
            _precache_state['done'] += 1
            continue  # già in cache, salta

        # Rispetta rate limit: pausa ogni 90 richieste
        if batch_count > 0 and batch_count % 90 == 0:
            _precache_state['log'].append('⏳ Pausa 15 min per rate limit Strava…')
            time_mod.sleep(901)

        try:
            r = requests.get(
                f'{STRAVA_API}/activities/{aid}/streams',
                headers=headers,
                params={'keys': 'latlng,time,velocity_smooth,altitude', 'key_by_type': 'true'},
                timeout=15
            )
            if r.status_code == 429:  # rate limited
                _precache_state['log'].append('⏳ Rate limit raggiunto, attesa 15 min…')
                time_mod.sleep(901)
                r = requests.get(f'{STRAVA_API}/activities/{aid}/streams',
                                 headers=headers,
                                 params={'keys': 'latlng,time,velocity_smooth,altitude',
                                         'key_by_type': 'true'}, timeout=15)
            if r.ok:
                cache[key] = r.json()
                batch_count += 1
            else:
                _precache_state['errors'] += 1
        except Exception as e:
            _precache_state['errors'] += 1
            _precache_state['log'].append(f'Errore {aid}: {e}')

        _precache_state['done'] += 1

    save_cache(cache)
    _precache_state['running'] = False
    _precache_state['log'].append('✅ Pre-caricamento completato!')


@app.route('/api/precache/start', methods=['POST'])
@login_required
def api_precache_start():
    global _precache_state
    if _precache_state['running']:
        return jsonify({'error': 'Pre-caricamento già in corso.'}), 409

    cache      = load_cache()
    athlete_id = str(session['athlete'].get('id', 'unknown'))
    key        = f'activities_{athlete_id}'

    if key not in cache:
        return jsonify({'error': 'Carica prima la lista attività.'}), 400

    commute_only = request.json.get('commute_only', True)
    acts = cache[key]['activities']
    if commute_only:
        acts = [a for a in acts if a.get('commute')]

    ids    = [a['id'] for a in acts]
    cached = sum(1 for i in ids if f'stream_{i}' in cache)

    token = session['access_token']
    t = threading.Thread(target=_precache_worker, args=(ids, token, cache), daemon=True)
    t.start()

    return jsonify({'started': True, 'total': len(ids), 'already_cached': cached})


@app.route('/api/precache/status')
@login_required
def api_precache_status():
    return jsonify(_precache_state)


if __name__ == '__main__':
    print("=" * 60)
    print("  Strava Commute Analyzer")
    print("  Apri: http://localhost:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)
