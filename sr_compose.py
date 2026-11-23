"""SwipeReach phase 2: AI email (Claude Pro via claude CLI -> Ollama -> template),
master-doc rule checker, selfie monitor composite, Gmail drafts, 1-2-2 follow-ups."""
import os, time, re, json, shutil, subprocess, urllib.request, urllib.error, base64, io
from datetime import datetime, timedelta, timezone
import sr_core as C
HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, 'master_prompt.txt')
CLAUDE_MODEL = 'claude-sonnet-5-5'          # Sonnet, not Opus: lighter on your Pro limits
NOFLAGS = 0x08000000 if os.name == 'nt' else 0  # no console window popping up on Windows

PKGS = {'google_auth_oauthlib': 'google-auth-oauthlib', 'googleapiclient': 'google-api-python-client', 'google.oauth2': 'google-auth', 'PIL': 'Pillow', 'youtube_transcript_api': 'youtube-transcript-api'}
def ensure(*mods):
    """installs missing libraries into the SAME Python the app runs on (fixes 'No module named ...')"""
    import importlib, sys
    miss = []
    for m in mods:
        try: importlib.import_module(m)
        except ImportError: miss.append(PKGS.get(m, m))
    if not miss: return
    if getattr(sys, 'frozen', False): raise RuntimeError('Missing in this build: ' + ', '.join(miss))   # the .exe has no pip
    exe = sys.executable
    if exe.lower().endswith('pythonw.exe'): exe = exe[:-11] + 'python.exe'
    p = subprocess.run([exe, '-m', 'pip', 'install', '--disable-pip-version-check', '-q'] + miss, capture_output=True, text=True, timeout=300, creationflags=NOFLAGS)
    importlib.invalidate_caches()
    for m in mods:
        try: importlib.import_module(m)
        except ImportError: raise RuntimeError(f'Could not install {PKGS.get(m, m)}: ' + (p.stderr or p.stdout or '').strip()[-200:])

def _py():
    import sys
    exe = sys.executable
    return exe[:-11] + 'python.exe' if exe.lower().endswith('pythonw.exe') else exe

GOOGLE_PKGS = ['google-auth', 'google-auth-oauthlib', 'google-api-python-client', 'google-auth-httplib2']
def google_ok():
    """checks the Google libraries in a fresh Python (the app's own process may have old versions loaded)"""
    import sys
    if getattr(sys, 'frozen', False): return True, ''   # bundled in the .exe
    p = subprocess.run([_py(), '-c', 'import googleapiclient.discovery, google_auth_oauthlib.flow, google.oauth2.credentials, google.auth.transport.requests'],
                       capture_output=True, text=True, timeout=60, creationflags=NOFLAGS)
    return p.returncode == 0, (p.stderr or '').strip()[-300:]

def fix_google():
    """upgrade the whole Google set together so versions match (fixes 'has no attribute CredentialsWithRegionalAccessBoundary')"""
    p = subprocess.run([_py(), '-m', 'pip', 'install', '-U', '--disable-pip-version-check', '-q'] + GOOGLE_PKGS, capture_output=True, text=True, timeout=600, creationflags=NOFLAGS)
    ok, err = google_ok()
    return ok, (err or (p.stderr or '')[-300:])

DOCS = os.path.join(C.DATA, 'docs')   # your editable copies (Setup > Your master docs); bundled ones are the fallback
DEFAULT_NOTES = ("CURRENT CLIENT: Ritch's only current client is a long-term strategy gaming channel. Keep the proof line VAGUE about the niche/format: "
                 "say 'I edit long-form videos for a creator right now' (never 'educational', 'documentary', 'gaming' etc. as current client work, never 'a couple/few creators'). "
                 "Optional once: 'their videos regularly reach 50k+ views' (the channel's audience, never a result Ritch caused). Never name the client or link their videos.")
def _doc(name, bundled=None):
    for p in (os.path.join(DOCS, name), bundled or os.path.join(HERE, name)):
        try:
            with open(p, encoding='utf-8') as f: return f.read()
        except Exception: pass
    return ''
NOTE_ADDS = [('RATE:', "RATE: Ritch charges per video and discusses the price with each person. Never put a number in a first message, cold email or job reply. If a post asks for a rate, say it's per video and he's happy to talk it through based on the footage and style."),
             ('SIGNATURE:', "SIGNATURE: Ritch's Gmail signature is added in Gmail, never write a sign-off or signature in the body."),
             ('AVOID TOPICS:', "AVOID TOPICS: Ritch doesn't edit politics, religion, drama/tea, gore or sexual content. If a job post is mainly about these, verdict SKIP.")]
def notes():
    p = os.path.join(DOCS, 'my_notes.txt')
    if not os.path.exists(p):
        try: os.makedirs(DOCS, exist_ok=True); open(p, 'w', encoding='utf-8').write(DEFAULT_NOTES)
        except Exception: return DEFAULT_NOTES
    t = _doc('my_notes.txt'); add = [line for key, line in NOTE_ADDS if key not in t]
    if add:   # new standing rules from Ritch, added once (he can edit them in Setup)
        t = t.rstrip() + '\n\n' + '\n\n'.join(add)
        try: open(p, 'w', encoding='utf-8').write(t)
        except Exception: pass
    return t
def _rules():
    """sections 7-10 of your Ritch master doc (outreach, awareness, first message, follow-ups), read fresh each time so edits apply"""
    d = _doc('ritch_master_doc.txt')
    m = re.search(r'(?ms)^7\. Outreach principles.*?(?=^11\. )', d)
    return m.group(0) if m else _doc('outreach_rules.txt')
# only the parts of your master prompt a task needs (sending all of it every time burns ~3x the tokens for the same email)
COLD_SECTIONS = ('ROLE', 'TASK B', 'LENGTH BY PLATFORM', 'PROOF OVER PROMISE', 'READ THE PERSON', 'DEFAULT EMAIL FLOW', 'ALWAYS TRUE', 'GREETINGS',
                 "DON'T RESTATE", 'PSYCHOLOGICAL FRAMING', 'VOICE RULES', "RITCH'S CLIENT PROOF")
def _sections(text, keep):
    parts = re.split(r'(?m)^(?=[A-Z][A-Z0-9 \'\u2019\u2014\-/()&:,.]{3,}$)', text)
    out = [x for x in parts if x.strip() and x.lstrip().upper().startswith(keep)]
    return '\n'.join(out) if len(out) >= 4 else text   # headings changed? fall back to everything
def _master(keep=None):
    t = _doc('master_prompt.txt', MASTER)
    if keep: t = _sections(t, keep)
    return t + "\n\n===== RITCH'S CURRENT NOTES (highest priority, override everything above) =====\n" + notes()

# ---------------- transcript (same captions YouTube's CC button shows) ----------------
def transcript(video_id, max_chars=3000):
    """first few minutes + a bit of the middle of the video. '' if captions are off or YouTube blocks it."""
    if not video_id: return ''
    try:
        ensure('youtube_transcript_api'); from youtube_transcript_api import YouTubeTranscriptApi as Y
        langs = ['en', 'en-US', 'en-GB']
        try: segs = [(s.start, s.text) for s in Y().fetch(video_id, languages=langs)]          # v1.x
        except AttributeError: segs = [(s['start'], s['text']) for s in Y.get_transcript(video_id, languages=langs)]  # v0.6
    except Exception: return ''
    if not segs: return ''
    clean = lambda part: re.sub(r'\s+', ' ', re.sub(r'\[[^\]]{0,30}\]|>>', ' ', ' '.join(t for _, t in part))).strip()
    end = segs[-1][0] or 1
    opening = clean([x for x in segs if x[0] <= 240])
    middle = clean([x for x in segs if end * 0.45 <= x[0] <= end * 0.45 + 90])
    out = '[opening] ' + opening[:int(max_chars * 0.7)] + (' [middle of the video] ' + middle[:int(max_chars * 0.3)] if middle else '')
    return out

def lead_transcript(lead):
    lv = lead.get('latest_video') or {}
    if lead.get('transcript_vid') == lv.get('video_id') and lead.get('transcript') is not None: return lead['transcript']
    t = transcript(lv.get('video_id')); lead['transcript'] = t; lead['transcript_vid'] = lv.get('video_id'); return t

# ---------------- prompt ----------------
STYLES = {
 'ask': ("ASK-FIRST cold email (the default for cold, ~45 words, hard max 60). Structure: Hey [Name], / I like [one specific thing they do], especially in your recent video about [topic or moment]. "
         "/ one short clause that the pic below is their video up on my screen / Would you mind if I sent over a 20-second edit of one of your videos? Just reply \"sure\". "
         "No links, no proof line, no portfolio. Subject: 'quick question about [video title]' style, lowercase, under 45 chars."),
 'proof': ("PROOF cold email (~55 words, hard max 70). Structure: Hey [Name], / one specific observation about a recent video (a real moment) / "
           "one-line proof mirroring their format ('I edit long-form [format] videos for a couple of creators, so figured I'd reach out') + mention the pic of their video on my screen in a few words / "
           "Here's one that's close to your style: [closest sample link from the link pool, else portfolio] / If you're not looking for help right now, all good. Just reply with a 👍 if you'd be open to chatting. "
           "Subject: lowercase, specific to them, under 45 chars ('quick one about [video title]', 'your [series] series')."),
}
# best of both: Flick's visual hook + specific compliment + one-word reply, run through Malice's rules
# (match how aware they are, relevance before ask, easy out, no copy-paste wording, proof only if you have it)
STYLES['best'] = (
 "BEST-OF-BOTH: you write ONLY the opening lines. The app adds the greeting, the picture and Ritch's fixed closing himself.\n"
 "\"body\" = 1-2 short sentences, max 35 words, that open the email right after 'Hey [name],'. Sound like a real person who just watched their "
 "latest video and genuinely liked something in it: warm, casual, a little personality, like a quick message to a creator you follow. "
 "Start naturally ('Just watched your ___ video,' / 'Loved your ___ video,' / 'Your ___ video was so good,') then name ONE real, specific moment or habit "
 "from it (paraphrase the transcript, never invent) and say why it's great in plain words. Use 'I love how' / 'such a good' energy, not analysis: "
 "never describe their video like a review or explain its structure ('leaves room for', 'is easy to follow because'). "
 "If SITUATION says they're hiring, open with that instead: 'Saw you're looking for an editor for the channel,' plus the specific compliment.\n"
 "Do NOT mention editing, yourself, the picture, or ask anything: the closing does that. No 'especially this part'. No em-dashes.\n"
 "SUBJECTS: 3 different ones under 40 chars, lowercase, no emojis, no '!': plain and specific ('your cute house video'), curiosity "
 "('quick idea for your channel'), or a question ('still editing these yourself?'). Never 'Opportunity', 'Collaboration', 'Video editing services'. "
 "Best one in \"subject\", all 3 in \"subjects\".")
