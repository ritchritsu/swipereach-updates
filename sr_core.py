"""SwipeReach Studio - lead finding engine.
Finds YouTube channels that (1) likely need an editor and (2) can likely pay, in three modes:
  business  - people who run a business and teach/sell on YouTube (golf, gardening, guitar lessons...)
  creators  - long-form creators (documentary, educational, vlogs, gaming, commentary, storytelling)
  growing   - channels whose recent videos are clearly beating their usual views (the algorithm is pushing them)
Reuses LeadScraper's proven YouTube code (API keys, quota rotation, caching, email extraction)."""
import os, time, sys, re, json, time, threading, shutil, statistics, importlib.util
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.expanduser('~'), '.swipereach')
os.makedirs(DATA, exist_ok=True)
LEADS_FILE = os.path.join(DATA, 'leads.json')
SETTINGS_FILE = os.path.join(DATA, 'settings.json')

# ---------- LeadScraper (YouTube client, keys, quota, cache) ----------
_LS = None
def ls():
    global _LS
    if _LS is None:
        cands = [os.environ.get('SR_LEADSCRAPER') or '', os.path.join(HERE, 'LeadScraper', 'LeadScraper.py'), os.path.join(os.path.dirname(HERE), 'LeadScraper', 'LeadScraper.py'),
                 os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] or '.')), 'LeadScraper', 'LeadScraper.py'), os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0] or '.'))), 'LeadScraper', 'LeadScraper.py')]
        p = next((c for c in cands if c and os.path.exists(c)), cands[2])   # works from the source folder, the installed .exe and a downloaded update
        if 'LeadScraper' in sys.modules and hasattr(sys.modules['LeadScraper'], '_key_has_headroom'):
            _LS = sys.modules['LeadScraper']
        else:
            spec = importlib.util.spec_from_file_location('LeadScraper', p); m = importlib.util.module_from_spec(spec)
            sys.modules['LeadScraper'] = m
            try: spec.loader.exec_module(m)
            except BaseException as e: sys.modules.pop('LeadScraper', None); raise RuntimeError('YouTube engine failed to load (build 4): ' + repr(e)[:300])   # show the real cause, never a half-loaded module
            if not hasattr(m, '_key_has_headroom'):   # loaded but incomplete: say exactly what was loaded
                sys.modules.pop('LeadScraper', None)
                raise RuntimeError('YouTube engine incomplete (build 4): ' + str(getattr(m, '__file__', p)) + ', ' + str(len(dir(m))) + ' names')
            _LS = m
        try: _LS.load_settings()
        except Exception: pass
        try:   # old app throttles to 5 calls/sec; a 1M-quota key handles far more. Only this app's process, old app untouched.
            _LS.SETTINGS['api_max_qps'] = int(load_settings().get('api_qps_safe', 60))   # fast, but under Google's per-minute limit
            _LS._rate_limiter = None
        except Exception: pass
    _fix_reset_clock(_LS)
    _LS._on_rate_limit = _on_rate_limit
    _only_primary(_LS)
    _quota_rollover(_LS)
    return _LS


def _only_primary(L):
    """Use only the Primary (1M quota) key; the small backup keys are never touched (settings 'only_primary_key': false to undo)."""
    if not load_settings().get('only_primary_key', True): return
    if not getattr(L, '_sr_orig_headroom', None): L._sr_orig_headroom = L._key_has_headroom
    orig = L._sr_orig_headroom
    L._key_has_headroom = lambda idx, is_search_call: idx == 0 and orig(idx, is_search_call)
    L.SETTINGS['active_key_index'] = 0


