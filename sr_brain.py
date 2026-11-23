"""Search brain: searches that get better on their own.

- Scores every search by what it actually found (great leads, kept leads, people who replied).
- Retires searches that keep finding nothing.
- Learns new searches from your best leads (the phrases they title their videos with) and from what's trending
  on YouTube right now (e.g. a big game launch), and puts them in the right niche.
Everything lives in ~/.swipereach/search_brain.json.
"""
import json, os, re, threading, time
from collections import Counter

_LK = threading.RLock()
SPORTS = re.compile(r"(?i)\b(nba|nfl|nhl|mlb|ufc|fifa|premier league|football|soccer|basketball|baseball|hockey|steph curry|lebron|kobe|messi|ronaldo|super bowl|playoffs?|draft picks?)\b")
_B = {}


def _file():
    import sr_core as C
    return os.path.join(C.DATA, 'search_brain.json')


def _load():
    with _LK:
        if not _B:
            try: _B.update(json.load(open(_file(), encoding='utf-8')))
            except Exception: pass
            _B.setdefault('stats', {}); _B.setdefault('learned', {}); _B.setdefault('trend_t', 0)
            if _B.get('clean_v') not in (2, 3, 4):   # drop junk learned before the quality check existed
                _B['learned'] = {q: d for q, d in _B['learned'].items() if quality(q) and _ok(q)}; _B['clean_v'] = 2
            if _B.get('clean_v') == 2:   # v3: no sports
                _B['learned'] = {q: d for q, d in _B['learned'].items() if d.get('n') != 'sports analysis' and not SPORTS.search(q)}; _B['clean_v'] = 3
            import sr_core as C   # every start: forget searches for niches you removed or topics your skip list now blocks
            _B['learned'] = {q: d for q, d in _B['learned'].items() if d.get('n') in C.ALL_NICHES and _ok(q)}; _B['clean_v'] = max(_B.get('clean_v', 0), 4)
        return _B


def save():
    with _LK:
        b = _load(); tmp = _file() + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f: json.dump(b, f)
            os.replace(tmp, _file())
        except Exception: pass


def _st(q):
    s = _load()['stats']; k = q.lower().strip()
    return s.setdefault(k, {'runs': 0, 'checked': 0, 'kept': 0, 'high': 0, 'won': 0})


def ran(q):
    with _LK: x = _st(q); x['runs'] += 1; x['last'] = time.strftime('%Y-%m-%d')


def checked(q, lead):
    """one channel from search q was checked; lead is the kept lead or None"""
    if not q: return
    with _LK:
        x = _st(q); x['checked'] += 1
        if lead:
            x['kept'] += 1
            if lead.get('tier') == 'HIGH': x['high'] += 1


def value(q):
    """how good a search has proven to be: > 0 good, 0 unknown, < 0 retired (ran 2+ times, kept nothing)"""
    x = _load()['stats'].get(q.lower().strip())
    if not x or not x.get('checked'): return 0.0
    if x['runs'] >= 2 and x['kept'] == 0 and x['checked'] >= 15: return -1.0
    return (x['high'] * 3 + x['kept'] + x.get('won', 0) * 10) / (x['checked'] + 5)


def _ok(q):
    import sr_core as C
    return not (C.EXCLUDE_RE.search(q) or C.TECH_RE.search(q) or C.KIDS_CONTENT.search(q) or C.KIDS_GAMING.search(q)
                or any(rx.search(q) for rx, _ in C.AVOID_TOPICS))


def learn(q, niche, src):
    """remember a new search for a niche (skips anything on your skip lists, and anything already known)"""
    import sr_core as C
    q = re.sub(r'\s+', ' ', (q or '').strip().lower())
    if len(q) < 6 or not quality(q) or not _ok(q) or SPORTS.search(q) or niche == 'sports analysis': return False
    if niche not in C.ALL_NICHES: return False
    with _LK:
        L = _load()['learned']
        if q in L or q in {x.lower() for x in C.ALL_NICHES.get(niche, [])}: return False
        L[q] = {'n': niche, 'from': src[:80], 'added': time.strftime('%Y-%m-%d')}
        if len(L) > 3000:   # keep the best 3000 (by proven value, then newest)
            for k in sorted(L, key=lambda k: (value(k), L[k]['added']))[:len(L) - 3000]: L.pop(k, None)
        return True