# the closing has to fit the picture: a frame from their video = "here's what I'd do", your selfie with it open in Premiere = you've already started
# one closing for every picture (frame or selfie): curiosity about "specific stuff" in THEIR video (Flick), a free tiny preview
# (reciprocity), proof + scarcity in one line, the reply made obvious, and an easy out (Malice)
BEST_CLOSE = ("There's some specific stuff in this video I can see working really well with my editing. "
              "Mind if I send over a quick 15-20 second edit showing how it'd look?\n\n"
              "I'm editing for another creator right now and have room for one more, so just reply \"sure\" and I'll send it over.\n\n"
              "If you've got editing covered, no worries!")
BEST_CLOSE_SELFIE = BEST_CLOSE
_OLD_CLOSES = ["I'm currently editing for another creator right now and have room for another, and I'd love to cut a quick 15-20 second edit "
               "from this video to show you what I'd do with it.\n\nJust reply \"sure\" and I'll send it over.\n\nIf you've got editing covered, no worries!",
               "I'm currently editing for another creator right now and have room for another, so I'd love to finish a quick 15-20 second edit "
               "of this and send it your way.\n\nJust reply \"sure\" and it's yours.\n\nIf you've got editing covered, no worries!",
               "I'm currently editing for another creator right now and have room for another, and I'd like to cut a 15-20 second edit of one of yours.\n\n"
               "Just reply \"sure\" and I'll send it over.\n\nIf you've got editing covered, no worries!"]
BEST_CLOSE_HIRING = ("I'm currently editing for another creator right now and have room for another. Are you still looking? "
                     "Happy to cut a 15-20 second edit of one of yours first so you can see how I work.\n\nIf you've already found someone, no worries!")
def best_close(lead):
    """your own closing, word for word (edit docs/best_closing.txt, best_closing_selfie.txt or best_closing_hiring.txt to change it)"""
    if situation(lead)['awareness'].startswith('hiring'): return _doc('best_closing_hiring.txt').strip() or BEST_CLOSE_HIRING
    if _img_mode(lead) == 'selfie': return _doc('best_closing_selfie.txt').strip() or BEST_CLOSE_SELFIE
    return _doc('best_closing.txt').strip() or BEST_CLOSE
def fit_picture(lead, body):
    """you switched between frame and selfie after writing: swap the closing (and the Premiere line) so the email matches the picture"""
    b = (body or '').replace('So I pulled it up in Premiere:\n\n', '')
    for c in [BEST_CLOSE, _doc('best_closing.txt').strip(), _doc('best_closing_selfie.txt').strip()] + _OLD_CLOSES:
        if c and c in b: return b.replace(c, best_close(lead))
    return body

def best_body(lead, opener):
    n = (lead.get('first_name') or '').strip()
    opener = re.sub(r'(?is)^\s*(hey|hi|hello)\b[^,\n]*,\s*', '', opener or '').strip()
    opener = re.sub(r'(?i)\s*\[IMAGE\]\s*', ' ', opener).strip()
    return f"Hey{' ' + n if n else ''},\n\n{opener}\n\n[IMAGE]\n\n{best_close(lead)}"

VOICE = ("VOICE (applies to everything you write for Ritch): upbeat, warm and human, like a friendly creator who just enjoyed their stuff. "
  "Never flat, formal, sad or robotic. Short sentences, natural contractions, a bit of genuine enthusiasm ('I love how', 'so good', 'such a smart'), "
  "never over the top or salesy. The mood to match, word for word in spirit:\n"
  "<<Hey Sarah,\nJust watched your cute house video, I love how you leave little gaps for the swish and beep parts so kids can shout along.\n[IMAGE]\n"
  "There's some specific stuff in this video I can see working really well with my editing. Mind if I send over a quick 15-20 second edit showing how it'd look?\n"
  "I'm editing for another creator right now and have room for one more, so just reply \"sure\" and I'll send it over.\nIf you've got editing covered, no worries!>>\n"
  "Bad (too dead): 'The wheels on the bus song in your video leaves room for the swish and beep parts, so it's easy for kids to join in.' "
  "That describes the video like a report. Always react to it like a person.\n")

def _img_mode(lead):
    return (lead.get('selfie_params') or {}).get('mode') or C.load_settings().get('img_mode') or 'selfie'

def situation(lead):
    """what we know about where this person is (Malice's levels of awareness) so the 'best' style can adapt"""
    txt = ' '.join([lead.get('about') or '', ' '.join(lead.get('reasons') or []), lead.get('source') or '', lead.get('notes') or ''])
    hiring = bool(re.search(r"(?i)job post|hiring|looking for (an? )?(video )?editor|need(s|ing)? (an? )?editor|editor wanted", txt))
    team = bool(re.search(r"(?i)has a team|already has|edited by|editor:|our editors?|likely has a team", txt))
    links = bool(re.search(r'https?://', _doc('my_notes.txt') or ''))
    subs = lead.get('subs') or 0
    return {'awareness': 'hiring / looking for an editor (be direct)' if hiring else 'not looking (lead with relevance, easy out)',
            'already_has_editor_or_team': team, 'kind': 'business owner' if lead.get('mode') == 'business' else 'creator',
            'size': 'small' if subs < 20000 else 'mid' if subs < 250000 else 'big', 'first_name_known': bool(lead.get('first_name')),
            'ritch_has_sample_links': links}

def _winners():
    """subject lines that already got replies (learned from your own results)"""
    try:
        import sr_boost; w = sr_boost.winning_subjects()
        return ("Subject lines that already got REPLIES from creators (match their feel, don't copy word for word): " + '; '.join(w) + "\n") if w else ''
    except Exception: return ''

def build_prompt(lead, mode='OPEN', extra='', style='ask'):
    lv = lead.get('latest_video') or {}
    facts = {
        'channel': lead.get('title'), 'niche': lead.get('niche'), 'subs': lead.get('subs'),
        'latest_video_title': lv.get('title'), 'latest_video_url': lv.get('url'),
        'why_they_fit': lead.get('reasons'), 'website': lead.get('website'),
        'kind': 'business owner who teaches/sells' if lead.get('mode') == 'business' else 'youtube creator',
        'first_name_set_by_ritch': lead.get('first_name') or '',
        'channel_about': (lead.get('about') or '')[:1200], 'recent_video_titles': lead.get('video_titles') or [],
        'their_email': lead.get('email') or '', 'other_contacts': (lead.get('contacts') or '')[:300],
        'latest_video_transcript_excerpt': lead.get('transcript') or '(not available, go off the title)',
    }
    if style == 'best': facts['SITUATION'] = situation(lead)
    return (_master(COLD_SECTIONS) + "\n\n" +   # the outreach doc's cold-email ideas are written into the STYLE below (saves ~5k tokens)
        "\n\n=====\n" + VOICE + "TASK: COLD OUTREACH EMAIL (they are not hiring). CAPACITY MODE: " + mode + ".\nSTYLE: " + STYLES.get(style, STYLES['ask']) + "\n"
        "Lead facts (real, use only these, never invent):\n" + json.dumps(facts, ensure_ascii=False, indent=1) +
        "\nThe specific detail must come from their latest video: if the transcript excerpt is there, tie it to ONE real moment, habit or point from it "
        "(paraphrase, never invent anything not in it). Generic praise ('love your content', 'great videos') is banned. Readable in 10 seconds. "
        "PROOF: follow RITCH'S CURRENT NOTES for the current-client line (vague, true, no names). For a sample link pick the closest one from the link pool, else the portfolio.\n"
        "A pic of their video up on Ritch's monitor is attached (the visual hook): mention it in a few words only. "
        "Short lines, one easy ask, no em-dashes, no sign-off, no self-intro, no client names, no promises of views/retention.\n"
        + (("Extra notes from Ritch: " + extra + "\n") if extra else '')
        + NAME_RULES +
        "Greet with the first name if you found one (\"Hey Sarah,\"), otherwise just \"Hey,\". If Ritch set a first name, use exactly that.\n"
        + (("A pic of their video on Ritch's monitor goes where you put [IMAGE].\n" if _img_mode(lead) != 'frame' else
             "PICTURE OVERRIDE: the picture is just a still frame from their video (no monitor, no selfie). Introduce it as that moment, e.g. 'this bit here:' "
             "or 'especially this part:', never mention a screen or monitor. Still put [IMAGE] alone on its own line.\n") if style in ('ask', 'proof') else '') +
        _winners() + "Reply ONLY with JSON: {\"subject\": \"...\", " + ('\"subjects\": [\"\", \"\", \"\"], ' if style == 'best' else '') + "\"body\": \"...\", \"first_name\": \"\", \"name_evidence\": \"\"}")

NAME_RULES = ("FIRST NAME: work out the channel owner's real first name from the evidence (channel name like 'Rick Shiels Golf', 'I'm Sarah' in the about text, "
              "sign-offs, the email address like craig@..., 'Coach X'/'Dr. X' where X is clearly a first name, names in video titles). "
              "Only return it if the evidence is solid; a surname ('Coach Lockey') or a brand is NOT a first name. When unsure return \"\". "
              "If two hosts, pick the one matching the email, else the first listed.\n")

def find_name(lead, engine='auto'):
    facts = {'channel': lead.get('title'), 'handle': lead.get('handle'), 'email': lead.get('email'), 'about': (lead.get('about') or '')[:1500],
             'recent_video_titles': lead.get('video_titles'), 'video_description_snippets': (lead.get('video_snips') or '')[:2000], 'other_contacts': (lead.get('contacts') or '')[:300]}
    prompt = NAME_RULES + "Evidence:\n" + json.dumps(facts, ensure_ascii=False, indent=1) + '\nReply ONLY with JSON: {"first_name": "", "evidence": "", "confidence": "high|medium|low"}'
    errs = []
    for e in ({'auto': ['claude', 'ollama'], 'claude': ['claude'], 'ollama': ['ollama']}.get(engine) or ['claude']):
        try:
            m = re.search(r'\{.*\}', ask_claude(prompt, 90, light=True) if e == 'claude' else ask_ollama(prompt), re.S)
            d = json.loads(m.group(0)); n = re.sub(r'[^A-Za-z\-\' ]', '', d.get('first_name') or '').strip().split(' ')[0]
            return {'first_name': n.capitalize() if n else '', 'evidence': d.get('evidence', ''), 'confidence': d.get('confidence', ''), 'engine': e}
        except Exception as x: errs.append(f'{e}: {x}')
    g = C.guess_name(lead.get('title') or '', lead.get('about') or '', lead.get('email') or '', lead.get('video_snips') or '')
    return {'first_name': g[0] if g[1] >= 2 else '', 'evidence': g[2] + ' (no AI: ' + '; '.join(errs) + ')', 'confidence': ['', 'low', 'medium', 'high'][g[1]], 'engine': 'guess'}

