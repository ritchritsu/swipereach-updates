"""SwipeReach Studio <-> Google Sheet ("SwipeReach Studio" tab in your leads spreadsheet).

- One clean row per lead, keyed by Channel ID (last column). Rows are updated in place, never duplicated.
- Two-way: Status, Email, First name and Notes you change in the sheet flow back into the app.
  Everything else is written by the app. Your Notes column is never overwritten.
- Writes are batched (a few API calls per sync, not one per lead) so fast searches never hit Google's rate limits.
- Backup: if the app's leads file is ever empty or lost, it can rebuild your leads from this tab.
- The old V2 tab (old scraper data) is left alone; it's only read so old channels are never found again.
"""
import re, threading, time
import sr_core as C

TAB = 'SwipeReach Studio'
COLS = ['Status', 'Channel', 'Fit', 'Score', 'Type', 'Niche', 'Subscribers', 'Views / video', 'Uploads / week', 'Growth',
        'Last upload', 'Email', 'Website', 'Socials', 'First name', 'Why it fits', 'Source', 'Added', 'Notes', 'Channel ID']
NCOL = len(COLS)
LAST = chr(ord('A') + NCOL - 1)          # 'T'
I = {c: i for i, c in enumerate(COLS)}
STATUS = {'new': 'New', 'drafted': 'Drafted', 'contacted': 'Outreach sent', 'followup': 'Following up', 'replied': 'Replied',
          'pitched': 'Pitched', 'meeting': 'Meeting', 'client': 'Client', 'no_answer': 'No answer', 'denied': 'Denied',
          'skip': 'Skipped', 'bounced': 'Bad email', 'removed': 'Removed'}
STATUS_BACK = {v.lower(): k for k, v in STATUS.items()}
FUP = ['1st follow-up', '2nd follow-up', '3rd follow-up (last)']
DROPDOWN = ['New', 'Drafted', 'Outreach sent'] + FUP + ['Replied', 'Pitched', 'Meeting', 'Client', 'No answer', 'Denied', 'Skipped', 'Bad email', 'Removed']
COLORS = {   # like your old tracker: warm = in progress, blue = follow-ups, green = good news, red = closed
    'New': (0.93, 0.93, 0.93), 'Drafted': (0.91, 0.87, 0.98), 'Outreach sent': (0.99, 0.87, 0.73), '1st follow-up': (0.80, 0.88, 0.98),
    '2nd follow-up': (0.72, 0.83, 0.97), '3rd follow-up (last)': (0.64, 0.77, 0.95), 'Replied': (0.80, 0.94, 0.80), 'Pitched': (0.86, 0.80, 0.97),
    'Meeting': (0.70, 0.90, 0.72), 'Client': (0.45, 0.80, 0.50), 'No answer': (0.85, 0.85, 0.85), 'Denied': (0.96, 0.70, 0.70),
    'Skipped': (0.90, 0.90, 0.90), 'Bad email': (0.96, 0.70, 0.70), 'Removed': (0.85, 0.85, 0.85)}
FMT_VERSION = 2

def status_label(l):
    st = l.get('status') or 'new'
    if st == 'followup':
        n = len((l.get('outreach') or {}).get('followups') or []) or 1
        return FUP[min(n, 3) - 1]
    return STATUS.get(st, 'New')

def status_key(v):
    v = str(v or '').strip().lower()
    if 'follow' in v and re.match(r'[123]', v): return 'followup'
    return STATUS_BACK.get(v)
FIT = {'HIGH': 'Great', 'MEDIUM': 'Good', 'LOW': 'Weak', 'MANUAL': 'Manual'}
SOCIAL_RX = re.compile(r'(?i)\b(instagram|x/twitter|twitter|tiktok|linkedin|threads|twitch|kick)\s*:\s*(@?[\w.\-]+)')


def _svc():
    L = C.ls(); s = L._get_sheets_service()
    if s is None: raise RuntimeError('Google Sheets not available')
    return L, s, L.GSHEETS_SPREADSHEET_ID


