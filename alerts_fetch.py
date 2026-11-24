import gzip, io, json, os, urllib.request
from datetime import datetime, timezone

KEY = os.environ['API_511_KEY']

def get(url):
    req = urllib.request.Request(url, headers={'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return json.loads(data.decode('utf-8-sig'))

def tr(texts):
    if not texts:
        return ''
    ts = texts.get('Translations') or []
    for t in ts:
        if (t.get('Language') or '').lower().startswith('en'):
            return t.get('Text') or ''
    return ts[0].get('Text') if ts else ''

alerts = []
try:
    raw = get('https://api.511.org/transit/servicealerts?api_key=' + KEY + '&format=json')
    for e in raw.get('Entities') or []:
        a = e.get('Alert')
        if not a:
            continue
        agency = ''
        route = ''
        for ie in a.get('InformedEntities') or []:
            if ie.get('AgencyId'):
                agency = ie['AgencyId']
            if ie.get('RouteId'):
                route = ie['RouteId']
            if agency:
                break
        periods = a.get('ActivePeriods') or []
        start = periods[0].get('Start') if periods else None
        end = periods[0].get('End') if periods else None
        alerts.append({
            'agency': agency,
            'route': tr(a.get('HeaderText')),
            'description': tr(a.get('DescriptionText')),
            'url': tr(a.get('Url')),
            'start': start,
            'end': end,
            'route': route,
        })
except Exception as ex:
    print('servicealerts failed:', ex)

events = []
try:
    raw = get('https://api.511.org/traffic/events?api_key=' + KEY + '&format=json')
    for ev in raw.get('events') or []:
        if (ev.get('status') or '') != 'ACTIVE':
            continue
        roads = ', '.join((r.get('name') or '') for r in (ev.get('roads') or []) if r.get('name'))
        areas = ev.get('areas') or []
        geo = ev.get('geography') or {}
        coords = geo.get('coordinates') if geo.get('type') == 'Point' else None
        events.append({
            'headline': ev.get('headline') or '',
            'type': ev.get('event_type') or '',
            'severity': ev.get('severity') or '',
            'roads': roads,
            'area': areas[0].get('name') if areas else '',
            'lat': coords[1] if coords else None,
            'lon': coords[0] if coords else None,
            'updated': ev.get('updated') or '',
        })
except Exception as ex:
    print('traffic events failed:', ex)

out = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'alerts': alerts[:300],
    'events': events[:200],
}
with open('alerts.json', 'w') as f:
    json.dump(out, f)
print('alerts:', len(out['alerts']), 'events:', len(out['events']))