def _parse(txt):
    m = re.search(r'\{.*\}', txt or '', re.S)
    if not m: return None
    try:
        d = json.loads(m.group(0))
        if d.get('subject') and d.get('body'):
            return {'subject': d['subject'].strip(), 'body': d['body'].strip(), 'first_name': (d.get('first_name') or '').strip(), 'name_evidence': d.get('name_evidence') or '', 'subjects': [str(x).strip() for x in (d.get('subjects') or []) if str(x).strip()]}
    except Exception: return None

# ---------------- engines ----------------
_CLI = {}
def claude_cli():
    if _CLI.get('p') and os.path.exists(_CLI['p']): return _CLI['p']
    p = _claude_cli()
    if p: _CLI['p'] = p
    return p

def _claude_cli():
    p = shutil.which('claude') or shutil.which('claude.cmd') or shutil.which('claude.exe')
    if p: return p
    home = os.path.expanduser('~')
    for c in (os.path.join(os.environ.get('APPDATA', ''), 'npm', 'claude.cmd'), os.path.join(home, '.local', 'bin', 'claude.exe'),
              os.path.join(home, '.claude', 'local', 'claude.cmd'), os.path.join(home, '.local', 'bin', 'claude')):
        if c and os.path.exists(c): return c
    return None

def claude_status():
    """installed + logged in? (quick, no tokens used)"""
    exe = claude_cli()
    if not exe: return {'installed': False, 'ok': False}
    c = _CLI.get('status')
    if c and c[0] == exe and time.time() - c[1] < 900: return c[2]   # starting claude just to check costs 1-2 s: check every 15 min, not every click
    try:
        p = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=20, creationflags=NOFLAGS)
        r = {'installed': True, 'ok': p.returncode == 0, 'version': (p.stdout or '').strip()[:40]}
        if r['ok']: _CLI['status'] = (exe, time.time(), r)
        return r
    except Exception as e: return {'installed': True, 'ok': False, 'error': str(e)}

def install_claude():
    """opens a visible window: official installer, then 'claude' so you can log in with Pro"""
    if os.name != 'nt': raise RuntimeError('Windows only')
    ps = "irm https://claude.ai/install.ps1 | iex; $env:Path += ';' + $env:USERPROFILE + '\\.local\\bin'; Write-Host ''; Write-Host 'Now log in with your Claude Pro account. Type /exit when done.' -Fore Green; claude"
    subprocess.Popen(['cmd', '/c', 'start', 'Install Claude', 'powershell', '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', ps])
    return True

LIGHT_MODEL = 'claude-haiku-5-5'   # small lookups (first names): Haiku is plenty and far lighter on your limits
SYSTEM = "You write and analyse cold outreach for Ritch, a long-form video editor. Follow the user's instructions exactly and reply only in the format asked for."
_CL_OFF = [0.0]
def ask_claude(prompt, timeout=180, light=False):
    if time.time() < _CL_OFF[0]: raise RuntimeError("Claude's Pro limit is used up for now (it resets after a few hours)")   # don't wait on it again and again
    exe = claude_cli()
    if not exe: raise RuntimeError('Claude not installed yet (Setup > Install Claude)')
    env = dict(os.environ); env.pop('ANTHROPIC_API_KEY', None)   # force your Pro login, never a paid key
    # lean call: our own short system prompt instead of Claude Code's big one, no tools, no MCP servers, no skills, no saved session,
    # one turn. Same answer quality, a fraction of the tokens per email.
    args = [exe, '-p', '--model', LIGHT_MODEL if light else CLAUDE_MODEL, '--output-format', 'text', '--system-prompt', SYSTEM, '--tools', '',
            '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence', '--max-turns', '1']
    if not light: args += ['--effort', 'medium']
    run = lambda a: subprocess.run(a, input=prompt, capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=env, creationflags=NOFLAGS, cwd=C.DATA)  # neutral folder: Claude never touches your files
    p = run(args)
    if p.returncode != 0 and re.search(r'(?i)unknown option|unknown argument|invalid (value|option)|not supported|effort', p.stderr or ''):
        p = run(args[:args.index('--system-prompt')])   # older Claude install: plain call still works
    if p.returncode != 0:
        m = (p.stderr or p.stdout or 'claude failed').strip()
        if re.search(r'(?i)usage limit|limit reached|rate.?limit|quota|out of (credits|usage)|5-hour|weekly limit', m): m = "Claude's Pro limit is used up for now (it resets after a few hours)"; _CL_OFF[0] = time.time() + 1800
        elif re.search(r'(?i)log ?in|auth|credential', m): m = 'Claude needs you to log in: Setup > Install Claude (it opens the login)'
        raise RuntimeError(m[:300])
    return p.stdout

def ask_ollama(prompt, model=None, timeout=240):
    model = model or C.load_settings().get('ollama_model') or 'llama3.1:8b'
    req = urllib.request.Request('http://127.0.0.1:11434/api/generate',
        data=json.dumps({'model': model, 'prompt': prompt, 'stream': False, 'format': 'json'}).encode(),
        headers={'Content-Type': 'application/json'})
    try: return json.loads(urllib.request.urlopen(req, timeout=timeout).read())['response']
    except urllib.error.HTTPError as e: raise RuntimeError(f'Ollama: model "{model}" missing? ({e.code})')
    except urllib.error.URLError: raise RuntimeError('Ollama is not running')

def _short_title(t):
    """a video title a person would actually say out loud: cut at | - ( : #, drop emojis, and give up (None) if it's still long or shouty"""
    t = re.sub(r'#\S+', '', t or ''); t = re.sub(r'[^\w\s\'&,.?!-]', '', t)
    t = re.split(r'\s[|\-\u2013\u2014:]\s|\(|\[', t)[0].strip(' -.,')
    if not t or len(t) > 38 or len(t.split()) > 7 or (sum(c.isupper() for c in t) > len(t) * 0.5 and len(t) > 8): return None
    return t

def template(lead):
    """no AI (Claude limit reached, offline...): still a ready-to-send email in your voice, with your exact closing.
    Long or messy titles are NOT quoted (that reads robotic): it just says 'your latest video'."""
    st = _short_title((lead.get('latest_video') or {}).get('title'))
    opener = f'Just watched your "{st}" video, really enjoyed it!' if st else 'Just watched your latest video, really enjoyed it!'
    return {'subject': st.lower() if st and len(st) < 30 else 'your latest video', 'body': best_body(lead, opener)}

def write_email(lead, mode='OPEN', extra='', engine='auto', style='ask'):
    """returns {'subject','body','engine','issues','warning','transcript'}"""
    if engine != 'template': lead_transcript(lead)
    prompt = build_prompt(lead, mode, extra, style); errs = []
    order = {'auto': ['claude', 'ollama'], 'claude': ['claude'], 'ollama': ['ollama'], 'template': []}[engine]
    for e in order:
        try:
            d = _parse(ask_claude(prompt) if e == 'claude' else ask_ollama(prompt))
            if d:
                d['body'] = fix(d['body']); d['subject'] = fix(d['subject'])
                if style == 'best':   # Claude wrote only the opener: your greeting, picture and closing go around it
                    if d.get('first_name') and not lead.get('first_name'): lead = dict(lead, first_name=d['first_name'])
                    d['body'] = best_body(lead, d['body'])
                    d['subjects'] = [fix(str(x)) for x in (d.get('subjects') or []) if x][:3] or [d['subject']]
                d.update(engine=e, issues=check(d['subject'], d['body'], lead, style), warning='; '.join(errs), transcript=bool(lead.get('transcript')), style=style); return d
            errs.append(e + ': unreadable reply')
        except Exception as x: errs.append(f'{e}: {x}')
    d = template(lead); d.update(engine='template', issues=[], warning='; '.join(errs), style='best'); return d

# ---------------- master-doc rule checker ----------------
BANNED = [(r'no pressure', '"no pressure" is banned'), (r'wishing you the best', '"wishing you the best" is banned'),
          (r'exactly the kind of thing I enjoy', 'banned phrase'), (r'flexible hours', 'never "flexible hours"'),
          (r"\bI'?m a (long-?form |professional )?(video )?editor\b|\bmy name is\b|\bI'?m Ritch\b|years of experience", 'no self-intro'),
          (r"\blove your content\b|\bgreat content\b|\byour videos are (good|great|awesome)\b|\bbig fan\b", 'generic praise, use one specific detail'),
          (r"\binterested in my services\b|\bvideo editing services\b", 'sounds like a sales email'),
          (r"\b(a )?(couple|few) (of )?(other )?(creators|channels|clients)\b", 'you have one client: say "a creator"'),
          (r"\bedit\w* (long-?form )?(educational|documentary|talking[- ]head|vlog|gaming|strategy[- ]gaming|commentary)\b[^.]{0,40}\bfor (a|one|another) (creator|client|channel)", 'keep your client vague: "I edit long-form videos for a creator"'),
          (r'\b\d+(\.\d+)?\s*%|\bgrew (a|the|my)\b|\b\d+k views\b|increase(d)? (your )?(views|retention)|maximi[sz]e retention', 'no outcome/view claims'),
          (r'^(best|cheers|thanks|regards)?,?\s*\n?\s*ritch\s*$', 'no sign-off (signature is automatic)')]
LIMITS = {'best': (40, 120, 'best-of-both ~70-100 words'), 'flick': (70, 160, 'Flick-style ~120 words'), 'ask': (25, 60, 'ask-first ~45 words'), 'proof': (30, 75, 'cold ~55, under ~70 words'), 'warm': (35, 95, 'warm email 50-90 words'),
          'dm': (12, 45, 'warm DM ~35 words'), 'followup': (3, 40, 'follow-up under 2 sentences')}
def fix(t):  # silent safe fixes
    return re.sub(r'\s*[\u2014\u2013]\s*', ', ', t or '').replace(' ,', ',')

def check(subject, body, lead=None, style='ask'):
    """Ritch's concise outreach system: length by message type, subject rules, banned phrases, one easy ask"""
    out = []; w = len(re.findall(r"\b\w[\w']*\b", body)); lo, hi, lab = LIMITS.get(style, LIMITS['proof'])
    if w > hi: out.append(f'too long ({w} words, {lab})')
    if w < lo: out.append(f'very short ({w} words, {lab})')
    if '\u2014' in body + subject: out.append('em-dash')
    if subject:
        if len(subject) > 60: out.append('subject over 60 chars')
        elif len(subject) > 45: out.append('subject over 45 chars (cuts off on mobile)')
        if style != 'flick' and re.fullmatch(r'\s*(application|inquiry|hello|hi|opportunity|collaboration|video editing services)\s*', subject, re.I): out.append('subject looks like spam')
        if style != 'flick' and ('!' in subject or re.search(r'[\U0001F300-\U0001FAFF]', subject)): out.append('no "!" or emojis in subject')
        if style in ('ask', 'proof') and re.search(r'[A-Z]{4,}', subject): out.append('no ALL CAPS in subject')
    body = re.sub(r'\[IMAGE\]', '', body)
    for rx, msg in BANNED:
        if style == 'flick' and msg in ('no self-intro',): continue   # "I'm a video editor" is part of the Flick template
        if re.search(rx, body, re.I | re.M): out.append(msg)
    links = re.findall(r'https?://\S+', body)
    if len(links) > 2: out.append(f'{len(links)} links (one, two max)')
    if style == 'proof' and not links: out.append('add one matching sample link')
    if style == 'ask' and links: out.append('ask-first has no links')
    if style in ('ask', 'proof', 'flick', 'best') and not re.search(r'\?|reply|👍|sure', body[-260:], re.I): out.append('end with one easy ask')
    if style == 'flick' and re.search(r'\{(name|niche|compliment|video)\}', body): out.append('a field is still empty')
    if lead and re.search(r'\bstart right away\b', body, re.I) and C.load_settings().get('capacity', 'OPEN') != 'OPEN':
        out.append('"start right away" only in OPEN mode')
    return out

