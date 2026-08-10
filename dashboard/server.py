#!/usr/bin/env python3
"""Small local proxy for the live Eastmoney sector flow endpoint."""

import json
import os
import ssl
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from dashboard.xueqiu_analysis import analyze_posts, demo_posts, normalize_influencers
except ModuleNotFoundError:  # Support ``python dashboard/server.py`` from project root.
    from xueqiu_analysis import analyze_posts, demo_posts, normalize_influencers

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
watchlist_lock = threading.Lock()
XUEQIU_USERS = []
XUEQIU_POSTS = []
XUEQIU_SOURCE = 'demo'
xueqiu_lock = threading.Lock()


def get_xueqiu_users():
    with xueqiu_lock:
        return [item.copy() for item in XUEQIU_USERS]


def get_xueqiu_analysis():
    with xueqiu_lock:
        source = XUEQIU_SOURCE
    with xueqiu_lock:
        users = [item.copy() for item in XUEQIU_USERS]
        posts = [item.copy() for item in XUEQIU_POSTS]
    return analyze_posts(posts, users, source=source)


def load_demo_xueqiu():
    """Seed a useful local view while remote Xueqiu access is optional."""
    global XUEQIU_USERS, XUEQIU_POSTS, XUEQIU_SOURCE
    raw = os.environ.get('XUEQIU_UIDS') or os.environ.get('XUEQIU_UID') or ''
    users = []
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        uid, _, name = token.partition(':')
        if uid.isdigit():
            users.append({'uid': uid, 'name': name.strip() or f'雪球用户 {uid[-4:]}'})
    if not users:
        users = [
            {'uid': '1247347556', 'name': '价值投资笔记'},
            {'uid': '1596036202', 'name': '行业观察员'},
            {'uid': '2292705444', 'name': '成长股研究'},
        ]
    XUEQIU_USERS = users
    XUEQIU_POSTS = demo_posts(users)
    XUEQIU_SOURCE = 'demo'


load_demo_xueqiu()


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


def get_watchlist(items=None):
    items = items if items is not None else WATCHLIST
    if not items:
        return []
    params = {
        'fltt': '2', 'invt': '2', 'fields': 'f2,f3,f12,f14',
        'secids': ','.join(f"{item['market']}.{item['code']}" for item in items),
    }
    request = Request(
        f'https://80.push2.eastmoney.com/api/qt/ulist.np/get?{urlencode(params)}',
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'},
    )
    with urlopen(request, timeout=12, context=ssl._create_unverified_context()) as response:
        payload = json.loads(response.read().decode('utf-8'))
    rows = (payload.get('data') or {}).get('diff') or []
    metadata = {item['code']: item for item in items}
    return [{
        'code': row.get('f12', ''), 'name': row.get('f14', '未知'),
        'sector': metadata.get(row.get('f12', ''), {}).get('sector', ''),
        'price': row.get('f2'), 'changePct': row.get('f3'),
    } for row in rows if row.get('f12') in metadata]


def get_watchlist_cached():
    global last_watchlist
    with watchlist_lock:
        items = [item.copy() for item in WATCHLIST]
    try:
        quotes = get_watchlist(items)
        with watchlist_lock:
            last_watchlist = quotes
    except Exception:
        with watchlist_lock:
            cached = last_watchlist
        if cached is None:
            raise
    with watchlist_lock:
        return list(last_watchlist or [])


def get_watchlist_config():
    with watchlist_lock:
        return [item.copy() for item in WATCHLIST]


def normalize_watchlist(payload):
    raw_items = payload.get('items') if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError('items 必须是数组')
    if len(raw_items) > 20:
        raise ValueError('观察列表最多支持 20 只股票')
    normalized = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError('观察项格式不正确')
        code = str(raw.get('code', '')).strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f'股票代码必须是 6 位数字：{code or "空值"}')
        if code in seen:
            continue
        sector = str(raw.get('sector', '')).strip()[:20]
        market = '1' if code.startswith(('5', '6', '9')) else '0'
        normalized.append({'code': code, 'market': market, 'sector': sector})
        seen.add(code)
    return normalized


def replace_watchlist(items):
    global last_watchlist
    with watchlist_lock:
        WATCHLIST[:] = items
        last_watchlist = None


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
        if parsed.path == '/api/xueqiu/analysis':
            return response_json(self, 200, {'users': get_xueqiu_users(), **get_xueqiu_analysis()})
        if parsed.path == '/api/xueqiu/users':
            return response_json(self, 200, {'items': get_xueqiu_users()})
        if parsed.path == '/api/watchlist':
            return response_json(self, 200, {'items': get_watchlist_config()})
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

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/xueqiu/users':
            try:
                content_length = int(self.headers.get('Content-Length', '0'))
                if content_length > 16_384:
                    return response_json(self, 413, {'error': '请求内容过大'})
                items = normalize_influencers(json.loads(self.rfile.read(content_length).decode('utf-8')))
                global XUEQIU_USERS, XUEQIU_POSTS, XUEQIU_SOURCE
                with xueqiu_lock:
                    XUEQIU_USERS = items
                    XUEQIU_POSTS = demo_posts(items)
                    XUEQIU_SOURCE = 'demo'
                return response_json(self, 200, {'items': items})
            except (ValueError, json.JSONDecodeError) as exc:
                return response_json(self, 400, {'error': str(exc)})
        if parsed.path != '/api/watchlist':
            return response_json(self, 404, {'error': '接口不存在'})
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
            if content_length > 16_384:
                return response_json(self, 413, {'error': '请求内容过大'})
            payload = json.loads(self.rfile.read(content_length).decode('utf-8'))
            items = normalize_watchlist(payload)
            replace_watchlist(items)
            response_json(self, 200, {'items': get_watchlist_config()})
        except (ValueError, json.JSONDecodeError) as exc:
            response_json(self, 400, {'error': str(exc)})

    def do_POST(self):
        global XUEQIU_POSTS, XUEQIU_SOURCE
        parsed = urlparse(self.path)
        if parsed.path == '/api/xueqiu/refresh':
            try:
                from adata.xueqiu.collector import XueqiuCollector
                credential = os.environ.get('XUEQIU_COOKIE') or None
                collector = XueqiuCollector(credential=credential)
                rows = []
                for user in get_xueqiu_users():
                    frame = collector.get_posts(user['uid'])
                    rows.extend({**row, 'uid': user['uid'], 'author': user['name']}
                                for row in frame.to_dict('records'))
                with xueqiu_lock:
                    XUEQIU_POSTS = rows
                    XUEQIU_SOURCE = 'xueqiu'
                return response_json(self, 200, get_xueqiu_analysis())
            except Exception as exc:
                return response_json(self, 502, {'error': '雪球动态暂时无法访问', 'detail': str(exc), **get_xueqiu_analysis()})
        if parsed.path != '/api/xueqiu/posts':
            return response_json(self, 404, {'error': '接口不存在'})
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
            if content_length > 512_000:
                return response_json(self, 413, {'error': '发言内容过大'})
            payload = json.loads(self.rfile.read(content_length).decode('utf-8'))
            posts = payload.get('posts') if isinstance(payload, dict) else None
            if not isinstance(posts, list):
                raise ValueError('posts 必须是数组')
            with xueqiu_lock:
                XUEQIU_POSTS = posts[:500]
                XUEQIU_SOURCE = 'import'
            return response_json(self, 200, get_xueqiu_analysis())
        except (ValueError, json.JSONDecodeError) as exc:
            return response_json(self, 400, {'error': str(exc)})


if __name__ == '__main__':
    print(f'资金雷达 running at http://{HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