def style_tab(s, sid, gid):
    """dropdown + colours + date/number formats (safe to run again on an existing tab)"""
    col = lambda name: {'sheetId': gid, 'startRowIndex': 1, 'startColumnIndex': I[name], 'endColumnIndex': I[name] + 1}
    rng = lambda name: {'sheetId': gid, 'startRowIndex': 1, 'startColumnIndex': I[name], 'endColumnIndex': I[name] + 1}
    meta = s.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(sheetId),conditionalFormats)').execute()
    old = next((len(sh.get('conditionalFormats', [])) for sh in meta.get('sheets', []) if sh.get('properties', {}).get('sheetId') == gid), 0)
    reqs = [{'deleteConditionalFormatRule': {'sheetId': gid, 'index': 0}} for _ in range(old)]
    reqs.append({'setDataValidation': {'range': col('Status'), 'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': v} for v in DROPDOWN]}, 'showCustomUi': True, 'strict': False}}})
    for v, (r, g, b) in COLORS.items():
        reqs.append({'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': [rng('Status')], 'booleanRule': {
            'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': v}]}, 'format': {'backgroundColor': {'red': r, 'green': g, 'blue': b}}}}}})
    for v, (r, g, b) in {'Great': (0.13, 0.55, 0.25), 'Good': (0.75, 0.50, 0.05), 'Weak': (0.55, 0.55, 0.55)}.items():
        reqs.append({'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': [rng('Fit')], 'booleanRule': {
            'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': v}]}, 'format': {'textFormat': {'bold': True, 'foregroundColor': {'red': r, 'green': g, 'blue': b}}}}}}})
    for name in ('Subscribers', 'Views / video'):
        reqs.append({'repeatCell': {'range': col(name), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}}, 'fields': 'userEnteredFormat.numberFormat'}})
    for name in ('Last upload', 'Added'):
        reqs.append({'repeatCell': {'range': col(name), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'TEXT'}}}, 'fields': 'userEnteredFormat.numberFormat'}})
    s.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': reqs}).execute()

