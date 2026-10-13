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

# Supabase (opzionale — se configurato, la cache persiste tra i riavvii)
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')


# ─── Cache layer (lazy) ───────────────────────────────────────────────────────
# Architettura:
#   1. All'avvio carica solo le CHIAVI (indice leggero, niente payload)
#   2. I valori vengono caricati on-demand da Supabase, uno per uno o in batch
#   3. In-memory layer per ogni worker Gunicorn (evita richieste ripetute)
#
# Questo risolve:
#   - Limite 1000 righe del REST API Supabase (usiamo select=key con limit=200000)
#   - Timeout per caricamento di 20MB+ di JSONB all'avvio
#   - Attività sempre recuperate da Strava (chiave non trovata nei primi 1000)

_mem_cache:   dict = {}    # key → valore (caricato on-demand)
_sb_known:    set  = set() # chiavi note in Supabase (solo indice, senza valori)
_cache_ready: bool = False

def _sb_headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
    }

def _ensure_index():
    """Carica l'indice delle chiavi da Supabase con paginazione (una volta per worker).
    Usa Range header per superare il limite max-rows di PostgREST (default 1000).
    """
    global _sb_known, _cache_ready
    if _cache_ready:
        return
    _cache_ready = True
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            PAGE = 1000
            offset = 0
            while True:
                r = requests.get(
                    f'{SUPABASE_URL}/rest/v1/cache_store?select=key',
                    headers={**_sb_headers(),
                             'Range-Unit': 'items',
                             'Range': f'{offset}-{offset + PAGE - 1}'},
                    timeout=20
                )
                if r.ok:
                    rows = r.json()
                    _sb_known.update(row['key'] for row in rows)
                    if len(rows) < PAGE:
                        break          # ultima pagina
                    offset += PAGE
                else:
                    print(f'[cache] Index page error: {r.status_code} {r.text[:200]}')
                    break
            print(f'[cache] Index caricato: {len(_sb_known)} chiavi in Supabase')
        except Exception as e:
            print(f'[cache] _ensure_index error: {e}')
    else:
        # Fallback file locale
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE) as f:
                    _mem_cache.update(json.load(f))
                _sb_known.update(_mem_cache.keys())
            except Exception:
                pass

def cache_has(key: str) -> bool:
    """True se la chiave esiste (in memoria o in Supabase)."""
    _ensure_index()
    return key in _mem_cache or key in _sb_known

def cache_get(key: str):
    """Restituisce il valore, caricandolo da Supabase se non è in memoria."""
    _ensure_index()
    if key in _mem_cache:
        return _mem_cache[key]
    if key not in _sb_known:
        return None
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                f'{SUPABASE_URL}/rest/v1/cache_store?key=eq.{key}&select=value&limit=1',
                headers=_sb_headers(), timeout=15
            )
            if r.ok:
                rows = r.json()
                if rows:
                    val = rows[0]['value']
                    _mem_cache[key] = val
                    return val
        except Exception as e:
            print(f'[cache] get({key}) error: {e}')
    return None

def cache_get_many(keys: list) -> dict:
    """Carica più chiavi in batch da Supabase (riduce le richieste HTTP)."""
    _ensure_index()
    result = {}
    missing = []
    for k in keys:
        if k in _mem_cache:
            result[k] = _mem_cache[k]
        elif k in _sb_known:
            missing.append(k)

    if missing and SUPABASE_URL and SUPABASE_KEY:
        for i in range(0, len(missing), 50):
            batch = missing[i:i + 50]
            keys_param = ','.join(batch)
            try:
                r = requests.get(
                    f'{SUPABASE_URL}/rest/v1/cache_store?key=in.({keys_param})&select=key,value&limit=100',
                    headers=_sb_headers(), timeout=30
                )
                if r.ok:
                    for row in r.json():
                        _mem_cache[row['key']] = row['value']
                        result[row['key']] = row['value']
            except Exception as e:
                print(f'[cache] get_many batch error: {e}')
    return result