# ---------------- selfie composite ----------------
def _coeffs(dst, src):
    """perspective coeffs mapping output(dst quad) -> input(src rect), pure python"""
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); B.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); B.append(v)
    n = 8; M = [A[i] + [B[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]; M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]

FX_DEFAULT = {'brightness': 1.0, 'contrast': 0.95, 'saturation': 0.97, 'glow': 0.1, 'soft': 0.6, 'reflect': 0.06, 'match': 0.5, 'grain': 1.0, 'falloff': 0.7,
              'temp': 0.0, 'tint': 0.0, 'g_temp': 0.0, 'g_tint': 0.0, 'g_bright': 1.0, 'g_contrast': 1.0, 'g_sat': 1.0}

def source_urls(lead):
    """images you can put on the monitor. Real frames from the video first (they look like you're actually editing it),
    then YouTube's own stills, then the thumbnail."""
    vid = (lead.get('latest_video') or {}).get('video_id')
    if not vid: return []
    return ([{'id': f'frame{i}', 'label': lab, 'urls': [], 'frame': i} for i, lab in ((1, 'Frame: early'), (2, 'Frame: middle'), (3, 'Frame: late'))] +
            [{'id': f'hq{i}', 'label': 'Still ' + str(i), 'urls': [f'https://i.ytimg.com/vi/{vid}/maxres{i}.jpg', f'https://i.ytimg.com/vi/{vid}/sd{i}.jpg', f'https://i.ytimg.com/vi/{vid}/hq{i}.jpg']} for i in (1, 2, 3)] +
            [{'id': 'thumb', 'label': 'Thumbnail', 'urls': [f'https://i.ytimg.com/vi/{vid}/maxresdefault.jpg', f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg']}])

def _fetch(urls):
    for u in urls:
        try:
            b = urllib.request.urlopen(u, timeout=15).read()
            if len(b) > 2000: return b
        except Exception: pass
    return None

def _tool(name):
    """ffmpeg / yt-dlp: the copies in C:\\ZXP\\bin first, then anything on PATH"""
    import shutil
    for d in (os.path.join(os.path.dirname(HERE), 'bin'), HERE, os.path.join(C.DATA, 'bin')):
        for n in (name + '.exe', name):
            if os.path.isfile(os.path.join(d, n)): return os.path.join(d, n)
    return shutil.which(name)

def _crop_bars(b):
    """YouTube stills come letterboxed (black bars): cut them off so only the picture goes on your monitor"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b)).convert('RGB'); g = im.convert('L').point(lambda v: 255 if v > 18 else 0)
        box = g.getbbox()
        if box and (box[2] - box[0]) * (box[3] - box[1]) < im.width * im.height * 0.97 and (box[2] - box[0]) > im.width * 0.5:
            o = io.BytesIO(); im.crop(box).save(o, 'JPEG', quality=93); return o.getvalue()
    except Exception: pass
    return b

_STREAM = {}; _SFAIL = {}; _FBLOCK = {}; import threading as _th; _LOCKS = {}; _LK = _th.Lock()
from concurrent.futures import ThreadPoolExecutor as _TPE
_FRAME_POOL = _TPE(max_workers=6)   # frames for several leads grab side by side
def _vlock(vid):
    with _LK: return _LOCKS.setdefault(vid, _th.Lock())

def _stream(vid):
    """the video's direct stream link (+ length). yt-dlp is the slow bit (~3-5 s), so the answer is kept in memory AND on disk
    until YouTube's link expires: reopening a lead, or the app, skips it."""
    st = _STREAM.get(vid)
    if st and st[2] > time.time() + 120: return st
    if time.time() - _SFAIL.get(vid, 0) < 600: return None   # just failed: don't make every tile wait for it again
    with _vlock(vid):
        if time.time() - _SFAIL.get(vid, 0) < 600: return None
        st = _STREAM.get(vid)
        if st and st[2] > time.time() + 120: return st
        f = os.path.join(C.DATA, 'frames', vid + '.stream.json')
        try:
            d = json.load(open(f, encoding='utf-8'))
            if d['exp'] > time.time() + 120 and d.get('hdr') is not None: st = _STREAM[vid] = (d['dur'], d['url'], d['exp'], d['hdr']); return st
        except Exception: pass
        args = ['--no-warnings', '--no-playlist', '-f', 'bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b/bv*/best',
                '--print', 'duration', '--print', 'urls', '--print', '%(http_headers)j', 'https://www.youtube.com/watch?v=' + vid]
        ls, errs = [], []
        for cmd in _ytdlp_cmds():
            try:
                p = subprocess.run(cmd + args, capture_output=True, text=True, timeout=60, creationflags=NOFLAGS)
                ls = [x.strip() for x in (p.stdout or '').splitlines() if x.strip() and not x.startswith('WARNING')]
                if len(ls) >= 2 and ls[1].startswith(('http', 'file:', '/')): break
                errs.append(f"{cmd[0]}: {(p.stderr or p.stdout or '').strip()[-400:]}"); ls = []
            except Exception as e: errs.append(f'{cmd[0]}: {e}')
        if len(ls) < 2:
            _flog(f'{vid}: no stream link\n  ' + '\n  '.join(errs)); _SFAIL[vid] = time.time(); return None
        try: dur = float(ls[0])
        except Exception: dur = 600.0
        url = ls[1].split('\n')[0]; m = re.search(r'[?&/]expire[=/](\d+)', url)
        try: hdr = json.loads(next(x for x in ls[2:] if x.startswith('{')))
        except Exception: hdr = {}
        exp = int(m.group(1)) if m else time.time() + 3 * 3600
        st = _STREAM[vid] = (dur, url, exp, hdr)
        try: json.dump({'dur': dur, 'url': url, 'exp': exp, 'hdr': hdr}, open(f, 'w', encoding='utf-8'))
        except Exception: pass
        return st

def _flog(msg):
    """frame problems go to SwipeReach\\logs\\frames.log so they can be fixed (YouTube changes things often)"""
    try:
        d = os.path.join(HERE, 'logs'); os.makedirs(d, exist_ok=True); fp = os.path.join(d, 'frames.log')
        if os.path.exists(fp) and os.path.getsize(fp) > 400_000:   # keep it small: last ~100KB only
            t = open(fp, encoding='utf-8', errors='ignore').read()[-100_000:]; open(fp, 'w', encoding='utf-8').write(t)
        with open(fp, 'a', encoding='utf-8') as f: f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg[-600:] + '\n')
    except Exception: pass

_YT_UPDATED = [False]
def _ytdlp_cmds():
    """yt-dlp breaks whenever YouTube changes: try the bundled exe, and if it fails, the always-current pip version
    (installed/updated automatically into the app's Python) and a self-updated exe"""
    import sys
    out = []; yd = _tool('yt-dlp')
    if yd: out.append([yd])
    def py():
        exe = sys.executable
        if exe.lower().endswith('pythonw.exe'): exe = exe[:-11] + 'python.exe'
        if not _YT_UPDATED[0] and not getattr(sys, 'frozen', False):   # update yt-dlp in the background (never makes you wait); the next try uses the fresh one
            _YT_UPDATED[0] = True
            def upd():
                try: subprocess.run([exe, '-m', 'pip', 'install', '-U', '-q', '--disable-pip-version-check', 'yt-dlp'], capture_output=True, timeout=300, creationflags=NOFLAGS)
                except Exception: pass
                if yd:
                    try: subprocess.run([yd, '-U'], capture_output=True, timeout=120, creationflags=NOFLAGS)
                    except Exception: pass
                _SFAIL.clear()
            _th.Thread(target=upd, daemon=True).start()
        return [exe, '-m', 'yt_dlp']
    class Lazy(list):   # only installs/updates if the bundled exe actually failed
        def __iter__(self):
            yield from list.__iter__(self)
            try:
                import importlib.util
                if importlib.util.find_spec('yt_dlp'): yield py()
                else: py()   # not installed yet: start installing it in the background, don't wait
            except Exception: pass
    return Lazy(out)

def real_frame(lead, i=1):
    """a real 720p frame from their latest video (no download: ffmpeg jumps to the moment and decodes one keyframe).
    Skips black / blank moments (fades, title cards). Cached on disk so the editor and the email use the same frame."""
    vid = (lead.get('latest_video') or {}).get('video_id')
    if not vid: return None
    d = os.path.join(C.DATA, 'frames'); os.makedirs(d, exist_ok=True); out = os.path.join(d, f'{vid}_{i}.jpg')
    if os.path.exists(out) and os.path.getsize(out) > 5000: return open(out, 'rb').read()
    ff = _tool('ffmpeg')
    if not ff: return None
    if time.time() - _FBLOCK.get(vid, 0) < 1800: return None   # YouTube just blocked this video: answer instantly instead of waiting minutes again
    st = _stream(vid)
    if not st: return None
    dur, url = st[0], st[1]; hdr = st[3] if len(st) > 3 else {}
    # YouTube refuses (403) a stream link requested without the same browser headers yt-dlp used to get it
    hargs = (['-user_agent', hdr['User-Agent']] if hdr.get('User-Agent') else []) + \
            (['-headers', ''.join(f'{k}: {v}\r\n' for k, v in hdr.items() if k != 'User-Agent')] if len(hdr) > 1 else [])
    if not url.startswith('http'): hargs = []
    from PIL import Image, ImageStat
    best = None; tmp = out + f'.{_th.get_ident()}.tmp.jpg'
    for k in range(4):   # try a few nearby moments, keep the first one with real picture in it
        t = max(5.0, dur * ({1: .22, 2: .48, 3: .72}.get(i, .4) + k * .04))
        r = subprocess.run([ff, '-hide_banner', '-loglevel', 'error'] + hargs + ['-ss', f'{t:.1f}', '-noaccurate_seek', '-skip_frame', 'nokey', '-i', url,
                            '-an', '-sn', '-dn', '-frames:v', '1', '-q:v', '2', '-y', tmp], capture_output=True, timeout=45, creationflags=NOFLAGS)
        if (r.returncode or not os.path.exists(tmp)) and k == 0:   # refused (403 / empty): one quick try letting yt-dlp fetch 2 seconds itself
            r = _frame_via_ytdlp(vid, t, tmp, ff) or r
        if r.returncode or not os.path.exists(tmp):
            _flog(f'{vid} frame {i}@{t:.0f}s: ffmpeg ' + (r.stderr or b'').decode('utf-8', 'ignore').strip()[-300:])
            if not best: _FBLOCK[vid] = time.time(); _STREAM.pop(vid, None)   # blocked: stop here (no more minute-long retries); other tiles answer instantly
            break
        im = Image.open(tmp).convert('L'); s_ = ImageStat.Stat(im); score = s_.stddev[0] + min(s_.mean[0], 90) / 3
        if not best or score > best[0]: best = (score, open(tmp, 'rb').read())
        if s_.mean[0] > 35 and s_.stddev[0] > 30: break
    try: os.remove(tmp)
    except Exception: pass
    if not best: return None
    b = _crop_bars(best[1]); open(out, 'wb').write(b); return b

def _frame_via_ytdlp(vid, t, out, ff):
    """fallback when YouTube blocks direct reads: yt-dlp downloads just 2 seconds around the moment (it handles YouTube's
    protections), then ffmpeg takes one frame from that tiny clip"""
    clip = out + '.clip.mp4'
    if time.time() - _FBLOCK.get(vid, 0) < 1800: return None
    for cmd in list(_ytdlp_cmds())[:1]:   # one quick try only
        try:
            subprocess.run(cmd + ['--no-warnings', '--no-playlist', '-f', 'bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/b',
                                  '--download-sections', f'*{t:.0f}-{t + 2:.0f}', '--ffmpeg-location', ff, '-o', clip, '--force-overwrites',
                                  'https://www.youtube.com/watch?v=' + vid], capture_output=True, timeout=25, creationflags=NOFLAGS)
            if os.path.exists(clip) and os.path.getsize(clip) > 1000:
                r = subprocess.run([ff, '-hide_banner', '-loglevel', 'error', '-i', clip, '-frames:v', '1', '-q:v', '2', '-y', out], capture_output=True, timeout=30, creationflags=NOFLAGS)
                try: os.remove(clip)
                except Exception: pass
                return r
        except Exception as e: _flog(f'{vid}: yt-dlp clip fallback {e}')
    return None

def prefetch_frames(lead):
    """grab all 3 frames for a lead in the background, side by side, so they're ready before you look"""
    if not (lead.get('latest_video') or {}).get('video_id'): return
    for i in (1, 2, 3): _FRAME_POOL.submit(lambda i=i: real_frame(lead, i))

def small_jpg(b, w=360):
    """tile-sized copy for the picker (a 720p frame is ~10x bigger than a tile needs)"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b)).convert('RGB')
        if im.width > w: im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
        o = io.BytesIO(); im.save(o, 'JPEG', quality=82); return o.getvalue()
    except Exception: return b

def frame_ready(lead, i=1):
    vid = (lead.get('latest_video') or {}).get('video_id')
    p = os.path.join(C.DATA, 'frames', f'{vid}_{i}.jpg')
    return bool(vid) and os.path.exists(p) and os.path.getsize(p) > 5000

_TB = {}
def thumb_bytes(lead, src='frame1', wait=False):
    """the picture for the screen/email. Instant: a real frame if it's already grabbed, otherwise YouTube's own still of the
    same moment right now (frame keeps grabbing in the background and replaces it when ready). wait=True blocks for the frame."""
    src = src or 'frame1'
    if str(src).startswith('file:'):
        try: return open(src[5:], 'rb').read()
        except Exception: return None
    S = source_urls(lead)
    near = 'hq' + src[5:] if src.startswith('frame') else ''   # same moment as YouTube's own still
    order = [x for x in S if x['id'] == src] + [x for x in S if x['id'] == near] + [x for x in S if x['id'] not in (src, near)]
    for s_ in order:
        if s_.get('frame'):
            if frame_ready(lead, s_['frame']) or wait:
                try: b = real_frame(lead, s_['frame'])
                except Exception: b = None
                if b: return b
            else: _FRAME_POOL.submit(real_frame, lead, s_['frame'])
            continue
        k = (s_['urls'][0] if s_['urls'] else '')
        b = _TB.get(k)
        if b is None:
            b = _fetch(s_['urls']); b = _crop_bars(b) if b and s_['id'] != 'thumb' else b
            if b: _TB[k] = b
        if b: return b
    lv = lead.get('latest_video') or {}
    return _fetch([lv['thumbnail']]) if lv.get('thumbnail') else None

def _grade(im, temp=0.0, tint=0.0, bright=1.0, contrast=1.0, sat=1.0):
    """colour grade: temperature (-1 cool/blue .. +1 warm/orange), tint (-1 green .. +1 magenta), then brightness/contrast/saturation"""
    from PIL import ImageEnhance
    temp, tint = float(temp or 0), float(tint or 0)
    if temp or tint:
        r, g, b = im.split()
        r = r.point(lambda v: min(255, int(v * (1 + 0.14 * temp + 0.03 * tint))))
        g = g.point(lambda v: min(255, int(v * (1 - 0.09 * tint + 0.02 * temp))))
        b = b.point(lambda v: min(255, int(v * (1 - 0.14 * temp + 0.03 * tint))))
        from PIL import Image as _I; im = _I.merge('RGB', (r, g, b))
    if float(bright) != 1: im = ImageEnhance.Brightness(im).enhance(float(bright))
    if float(contrast) != 1: im = ImageEnhance.Contrast(im).enhance(float(contrast))
    if float(sat) != 1: im = ImageEnhance.Color(im).enhance(float(sat))
    return im

def _camera_match(bg, warped, mask, quad, fx, stats=None, full=None):
    """make the pasted frame look PHOTOGRAPHED by the same camera instead of pasted on:
    1) colour + exposure pulled toward what the real screen looked like in your photo (white balance, brightness, contrast)
    2) the real screen's brightness falloff (viewing angle, panel unevenness) multiplied in
    3) the photo's own sensor grain added, so the screen isn't cleaner than everything around it"""
    import numpy as np
    from PIL import Image, ImageFilter
    B = np.asarray(bg, np.float32); W = np.asarray(warped, np.float32); M = np.asarray(mask, np.float32) / 255.0
    inside = M > 0.98
    if inside.sum() < 50: return warped
    a = float(fx.get('match', 0.5))
    if a > 0:
        # exposure: the real screen's brightness in your photo (not its colours: those belong to whatever was on it)
        lo, lw = B[inside].mean(), W[inside].mean() + 1e-3
        W = W * (1 + a * 0.5 * (lo / lw - 1))
        # camera white balance: the photo's overall tint (a cool camera makes screens look bluer, and so on)
        g = stats['mean'] if stats else B.reshape(-1, 3).mean(0); wb = g / (g.mean() + 1e-3)
        W = W * (1 + a * 0.6 * (wb - 1))[None, None, :]
    # brightness falloff across the real screen (very blurred luminance, so UI detail doesn't print through)
    r = max(4, int(max(full or bg.size) / 40))
    L = Image.fromarray(B.mean(2).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r))
    Lm = np.asarray(Image.fromarray((M * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r)), np.float32) / 255.0 + 1e-3
    Ln = np.asarray(L, np.float32) / Lm
    ref = Ln[inside].mean() + 1e-3
    fall = np.clip(Ln / ref, 0.8, 1.15) ** float(fx.get('falloff', 0.7))
    W = W * fall[..., None]
    # sensor grain measured from the photo itself
    if stats: sigma = stats['noise'] * float(fx.get('grain', 1.0))
    else:
        hp = B.mean(2) - np.asarray(Image.fromarray(B.mean(2).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2)), np.float32)
        sigma = float(np.median(np.abs(hp))) * 1.25 * float(fx.get('grain', 1.0))
    if sigma > 0.2: W = W + np.random.default_rng(7).normal(0, sigma, W.shape[:2])[..., None]
    return Image.fromarray(np.clip(W, 0, 255).astype(np.uint8))

_BASE = {}
def _base_img(path):
    """your desk photo, opened, turned the right way up and shrunk to 1600px ONCE (decoding a 12MP phone photo is the slow part)"""
    from PIL import Image, ImageOps
    k = (path, os.path.getmtime(path))
    if _BASE.get('k') != k:
        im = Image.open(path); im = ImageOps.exif_transpose(im).convert('RGB'); f = 1.0
        if im.width > 1600: f = 1600 / im.width; im = im.resize((1600, int(im.height * f)), Image.LANCZOS)
        import numpy as np
        from PIL import ImageFilter
        A = np.asarray(im, np.float32); Lg = A.mean(2)
        hp = Lg - np.asarray(Image.fromarray(Lg.astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2)), np.float32)
        _BASE.clear(); _BASE.update(k=k, im=im, f=f, stats={'mean': A.reshape(-1, 3).mean(0), 'noise': float(np.median(np.abs(hp))) * 1.25})
    return _BASE['im'].copy(), _BASE['f']

def selfie(lead, out_dir=None, params=None, preview_w=None):
    """your desk photo with their video on the monitor. params: quad (4 corners), src (frame1-3/hq1-3/thumb/file:path), fx sliders"""
    ensure('PIL'); from PIL import Image, ImageFilter, ImageEnhance, ImageChops
    S = C.load_settings(); base = S.get('selfie_path') or os.path.join(C.DATA, 'selfie.jpg')
    P = dict(lead.get('selfie_params') or {}, **(params or {}))
    if (P.get('mode') or S.get('img_mode') or 'frame') == 'frame':   # no selfie: just a clean frame from their video
        tb = thumb_bytes(lead, P.get('src') or 'frame1')
        if not tb: raise RuntimeError('Could not grab a frame from their video')
        im = Image.open(io.BytesIO(tb)).convert('RGB')
        if im.width > 1280: im = im.resize((1280, int(im.height * 1280 / im.width)), Image.LANCZOS)
        if preview_w and im.width > preview_w: im = im.resize((preview_w, int(im.height * preview_w / im.width)), Image.LANCZOS)
        out_dir = out_dir or os.path.join(C.DATA, 'selfies'); os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, re.sub(r'\W+', '_', lead.get('channel_id', 'x')) + '_frame' + ('_prev' if preview_w else '') + '.jpg'); im.save(p, quality=92)
        return p
    quad = P.get('quad') or S.get('monitor'); fx = {**FX_DEFAULT, **(S.get('selfie_fx') or {}), **(P.get('fx') or {})}
    if not os.path.exists(base): raise RuntimeError('No selfie yet: Setup > Your selfie')
    if not quad or len(quad) != 4: raise RuntimeError('Mark the 4 monitor corners first (Setup > Your selfie)')
    tb = thumb_bytes(lead, P.get('src') or 'frame1')
    if not tb: raise RuntimeError('Could not download that image from YouTube')
    import hashlib
    key = hashlib.md5(json.dumps([base, os.path.getmtime(base), quad, fx, P.get('src') or 'frame1', len(tb), preview_w], sort_keys=True, default=str).encode()).hexdigest()[:12]
    out_dir = out_dir or os.path.join(C.DATA, 'selfies'); os.makedirs(out_dir, exist_ok=True)
    cached = os.path.join(out_dir, re.sub(r'\W+', '_', lead.get('channel_id', 'x')) + '_' + key + '.jpg')
    if os.path.exists(cached): return cached   # same photo, corners, look and frame as last time: already made
    bg, bscale = _base_img(base); sh = Image.open(io.BytesIO(tb)).convert('RGB')
    pre = re.sub(r'\W+', '_', lead.get('channel_id', 'x')) + '_'
    try:   # keep only the latest few renders per lead
        old = sorted((os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith(pre)), key=os.path.getmtime)[:-8]
        for f in old: os.remove(f)
    except Exception: pass
    quad = [(x * bscale, y * bscale) for x, y in quad]   # corners are saved on the full photo; we work on the 1600px copy
    q = [tuple(map(float, p)) for p in quad]
    if preview_w and bg.width > preview_w * 1.5:   # editor preview: work at screen size so it comes back in a blink
        f = preview_w * 1.5 / bg.width; bg = bg.resize((int(bg.width * f), int(bg.height * f)), Image.LANCZOS); q = [(x * f, y * f) for x, y in q]
    # shrink the frame to about the size it really covers in the photo first: a big sharp image squeezed into a small
    # screen looks crisper than the camera could ever capture (the #1 "this is edited" tell)
    side = max(((q[1][0] - q[0][0]) ** 2 + (q[1][1] - q[0][1]) ** 2) ** .5, ((q[2][0] - q[3][0]) ** 2 + (q[2][1] - q[3][1]) ** 2) ** .5)
    tw = int(max(64, min(sh.width, side * 1.15)))
    sh = sh.resize((tw, max(36, int(sh.height * tw / sh.width))), Image.LANCZOS)
    sh = ImageEnhance.Brightness(sh).enhance(float(fx['brightness']))
    sh = ImageEnhance.Contrast(sh).enhance(float(fx['contrast']))
    sh = ImageEnhance.Color(sh).enhance(float(fx['saturation']))
    sh = _grade(sh, fx.get('temp', 0), fx.get('tint', 0))   # the screen image on its own
    # only the area around the screen is worked on (blur, glow, grain): ~10x less work than the whole photo
    full = bg; FW, FH = full.size; big = max(FW, FH)
    pad = int(big / 50 * 2.5) + 6
    xs, ys = [p[0] for p in q], [p[1] for p in q]
    box = (max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad), min(FW, int(max(xs)) + pad), min(FH, int(max(ys)) + pad))
    bg = full.crop(box); qc = [(x - box[0], y - box[1]) for x, y in q]
    w, h = sh.size
    co = _coeffs(qc, [(0, 0), (w, 0), (w, h), (0, h)])
    warped = sh.transform(bg.size, Image.PERSPECTIVE, co, Image.BICUBIC)
    scale = big / 1500.0   # blur/feather sized to the photo, so the sliders mean the same on any camera
    if float(fx['soft']) > 0: warped = warped.filter(ImageFilter.GaussianBlur(float(fx['soft']) * scale))
    mask = Image.new('L', (w, h), 255).transform(bg.size, Image.PERSPECTIVE, co, Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.6 * scale + 0.3))
    try: warped = _camera_match(bg, warped, mask, qc, fx, _BASE.get('stats'), (FW, FH))
    except Exception as e: print('camera match skipped:', e)
    if float(fx['reflect']) > 0:   # keep a bit of the real screen (reflections, glare) showing through
        warped = Image.blend(warped, bg, float(fx['reflect']))
    if float(fx['glow']) > 0:      # light spilling from the screen
        glow = warped.filter(ImageFilter.GaussianBlur(big / 60))
        gm = mask.filter(ImageFilter.GaussianBlur(big / 50)).point(lambda v: int(v * float(fx['glow'])))
        bg = Image.composite(ImageChops.screen(bg, glow), bg, gm)
    bg.paste(warped, (0, 0), mask)
    full.paste(bg, box[:2]); bg = full
    bg = _grade(bg, fx.get('g_temp', 0), fx.get('g_tint', 0), fx.get('g_bright', 1), fx.get('g_contrast', 1), fx.get('g_sat', 1))   # whole photo: selfie + screen together
    if preview_w and bg.width > preview_w: bg = bg.resize((preview_w, int(bg.height * preview_w / bg.width)), Image.LANCZOS)
    bg.save(cached, quality=88, optimize=False); return cached

# ---------------- Flick-style email (fields + template) ----------------
FLICK_SUBJECTS = ['An opportunity for you', 'An opportunity for you 👀', 'An opportunity for you, {name}', '{name}, an opportunity for you']
FLICK_TEMPLATE = """Hey {name},

I've been looking for an extra person to help with the following... and I noticed you're going at it within the {niche} niche.

I like {compliment}, especially in your recent video about {video}:

[IMAGE]

I'd love to help you make videos like this hit even harder (and save you time in the process).

I'm a video editor, and there's some specific stuff in this video I can see would work really well with my editing. Would you mind me sending over a 15-20 second edit, showing how it would look on your video?

Just reply with a "sure!" and I'll send a video of me and the edit.

(But only if you're open to a good editor who'll actually help with your channel 😉)"""

def template_text():
    t = _doc('email_template.txt')
    return t if '{compliment}' in t else FLICK_TEMPLATE

def fill(fields, template=None):
    f = {k: (fields.get(k) or '').strip() for k in ('name', 'niche', 'compliment', 'video')}
    body = (template or template_text())
    if not f['name']: body = body.replace('Hey {name},', 'Hey,').replace(', {name}', '').replace('{name}, ', '')
    for k, v in f.items(): body = body.replace('{' + k + '}', v)
    return body

def subject_for(fields, i=0):
    n = (fields.get('name') or '').strip(); opts = [x for x in FLICK_SUBJECTS if n or '{name}' not in x]
    return opts[i % len(opts)].replace('{name}', n)

FIELD_RULES = ("Write the 4 fields for Ritch's outreach email template (Flick Edits style). Give 3 different options for each, best first.\n"
  "- name: the owner's real first name only if the evidence is solid (see FIRST NAME rules), else \"\" for all 3.\n"
  "- niche: short lowercase phrase for what the channel is about, as a viewer would say it: 'golf coaching', 'vibe coding and ai tools', 'souls-like lore'. 2-5 words. Fits 'going at it within the ___ niche'.\n"
  "- compliment: a genuine, specific habit or strength from their videos, starting with 'your' or 'how you': 'your honesty about the prompts you use', "
  "'how you break a swing down into one simple fix', 'how you leave little gaps so kids can shout along'. It should read like genuine admiration, never a description. Base it on the transcript if given. 4-12 words, lowercase, no exclamation marks, never generic ('your content', 'your videos').\n"
  "- video: what their latest video is about in plain lowercase words (not the clickbait title): 'vibe coding an animated app with claude', 'fixing a slice with one grip change'. 4-12 words. Fits 'your recent video about ___'.\n"
  "No em-dashes. Never invent anything not supported by the facts.\n")

def write_fields(lead, engine='auto'):
    """3 suggestions for each field (Claude), with a no-AI fallback"""
    if engine != 'template': lead_transcript(lead)
    lv = lead.get('latest_video') or {}
    facts = {'channel': lead.get('title'), 'niche_hint': lead.get('niche'), 'about': (lead.get('about') or '')[:1200], 'email': lead.get('email'),
             'latest_video_title': lv.get('title'), 'recent_video_titles': lead.get('video_titles') or [],
             'latest_video_transcript_excerpt': lead.get('transcript') or '(not available)', 'first_name_set_by_ritch': lead.get('first_name') or ''}
    prompt = (VOICE + FIELD_RULES + NAME_RULES + "Facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1) +
              '\nReply ONLY with JSON: {"name": ["","",""], "niche": ["","",""], "compliment": ["","",""], "video": ["","",""]}')
    errs = []
    for e in ({'auto': ['claude', 'ollama'], 'claude': ['claude'], 'ollama': ['ollama']}.get(engine) or []):
        try:
            m = re.search(r'\{.*\}', ask_claude(prompt) if e == 'claude' else ask_ollama(prompt), re.S)
            d = json.loads(m.group(0)); out = {}
            for k in ('name', 'niche', 'compliment', 'video'):
                v = d.get(k) or []; v = [v] if isinstance(v, str) else v
                out[k] = [fix(str(x)).strip().rstrip('.!') for x in v][:3] or ['']
            if lead.get('first_name'): out['name'] = [lead['first_name']] + [x for x in out['name'] if x != lead['first_name']][:2]
            return {'fields': out, 'engine': e, 'transcript': bool(lead.get('transcript')), 'warning': '; '.join(errs)}
        except Exception as x: errs.append(f'{e}: {x}')
    title = re.sub(r'[^\w\s\'-]', '', lv.get('title') or 'your latest video').strip().lower()
    return {'fields': {'name': [lead.get('first_name') or ''], 'niche': [(lead.get('niche') or 'your').lower()],
                       'compliment': ['how clearly you explain things', 'how easy you make it to follow along', 'your no-fluff style'],
                       'video': [title]}, 'engine': 'template', 'warning': '; '.join(errs)}

def pic_name(lead):
    """'image.jpg': the same plain name Gmail gives any picture you paste in. A name built from their video title reads as
    auto-generated; 'noname' (no name at all) reads as odd. The ordinary default is the least noticeable."""
    return 'image.jpg'

def email_file(lead):
    """the email-sized picture (under ~95KB) saved as a file, made from the cached render: instant after the first time"""
    p = selfie(lead); out = os.path.join(os.path.dirname(p), 'email_' + os.path.basename(p))
    if not os.path.exists(out): open(out, 'wb').write(email_jpg(p))
    return out

def copy_image(path):
    """put the picture on the Windows clipboard so you can paste it into Gmail with Ctrl+V.
    Done in-process through the Windows clipboard API (instant); PowerShell only as a fallback."""
    if os.name != 'nt': raise RuntimeError('clipboard images need Windows')
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image
        o = io.BytesIO(); Image.open(path).convert('RGB').save(o, 'BMP'); dib = o.getvalue()[14:]   # BMP minus its file header = CF_DIB
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        k32.GlobalAlloc.restype = wintypes.HGLOBAL; k32.GlobalLock.restype = ctypes.c_void_p; k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        for _ in range(10):
            if u32.OpenClipboard(None): break
            time.sleep(0.05)
        else: raise RuntimeError('clipboard busy')
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x0002, len(dib)); ptr = k32.GlobalLock(h); ctypes.memmove(ptr, dib, len(dib)); k32.GlobalUnlock(h)
            if not u32.SetClipboardData(8, h): raise RuntimeError('SetClipboardData failed')   # 8 = CF_DIB
        finally: u32.CloseClipboard()
        return
    except Exception: pass
    ps = f"Add-Type -AssemblyName System.Windows.Forms,System.Drawing; [System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile('{path.replace(chr(39), chr(39)*2)}'))"
    subprocess.run(['powershell', '-NoProfile', '-STA', '-Command', ps], capture_output=True, timeout=30, creationflags=NOFLAGS)

# ---------------- Gmail drafts ----------------
SCOPES = ['https://www.googleapis.com/auth/gmail.compose', 'https://www.googleapis.com/auth/gmail.readonly']
_GM = {}
def gmail():
    """Gmail connection. The login (credentials) is kept in memory so each click skips reading + refreshing the token;
    the light service wrapper is made fresh per call because it isn't safe to share between threads"""
    cr = _GM.get('cr')
    if cr and cr.valid:
        from googleapiclient.discovery import build
        return build('gmail', 'v1', credentials=cr, cache_discovery=False)
    return _gmail_build()

def _gmail_build():
    ensure('google_auth_oauthlib', 'googleapiclient', 'google.oauth2')
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except (ImportError, AttributeError):
        ok, err = fix_google()
        raise RuntimeError('Updated your Google libraries so they match. Close SwipeReach and open it again, then click Connect Gmail.' if ok else 'Google libraries need updating and it failed: ' + err)
    tok = os.path.join(C.DATA, 'gmail_token.json'); cs = os.path.join(C.DATA, 'gmail_client.json')
    cr = Credentials.from_authorized_user_file(tok, SCOPES) if os.path.exists(tok) else None
    if not cr or not cr.valid:
        if cr and cr.expired and cr.refresh_token:
            try: cr.refresh(Request())
            except Exception: cr = None
        if cr and cr.valid: pass
        else:
            if not os.path.exists(cs): raise RuntimeError('Gmail not set up: put your Google OAuth client file at ' + cs)
            if not _GM.get('interactive'):   # never pop a Google login from a background check (it would wait forever and block Check Gmail)
                raise RuntimeError('Gmail needs you to sign in again: Setup > Gmail drafts > Connect')
            cr = InstalledAppFlow.from_client_secrets_file(cs, SCOPES).run_local_server(port=0, timeout_seconds=300)
        open(tok, 'w').write(cr.to_json())
    _GM['cr'] = cr
    return build('gmail', 'v1', credentials=cr, cache_discovery=False)

_EJ = {}
def email_jpg(path, limit=95_000):
    k = (path, os.path.getmtime(path))
    if k not in _EJ:
        if len(_EJ) > 50: _EJ.clear()
        _EJ[k] = _email_jpg(path, limit)
    return _EJ[k]

def _email_jpg(path, limit=95_000):
    """the picture as it goes in the email: 960px wide (sharp on phones and retina, shown at 480px) and under ~95KB,
    the size deliverability guides recommend for one inline image in a cold email"""
    from PIL import Image
    im = Image.open(path).convert('RGB')
    if im.width > 960: im = im.resize((960, int(im.height * 960 / im.width)), Image.LANCZOS)
    for q in (82, 76, 70, 64, 58, 52):
        o = io.BytesIO(); im.save(o, 'JPEG', quality=q, optimize=True, progressive=True)
        if o.tell() <= limit: break
        if q == 64 and im.width > 760: im = im.resize((760, int(im.height * 760 / im.width)), Image.LANCZOS)
    return o.getvalue()

def _mime(to, subject, body, img=None, thread_hdr=None, alt='a moment from your latest video', fname='video.jpg', sig=''):
    """plain + HTML email; the picture sits INLINE under the first lines (the 'visual hook'), not as an attachment.
    sig = your Gmail signature HTML, added at the bottom exactly like Gmail does (div class gmail_signature)"""
    from email.message import EmailMessage
    from email.utils import make_msgid
    import html as H
    m = EmailMessage(); m['To'] = to; m['Subject'] = subject
    if thread_hdr: m['In-Reply-To'] = m['References'] = thread_hdr
    plain = re.sub(r'\n*\[IMAGE\]\n*', '\n\n', body).strip()
    if sig:
        st = re.sub(r'(?i)<br\s*/?>|</(p|div)>', '\n', sig); st = H.unescape(re.sub(r'<[^>]+>', '', st)).strip()
        if st: plain += '\n\n' + re.sub(r'\n{3,}', '\n\n', st)
    m.set_content(plain)
    if img or sig:
        cid = make_msgid(domain='swipereach') if img else None
        paras = [p for p in re.split(r'\n\s*\n', body.strip()) if p.strip()]
        lnk = lambda t: re.sub(r'(https?://\S+)', r'<a href="\1">\1</a>', H.escape(t)).replace('\n', '<br>')
        k = next((i for i, p in enumerate(paras) if p.strip() == '[IMAGE]'), None)   # picture exactly where the template says
        if k is not None: paras.pop(k)
        else: k = min(2, len(paras))   # greeting + detail line, then the picture
        pic = f'<p><img src="cid:{cid[1:-1]}" alt="{H.escape(alt)}" width="480" style="max-width:480px;width:100%;border-radius:8px"></p>' if img else ''
        htm = ''.join(f'<p>{lnk(p)}</p>' for p in paras[:k]) + pic + ''.join(f'<p>{lnk(p)}</p>' for p in paras[k:])
        if sig: htm += f'<br><div dir="ltr" class="gmail_signature">{sig}</div>'
        m.add_alternative(f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">{htm}</div>', subtype='html')
        if img: m.get_payload()[1].add_related(email_jpg(img), maintype='image', subtype='jpeg', cid=cid, filename=fname, disposition='inline')
    return base64.urlsafe_b64encode(m.as_bytes()).decode()

_SIG = {}
def signature(svc=None):
    """your Gmail signature (Settings > Signature). Gmail only adds it when YOU press Compose, never to drafts made by an app,
    so we add the exact same one ourselves. Read once and kept."""
    mine = (C.load_settings().get('signature_html') or '').strip()
    if mine: return '' if mine == '-' else mine   # the one you pasted in Setup wins ('-' = no signature)
    if 'html' in _SIG and time.time() - _SIG['t'] < 3600: return _SIG['html']
    html = ''
    try:
        svc = svc or gmail(); me = _me(svc)
        sa = svc.users().settings().sendAs().list(userId='me').execute().get('sendAs', [])
        a = next((x for x in sa if (x.get('sendAsEmail') or '').lower() == me), None) or next((x for x in sa if x.get('isDefault')), None) or {}
        html = a.get('signature') or ''
    except Exception: html = _SIG.get('html', '')
    _SIG.update(html=html, t=time.time()); return html

def make_draft(lead, subject, body, img=None, draft_id=None):
    """one draft per lead: opening it again UPDATES the same Gmail draft instead of piling up copies in Drafts"""
    if not lead.get('email'): raise RuntimeError('This lead has no email yet')
    svc = gmail()
    msg = {'raw': _mime(lead['email'], subject, body, img, alt=('your video on my screen' if _img_mode(lead) == 'selfie' else 'a moment from your latest video'),
                        fname=pic_name(lead), sig=signature(svc))}
    d = None
    if draft_id:
        try: d = svc.users().drafts().update(userId='me', id=draft_id, body={'id': draft_id, 'message': msg}).execute()
        except Exception: d = None   # you deleted or sent it: make a fresh one
    if d is None: d = svc.users().drafts().create(userId='me', body={'message': msg}).execute()
    return {'draft_id': d['id'], 'thread_id': d['message'].get('threadId'), 'message_id': d['message']['id']}

# ---------------- follow-ups (Malice 1-2-2) ----------------
STAGE_A = [1, 2, 2]   # days after previous touch
import random
FU_VARIANTS = {   # Ritch's own follow-ups (stage A, no reply yet: 3 max, 1-2-2 days). Main wording first, one light variation so it never reads templated.
 'ask': [   # cold ask-first: they were asked "can I send a 20-second edit?"
   [lambda n: "Bumping this up in case it got buried", lambda n: "Bumping this up in case it got buried"],
   [lambda n: "Happy to just send the 20-second edit over if that's easier, just say the word", lambda n: "If it's easier, I can just send the 20-second edit over so you can see what I mean"],
   [lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch", lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch"]],
 'proof': [   # cold with a sample link
   [lambda n: "Bumping this up in case it got buried", lambda n: "Bumping this up in case it got buried"],
   [lambda n: "Happy to do a quick 20-second edit of one of your videos if that makes it an easier call", lambda n: "If it helps, I can do a quick 20-second edit of one of your videos first so you can see the style"],
   [lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch", lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch"]],
 'dm': [   # Discord / X / job-site chat: casual, lowercase, no "emails"
   [lambda n: "bumping this up in case it got buried", lambda n: "bumping this up in case it got buried"],
   [lambda n: "happy to start with a quick trial if that makes it an easier call", lambda n: "happy to start with a quick trial on a short section if that makes it easier"],
   [lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch", lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch"]],
 'warm': [   # job posts / they were hiring
   [lambda n: "Bumping this up in case it got buried", lambda n: "Bumping this up in case it got buried"],
   [lambda n: "Happy to start with a quick trial if that makes it an easier call", lambda n: "Happy to start with a quick trial on a short section if that makes it an easier call"],
   [lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch", lambda n: "One last note, if the timing works out down the line, feel free to reach out anytime!\n\n- Ritch"]],
}
FU_STYLE = {'flick': 'ask', 'best': 'ask'}   # both offered a 15-20 second edit, so the ask-first follow-ups fit
def fu_text(i, name='', style='ask', lead=None):
    if style == 'best' and lead and situation(lead)['awareness'].startswith('hiring'): style = 'warm'   # that email asked "still looking?"
    style = FU_STYLE.get(style, style)
    return random.choice(FU_VARIANTS.get(style, FU_VARIANTS['ask'])[min(i, 2)])(name if i == 0 else '')
FU_TEXT = [lambda n, i=i: fu_text(i, n) for i in range(3)]

def due(lead, now=None):
    """returns follow-up index due (0..2) or None"""
    now = now or datetime.now(timezone.utc); o = lead.get('outreach') or {}
    if lead.get('status') not in ('contacted', 'followup') or not o.get('sent_at'): return None
    n = len(o.get('followups', []))
    if n >= len(STAGE_A): return None
    last = datetime.fromisoformat((o['followups'][-1] if n else o['sent_at']).replace('Z', '+00:00'))
    if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
    return n if now - last >= timedelta(days=STAGE_A[n]) - timedelta(hours=2) else None

# Stage B (they replied, then went quiet) and C (you gave a price, then quiet): up to 5, 1-2-2-2-2 days, never skip the stage
STAGE_BC = [1, 2, 2, 2, 2]
BC_VARIANTS = {
 'replied': [lambda n: "just checking in on this?", lambda n: "does that work for you?", lambda n: f"{n}?" if n else "any thoughts on this?",
             lambda n: "checking in on this, if now's not a good time, all good, just let me know", lambda n: "still keen to help with this whenever you're ready, anything you need from me?"],
 'pitched': [lambda n: "just checking if that works for you?", lambda n: "let me know if that rate works or not, happy to discuss",
             lambda n: "are you still looking to get this video done?", lambda n: f"{n}?" if n else "any thoughts on the rate?", lambda n: "no worries if the timing's off, just let me know either way"]}
def bc_text(stage, i, name=''):
    return BC_VARIANTS['pitched' if stage == 'pitched' else 'replied'][min(i, 4)](name or '')

_SR = {}
def sent_recipients(days=90):
    """everyone you've emailed from Gmail in the last ~3 months, in a handful of calls (one list + batched header reads).
    Lets Check Gmail look up only the leads you actually emailed, instead of searching Gmail once per lead (2000 leads = minutes)."""
    if _SR.get('t') and time.time() - _SR['t'] < 120: return _SR['set']
    svc = gmail(); ids, tok = [], None
    while len(ids) < 2000:
        r = svc.users().messages().list(userId='me', q=f'in:sent newer_than:{days}d', maxResults=500, pageToken=tok).execute()
        ids += [m['id'] for m in r.get('messages', [])]; tok = r.get('nextPageToken')
        if not tok: break
    out = set()
    def cb(_id, resp, err):
        if err or not resp: return
        for h in resp.get('payload', {}).get('headers', []):
            if h['name'].lower() in ('to', 'cc', 'bcc'): out.update(e.lower() for e in re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', h['value']))
    if not hasattr(svc, 'new_batch_http_request'):
        for mid in ids: cb(mid, svc.users().messages().get(userId='me', id=mid, format='metadata', metadataHeaders=['To', 'Cc', 'Bcc']).execute(), None)
        _SR.update(t=time.time(), set=out); return out
    for i in range(0, len(ids), 90):
        b = svc.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 90]: b.add(svc.users().messages().get(userId='me', id=mid, format='metadata', metadataHeaders=['To', 'Cc', 'Bcc']))
        b.execute()
    _SR.update(t=time.time(), set=out); return out

def find_sent_thread(email):
    """the newest email YOU sent to this address (sent from Gmail yourself, not through the app) -> (thread_id, sent_time, subject)"""
    if not email: return None
    svc = gmail()
    r = svc.users().messages().list(userId='me', q=f'in:sent to:{email}', maxResults=5).execute().get('messages', [])
    if not r: return None
    oldest = None
    for m in r:   # the thread of the most recent sent message; its first message is your initial email
        full = svc.users().messages().get(userId='me', id=m['id'], format='metadata', metadataHeaders=['Subject']).execute()
        t = datetime.fromtimestamp(int(full.get('internalDate', 0)) / 1000, timezone.utc)
        subj = next((h['value'] for h in full['payload']['headers'] if h['name'] == 'Subject'), '')
        if oldest is None or t > oldest[1]: oldest = (full['threadId'], t, subj)
    if not oldest: return None
    # read the whole conversation: your first email, follow-ups you already sent, and whether they replied
    me = svc.users().getProfile(userId='me').execute()['emailAddress'].lower()
    th = svc.users().threads().get(userId='me', id=oldest[0], format='metadata', metadataHeaders=['From', 'Subject']).execute()
    ts = lambda m: datetime.fromtimestamp(int(m.get('internalDate', 0)) / 1000, timezone.utc)
    msgs = [m for m in th['messages'] if 'DRAFT' not in m.get('labelIds', [])]
    frm = lambda m: next((h['value'] for h in m['payload']['headers'] if h['name'] == 'From'), '').lower()
    mine = [ts(m) for m in msgs if me in frm(m)]; theirs = [m for m in msgs if me not in frm(m)]
    first = msgs[0] if msgs else None
    subj = next((h['value'] for h in (first or {}).get('payload', {}).get('headers', []) if h['name'] == 'Subject'), oldest[2])
    return {'thread_id': oldest[0], 'sent_at': (mine[0] if mine else oldest[1]).isoformat(), 'subject': subj,
            'followups': [t.isoformat() for t in mine[1:]], 'replied': bool(theirs)}

AUTO_FROM = re.compile(r'(?i)mailer-daemon|postmaster|no-?reply|noreply|do-not-reply|notifications?@|bounce')
AUTO_SUBJ = re.compile(r'(?i)^(automatic reply|auto[- ]?reply|out of (the )?office|ooo\b|delivery status notification|undeliverable|mail delivery (failed|subsystem)|returned mail|delivery (has )?failed|autoreply)')
_ME = {}
def _me(svc):
    if 'e' not in _ME: _ME['e'] = svc.users().getProfile(userId='me').execute()['emailAddress'].lower()
    return _ME['e']

def thread_info(lead):
    """the real state of this lead's Gmail thread: your sent messages (with real send times), their real replies,
    bounces and auto-replies (out of office) ignored, drafts waiting"""
    svc = gmail(); o = lead.get('outreach') or {}
    me = _me(svc)
    t = svc.users().threads().get(userId='me', id=o['thread_id'], format='metadata', metadataHeaders=['From', 'Subject']).execute()
    hdr = lambda m, k: next((h['value'] for h in m.get('payload', {}).get('headers', []) if h['name'] == k), '')
    bounced = any(re.search(r'(?i)mailer-daemon|postmaster', hdr(m, 'From')) or re.search(r'(?i)delivery status notification|undeliverable|delivery (has )?failed|address not found', hdr(m, 'Subject'))
                  for m in t['messages'])
    msgs = [m for m in t['messages'] if 'DRAFT' not in m.get('labelIds', [])
            and not (me not in hdr(m, 'From').lower() and (AUTO_FROM.search(hdr(m, 'From')) or AUTO_SUBJ.search(hdr(m, 'Subject'))))]
    has_draft = any('DRAFT' in m.get('labelIds', []) for m in t['messages'])
    ts = lambda m: datetime.fromtimestamp(int(m.get('internalDate', 0)) / 1000, timezone.utc)
    mine = [ts(m) for m in msgs if me in hdr(m, 'From').lower()]
    theirs = [ts(m) for m in msgs if me not in hdr(m, 'From').lower()]
    if not msgs: return {'has_draft': has_draft, 'last_me': True, 'last_t': None, 'their_last_t': None, 'mine': [], 'theirs': [], 'bounced': bounced}
    return {'has_draft': has_draft, 'last_me': me in hdr(msgs[-1], 'From').lower(), 'last_t': ts(msgs[-1]),
            'their_last_t': theirs[-1] if theirs else None, 'mine': mine, 'theirs': theirs, 'bounced': bounced}

def followup_draft(lead, i, text=None):
    o = lead['outreach']; svc = gmail()
    t = svc.users().threads().get(userId='me', id=o['thread_id'], format='metadata', metadataHeaders=['Message-ID', 'Subject']).execute()
    hdrs = {h['name']: h['value'] for h in t['messages'][-1]['payload']['headers']}
    subj = hdrs.get('Subject', o.get('subject', '')); subj = subj if subj.lower().startswith('re:') else 'Re: ' + subj
    raw = _mime(lead['email'], subj, text or fu_text(i, lead.get('first_name') or '', o.get('style') or 'ask', lead), None, hdrs.get('Message-ID'))
    return svc.users().drafts().create(userId='me', body={'message': {'raw': raw, 'threadId': o['thread_id']}}).execute()['id']

def check_thread(lead):
    """'replied' if anyone other than me wrote in the thread, 'sent' if my draft went out, else 'draft'"""
    svc = gmail(); o = lead.get('outreach') or {}
    me = svc.users().getProfile(userId='me').execute()['emailAddress'].lower()
    t = svc.users().threads().get(userId='me', id=o['thread_id'], format='metadata', metadataHeaders=['From', 'Date']).execute()
    st = 'draft'; has_draft = False
    for m in t['messages']:
        if 'DRAFT' in m.get('labelIds', []): has_draft = True; continue
        frm = next((h['value'] for h in m['payload']['headers'] if h['name'] == 'From'), '').lower()
        if me not in frm: return 'replied', has_draft
        st = 'sent'
    return st, has_draft

# ---------------- warm: job posts (YTJobs / Discord paste) ----------------
def triage_job(text, mode='OPEN', engine='auto'):
    prompt = (_master() + "\n\n=====\nTASK A+B for ONE pasted job post (YTJobs or Discord). CAPACITY MODE: " + mode +
        ".\n\n===== RITCH'S CONCISE OUTREACH SYSTEM =====\n" + _rules() + "\n=====\n"
        + VOICE + "Apply hard exclusions, workload filter, rate sanity check and staleness. If it fits, also write the application using the WARM formulas above "
        "(email 50-90 words, ~120 only if they asked numbered questions; Discord/DM ~35 words, no subject) "
        "(email or DM depending on where it was posted).\nPost:\n<<<\n" + text[:8000] + "\n>>>\n"
        'Reply ONLY with JSON: {"verdict":"APPLY|MAYBE|SKIP","why":"one or two lines","rate":"effective hourly or TBD",'
        '"contact":"email/discord handle/where to reply if visible, else empty","subject":"","body":""}')
    errs = []
    for e in ({'auto': ['claude', 'ollama'], 'claude': ['claude'], 'ollama': ['ollama']}.get(engine) or []):
        try:
            m = re.search(r'\{.*\}', ask_claude(prompt) if e == 'claude' else ask_ollama(prompt), re.S)
            d = json.loads(m.group(0)); d['body'] = fix(d.get('body', '')); d['subject'] = fix(d.get('subject', ''))
            d['engine'] = e; d['issues'] = check(d.get('subject') or '', d['body'], None, 'warm' if d.get('subject') else 'dm') if d['body'] else []; return d
        except Exception as x: errs.append(f'{e}: {x}')
    return {'verdict': '?', 'why': 'No AI available: ' + '; '.join(errs), 'body': '', 'subject': '', 'engine': 'none', 'issues': []}

def extract_contacts(text):
    em = sorted(set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)))
    yt = sorted(set(re.findall(r'(?:youtube\.com/(?:@[\w.-]+|channel/[\w-]+|c/[\w.-]+)|youtu\.be/[\w-]+)', text)))
    dc = sorted(set(re.findall(r'(?:discord(?:app)?\.com/users/\d+|@[\w.]{2,32}#?\d{0,4})', text)))
    return {'emails': em, 'youtube': yt, 'discord': dc}