def ensure_tab():
    """create the tab (header, frozen row, status dropdown, date formats, widths) if it doesn't exist yet"""
    L, s, sid = _svc()
    meta = s.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(sheetId,title))').execute()
    for sh in meta.get('sheets', []):
        if sh['properties']['title'] == TAB:
            hdr = s.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'!A1:{LAST}1").execute().get('values', [])
            if not hdr or not any(hdr[0]):   # tab exists but its header row is empty: put it back
                s.spreadsheets().values().update(spreadsheetId=sid, range=f"'{TAB}'!A1:{LAST}1", valueInputOption='RAW', body={'values': [COLS]}).execute()
            gid = sh['properties']['sheetId']
            if int(C.load_settings().get('sheet_fmt', 0)) < FMT_VERSION:
                style_tab(s, sid, gid); S = C.load_settings(); S['sheet_fmt'] = FMT_VERSION; C.save_settings(S)
            return gid
    r = s.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': [{'addSheet': {'properties': {
        'title': TAB, 'gridProperties': {'frozenRowCount': 1, 'columnCount': NCOL}}}}]}).execute()
    gid = r['replies'][0]['addSheet']['properties']['sheetId']
    s.spreadsheets().values().update(spreadsheetId=sid, range=f"'{TAB}'!A1:{LAST}1", valueInputOption='RAW', body={'values': [COLS]}).execute()
    col = lambda name: {'sheetId': gid, 'startRowIndex': 1, 'startColumnIndex': I[name], 'endColumnIndex': I[name] + 1}
    widths = {'Status': 120, 'Channel': 220, 'Fit': 70, 'Score': 60, 'Type': 80, 'Niche': 130, 'Why it fits': 420, 'Email': 220, 'Website': 200, 'Socials': 200, 'Notes': 260, 'Channel ID': 110}
    reqs = [
        {'repeatCell': {'range': {'sheetId': gid, 'startRowIndex': 0, 'endRowIndex': 1},
                        'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': .93, 'green': .93, 'blue': .96}}},
                        'fields': 'userEnteredFormat(textFormat,backgroundColor)'}},
        {'setDataValidation': {'range': col('Status'), 'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': v} for v in STATUS.values()]}, 'showCustomUi': True, 'strict': False}}},
        {'repeatCell': {'range': col('Last upload'), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': col('Added'), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': col('Subscribers'), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': col('Views / video'), 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
    ] + [{'updateDimensionProperties': {'range': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': I[k], 'endIndex': I[k] + 1},
                                        'properties': {'pixelSize': v}, 'fields': 'pixelSize'}} for k, v in widths.items()]
    s.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': reqs}).execute()
    style_tab(s, sid, gid); S = C.load_settings(); S['sheet_fmt'] = FMT_VERSION; C.save_settings(S)
    return gid


def socials(l):
    out = []
    for m in SOCIAL_RX.finditer(l.get('contacts') or ''):
        v = f"{m.group(1).split('/')[-1].capitalize()} {m.group(2) if m.group(2).startswith('@') else '@' + m.group(2)}"
        if v not in out: out.append(v)
    return ', '.join(out[:4])


def row_of(l):
    """the app's values for one lead (Notes column left as is)"""
    t = str(l.get('title') or '').replace('"', "'")
    url = l.get('url') or ''
    ch = f'=HYPERLINK("{url}","{t}")' if url.startswith('http') else t
    lv = (l.get('latest_video') or {}).get('published') or ''
    wk = next((re.search(r'([\d.]+) long videos/week', r).group(1) for r in l.get('reasons', []) if 'long videos/week' in r), '')
    gx = l.get('growth_x') or 0
    why = ' · '.join(r for r in l.get('reasons', []) if not r.startswith(('money signals', 'based in')))[:450]
    typ = 'Manual' if l.get('mode') == 'manual' else ('Creator' if C.Run.creator(l.get('mode'), l.get('niche')) or l.get('mode') == 'creators' else 'Business')
    d = lambda x: ("'" + str(x)[:10]) if x else ''   # stored as text so it shows 2026-10-28, never 46323
    return [status_label(l), ch, FIT.get(l.get('tier'), ''), l.get('score') if l.get('mode') != 'manual' else '',
            typ, l.get('niche') or '', l.get('subs') or '', l.get('median_views') or '', wk, f'{gx}x' if gx else '',
            d(lv), l.get('email') or '', l.get('website') or '', socials(l), l.get('first_name') or '', why,
            l.get('source') or ('Cold email' if l.get('mode') != 'manual' else 'Other'), d(l.get('added')), None, l.get('channel_id')]


def _norm(v):
    s = str(v if v is not None else '').strip().lstrip("'")
    return re.sub(r'[,\s]', '', s) if re.fullmatch(r'[\d,.\s]+', s or 'x') else s


_lock = threading.Lock()


def sync(pull=True):
    """one full two-way sync. returns {'pulled', 'updated', 'added'}"""
    with _lock:
        L, s, sid = _svc(); ensure_tab()
        vals = s.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'!A2:{LAST}", valueRenderOption='FORMULA').execute().get('values', [])
        rows = {}
        for n, r in enumerate(vals, start=2):
            r = list(r) + [''] * (NCOL - len(r))
            cid = str(r[I['Channel ID']]).strip()
            if cid: rows[cid] = (n, r)
        out = {'pulled': 0, 'updated': 0, 'added': 0}
        # 1) your edits in the sheet -> app (only fields you changed since the app last wrote them)
        if pull:
            def f(Lx):
                for l in Lx:
                    hit = rows.get(l['channel_id'])
                    if not hit: continue
                    r = hit[1]; last = l.get('_sheet') or {}
                    st = status_key(r[I['Status']])
                    # 'Removed' is written by the app itself (a row it no longer has): never read it back as your choice
                    if st and st != 'removed' and st != last.get('status') and st != l.get('status') and last:
                        l['status'] = st; l.setdefault('history', []).append({'t': C._now().isoformat()[:19], 'status': st, 'via': 'sheet'}); out['pulled'] += 1
                    em = str(r[I['Email']]).strip()
                    if em != (last.get('email') or '') and em != (l.get('email') or '') and last and (not em or '@' in em): l['email'] = em; out['pulled'] += 1
                    fn = str(r[I['First name']]).strip()
                    if fn != (last.get('first') or '') and fn != (l.get('first_name') or '') and last: l['first_name'] = fn; out['pulled'] += 1
                    nt = str(r[I['Notes']]).strip()
                    if nt != (l.get('sheet_notes') or ''): l['sheet_notes'] = nt
            C.mutate(f)
        # 2) app -> sheet (batched)
        leads = C.leads_ro(); data, new, stamped = [], [], {}   # read only (no 30MB copy)
        for l in leads:
            want = row_of(l); hit = rows.get(l['channel_id'])
            if hit:
                n, cur = hit
                if any(_norm(want[i]) != _norm(cur[i]) for i in range(NCOL) if i != I['Notes']):
                    data.append({'range': f"'{TAB}'!A{n}:R{n}", 'values': [want[:I['Notes']]]}); out['updated'] += 1
            else:
                want[I['Notes']] = ''; new.append(want)
            stamped[l['channel_id']] = {'status': l.get('status') or 'new', 'email': l.get('email') or '', 'first': l.get('first_name') or ''}
        have = {l['channel_id'] for l in C.load_leads()}   # fresh: leads Find added during this sync are NOT gone
        for cid, (n, cur) in rows.items():   # you pressed Never in the app: keep the row as history, mark it Removed
            if cid not in have and str(cur[I['Status']]) != 'Removed':
                data.append({'range': f"'{TAB}'!A{n}", 'values': [['Removed']]})
        for i in range(0, len(data), 400):
            s.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={'valueInputOption': 'USER_ENTERED', 'data': data[i:i + 400]}).execute()
        if new:
            s.spreadsheets().values().append(spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption='USER_ENTERED',
                                             insertDataOption='INSERT_ROWS', body={'values': new}).execute(); out['added'] = len(new)
        if any(l.get('_sheet') != stamped.get(l['channel_id']) for l in leads):   # only save when a stamp actually changed
            C.mutate(lambda Lx: [x.__setitem__('_sheet', stamped[x['channel_id']]) for x in Lx if x['channel_id'] in stamped and x.get('_sheet') != stamped[x['channel_id']]])
        return out


