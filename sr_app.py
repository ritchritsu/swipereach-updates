"""SwipeReach Studio - desktop app (pywebview). Start with Start_SwipeReach.bat"""
import sys, os
FROZEN = getattr(sys, 'frozen', False)
if FROZEN and len(sys.argv) > 1 and sys.argv[1] in ('-m', '-c'): sys.exit(0)   # the installed .exe was asked to run "python -m pip ..." / "python -c": never open another window
if FROZEN:   # only one SwipeReach at a time
    import ctypes; _mx = ctypes.windll.kernel32.CreateMutexW(None, False, 'SwipeReachStudioSingle')
    if ctypes.windll.kernel32.GetLastError() == 183: sys.exit(0)
for _n in ('stdout', 'stderr'):
    if getattr(sys, _n, None) is None: setattr(sys, _n, open(os.devnull, 'w', encoding='utf-8'))
import json, time, re, threading, shutil, urllib.request, webbrowser, logging, traceback
import sr_core as C, sr_compose as X, sr_sheet as SH, base64
import sr_boost as BO
import webview
HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(C.DATA, exist_ok=True)
logging.basicConfig(filename=os.path.join(C.DATA, 'swipereach.log'), level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('sr')
WIN = None
API = None
def _OPEN():   # pywebview 5/6 renamed this
    fd = getattr(webview, 'FileDialog', None)
    return fd.OPEN if fd is not None else getattr(webview, 'OPEN_DIALOG', 10)

_PQ = []; _PL = threading.Lock(); _PT = [None]
def push(kind, data):
    """events for the page are queued and sent together 5x a second: a fast search makes hundreds per second, and sending
    each one to the window separately would make the search wait on the screen"""
    try: item = '[%s,%s]' % (json.dumps(kind), json.dumps(data, ensure_ascii=False))
    except Exception: return
    with _PL:
        _PQ.append(item)
        if _PT[0] is None or not _PT[0].is_alive():
            _PT[0] = threading.Thread(target=_push_loop, daemon=True); _PT[0].start()
def _push_loop():
    while True:
        time.sleep(0.2)
        with _PL:
            batch = _PQ[:]; _PQ.clear()
        if not batch: continue
        try: WIN.evaluate_js('SR.many([%s])' % ','.join(batch))
        except Exception: pass

class Api:
    def __init__(self): self.run = None
    def state(self):
        S = C.load_settings(); L = C.leads_ro()
        counts = {}
        for l in L: counts[l.get('status', 'new')] = counts.get(l.get('status', 'new'), 0) + 1
        groups = {m: {g: {n: len(C.ALL_NICHES.get(n, q)) for n, q in ns.items()} for g, ns in gs.items()} for m, gs in C.NICHES.items()}
        due = sum(1 for l in L if X.due(l) is not None)
        try: due += sum(1 for d in self.dm_followups() if d.get('due'))
        except Exception: pass
        return {'settings': S, 'groups': groups, 'counts': counts, 'total': len(L), 'due': due, 'running': bool(self.run)}
    def setup_status(self):
        S = C.load_settings(); cs = X.claude_status()
        ol = False
        try: urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=0.6); ol = True
        except Exception: pass
        return {'claude': cs, 'model': X.CLAUDE_MODEL, 'selfie': bool(S.get('selfie_path') and os.path.exists(S['selfie_path'])),
                'monitor': bool(S.get('monitor') and len(S['monitor']) == 4),
                'gmail_client': os.path.exists(os.path.join(C.DATA, 'gmail_client.json')), 'gmail_token': os.path.exists(os.path.join(C.DATA, 'gmail_token.json')),
                'ollama': ol, 'capacity': S.get('capacity', 'OPEN'), 'gmail_account': S.get('gmail_account') or X._ME.get('e') or ''}
    def install_claude(self):
        try: return X.install_claude()
        except Exception as e: return {'error': str(e)}
    def import_gmail_client(self):
        r = WIN.create_file_dialog(_OPEN(), file_types=('Google OAuth client (*.json)',))
        if not r: return None
        p = r[0] if isinstance(r, (list, tuple)) else r
        try:
            d = json.load(open(p, encoding='utf-8'))
            if 'installed' not in d and 'web' not in d: return {'error': 'That is not an OAuth client file (it should contain "installed")'}
            shutil.copy(p, os.path.join(C.DATA, 'gmail_client.json')); return {'ok': True}
        except Exception as e: return {'error': str(e)}
    def connect_gmail(self):
        try:
            X._GM['interactive'] = True; X._GM.pop('cr', None); X.gmail(); return {'ok': True}
        except Exception as e: return {'error': str(e)}
        finally: X._GM['interactive'] = False
    def find(self, mode, niches, custom, settings, growing=False):
        if growing: mode = 'growing'
        if self.run: return {'error': 'A search is already running'}
        S = C.load_settings(); S.update({k: v for k, v in (settings or {}).items() if v not in (None, '')}); C.save_settings(S)
        self.run = C.Run(push); C.SEARCHING[0] = 1
        def go():
            try:
                C.ls()   # loads the YouTube engine (first time takes a few seconds)
                t0 = time.time(); found = self.run.find(mode, niches or [], [q for q in (custom or '').splitlines() if q.strip()], S)
                if time.time() - t0 > 600: BO.notify('Search finished', f"{len(found or [])} new leads found. Open SwipeReach > Leads > Today.")
            except Exception as e:
                log.error(traceback.format_exc()); push('error', str(e))
                push('done', {'checked': 0, 'found': 0, 'stopped': True})
            finally: self.run = None; C.SEARCHING[0] = 0; self._after_run()
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def _after_run(self):
        try: L = C.ls(); L.save_daily_quota(L.API_QUOTA_USED, L.key_quota_used, L.search_calls_used, force=True)
        except Exception: pass
        push('quota', self.quota())
    def quota(self):
        """today's YouTube usage on the same keys/counters as old SwipeReach"""
        try:
            L = C.ls(); idx = int(getattr(L, 'SETTINGS', {}).get('active_key_index', getattr(L, 'ACTIVE_KEY_INDEX', 0)) or 0)
            keys = [{'label': c.get('label'), 'quota': L.get_key_quota(i), 'used': (L.key_quota_used[i] if i < len(L.key_quota_used) else 0),
                     'search_cap': L.get_search_cap(i), 'searches': (L.search_calls_used[i] if i < len(L.search_calls_used) else 0)} for i, c in enumerate(L.API_KEY_CONFIGS)]
            q = {'active': idx, 'keys': keys}; self._log_usage(q)
            return {'active': idx, 'keys': keys, 'resets_in_min': L._quota_reset_minutes_left() if hasattr(L, '_quota_reset_minutes_left') else None}
        except Exception as e: return {'error': str(e)}
    # ---- YouTube quota grant: when it ends, when to reapply, and a usage history to quote in the renewal form ----
    GRANT_DEFAULT = {'start': '2026-10-22', 'months': 6}
    _last_log = {}
    def _log_usage(self, q):
        """remember the most units / searches used each (Pacific) day, so the renewal form can quote real numbers"""
        try:
            L = C.ls(); day = str(L._quota_today()); used = sum(k['used'] for k in q.get('keys', [])); srch = sum(k['searches'] for k in q.get('keys', []))
            if Api._last_log.get('day') == day and used - Api._last_log.get('used', 0) < 5000: return
            Api._last_log = {'day': day, 'used': used}
            S = C.load_settings(); h = S.setdefault('yt_usage', {})
            if used > h.get(day, {}).get('units', -1): h[day] = {'units': used, 'searches': srch}
            for d in sorted(h)[:-400]: h.pop(d, None)
            C.save_settings(S)
        except Exception: pass
    def quota_grant(self):
        S = C.load_settings(); g = dict(Api.GRANT_DEFAULT, **(S.get('yt_grant') or {}))
        try:
            start = C.datetime.fromisoformat(g['start']).date(); months = int(g['months'])
            end = start.replace(year=start.year + (start.month - 1 + months) // 12, month=(start.month - 1 + months) % 12 + 1)
        except Exception: return {'error': 'Bad date'}
        today = C._now().date(); left = (end - today).days; apply_from = end - C.timedelta(days=90)
        h = S.get('yt_usage') or {}; vals = [v['units'] for v in h.values() if v.get('units')]
        return {'start': start.isoformat(), 'months': months, 'end': end.isoformat(), 'days_left': left, 'apply_from': apply_from.isoformat(),
                'apply_now': today >= apply_from, 'days': len(vals), 'avg_units': int(sum(vals) / len(vals)) if vals else 0, 'peak_units': max(vals) if vals else 0,
                'avg_searches': int(sum(v.get('searches', 0) for v in h.values()) / len(h)) if h else 0}
    def set_quota_grant(self, start, months):
        try: C.datetime.fromisoformat(start); months = int(months)
        except Exception: return {'error': 'Use a date like 2026-10-22'}
        S = C.load_settings(); S['yt_grant'] = {'start': start, 'months': months}; C.save_settings(S); return self.quota_grant()
    def search_brain(self):
        try:
            import sr_brain; return sr_brain.summary()
        except Exception as e: return {'error': str(e)}
    # =============== boosters (sr_boost) ===============
    def find_emails(self):
        """look for missing emails on link-in-bio pages and their websites (no YouTube quota), in the background"""
        if BO._JOB['running']: return {'busy': True}
        def go():
            try:
                r = BO.find_emails(progress=lambda d: push('emails', d), pause=lambda: bool(API and API.run)); push('emails_done', r)
            except Exception as e: log.error(traceback.format_exc()); push('emails_done', {'error': str(e)})
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def stop_emails(self): BO._JOB['stop'] = True; return True
    def check_emails(self):
        def go():
            try: push('checked_emails', BO.check_all())
            except Exception as e: push('checked_emails', {'error': str(e)})
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def email_check(self, cid):
        l = self._lead(cid) or {}
        if not l.get('email'): return {'ok': None, 'why': 'no email'}
        r = BO.check_email(l['email']); self._save(cid, email_check=r); return r
    def warmup(self): return BO.warmup()
    def set_warmup(self, start):
        try: return BO.set_warmup(start or None)
        except Exception: return {'error': 'Use a date like 2026-10-31'}
    def lookalikes(self):
        if self.run: return {'error': 'A search is already running'}
        self.run = C.Run(push); C.SEARCHING[0] = 1
        def go():
            try: C.ls(); BO.lookalikes(self.run)
            except Exception as e: log.error(traceback.format_exc()); push('error', str(e)); push('done', {'checked': 0, 'found': 0, 'stopped': True})
            finally: self.run = None; C.SEARCHING[0] = 0; self._after_run()
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def radar(self):
        try: return BO.radar()
        except Exception as e: return {'error': str(e), 'posts': []}
    def price(self, cid): return BO.price(self._lead(cid) or {})
    def ntfy(self, create=False):
        t = BO.ntfy_topic(create); return {'topic': t, 'url': f'https://ntfy.sh/{t}' if t else ''}
    def ntfy_test(self):
        return {'ok': BO.notify('SwipeReach', 'Phone alerts work! You will get one here when someone replies.')}
    def ntfy_off(self):
        S = C.load_settings(); S.pop('ntfy_topic', None); C.save_settings(S); return True
    def reply_draft(self, cid):
        l = self._lead(cid) or {}
        try:
            r = BO.reply_draft(l)
            if r.get('draft_id'): self.open_drafts()
            return r
        except Exception as e: log.error(traceback.format_exc()); return {'error': str(e)}
    def sample_kit(self, cid):
        l = self._lead(cid) or {}
        def go():
            try:
                r = BO.sample_kit(l, log=lambda m: push('log', m))
                if r.get('folder'):
                    try: os.startfile(r['folder'])
                    except Exception: pass
                push('kit', dict(r, title=l.get('title')))
            except Exception as e: log.error(traceback.format_exc()); push('kit', {'error': str(e), 'title': l.get('title')})
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def spam_check(self, subject, body, img=False): return BO.spam_check(subject, body, img)
    def schedule_send(self, cid, subject, body, with_selfie, style='best'):
        """make the Gmail draft now, send it at ~9am their time (the app has to be open then)"""
        l = self._lead(cid) or {}
        if not l.get('email'): return {'error': 'Add their email first'}
        r = self.draft(cid, subject, body, with_selfie, style, True)
        if not r.get('ok'): return r
        o = (self._lead(cid) or {}).get('outreach') or {}
        if r.get('test') or not o.get('draft_id'): return {'error': 'Scheduling needs a real lead (not a test to yourself)'}
        at = BO.their_morning(l); BO.schedule(cid, o['draft_id'], at)
        return {'ok': True, 'at': at.isoformat(), 'local': at.astimezone().strftime('%a %d %b, %I:%M %p'), 'country': l.get('country') or 'unknown (US time)'}
    def unschedule(self, cid): BO.unschedule(cid); return True
    def send_queue(self): return BO.queue()
    def insights(self): return BO.insights()
    def today(self): return BO.today()
    def dm_backups(self): return BO.dm_backups()
    def dm_backup_done(self, cid): BO.dm_done(cid); return True
    def extras(self, cid):
        l = self._lead(cid) or {}
        return {'sample': BO.sample_for(l), 'proof': BO.proof_for(l), 'price': BO.price(l), 'check': l.get('email_check'),
                'scheduled': next((x for x in BO.queue() if x['cid'] == cid), None)}
    def save_lists(self, samples, testimonials):
        S = C.load_settings(); S['samples'] = samples or ''; S['testimonials'] = testimonials or ''; C.save_settings(S); return True
    def lists(self):
        S = C.load_settings(); return {'samples': S.get('samples') or '', 'testimonials': S.get('testimonials') or ''}
    def testimonial_due(self): return BO.testimonial_due()
    def testimonial_asked(self, cid): self._save(cid, testimonial_asked=C._now().isoformat()[:10]); return True
    def client_care(self): return BO.client_care()
    def weekly(self): return BO.weekly()
    def stop(self):
        if self.run: self.run.stop = True
        return True
    HEAVY = ('transcript', 'video_snips', 'about', 'video_titles', 'transcript_vid', '_sheet', 'fields', 'proof', 'email_hidden_check')
    def leads(self):
        """the list only needs the summary: the big text (transcript, about, video text) stays behind until you open a lead"""
        return [{k: v for k, v in l.items() if k not in Api.HEAVY} for l in C.leads_ro()]
    _SLIM = [None, None]
    @staticmethod
    def _slim(l): return {k: x for k, x in l.items() if k not in Api.HEAVY}
    def leads_v(self, v=None):
        """the list only when it changed since version v; after a click on one lead, only that lead (30,000 leads is ~40MB: never resend it for one change)"""
        L = C.leads_ro(); cur = str(C._RO_N[0])
        if v == cur: return {'same': True, 'v': cur}
        try:
            v0 = int(v); ch = [c for n, c in C._CHG if n > v0]
            if v0 >= C._CHG[0][0] - 1 and ch and None not in ch and len(ch) == C._RO_N[0] - v0:
                idx = C._index(); return {'v': cur, 'delta': [Api._slim(idx[c]) for c in dict.fromkeys(ch) if c in idx]}
        except Exception: pass
        if Api._SLIM[0] != cur: Api._SLIM[:] = [cur, [Api._slim(l) for l in L]]
        return {'v': cur, 'leads': Api._SLIM[1]}
    def lead(self, cid):
        return self._lead(cid) or {}
    def set_status(self, cid, status):
        x = C.leads_ro() and C._index().get(cid)
        if not x: return True
        lead = C.patch(cid, status=status, history=(x.get('history') or []) + [{'t': C._now().isoformat()[:19], 'status': status}])
        lead = dict(lead) if lead else None
        if lead: self._sheet_bg(lead, status=status)
        return True
    def _sheet_bg(self, lead=None, status=None, contact=None):
        SH.AUTO.mark()   # batched two-way sync with the SwipeReach Studio tab a few seconds later
    def add_manual(self, d):
        """track a lead that didn't come from Find (YT Jobs post, Discord, referral...)"""
        import uuid
        d = d or {}; now = C._now().isoformat()[:19]; st = d.get('status') or 'contacted'
        l = {'id': 'm-' + uuid.uuid4().hex[:10], 'channel_id': 'm-' + uuid.uuid4().hex[:10], 'title': (d.get('title') or 'Untitled').strip()[:120],
             'url': (d.get('url') or '').strip(), 'photo': '', 'mode': 'manual', 'niche': d.get('niche') or '', 'source': d.get('source') or 'Other',
             'subs': 0, 'median_views': 0, 'score': 0, 'tier': 'MANUAL', 'reasons': [d['notes']] if d.get('notes') else [], 'email': (d.get('email') or '').strip(),
             'status': st, 'history': [{'t': now, 'status': st}], 'added': now, 'draft': {'subject': d.get('subject') or '', 'body': d.get('body') or ''}}
        C.add_lead(l); return l
    def set_source(self, cid, source):
        self._save(cid, source=source); return True
    def save_sources(self, sources):
        S = C.load_settings(); S['sources'] = sources; C.save_settings(S); return True
    def save_prefs(self, prefs):
        S = C.load_settings(); f = (prefs or {}).pop('filters', None) or {}
        S.update({k: v for k, v in f.items() if k in ('min_subs', 'max_subs', 'min_tier', 'max_searches', 'cr_min_subs', 'cr_max_subs', 'cr_min_views') and v not in (None, '', 0)})
        for b in ('sheet_push', 'cr_need_money'):
            if b in f: S[b] = bool(f[b])
        S['ui'] = prefs; C.save_settings(S); return True
    def prefetch(self, cid, imgs=True, transcript=True):
        """when you open a lead: grab its transcript in the background so Write with AI starts straight away"""
        def go():
            try:
                l = self._lead(cid)
                if transcript and not X._GM.get('cr') and os.path.exists(os.path.join(C.DATA, 'gmail_token.json')):
                    threading.Thread(target=lambda: X.signature(), daemon=True).start()   # Gmail login + your signature ready before you click Open in Gmail
                if l and imgs: X.prefetch_frames(l)   # frames start grabbing right away, in parallel with the transcript
                if l and transcript and l.get('transcript') is None:
                    X.lead_transcript(l); self._save(cid, transcript=l.get('transcript'), transcript_vid=l.get('transcript_vid'))
            except Exception: pass
        threading.Thread(target=go, daemon=True).start(); return True
    def docs(self):
        X.notes()
        out = {}
        for n, lab in (('master_prompt.txt', 'Master prompt'), ('ritch_master_doc.txt', 'Ritch master doc')):
            p = os.path.join(X.DOCS, n); txt = X._doc(n, X.MASTER if n == 'master_prompt.txt' else None)
            out[n] = {'label': lab, 'custom': os.path.exists(p), 'words': len(txt.split()),
                      'updated': time.strftime('%b %d', time.localtime(os.path.getmtime(p))) if os.path.exists(p) else 'built-in copy'}
        out['notes'] = X.notes(); return out
    def replace_doc(self, name):
        if name not in ('master_prompt.txt', 'ritch_master_doc.txt'): return {'error': 'unknown doc'}
        r = WIN.create_file_dialog(_OPEN(), file_types=('Text (*.txt;*.md)', 'All files (*.*)'))
        if not r: return None
        p = r[0] if isinstance(r, (list, tuple)) else r
        try:
            if p.lower().endswith('.docx'):
                import zipfile; txt = re.sub(r'<[^>]+>', ' ', zipfile.ZipFile(p).read('word/document.xml').decode('utf-8', 'ignore').replace('</w:p>', '\n'))
            else: txt = open(p, encoding='utf-8', errors='ignore').read()
            if len(txt.split()) < 200: return {'error': 'That file looks too short to be your doc'}
            os.makedirs(X.DOCS, exist_ok=True); open(os.path.join(X.DOCS, name), 'w', encoding='utf-8').write(txt)
            return {'ok': True, 'words': len(txt.split())}
        except Exception as e: return {'error': str(e)}
    def save_notes(self, txt):
        os.makedirs(X.DOCS, exist_ok=True); open(os.path.join(X.DOCS, 'my_notes.txt'), 'w', encoding='utf-8').write(txt or ''); return True
    def rescore(self):
        try: return {'changed': C.rescore_leads()}
        except Exception as e: return {'error': str(e)}
    def dm_followups(self):
        """leads you message outside Gmail (Discord, X, YT Jobs chat): which follow-up is due today + ready-to-paste text"""
        out = []
        for l in C.leads_ro():   # read only
            o = l.get('outreach') or {}
            if o.get('thread_id') or l.get('status') not in ('contacted', 'followup'): continue
            t0 = o.get('sent_at') or next((h['t'] for h in l.get('history', []) if h.get('status') in ('contacted', 'drafted')), None) or l.get('added')
            fus = o.get('followups', []); n = len(fus)
            try:
                last = C.datetime.fromisoformat((fus[-1] if fus else t0).replace('Z', '+00:00'))
                if last.tzinfo is None: last = last.replace(tzinfo=C.timezone.utc)
            except Exception: continue
            if n >= 3:
                if C._now() - last >= _d(2): out.append({'channel_id': l['channel_id'], 'title': l['title'], 'done': True})
                continue
            due_in = (last + _d(X.STAGE_A[n]) - C._now()).total_seconds() / 3600
            out.append({'channel_id': l['channel_id'], 'title': l['title'], 'source': l.get('source') or 'Other', 'url': l.get('url'), 'n': n,
                        'due': due_in <= 2, 'due_in_h': round(due_in), 'text': X.fu_text(n, l.get('first_name') or '', 'warm' if l.get('email') else 'dm')})
        return out
    def dm_sent(self, cid):
        """you sent the follow-up yourself in Discord/X/etc: log it so the next one is timed from now"""
        def f(L):
            for l in L:
                if l['channel_id'] == cid:
                    o = l.setdefault('outreach', {}); o.setdefault('sent_at', next((h['t'] for h in l.get('history', []) if h.get('status') == 'contacted'), C._now().isoformat()))
                    o.setdefault('followups', []).append(C._now().isoformat()); l['status'] = 'followup'
                    if len(o['followups']) >= 3: o['last_fu'] = C._now().isoformat()
                    return dict(l)
        l = C.mutate(f)
        if l: self._sheet_bg(l, status='followup')
        return True
    # ---------- Flick-style compose ----------
    def fields(self, cid, engine='auto'):
        l = self._lead(cid)
        if not l: return {'error': 'lead not found'}
        try:
            d = X.write_fields(l, engine or 'auto')
            self._save(cid, fields_sugg=d['fields'], transcript=l.get('transcript'), transcript_vid=l.get('transcript_vid'))
            if d['fields'].get('name') and d['fields']['name'][0] and not l.get('first_name'):
                self._save(cid, first_name=d['fields']['name'][0], name_guess={'name': d['fields']['name'][0], 'conf': 3, 'src': 'Claude'})
            return d
        except Exception as e: log.error(traceback.format_exc()); return {'error': str(e)}
    def email_template(self):
        return {'template': X.template_text(), 'subjects': X.FLICK_SUBJECTS, 'default': X.FLICK_TEMPLATE}
    def save_template(self, txt):
        os.makedirs(X.DOCS, exist_ok=True)
        p = os.path.join(X.DOCS, 'email_template.txt')
        if not txt or txt.strip() == X.FLICK_TEMPLATE.strip():
            try: os.replace(p, p + '.old')
            except Exception: pass
        else: open(p, 'w', encoding='utf-8').write(txt)
        return True
    def selfie_sources(self, cid):
        l = self._lead(cid) or {}
        return {'sources': [{'id': s['id'], 'label': s['label'], 'url': (s['urls'][-1] if s['urls'] else None), 'hi': (s['urls'][0] if s['urls'] else None)} for s in X.source_urls(l)],
                'params': l.get('selfie_params') or {}, 'fx': dict(X.FX_DEFAULT, **(C.load_settings().get('selfie_fx') or {}))}
    def selfie_preview(self, cid, params, w=1200):
        """the exact final look (camera match, grain, falloff) at editor size, so what you see is what gets sent"""
        l = self._lead(cid)
        if not l: return {'error': 'lead not found'}
        try:
            p = X.selfie(l, params=params or {}, preview_w=int(w or 1200))
            return {'img': 'data:image/jpeg;base64,' + base64.b64encode(open(p, 'rb').read()).decode()}
        except Exception as e: return {'error': str(e)}
    def set_img(self, cid, mode, src=None, make_default=False):
        """pick what goes in the email: 'selfie' (your desk photo with their video on screen) or 'frame' (just a frame from their video)"""
        l = self._lead(cid) or {}; P = dict(l.get('selfie_params') or {}, mode=mode)
        if src: P['src'] = src
        self._save(cid, selfie_params=P)
        if make_default: S = C.load_settings(); S['img_mode'] = mode; C.save_settings(S)
        return {'ok': True}   # the picture itself is rendered by selfie_render (so switching is instant)
    def fit_picture(self, cid, body):
        return {'body': X.fit_picture(self._lead(cid) or {}, body)}
    def selfie_src_img(self, cid, sid, small=False):
        """a real video frame (or a still with its black bars cut off) for the editor, as an image the page can show"""
        l = self._lead(cid) or {}
        try:
            b = X.real_frame(l, int(sid[5:])) if str(sid).startswith('frame') else None
            if not b and not str(sid).startswith('frame'):
                src = next((x for x in X.source_urls(l) if x['id'] == sid), None); b = X._fetch(src['urls']) if src else None
                b = X._crop_bars(b) if b and sid != 'thumb' else b
            return {'img': 'data:image/jpeg;base64,' + base64.b64encode(X.small_jpg(b) if small else b).decode()} if b else {'error': ('YouTube blocked frames for this video. Use the Thumbnail or another picture' if str(sid).startswith('frame') else 'Not available for this video')}
        except Exception as e: return {'error': str(e)}
    def save_selfie_fx(self, fx):
        S = C.load_settings(); S['selfie_fx'] = fx or {}; C.save_settings(S); return True
    def _gmail_account(self):
        """which Google account to open Gmail in: the one you set in Setup, else the one connected for drafts/tracking (remembered)"""
        S = C.load_settings(); a = (S.get('gmail_account') or '').strip()
        if a: return a
        a = X._ME.get('e') or ''
        if not a and os.path.exists(os.path.join(C.DATA, 'gmail_token.json')):
            try: a = X._me(X.gmail())
            except Exception: a = ''
        if a: S['gmail_account'] = a; C.save_settings(S)
        return a
    def set_signature(self, html):
        """your chosen signature (pasted in Setup). Gmail's API only hands apps the DEFAULT signature, so a 2nd one has to come from here"""
        import html as H
        h = (html or '').strip()
        h = re.sub(r'(?is)<(script|style|iframe|object|embed)[^>]*>.*?</\1>', '', h); h = re.sub(r'''(?i)\son\w+\s*=\s*("[^"]*"|'[^']*')''', '', h)
        S = C.load_settings(); S['signature_html'] = h; C.save_settings(S); X._SIG.clear(); return True
    def get_signature(self):
        S = C.load_settings(); h = S.get('signature_html') or ''
        if not h:
            try: h = X.signature()
            except Exception: h = ''
        return {'html': h, 'custom': bool(S.get('signature_html'))}
    def open_drafts(self, auto=False):
        # auto=True (opened by a follow-up, not by you clicking): only once per 10 minutes, so Gmail tabs don't pile up
        if auto:
            if time.time() - getattr(Api, '_drafts_t', 0) < 600: return False
        Api._drafts_t = time.time()
        au = self._gmail_account(); au = f'?authuser={au}' if au else ''
        threading.Thread(target=lambda: webbrowser.open(f'https://mail.google.com/mail/{au}#drafts'), daemon=True).start(); return True
    def set_gmail_account(self, a):
        S = C.load_settings(); S['gmail_account'] = (a or '').strip(); C.save_settings(S); return True
    def open_in_gmail(self, cid, subject, body, with_selfie, style='flick', force=False):
        """instant, like the old SwipeReach: Gmail's compose window opens straight away with everything filled in,
        and the picture is put on your clipboard a moment later, so you click where it says and press Ctrl+V.
        (Setting gmail_mode='draft' instead makes a real Gmail draft with the picture already inside; slower.)"""
        l = self._lead(cid) or {}
        if not l.get('email'): return {'error': 'Add their email first'}
        test = self._is_me(l['email']) if X._GM.get('cr') else False
        if not force and not test:
            ck = l.get('email_check') or {}
            if ck.get('email') != l['email']: ck = dict(BO.check_email(l['email']), email=l['email']); self._save(cid, email_check=ck)
            if ck.get('ok') is False:
                return {'error': f"{l['email']}: {ck.get('why')}. Bounces hurt your sending account. Fix the email, or click again to send anyway.", 'dup': True}
            w = BO.warmup()
            if w.get('on') and w.get('left') == 0:
                return {'error': f"That's today's warm-up limit ({w['limit']} emails). More could get the new account flagged. Click again to send anyway.", 'dup': True}
            em = l['email'].lower()
            dup = next((x for x in C.leads_ro() if x['channel_id'] != cid and (x.get('email') or '').lower() == em and x.get('outreach')), None)
            if dup: return {'error': f"You already emailed {l['email']} for \"{dup['title']}\" (same owner?). Click Open in Gmail again to send anyway.", 'dup': True}
        # Gmail connected: a real draft with the picture already inside (a compose link can't carry a picture).
        # Not connected (or gmail_mode='compose'): compose window + picture on your clipboard to paste.
        if C.load_settings().get('gmail_mode', 'draft') != 'compose' and os.path.exists(os.path.join(C.DATA, 'gmail_token.json')):
            r = self.draft(cid, subject, body, with_selfie, style, True)
            if r.get('ok'):
                mid = r.get('message_id') or ((self._lead(cid) or {}).get('outreach') or {}).get('message_id')
                au = self._gmail_account(); au = f'?authuser={au}' if au else ''
                threading.Thread(target=lambda: webbrowser.open(f"https://mail.google.com/mail/{au}#drafts?compose={mid}" if mid else f'https://mail.google.com/mail/{au}#drafts'), daemon=True).start()
                r['opened'] = 'draft'
            return r
        import urllib.parse as U
        has_img = with_selfie and '[IMAGE]' in (body or '')
        clean = re.sub(r'\n*\[IMAGE\]\n*', '\n\n(press Ctrl+V here for the picture)\n\n' if with_selfie else '\n\n', body or '').strip()
        acct = self._gmail_account()   # your outreach Gmail, not whichever Google account the browser opens by default (e.g. school)
        q = ({'authuser': acct} if acct else {}) | {'view': 'cm', 'fs': '1', 'tf': '1', 'to': l['email'], 'su': subject or '', 'body': clean}
        url = 'https://mail.google.com/mail/?' + U.urlencode(q, quote_via=U.quote)
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()   # the browser opens while we carry on
        if with_selfie:
            def clip():
                try: X.copy_image(X.email_file(l))
                except Exception as e: log.error(f'clipboard picture: {e}')
            threading.Thread(target=clip, daemon=True).start()
        if not test:
            o = dict(l.get('outreach') or {}, subject=subject, style=style or 'ask', draft_at=C._now().isoformat()); o.setdefault('followups', [])
            self._save(cid, status='drafted', draft={'subject': subject, 'body': body}, outreach=o)   # style + subject: right follow-ups, the Gmail check finds the thread, stats by style
        return {'ok': True, 'opened': 'compose', 'test': test, 'acct': acct,
                'note': 'Picture copied: click on "(press Ctrl+V here for the picture)" and paste' if with_selfie else ''}
    def i_sent_it(self, cid):
        """you pressed Send in Gmail: mark it now (Gmail check also confirms it and links the conversation for follow-ups)"""
        if self._is_me((self._lead(cid) or {}).get('email')): return {'ok': True, 'test': True}
        def f(L):
            for l in L:
                if l['channel_id'] == cid:
                    o = l.setdefault('outreach', {}); o.setdefault('sent_at', C._now().isoformat()); o.setdefault('followups', [])
                    l['status'] = 'contacted'; l.setdefault('history', []).append({'t': C._now().isoformat()[:19], 'status': 'contacted'}); return dict(l)
        l = C.mutate(f)
        if l: self._sheet_bg(l)
        threading.Thread(target=lambda: (time.sleep(20), self.sync_gmail()), daemon=True).start()   # pick up the thread for follow-ups
        return True
    def selfie_pick_file(self):
        r = WIN.create_file_dialog(_OPEN(), file_types=('Images (*.jpg;*.jpeg;*.png;*.webp)',))
        if not r: return None
        p = r[0] if isinstance(r, (list, tuple)) else r
        ext = os.path.splitext(p)[1].lower().strip('.') or 'jpeg'
        return {'src': 'file:' + p, 'img': f'data:image/{"jpeg" if ext == "jpg" else ext};base64,' + base64.b64encode(open(p, 'rb').read()).decode()}
    def selfie_render(self, cid, params, make_default=False):
        """final full-quality render (what goes in the email) with your corners, image and realism settings"""
        l = self._lead(cid)
        try:
            p = X.selfie(l, params=params or {})
            self._save(cid, selfie_params=params or {})
            if make_default:
                S = C.load_settings()
                if (params or {}).get('quad'): S['monitor'] = params['quad']
                if (params or {}).get('fx'): S['selfie_fx'] = params['fx']
                C.save_settings(S)
            src = (dict(l.get('selfie_params') or {}, **(params or {}))).get('src') or 'frame1'
            up = str(src).startswith('frame') and not X.frame_ready(l, int(src[5:] or 1))   # showing YouTube's still for now, real frame on its way
            threading.Thread(target=lambda: X.email_jpg(p), daemon=True).start()   # email-sized copy ready before you click Open in Gmail
            return {'path': p, 'img': 'data:image/jpeg;base64,' + base64.b64encode(open(p, 'rb').read()).decode(), 'upgrade': up, 'src': src}
        except Exception as e: return {'error': str(e)}
    def find_email(self, cid):
        l = self._lead(cid) or {}
        e = C.site_email(l.get('website') or '')
        if e: self.update_lead(cid, {'email': e})
        return {'email': e}
    def sheet_test(self):
        try: return SH.selftest()
        except Exception as e: return {'steps': [{'ok': False, 'msg': str(e)}]}
    def pull_sheet(self):
        if not C.load_settings().get('sheet_push', True): return {'changed': 0}
        try: r = SH.sync(); return {'changed': r['pulled'], **r}
        except Exception as e: return {'error': str(e)}
    def restore_sheet(self):
        try: n = SH.restore(); return {'restored': n}
        except Exception as e: return {'error': str(e)}
    def remove(self, cid):
        C.mutate(lambda L: L.__setitem__(slice(None), [l for l in L if l['channel_id'] != cid]))
        try: C.ls().save_processed_id(cid)   # never suggested again
        except Exception: pass
        return True
    def delete(self, cids):
        """delete one or more leads (also never suggested again). returns them so Undo can put them back"""
        ids = set(cids or []); gone = []
        def f(L):
            keep = []
            for l in L: (gone if l['channel_id'] in ids else keep).append(l)
            L[:] = keep
        C.mutate(f)
        try:
            Ls = C.ls()
            for c in ids: Ls.save_processed_id(c)
        except Exception: pass
        C.sheet_mark(); return {'deleted': len(gone), 'leads': gone}
    def undo_delete(self, leads):
        for l in leads or []:
            if l.get('channel_id'): C.add_lead(l)
        C.sheet_mark(); return {'restored': len(leads or [])}
    def open(self, url):
        if str(url).startswith('http'): webbrowser.open(url)
        return True

    # ---------- phase 2: compose / gmail / follow-ups / jobs ----------
    def _lead(self, cid): return C.get_lead(cid)
    def _save(self, cid, **kw):
        C.patch(cid, **kw)
    def update_lead(self, cid, fields):
        ok = {k: v for k, v in (fields or {}).items() if k in ('email', 'first_name', 'notes', 'draft', 'fields')}
        old = self._lead(cid) or {}
        self._save(cid, **ok)
        if ok.get('email') and ok['email'] != old.get('email'):
            l = self._lead(cid)
            if not l: return True   # deleted meanwhile
            self._sheet_bg(l, contact=' | '.join(x for x in (l.get('email'), l.get('website'), l.get('contacts')) if x)[:400])
        return True
    def engines(self):
        return {'claude': bool(X.claude_cli()), 'model': X.CLAUDE_MODEL, 'capacity': C.load_settings().get('capacity', 'OPEN')}
    def write(self, cid, extra, engine, style='ask'):
        l = self._lead(cid)
        if not l: return {'error': 'lead not found'}
        try:
            d = X.write_email(l, C.load_settings().get('capacity', 'OPEN'), extra or '', engine or 'auto', style or 'ask')
            kw = {'draft': {'subject': d['subject'], 'body': d['body']}, 'transcript': l.get('transcript'), 'transcript_vid': l.get('transcript_vid')}
            if d.get('first_name') and not l.get('first_name'): kw['first_name'] = d['first_name']; kw['name_guess'] = {'name': d['first_name'], 'conf': 3, 'src': 'Claude: ' + d.get('name_evidence', '')}
            self._save(cid, **kw); return d
        except Exception as e: log.error(traceback.format_exc()); return {'error': str(e)}
    def find_name(self, cid):
        l = self._lead(cid)
        if not l.get('about'):   # older leads: fetch their about + video info once (2 quota units)
            try:
                L = C.ls(); info = L.get_channel_info(cid) or {}; vids = C.Run().videos(cid, 15) or []
                l['about'] = (info.get('description') or '')[:1500]; l['video_titles'] = [v.get('title') for v in vids]
                l['video_snips'] = '\n'.join((v.get('description') or '')[:600] for v in vids[:6])[:2500]
                self._save(cid, about=l['about'], video_titles=l['video_titles'], video_snips=l['video_snips'])
            except Exception: pass
        d = X.find_name(l)
        if d.get('first_name'): self._save(cid, first_name=d['first_name'], name_guess={'name': d['first_name'], 'conf': 3, 'src': d.get('evidence', '')})
        return d
    def push_sheet(self, cids=None):
        try: r = SH.sync(); return {'pushed': r['added'], 'dupes': 0, 'updated': r['updated'], 'errors': []}
        except Exception as e: return {'pushed': 0, 'dupes': 0, 'errors': [str(e)]}
    def similar(self, cid):
        if self.run: return {'error': 'A search is already running'}
        self.run = C.Run(push); C.SEARCHING[0] = 1
        def go():
            try: C.ls(); self.run.similar(cid)
            except Exception as e: log.error(traceback.format_exc()); push('error', str(e)); push('done', {'checked': 0, 'found': 0, 'stopped': True})
            finally: self.run = None; C.SEARCHING[0] = 0; self._after_run()
        threading.Thread(target=go, daemon=True).start(); return {'ok': True}
    def recheck(self, subject, body, style='ask'): return X.check(subject or '', X.fix(body or ''), None, style or 'ask')
    def selfie(self, cid):
        try:
            p = X.selfie(self._lead(cid))
            return {'path': p, 'img': 'data:image/jpeg;base64,' + base64.b64encode(open(p, 'rb').read()).decode()}
        except Exception as e: return {'error': str(e)}
    def frame_ready(self, cid, src='frame1'):
        l = self._lead(cid) or {}
        return str(src).startswith('frame') and X.frame_ready(l, int(str(src)[5:] or 1))
    def selfie_setup(self):
        r = WIN.create_file_dialog(_OPEN(), file_types=('Images (*.jpg;*.jpeg;*.png)',))
        if not r: return None
        p = r[0] if isinstance(r, (list, tuple)) else r
        S = C.load_settings(); S['selfie_path'] = p; S['monitor'] = None; C.save_settings(S)
        return {'img': 'data:image/jpeg;base64,' + base64.b64encode(open(p, 'rb').read()).decode()}
    def selfie_base(self):
        p = C.load_settings().get('selfie_path')
        if not p or not os.path.exists(p): return None
        from PIL import Image
        try: w, h = Image.open(p).size
        except Exception: w = h = 0
        return {'img': 'data:image/jpeg;base64,' + base64.b64encode(open(p, 'rb').read()).decode(), 'monitor': C.load_settings().get('monitor'), 'w': w, 'h': h,
                'fx': dict(X.FX_DEFAULT, **(C.load_settings().get('selfie_fx') or {}))}
    def set_monitor(self, pts):
        S = C.load_settings(); S['monitor'] = pts; C.save_settings(S); return True
    def set_capacity(self, m):
        S = C.load_settings(); S['capacity'] = m; C.save_settings(S); return True
    def _is_me(self, email):
        """an email to yourself = a test: no duplicate warning, and the lead isn't marked as emailed"""
        e = (email or '').strip().lower()
        if not e: return False
        mine = {x.lower() for x in (C.load_settings().get('my_emails') or [])}
        if os.path.exists(os.path.join(C.DATA, 'gmail_token.json')):
            try: mine.add(X._me(X.gmail()))
            except Exception: pass
        return e in mine
    def draft(self, cid, subject, body, with_selfie, style='ask', force=False):
        l = self._lead(cid)
        dup = [x for x in C.leads_ro() if x['channel_id'] != cid and (x.get('email') or '').lower() == (l.get('email') or '').lower() and x.get('outreach')]
        test = self._is_me(l.get('email'))
        if l.get('email') and dup and not force and not test:
            return {'error': f"You already emailed {l['email']} for \"{dup[0]['title']}\" (same owner?). Click Open in Gmail again to send anyway.", 'dup': True}
        try:
            img, note = None, ''
            if with_selfie:
                try: img = X.selfie(l)
                except Exception as e: note = f' (no selfie attached: {e})'
            o0 = l.get('outreach') or {}
            d = X.make_draft(l, subject, X.fix(body), img, l.get('test_draft') if test else (o0.get('draft_id') if l.get('status') == 'drafted' else None))
            if test: self._save(cid, test_draft=d.get('draft_id')); return {'ok': True, 'note': note, 'test': True, 'message_id': d.get('message_id')}   # test to yourself: lead stays as it was (same test draft reused)
            now = C._now().isoformat()[:19]
            self._save(cid, status='drafted', outreach={'subject': subject, 'thread_id': d['thread_id'], 'message_id': d.get('message_id'), 'draft_id': d.get('draft_id'), 'draft_at': now, 'followups': [], 'style': style or 'ask'},
                       history=(l.get('history') or []) + [{'t': now, 'status': 'drafted'}])
            return {'ok': True, 'note': note}
        except Exception as e: log.error(traceback.format_exc()); return {'error': str(e)}
    _sync_lock = threading.Lock()
    def sync_gmail(self, auto=False):
        """drafted->contacted when you hit send, contacted->replied when they answer, then create due follow-up drafts"""
        if not Api._sync_lock.acquire(blocking=False): return {'sent': 0, 'replied': 0, 'followups': 0, 'linked': 0, 'errors': [], 'busy': True}
        try: return self._sync_gmail(auto=auto)
        finally: Api._sync_lock.release()
    _last_sheet = [0.0]
    def _sync_gmail(self, only=None, force=False, auto=False):
        out = {'sent': 0, 'replied': 0, 'followups': 0, 'linked': 0, 'errors': [], 'why': {}}
        why = lambda l, m: out['why'].__setitem__(l['title'], m)
        try: sent_to = X.sent_recipients()   # one quick look at who you've emailed, instead of one Gmail search per lead
        except Exception as e: sent_to = set(); out['errors'].append(f'Gmail: {e}')
        import copy
        for l in [copy.deepcopy(x) for x in C.leads_ro() if (x.get('email') or '').lower() in sent_to]:   # emails you sent yourself in Gmail: attach that conversation so follow-ups reply to it
            if l.get('email') and l['email'].lower() in sent_to and not (l.get('outreach') or {}).get('thread_id') and l.get('status') in ('new', 'contacted', 'followup', 'replied', 'pitched'):
                try:
                    f = X.find_sent_thread(l['email'])
                    if f:
                        st = 'replied' if f['replied'] else ('contacted' if l['status'] == 'new' else l['status'])
                        self._save(l['channel_id'], status=st, outreach={'thread_id': f['thread_id'], 'sent_at': f['sent_at'], 'subject': f['subject'],
                                   'followups': f['followups'][:3], 'style': 'warm' if l.get('mode') == 'manual' else 'ask', 'linked': True})
                        out['linked'] += 1
                        if st != l['status']: self._sheet_bg(l, status=st)
                except Exception as e: out['errors'].append(f"{l['title']}: {e}")
        for l in [copy.deepcopy(x) for x in C.leads_ro() if (x.get('outreach') or {}).get('thread_id') or x.get('status') in ('contacted', 'followup')]:   # copies: changed below
            if only and l['channel_id'] != only: continue
            o = l.get('outreach') or {}
            if l.get('status') in ('contacted', 'followup') and not o.get('thread_id'):
                why(l, "can't find this email in your Gmail Sent folder (sent from another account, or to a different address?)"); continue
            if not o.get('thread_id') or l.get('status') not in ('drafted', 'contacted', 'followup', 'replied', 'pitched'): continue
            try:
                if l['status'] in ('replied', 'pitched'):   # stage B/C: they engaged (or got a price) and went quiet
                    ti = X.thread_info(l); key = 'bc_' + l['status']
                    if ti['their_last_t'] and o.get('bc_since') != ti['their_last_t'].isoformat():   # they wrote again: new quiet streak
                        o['bc_since'] = ti['their_last_t'].isoformat(); o[key] = []; self._save(l['channel_id'], outreach=o)
                    n = len(o.get(key, []))
                    if ti['last_me'] and not ti['has_draft'] and ti['last_t']:
                        if n >= 5 and C._now() - ti['last_t'] >= _d(2):
                            self._save(l['channel_id'], status='no_answer'); self._sheet_bg(l, status='no_answer'); continue
                        if n < 5 and C._now() - ti['last_t'] >= _d(X.STAGE_BC[n], True):
                            X.followup_draft(l, 0, X.bc_text(l['status'], n, l.get('first_name') if n in (0, 2, 3) else ''))
                            o.setdefault(key, []).append(C._now().isoformat()); self._save(l['channel_id'], outreach=o); out['followups'] += 1
                    continue
                ti = X.thread_info(l); has_draft = ti['has_draft']
                if ti['bounced'] and not ti['theirs']:   # address doesn't exist: stop, don't count as a reply
                    self._save(l['channel_id'], status='bounced'); out['bounced'] = out.get('bounced', 0) + 1; continue
                if ti['theirs']:
                    self._save(l['channel_id'], status='replied'); out['replied'] += 1
                    threading.Thread(target=BO.notify, args=(f"{l.get('title')} replied!", 'Open SwipeReach > Follow-ups to answer them fast.'), daemon=True).start()
                    continue
                if not ti['mine']:   # still sitting in Drafts
                    if l['status'] != 'drafted':   # you clicked "I sent it" but Gmail never sent it: put it back so follow-ups don't count from a send that didn't happen
                        self._save(l['channel_id'], status='drafted'); self._sheet_bg(l, status='drafted')
                        why(l, 'marked as sent, but it is still in your Gmail Drafts: moved back to Drafted. Send it from Gmail first')
                    else: why(l, 'still in your Gmail Drafts, not sent yet')
                    continue
                mine = ti['mine']
                if l['status'] == 'drafted':
                    o['sent_at'] = mine[0].isoformat(); l['status'] = 'contacted'; out['sent'] += 1; out.setdefault('sent_to', []).append(l['title'])
                    self._save(l['channel_id'], status='contacted', outreach=o); self._sheet_bg(l, status='contacted')
                o['sent_at'] = mine[0].isoformat()   # real send time, not when you pressed Check Gmail
                nf = len(mine) - 1; o['followups'] = [t.isoformat() for t in mine[1:]]
                if o.get('fu_ready') is not None and nf > o['fu_ready']: o.pop('fu_ready', None)   # you sent the follow-up that was waiting
                o['next_due'] = (mine[-1] + _d(X.STAGE_A[nf], True)).isoformat() if nf < 3 else None   # shown on the Follow-ups page
                if nf and o.get('fu_marked') != nf: o['fu_marked'] = nf; self._sheet_bg(l, status='followup')
                self._save(l['channel_id'], outreach=o)
                if has_draft:   # one follow-up waiting at a time
                    if o.get('fu_ready') is None and nf >= 0: o['fu_ready'] = nf; self._save(l['channel_id'], outreach=o)
                    why(l, 'a follow-up draft is waiting in Gmail Drafts: open it and press Send'); continue
                since = C._now() - mine[-1]   # timed from when you actually SENT the last one
                if nf >= 3:
                    if since >= _d(2): self._save(l['channel_id'], status='no_answer'); self._sheet_bg(l, status='no_answer')
                    continue
                due = _d(X.STAGE_A[nf], True)
                if since >= due or force:
                    X.followup_draft(l, nf); out['followups'] += 1; o['fu_ready'] = nf; self._save(l['channel_id'], outreach=o)
                else:
                    h = (due - since).total_seconds() / 3600
                    why(l, f"follow-up {nf + 1} is due in {h:.0f}h (timed from when Gmail actually sent your last email)" if h >= 1 else f"follow-up {nf + 1} is due in {max(1, h * 60):.0f} min")
            except Exception as e: out['errors'].append(f"{l['title']}: {e}"); log.error(traceback.format_exc())
        quiet = auto and not (out['sent'] or out['replied'] or out['followups'] or out['linked'] or out['errors'] or out.get('bounced'))
        try:   # what happened, per lead, in SwipeReach\\logs\\gmail.log (so problems can be fixed)
            if quiet: raise StopIteration
            d = os.path.join(HERE, 'logs'); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'gmail.log'), 'a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + json.dumps({k: v for k, v in out.items()}, ensure_ascii=False, default=str) + '\n')
        except Exception: pass
        try:   # sheet: every check when you press it, at most every 10 min in the background
            if C.load_settings().get('sheet_push', True) and (not auto or time.time() - Api._last_sheet[0] > 600 or not quiet):
                Api._last_sheet[0] = time.time(); out['sheet'] = SH.sync()['pulled']
        except Exception: pass
        return out
    def followup_now(self, cid):
        """make this lead's next follow-up draft right now, whatever the timer says (testing, or you just want to nudge them early)"""
        if not Api._sync_lock.acquire(timeout=20): return {'error': 'Gmail check is busy, try again in a moment'}
        try:
            r = self._sync_gmail(only=cid, force=True)
            if r['followups']: self.open_drafts(auto=True)
            return r
        finally: Api._sync_lock.release()
    def set_fu_test(self, on):
        S = C.load_settings(); S['fu_test'] = bool(on); C.save_settings(S); return True
    def triage(self, text, engine):
        d = X.triage_job(text, C.load_settings().get('capacity', 'OPEN'), engine or 'auto'); d['contacts'] = X.extract_contacts(text); return d
    def copy(self, text):
        try:
            import subprocess; subprocess.run('clip', input=text, text=True, encoding='utf-16', shell=True, creationflags=0x08000000); return True
        except Exception: return False

