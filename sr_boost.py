"""SwipeReach boosters: more emails, safer sending, warmer leads, faster replies, and what's working.
Everything here is extra: if any piece fails, the rest of the app carries on as before."""
import json, os, re, time, threading, urllib.request, urllib.parse, random, string
from datetime import datetime, timedelta, timezone

import sr_core as C

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'}
REPLIED = ('replied', 'pitched', 'meeting', 'client', 'won')


def _get(url, timeout=8, limit=250000, headers=None):
    req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
    return urllib.request.urlopen(req, timeout=timeout).read(limit).decode('utf-8', 'ignore')


def _today():
    return datetime.now().date()


def _dt(s):
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception: return None


# =====================================================================================================
# 1) MORE EMAILS: link-in-bio pages, their website, hidden "name [at] domain [dot] com" spellings
# =====================================================================================================
BIO = re.compile(r"https?://(?:www\.)?(?:linktr\.ee|beacons\.ai|stan\.store|bio\.link|linkin\.bio|campsite\.bio|[\w-]+\.carrd\.co|carrd\.co|hoo\.be|lnk\.bio|"
                 r"msha\.ke|solo\.to|taplink\.cc|komi\.io|snipfeed\.co|allmylinks\.com|bento\.me|linkpop\.com|tap\.bio|linkr\.bio|biolinky\.co|lynx\.bio|"
                 r"withkoji\.com|koji\.to|direct\.me|contactinbio\.com|milkshake\.app|later\.com/linkinbio)/?[\w.\-/@]*", re.I)
URL = re.compile(r"https?://[^\s<>()\"']+", re.I)
SOCIAL = re.compile(r"(?i)(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|facebook\.com|threads\.net|discord|twitch\.tv|"
                    r"patreon\.com|spotify|apple\.com|amazon\.|amzn|bit\.ly|geni\.us|ko-fi|paypal|linkedin|pinterest|snapchat|reddit|t\.me|wa\.me)")


def deobfuscate(t):
    t = t or ''
    t = re.sub(r'(?i)\s*[\[\(\{<]\s*(?:at|@)\s*[\]\)\}>]\s*', '@', t)
    t = re.sub(r'(?i)\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*', '.', t)
    t = re.sub(r'(?i)\b([\w.+-]{2,})\s+at\s+([\w-]{2,})\s+dot\s+(com|net|org|co|io|me|tv|uk|ca|au)\b', r'\1@\2.\3', t)
    t = re.sub(r'(?i)\b([\w.+-]{2,})@([\w-]{2,})\s+dot\s+(com|net|org|co|io|me|tv|uk|ca|au)\b', r'\1@\2.\3', t)
    return t


def emails_in(text):
    out = []
    for e in re.findall(r'(?:mailto:)?([A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})', deobfuscate(text)):
        e = C.clean_email(e.strip('.').lower())
        if e and not re.search(r'\.(png|jpe?g|gif|webp|svg|js|css)$', e) and not re.search(r'(?i)sentry|wixpress|example\.|domain\.com|email\.com$|yourname', e) and e not in out:
            out.append(e)
    return out


def _best(cands, lead):
    """prefer an address that matches their name / channel, then business-y ones, never no-reply"""
    if not cands: return ''
    name = C._tok(lead.get('title') or '') | C._tok(lead.get('first_name') or '')
    def key(e):
        u, d = e.split('@')
        return (e.startswith(('noreply', 'no-reply', 'donotreply')), not any(n in e for n in name if len(n) >= 4),
                not re.search(r'(?i)business|collab|contact|hello|hi@|inquir|booking|work|partner|team|mgmt|management', u), len(e))
    return sorted(cands, key=key)[0]


def find_email(lead, deep=True):
    """(email, where) or ('', '')"""
    text = ' '.join(str(lead.get(k) or '') for k in ('about', 'video_snips', 'contacts'))
    text += ' ' + str((lead.get('latest_video') or {}).get('description') or '')
    e = _best(emails_in(text), lead)
    if e: return e, 'their YouTube text'
    if not deep: return '', ''
    tried = set()
    for u in list(dict.fromkeys(m.group(0).rstrip('.,)') for m in BIO.finditer(text)))[:3]:   # link-in-bio pages
        tried.add(u)
        try: page = _get(u)
        except Exception: continue
        e = _best(emails_in(page), lead)
        if e: return e, 'their link-in-bio page'
        for site in [x.rstrip('.,)"\'') for x in URL.findall(page) if not SOCIAL.search(x) and not BIO.search(x)][:2]:   # their site linked from the bio page
            if site in tried: continue
            tried.add(site)
            try:
                e = C.site_email(site)
                if e: return e, 'their website (via link-in-bio)'
            except Exception: pass
    if lead.get('website') and lead['website'] not in tried:
        try:
            e = C.site_email(lead['website'])
            if e: return e, 'their website'
        except Exception: pass
        try:   # deobfuscated contact page text the plain search misses
            base = re.match(r'https?://[^/]+', lead['website']).group(0)
            for p in ('/contact', '/about', ''):
                e = _best(emails_in(_get(base + p)), lead)
                if e: return e, 'their website'
        except Exception: pass
    return '', ''


_JOB = {'running': False, 'stop': False}


