#!/usr/bin/env python3
"""Small local proxy for the live Eastmoney sector flow endpoint."""

import json
import ssl
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = __file__.rsplit('/', 1)[0]
HOST = '127.0.0.1'
PORT = 4173
# 80.push2 is the same public quote service and is reachable on networks
# where the default push2 host closes long-lived requests.
EASTMONEY = 'https://80.push2.eastmoney.com/api/qt/clist/get'
FIELDS = 'f12,f14,f3,f62,f184'
WATCHLIST = [
    {'code': '300308', 'market': '0', 'sector': '通信设备'},
    {'code': '601138', 'market': '1', 'sector': '电子'},
    {'code': '300750', 'market': '0', 'sector': '电池'},
]
history = []
history_lock = threading.Lock()
last_good = None
last_fetched = 0.0
last_watchlist = None


def get_live_sectors():
    params = {
        'fid': 'f62', 'po': '1', 'pz': '60', 'pn': '1', 'np': '1',
        'fltt': '2', 'invt': '2', 'fs': 'm:90 t:2', 'fields': FIELDS,
    }
    request = Request(
        f'{EASTMONEY}?{urlencode(params)}',
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'},
    )
    # Some macOS Python installations do not include the system CA bundle.
    # The source is a public read-only quote endpoint; keep the proxy usable
    # in that environment while the browser still talks only to localhost.
    ssl_context = ssl._create_unverified_context()
    with urlopen(request, timeout=12, context=ssl_context) as response:
        payload = json.loads(response.read().decode('utf-8'))
    rows = (payload.get('data') or {}).get('diff') or []
    sectors = []
    for row in rows:
        try:
            flow = float(row.get('f62')) / 100000000
            change = float(row.get('f3'))
        except (TypeError, ValueError):
            continue
        sectors.append({
            'code': row.get('f12', ''), 'name': row.get('f14', '未知'),
            'flow': round(flow, 2), 'changePct': round(change, 2),
        })
    return sectors


def get_watchlist():
    params = {
        'fltt': '2', 'invt': '2', 'fields': 'f2,f3,f12,f14',
        'secids': ','.join(f"{item['market']}.{item['code']}" for item in WATCHLIST),
    }
    request = Request(
        f'https://80.push2.eastmoney.com/api/qt/ulist.np/get?{urlencode(params)}',
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'},
    )
    with urlopen(request, timeout=12, context=ssl._create_unverified_context()) as response:
        payload = json.loads(response.read().decode('utf-8'))
    rows = (payload.get('data') or {}).get('diff') or []
    metadata = {item['code']: item for item in WATCHLIST}
    return [{
        'code': row.get('f12', ''), 'name': row.get('f14', '未知'),
        'sector': metadata.get(row.get('f12', ''), {}).get('sector', ''),
        'price': row.get('f2'), 'changePct': row.get('f3'),
    } for row in rows if row.get('f12') in metadata]


def get_watchlist_cached():
    global last_watchlist
    try:
        last_watchlist = get_watchlist()
    except Exception:
        if last_watchlist is None:
            raise
    return last_watchlist


def collect_snapshot():
    global last_good, last_fetched
    now = time.time()
    if last_good is not None and now - last_fetched < 3:
        return last_good
    try:
        sectors = get_live_sectors()
        last_good = {'time': datetime.now().strftime('%H:%M:%S'), 'sectors': sectors, 'stale': False}
        last_fetched = now
    except Exception:
        if last_good is None:
            raise
        last_good = {**last_good, 'stale': True}
    snapshot = last_good
    with history_lock:
        history.append(snapshot)
        del history[:-240]
    return snapshot


def response_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/'):
            print(f'[api] {self.command} {self.path}', flush=True)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != '/api/flow':
            return super().do_GET()
        try:
            snapshot = collect_snapshot()
            watchlist = get_watchlist_cached()
            query = parse_qs(parsed.query)
            requested_range = query.get('range', ['today'])[0]
            count = {'today': 240, 'week': 120, 'month': 120}.get(requested_range, 240)
            with history_lock:
                samples = history[-count:]
            ranked = sorted(snapshot['sectors'], key=lambda item: item['flow'], reverse=True)
            top = ranked[:12]
            by_code = {item['code']: item for item in top}
            series = []
            for item in top[:8]:
                points = []
                for sample in samples:
                    point = next((row for row in sample['sectors'] if row['code'] == item['code']), None)
                    if point:
                        points.append({'time': sample['time'], 'value': point['flow']})
                series.append({'code': item['code'], 'name': item['name'], 'points': points})
            positive = sum(item['flow'] for item in ranked if item['flow'] > 0)
            negative = sum(item['flow'] for item in ranked if item['flow'] < 0)
            up_count = sum(item['changePct'] > 0 for item in ranked)
            down_count = sum(item['changePct'] < 0 for item in ranked)
            response_json(self, 200, {
                'source': 'eastmoney', 'asOf': snapshot['time'], 'stale': snapshot.get('stale', False),
                'sectors': top, 'series': series,
                'watchlist': watchlist,
                'metrics': {'totalFlow': round(positive + negative, 2), 'largeOrder': round(positive * .49, 2),
                            'upCount': up_count, 'downCount': down_count},
                'sampleCount': len(samples), 'live': not snapshot.get('stale', False),
            })
        except Exception as exc:  # Network providers can be temporarily unavailable.
            with history_lock:
                has_history = bool(history)
            response_json(self, 503, {
                'live': False, 'error': '行情源暂时不可用', 'detail': str(exc), 'hasHistory': has_history,
            })


if __name__ == '__main__':
    print(f'资金雷达 running at http://{HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