def _d(days, early=False):
    """follow-up timing. Test mode (Follow-ups page): 1 day = 1 minute, so you can run the whole cycle on yourself in a few minutes"""
    if C.load_settings().get('fu_test'): return C.timedelta(minutes=days)
    return C.timedelta(days=days) - (C.timedelta(hours=2) if early else C.timedelta(0))

def _repair_sheet_removed():
    """bug fix (Oct 31): sheet sync wrongly marked leads 'Removed' while Find was adding them. Put them back where they were."""
    S = C.load_settings()
    if S.get('fix_sheet_removed'): return
    def f(L):
        n = 0
        for l in L:
            h = l.get('history') or []
            if l.get('status') == 'removed' and h and h[-1].get('via') == 'sheet' and h[-1].get('status') == 'removed':
                prev = next((x.get('status') for x in reversed(h[:-1]) if x.get('status') and x.get('status') != 'removed'), 'new')
                l['status'] = prev; h.append({'t': C._now().isoformat()[:19], 'status': prev, 'via': 'repair'}); n += 1
        return n
    n = C.mutate(f); C.flush_leads(); S = C.load_settings(); S['fix_sheet_removed'] = True; C.save_settings(S)
    if n: push('notice', f'Brought back {n} leads that a sheet-sync bug had hidden')