def is_trend(q):
    return (_load()['learned'].get(q.lower().strip()) or {}).get('from') == 'trending now'


def pool(niche):
    """learned searches for this niche (retired ones left out)"""
    return [q for q, d in _load()['learned'].items() if d.get('n') == niche and value(q) >= 0]


def credit_winners(leads):
    """leads you replied with / pitched / won: the search that found them gets the biggest boost,
    and their video titles become new searches"""
    with _LK:
        for l in leads:
            if l.get('status') in ('replied', 'pitched', 'meeting', 'client', 'won') and l.get('query'):
                x = _st(l['query']); seen = x.setdefault('won_ids', [])
                if l['channel_id'] not in seen: seen.append(l['channel_id']); x['won'] = len(seen)
    best = [l for l in leads if l.get('status') in ('replied', 'pitched', 'meeting', 'client', 'won')]
    for q, n, src in phrases(best): learn(q, n, src)


_STOP = set('''video videos episode part official how what why this that with your from about best ever first every should the and for you are was were
will can just get got make made new full vs out into over under its it's i'm im our they them their his her has have had not but all any more most
one two day days week year years time today here there when where who which than then also very really much many some still even of a an in to on is my me we us at by as or if so do does did be'''.split())


_JUNK = set("""don doesn isn won didn wasn aren can couldn wouldn shouldn ve ll re ep ep1 pt vol part episode episodes chapter season day days week weeks
final life lives world worlds duo pro max ultra plus mini new old official live full update updates news trailer leak leaks vs reaction reacts react
shorts short clip clips vlog vlogs stream streams highlights compilation free quick easy hack hacks secret secrets""".split())


def quality(q):
    """a search a person would type to find creators: 2-5 real words, at least one meaningful word, no episode numbers or filler"""
    ws = re.findall(r"[a-z0-9][a-z0-9'.&-]*", (q or '').lower())
    if not 2 <= len(ws) <= 6: return False
    if any(re.fullmatch(r'(ep|pt|vol|s|e)\.?\d+', w) for w in ws): return False
    core = [w for w in ws if w not in _STOP and w not in _GENERIC and w not in _JUNK and not w.isdigit()]
    if not core or not (any(len(w) >= 4 for w in core) or any(w.isdigit() for w in ws)): return False
    if ws[0] in _STOP or ws[0] in _JUNK: return False
    return True


def phrases(leads, per=3):
    """phrases a lead repeats in their own video titles -> (search, niche, where it came from).
    Phrases are real word runs from the titles (never words glued together across a removed word)."""
    out = []
    for l in leads:
        grams = Counter()
        for t in l.get('video_titles') or []:
            seen = set()
            for chunk in re.split(r"[|:;,.!?()\[\]\"\u2013\u2014-]+", (t or '').lower()):
                ws = re.findall(r"[a-z0-9][a-z0-9']*", chunk)
                runs, cur = [], []
                for w in ws:   # stop words split a title into runs; phrases never jump over them
                    if w in _STOP or "'" in w or w in _JUNK:
                        if cur: runs.append(cur); cur = []
                    else: cur.append(w)
                if cur: runs.append(cur)
                for r in runs:
                    for n in (4, 3, 2):
                        for i in range(len(r) - n + 1): seen.add(' '.join(r[i:i + n]))
            grams.update(seen)   # counted once per video title
        picked = []
        for g, c in sorted(grams.items(), key=lambda kv: (-kv[1], -len(kv[0].split()))):
            if c < 2 or not quality(g) or any(g in p or p in g for p in picked): continue
            picked.append(g)
            if len(picked) >= per: break
        out += [(g, l.get('niche') or '', l.get('title') or '') for g in picked]
    return out


# YouTube categories worth watching for trends -> the niche their searches go into, and how to phrase them
TREND_CATS = {'20': ('game essays & lore', ['{t} video essay', '{t} analysis', '{t} lore explained', '{t} review']),
              '28': ('science & tech', ['{t} explained', '{t} deep dive']),
              '27': ('education', ['{t} explained', '{t} documentary']),
              '1': ('film & tv analysis', ['{t} breakdown', '{t} video essay']),
              '24': ('commentary', ['{t} explained', '{t} breakdown']),
              '26': ('talking head & advice', ['{t} explained', '{t} tips']),
              '19': ('travel documentary', ['{t} travel documentary'])}