def cache_set(key: str, value) -> bool:
    """Salva in memoria + Supabase (upsert) o file locale."""
    global _sb_known
    _ensure_index()
    _mem_cache[key] = value
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.post(
                f'{SUPABASE_URL}/rest/v1/cache_store',
                headers={**_sb_headers(),
                         'Prefer': 'resolution=merge-duplicates,return=minimal'},
                json={'key': key, 'value': value},
                timeout=30
            )
            if r.ok:
                _sb_known.add(key)
                return True
            else:
                print(f'[cache] set({key}) HTTP {r.status_code}: {r.text[:200]}')
                return False
        except Exception as e:
            print(f'[cache] set({key}) error: {e}')
            return False
    else:
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(_mem_cache, f)
        except Exception:
            pass
        return True


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
    athlete_id    = str(session['athlete'].get('id', 'unknown'))
    cache_key     = f'activities_{athlete_id}'
    force_refresh = request.args.get('refresh') == 'true'

    if not cache_has(cache_key) or force_refresh:
        activities, page = [], 1
        while True:
            batch = strava('athlete/activities', {'per_page': 200, 'page': page})
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < 200:
                break
            page += 1
        cache_set(cache_key, {'activities': activities, 'fetched_at': datetime.now().isoformat()})

    data       = cache_get(cache_key)
    activities = data['activities'] if data else []

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
    key = f'stream_{aid}'
    if not cache_has(key):
        try:
            data = strava(
                f'activities/{aid}/streams',
                {'keys': 'latlng,time,velocity_smooth,altitude', 'key_by_type': 'true'}
            )
            cache_set(key, data)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    return jsonify(cache_get(key))


# ─── Helpers: soste e clustering ──────────────────────────────────────────────

def _find_stops_in_segment(coords, times, vels, threshold=0.5, min_dur=10):
    """Trova soste in un segmento GPS (threshold in m/s, min_dur in secondi)."""
    stops = []
    in_stop = False
    stop_start = stop_lat = stop_lon = None
    for pos, t, v in zip(coords, times, vels):
        if v < threshold:
            if not in_stop:
                in_stop, stop_start, stop_lat, stop_lon = True, t, pos[0], pos[1]
        else:
            if in_stop:
                dur = t - stop_start
                if dur >= min_dur:
                    stops.append({'lat': stop_lat, 'lon': stop_lon, 'duration': int(dur)})
                in_stop = False
    return stops


def _cluster_stops(stops, cluster_radius=60):
    """Raggruppa soste vicine in cluster, restituisce centro e statistiche."""
    if not stops:
        return []
    used = [False] * len(stops)
    clusters = []
    for i, s in enumerate(stops):
        if used[i]:
            continue
        group = [s]
        used[i] = True
        for j, other in enumerate(stops):
            if not used[j] and haversine(s['lat'], s['lon'], other['lat'], other['lon']) < cluster_radius:
                group.append(other)
                used[j] = True
        clusters.append({
            'lat':          round(statistics.mean(c['lat'] for c in group), 6),
            'lon':          round(statistics.mean(c['lon'] for c in group), 6),
            'count':        len(group),
            'avg_duration': round(statistics.mean(c['duration'] for c in group)),
        })
    return clusters


