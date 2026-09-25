import base64
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CT = "moviepilot-v3"

VERIFIER = r'''
import json, traceback, urllib.request

try:
    from app.core.config import settings
    tok = str(getattr(settings, 'API_TOKEN', '') or '')
except Exception:
    traceback.print_exc()
    tok = ''

API = 'http://127.0.0.1:3001/api/v1'


def get(path, **params):
    params['apikey'] = tok
    q = '&'.join('%s=%s' % (k, v) for k, v in params.items())
    url = API + path + '?' + q
    r = urllib.request.urlopen(url, timeout=180)
    return json.loads(r.read().decode('utf-8', 'replace'))


def unwrap(payload):
    if isinstance(payload, dict) and 'data' in payload and ('success' in payload or 'message' in payload):
        return payload['data']
    return payload


def walk(node, out):
    if isinstance(node, list):
        for x in node:
            walk(x, out)
        return
    if not isinstance(node, dict):
        return
    out.append(node)
    for v in node.values():
        if isinstance(v, (list, dict)):
            walk(v, out)


print('=== 1. 页面结构 ===')
page = unwrap(get('/plugin/page/EmbyUnwatchedWash'))
print('RENDER_MODE', page.get('render_mode'))
items = page.get('page') or []
nodes = []
walk(items, nodes)
print('TOP_NODES', len(items), 'ALL_NODES', len(nodes))

rows, toolbars = [], []
for n in nodes:
    if n.get('component') != 'VBtn':
        continue
    p = n.get('props') or {}
    ev = ((n.get('events') or {}).get('click') or {})
    api = ev.get('api', '')
    if 'select_set' in api:
        rows.append((p.get('text'), p.get('prepend-icon'), p.get('variant'), p.get('color'),
                     (ev.get('params') or {}).get('value')))
    elif 'select_' in api:
        toolbars.append((p.get('text'), api.split('/')[-1], p.get('disabled', False),
                         (ev.get('params') or {})))

print('ROW_COUNT', len(rows))
for r in rows[:3]:
    print('ROW', r)
print('TOOLBAR', toolbars)

txt = json.dumps(items, ensure_ascii=False)
for probe in ('媒体库未观看清单', '每页 12 部', '全选本页', '取消本页', '清空全部', 'mdi-format-list-checks'):
    print('HAS_%s' % probe, probe in txt)
idx_pick = txt.find('媒体库未观看清单')
idx_hist = txt.find('洗版历史')
print('PICKER_BEFORE_HISTORY', 0 <= idx_pick < idx_hist)
print('OLD_GROUP_LIST_GONE', '另有 ' not in txt and '每组仅预览' not in txt)
print('SECTION_TITLES_ORDER', [n.get('text') for n in nodes
                               if n.get('component') == 'span'
                               and (n.get('props') or {}).get('class', '').startswith('text-subtitle-1')])

print('=== 2. 功能实测（幂等勾选 / 批量 / 翻页）===')
if rows:
    tid = rows[0][4]
    print('ON_1', get('/plugin/EmbyUnwatchedWash/select_set', value=tid, on=1, page=1))
    page2 = unwrap(get('/plugin/page/EmbyUnwatchedWash'))
    n2 = []
    walk(page2.get('page') or [], n2)
    for n in n2:
        if n.get('component') != 'VBtn':
            continue
        ev = ((n.get('events') or {}).get('click') or {})
        if 'select_set' in ev.get('api', '') and str((ev.get('params') or {}).get('value')) == str(tid):
            print('AFTER_ON_ROW', (n.get('props') or {}).get('variant'),
                  (n.get('props') or {}).get('color'),
                  (n.get('props') or {}).get('prepend-icon'))
            break
    print('AFTER_ON_ALERT', '已勾选 1 部' in json.dumps(page2, ensure_ascii=False))
    print('ON_1_AGAIN', get('/plugin/EmbyUnwatchedWash/select_set', value=tid, on=1, page=1))
    print('OFF', get('/plugin/EmbyUnwatchedWash/select_set', value=tid, on=0, page=1))
    print('BULK_PAGE_ALL', get('/plugin/EmbyUnwatchedWash/select_bulk', mode='page_all', page=1))
    print('BULK_PAGE_NONE', get('/plugin/EmbyUnwatchedWash/select_bulk', mode='page_none', page=1))
    print('BULK_CLEAR', get('/plugin/EmbyUnwatchedWash/select_bulk', mode='clear_all', page=1))
    print('PAGE_2', get('/plugin/EmbyUnwatchedWash/select_page', page=2))
    page3 = unwrap(get('/plugin/page/EmbyUnwatchedWash'))
    t3 = json.dumps(page3, ensure_ascii=False)
    print('PAGE_2_REFLECTED', '第 2/' in t3)
    print('PAGE_1_BACK', get('/plugin/EmbyUnwatchedWash/select_page', page=1))
else:
    print('NO_ROWS_TO_TEST')
'''

b64 = base64.b64encode(VERIFIER.encode("utf-8")).decode("ascii")
chunks = [b64[i:i + 900] for i in range(0, len(b64), 900)]

lines = ["#!/bin/bash", "set +e", "rm -f /tmp/uwv9.b64 /tmp/uwv9.py"]
for c in chunks:
    lines.append("printf '%%s' '%s' >> /tmp/uwv9.b64" % c)
lines += [
    "base64 -d /tmp/uwv9.b64 > /tmp/uwv9.py",
    "echo '=== restart container ==='",
    "docker restart %s" % CT,
    "for i in $(seq 1 80); do",
    "  if docker logs --tail 400 %s 2>&1 | grep -q 'EmbyUnwatchedWash'; then echo wait_ok=$i; break; fi",
    "  sleep 3",
    "done",
    "docker exec -u 1000:10 %s sh -c 'grep -n plugin_version /app/app/plugins/embyunwatchedwash/__init__.py | head -2'" % CT,
    "docker logs --tail 400 %s 2>&1 | grep -E 'EmbyUnwatchedWash' | tail -4" % CT,
    "echo '=== verifier ==='",
    "docker exec -i -u 1000:10 %s python - < /tmp/uwv9.py 2>&1 | tail -70" % CT,
    "echo '=== sql state ==='",
    "docker exec postgres_moviepilot psql -U imushroom -d moviepilot -t -A -c \"select config_data from plugininstance where source_plugin_id ilike '%UnwatchedWash%';\"" ,
    "docker exec postgres_moviepilot psql -U imushroom -d moviepilot -t -A -c \"select plugin_id, key, value from plugindata where plugin_id ilike '%UnwatchedWash%' and key = 'list_page';\"",
    "echo '---end---'",
]

with open(os.path.join(BASE, "_v19.sh"), "w", encoding="ascii", newline="\n") as f:
    f.write("\n".join(lines) + "\n")
print("written _v19.sh b64=%d" % len(b64))