_GENERIC = set("""explained explain lore review reviews tips guide analysis breakdown essay documentary history story trailer gameplay walkthrough
reaction reacts react update news leak leaks secrets things facts ranking ranked tier list boss bosses ending map hard easy beginner beginners""".split())


def _topics(titles):
    """names that several trending videos share: 'GTA 6', 'Elden Ring Nightreign', 'Hollow Knight Silksong'..."""
    c = Counter()
    for t in titles:
        t = re.sub(r"[\[\](){}|:!?\"#,]", ' ', t or '')
        seen = set()
        for m in re.finditer(r"\b[A-Z0-9][\w'.-]*(?:\s+(?:(?:of|the|and|a|in)\s+)?[A-Z0-9][\w'.-]*)*", t):
            ws = m.group(0).split()
            for n in range(1, min(5, len(ws)) + 1):
                for i in range(len(ws) - n + 1):
                    g = ws[i:i + n]; low = [w.lower().strip(".'") for w in g]
                    if low[0].isdigit() or low[0] in _STOP or low[-1] in _STOP or all(w in _GENERIC or w.isdigit() for w in low): continue
                    if any(w in _GENERIC for w in low): continue
                    if n == 1 and not (re.search(r'\d', g[0]) or (g[0].isupper() and 2 < len(g[0]) <= 5)) : continue
                    seen.add(' '.join(low))
        c.update(seen)
    picked = []
    for p, n in sorted(c.items(), key=lambda kv: (-kv[1], -len(kv[0]))):
        if n < 3: break   # at least 3 trending videos share it
        ws = p.split()
        if (len(ws) == 1 and len(p) <= 3) or ws[0] in _JUNK or all(w in _JUNK or w.isdigit() or len(w) <= 2 for w in ws): continue
        if any(p in q and c[q] >= n for q in picked) or any(q in p for q in picked): continue
        picked.append(p)
    return picked[:40]


_TR = threading.Lock()
def refresh_trends(log=lambda m: None, force=False):
    """once a day: read YouTube's trending charts (1 quota unit per category, ~8 total), turn the topics many
    videos share into searches for mid-size creators covering them (e.g. 'gta 6 video essay')"""
    import sr_core as C
    if not _TR.acquire(blocking=False): return 0
    try: return _refresh_trends(log, force)
    finally: _TR.release()


def _refresh_trends(log, force):
    import sr_core as C
    b = _load()
    if not force and time.time() - b.get('trend_t', 0) < 20 * 3600: return 0
    b['trend_t'] = time.time()   # claim today's refresh up front
    L = C.ls(); added = 0
    for cat, (niche, forms) in TREND_CATS.items():
        if niche not in C.ALL_NICHES: continue
        try:
            r = L.execute_with_rotation(lambda yt: yt.videos().list(part='snippet', chart='mostPopular', regionCode='US',
                                                                   videoCategoryId=cat, maxResults=50).execute(), 1)
        except Exception as e:
            log(f'trending ({cat}): {e}'); continue
        titles = [(it.get('snippet') or {}).get('title', '') for it in (r or {}).get('items', [])]
        for t in _topics(titles)[:8]:
            for f in forms:
                if learn(f.format(t=t), niche, 'trending now'): added += 1
    with _LK: b['trend_t'] = time.time()
    save()
    if added: log(f'{added} new searches from what is trending on YouTube right now')
    return added


def summary():
    b = _load(); s = b['stats']
    good = sorted((q for q in s if value(q) > 0), key=value, reverse=True)
    return {'learned': len(b['learned']), 'tracked': len(s), 'retired': sum(1 for q in s if value(q) < 0),
            'top': [{'q': q, 'high': s[q]['high'], 'kept': s[q]['kept'], 'won': s[q].get('won', 0)} for q in good[:10]],
            'trending': [q for q, d in b['learned'].items() if d.get('from') == 'trending now'][-12:]}