def _compute_avg_speed_profile(segments, ref_idx, n_bins=80):
    """
    Crea profilo di velocità media combinando tutti i segmenti.
    Usa il segmento di riferimento come scheletro spaziale.
    Divide la tratta in n_bins slot di distanza e media le velocità
    di tutti i segmenti in ciascuno slot.
    """
    ref = segments[ref_idx]
    ref_coords = ref['coords']

    # Distanze cumulative del percorso di riferimento
    cum_ref = [0.0]
    for i in range(1, len(ref_coords)):
        d = haversine(ref_coords[i-1][0], ref_coords[i-1][1],
                      ref_coords[i][0], ref_coords[i][1])
        cum_ref.append(cum_ref[-1] + d)
    total_ref = cum_ref[-1] or 1.0

    # Accumula velocità per bin da ogni segmento
    bin_speeds = [[] for _ in range(n_bins)]
    for sg in segments:
        sg_coords = sg['coords']
        sg_vels   = sg['vels']
        # Distanze cumulative del segmento corrente
        cum_sg = [0.0]
        for i in range(1, len(sg_coords)):
            d = haversine(sg_coords[i-1][0], sg_coords[i-1][1],
                          sg_coords[i][0], sg_coords[i][1])
            cum_sg.append(cum_sg[-1] + d)
        total_sg = cum_sg[-1] or 1.0
        # Assegna ogni punto GPS al bin proporzionale
        for v, dist in zip(sg_vels, cum_sg):
            rel = dist / total_sg
            b   = min(int(rel * n_bins), n_bins - 1)
            bin_speeds[b].append(v * 3.6)

    # Costruisce il profilo usando le coordinate di riferimento
    profile = []
    for i, coord in enumerate(ref_coords):
        rel = cum_ref[i] / total_ref
        b   = min(int(rel * n_bins), n_bins - 1)
        speeds = bin_speeds[b]
        profile.append({
            'lat':   round(coord[0], 6),
            'lon':   round(coord[1], 6),
            'speed': round(statistics.mean(speeds), 1) if speeds else 0.0,
            'count': len(speeds),
        })
    return profile


# ─── API: tempo tra due punti ─────────────────────────────────────────────────

MAX_LIVE_FETCH = 12  # max stream fetch real-time per non andare in timeout