def _set_selfie_default():
    S = C.load_settings()
    if not S.get('img_default_v2'): S['img_default_v2'] = True; S['img_mode'] = 'selfie'; C.save_settings(S)   # Ritch picked the selfie (Oct 31)

def _repair_emails():
    """once: drop junk addresses the website scan picked up (w3.org, privacy@...), re-check the rest later"""
    S = C.load_settings()
    if S.get('fix_emails_v1'): return
    def f(L):
        n = 0
        for l in L:
            e = l.get('email') or ''
            if e and not C.clean_email(e): l['email'] = ''; l.pop('email_check', None); l['email_searched'] = ''; n += 1
            elif e: l.pop('email_check', None)
        return n
    n = C.mutate(f); S = C.load_settings(); S['fix_emails_v1'] = True; C.save_settings(S)
    if n: push('notice', f'Removed {n} junk email addresses (website code, not real contacts)')

def _preload():   # load the YouTube engine at startup so the first search starts instantly
    try: _repair_sheet_removed(); _repair_emails(); _set_selfie_default()
    except Exception as e: log.warning('repair: %s', e)
    try:
        def warm():   # your selfie photo opened + measured once at startup, so the first picture is instant too
            try:
                S = C.load_settings(); b = S.get('selfie_path')
                if b and os.path.exists(b): X._base_img(b)
            except Exception: pass
        threading.Thread(target=warm, daemon=True).start()
        C.ls(); push('quota', Api().quota())
        try:   # backup: if the app has no leads but your sheet tab does, bring them back
            if not C.load_leads() and C.load_settings().get('sheet_push', True):
                n = SH.restore()
                if n: push('notice', f'Restored {n} leads from your Google Sheet backup'); push('rescored', n)
        except Exception as e: log.warning('restore: %s', e)
        S = C.load_settings()
        if S.pop('_rescore', None):   # scoring rules changed: re-score saved leads once (cached data, no quota)
            push('notice', 'Re-checking your saved leads with the new rules (fresh data, a minute or two)…')
            n = C.rescore_leads(fetch=False, progress=lambda d, t: push('log', f're-checked {d}/{t} leads') if not API.run else None, pause=lambda: bool(API.run)); C.save_settings(S)   # new rules only need the saved data: no YouTube calls
            push('notice', f'Re-checked your leads: {n} changed fit. Emails, websites and names cleaned up too.'); push('rescored', n)
    except Exception as e: log.warning('preload: %s', e)
    # check Gmail by itself every minute while the app is open: you just press Send in Gmail and the app notices
    time.sleep(6)   # let the window finish loading
    def boosters():   # once at startup, in the background: no waiting, no YouTube quota except the monthly re-check
        try:
            S = C.load_settings(); today = time.strftime('%Y-%m-%d')
            if S.get('emails_sweep') != today:   # look for missing emails on link-in-bio pages + websites
                S['emails_sweep'] = today; C.save_settings(S)
                r = BO.find_emails(progress=lambda d: push('emails', d), pause=lambda: bool(API and API.run))
                if r.get('found'): push('notice', f"Found {r['found']} more email addresses (link-in-bio pages and websites)")
                BO.check_all()
            S = C.load_settings()
            if not S.get('last_recheck'): S['last_recheck'] = time.time(); C.save_settings(S)   # first one a month from now (your leads were just checked)
            if time.time() - float(S.get('last_recheck') or 0) > 30 * 86400 and C.load_leads():   # monthly: fresh stats, new emails, editor-left / slowing signals
                S['last_recheck'] = time.time(); C.save_settings(S)
                n = C.rescore_leads(fetch=True, pause=lambda: bool(API.run)); push('rescored', n)
            S = C.load_settings()
            if time.strftime('%a') == 'Mon' and S.get('weekly_shown') != today:
                S['weekly_shown'] = today; C.save_settings(S); w = BO.weekly(); push('weekly', w)
                BO.notify('Your SwipeReach week', f"{w['sent']} emails sent, {w['replies']} replies ({w['rate']}%), {w['new_leads']} new leads.")
        except Exception as e: log.warning('boosters: %s', e)
    threading.Thread(target=boosters, daemon=True).start()
    while True:
        try:   # once a day (after 9am): phone alert if follow-up drafts are waiting to be sent
            S = C.load_settings(); today = time.strftime('%Y-%m-%d')
            if S.get('ntfy_topic') and int(time.strftime('%H')) >= 9 and S.get('fu_alert') != today:
                n = sum(1 for l in C.leads_ro() if (l.get('outreach') or {}).get('fu_ready') is not None and l.get('status') in ('contacted', 'followup'))
                S['fu_alert'] = today; C.save_settings(S)
                if n: BO.notify('Follow-ups waiting', f'{n} follow-up draft{"s" if n > 1 else ""} ready in Gmail Drafts: open Drafts and press Send.')
        except Exception as e: log.warning('fu alert: %s', e)
        try:   # keys marked 'used up': check every 15 min whether YouTube has reset them
            if C._LS is not None and C.probe_keys(): push('quota', API.quota())
        except Exception as e: log.warning('probe keys: %s', e)
        try:   # emails you scheduled for the lead's morning
            if BO.queue():
                r = BO.send_due()
                if r['sent']: push('scheduled_sent', r)
        except Exception as e: log.warning('send queue: %s', e)
        try:
            if os.path.exists(os.path.join(C.DATA, 'gmail_token.json')):
                r = API.sync_gmail(auto=True)
                if not r.get('busy'): push('synced', r)
        except Exception as e: log.warning('auto gmail sync: %s', e)
        time.sleep(30)

def _warm():   # install/repair libraries in the background at startup
    try: X.ensure('PIL', 'youtube_transcript_api')
    except Exception as e: log.warning('auto-install: %s', e)
    try:
        ok, err = X.google_ok()
        if not ok:
            log.warning('google libs broken: %s', err)
            ok2, err2 = X.fix_google()
            push('notice', 'Updated the Google libraries for Gmail. Close and reopen SwipeReach before connecting Gmail.' if ok2 else 'Could not repair Google libraries: ' + err2)
    except Exception as e: log.warning('google check: %s', e)

if __name__ == '__main__':
    threading.Thread(target=_warm, daemon=True).start(); threading.Thread(target=_preload, daemon=True).start()
    API = Api()
    WIN = webview.create_window('SwipeReach Studio' + ((' (build ' + str(json.load(open(os.path.join(HERE, 'version.json'))).get('v')) + ')') if FROZEN and os.path.exists(os.path.join(HERE, 'version.json')) else ''), os.path.join(HERE, 'ui.html'), js_api=API, width=1280, height=860, min_size=(980, 640), background_color='#0d0f13')
    webview.start(debug=bool(os.environ.get('SR_DEBUG')))