def find_emails(progress=lambda d: None, stop=None, max_leads=None, pause=lambda: False):
    """background sweep over every lead with no email (no YouTube quota). Retries a lead at most every 14 days."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if _JOB['running']: return {'busy': True}
    _JOB.update(running=True, stop=False)
    try:
        cut = (_today() - timedelta(days=14)).isoformat()
        todo = [l for l in C.leads_ro() if not l.get('email') and l.get('status', 'new') in ('new', 'skip') and (l.get('email_searched') or '') < cut]
        if max_leads: todo = todo[:max_leads]
        got, done, found, last = {}, [0], [0], [time.time()]
        def one(l):
            while pause() and not _JOB['stop']: time.sleep(2)   # a search is running: it gets the whole PC + connection
            if _JOB['stop']: return l['channel_id'], ('', '')
            try: return l['channel_id'], find_email(l)
            except Exception: return l['channel_id'], ('', '')
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(one, l) for l in todo]
            for f in as_completed(futs):
                cid, (e, src) = f.result(); done[0] += 1
                got[cid] = (e, src)
                if e: found[0] += 1
                if done[0] % 25 == 0 or done[0] == len(todo): progress({'done': done[0], 'total': len(todo), 'found': found[0]})
                if (len(got) >= 300 or time.time() - last[0] > 90) and not pause():   # saving 6,000+ leads is heavy: do it rarely, never mid-search
                    _apply(got); got = {}; last[0] = time.time()
        _apply(got)
        n = sum(1 for l in C.leads_ro() if l.get('email_src') and l.get('email_found_on') == _today().isoformat())
        return {'checked': len(todo), 'found': n}
    finally: _JOB['running'] = False


def _apply(got):
    if not got: return
    day = _today().isoformat()
    def f(L):
        for x in L:
            r = got.get(x['channel_id'])
            if not r: continue
            x['email_searched'] = day
            if r[0] and not x.get('email'):
                x['email'] = r[0]; x['email_src'] = r[1]; x['email_found_on'] = day
                ck = check_email(r[0]); x['email_check'] = ck
    C.mutate(f)


# =====================================================================================================
# 4) EMAIL CHECK: can this address actually receive mail? (syntax, typos, no-reply, domain has mail servers)
# =====================================================================================================
BIG = {'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'yahoo.com', 'icloud.com', 'me.com', 'mac.com', 'aol.com',
       'proton.me', 'protonmail.com', 'gmx.com', 'gmx.de', 'zoho.com', 'yandex.com', 'mail.com', 'hotmail.co.uk', 'yahoo.co.uk', 'outlook.co.uk', 'fastmail.com'}
TYPO = {'gmial.com': 'gmail.com', 'gamil.com': 'gmail.com', 'gmai.com': 'gmail.com', 'gmail.co': 'gmail.com', 'gmail.con': 'gmail.com', 'gnail.com': 'gmail.com',
        'hotmial.com': 'hotmail.com', 'hotmail.co': 'hotmail.com', 'yahooo.com': 'yahoo.com', 'yaho.com': 'yahoo.com', 'outlok.com': 'outlook.com', 'iclod.com': 'icloud.com'}
DISPOSABLE = re.compile(r'(?i)@(mailinator|guerrillamail|10minutemail|tempmail|temp-mail|yopmail|trashmail|sharklasers|getnada|dispostable)\.')
_MX = {}


def _mx(dom):
    """True = has mail servers, False = domain can't receive mail, None = couldn't tell (offline)"""
    if dom in _MX: return _MX[dom]
    res = None
    for url in (f'https://dns.google/resolve?name={dom}&type=MX', f'https://cloudflare-dns.com/dns-query?name={dom}&type=MX'):
        try:
            d = json.loads(_get(url, timeout=5, headers={'accept': 'application/dns-json'}))
            if d.get('Status') == 3: res = False; break   # NXDOMAIN: the domain doesn't exist
            if any(a.get('type') == 15 for a in d.get('Answer', []) or []): res = True; break
            a = json.loads(_get(url.replace('type=MX', 'type=A'), timeout=5, headers={'accept': 'application/dns-json'}))
            res = bool(a.get('Answer')); break   # no MX but a website: mail usually still works
        except Exception: continue
    if res is not None: _MX[dom] = res
    return res


FREE = {'gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'live.com', 'aol.com', 'proton.me', 'protonmail.com', 'me.com', 'msn.com',
        'gmx.com', 'yahoo.co.uk', 'hotmail.co.uk', 'mail.com', 'zoho.com', 'yandex.com'}
AGENCY_WORDS = re.compile(r'(?i)(talent|management|mgmt|agency|influen|creators?(group|agency)|media(group)?\b|partnerships?|represent)')
_AG = [None, set()]


def agency_domains():
    """domains that several different creators use = a management agency (handles brand deals, rarely forwards editing offers)"""
    L = C.leads_ro()
    if _AG[0] is L: return _AG[1]
    cnt = {}
    for l in L:
        e = (l.get('email') or '').lower()
        if '@' in e:
            d = e.split('@')[1]
            if d not in FREE: cnt.setdefault(d, set()).add(l['channel_id'])
    _AG[0], _AG[1] = L, {d for d, ids in cnt.items() if len(ids) >= 3}
    return _AG[1]


def is_agency(e):
    e = (e or '').lower()
    if '@' not in e: return False
    u, d = e.split('@')
    return d in agency_domains() or (d not in FREE and bool(AGENCY_WORDS.search(d)) and not AGENCY_WORDS.search(u))


def check_email(e):
    r = _check_email(e); r['email'] = (e or '').strip()
    if r.get('ok') is not False and is_agency(e): r['agency'] = True; r['why'] = 'a management agency address (brand deals): it may not reach the creator'
    if r.get('ok') is not False and not C.clean_email(e): r = {'ok': False, 'why': 'not a real contact address', 'email': (e or '').strip()}
    return r


def _check_email(e):
    e = (e or '').strip().lower()
    if not re.fullmatch(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', e): return {'ok': False, 'why': "that isn't a valid email address"}
    u, d = e.split('@')
    if d in TYPO: return {'ok': False, 'why': f'typo? probably {u}@{TYPO[d]}', 'fix': f'{u}@{TYPO[d]}'}
    if re.match(r'(?i)(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce)', u): return {'ok': False, 'why': 'a no-reply address: nobody reads it'}
    if DISPOSABLE.search(e): return {'ok': False, 'why': 'a throwaway address'}
    if d in BIG: return {'ok': True, 'why': 'real mail provider'}
    mx = _mx(d)
    if mx is False: return {'ok': False, 'why': f'{d} has no mail server: it would bounce'}
    if mx is None: return {'ok': None, 'why': "couldn't check right now"}
    return {'ok': True, 'why': 'domain receives email'}


def check_all(progress=lambda d: None):
    from concurrent.futures import ThreadPoolExecutor
    todo = [l for l in C.leads_ro() if l.get('email') and not (l.get('email_check') or {}).get('ok') is True and l.get('status', 'new') in ('new', 'drafted')]
    res = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for l, r in zip(todo, ex.map(lambda l: check_email(l['email']), todo)): res[l['channel_id']] = r
    def f(L):
        for x in L:
            if x['channel_id'] in res: x['email_check'] = res[x['channel_id']]
    C.mutate(f)
    return {'checked': len(todo), 'bad': sum(1 for r in res.values() if r.get('ok') is False)}


# =====================================================================================================
# 15) WARM-UP AUTOPILOT: how many cold emails today, for a new sending account
# =====================================================================================================
WARMUP = [0, 5, 10, 15, 20, 25]   # per day, by week (week 1 = normal emails only)


def sent_today():
    t = _today(); n = 0
    for l in C.leads_ro():
        o = l.get('outreach') or {}
        for s in [o.get('sent_at')] + list(o.get('followups') or []):
            d = _dt(s)
            if d and d.astimezone().date() == t: n += 1
        d = _dt(o.get('draft_at'))
        if l.get('status') == 'drafted' and d and d.astimezone().date() == t: n += 1   # opened today, about to be sent
    return n


def warmup():
    S = C.load_settings(); w = S.get('warmup') or {}
    sent = sent_today()
    if not w.get('start'): return {'on': False, 'sent_today': sent, 'limit': None, 'left': None}
    try: start = datetime.fromisoformat(w['start']).date()
    except Exception: return {'on': False, 'sent_today': sent, 'limit': None, 'left': None}
    day = (_today() - start).days; wk = max(0, day // 7)
    limit = int(w.get('max', 30)) if wk >= len(WARMUP) else WARMUP[wk]
    nxt = None if wk >= len(WARMUP) - 1 else {'in_days': 7 - day % 7, 'limit': WARMUP[wk + 1]}
    return {'on': True, 'start': start.isoformat(), 'day': day + 1, 'week': wk + 1, 'limit': limit, 'sent_today': sent, 'left': max(0, limit - sent), 'next': nxt,
            'tip': 'Week 1: only normal emails to friends and family, get replies. Cold emails start next week.' if wk == 0 else
                   'Spread your emails through the day, keep bounces at zero.'}


def set_warmup(start):
    S = C.load_settings()
    if start: datetime.fromisoformat(start); S['warmup'] = {'start': start, 'max': (S.get('warmup') or {}).get('max', 30)}
    else: S.pop('warmup', None)
    C.save_settings(S); return warmup()


# =====================================================================================================
# 6, 7, 19) TIMING SIGNALS (computed when a channel is checked; see analyze in sr_core)
# =====================================================================================================
def signals(info, vids):
    """('editor_left', why) / ('slowing', why) / ('pays_editor', why) from their recent videos"""
    out = []
    try:
        ed = C.ls().editor_credits
        if len(vids) >= 8:
            new_n, _ = ed(vids[:4]); old_n, who = ed(vids[4:])
            if old_n >= 2 and new_n == 0: out.append(('editor_left', 'credited an editor (' + ', '.join(who[:2]) + ') before, not in the latest videos: may need a new editor'))
        n, who = ed(vids)
        if n and not any(s[0] == 'editor_left' for s in out): out.append(('pays_editor', 'pays an editor (' + ', '.join(who[:2]) + ')'))
    except Exception: pass
    try:
        lv = [v for v in vids if (v.get('duration_seconds') or 0) >= 240 or v.get('was_live')]
        ds = sorted((_dt(v.get('published_raw')) for v in lv if _dt(v.get('published_raw'))), reverse=True)
        if len(ds) >= 6:
            gaps = [(ds[i] - ds[i + 1]).days for i in range(len(ds) - 1)]
            usual = sorted(gaps[2:])[len(gaps[2:]) // 2] or 1
            now_gap = max((datetime.now(timezone.utc) - ds[0]).days, gaps[0], gaps[1])
            if usual <= 14 and now_gap >= max(2.2 * usual, usual + 6) and now_gap <= 75:
                out.append(('slowing', f'used to upload every ~{usual} days, now {now_gap} days: may be overwhelmed'))
    except Exception: pass
    return out


# =====================================================================================================
# 2) LOOKALIKES: peers of your best leads (their featured channels + creators commenting on their videos)
# =====================================================================================================
def lookalikes(run, seeds=None, max_seeds=25):
    L = C.ls(); S = C.load_settings(); leads = C.leads_ro()
    if seeds is None:
        won = [l for l in leads if l.get('status') in REPLIED]
        high = sorted((l for l in leads if l.get('tier') == 'HIGH' and l.get('status', 'new') in ('new', 'contacted', 'followup', 'drafted')),
                      key=lambda l: -(l.get('score') or 0))
        done = set(S.get('lookalike_done') or [])
        seeds = [l for l in won + high if l['channel_id'] not in done][:max_seeds]
    existing = {l['channel_id'] for l in leads}
    try: processed = set(L.load_processed_ids())
    except Exception: processed = set()
    total, found, cnt = len(seeds), [], {'checked': 0, 'seeds': 0}
    lk = threading.Lock()
    def one_seed(s):
        if run.stop: return
        cid = s['channel_id']; ids = []
        try:
            info = L.load_cached_channel(cid) or L.get_channel_info(cid) or {}
            for f in info.get('featured_channels') or []:
                m = re.search(r'UC[\w-]{22}', str(f))
                if m: ids.append(m.group(0))
        except Exception: pass
        try:
            vids = [v for v in (run.videos(cid, 15) or []) if (v.get('duration_seconds') or 0) >= 240][:2]
            for v in vids:   # creators comment on each other's videos: 1 quota unit per 100 comments
                r = L.execute_with_rotation(lambda yt, vid=v['video_id']: yt.commentThreads().list(part='snippet', videoId=vid, maxResults=100, order='relevance', textFormat='plainText').execute(), 1)
                for it in (r or {}).get('items', []):
                    a = (((it.get('snippet') or {}).get('topLevelComment') or {}).get('snippet') or {}).get('authorChannelId', {}).get('value')
                    if a: ids.append(a)
        except Exception as e: run.log(f'comments for {s.get("title")}: {e}')
        with lk:
            cand = [c for c in dict.fromkeys(ids) if c != cid and c not in existing and c not in processed]
            existing.update(cand)
        mode = s.get('mode') if s.get('mode') in ('business', 'creators') else 'business'
        cand = run._prefilter(cand, run.creator(mode, s.get('niche')), S)
        run.log(f'like {s.get("title")}: {len(ids)} related channels, {len(cand)} worth checking')
        for c, lead in run._check_many(cand, mode, s.get('niche') or 'similar', 'lookalike: ' + (s.get('title') or ''), S):
            with lk:
                cnt['checked'] += 1
                if lead:
                    lead.setdefault('reasons', []).insert(0, f'similar to {s.get("title")}')
                    found.append(lead); run.ev('lead', lead); C.add_lead(lead); C.sheet_mark()
                if cnt['checked'] % 10 == 0: run.ev('checked', cnt['checked'])
            if run.stop: break
        with lk:
            cnt['seeds'] += 1; run.ev('progress', {'i': cnt['seeds'], 'n': total, 'q': 'like ' + (s.get('title') or ''), 'done': cnt['seeds'], 'total': total + 1})
            S2 = C.load_settings(); S2['lookalike_done'] = (list(S2.get('lookalike_done') or []) + [cid])[-3000:]; C.save_settings(S2)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex: list(ex.map(one_seed, seeds))   # 6 of your best leads at a time
    checked = cnt['checked']
    run.ev('done', {'checked': checked, 'found': len(found), 'stopped': run.stop, 'searches': total})
    return found


# =====================================================================================================
# 18) HIRING RADAR: fresh "looking for a video editor" posts
# =====================================================================================================
RADAR = [('forhire', 'video editor'), ('forhire', 'youtube editor'), ('hiring', 'video editor'), ('freelance_forhire', 'video editor'),
         ('VideoEditingRequests', 'paid'), ('NewTubers', 'hiring editor'), ('youtubers', 'looking for editor'), ('PartneredYoutube', 'editor')]
HIRE = re.compile(r'(?i)\[?\s*hiring\s*\]?|looking for (an? )?(video |youtube |long[- ]form )?editor|need(ing)? (an? )?(video |youtube )?editor|paid')
FORHIRE = re.compile(r'(?i)\[\s*for hire\s*\]|\bfor hire\b')


def radar(limit=40):
    S = C.load_settings(); seen = set(S.get('radar_seen') or []); posts, errs = [], []
    for sub, q in RADAR:
        url = f'https://www.reddit.com/r/{sub}/search.json?' + urllib.parse.urlencode({'q': q, 'restrict_sr': 1, 'sort': 'new', 't': 'week', 'limit': 25})
        try: d = json.loads(_get(url, timeout=10, headers={'User-Agent': 'SwipeReachStudio/1.0 (job radar)'}))
        except Exception as e: errs.append(f'r/{sub}: {e}'); continue
        for c in (d.get('data') or {}).get('children', []):
            p = c.get('data') or {}; t = p.get('title') or ''; body = p.get('selftext') or ''
            if FORHIRE.search(t) or not HIRE.search(t + ' ' + body[:400]) or not re.search(r'(?i)edit', t + ' ' + body[:600]): continue
            if p.get('over_18'): continue
            posts.append({'id': p.get('id'), 'title': t, 'text': body[:4000], 'url': 'https://www.reddit.com' + (p.get('permalink') or ''),
                          'where': 'r/' + sub, 'when': datetime.fromtimestamp(p.get('created_utc') or 0, timezone.utc).isoformat()[:16],
                          'author': p.get('author'), 'new': p.get('id') not in seen})
    uniq = {}
    for p in sorted(posts, key=lambda p: p['when'], reverse=True): uniq.setdefault(p['id'], p)
    out = list(uniq.values())[:limit]
    S['radar_seen'] = (list(seen) + [p['id'] for p in out if p['id'] not in seen])[-2000:]; C.save_settings(S)
    return {'posts': out, 'errors': errs[:3]}


# =====================================================================================================
# 11) PRICE SUGGESTION
# =====================================================================================================
HIGH_PAY = {'US', 'CA', 'GB', 'UK', 'AU', 'NZ', 'IE', 'DE', 'NL', 'CH', 'NO', 'SE', 'DK', 'FI', 'AT', 'SG', 'AE'}


def price(lead):
    """a sensible per-video rate range for a 10-20 min long-form edit, from what they can likely afford"""
    mv = int(lead.get('median_views') or 0); subs = int(lead.get('subs') or 0)
    r = ' '.join(lead.get('reasons') or [])
    if mv >= 100000 or subs >= 300000: lo, hi = 350, 700
    elif mv >= 30000 or subs >= 100000: lo, hi = 200, 400
    elif mv >= 8000 or subs >= 30000: lo, hi = 120, 250
    else: lo, hi = 70, 150
    why = []
    if re.search(r'sponsor', r): lo, hi = lo * 1.2, hi * 1.25; why.append('has sponsors')
    if re.search(r'courses|coaching|sells|own website|runs a business', r): lo, hi = lo * 1.15, hi * 1.2; why.append('sells something')
    if re.search(r'pays an editor|credits an editor', r): lo, hi = lo * 1.1, hi * 1.15; why.append('already pays an editor')
    cc = str(lead.get('country') or '').upper()
    if cc in HIGH_PAY: why.append(f'based in {cc}')
    elif cc and cc not in ('N/A', ''): lo, hi = lo * 0.75, hi * 0.8; why.append(f'based in {cc} (usually lower budgets)')
    rnd = lambda x: int(round(x / 10.0) * 10)
    return {'low': rnd(lo), 'high': rnd(hi), 'text': f'${rnd(lo)}-{rnd(hi)} per 10-20 min video', 'why': ', '.join(why) or 'based on their views'}


# =====================================================================================================
# 12, 23) PHONE ALERTS (ntfy: free app, no account)
# =====================================================================================================
def ntfy_topic(create=False):
    S = C.load_settings(); t = S.get('ntfy_topic')
    if not t and create:
        t = 'swipereach-' + ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12)); S['ntfy_topic'] = t; C.save_settings(S)
    return t


def notify(title, msg, url=None):
    t = C.load_settings().get('ntfy_topic')
    if not t: return False
    try:
        h = {'Title': title.encode('ascii', 'ignore').decode() or 'SwipeReach', 'Tags': 'tada'}
        if url: h['Click'] = url
        req = urllib.request.Request('https://ntfy.sh/' + t, data=msg.encode('utf-8'), headers=h, method='POST')
        urllib.request.urlopen(req, timeout=8).read(); return True
    except Exception: return False


# =====================================================================================================
# 10) REPLY DRAFTS
# =====================================================================================================
def _text_of(payload):
    import base64
    if not payload: return ''
    if payload.get('mimeType', '').startswith('text/plain') and (payload.get('body') or {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data'] + '==').decode('utf-8', 'ignore')
    for p in payload.get('parts') or []:
        t = _text_of(p)
        if t: return t
    if (payload.get('body') or {}).get('data'):
        h = base64.urlsafe_b64decode(payload['body']['data'] + '==').decode('utf-8', 'ignore'); return re.sub(r'<[^>]+>', ' ', h)
    return ''


def their_last_reply(lead):
    import sr_compose as X
    svc = X.gmail(); me = X._me(svc); o = lead.get('outreach') or {}
    t = svc.users().threads().get(userId='me', id=o['thread_id'], format='full').execute()
    hdr = lambda m, k: next((h['value'] for h in m.get('payload', {}).get('headers', []) if h['name'] == k), '')
    theirs = [m for m in t['messages'] if 'DRAFT' not in m.get('labelIds', []) and me not in hdr(m, 'From').lower()]
    if not theirs: return ''
    txt = _text_of(theirs[-1].get('payload'))
    txt = re.split(r'(?m)^On .{5,80}wrote:\s*$|^>|^-{2,}\s*Original Message', txt)[0]   # just their new words, not the quoted history
    return txt.strip()[:3000]


def kind_of(reply):
    r = (reply or '').lower()
    if re.search(r"\b(not interested|no thanks|no thank you|we('re| are) good|already have|not looking|pass on this|unsubscribe|stop emailing)\b", r): return 'no'
    if re.search(r'\b(rate|rates|price|pricing|cost|charge|budget|how much|per video|quote)\b', r): return 'price'
    if re.search(r'\b(sure|yes|yeah|yep|go ahead|send it|sounds good|love to see|happy to see|would love|let.?s see|absolutely|ok|okay)\b', r): return 'sure'
    return 'question'


def reply_text(lead, reply):
    """their reply -> your answer: Claude with your master doc when it can, otherwise your own short templates"""
    import sr_compose as X
    k = kind_of(reply); n = lead.get('first_name') or ''; p = price(lead)
    tmpl = {'sure': f"Awesome{', ' + n if n else ''}! I'll get started on it and send it over in the next day or two 🙂",
            'price': f"Great question! For videos like yours I usually charge around {p['text'].replace(' per 10-20 min video', '')} per video, depending on length and how much "
                     f"editing it needs. Happy to do the first short sample free so you can see if it's a fit. Want me to send it over?",
            'no': "Totally understand, thanks for getting back to me! If anything changes down the line, feel free to reach out anytime.",
            'question': f"Thanks for getting back to me{', ' + n if n else ''}! "}
    try:
        prompt = (X.VOICE + "\nYou are replying, as Ritch (long-form video editor), to a creator who answered his cold email. Use Ritch's rules below. "
                  "Short (under 80 words), warm, human, no sign-off, no subject. If they said yes to the free 15-20 second sample, say you'll send it in a day or two. "
                  f"If they ask about price, a fair range for them is {p['text']} (only give it if they asked). If they decline, be gracious in one or two lines.\n"
                  "Ritch's rules:\n" + X._rules()[:6000] + f"\n\nTheir channel: {lead.get('title')} ({lead.get('niche')}). Their reply:\n<<<\n{reply}\n>>>\n"
                  'Reply ONLY with JSON: {"body": ""}')
        m = re.search(r'\{.*\}', X.ask_claude(prompt, 120), re.S)
        b = X.fix(json.loads(m.group(0))['body']).strip()
        if b: return {'body': b, 'kind': k, 'engine': 'claude'}
    except Exception: pass
    return {'body': tmpl[k], 'kind': k, 'engine': 'template'}


def reply_draft(lead):
    import sr_compose as X
    if not (lead.get('outreach') or {}).get('thread_id'): return {'error': 'No email thread for this lead yet'}
    r = their_last_reply(lead)
    if not r: return {'error': "Couldn't find their reply in the thread"}
    d = reply_text(lead, r)
    did = X.followup_draft(lead, 0, d['body'])
    return dict(d, draft_id=did, their_reply=r[:600])


# =====================================================================================================
# 9) SAMPLE EDIT KIT: the best 90 seconds of their video, downloaded and ready to edit
# =====================================================================================================
def _pick_moment(lead, dur):
    """seconds to start at: Claude picks from the transcript when it can, else ~35% in (past the intro, before the outro)"""
    import sr_compose as X
    tr = lead.get('transcript') or ''
    if tr and dur:
        try:
            prompt = (f"This is the transcript of a {int(dur // 60)}-minute YouTube video. A video editor wants to re-edit a 60-90 second stretch to show the creator "
                      "what his editing would add: pick the most engaging, self-contained moment (a story beat, a reveal, a strong explanation), not the intro or outro. "
                      "Estimate where it starts as a fraction of the video (0-1).\nTranscript:\n" + tr[:12000] + '\nReply ONLY with JSON: {"start_fraction": 0.4, "why": ""}')
            m = re.search(r'\{.*\}', X.ask_claude(prompt, 90, light=True), re.S); d = json.loads(m.group(0))
            f = min(0.85, max(0.08, float(d['start_fraction'])))
            return int(dur * f), d.get('why') or 'picked from the transcript'
        except Exception: pass
    return int(max(30, dur * 0.35)) if dur else 120, 'about a third in (past the intro)'


def sample_kit(lead, log=lambda m: None):
    import sr_compose as X, subprocess
    vid = (lead.get('latest_video') or {}).get('video_id')
    if not vid: return {'error': 'No latest video for this lead'}
    yd, ff = X._tool('yt-dlp'), X._tool('ffmpeg')
    if not yd or not ff: return {'error': 'yt-dlp / ffmpeg missing in C:\\ZXP\\bin'}
    try: X.lead_transcript(lead)
    except Exception: pass
    dur = 0
    try:
        p = subprocess.run([yd, '--no-warnings', '--print', 'duration', 'https://www.youtube.com/watch?v=' + vid], capture_output=True, text=True, timeout=60, creationflags=X.NOFLAGS)
        dur = float((p.stdout or '0').strip().splitlines()[0])
    except Exception: pass
    start, why = _pick_moment(lead, dur)
    end = start + 95
    safe = re.sub(r'[^\w\- ]', '', lead.get('title') or vid).strip()[:60] or vid
    d = os.path.join(os.path.expanduser('~'), 'Videos', 'SwipeReach Samples', safe); os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f'{safe} - {start // 60}m{start % 60:02d}s.mp4')
    log(f'downloading {start // 60}:{start % 60:02d}-{end // 60}:{end % 60:02d} of their latest video')
    p = subprocess.run([yd, '--no-warnings', '--no-playlist', '-f', 'bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b',
                        '--merge-output-format', 'mp4', '--download-sections', f'*{start}-{end}', '--force-keyframes-at-cuts', '--ffmpeg-location', ff,
                        '-o', out, '--force-overwrites', 'https://www.youtube.com/watch?v=' + vid], capture_output=True, text=True, timeout=600, creationflags=X.NOFLAGS)
    ok = os.path.exists(out) and os.path.getsize(out) > 100000
    with open(os.path.join(d, 'about this sample.txt'), 'w', encoding='utf-8') as f:
        f.write(f"{lead.get('title')}\nVideo: https://www.youtube.com/watch?v={vid}&t={start}s\nClip: {start // 60}:{start % 60:02d} to {end // 60}:{end % 60:02d} ({why})\n"
                f"Edit 15-20 seconds of this into your best work, then reply in the same Gmail thread.\n\nTranscript:\n{lead.get('transcript') or '(not available)'}\n")
    if not ok: return {'error': 'YouTube blocked the download for this video (' + ((p.stderr or '').strip().splitlines() or [''])[-1][:160] + '). The folder has the link and timestamp.', 'folder': d}
    return {'ok': True, 'folder': d, 'file': out, 'start': start, 'why': why}


# =====================================================================================================
# 17) SPAM CHECK for one email
# =====================================================================================================
SPAMMY = re.compile(r"(?i)\b(free money|guarantee(d)?|100% free|act now|limited time|click here|buy now|risk[- ]free|winner|cash bonus|no obligation|"
                    r"special promotion|best price|cheap|discount|earn \$|make money|increase (your )?(sales|revenue)|dear (sir|madam|friend)|urgent)\b")


def spam_check(subject, body, has_img=False):
    s, b = subject or '', re.sub(r'\[IMAGE\]', '', body or '')
    issues, pts = [], 0
    if SPAMMY.search(s + ' ' + b): issues.append('spammy words: "' + SPAMMY.search(s + ' ' + b).group(0) + '"'); pts += 3
    links = len(re.findall(r'https?://', b))
    if links >= 2: issues.append(f'{links} links (keep it to 0-1 in a first email)'); pts += 2
    elif links == 1: pts += 0.5
    if re.search(r'(bit\.ly|tinyurl|geni\.us|goo\.gl)', b): issues.append('link shortener (looks like spam)'); pts += 3
    if s.isupper() and len(s) > 4: issues.append('subject in ALL CAPS'); pts += 2
    if s.count('!') or b.count('!') > 3: issues.append('too many "!"'); pts += 1
    if re.search(r'\$\d|\d+% off', s + ' ' + b): issues.append('prices or % in a first email'); pts += 1
    w = len(b.split())
    if w > 160: issues.append(f'{w} words (cold emails under ~120 get more replies)'); pts += 1
    if has_img and links: issues.append('picture + link together: pick one'); pts += 1
    if not re.search(r'\b(hey|hi|hello)\b', b[:40], re.I): issues.append('no greeting'); pts += 0.5
    level = 'good' if pts < 1.5 else 'ok' if pts < 3.5 else 'risky'
    return {'level': level, 'issues': issues, 'score': round(pts, 1)}


# =====================================================================================================
# 16) SEND AT THEIR MORNING
# =====================================================================================================
TZ = {'US': -5, 'CA': -5, 'MX': -6, 'BR': -3, 'AR': -3, 'GB': 0, 'UK': 0, 'IE': 0, 'PT': 0, 'ES': 1, 'FR': 1, 'DE': 1, 'NL': 1, 'BE': 1, 'IT': 1, 'CH': 1, 'AT': 1,
      'SE': 1, 'NO': 1, 'DK': 1, 'PL': 1, 'CZ': 1, 'FI': 2, 'GR': 2, 'RO': 2, 'ZA': 2, 'IL': 2, 'TR': 3, 'AE': 4, 'IN': 5.5, 'PK': 5, 'BD': 6, 'TH': 7, 'VN': 7,
      'ID': 7, 'MY': 8, 'SG': 8, 'PH': 8, 'HK': 8, 'CN': 8, 'TW': 8, 'JP': 9, 'KR': 9, 'AU': 10, 'NZ': 12}


def their_morning(lead, hour=9):
    off = TZ.get(str(lead.get('country') or '').upper(), -5)   # unknown: most of your leads are North American
    now = datetime.now(timezone.utc); local = now + timedelta(hours=off)
    t = local.replace(hour=hour, minute=random.randint(0, 40), second=0, microsecond=0)
    if t <= local + timedelta(minutes=10): t += timedelta(days=1)
    if t.weekday() >= 5: t += timedelta(days=7 - t.weekday())   # weekends: Monday morning
    return (t - timedelta(hours=off)).astimezone(timezone.utc)


def queue():
    return list(C.load_settings().get('send_queue') or [])


def schedule(cid, draft_id, at):
    S = C.load_settings(); q = [x for x in (S.get('send_queue') or []) if x['cid'] != cid]
    q.append({'cid': cid, 'draft_id': draft_id, 'at': at.isoformat()}); S['send_queue'] = q; C.save_settings(S)


def unschedule(cid):
    S = C.load_settings(); S['send_queue'] = [x for x in (S.get('send_queue') or []) if x['cid'] != cid]; C.save_settings(S)


def send_due():
    """called every 30 s by the app: sends drafts whose time has come (only while the app is open)"""
    import sr_compose as X
    S = C.load_settings(); q = S.get('send_queue') or []; now = datetime.now(timezone.utc); sent, keep, errs = [], [], []
    for x in q:
        if _dt(x['at']) and _dt(x['at']) <= now:
            try:
                X.gmail().users().drafts().send(userId='me', body={'id': x['draft_id']}).execute(); sent.append(x['cid'])
            except Exception as e:
                if 'notFound' in str(e) or '404' in str(e): sent.append(x['cid'])   # you sent or deleted it yourself
                else: errs.append(str(e)[:200]); keep.append(x)
        else: keep.append(x)
    if len(keep) != len(q): S = C.load_settings(); S['send_queue'] = keep; C.save_settings(S)
    return {'sent': sent, 'errors': errs}


# =====================================================================================================
# 5, 14) WHAT WORKS: reply rates by niche / size / country / style / subject
# =====================================================================================================
def _size(s):
    s = int(s or 0)
    return '<10k' if s < 10000 else '10-50k' if s < 50000 else '50-150k' if s < 150000 else '150k+'


def insights(min_n=3):
    sent = [l for l in C.leads_ro() if (l.get('outreach') or {}).get('sent_at')]
    def table(key):
        g = {}
        for l in sent:
            k = key(l) or '?'; a = g.setdefault(k, [0, 0]); a[0] += 1; a[1] += l.get('status') in REPLIED
        rows = [{'k': k, 'sent': a[0], 'replied': a[1], 'rate': round(100 * a[1] / a[0])} for k, a in g.items() if a[0] >= min_n]
        return sorted(rows, key=lambda r: (-r['rate'], -r['sent']))
    return {'sent': len(sent), 'replied': sum(1 for l in sent if l.get('status') in REPLIED),
            'niche': table(lambda l: l.get('niche')), 'size': table(lambda l: _size(l.get('subs'))), 'country': table(lambda l: (l.get('country') or '?').upper()),
            'style': table(lambda l: (l.get('outreach') or {}).get('style')), 'subject': table(lambda l: ((l.get('outreach') or {}).get('subject') or '').lower()[:60]),
            'type': table(lambda l: 'Creator' if l.get('mode') == 'creators' else 'Business')}


def niche_boost():
    """+ points for niches that reply more than your average, - for ones that never do (needs a few sends)"""
    ins = insights(min_n=4); avg = (ins['replied'] / ins['sent']) if ins['sent'] else 0
    return {r['k']: max(-10, min(15, (r['rate'] / 100 - avg) * 60)) for r in ins['niche']} if ins['sent'] >= 15 else {}


def winning_subjects():
    return [r['k'] for r in insights(min_n=2)['subject'] if r['replied'] >= 1][:5]


# =====================================================================================================
# 3) TODAY'S LIST
# =====================================================================================================
def today(n=None):
    w = warmup(); goal = int(((C.load_settings().get('ui') or {}).get('goal')) or 30)
    n = n or (w['left'] if w.get('on') else max(0, goal - w['sent_today'])) or 0
    boost = niche_boost(); picks = []
    for l in C.leads_ro():
        if l.get('status', 'new') != 'new' or not l.get('email') or (l.get('email_check') or {}).get('ok') is False: continue
        if l.get('tier') not in ('HIGH', 'MEDIUM'): continue
        r = ' '.join(l.get('reasons') or []); sig = [s[0] for s in (l.get('signals') or [])]
        pts = float(l.get('score') or 0); why = []
        if 'editor_left' in sig or 'not in the latest videos' in r: pts += 25; why.append('their editor seems to have left')
        if 'slowing' in sig or 'may be overwhelmed' in r: pts += 10; why.append('uploads slowing down')
        if (l.get('growth_x') or 0) >= 1.6: pts += 8; why.append(f"recent video at {l['growth_x']}x usual views")
        if 'pays_editor' in sig or 'credits an editor' in r: pts += 6; why.append('already pays an editor')
        if (l.get('email_check') or {}).get('ok') is True: pts += 3
        if is_agency(l.get('email')): pts -= 12; why.append('agency email (may not reach them)')
        b = boost.get(l.get('niche'))
        if b:
            pts += b
            if abs(b) >= 3: why.append(f"{l.get('niche')} replies {'more' if b > 0 else 'less'} than average")
        lv = _dt((l.get('latest_video') or {}).get('published_raw') or (l.get('latest_video') or {}).get('published'))
        if lv and (datetime.now(timezone.utc) - lv).days <= 7: pts += 4; why.append('posted this week')
        picks.append((pts, l['channel_id'], why))
    picks.sort(key=lambda x: -x[0])
    return {'ids': [p[1] for p in picks[:max(0, n)]], 'why': {p[1]: p[2] for p in picks[:max(0, n)]}, 'limit': n, 'warmup': w}


# =====================================================================================================
# 13) DM BACKUP when email gets no answer
# =====================================================================================================
def dm_backups():
    out = []
    for l in C.leads_ro():
        o = l.get('outreach') or {}
        if not (l.get('status') == 'no_answer' or (l.get('status') in ('contacted', 'followup') and len(o.get('followups') or []) >= 3)): continue
        if o.get('dm_tried'): continue
        txt = ' '.join(str(l.get(k) or '') for k in ('contacts', 'about'))
        links = []
        for rx, name in ((r'https?://(?:www\.)?(?:x|twitter)\.com/\w+', 'X'), (r'https?://(?:www\.)?instagram\.com/[\w.]+', 'Instagram'),
                         (r'https?://(?:www\.)?tiktok\.com/@[\w.]+', 'TikTok'), (r'https?://(?:www\.)?discord\.(?:gg|com/invite)/\w+', 'Discord')):
            m = re.search(rx, txt)
            if m: links.append({'where': name, 'url': m.group(0)})
        if not links: continue
        n = l.get('first_name') or ''
        out.append({'cid': l['channel_id'], 'title': l.get('title'), 'links': links,
                    'text': f"Hey{(' ' + n) if n else ''}! Sent you an email a little while back about your videos, not sure if it reached you. "
                            "I edit long-form for another creator and had an idea for a quick 15-20 sec edit of one of yours. Mind if I send it over here?"})
    return out


def dm_done(cid):
    def f(L):
        for x in L:
            if x['channel_id'] == cid: x.setdefault('outreach', {})['dm_tried'] = _today().isoformat()
    C.mutate(f)


# =====================================================================================================
# 20, 21) SAMPLES + TESTIMONIALS for the email
# =====================================================================================================
def _lines(key):
    out = []
    for ln in (C.load_settings().get(key) or '').splitlines():
        parts = [p.strip() for p in ln.split('|')]
        if len(parts) >= 2 and parts[1]: out.append(parts)
    return out


def _match(rows, lead):
    t = ' '.join(str(lead.get(k) or '') for k in ('niche', 'title', 'about')).lower()
    best = None
    for r in rows:
        words = [w for w in re.split(r'[,\s]+', r[0].lower()) if w]
        hit = sum(1 for w in words if w in t)
        if hit and (not best or hit > best[0]): best = (hit, r)
    return best[1] if best else (rows[0] if rows else None)


def sample_for(lead):
    r = _match(_lines('samples'), lead)
    return {'line': f"Here's a recent edit of mine in a similar style: {r[1]}", 'url': r[1], 'label': r[0]} if r else None


def proof_for(lead):
    r = _match(_lines('testimonials'), lead)
    if not r: return None
    who = f' ({r[2]})' if len(r) > 2 and r[2] else ''
    return {'line': f'One of the creators I edit for said: "{r[1]}"{who}'}


def testimonial_due():
    """clients you've worked with for 2+ weeks and haven't asked for a testimonial yet"""
    out = []
    for l in C.leads_ro():
        if l.get('status') not in ('client', 'won') or l.get('testimonial_asked'): continue
        h = [x for x in (l.get('history') or []) if x.get('status') in ('client', 'won')]
        t = _dt(h[-1]['t']) if h else None
        if t and (datetime.now(timezone.utc) - t).days >= 14: out.append({'cid': l['channel_id'], 'title': l.get('title')})
    return out


# =====================================================================================================
# 22) CLIENT CARE
# =====================================================================================================
def client_care():
    out = []
    for l in C.leads_ro():
        if l.get('status') not in ('client', 'won'): continue
        lv = l.get('latest_video') or {}; r = ' '.join(l.get('reasons') or [])
        ideas = []
        if 'mostly Shorts' not in r: ideas.append('Shorts cut from each long video (3-5 per video)')
        if re.search(r'thumbnail', r, re.I) is None: ideas.append('thumbnails, if they make their own')
        m = re.search(r'([\d.]+) long videos/week', r)
        if m and float(m.group(1)) >= 1.5: ideas.append(f'a monthly package ({m.group(1)} videos/week)')
        out.append({'cid': l['channel_id'], 'title': l.get('title'), 'last_upload': lv.get('published') or '', 'ideas': ideas})
    return out


# =====================================================================================================
# 23) WEEKLY REPORT
# =====================================================================================================
def weekly(days=7):
    import sr_brain as B
    since = datetime.now(timezone.utc) - timedelta(days=days); L = C.leads_ro()
    ins = lambda s: (_dt(s) or datetime(1970, 1, 1, tzinfo=timezone.utc)) >= since
    sent = sum(1 for l in L if ins((l.get('outreach') or {}).get('sent_at')))
    fus = sum(1 for l in L for t in ((l.get('outreach') or {}).get('followups') or []) if ins(t))
    rep = [l for l in L if any(h.get('status') in REPLIED and ins(h.get('t')) for h in (l.get('history') or []))]
    added = sum(1 for l in L if ins(l.get('added')))
    niches = {}
    for l in rep: niches[l.get('niche') or '?'] = niches.get(l.get('niche') or '?', 0) + 1
    try: top = [x['q'] for x in B.summary()['top'][:3]]
    except Exception: top = []
    return {'sent': sent, 'followups': fus, 'replies': len(rep), 'reply_names': [l.get('title') for l in rep][:8], 'new_leads': added,
            'rate': round(100 * len(rep) / sent) if sent else 0, 'best_niches': sorted(niches, key=lambda k: -niches[k])[:3], 'best_searches': top}