@app.route('/api/analyze/time_between_points', methods=['POST'])
@login_required
def api_time_between_points():
    body             = request.json
    point_a          = body.get('point_a')
    point_b          = body.get('point_b')
    radius           = body.get('radius', 50)
    activity_ids     = body.get('activity_ids', [])
    speed_threshold  = 0.5   # m/s ≈ 1.8 km/h
    min_stop_dur     = 10    # secondi

    results  = []
    segments = []          # sub-segmenti A→B con speed profile
    fetched  = 0           # contatore fetch live

    # Carica tutti gli stream disponibili in un'unica richiesta batch
    stream_keys = [f'stream_{aid}' for aid in activity_ids]
    streams     = cache_get_many(stream_keys)

    for aid in activity_ids:
        key = f'stream_{aid}'
        if key not in streams:
            if fetched >= MAX_LIVE_FETCH:
                continue   # troppi da scaricare: salta, suggerisci pre-cache
            try:
                data = strava(
                    f'activities/{aid}/streams',
                    {'keys': 'latlng,time,velocity_smooth', 'key_by_type': 'true'}
                )
                cache_set(key, data)
                streams[key] = data
                fetched += 1
            except:
                continue

        s = streams.get(key)
        if not s or 'latlng' not in s or 'time' not in s:
            continue

        coords = s['latlng']['data']
        times  = s['time']['data']
        vels   = s.get('velocity_smooth', {}).get('data', [0.0] * len(times))

        idx_a = idx_b = None
        for i, (pos, t) in enumerate(zip(coords, times)):
            if idx_a is None and haversine(pos[0], pos[1], point_a[0], point_a[1]) < radius:
                idx_a = i
            if idx_a is not None and idx_b is None and \
               haversine(pos[0], pos[1], point_b[0], point_b[1]) < radius:
                idx_b = i
                break

        if idx_a is None or idx_b is None or times[idx_b] <= times[idx_a]:
            continue

        secs = times[idx_b] - times[idx_a]
        results.append({'activity_id': aid, 'seconds': secs, 'minutes': round(secs / 60, 1)})
        segments.append({
            'activity_id': aid,
            'seconds':     secs,
            'coords':      coords[idx_a:idx_b + 1],
            'vels':        vels[idx_a:idx_b + 1],
            'times':       times[idx_a:idx_b + 1],
        })

    if not results:
        msg = 'Nessuna attività attraversa entrambi i punti.'
        if fetched == 0 and any(not cache_has(f'stream_{i}') for i in activity_ids):
            msg += ' Usa "Pre-carica GPS" nella sidebar per scaricare gli stream prima.'
        return jsonify({'error': msg, 'results': []})

    secs_list = [r['seconds'] for r in results]

    # Percorso rappresentativo = attività con tempo più vicino alla mediana
    median_secs = statistics.median(secs_list)
    ref_idx = min(range(len(segments)), key=lambda i: abs(segments[i]['seconds'] - median_secs))

    # Profilo velocità media combinando TUTTI i segmenti
    speed_profile = _compute_avg_speed_profile(segments, ref_idx)

    # Soste ricorrenti: trova in ogni segmento, poi raggruppa e filtra per frequenza
    all_stops = []
    for sg in segments:
        all_stops.extend(_find_stops_in_segment(sg['coords'], sg['times'], sg['vels'],
                                                speed_threshold, min_stop_dur))
    clustered   = _cluster_stops(all_stops, cluster_radius=60)
    min_count   = max(1, len(segments) // 4)   # almeno nel 25% delle attività
    freq_stops  = [st for st in clustered if st['count'] >= min_count]

    skipped = len(activity_ids) - len(results) - fetched

    return jsonify({
        'results': sorted(results, key=lambda x: x['seconds']),
        'stats': {
            'count':      len(secs_list),
            'avg_min':    round(statistics.mean(secs_list) / 60, 1),
            'median_min': round(statistics.median(secs_list) / 60, 1),
            'min_min':    round(min(secs_list) / 60, 1),
            'max_min':    round(max(secs_list) / 60, 1),
            'stdev_sec':  round(statistics.stdev(secs_list), 0) if len(secs_list) > 1 else 0,
        },
        'heatmap': {
            'speed_profile': speed_profile,
            'stops':         freq_stops,
        },
        'note': f'{fetched} stream scaricati ora; usa Pre-carica GPS per analisi più completa.' if fetched > 0 else '',
    })


# ─── API: analisi soste e velocità ────────────────────────────────────────────

@app.route('/api/analyze/stops', methods=['POST'])
@login_required
def api_stops():
    body      = request.json
    aid       = body.get('activity_id')
    threshold = body.get('speed_threshold', 0.5)   # m/s ≈ 1.8 km/h
    min_dur   = body.get('min_duration', 5)         # secondi minimi per considerarla una sosta

    key = f'stream_{aid}'
    if not cache_has(key):
        try:
            data = strava(
                f'activities/{aid}/streams',
                {'keys': 'latlng,time,velocity_smooth', 'key_by_type': 'true'}
            )
            cache_set(key, data)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    s      = cache_get(key)
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
    athlete_id = str(session['athlete'].get('id', 'unknown'))
    key        = f'activities_{athlete_id}'

    data = cache_get(key)
    if not data:
        return jsonify({'error': 'Attività non ancora caricate.'}), 400

    activities = [a for a in data['activities'] if a.get('commute')]
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

# Stato pre-cache persistito su file /tmp per essere condiviso tra i worker Gunicorn
_PRECACHE_STATE_FILE = '/tmp/strava_precache_state.json'
_precache_thread = None   # riferimento al thread corrente (solo nel worker che lo ha avviato)
_precache_lock   = threading.Lock()


def _read_precache_state():
    """Legge lo stato del pre-cache dal file condiviso (qualsiasi worker)."""
    try:
        with open(_PRECACHE_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {'running': False, 'done': 0, 'total': 0, 'errors': 0, 'log': []}


def _write_precache_state(state):
    """Scrive lo stato del pre-cache sul file condiviso (qualsiasi worker)."""
    try:
        with _precache_lock:
            with open(_PRECACHE_STATE_FILE, 'w') as f:
                json.dump(state, f)
    except Exception as e:
        print(f'[precache] state write error: {e}')


def _precache_worker(activity_ids, token):
    state = {'running': True, 'done': 0, 'total': len(activity_ids), 'errors': 0, 'log': []}
    _write_precache_state(state)

    headers     = {'Authorization': f'Bearer {token}'}
    batch_count = 0

    for aid in activity_ids:
        # Controlla se è stato richiesto uno stop
        current = _read_precache_state()
        if not current.get('running'):
            break

        key = f'stream_{aid}'
        if cache_has(key):
            state['done'] += 1
            if state['done'] % 10 == 0:
                _write_precache_state(state)
            continue  # già in cache, salta

        # Rispetta rate limit: pausa ogni 90 richieste
        if batch_count > 0 and batch_count % 90 == 0:
            state['log'] = ['⏳ Pausa 15 min per rate limit Strava…']
            _write_precache_state(state)
            time_mod.sleep(901)

        try:
            r = requests.get(
                f'{STRAVA_API}/activities/{aid}/streams',
                headers=headers,
                params={'keys': 'latlng,time,velocity_smooth,altitude', 'key_by_type': 'true'},
                timeout=15
            )
            if r.status_code == 429:  # rate limited
                state['log'] = ['⏳ Rate limit raggiunto, attesa 15 min…']
                _write_precache_state(state)
                time_mod.sleep(901)
                r = requests.get(f'{STRAVA_API}/activities/{aid}/streams',
                                 headers=headers,
                                 params={'keys': 'latlng,time,velocity_smooth,altitude',
                                         'key_by_type': 'true'}, timeout=15)
            if r.ok:
                cache_set(key, r.json())   # salva subito su Supabase
                batch_count += 1
            else:
                state['errors'] += 1
        except Exception as e:
            state['errors'] += 1
            state['log'] = [f'Errore {aid}: {e}']

        state['done'] += 1
        # Aggiorna il file ogni 5 attività (non ogni volta per non saturare I/O)
        if state['done'] % 5 == 0:
            _write_precache_state(state)

    state['running'] = False
    state['log'] = [f'✅ Pre-caricamento completato! ({state["done"]} stream salvati)']
    _write_precache_state(state)


@app.route('/api/precache/stop', methods=['POST'])
@login_required
def api_precache_stop():
    """Forza il reset del pre-cache (utile se il thread è bloccato)."""
    state = _read_precache_state()
    state['running'] = False
    state['log'] = ['⛔ Interrotto manualmente.']
    _write_precache_state(state)
    return jsonify({'stopped': True})


@app.route('/api/precache/start', methods=['POST'])
@login_required
def api_precache_start():
    global _precache_thread

    state = _read_precache_state()

    # Se risulta "running" nel file condiviso, controlla se il thread è vivo in questo worker
    if state.get('running'):
        if _precache_thread is not None and _precache_thread.is_alive():
            # Thread davvero in corso: il frontend riprende il polling
            return jsonify({**state, 'already_running': True}), 409
        else:
            # File dice running ma thread non c'è (altro worker o crash) → reset
            state['running'] = False
            _write_precache_state(state)

    athlete_id = str(session['athlete'].get('id', 'unknown'))
    key        = f'activities_{athlete_id}'

    data = cache_get(key)
    if not data:
        # Prova a caricare le attività on-the-fly
        try:
            activities, page = [], 1
            while True:
                batch = strava('athlete/activities', {'per_page': 200, 'page': page})
                if not batch: break
                activities.extend(batch)
                if len(batch) < 200: break
                page += 1
            data = {'activities': activities, 'fetched_at': datetime.now().isoformat()}
            cache_set(key, data)
        except Exception as e:
            return jsonify({'error': f'Impossibile caricare attività: {e}'}), 400

    commute_only = request.json.get('commute_only', True)
    acts = data['activities']
    if commute_only:
        acts = [a for a in acts if a.get('commute')]

    # Se non ci sono attività commute, usa tutte
    if not acts:
        acts = data['activities']

    ids    = [a['id'] for a in acts]
    cached = sum(1 for i in ids if cache_has(f'stream_{i}'))

    token = session['access_token']
    _precache_thread = threading.Thread(target=_precache_worker, args=(ids, token), daemon=True)
    _precache_thread.start()

    return jsonify({'started': True, 'total': len(ids), 'already_cached': cached})


@app.route('/api/precache/status')
@login_required
def api_precache_status():
    return jsonify(_read_precache_state())


if __name__ == '__main__':
    print("=" * 60)
    print("  Strava Commute Analyzer")
    print("  Apri: http://localhost:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)