def restore():
    """rebuild leads from the tab (backup). returns how many leads were restored"""
    L, s, sid = _svc()
    vals = s.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'!A2:{LAST}", valueRenderOption='FORMULA').execute().get('values', [])
    have = {x['channel_id'] for x in C.load_leads()}; got = []
    for r in vals:
        r = list(r) + [''] * (NCOL - len(r)); cid = str(r[I['Channel ID']]).strip()
        if not cid or cid in have or str(r[I['Status']]) == 'Removed': continue
        m = re.match(r'=HYPERLINK\("([^"]*)","(.*)"\)', str(r[I['Channel']]))
        url, title = (m.group(1), m.group(2)) if m else ('', str(r[I['Channel']]))
        num = lambda v: int(re.sub(r'\D', '', str(v)) or 0)
        tier = {v: k for k, v in FIT.items()}.get(str(r[I['Fit']]), 'MEDIUM')
        got.append({'id': cid, 'channel_id': cid, 'title': title, 'url': url, 'photo': '', 'mode': 'manual' if r[I['Type']] == 'Manual' else ('creators' if r[I['Type']] == 'Creator' else 'business'),
                    'niche': r[I['Niche']], 'subs': num(r[I['Subscribers']]), 'median_views': num(r[I['Views / video']]), 'score': num(r[I['Score']]), 'tier': tier,
                    'reasons': [x for x in str(r[I['Why it fits']]).split(' · ') if x], 'email': r[I['Email']], 'website': r[I['Website']], 'first_name': r[I['First name']],
                    'status': status_key(r[I['Status']]) or 'new', 'source': r[I['Source']], 'added': str(r[I['Added']]), 'history': [],
                    'sheet_notes': r[I['Notes']], 'restored': True})
    if got: C.mutate(lambda Lx: Lx.extend(got))
    return len(got)


def known_ids():
    """channel ids + handles in the old V2 tab and this tab: never 'new' again"""
    L, s, sid = _svc(); ids, handles = set(), set()
    for rng in (f"'{L.GSHEETS_TAB_NAME}'!B:B", f"'{TAB}'!B:B", f"'{TAB}'!{LAST}:{LAST}"):
        try:
            v = s.spreadsheets().values().get(spreadsheetId=sid, range=rng, valueRenderOption='FORMULA').execute().get('values', [])
        except Exception: continue
        txt = ' '.join(str(x[0]) for x in v if x)
        ids |= set(re.findall(r'UC[\w-]{22}', txt)); handles |= {h.lower() for h in re.findall(r'youtube\.com/(@[\w.\-]+)', txt)}
    return ids, handles


class _Auto:
    """batches sheet writes: anything that changes a lead calls mark(); a background thread syncs every few seconds"""
    def __init__(self): self.dirty = threading.Event(); self.err = None; self.t = None; self.last = None
    def mark(self):
        self.dirty.set()
        if not self.t or not self.t.is_alive():
            self.t = threading.Thread(target=self._loop, daemon=True); self.t.start()
    def _loop(self):
        while True:
            if not self.dirty.wait(600): continue
            time.sleep(8)   # collect a burst of changes into one write
            if C.SEARCHING[0] and time.time() - getattr(self, 't_sync', 0) < 120: time.sleep(5); continue   # searching: sync at most every 2 min
            self.dirty.clear(); self.t_sync = time.time()
            if not C.load_settings().get('sheet_push', True): continue
            try: self.last = sync(); self.err = None
            except Exception as e: self.err = str(e)

AUTO = _Auto()


def selftest():
    """read + no-change write check of the SwipeReach Studio tab"""
    steps = []
    def step(ok, msg): steps.append({'ok': bool(ok), 'msg': msg})
    try: L, s, sid = _svc()
    except Exception as e: step(False, f'Could not connect: {e}'); return {'steps': steps}
    try:
        ensure_tab(); step(True, f'Connected, tab "{TAB}" ready (your old V2 tab is untouched, only read to skip old channels)')
        hdr = s.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'!A1:{LAST}1").execute().get('values', [[]])
        step((hdr[0] if hdr else []) == COLS, 'Columns match' if (hdr[0] if hdr else []) == COLS else 'Header row was changed, columns may be off')
        r = sync(); n = len(C.load_leads())
        step(True, f"Synced: {r['added']} added, {r['updated']} updated, {r['pulled']} edits pulled from the sheet ({n} leads in the app)")
        ids, hs = known_ids(); step(True, f'{len(ids) + len(hs)} channels known from your sheet tabs (never found twice)')
        s.spreadsheets().values().update(spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption='RAW', body={'values': [[COLS[0]]]}).execute()
        step(True, 'Write access OK')
    except Exception as e: step(False, f'Error: {e}')
    return {'steps': steps}