def _fix_reset_clock(L):
    """The old engine works out 'minutes until midnight Pacific' with today's clock offset, so on a day before the US changes
    its clocks it is an hour out (said 3 PM, real reset 4 PM). Use the offset that applies at midnight itself."""
    if getattr(L, '_sr_reset_fixed', False): return
    try:
        def left():
            now = datetime.now(timezone.utc)
            pn = now + L._pacific_offset_for_utc(now)
            mid = (pn + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            utc = mid - L._pacific_offset_for_utc(mid + timedelta(hours=8))
            utc = mid - L._pacific_offset_for_utc(utc)
            return max(0, int((utc - now).total_seconds() / 60))
        L._quota_reset_minutes_left = left; L._sr_reset_fixed = True
    except Exception: pass

def _quota_rollover(L):
    """YouTube resets everyone's quota at midnight Pacific (3-4 pm in Manila). The old engine only reads the day's count when it
    starts, so an app left open past midnight kept yesterday's numbers (and even saved them as today's on close). Reset in place."""
    try:
        day = L._quota_today()
        if getattr(L, '_sr_day', None) is None:
            L._sr_day = day; return
        if day != L._sr_day:
            L._sr_day = day
            with L.quota_file_lock if hasattr(L, 'quota_file_lock') else threading.Lock():
                L.API_QUOTA_USED = 0
                for arr in (L.key_quota_used, L.search_calls_used):
                    for i in range(len(arr)): arr[i] = 0
            try: L._quota_last_save_value = 0
            except Exception: pass
            L.save_daily_quota(0, L.key_quota_used, L.search_calls_used, force=True)
    except Exception: pass

# ---------- niches ----------
from sr_niches import NICHES   # 85 niches / 540+ searches incl. all old SwipeReach keyword lists
VARIANTS = ['{n} tips', '{n} for beginners', '{n} mistakes to avoid', '{n} instructor explains']
_PROBE_T = [0.0]
_SPACE_LK = threading.Lock(); _SPACE_T = [0.0]; SEARCH_GAP = float(os.environ.get('SR_SEARCH_GAP', 0.25))
_GAP = [SEARCH_GAP]   # adaptive: starts fast, slows down the moment Google says 'too fast', speeds back up slowly
def _on_rate_limit():
    _GAP[0] = min(3.0, max(0.5, _GAP[0] * 2))
def _search_ok():
    _GAP[0] = max(SEARCH_GAP, _GAP[0] * 0.97)
def probe_keys(force=False):
    """keys the app has marked as used up: ask YouTube whether they work again (1 quota unit each).
    Google's real reset time doesn't always match the clock (e.g. the day the US changes its clocks), and without this
    a key marked 'used up' stays unused for the rest of the day even after YouTube has reset it."""
    L = ls()
    if not force and time.time() - _PROBE_T[0] < 900: return 0
    _PROBE_T[0] = time.time(); n = 0
    for i in range(len(L.API_KEYS)):
        try:
            if i and load_settings().get('only_primary_key', True): continue
            if L.key_quota_used[i] < L.get_key_quota(i) and L.search_calls_used[i] < L.get_search_cap(i): continue
            yt = L.get_youtube_service(i)
            if L.search_calls_used[i] >= L.get_search_cap(i):   # Google has a separate daily SEARCH limit: test it with a real search (100 units)
                yt.search().list(part='id', q='news', type='channel', maxResults=1).execute()
            else: yt.i18nRegions().list(part='snippet', hl='en_US').execute()
        except Exception: continue
        L.key_quota_used[i] = 1; L.search_calls_used[i] = 0; n += 1
    if n:
        try:
            L.API_QUOTA_USED = sum(L.key_quota_used); L._quota_last_save_value = 0
            L.save_daily_quota(L.API_QUOTA_USED, L.key_quota_used, L.search_calls_used, force=True)
        except Exception: pass
    return n

def _flat(m):
    out = {}
    for g, ns in NICHES[m].items():
        for n, q in ns.items():
            q = list(q)
            # teaching niches: add proven "how-to" phrasings coaches title their videos with (more owners per niche)
            if m == 'business' and '&' not in n and not g.startswith(('Online coaches', 'Small business', 'Experts who teach', 'Money &')) and len(n.split()) <= 2:
                have = {x.lower() for x in q}
                q += [v.format(n=n) for v in VARIANTS if v.format(n=n).lower() not in have]
            out[n] = q
    return out
BUSINESS_NICHES = _flat('business'); CREATOR_NICHES = _flat('creators')
ALL_NICHES = dict(CREATOR_NICHES, **BUSINESS_NICHES)

# Ritch's real work: strategy gaming, educational, talking head, documentary, vlogs -> which proof fits each niche
PROOF_RULES = [('strategy gaming', r'strategy|total war|grand strategy|civilization'), ('gaming', r'gaming|game'),
               ('documentary', r'documentary|essay|history|crime|mystery|mythology|military|film|tv analysis|geopolitics|story'),
               ('vlogs', r'vlog|irl|travel|lifestyle|van|family|outdoor|adventure|food & cooking shows'),
               ('talking head', r'talking head|commentary|opinion|storytime|podcast|career|advice|self improvement'),
               ('educational', r'educat|explained|learning|science|psychology|philosophy|cars|sports analysis|books|business explained|geography|coding|tech')]
def proof_for(niche, mode):
    n = (niche or '').lower()
    for label, rx in PROOF_RULES:
        if re.search(rx, n): return 'strategy gaming' if label == 'gaming' else label
    return 'talking head + educational' if mode != 'creators' else ''

_OSF = {}
def _fast_list(name):
    """the old detectors, made ~3x faster with the same answers: the text is lowercased once (instead of case-insensitive
    matching 175 times), and a pattern that starts with a plain word is skipped unless that word is in the text at all"""
    if name in _OSF: return _OSF[name]
    def low(p):
        out, i = [], 0
        while i < len(p):
            if p[i] == '\\' and i + 1 < len(p): out.append(p[i:i + 2]); i += 2; continue
            out.append(p[i].lower()); i += 1
        return ''.join(out)
    def alt(p):
        d, i = 0, 0
        while i < len(p):
            c = p[i]
            if c == '\\': i += 2; continue
            if c == '[':
                j = p.find(']', i + 2); i = j + 1 if j > 0 else len(p); continue
            if c == '(': d += 1
            elif c == ')': d -= 1
            elif c == '|' and d == 0: return True
            i += 1
        return False
    def lit(p):
        if alt(p): return ''
        q = p
        while q.startswith('\\b') or q.startswith('^'): q = q[2:] if q.startswith('\\b') else q[1:]
        m = re.match(r"[a-z0-9 '@/:-]+", q)
        if not m: return ''
        w = m.group(0)
        if q[len(w):len(w) + 1] in ('?', '*', '{'): w = w[:-1]
        return w if len(w) >= 3 else ''
    out = []
    for x in getattr(_LS, name, []) or []:
        rx, lab = (x if isinstance(x, tuple) else (x, None))
        try: lp = low(rx.pattern); out.append((re.compile(lp, rx.flags & ~re.IGNORECASE), lit(lp), lab))
        except Exception: out.append((rx, '', lab))   # keep the original if it can't be converted
    _OSF[name] = out
    return out

def old_signals(text):
    """old SwipeReach's money/seller detectors (Patreon, Kajabi, 'book a call', 'I help', '6-figure'...)."""
    L = _LS; hits = []
    if not L or not text: return 0, hits
    try:
        lo = text.lower()
        m = lambda name: [lab for rx, w, lab in _fast_list(name) if (not w or w in lo) and rx.search(lo if not (rx.flags & re.IGNORECASE) else text)]
        for lab in m('MONETIZATION_PATTERNS'):
            if lab not in hits: hits.append(lab)
        seller = len(m('SELLER_INTENT_PHRASES'))
        strong = m('INCOME_SIGNALS_STRONG'); mod = m('INCOME_SIGNALS_MODERATE')
        pts = min(12, 3 * len([h for h in hits if h not in ('Linktree', 'Beacons')])) + min(8, 4 * seller) + min(10, 5 * len(strong)) + min(6, 2 * len(mod))
        if seller: hits.append('seller language')
        hits += strong[:1] + mod[:1]
        bad = bool(m('_EXCLUDE_KW_RES'))
        return (-999 if bad else pts), hits
    except Exception: return 0, hits

# technical / finance / legal people Ritch doesn't want to edit for
TECH_RE = re.compile(r"(?i)\b(real ?estate|realtor|mortgage|accountant|accounting|bookkeep\w*|cpa|tax(es)? (tips|advice|strateg\w+)|lawyer|attorney|law firm|legal advice|insurance (agent|broker)|day trad\w+|forex|crypto\w*|stock market|options trading)\b")

def guess_name(title, desc='', email='', vdesc=''):
    """best-effort owner first name without AI. returns (name, confidence 0-3, source)"""
    STOP = {'the','coach','golf','your','my','our','this','here','welcome','hi','hey','so','just','a','an','channel','official','tv','pro','pga','dr','mr','mrs','ms',
            'simple','world','class','academy','school','lessons','music','guitar','piano','tennis','studio','team','garden','gardening','fitness','home','best','real',
            'chef','kitchen','life','daily','vlog','show','podcast','media','productions','films','club','shop','store','company','co','inc','ltd','new','free','learn'}
    cand = {}
    def add(n, pts, src):
        if not n: return
        n = n.strip(".,!:;'\"").capitalize()
        if len(n) < 2 or n.lower() in STOP or not n.isalpha(): return
        c = cand.setdefault(n, [0, src]); c[0] += pts
    tw = re.findall(r"[A-Za-z]+", title or '')
    for m in re.finditer(r"(?:\bI'?m|\bI am|\bmy name is|\bname'?s|\bthis is|\bit'?s)\s+(?:coach |dr\.? |chef |pga pro |pro )?([A-Z][a-z]{1,14})\b", (desc or '') + '\n' + (vdesc or '')[:4000]):
        add(m.group(1), 3, 'says "I\'m ' + m.group(1) + '"')
    for m in re.finditer(r"(?:^|\n)\s*(?:-|—|cheers,?|thanks,?|best,?|love,?)\s*([A-Z][a-z]{2,14})\s*(?:\n|$)", desc or ''):
        add(m.group(1), 1, 'signs off as ' + m.group(1))
    m = re.search(r"\b(?:coach|dr\.?|chef)\s+([A-Z][a-z]{2,14})\b", title or '')
    if m: add(m.group(1), 2, 'channel name')
    for i in range(len(tw) - 1):   # "First Last" pairs anywhere in the title, e.g. "WorldClassGolf - Craig Hanson & Dr. Rob Neal"
        a, b = tw[i], tw[i + 1]
        if a[:1].isupper() and b[:1].isupper() and a[1:].islower() and b[1:].islower() and len(a) > 2 and a.lower() not in STOP and b.lower() not in STOP:
            rest = tw[i + 2:]
            add(a, 1, 'channel name')   # a channel name alone is a hint, never enough ("Armchair Analysis")
    loc = (email or '').split('@')[0].lower()
    if loc and not re.match(r'(info|contact|hello|hi|support|team|admin|business|booking|bookings|enquiries|inquiries|mail|office|sales|collab|partnerships?|media|press)\b', loc):
        parts = [p for p in re.split(r'[._\-\d]+', loc) if len(p) > 1]
        for n in list(cand):
            if any(p == n.lower() or p.startswith(n.lower()) for p in parts): cand[n][0] += 3; cand[n][1] += ' + email'
        if parts and not cand and parts[0].isalpha() and 2 < len(parts[0]) < 12 and len(parts) > 1: add(parts[0], 1, 'email')
    if not cand: return '', 0, ''
    n, (pts, src) = max(cand.items(), key=lambda kv: kv[1][0])
    return n, min(3, pts), src

# master prompt hard exclusions (AI content, ad mills) - matched in title/description/keywords
EXCLUDE_RE = re.compile(r"(?i)\b(ai[- ]generated|faceless (?:channel|youtube)|ai (?:tools?|avatar|ugc|automation)|chatgpt prompts|ugc ads?|facebook ads?|dropshipping|make money with ai|crypto signals?|forex signals?)\b")
PAY_WORDS = re.compile(r"(?i)\b(online courses?|my course|coaching|1[:-]on[:-]1|private lessons?|book (?:a|your) (?:call|lesson|session|class)|lessons? (?:available|start)|academy|masterclass|membership|patreon|consultation|my (?:shop|store|studio|clinic|gym|school|business|company|bakery|salon|farm)|workshops?|classes|enroll(?:ment)?|programs? (?:is|are) open|join (?:my|the) (?:program|community))\b")
SPONSOR_WORDS = re.compile(r"(?i)(sponsored by|this video is sponsored|thanks to [^.\n]{1,40} for sponsoring|today's sponsor|in partnership with|use (?:my )?code \w+|paid partnership)")
AFFILIATE_WORDS = re.compile(r"(?i)(affiliate link|amazon associate|as an amazon|commission|% off|kit\.co|geni\.us|amzn\.to)")
OWNER_WORDS = re.compile(r"(?i)\b(owner|founder|ceo|instructor|teacher|coach|pro shop|pga|certified|licensed|we help|i help|our team)\b")
SOCIAL = re.compile(r"(?i)(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|twitter\.com|x\.com|facebook\.com|fb\.com|linktr\.ee|discord\.gg|twitch\.tv|patreon\.com|amzn\.to|amazon\.)")
URL_RE = re.compile(r"https?://[^\s)>\]]+")

PLATFORM_EMAIL = re.compile(r"(?i)@(?:[\w-]+\.)*(patreon|skool|streamlabs|theguardian|nytimes|domain|example|email|youtube|google|apple|amazon|kajabi|teachable|gumroad|stan|linktree|beacons|substack|squarespace|wix|shopify|paypal|stripe|discord|twitch|tiktok|instagram|facebook|twitter|x|spotify|epidemicsound|artlist|nordvpn|skillshare|audible|bbc|cnn)\.(?:com|co|org|net|gg|io|store|tv|me|live)\b")
WEB_JUNK_EMAIL = re.compile(r"(?i)@(?:[\w-]+\.)*(w3|schema|creativecommons|wordpress|wixpress|sentry|sentry-next|godaddy|cloudflare|gstatic|jquery|github|mozilla|"
                            r"mailchimp|sendgrid|hubspot|zendesk|intercom|automattic|gravatar|jsdelivr|unpkg|fontawesome|adobe|microsoft|w3schools|cookiebot|onetrust)\.(?:com|org|net|io|co)$"
                            r"|^(privacy|legal|abuse|dmca|copyright|webmaster|postmaster|hostmaster|security|gdpr|unsubscribe|bounce|mailer-daemon|no-?reply|donotreply|giving|public-[\w-]+)@")
PLACEHOLDER = re.compile(r"(?i)^(you|your|name|yourname|email|user|username|someone|test|sample|first\.?last|john\.?doe|abc|xyz)@")
def clean_email(e):
    """strip junk; reject platform/sponsor/placeholder addresses that aren't the creator's"""
    e = re.sub(r'(?i)^(u003e|u003c|>|<|mailto:)+', '', str(e or '').strip()).strip('.,;:)(<>"\'')
    if not re.fullmatch(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', e): return ''
    if PLATFORM_EMAIL.search(e) or PLACEHOLDER.search(e) or WEB_JUNK_EMAIL.search(e): return ''
    return e

NOT_SITE = re.compile(r"(?i)(bit\.ly|geni\.us|tinyurl|amzn|kit\.co|linktr|beacons\.ai|shareasale|rstyle|go\.|click\.|ref=|affiliate|awin|impact\.com|skimlinks|lnk\.to|spoti\.fi|spotify|apple\.com|google\.com|goo\.gl|forms\.|calendly|paypal|ko-fi|buymeacoffee|gofundme|discord|reddit|t\.me|wa\.me|whatsapp|threads\.net|pinterest|snapchat|soundcloud|bandcamp|steam|epicgames|imdb|wikipedia|medium\.com|substack|onlyfans)")
PLATFORM_SITE = re.compile(r"(?i)(apple\.com|apps\.apple|play\.google|github\.com|linkedin\.com|letterboxd|boxd\.it|goodreads|stan\.store|skool\.com|buymeacoffee|onelink\.me|oia\.bio|shorturl|tinyurl|bit\.ly|link\.amazon|amazon\.|nebula\.tv|patreon|gumroad|teespring|spring\.com|merch|kick\.com|streamlabs|journals\.|plos\.org|wikipedia|\.gov\b|maps\.app|forms\.gle|typeform|milanote|libsyn|anchor\.fm|podcasts\.apple|open\.spotify|soundcloud|bandcamp|steam|epicgames|gog\.com|hoyo\.link|sbx\.gg|gamersupps|incogni|drinktrade|nordvpn|surfshark|squarespace\.com|hellofresh|raycon|manscaped|ground\.news|brilliant\.org|audible|skillshare|bbcstudios|facebook\.|teer\.id|trakteer)")
def _tok(x): return {t for t in re.findall(r'[a-z0-9]{3,}', (x or '').lower()) if t not in {'the', 'and', 'with', 'official', 'channel', 'tv', 'com', 'www', 'http', 'https'}}

def own_site(desc, vids, title='', email=''):
    """their OWN website: a link whose domain matches their name/handle/email, or one they introduce as their site/courses/shop.
    Sponsors, affiliate links, app stores and platforms are ignored."""
    dom = lambda u: re.sub(r'^https?://(www\.)?', '', u).split('/')[0].lower()
    ok = lambda u: not SOCIAL.search(u) and not NOT_SITE.search(u) and not PLATFORM_SITE.search(u)
    name = _tok(title) | _tok((email or '').split('@')[0]); edom = (email or '').split('@')[-1].lower() if '@' in (email or '') else ''
    links = [u.rstrip('.,)') for u in URL_RE.findall(desc) if ok(u)]
    for u in links:   # 1) domain matches their name or their email domain
        d = dom(u); dt = _tok(d.replace('-', ' ').replace('.', ' '))
        if (edom and d.endswith(edom)) or (name and any(n in d for n in name if len(n) >= 4)) or (name & dt): return u.split('?')[0]
    for m in re.finditer(r'(?i)(website|site|courses?|coaching|lessons?|book|shop|store|academy|program|training|membership|join)[^\n]{0,40}?(https?://[^\s)>\]]+)', desc):
        u = m.group(2).rstrip('.,)')   # 2) a link they introduce as their own thing
        if ok(u): return u.split('?')[0]
    return ''

def _old_own_site(desc, vids):
    """their own website: a link in the channel About, or one that repeats across their videos (not affiliate/shorteners/socials)"""
    dom = lambda u: re.sub(r'^https?://(www\.)?', '', u).split('/')[0].lower()
    ok = lambda u: not SOCIAL.search(u) and not NOT_SITE.search(u)
    a = [u.rstrip('.,') for u in URL_RE.findall(desc) if ok(u)]
    if a: return a[0]
    cnt = {}
    for v in vids[:10]:
        for d in {dom(u) for u in URL_RE.findall(v.get('description') or '') if ok(u)}: cnt[d] = cnt.get(d, 0) + 1
    best = max(cnt.items(), key=lambda kv: kv[1], default=(None, 0))
    return 'https://' + best[0] if best[1] >= 3 else ''

def site_email(url, timeout=6):
    """looks for a contact email on their own website (home, /contact, /about). '' if none. Business owners almost always list one."""
    import urllib.request, html as H
    if not url or not url.startswith('http'): return ''
    base = re.match(r'https?://[^/]+', url).group(0); dom = base.split('//')[1].replace('www.', '').lower()
    try: black = [b.lower() for b in ls().EMAIL_BLACKLIST]
    except Exception: black = []
    found = []
    for path in ('', '/contact', '/contact-us', '/about', '/pages/contact'):
        try:
            req = urllib.request.Request(base + path, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'})
            page = H.unescape(urllib.request.urlopen(req, timeout=timeout).read(600000).decode('utf-8', 'ignore'))
        except Exception: continue
        page = re.sub(r'\s*\[\s*at\s*\]\s*|\s+\(at\)\s+', '@', page, flags=re.I)
        for e in re.findall(r'(?:mailto:)?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', page):
            e = e.strip('.').lower()
            if re.search(r'\.(png|jpe?g|gif|webp|svg)$', e) or any(b in e for b in black) or 'example.' in e or 'sentry' in e or 'wixpress' in e: continue
            if e not in found: found.append(e)
        if any(dom.split('.')[0] in e for e in found): break
    if not found: return ''
    return sorted(found, key=lambda e: (dom.split('.')[0] not in e, e.startswith(('noreply', 'no-reply')), len(e)))[0]

TEAM_ROLES = re.compile(r"(?im)^[^\n]{0,25}?\b(edited|editor|editing|thumbnails?|written|writer|script(?:ed|writer)?|produced|producer|animation|animated|animator|research(?:ed|er)?|motion graphics|sound design|audio|mix(?:ed|ing)|camera|filmed|cinematograph\w*|colou?r(?:ed|ist)?|channel manager|graphics|illustrat\w+|narrat\w+)\b[^\n]{0,6}?(?:by|:|-)\s*\S")
COMPANY = re.compile(r"(?i)\b(media|studios?|network|productions?|official|news|magazine|inc\.?|llc|ltd|gmbh|by the|the athletic|podcasts?\b network|entertainment)\b|\b(our team|we are a team|our writers|our editors|join our team|we're hiring|careers)\b")
def team_signals(info, vids):
    """how many different crew roles they credit + company-run signs -> already has a team?"""
    roles = set()
    for v in vids[:10]:
        for m in TEAM_ROLES.finditer(v.get('description') or ''):
            r = m.group(1).lower()
            r = 'editor' if r.startswith('edit') else 'thumbnails' if r.startswith('thumb') else 'writer' if r.startswith(('writ', 'script')) else 'producer' if r.startswith('produc') else 'animation' if r.startswith('anim') else r
            roles.add(r)
    company = bool(COMPANY.search((info.get('title') or '') + ' ' + (info.get('description') or '')[:800]))
    return roles, company

ORG_RE = re.compile(r"(?i)(&\s*company|\bcompany\b|\buniversity\b|\bcollege\b|\binstitute\b|\bmedicine\b|\bhospital\b|\bbbc\b|\bcnn\b|\bnbc\b|\bpbs\b|\bnews\b|\bfoundation\b|\bministry\b|\bgovernment\b|physics ?wallah|\bpw\b|\bbyju|unacademy|mckinsey|deloitte|stanford|harvard|\bmit\b)")
AVOID_TOPICS = [
    # Ritch's picks, Oct 31: no money/business coaching, no stock/investing talk, no podcasts, no military history, no family channels
    (re.compile(r"(?i)\b(make money online|passive income|side hustles?|business coach(ing)?|scale your (business|agency)|high[- ]ticket|agency owners?|marketing agency|"
                r"clickfunnels|amazon fba|copywriting|financial coach(ing)?|money coach(ing)?|money mindset|entrepreneurs?(hip)?|startup founders?|grow your business)\b"), 'money / business coaching (you skip these)'),
    (re.compile(r"(?i)\b(stock market|stocks? to buy|stock (analysis|picks?)|value investing|dividend stocks?|wall street|investor'?s podcast|earnings (report|call)|"
                r"portfolio update|buy the dip|s&p ?500|nasdaq|warren buffett|business explained)\b"), 'investing / business talk (you skip these)'),
    (re.compile(r"(?i)\b(podcast|pod cast|full episode)\b"), 'podcast (you skip these)'),
    (re.compile(r"(?i)\b(ww1|ww2|wwii|world war|military history|warfare|wehrmacht|nazis?|third reich|vietnam war|korean war|war documentary|"
                r"battle of (the )?\w+|d-day|tank battles?|fighter jets?)\b"), 'military / war history (you skip these)'),
    (re.compile(r"(?i)\b(family vlogs?|family channel|our family|mom of \d+|dad of \d+|mum of \d+|family of \d+|homeschool\w*|our kids|kids react)\b"), 'family channel (you skip these)'),
    (re.compile(r"(?i)\b(politics?|political|democrats?|republicans?|liberals?|conservatives?|trump|biden|maga|leftist|right[- ]wing|left[- ]wing|elections?|congress|parliament|socialism|communism|woke)\b"), 'politics (you skip these)'),
    (re.compile(r"(?i)\b(religio\w*|christian\w*|church|bible|jesus|god's|sermon|prayer|islam\w*|muslim|quran|allah|hindu\w*|buddhis\w*|atheis\w*|apologetics|theology|catholic|scripture)\b"), 'religion (you skip these)'),
    (re.compile(r"(?i)\b(drama|spill(ing)? the tea|tea time|exposing|got cancel(l)?ed|callout|scandal|downfall of|gossip)\b"), 'drama / tea (you skip these)'),
    (re.compile(r"(?i)\b(gore|gory|graphic violence|disturbing footage|real death|brutal kill|dismember\w*|autopsy|crime scene photos)\b"), 'gore (you skip these)'),
    (re.compile(r"(?i)\b(nsfw|onlyfans|sexy|thirst trap|hentai|lewd|adult content|stripper|fetish)\b|18\+"), 'sexual content (you skip these)')]
for _d in (BUSINESS_NICHES, CREATOR_NICHES, ALL_NICHES):   # searches that only find channels on your skip list waste quota
    for _n in list(_d): _d[_n] = [q for q in _d[_n] if not any(rx.search(q) for rx, _ in AVOID_TOPICS)]
KIDS_CONTENT = re.compile(r"(?i)\b(nursery rhymes?|kids songs?|songs for (kids|children|toddlers|babies)|for (toddlers|preschoolers|babies)|baby songs?|wheels on the bus|abc song|phonics song|learn colou?rs|kids cartoons?|cartoons? for kids|toddler learning|preschool|for kids|kids & toddlers|for children)\b")
KIDS_GAMING = re.compile(r"(?i)\b(roblox|minecraft|fortnite|(?<!wolf )among us|fall guys|poppy playtime|skibidi|gacha|brookhaven|bedwars|blox ?fruits|pet simulator|toca|bluey|fnaf|five nights|garten of banban)\b")
NATIVE_EN = {'US', 'GB', 'UK', 'CA', 'AU', 'NZ', 'IE'}   # Ritch: native-English countries only (unknown country is kept)
LOW_BUDGET_COUNTRIES = {'IN', 'PK', 'BD', 'NP', 'LK', 'ID', 'NG', 'EG', 'KE', 'GH', 'ET', 'VN', 'MM', 'KH', 'TR', 'IR', 'IQ', 'DZ', 'MA'}
HIGH_PAY_COUNTRIES = {'US', 'GB', 'CA', 'AU', 'NZ', 'IE', 'DE', 'NL', 'CH', 'NO', 'SE', 'DK'}
def _now(): return datetime.now(timezone.utc)
def _days(iso):
    try: return (_now() - datetime.fromisoformat(iso.replace('Z', '+00:00'))).total_seconds() / 86400
    except Exception: return 9999

# ---------- store ----------
_lock = threading.RLock()   # one writer at a time: searches, email finder, Gmail check and clicks never overwrite each other
BACKUPS = os.path.join(DATA, 'backups')
class LeadsFileDamaged(Exception): pass
def _read(p):
    with open(p, encoding='utf-8') as f: d = json.load(f)
    if not isinstance(d, list): raise ValueError('not a list')
    return d
_LIVE = {'L': None, 'idx': None, 'disk': None}   # the one in-memory list of leads; the disk copy follows it
def _disk_key():
    try: st = os.stat(LEADS_FILE); return (st.st_mtime_ns, st.st_size)
    except Exception: return None
def _read_disk():
    if not os.path.exists(LEADS_FILE): return []
    try: return _read(LEADS_FILE)
    except Exception:
        # damaged (e.g. PC shut down mid-save): fall back to the last good copy, never treat it as "no leads"
        cands = [LEADS_FILE + '.bak'] + sorted((os.path.join(BACKUPS, f) for f in (os.listdir(BACKUPS) if os.path.isdir(BACKUPS) else [])), reverse=True)
        for p in cands:
            try:
                d = _read(p)
                try: os.replace(LEADS_FILE, LEADS_FILE + '.damaged')
                except Exception: pass
                return d
            except Exception: continue
        raise LeadsFileDamaged('leads.json is damaged and no backup could be read')
def _live():
    """the live list. Re-read only if the file was changed by something else (a fix script) and we have nothing unsaved."""
    with _lock:
        k = _disk_key()
        if _LIVE['L'] is None or (not _PEND['t'] and k != _LIVE['disk']):
            _LIVE['L'] = _read_disk(); _LIVE['disk'] = k; _LIVE['idx'] = None; _bump()
        return _LIVE['L']
def load_leads():
    """your own copy of all leads (safe to change; save with save_leads). Prefer leads_ro/mutate: they don't copy."""
    import pickle
    return pickle.loads(pickle.dumps(_live(), protocol=pickle.HIGHEST_PROTOCOL))
SEARCHING = [0]   # >0 while a search runs: background jobs step aside
_RO_N = [0]   # bumps every time the leads change (the page uses it to skip re-sending the list)
_CHG = []   # (version, channel_id) for single-lead edits, or (version, None) = everything may have changed: the page then gets only what changed
def _bump(cid=None):
    _RO_N[0] += 1; _CHG.append((_RO_N[0], cid)); del _CHG[:-500]
def leads_ro():
    """all leads, read-only and instant (no copy). NEVER change what it returns; use mutate()/patch()."""
    return _live()
def _index():
    if _LIVE['idx'] is None: _LIVE['idx'] = {l.get('channel_id'): l for l in _live()}
    return _LIVE['idx']
def get_lead(cid):
    """one lead (your own copy, safe to change), without copying all of them"""
    import copy
    l = _index().get(cid)
    return copy.deepcopy(l) if l is not None else None
_TOPIC_GENERIC = {'Lifestyle (sociology)', 'Entertainment', 'Knowledge', 'Society', 'Hobby'}
def topic_niches(ids):
    """YouTube's own topic for each channel ({channel_id: niche}); 1 quota unit per 50 channels"""
    out = {}; ids = [i for i in ids if i]
    try: yt = ls().get_youtube_service(0)
    except Exception: return out
    for i in range(0, len(ids), 50):
        try: r = yt.channels().list(part='topicDetails', id=','.join(ids[i:i + 50]), maxResults=50).execute()
        except Exception: continue
        for it in r.get('items', []):
            cats = [u.rsplit('/', 1)[-1].replace('_', ' ').replace('%27', "'") for u in (it.get('topicDetails') or {}).get('topicCategories', [])]
            t = ([c for c in cats if c not in _TOPIC_GENERIC] or cats or [None])[0]
            if t: out[it['id']] = t.lower()
    return out
def _tag_new_soon(ids):
    """new leads get YouTube's topic as their niche (in the background, a moment after they're added)"""
    def run():
        T = topic_niches(ids)
        if not T: return
        def f(L):
            for l in L:
                t = T.get(l.get('channel_id'))
                if t and l.get('mode') != 'manual': l.setdefault('niche_old', l.get('niche')); l['niche'] = t
        mutate(f)
    threading.Thread(target=run, daemon=True).start()
def add_leads(new):
    """several new leads in ONE save (a search finds them in bursts: one write instead of one per lead)"""
    if not new: return 0
    def f(L):
        have = {x['channel_id'] for x in L}; n = 0
        for l in new:
            if l['channel_id'] not in have: L.append(l); have.add(l['channel_id']); n += 1
        return n
    n = mutate(f)
    if n: _tag_new_soon([l.get('channel_id') for l in new])
    return n
_BAK_T = [0.0]; _PEND = {'t': None}
def save_leads(L):
    """replace all leads. The app sees it at once; the disk write happens a moment later in the background (several quick changes = one write)."""
    if not isinstance(L, list): return
    with _lock:
        _LIVE['L'] = L; _LIVE['idx'] = None; _bump(); _schedule()
def _schedule():
    if not _PEND['t']:
        _PEND['t'] = threading.Timer(0.5, flush_leads); _PEND['t'].daemon = True; _PEND['t'].start()
def flush_leads():
    with _lock:
        if not _PEND['t']: return
        _PEND['t'] = None; L = _LIVE['L']
        if L is None: return
        data = json.dumps(L, separators=(',', ':'))   # compact: the fast path
    tmp = LEADS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    if os.path.exists(LEADS_FILE) and time.time() - _BAK_T[0] > 30:
        try: shutil.copyfile(LEADS_FILE, LEADS_FILE + '.bak'); _BAK_T[0] = time.time()   # last good copy
        except Exception: pass
    with _lock:
        os.replace(tmp, LEADS_FILE)
        if not _PEND['t']: _LIVE['disk'] = _disk_key()
    try:   # one dated backup per day, keep 14
        day = os.path.join(BACKUPS, f'leads-{_now().strftime("%Y-%m-%d")}.json')
        if not os.path.exists(day):
            os.makedirs(BACKUPS, exist_ok=True); shutil.copyfile(LEADS_FILE, day)
            for old in sorted(os.listdir(BACKUPS))[:-14]: os.remove(os.path.join(BACKUPS, old))
    except Exception: pass
import atexit; atexit.register(flush_leads)   # never lose the last change when the app closes

def mutate(fn):
    """change leads in place: fn(list) edits the live list; no copying of 30,000 leads per click"""
    with _lock:
        L = _live(); r = fn(L); _LIVE['idx'] = None
        _bump(); _schedule(); return r
def patch(cid, **kw):
    with _lock:
        l = _index().get(cid)
        if l is None or l not in _live(): return mutate(lambda L: next((x.update(kw) or x for x in L if x['channel_id'] == cid), None))
        l.update(kw); _bump(cid); _schedule(); return l
def add_lead(lead):
    def f(L):
        if lead['channel_id'] in {x['channel_id'] for x in L}: return False
        L.append(lead); return True
    ok = mutate(f)
    if ok and lead.get('mode') != 'manual': _tag_new_soon([lead['channel_id']])
    return ok
DEFAULT_SOURCES = [{'name': 'Cold email', 'on': True}, {'name': 'YT Jobs', 'on': True}, {'name': 'Actively Hiring', 'on': False},
                   {'name': 'Discord', 'on': False}, {'name': 'OLJ', 'on': False}, {'name': 'X', 'on': False}, {'name': 'Referral', 'on': False}, {'name': 'Other', 'on': False}]
def load_settings():
    d = {'v': 11, 'min_subs': 1000, 'max_subs': 300000, 'cr_min_subs': 20000, 'cr_max_subs': 400000, 'cr_min_views': 10000, 'cr_need_money': True, 'max_searches': 12, 'per_query': 50, 'days_recent': 120, 'min_tier': 'MEDIUM', 'sources': [dict(x) for x in DEFAULT_SOURCES]}
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f: d.update(json.load(f))
    except Exception: pass
    if int(d.get('v', 1)) in (2, 3, 4, 5, 6, 7, 8, 9, 10): d['v'] = 11; d['_rescore'] = True   # v7: red-flag rule + niche labels from their own videos
    if int(d.get('v', 1)) < 2:   # v2: goldilocks sizes (big established channels usually already have a team)
        d['cr_max_subs'] = min(int(d.get('cr_max_subs', 400000)), 400000); d['max_subs'] = min(int(d.get('max_subs', 300000)), 300000)
        d['cr_min_subs'] = max(int(d.get('cr_min_subs', 20000)), 20000) if int(d.get('cr_min_subs', 30000)) == 30000 else int(d.get('cr_min_subs'))
        d['v'] = 6; d['_rescore'] = True
        try: save_settings(d)
        except Exception: pass
    return d
def save_settings(d):
    with _lock:   # atomic, so a crash mid-save can't wipe your settings
        tmp = SETTINGS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f: json.dump(d, f, indent=1)
        os.replace(tmp, SETTINGS_FILE)

# ---------- scoring ----------
def analyze(info, vids, mode, rules=None):
    """Returns dict with score 0-100, tier, reasons, needs/pay/growth parts. Pure function (tested)."""
    desc = (info.get('description') or '') + ' ' + (info.get('keywords') or '')
    vdesc = ' '.join((v.get('description') or '') for v in vids[:10])
    title = info.get('title') or ''
    try: subs = int(info.get('subscriber_count') or 0)
    except Exception: subs = 0
    R, X = [], []   # reasons, exclusion reasons
    long_v = [v for v in vids if (v.get('duration_seconds') or 0) >= 480 or v.get('was_live')]   # long-form = 8+ minutes
    shorts = [v for v in vids if 0 < (v.get('duration_seconds') or 0) < 61]
    recent = [v for v in vids if _days(v.get('published_raw') or '') <= 60]
    last_days = min([_days(v.get('published_raw') or '') for v in vids] or [9999])
    views = [int(v.get('views') or 0) for v in long_v] or [int(v.get('views') or 0) for v in vids]
    med = statistics.median(views) if views else 0
    if EXCLUDE_RE.search(title + ' ' + desc): X.append('AI/ads content (excluded)')
    if TECH_RE.search(title + ' ' + (info.get('description') or '')[:600]): X.append('technical/finance/legal niche (excluded)')
    alltxt = title + ' ' + ' '.join(v.get('title', '') for v in vids[:10])
    nonlatin = sum(1 for ch in alltxt if ord(ch) > 0x24F and ch.isalpha()) / max(1, sum(1 for ch in alltxt if ch.isalpha()))
    if nonlatin > 0.15 or re.search(r'(?i)\b(hindi|urdu|bangla|bengali|tamil|telugu|malayalam|marathi|kannada|tagalog|t[üu]rk[çc]e|indonesia|bahasa|español|portugu[eê]s|deutsch|fran[çc]ais|arabic|nepali)\b', alltxt):
        X.append('not an English channel')
    if ORG_RE.search(title) or ORG_RE.search((info.get('description') or '')[:300]): X.append('company / institution channel (in-house team)')
    for rx, why in AVOID_TOPICS:   # topics Ritch doesn't edit: channel name/about, or a big share of recent videos
        hits = sum(1 for v in vids if rx.search(v.get('title') or ''))
        if rx.search(title + ' ' + (info.get('description') or '')[:400]) or (vids and hits >= max(3, len(vids) * 0.35)): X.append(why); break
    kids = sum(1 for v in vids if KIDS_GAMING.search(v.get('title') or '')) + (3 if KIDS_GAMING.search(title) else 0)
    if vids and kids >= max(3, len(vids) * 0.4): X.append('kids / over-edited gaming (Roblox, Minecraft, Fortnite...)')
    kc = sum(1 for v in vids if KIDS_CONTENT.search(v.get('title') or '')) + (3 if KIDS_CONTENT.search(title + ' ' + (info.get('description') or '')[:400]) else 0)
    if kc >= max(2, len(vids) * 0.3) or info.get('made_for_kids'): X.append("made for kids (nursery rhymes / kids songs: low pay, not long-form)")
    try:
        lang = ls().detect_language(title + ' ' + (info.get('description') or '') + ' ' + ' '.join(v.get('title', '') for v in vids[:8]))
        if lang not in ('English', 'Unknown'): X.append(f'not English ({lang})')
    except Exception: pass
    if not vids or last_days > 90: X.append('inactive 90+ days')
    elif vids and len([v for v in long_v if _days(v.get('published_raw') or '') <= 60]) < 2: X.append('fewer than 1 long video a month')
    cc0 = str(info.get('country') or '').upper()
    if cc0 and cc0 not in ('N/A', 'NONE') and cc0 not in NATIVE_EN: X.append(f'based in {cc0} (not a native-English country)')
    try:   # old-school: views flat or falling for 6+ months
        aged_ = sorted(((_days(v.get('published_raw') or ''), int(v.get('views') or 0)) for v in (long_v or vids)), key=lambda x: x[0])
        aged_ = [x for x in aged_ if x[0] >= 5]
        if len(aged_) >= 8 and aged_[-1][0] >= 180:
            rec_ = statistics.median(v for _, v in aged_[:5]); old_ = statistics.median(v for _, v in aged_[5:])
            if old_ and rec_ <= 0.85 * old_: X.append(f'no growth for 6+ months (recent videos at {rec_ / old_:.1f}x their older ones)')
    except Exception: pass
    if vids and len(shorts) > len(vids) * 0.5: X.append('mostly Shorts')
    if vids and not long_v: X.append('no long-form videos')
    clipish = sum(1 for v in vids if re.search(r'(?i)\b(compilation|best moments|funniest moments|highlights?|clips?|full (?:movie|match|episode)|reupload|#shorts)\b', v.get('title') or ''))
    if vids and clipish >= max(3, len(vids) * 0.5): X.append('clips/compilation channel (short-form clipping work)')
    # needs help: active long-form uploader, no editor credited
    needs = 0
    if len(recent) >= 2: needs += 15; R.append(f"{len(recent)}{'+' if len(recent) >= len(vids) and len(vids) >= 15 else ''} uploads in 60 days")
    elif recent: needs += 6
    if len(long_v) >= max(3, len(vids) * 0.5): needs += 10
    try: ed_n, ed_who = ls().editor_credits(vids)
    except Exception: ed_n, ed_who = 0, []
    if ed_n: R.append('already credits an editor (' + ', '.join(ed_who[:2]) + ') - proven payer')
    else: needs += 10; R.append('no editor credited')
    try:   # timing: their editor just left / uploads slowing down (they need help NOW)
        import sr_boost; SIG = sr_boost.signals(info, vids)
    except Exception: SIG = []
    for k_, why_ in SIG:
        if k_ == 'editor_left': needs += 14; R.insert(0, why_)
        elif k_ == 'slowing': needs += 6; R.append(why_)
    # can pay
    pay = 0
    site = own_site(info.get('description') or '', vids, title, '')
    if site: pay += 12; R.append('own website')
    if PAY_WORDS.search(desc) or PAY_WORDS.search(vdesc): pay += 12; R.append('sells courses/lessons/services')
    sponsors = len([v for v in vids[:10] if SPONSOR_WORDS.search(v.get('description') or '')])
    if sponsors >= 2: pay += 14; R.append(f'sponsors on {sponsors} recent videos')
    elif sponsors: pay += 8; R.append('has a sponsor')
    elif AFFILIATE_WORDS.search(vdesc): pay += 3
    if mode == 'business' and OWNER_WORDS.search(desc): pay += 6; R.append('runs a business')
    op, oh = old_signals(desc + ' ' + vdesc)
    if op == -999: X.append('guru/get-rich content (excluded)')
    elif op: pay += op; R.append('money signals: ' + ', '.join(dict.fromkeys(oh[:4])))
    if ed_n: pay += 10
    cc = str(info.get('country') or '').upper()
    if cc in HIGH_PAY_COUNTRIES: pay += 5; R.append(f"based in {cc}")
    elif cc in LOW_BUDGET_COUNTRIES: pay -= 12; R.append(f"based in {cc} (usually lower budgets)")
    if subs >= 10000: pay += 8
    elif subs >= 3000: pay += 4
    if med >= 5000: pay += 8; R.append(f'~{int(med):,} views per video')
    elif med >= 1000: pay += 4
    # workload: long-form uploads per week from the sample (master prompt: avoid 5+/week, daily output)
    lf_dates = sorted(_days(v.get('published_raw') or '') for v in long_v)
    if len(lf_dates) >= 4:
        span = max(1.0, lf_dates[-1] - lf_dates[0]); per_week = (len(lf_dates) - 1) / span * 7
        if per_week > 5: cap_work = True; R.append(f'~{per_week:.0f} long videos/week (too much volume)')
        else: cap_work = False
        if 0.7 <= per_week <= 3: needs += 5; R.append(f'{per_week:.1f} long videos/week (ideal workload)')
    else: cap_work = False
    # goldilocks: big enough to pay, not so established they already have a team, with room to improve
    roles, company = team_signals(info, vids); size = 0; cap = None
    creator_like = mode == 'creators' or bool(rules and rules.get('creator'))
    lo_s, hi_s, big = (20000, 250000, 600000) if creator_like else (2000, 120000, 400000)
    if lo_s <= subs <= hi_s: size = 10; R.append('sweet-spot size')
    elif subs > big: size = -8; cap = 'MEDIUM'; R.append(f'big channel ({subs:,} subs), likely has a team')
    elif subs > hi_s: size = -6
    if cap_work: cap = 'MEDIUM'
    if len(roles) >= 3 or (company and len(roles) >= 1):
        needs -= 15; cap = 'MEDIUM'; R.append('already has a team (' + ', '.join(sorted(roles)[:3]) + (', company channel' if company else '') + ')')
    elif company: needs -= 8; R.append('company/brand channel')
    if subs >= 10000 and med and med < subs * 0.04: needs += 6; R.append('views low for their size (room to improve)')
    # growth: recent videos vs the channel's usual
    growth, gx = 0, 0.0
    aged = [(int(v.get('views') or 0), _days(v.get('published_raw') or '')) for v in (long_v or vids)]
    aged = [x for x, d in aged if d >= 5]   # a 2-day-old video hasn't had time to get its views yet
    if len(aged) >= 6:
        rv = aged[:3]; base = statistics.median(aged[3:]) or 1
        gx = max(rv) / base
        if gx >= 3: growth = 20; R.append(f'recent video at {gx:.1f}x usual views (algorithm push)')
        elif gx >= 1.6: growth = 10; R.append(f'recent videos up {gx:.1f}x')
    # 100 = needs 40 + can-pay 45 + growth 15 (growing mode: 30/40/30), capped parts so scores spread out instead of all hitting 100
    # 100 points: needs an editor 35 + can pay 40 + growth 15 + size fit 10 (growing mode: 25 / 35 / 30 / 10)
    nP = min(max(needs, 0), 46) / 46; pP = min(pay, 60) / 60; gP = growth / 20; zP = max(size, 0) / 10
    if mode == 'growing': score = nP * 25 + pP * 35 + gP * 30 + zP * 10
    else: score = nP * 35 + pP * 40 + gP * 15 + zP * 10
    score = max(0, min(100, score + min(size, 0)))   # size penalties still subtract
    if mode == 'growing' and gx < 1.6: X.append('not growing right now')
    if rules and rules.get('creator'):
        # creators must earn enough to keep an editor: real views + sponsors / paid community / merch / an editor already paid
        money = sponsors > 0 or bool(ed_n) or any(h in ('Patreon', 'Membership', 'Merch/Shop', 'Sponsorships', 'Course', 'Paid Subscription') for h in oh)
        if med < int(rules.get('min_views') or 0): X.append(f'under {int(rules["min_views"]):,} views per video')
        if rules.get('need_money') and not money: X.append('no sponsors or paid products yet')
        elif money: pass   # money is a gate for creators; sponsors already count in 'can pay'
    tier = 'EXCLUDED' if X else ('HIGH' if score >= 62 else 'MEDIUM' if score >= 42 else 'LOW')
    if cap == 'MEDIUM':
        score = min(score, 61)
        if tier == 'HIGH': tier = 'MEDIUM'
    # several red flags together (big + team, low budget + daily volume...) = not worth your time, whatever else it scores
    flags = [r for r in R if re.search(r'likely has a team|already has a team|company/brand channel|usually lower budgets|too much volume', r)]
    if len(flags) >= 2 and tier in ('HIGH', 'MEDIUM'): tier = 'LOW'; score = min(score, 41)
    return {'signals': SIG, 'score': round(score), 'tier': tier, 'reasons': R, 'excluded': X, 'needs': needs, 'pay': pay, 'growth_x': round(gx, 1),
            'median_views': int(med), 'subs': subs, 'website': site, 'last_upload_days': round(last_days)}

_NTOK = {}
_NGEN = set("""video videos channel channels youtube explained explain tips guide how why what with your for the and best beginners beginner lesson lessons
review reviews series documentary essay story stories vlog analysis deep dive full course long form show life day journey building build""".split())
def _ntoks(n):
    if n not in _NTOK:
        _NTOK[n] = {w for q in ALL_NICHES.get(n, []) for w in re.findall(r'[a-z]{4,}', q.lower()) if w not in _NGEN} | {w for w in re.findall(r'[a-z]{4,}', n.lower()) if w not in _NGEN}
    return _NTOK[n]
def fit_niche(niche, mode, info, vids):
    """the niche label = the search that found them, unless their own videos clearly belong to another niche
    (e.g. an investing podcast found by a hiking search)"""
    return niche   # the word-count re-guess mislabelled most leads (generic words pulled them into personal finance); Fix_Niches.bat sets YouTube's own topic instead
    words = re.findall(r'[a-z]{4,}', ((info.get('title') or '') + ' ' + (info.get('description') or '')[:600] + ' ' + ' '.join(v.get('title') or '' for v in vids[:12])).lower())
    if not words or niche not in ALL_NICHES: return niche
    cnt = {}
    for w in words + re.findall(r'[a-z]{4,}', (info.get('title') or '').lower()):   # their channel name counts double
        cnt[w] = cnt.get(w, 0) + 1
    score = lambda n: sum(min(cnt.get(w, 0), 3) for w in _ntoks(n))
    mine = score(niche)
    if mine >= 2: return niche
    pool = CREATOR_NICHES if mode in ('creators', 'growing') else BUSINESS_NICHES
    best = max(pool, key=score, default=niche)
    return best if score(best) >= 3 and score(best) >= mine + 2 else niche

def lead_from(info, vids, a, mode, niche, query):
    L = ls()
    try: niche = fit_niche(niche, mode, info, vids)
    except Exception: pass
    text = (info.get('description') or '') + '\n' + '\n'.join((v.get('description') or '') for v in vids[:10])
    email = None; contacts = ''
    try: email = L.extract_email(text)
    except Exception: pass
    try: contacts = L.build_contact_info(text, do_linktree=False, channel_bio=info.get('description') or '') or ''   # IG/X/LinkedIn; Linktree + website are looked up in the background
    except Exception: pass
    email = clean_email(email) or None
    site2 = own_site(info.get('description') or '', vids, info.get('title') or '', email or '')
    if site2: a['website'] = site2
    vd = '\n'.join((v.get('description') or '')[:600] for v in vids[:6])
    fn, fc, fs = guess_name(info.get('title') or '', info.get('description') or '', email or '', vd)
    lv = next((v for v in vids if (v.get('duration_seconds') or 0) >= 240 or v.get('was_live')), vids[0] if vids else {})
    cu = info.get('custom_url') or ''
    url = f"https://www.youtube.com/{cu}" if cu and cu.startswith('@') else f"https://www.youtube.com/channel/{info.get('channel_id')}"
    return {'id': info.get('channel_id'), 'channel_id': info.get('channel_id'), 'title': info.get('title'), 'url': url, 'handle': cu,
            'photo': info.get('photo', ''), 'country': info.get('country'), 'mode': mode, 'niche': niche, 'query': query,
            'subs': a['subs'], 'median_views': a['median_views'], 'score': a['score'], 'tier': a['tier'], 'reasons': a['reasons'],
            'growth_x': a['growth_x'], 'signals': a.get('signals') or [], 'website': a['website'], 'email': email or '', 'email_hidden_check': not email,
            'latest_video': {k: lv.get(k) for k in ('video_id', 'title', 'url', 'thumbnail', 'published', 'views', 'duration')} if lv else {},
            'status': 'new', 'history': [], 'added': _now().isoformat()[:19],
            'contacts': contacts, 'proof': proof_for(niche, mode), 'about': (info.get('description') or '')[:1500], 'video_titles': [v.get('title') for v in vids[:8]],
            'video_snips': vd[:2500], 'first_name': fn if fc >= 3 else '', 'name_guess': {'name': fn, 'conf': fc, 'src': fs}}

# ---------- search ----------
def sheet_mark():
    """tell the sheet syncer something changed (batched write a few seconds later)"""
    try:
        import sr_sheet; sr_sheet.AUTO.mark()
    except Exception: pass

def enrich(l):
    """slow web lookups done AFTER the lead is saved, so they never hold up a search: Linktree pages + their own website for an email"""
    if l.get('email'): return None
    L = ls(); email = None
    try:
        lt = re.findall(r'https?://(?:www\.)?linktr\.ee/[\w.\-]+', (l.get('about') or '') + ' ' + (l.get('contacts') or ''))
        for u in lt[:1]:
            for x in (L.fetch_linktree(u) or []):
                m = re.search(r'mailto:([\w.+-]+@[\w-]+\.[\w.-]+)', str(x)) or re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', str(x))
                if m: email = m.group(1) if m.lastindex else m.group(0); break
    except Exception: pass
    if not email and l.get('website') and load_settings().get('site_email', True):
        try: email = site_email(l['website']) or None
        except Exception: pass
    email = clean_email(email)
    if not email: return None
    def f(L):
        for x in L:
            if x['channel_id'] == l['channel_id'] and not x.get('email'): x['email'] = email
    mutate(f)
    return email

def rescore_leads(fetch=True, progress=None, pause=lambda: False):
    """re-score saved leads with the current rules. Uses saved channel data, and fetches it again when it has expired
    (about 3 quota units per lead). Also re-checks email, website and first name with the stricter rules."""
    from concurrent.futures import ThreadPoolExecutor
    L = ls(); S = load_settings(); leads = [dict(x) for x in load_leads() if x.get('mode') != 'manual']; upd = {}; changed = [0]; done = [0]
    run = Run()
    def one(l):
        cid = l['channel_id']
        while pause(): time.sleep(2)   # a search is running: it gets the whole PC + YouTube connection
        try:
            info = L.load_cached_channel(cid) or (L.get_channel_info(cid) if fetch else None)
            vids = L.load_cached_videos(cid, 15)
            if vids is None and fetch: vids = run.videos(cid, 15)
            if not info or not vids: return
            cr = Run.creator(l.get('mode'), l.get('niche'))
            a = analyze(info, vids, l.get('mode', 'business'), {'creator': cr, 'min_views': S.get('cr_min_views', 0), 'need_money': S.get('cr_need_money', True)} if cr else None)
            tier = a['tier'] if a['tier'] in ('HIGH', 'MEDIUM') else 'LOW'
            try: nn = fit_niche(l.get('niche'), l.get('mode', 'business'), info, vids)
            except Exception: nn = l.get('niche')
            u = dict(niche=nn, score=min(a['score'], 30) if a['excluded'] else a['score'], tier=tier,   # outside your filters: sinks to the bottom growth_x=a['growth_x'], signals=a.get('signals') or [], median_views=a['median_views'], subs=a['subs'] or l.get('subs'),
                     reasons=a['reasons'] + (['now outside your filters: ' + '; '.join(a['excluded'])] if a['excluded'] else []))
            em = l.get('email') or ''
            if em and not clean_email(em): u['email'] = ''          # platform / sponsor / placeholder address
            elif em: u['email'] = clean_email(em)
            u['website'] = own_site(info.get('description') or '', vids, info.get('title') or '', u.get('email', em))
            g = l.get('name_guess') or {}
            if l.get('first_name') and l['first_name'] == g.get('name') and not str(g.get('src', '')).startswith('Claude'):
                vd = '\n'.join((v.get('description') or '')[:600] for v in vids[:6])
                fn, fc, fs = guess_name(info.get('title') or '', info.get('description') or '', u.get('email', em), vd)
                u['first_name'] = fn if fc >= 3 else ''; u['name_guess'] = {'name': fn, 'conf': fc, 'src': fs}
            if tier != l.get('tier'): changed[0] += 1
            upd[cid] = u
        finally:
            done[0] += 1
            if progress and done[0] % 20 == 0: progress(done[0], len(leads))
    with ThreadPoolExecutor(max_workers=16) as ex: list(ex.map(one, leads))
    def f(Lx):
        for x in Lx:
            if x['channel_id'] in upd: x.update(upd[x['channel_id']])
    mutate(f); sheet_mark()
    return changed[0]

def sheet_row(l):
    t = str(l.get('title') or '').replace('"', "'")
    return {'Channel': f'=HYPERLINK("{l.get("url")}","{t}")', 'ChannelId': l.get('channel_id'), 'Title': l.get('title'),
            'Subs': str(l.get('subs') or ''), 'Avg Views': str(l.get('median_views') or ''), 'Engagement': '', 'Upload Freq': next((r for r in l.get('reasons', []) if 'uploads in' in r), ''),
            'Last Upload': str((l.get('latest_video') or {}).get('published') or '')[:10], 'Est. Revenue/Video': '',
            'Fit': {'HIGH': 'HIGH', 'MEDIUM': 'MODERATE'}.get(l.get('tier'), 'LOW'), 'Reason': ' · '.join(l.get('reasons', [])[:5]),
            'Contact': ' | '.join(x for x in (l.get('email'), l.get('website'), l.get('contacts')) if x)[:400], 'Date': _now().strftime('%Y-%m-%d'),
            'Genre': l.get('niche', ''), 'Country': l.get('country') or 'N/A', 'Channel Age': '', 'Monetization Signals': next((r[15:] for r in l.get('reasons', []) if r.startswith('money signals')), ''),
            'Notes': f"SwipeReach Studio · score {l.get('score')}" + (f" · growth {l.get('growth_x')}x" if (l.get('growth_x') or 0) >= 1.6 else ''), 'Avg Dur': '', 'Consistency': ''}

def push_sheet(l):
    """same Google Sheet + columns + HIGH/MODERATE fit as old SwipeReach"""
    try: r = ls().push_single_row_to_sheets(sheet_row(l))
    except Exception as e: return {'status': 'error', 'reason': str(e)}
    return r or {}

# ---------- two-way status sync with the sheet (V2 tab, column A = Status) ----------
TO_SHEET = {'contacted': 'First Outreach Sent', 'followup': 'Follow Up', 'meeting': 'Accepted Meeting', 'no_answer': 'No Answer',
            'denied': 'Denied', 'client': 'Client', 'new': 'N/A', 'drafted': 'N/A'}
FROM_SHEET = {'First Outreach Sent': 'contacted', 'Follow Up': 'followup', 'Accepted Meeting': 'meeting', 'No Answer': 'no_answer', 'Denied': 'denied', 'Client': 'client'}
_sheet_lock = threading.Lock()
def _sheet():
    L = ls(); svc = L._get_sheets_service()
    if svc is None: raise RuntimeError('Google Sheets not available')
    tab = L._resolve_sheet_tab_title(svc, L.GSHEETS_SPREADSHEET_ID, L.GSHEETS_TAB_NAME)
    if not tab: raise RuntimeError('sheet tab not found')
    return L, svc, tab

def _rows(svc, L, tab):
    r = svc.spreadsheets().values().get(spreadsheetId=L.GSHEETS_SPREADSHEET_ID, range=f"'{tab}'!A:B", valueRenderOption='FORMULA').execute()
    return r.get('values', [])

def _row_of(rows, l):
    keys = [k for k in (l.get('channel_id'), (l.get('handle') or '').lower() if (l.get('handle') or '').startswith('@') else None, l.get('url')) if k]
    pats = [re.compile(re.escape(k.lower().rstrip('/')) + r'(?![\w.-])') for k in keys]   # exact: @c11 must not match @c111
    for i, r in enumerate(rows):
        b = str(r[1] if len(r) > 1 else '').lower()
        if any(p.search(b) for p in pats): return i + 1
    return None

def sheet_update(l, status=None, contact=None):
    """change this lead's Status (col A) / Contact (col K) in the sheet; adds the row first if it isn't there"""
    with _sheet_lock:
        L, svc, tab = _sheet(); rows = _rows(svc, L, tab); n = _row_of(rows, l)
        if not n:
            push_sheet(l); rows = _rows(svc, L, tab); n = _row_of(rows, l)
            if not n: return False
        data = []
        if status and TO_SHEET.get(status): data.append({'range': f"'{tab}'!A{n}", 'values': [[TO_SHEET[status]]]})
        if contact: data.append({'range': f"'{tab}'!K{n}", 'values': [[contact]]})
        if data: svc.spreadsheets().values().batchUpdate(spreadsheetId=L.GSHEETS_SPREADSHEET_ID, body={'valueInputOption': 'USER_ENTERED', 'data': data}).execute()
        return True

def sheet_pull(leads):
    """statuses you changed in the sheet -> app. returns number changed"""
    L, svc, tab = _sheet(); rows = _rows(svc, L, tab); ch = 0; leads = [dict(x) for x in leads]; changes = {}
    for l in leads:
        n = _row_of(rows, l)
        if not n: continue
        st = FROM_SHEET.get(str(rows[n - 1][0] if rows[n - 1] else '').strip())
        if st and st != l.get('status') and not (st == 'followup' and l.get('status') in ('contacted', 'replied')) and not (st == 'contacted' and l.get('status') in ('replied', 'followup')):
            l['status'] = st; ch += 1; changes[l['channel_id']] = st
    if changes:
        def f(Lx):
            for x in Lx:
                if x['channel_id'] in changes: x['status'] = changes[x['channel_id']]
        mutate(f)
    return ch

def sheet_selftest(leads):
    """read-only health check of the real sheet + a no-change write to prove write access"""
    out = {'steps': []}
    def step(ok, msg): out['steps'].append({'ok': bool(ok), 'msg': msg}); return ok
    try: L, svc, tab = _sheet()
    except Exception as e: step(False, f'Could not connect: {e}'); return out
    step(True, f'Connected to the sheet, tab "{tab}"')
    try:
        hdr = svc.spreadsheets().values().get(spreadsheetId=L.GSHEETS_SPREADSHEET_ID, range=f"'{tab}'!A1:S1").execute().get('values', [[]])
        hdr = [h.strip() for h in (hdr[0] if hdr else [])]
        want = list(getattr(L, 'GSHEETS_COLUMNS', []))
        miss = [c for c in want if c not in hdr]
        step(not miss, 'Columns match' if not miss else 'Header row is different from what the app writes. Missing: ' + ', '.join(miss[:6]) + f' (sheet has: {", ".join(hdr[:8])}...)')
        rows = _rows(svc, L, tab); step(True, f'{max(0, len(rows) - 1)} rows in the sheet')
        mine = [l for l in leads if l.get('mode') != 'manual']; found = sum(1 for l in mine if _row_of(rows, l))
        step(True, f'{found} of your {len(mine)} app leads are in the sheet' + (' (use Leads > Send to Sheet for the rest)' if found < len(mine) else ''))
        bad = [r[0] for r in rows[1:] if r and r[0] and r[0] not in list(TO_SHEET.values()) + ['N/A', '']]
        if bad: step(False, f'{len(bad)} rows use a status the app does not know: ' + ', '.join(sorted(set(bad))[:5]))
        a1 = rows[0][0] if rows and rows[0] else 'Status'
        svc.spreadsheets().values().update(spreadsheetId=L.GSHEETS_SPREADSHEET_ID, range=f"'{tab}'!A1", valueInputOption='RAW', body={'values': [[a1]]}).execute()
        step(True, 'Write access OK (nothing was changed)')
    except Exception as e: step(False, f'Error: {e}')
    return out

USED_FILE = os.path.join(DATA, 'used_queries.json')
def plan_queries(niches, custom=None, budget=12):
    """custom first, then round-robin across niches, preferring searches you haven't run lately (so every run finds new people)"""
    try: used = json.load(open(USED_FILE, encoding='utf-8'))
    except Exception: used = {}
    out = [('custom', q.strip()) for q in (custom or []) if q.strip()]
    import sr_brain as B   # proven searches first, then ones not run lately; retired ones (found nothing twice) last
    def order(n):
        qs = list(dict.fromkeys(list(ALL_NICHES.get(n, [])) + B.pool(n)))
        live = [q for q in qs if B.value(q) >= 0] or qs
        return [(n, q) for q in sorted(live, key=lambda q: (-min(B.value(q), 3.0) if B.value(q) > 0 else 0, used.get(q.lower(), ''), 0 if B.is_trend(q) else 1))]
    lists = [order(n) for n in niches]
    i = 0
    while len(out) < budget and any(lists):
        l = lists[i % len(lists)]
        if l: out.append(l.pop(0))
        i += 1
    out = out[:budget]
    for _, q in out: used[q.lower()] = _now().isoformat()[:19]
    try: json.dump(used, open(USED_FILE, 'w', encoding='utf-8'))
    except Exception: pass
    return out

class Run:
    def __init__(self, on_event=None):
        self.stop = False; self.quota_out = False; self.sheet_ids = set(); self.sheet_handles = set(); self._handles = {}; self.ev = on_event or (lambda kind, data: None)
    def log(self, m): self.ev('log', m)

    def find(self, mode, niches, custom_queries=None, settings=None):
        """searches -> cheap subscriber prefilter -> full checks, all in parallel.
        keep_going: after the planned searches, keeps fetching next pages and searches mined from your best new leads
        until you press Stop, nothing new turns up, or the day's quota is nearly used."""
        L = ls(); S = dict(load_settings(), **(settings or {}))
        existing = {l['channel_id'] for l in leads_ro()}
        try: processed = set(L.load_processed_ids()) if hasattr(L, 'load_processed_ids') else set()
        except Exception: processed = set()
        import sr_brain as B
        try:
            left0 = self._quota_left()
            if left0 is not None and left0 < int(S.get('quota_reserve', 20000)) + 5000:
                k = probe_keys(force=True)
                if k: self.log(f'YouTube has reset {k} key{"s" if k > 1 else ""} early: back in use')
        except Exception: pass
        try:
            B.credit_winners(leads_ro())   # people who replied: the searches that found them get boosted
            if any(n in CREATOR_NICHES for n in niches): threading.Thread(target=B.refresh_trends, args=(self.log,), daemon=True).start()   # once a day, ~8 quota units, never makes the search wait
        except Exception as e: self.log(f'search brain: {e}')
        queries = plan_queries(niches, custom_queries, int(S['max_searches']))
        keep = bool(S.get('keep_going')); reserve = int(S.get('quota_reserve', 20000))
        after = (_now() - timedelta(days=30 if mode == 'growing' else int(S['days_recent']))).strftime('%Y-%m-%dT%H:%M:%SZ')
        found, lock = [], threading.Lock(); cnt = {'checked': 0, 'searches': 0}
        self.sheet_ids, self.sheet_handles = set(), set()
        if S.get('sheet_push', True):
            try:   # channels already in your sheet (old V2 tab + SwipeReach Studio tab) are never "new"
                import sr_sheet; self.sheet_ids, self.sheet_handles = sr_sheet.known_ids()
                self.log(f'skipping {len(self.sheet_ids) + len(self.sheet_handles)} channels already in your sheet')
            except Exception as e: self.log(f'could not read your sheet for duplicates: {e}')
        claimed = set(existing) | set(processed) | self.sheet_ids; used_q = {q.lower() for _, q in queries}
        self.log(f'{len(queries)} searches planned' + (' then keep going until you stop' if keep else ''))
        from concurrent.futures import ThreadPoolExecutor, as_completed, wait
        sp, cp, ep = ThreadPoolExecutor(max_workers=16), ThreadPoolExecutor(max_workers=48), ThreadPoolExecutor(max_workers=6)   # network-bound: more at once = faster
        def enrich_bg(lead):
            e = enrich(lead)
            if e:
                lead['email'] = e; self.ev('lead_update', {'channel_id': lead['channel_id'], 'email': e})
                sheet_mark()
        # mode 'both': each search uses the rules of the list its niche came from (your own searches use business rules)
        m_of = (lambda n: 'creators' if n in CREATOR_NICHES else 'business') if mode == 'both' else (lambda n: mode)
        cfuts = []; buf = []; buf_t = [time.time()]
        def flush_buf():   # saving 6,000+ leads takes a moment: save new ones in batches, not one by one
            if buf: add_leads(list(buf)); buf.clear(); sheet_mark()
            buf_t[0] = time.time()
        def on_lead(f, q=None):
            lead = f.result()
            with lock:
                B.checked(q, lead)
                cnt['checked'] += 1
                if cnt['checked'] % 10 == 0: self.ev('checked', cnt['checked'])
                if not lead: return
                found.append(lead); self.ev('lead', lead)
                buf.append(lead)
                if len(buf) >= 15 or time.time() - buf_t[0] > 3: flush_buf()
            if not lead.get('email') and (lead.get('website') or 'linktr' in (lead.get('about') or '')): ep.submit(enrich_bg, lead)
        def do_search(niche, q, tok, dur, page):
            """one search + its subscriber prefilter, all inside the worker (so searches never queue behind each other)"""
            if self.stop or self.quota_out: return niche, q, [], [], None, dur, page
            B.ran(q)
            with _SPACE_LK:   # at most ~1 search a second (6,000 units/min): Google's per-minute limit never trips
                w = _SPACE_T[0] + _GAP[0] - time.time()
                if w > 0: time.sleep(w)
                _SPACE_T[0] = time.time()
            cids, nxt = self._search(q, after, m_of(niche), 50, dur, tok); _search_ok()
            with lock: new = [c for c in dict.fromkeys(cids) if c not in claimed]; claimed.update(new)
            new = self._prefilter(new, self.creator(m_of(niche), niche), S) if new else []
            return niche, q, cids, new, nxt, dur, page
        # creators often post 20+ min videos: alternate medium (4-20 min) and long (20+ min) searches
        from collections import deque
        from concurrent.futures import FIRST_COMPLETED
        queue = deque((n, q, None, 'long' if (n in CREATOR_NICHES and n not in BUSINESS_NICHES and i % 2) else 'medium', 1) for i, (n, q) in enumerate(queries))
        pending = set(); self.quota_out = False; mined_at = [0]; dry_new = [0]; last_quota = [time.time()]; deep = [1]
        def top_up():   # keep every search slot busy: no waiting for a whole "round" to finish
            while queue and len(pending) < 16 and not self.stop and not self.quota_out:
                pending.add(sp.submit(do_search, *queue.popleft()))
        def mine():   # new searches from the best leads found so far (no pause: searches keep running meanwhile)
            with lock: fresh = found[mined_at[0]:]; mined_at[0] = len(found)
            got = self._mine(fresh, used_q)
            for n_, q_, *_ in got: B.learn(q_, n_, 'your best new leads')
            if got: self.log(f'{len(got)} new searches from your best new leads'); B.save()
            queue.extend((n_, q_, None, d_, 1) for n_, q_, _, d_ in got)
            return len(got)
        top_up()
        while not self.stop:
            if not pending:
                if not keep: break
                wait(cfuts); mine()   # out of searches: learn from the last checks before deciding to stop
                if not queue: self.log('no new searches left, stopping'); break
                top_up(); continue
            done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for f in done:
                pending.discard(f)
                niche, q, cids, new, nxt, dur, page = f.result(); cnt['searches'] += 1; deep[0] = max(deep[0], page)
                self.log(f'"{q}"' + (f' (page {page})' if page > 1 else '') + f': {len(cids)} channels, {len(new)} worth checking')
                self.ev('progress', {'i': cnt['searches'], 'n': len(queries) if not keep else 0, 'q': q, 'done': cnt['searches'] if not keep else 0, 'total': len(queries) + 1, 'round': deep[0]})
                for c in new:
                    fu = cp.submit(self._safe_check, c, m_of(niche), niche, q, S); fu.add_done_callback(lambda f, q=q: on_lead(f, q)); cfuts.append(fu)
                dry_new[0] = 0 if new else dry_new[0] + 1
                if keep and nxt and len(cids) >= 20 and len(new) >= 2: queue.append((niche, q, nxt, dur, page + 1))   # next page only if this page still found new people (searches are limited to 10,000/day)
            if self.quota_out: self.log('YouTube quota used up for today, stopping'); break
            if keep:
                if len(found) - mined_at[0] >= 8: mine()
                if time.time() - last_quota[0] > 4:   # fast searches burn quota quickly: check often
                    last_quota[0] = time.time(); left = self._quota_left()
                    if left is not None and left < reserve: self.log(f'stopping: only {left:,} quota left today (keeping {reserve:,} spare)'); break
                if dry_new[0] >= 60: self.log('the last 60 searches found nothing new, stopping'); break
                with lock:
                    if time.time() - buf_t[0] > 3: flush_buf()
            top_up()
        sp.shutdown(wait=False, cancel_futures=True); cp.shutdown(wait=True, cancel_futures=self.stop)   # waits for running checks + their callbacks
        ep.shutdown(wait=not self.stop, cancel_futures=self.stop)   # email lookups finish unless you pressed Stop
        with lock: flush_buf()
        try:
            for q_, n_, src in B.phrases([l for l in found if l.get('tier') == 'HIGH']): B.learn(q_, n_, src)
            B.save()
        except Exception: pass
        try: low_q = (self._quota_left() or 0) < 1000
        except Exception: low_q = False
        self.ev('done', {'checked': cnt['checked'], 'found': len(found), 'stopped': self.stop, 'searches': cnt['searches'], 'quota_out': bool(self.quota_out or low_q),
                         'resets_in_min': ls()._quota_reset_minutes_left() if hasattr(ls(), '_quota_reset_minutes_left') else None})
        return found

    def _quota_left(self):
        try:
            L = ls(); i = int(L.SETTINGS.get('active_key_index', 0) or 0)
            return sum(max(0, L.get_key_quota(k) - L.key_quota_used[k]) for k in range(len(L.API_KEYS)))
        except Exception: return None

    def _mine(self, leads, used):
        """new searches from what your best new leads actually title their videos (real phrases they repeat)"""
        import sr_brain as B
        out = []
        for q, n, src in B.phrases(sorted((l for l in leads if l.get('tier') == 'HIGH'), key=lambda x: -x.get('score', 0))[:15]):
            if q.lower() not in used and B._ok(q): used.add(q.lower()); out.append((n or 'mined', q, None, 'medium'))
        return out[:20]

    def _safe_check(self, c, mode, niche, q, S):
        if self.stop: return None
        try: return self.check(c, mode, niche, q, S)
        except Exception as e: self.log(f'check failed {c}: {e}'); return None

    def _prefilter(self, cids, cr, S):
        """one channels.list call per 50 ids (1 unit) to drop channels outside the subscriber range"""
        if not cids: return []
        L = ls(); lo, hi = self.sub_range(S, cr); keep = []
        for i in range(0, len(cids), 50):
            chunk = cids[i:i + 50]
            try:
                r = L.execute_with_rotation(lambda yt, ids=','.join(chunk): yt.channels().list(part='snippet,statistics,brandingSettings', id=ids, maxResults=50).execute(), 1)
                stats = {}
                self._handles = getattr(self, '_handles', {})
                for it in (r or {}).get('items', []):
                    stats[it['id']] = it.get('statistics', {}); self._handles[it['id']] = (it.get('snippet') or {}).get('customUrl', '')
                    try: self._seed(it)   # full channel info from the same 1-unit call, so the check needs no extra request
                    except Exception: pass
            except Exception: keep += chunk; continue
            for c in chunk:
                st = stats.get(c)
                if st is None: continue
                h = (getattr(self, '_handles', {}) or {}).get(c, '')
                if h and h.lower() in getattr(self, 'sheet_handles', set()): continue   # already in your sheet
                if st.get('hiddenSubscriberCount'): keep.append(c); continue
                try: n = int(st.get('subscriberCount') or 0)
                except Exception: n = 0
                if lo <= n <= hi: keep.append(c)
        return keep

    def _seed(self, ch):
        L = ls(); sn = ch.get('snippet', {}); st = ch.get('statistics', {}); br = ch.get('brandingSettings', {}).get('channel', {})
        th = sn.get('thumbnails', {}); photo = (th.get('high') or th.get('medium') or th.get('default') or {}).get('url', '')
        L.save_cached_channel(ch['id'], {'title': sn.get('title', 'N/A'), 'description': sn.get('description', ''), 'custom_url': sn.get('customUrl', 'N/A'),
            'published_at': sn.get('publishedAt', 'N/A'), 'country': sn.get('country', 'N/A'), 'subscriber_count': st.get('subscriberCount', 'Hidden'),
            'video_count': st.get('videoCount', '0'), 'view_count': st.get('viewCount', '0'), 'keywords': br.get('keywords', ''), 'channel_id': ch['id'],
            'featured_channels': br.get('featuredChannelsUrls', []), 'photo': photo})

    def videos(self, cid, n=15):
        """same output as the old get_recent_videos_detailed, minus its global lock (which made every channel wait in one line)"""
        L = ls()
        c = L.load_cached_videos(cid, n)
        if c is not None: return c
        try:
            pr = L.execute_with_rotation(lambda yt: yt.playlistItems().list(part='contentDetails', playlistId='UU' + cid[2:], maxResults=n).execute(), 1)
        except Exception as e:
            if '404' in str(e) or 'playlistNotFound' in str(e): L.save_cached_videos(cid, n, []); return []
            raise
        ids = [i['contentDetails']['videoId'] for i in (pr or {}).get('items', []) if i.get('contentDetails', {}).get('videoId')]
        if not ids: L.save_cached_videos(cid, n, []); return []
        vr = L.execute_with_rotation(lambda yt: yt.videos().list(part='snippet,statistics,contentDetails', id=','.join(ids)).execute(), 1)
        out = []
        for v in (vr or {}).get('items', []):
            dur = L.parse_duration(v['contentDetails'].get('duration', 'PT0S')); sn = v['snippet']; d = sn.get('description', '')
            live = sn.get('liveBroadcastContent', 'none') in ('live', 'upcoming') and dur == 0
            if live: dur = 3600
            out.append({'video_id': v['id'], 'title': sn['title'], 'url': f"https://www.youtube.com/watch?v={v['id']}", 'published_raw': sn['publishedAt'],
                        'published': sn['publishedAt'][:10], 'views': v['statistics'].get('viewCount', '0'), 'likes': v['statistics'].get('likeCount', '0'),
                        'comments': v['statistics'].get('commentCount', '0'), 'duration_seconds': dur, 'duration': L.format_duration_human(dur),
                        'thumbnail': sn['thumbnails'].get('medium', {}).get('url', ''), 'description': d[:1000], 'tags': sn.get('tags', [])[:20], 'was_live': live})
        out.sort(key=lambda x: x['published_raw'], reverse=True)
        if out: L.save_cached_videos(cid, n, out)
        return out

    def _check_many(self, cids, mode, niche, q, S):
        """checks channels 4 at a time (same as old SwipeReach's worker count), yields (cid, lead|None) as they finish"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def one(c):
            if self.stop: return c, None
            try: return c, self.check(c, mode, niche, q, S)
            except Exception as e: self.log(f'check failed {c}: {e}'); return c, None
        with ThreadPoolExecutor(max_workers=48) as ex:
            futs = [ex.submit(one, c) for c in cids]
            for f in as_completed(futs):
                yield f.result()

    def similar(self, cid, S=None, limit=40):
        """lookalikes of a lead you liked: its featured channels + topic searches (old SwipeReach 'Discover similar')"""
        L = ls(); S = dict(load_settings(), **(S or {}))
        seed = next((l for l in load_leads() if l['channel_id'] == cid), {}); info = L.get_channel_info(cid) or {}
        vids = self.videos(cid, 15) or []
        existing = {l['channel_id'] for l in load_leads()}
        try: processed = set(L.load_processed_ids())
        except Exception: processed = set()
        ids = []
        for f in info.get('featured_channels') or []:
            m = re.search(r'UC[\w-]{22}', f)
            if m: ids.append(m.group(0))
        try: qs = L.extract_topic_queries(info, [v.get('title', '') for v in vids], [])[1:1 + max(2, int(S.get('max_searches', 12)) // 2)]   # [0] is their own name
        except Exception: qs = []
        after = (_now() - timedelta(days=int(S['days_recent']))).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.log(f'similar to {info.get("title")}: {len(ids)} featured channels, {len(qs)} searches')
        mode, niche = seed.get('mode', 'business'), seed.get('niche', 'similar')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:   # all topic searches at once
            for i, r in enumerate(ex.map(lambda q: self._search(q, after, mode, 50)[0], qs)):
                ids += r; self.ev('progress', {'i': i + 1, 'n': len(qs), 'q': qs[i], 'done': i + 1, 'total': len(qs) + 1})
        cand = [c for c in dict.fromkeys(ids) if c != cid and c not in existing and c not in processed]
        cr = self.creator('creators' if mode == 'creators' else 'business', niche)
        cand = self._prefilter(cand, cr, S)[:max(limit, 120)]
        found, checked = [], 0
        for c, lead in self._check_many(cand, 'business' if mode == 'growing' else mode, niche, 'similar to ' + (info.get('title') or ''), S):
            checked += 1
            if lead:
                found.append(lead); self.ev('lead', lead)
                add_lead(lead); sheet_mark()
            if self.stop: break
        self.ev('done', {'checked': checked, 'found': len(found), 'stopped': self.stop})
        return found

    def _search(self, q, after, mode, n, dur='medium', page=None):
        """returns (channel ids, next page token)"""
        L = ls()
        def req(yt):   # one search costs 100 units whatever maxResults is, so always take 50
            kw = dict(part='snippet', q=q, type='video', maxResults=50, publishedAfter=after,
                      order='viewCount' if mode == 'growing' else 'relevance', relevanceLanguage='en', videoDuration=dur)
            if page: kw['pageToken'] = page
            return yt.search().list(**kw).execute()
        try: r = L.execute_with_rotation(req, 100)
        except Exception as e:
            self.log(f'search failed: {e}')
            if re.search(r'(?i)quota|exhaust|all keys', str(e)): self.quota_out = True
            return [], None
        out = []
        for it in (r or {}).get('items', []):
            c = (it.get('snippet') or {}).get('channelId')
            if c and c not in out: out.append(c)
        return out, (r or {}).get('nextPageToken')

    @staticmethod
    def creator(mode, niche): return mode == 'creators' or (mode == 'growing' and niche in CREATOR_NICHES and niche not in BUSINESS_NICHES)
    @staticmethod
    def sub_range(S, cr): return (int(S['cr_min_subs']), int(S['cr_max_subs'])) if cr else (int(S['min_subs']), int(S['max_subs']))

    def check(self, cid, mode, niche, q, S):
        L = ls(); cr = self.creator(mode, niche)
        info = L.get_channel_info(cid)
        if not info: return None
        try: subs = int(info.get('subscriber_count') or 0)
        except Exception: subs = 0
        lo, hi = self.sub_range(S, cr)
        if subs and not (lo <= subs <= hi): return None
        vids = self.videos(cid, 15) or []
        a = analyze(info, vids, mode, {'creator': cr, 'min_views': S.get('cr_min_views', 0), 'need_money': S.get('cr_need_money', True)} if cr else None)
        if a['tier'] in ('EXCLUDED', 'LOW') or (S.get('min_tier') == 'HIGH' and a['tier'] != 'HIGH'): return None
        return lead_from(info, vids, a, mode, niche, q)
