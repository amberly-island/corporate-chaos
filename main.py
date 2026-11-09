import os, time, uuid, secrets, base64, io, threading, re
from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO, emit
import qrcode

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('APP_SECRET', secrets.token_hex(24))
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')
HOST_KEY = os.environ.get('HOST_KEY') or secrets.token_urlsafe(24)
if not os.environ.get('HOST_KEY'):
    print('WARNING: HOST_KEY secret not set. Temporary host dashboard: /host/' + HOST_KEY, flush=True)
# Public student URL for the QR code. Hugging Face Spaces sets SPACE_HOST; Render sets RENDER_EXTERNAL_URL.
PUBLIC_URL = (os.environ.get('PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL')
              or ('https://' + os.environ['SPACE_HOST'] if os.environ.get('SPACE_HOST') else '')).rstrip('/')
LOCK = threading.RLock()
ROUND_SECONDS = int(os.environ.get('ROUND_SECONDS', '30'))

SCENARIOS = [
    {'title':'The Hiring Dilemma', 'concept':'Attraction–Selection–Attrition (ASA)',
     'story':'Your company says collaboration matters, but needs a new manager quickly. A technically brilliant applicant has a history of conflict. Another applicant is less experienced but works well with teams. What do you do?',
     'choices':[
         {'key':'A','text':'Hire the technically brilliant candidate','delta':[20,-12,-12],'outcome':'Productivity improves quickly, but coworkers report conflict. Skills helped performance; behavior strained collaboration.'},
         {'key':'B','text':'Hire the collaborative candidate and invest in training','delta':[2,15,13],'outcome':'Training takes time, but employees support the new hire. Culture fit helps integration, though it should not become an excuse to exclude different perspectives.'},
         {'key':'C','text':'Extend the search and use structured skill-and-values criteria','delta':[-10,5,12],'outcome':'The vacancy slows work initially, but a structured search supports clearer, fairer hiring standards.'}]
    },
    {'title':'The Employee Crisis','concept':'Espoused vs. enacted values',
     'story':'Your top salesperson drives 30% of revenue but repeatedly humiliates coworkers. Your company publicly promises respect and teamwork. What should leadership do?',
     'choices':[
         {'key':'A','text':'Terminate the employee after confirming the misconduct','delta':[-18,18,17],'outcome':'Revenue dips as the role is filled, but the team sees that respect is not just a slogan.'},
         {'key':'B','text':'Set a formal behavior plan, support coaching, and enforce deadlines','delta':[4,13,13],'outcome':'The company preserves expertise for now while making accountability visible. Follow-through will matter.'},
         {'key':'C','text':'Ignore the complaints to protect revenue','delta':[17,-23,-24],'outcome':'Sales stay high briefly, but employees see a gap between stated values and actual rewards.'}]
    },
    {'title':'Rapid Expansion','concept':'Organizational socialization',
     'story':'Your company is expanding fast. New hires are unclear about expectations, and productivity is inconsistent. How will you bring them into the culture?',
     'choices':[
         {'key':'A','text':'Introduce structured onboarding and formal training','delta':[-8,12,18],'outcome':'Training costs time now, but new hires learn consistent expectations and practices.'},
         {'key':'B','text':'Pair new employees with experienced mentors','delta':[3,16,15],'outcome':'Mentors transmit workplace norms and help new employees become connected contributors.'},
         {'key':'C','text':'Skip formal support and focus on immediate output','delta':[18,-18,-20],'outcome':'Output rises in the short term, but unclear expectations cause frustration and weaker integration.'}]
    },
    {'title':'The Culture Test','concept':'Strong cultures: advantages and disadvantages',
     'story':'Sales fall sharply and investors want action. Employees are anxious, but your company needs to adapt. Which response do you choose?',
     'choices':[
         {'key':'A','text':'Cut costs rapidly, including immediate layoffs','delta':[22,-26,-20],'outcome':'Costs fall quickly, but sudden cuts damage trust and can undermine the culture.'},
         {'key':'B','text':'Protect all existing jobs and preserve current practices','delta':[-19,15,4],'outcome':'Employees value the support, but refusing to adapt strains the business. Stability can become rigidity.'},
         {'key':'C','text':'Explain the challenge, redesign operations, and share sacrifices','delta':[9,12,15],'outcome':'Transparent changes ask everyone to adapt while keeping the organization aligned with its values.'}]
    }
]

def new_state():
    return {'round':0,'phase':'lobby','deadline':None,'teams':{},'registered_order':[], 'session':uuid.uuid4().hex}
STATE=new_state()

def clamp(x): return max(0,min(100,x))
def apply_branch(team, round_idx, choice):
    changes=[0,0,0]; details=[]
    if round_idx == 1 and team['history'].get('0') == 'A':
        changes=[0,-4,-4]; details.append('Earlier risky hiring increased the consequences of this conflict (−4 trust, −4 culture).')
    if round_idx == 3:
        if team['history'].get('2') == 'B':
            changes=[changes[0]+3,changes[1]+4,changes[2]+2]
            details.append('Your earlier mentoring program improves resilience (+3 performance, +4 trust, +2 culture).')
        elif team['history'].get('2') == 'C':
            changes=[changes[0]-3,changes[1]-4,changes[2]-3]
            details.append('Earlier weak onboarding makes change harder (−3 performance, −4 trust, −3 culture).')
    return changes, details

def qr_data(url):
    if not url:return ''
    img=qrcode.make(url)
    b=io.BytesIO();img.save(b,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()

def public_state():
    with LOCK:
        teams=[]
        for tid in STATE['registered_order']:
            t=STATE['teams'][tid]
            teams.append({'id':tid,'name':t['name'],'perf':t['perf'],'trust':t['trust'],'culture':t['culture'],
                          'total':t['perf']+t['trust']+t['culture'], 'submitted':STATE['round'] in t['submitted'],
                          'results':t['results'].get(str(STATE['round'])) if STATE['phase'] in ('revealed','finished') else None,
                          'history':t['history'] if STATE['phase']=='finished' else None})
        teams.sort(key=lambda t:(-t['total'],-t['trust'],-t['culture'], t['name'].casefold()))
        return {'round':STATE['round'],'phase':STATE['phase'],'deadline':STATE['deadline'],
                'teams':teams,'scenarios':SCENARIOS,'round_seconds':ROUND_SECONDS,'server_now':time.time(),'join_url':PUBLIC_URL,
                'qr':qr_data(PUBLIC_URL) if PUBLIC_URL else '', 'session':STATE['session']}

def broadcast(): socketio.emit('state_update',public_state())

def lock_if_due():
    with LOCK:
        if STATE['phase']=='open' and STATE['deadline'] is not None and time.time() >= STATE['deadline']:
            STATE['phase']='locked';STATE['deadline']=None;return True
    return False

def timer_thread(session):
    while True:
        socketio.sleep(0.35)
        with LOCK:
            if STATE['session'] != session:return
            if STATE['phase'] != 'open':return
        if lock_if_due():broadcast();return

def host_authorized(d):return secrets.compare_digest(str(d.get('key','')),HOST_KEY)
def captain_team(d):
    team=STATE['teams'].get(str(d.get('team_id','')))
    if not team:return None
    return team if secrets.compare_digest(str(d.get('token','')),team['token']) else None

@app.route('/')
def index():return render_template_string(HTML, mode='player')
@app.route('/host/<key>')
def host(key):
    if not secrets.compare_digest(key,HOST_KEY):return 'Unauthorized host',403
    return render_template_string(HTML, mode='host')
@app.route('/health')
def health():return jsonify({'ok':True,'phase':STATE['phase']})
@app.route('/internal/public-url',methods=['POST'])
def set_url():
    global PUBLIC_URL
    data=request.get_json(silent=True) or {}
    if not host_authorized(data):return jsonify({'error':'unauthorized'}),403
    url=str(data.get('url','')).strip().rstrip('/')
    if not url.startswith('https://'):return jsonify({'error':'https URL required'}),400
    PUBLIC_URL=url;broadcast();return jsonify({'ok':True})

@socketio.on('connect')
def connect(auth=None):emit('state_update',public_state())
@socketio.on('register_team')
def register(data):
    data=data if isinstance(data,dict) else {}
    name=str(data.get('name','')).strip()
    if not re.fullmatch(r'[\w .&\-]{2,28}',name,flags=re.UNICODE):return {'ok':False,'error':'Use 2–28 letters, numbers, spaces, &, hyphens or periods.'}
    with LOCK:
        if STATE['phase']!='lobby':return {'ok':False,'error':'Registration is closed. Ask the host to reset.'}
        if len(STATE['teams'])>=12:return {'ok':False,'error':'Maximum of 12 teams reached.'}
        if any(t['name'].casefold()==name.casefold() for t in STATE['teams'].values()):return {'ok':False,'error':'That team name is already taken.'}
        tid=uuid.uuid4().hex; token=secrets.token_urlsafe(32)
        STATE['teams'][tid]={'id':tid,'name':name,'token':token,'perf':50,'trust':50,'culture':50,'submitted':{},'history':{},'results':{}}
        STATE['registered_order'].append(tid)
        session=STATE['session']
    broadcast();return {'ok':True,'team_id':tid,'token':token,'session':session}

@socketio.on('submit_choice')
def submit(data):
    data=data if isinstance(data,dict) else {}
    with LOCK:
        team=captain_team(data)
        if not team:return {'ok':False,'error':'Captain session not authorized.'}
        if lock_if_due():broadcast()
        if STATE['phase']!='open':return {'ok':False,'error':'This round is not accepting answers.'}
        ri=STATE['round']; choice=data.get('choice')
        if choice not in ('A','B','C'):return {'ok':False,'error':'Invalid choice.'}
        if ri in team['submitted']:return {'ok':False,'error':'Your decision is already locked.'}
        team['submitted'][ri]=choice
    broadcast();return {'ok':True,'message':'Decision locked.'}

@socketio.on('host_action')
def host_action(data):
    global STATE
    data=data if isinstance(data,dict) else {}
    if not host_authorized(data):return {'ok':False,'error':'Host authorization required.'}
    action=data.get('action'); new_timer=False
    with LOCK:
        if action=='start':
            if STATE['phase'] not in ('lobby','ready'):return {'ok':False,'error':'Cannot start this round yet.'}
            if not STATE['teams']:return {'ok':False,'error':'No teams registered.'}
            STATE['phase']='open';STATE['deadline']=time.time()+ROUND_SECONDS
            session=STATE['session'];new_timer=True
        elif action=='lock':
            if STATE['phase']!='open':return {'ok':False,'error':'Round is not open.'}
            STATE['phase']='locked';STATE['deadline']=None
        elif action=='reveal':
            if STATE['phase']!='locked':return {'ok':False,'error':'Lock answers before revealing.'}
            ri=STATE['round']; scenario=SCENARIOS[ri]
            for t in STATE['teams'].values():
                selected=t['submitted'].get(ri)
                if selected:
                    c=next(x for x in scenario['choices'] if x['key']==selected)
                    branch,extra=apply_branch(t,ri,selected)
                    delta=[c['delta'][i]+branch[i] for i in range(3)]
                    prev=[t['perf'],t['trust'],t['culture']]
                    nextvals=[clamp(prev[i]+delta[i]) for i in range(3)]
                    t['perf'],t['trust'],t['culture']=nextvals
                    t['history'][str(ri)]=selected
                    t['results'][str(ri)]={'choice':selected,'outcome':c['outcome'],'extra':' '.join(extra),
                                             'delta':[nextvals[i]-prev[i] for i in range(3)]}
                else:
                    t['history'][str(ri)]='—'
                    t['results'][str(ri)]={'choice':'—','outcome':'No decision submitted. No score change this round.', 'extra':'','delta':[0,0,0]}
            STATE['phase']='revealed'
        elif action=='next':
            if STATE['phase']!='revealed':return {'ok':False,'error':'Reveal results before continuing.'}
            if STATE['round']==len(SCENARIOS)-1:STATE['phase']='finished'
            else:STATE['round']+=1;STATE['phase']='ready'
        elif action=='reset':
            STATE=new_state()
        else:return {'ok':False,'error':'Unknown action.'}
    broadcast()
    if new_timer:socketio.start_background_task(timer_thread,session)
    return {'ok':True}

HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Corporate Chaos</title>\n<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>\n<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">\n<style>\n:root{--ink:#15243b;--muted:#64748b;--line:#dce8f1;--blue:#448fe7;--teal:#23b6b0;--bg:#f6f9fd;--white:#fff;--green:#12785e;--red:#ba3d4b}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top right,#baf4ef70,transparent 38%),radial-gradient(ellipse at bottom left,#9fbeff70,transparent 42%),var(--bg);font-family:Inter,system-ui,sans-serif;color:var(--ink);min-height:100vh;padding:22px}.shell{max-width:1150px;margin:auto}.header{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:24px}.wordmark{font-size:24px;letter-spacing:-1px;font-weight:800}.wordmark span{color:var(--teal)}.tiny{font-size:12px;letter-spacing:1.6px;font-weight:700;color:#278c98;text-transform:uppercase}.card{background:#ffffffec;border:1px solid var(--line);border-radius:23px;box-shadow:0 14px 45px #3d84ad13;padding:26px;margin-bottom:18px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.split{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(260px,.85fr);gap:18px}.metric{padding:16px;background:#f4f8fc;border-radius:15px;border:1px solid #e1eaf3}.metric .val{font-size:28px;font-weight:800}.metric .label{font-size:12px;color:var(--muted);margin-top:5px}h1{font-size:clamp(28px,4vw,46px);letter-spacing:-1.8px;margin:9px 0 16px}h2{font-size:23px;letter-spacing:-.6px;margin:8px 0 14px}h3{margin:8px 0 12px;font-size:16px}p{line-height:1.65;color:#394862}button{font:inherit;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}.btn{padding:13px 17px;border-radius:12px;background:linear-gradient(100deg,var(--blue),var(--teal));border:0;color:white;font-weight:700;min-height:46px}.btn.alt{background:#eef4fa;color:var(--ink);border:1px solid var(--line)}.btn.danger{background:#fff1f1;color:#b91c1c;border:1px solid #fecaca}.controls{display:flex;gap:10px;flex-wrap:wrap}.choice{display:block;width:100%;text-align:left;border:1px solid #d9e7f3;background:#f8fbfe;border-radius:14px;padding:18px;margin:10px 0;font-weight:600;line-height:1.5;color:var(--ink)}.choice b{color:#277bb1;margin-right:8px}.choice.sel{border:2px solid var(--blue);background:#eaf6ff}.choice:disabled{opacity:.6}.badge{background:#dcf7f2;padding:8px 13px;border-radius:99px;color:#087c74;font-size:12px;font-weight:800;white-space:nowrap}.row{display:flex;justify-content:space-between;gap:15px;align-items:center}.teamline{padding:13px;border:1px solid var(--line);border-radius:12px;margin:7px 0;background:#fafcff}.quiet{font-size:13px;color:var(--muted)}.countdown{font-size:38px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--blue)}.msg{padding:12px;border-radius:10px;background:#e7f7f0;color:#165f49;margin:12px 0}.error{background:#fff0f0;color:#a32437}input{width:100%;font:inherit;padding:16px;border:1px solid #cfdce8;border-radius:13px;margin:12px 0}.qr{width:205px;max-width:100%;image-rendering:pixelated}.results{background:#f1faf8;border:1px solid #caebe1;border-radius:14px;padding:18px;margin:12px 0}.delta{font-weight:700}.positive{color:var(--green)}.negative{color:var(--red)}.mini-table{width:100%;border-collapse:collapse}.mini-table th,.mini-table td{padding:11px 6px;text-align:left;border-bottom:1px solid #edf0f4;font-size:13px}.mini-table th{color:var(--muted);font-size:11px;text-transform:uppercase}.highlight{color:#0b918b}.scroll{overflow-x:auto}.center{text-align:center}.step{margin-top:16px}.callout{background:#f6f8fc;border-left:3px solid var(--teal);padding:13px 16px;border-radius:7px}.leader{font-size:17px;font-weight:800}.confetti{font-size:48px}.notice{padding:10px 14px;background:#fff8e7;color:#885e12;border-radius:10px;font-size:13px}.footer{color:#8291a4;text-align:center;font-size:12px;margin:24px 0}.rules{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:8px 0 18px}.rule{display:flex;gap:14px;padding:16px;border:1px solid var(--line);border-radius:14px;background:#fafcff}.rule .ri{font-size:26px;line-height:1}.rule p{margin:5px 0 0;font-size:14px;line-height:1.5}.waiting{font-size:17px;font-weight:700;text-align:center;padding:16px} @media(max-width:780px){body{padding:13px}.card{padding:18px}.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.metric{padding:10px}.metric .val{font-size:22px}.split{grid-template-columns:1fr}.rules{grid-template-columns:1fr}.row{align-items:flex-start}.controls .btn{flex:1}h1{font-size:31px}} \n</style></head><body><div class="shell"><div class="header"><div class="wordmark">Corporate <span>Chaos</span><div class="tiny">MGMT 304 • Organizational culture</div></div><span class="badge" id="status">Connecting</span></div><main id="app"><div class="card">Connecting to game… If this message remains, check the public link and browser console.</div></main><div class="footer">A fictional classroom simulation • Results are illustrative, not real company metrics</div></div>\n<script>\nconst MODE={{ mode|tojson }};const socket=io({transports:[\'polling\',\'websocket\'],reconnection:true});\nconst hostKey=MODE===\'host\'?location.pathname.split(\'/\').pop():null;\nlet state=null, selected=null, flash=\'\',flashError=false;const stored=()=>{try{return JSON.parse(localStorage.getItem(\'cc_captain\')||\'null\')}catch{return null}};let captain=stored();\nfunction esc(s){return String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]))}\nfunction showError(s){flash=s;flashError=true;render();}function notice(s){flash=s;flashError=false;render()}\nfunction emitAck(event,payload,onOk){socket.emit(event,payload,res=>{if(!res?.ok){showError(res?.error||\'Request failed. Try again.\');return}flash=\'\';if(onOk)onOk(res);else render()})}\nfunction captainNow(){return captain&&state&&captain.session===state.session&&state.teams.some(t=>t.id===captain.team_id)}\nfunction register(){const input=document.getElementById(\'name\');const name=input?.value.trim();if(!name){showError(\'Enter your team name.\');return}emitAck(\'register_team\',{name},r=>{captain={team_id:r.team_id,token:r.token,session:r.session};localStorage.setItem(\'cc_captain\',JSON.stringify(captain));render()})}\nfunction submit(){if(!captainNow()||!selected)return;emitAck(\'submit_choice\',{team_id:captain.team_id,token:captain.token,choice:selected},()=>{notice(\'Your decision is locked. Wait for the host to reveal the consequences.\')})}\nfunction host(action){if(action===\'reset\'&&!confirm(\'Reset ALL teams and scores? Captains will need to register again.\'))return;emitAck(\'host_action\',{key:hostKey,action},()=>render())}\nfunction fmtDelta(v){return `<span class="delta ${v>=0?\'positive\':\'negative\'}">${v>0?\'+\':\'\'}${v}</span>`}\nfunction timerText(){if(!state||state.phase!==\'open\')return \'\';return `${Math.max(0,Math.ceil(state.deadline-Date.now()/1000))}s`}\nfunction tick(){document.querySelectorAll(\'[data-timer]\').forEach(e=>e.textContent=timerText())}\nfunction metric(label,value){return `<div class="metric"><div class="val">${value}</div><div class="label">${label}</div></div>`}\nfunction choices(sc,interactive){return sc.choices.map(c=>`<button class="choice ${selected===c.key?\'sel\':\'\'}" ${interactive?\'\':\'disabled\'} onclick="choose(\'${c.key}\')"><b>${esc(c.key)}</b>${esc(c.text)}</button>`).join(\'\')}\nfunction choose(key){if(!state||state.phase!==\'open\'||!captainNow())return;let t=state.teams.find(t=>t.id===captain.team_id);if(t?.submitted)return;selected=key;render()}\nfunction scoreboard(){if(!state.teams.length)return \'<p class="quiet">Teams appear here when captains register.</p>\';let rows=state.teams.map((t,i)=>`<tr><td>${i===0?\'🏆 \':\'\'}${i+1}</td><td><b>${esc(t.name)}</b></td><td>${t.perf}</td><td>${t.trust}</td><td>${t.culture}</td><td><b>${t.total}</b></td></tr>`).join(\'\');return `<div class="scroll"><table class="mini-table"><thead><tr><th>#</th><th>Company</th><th>Performance</th><th>Trust</th><th>Culture</th><th>Total</th></tr></thead><tbody>${rows}</tbody></table></div>`}\nfunction results(){let items=state.teams.map(t=>{const r=t.results;if(!r)return \'\';return `<div class="results"><div class="row"><h3>${esc(t.name)} — Choice ${esc(r.choice)}</h3><span>${fmtDelta(r.delta[0])} Perf · ${fmtDelta(r.delta[1])} Trust · ${fmtDelta(r.delta[2])} Culture</span></div><p>${esc(r.outcome)}</p>${r.extra?`<p class="quiet">🔗 ${esc(r.extra)}</p>`:\'\'}</div>`}).join(\'\');return `<h2>What happened?</h2>${items}<div class="callout"><b>Chapter 14 connection:</b> ${esc(state.scenarios[state.round].concept)}. The simulated results illustrate trade-offs; the values are game design assumptions.</div>`}\nfunction renderHost(){let s=state,sc=s.scenarios[s.round],sub=s.teams.filter(t=>t.submitted).length,action=\'\';\nif(s.phase===\'lobby\')action=`<button class="btn" onclick="host(\'start\')">Start Round 1</button>`;\nif(s.phase===\'ready\')action=`<button class="btn" onclick="host(\'start\')">Start Round ${s.round+1}</button>`;\nif(s.phase===\'open\')action=`<button class="btn alt" onclick="host(\'lock\')">Lock Answers Early</button>`;\nif(s.phase===\'locked\')action=`<button class="btn" onclick="host(\'reveal\')">Reveal Consequences + Apply Scores</button>`;\nif(s.phase===\'revealed\')action=`<button class="btn" onclick="host(\'next\')">${s.round===3?\'Finish Game & Announce Winner\':\'Advance to Round \'+(s.round+2)}</button>`;\nconst steps=`<span class="badge">${esc(s.phase.toUpperCase())}</span> <span class="quiet">Round ${s.round+1} / 4</span>`;\nconst welcome=`<div class="card"><div class="row"><div><div class="tiny">Before the competition</div><h2>Join your company</h2><p>One captain per team: scan the QR, create a company name, then submit one decision per round.</p>${s.join_url?`<p class="quiet" style="word-break:break-all">${esc(s.join_url)}</p>`:\'<p class="notice">Public link is being created. Start the tunnel cell in Colab.</p>\'}</div>${s.qr?`<img class="qr" src="${s.qr}" alt="Scan to join game">`:\'\'}</div></div>`;\nconst stage=s.phase===\'finished\'?`<div class="card center"><div class="confetti">🏆</div><h1>Game Over!</h1><h2>${s.teams.length?esc(s.teams[0].name)+\' wins!\':\'No teams\'}</h2><p>Final scores reflect each company\'s four decisions.</p></div>`:`<div class="card"><div class="row"><div>${steps}<h1>Round ${s.round+1} — ${esc(sc.title)}</h1><p><b>Chapter concept:</b> ${esc(sc.concept)}</p></div>${s.phase===\'open\'?`<div class="countdown" data-timer>${timerText()}</div>`:\'\'}</div><p>${esc(sc.story)}</p><h3>Strategies</h3>${sc.choices.map(c=>`<div class="teamline"><b>${esc(c.key)}</b> — ${esc(c.text)}</div>`).join(\'\')}<div class="row step"><b>${sub} of ${s.teams.length} captains submitted</b>${action}</div></div>`;\nreturn `${welcome}<div class="split"><div>${stage}${s.phase===\'revealed\'?`<div class="card">${results()}</div>`:\'\'}</div><div><div class="card"><h2>Live Leaderboard</h2>${scoreboard()}</div><div class="card"><h3>Host controls</h3><p class="quiet">Show QR before starting. Start → lock when time expires → reveal → next round.</p><div class="controls"><button class="btn danger" onclick="host(\'reset\')">Reset entire game</button></div></div></div></div>`}\nfunction rulesCard(){const secs=state.round_seconds||30;const rules=[["🏁","Everyone starts even","Performance, Employee trust, and Culture all start at 50."],["🔄","Four rounds","Each round is a new workplace dilemma from Chapter 14."],["⏱️",secs+" seconds to decide","Discuss with your team. The captain picks A, B, or C and taps Lock in."],["🔒","One answer per round","Once locked, it can’t be changed. No answer means no score change."],["🔗","Choices follow you","Some decisions affect what happens in later rounds."],["🏆","Highest total wins","Total = Performance + Trust + Culture. Ties go to higher Trust, then Culture."]];return `<div class="card"><div class="tiny">How to play</div><h1>Game rules</h1><div class="rules">${rules.map(r=>`<div class="rule"><div class="ri">${r[0]}</div><div><b>${r[1]}</b><p>${r[2]}</p></div></div>`).join("")}</div><div class="notice waiting">⏳ Waiting for the host to start…</div></div>`}\nfunction renderPlayer(){let s=state;if(!captainNow()){return `<div class="card" style="max-width:530px;margin:40px auto"><div class="tiny">Your company starts here</div><h1>Become a CEO.</h1><p>Choose one captain for your team. Enter a unique company name, then work together in person to choose A, B, or C. Only the captain submits.</p>${s.phase!==\'lobby\'?`<div class="notice">Team registration is closed. Ask the host for help.</div>`:`<label for="name"><b>Company / team name</b></label><input id="name" maxlength="28" placeholder="e.g. Profit Pirates"><button class="btn" onclick="register()">Create My Team</button>`}</div>`}\nlet t=s.teams.find(t=>t.id===captain.team_id),sc=s.scenarios[s.round],isOpen=s.phase===\'open\',hasSubmitted=t.submitted,parts=`<div class="card"><div class="row"><div><div class="tiny">Your company</div><h2>🏢 ${esc(t.name)}</h2></div><span class="badge">Round ${s.round+1} / 4</span></div><div class="grid">${metric(\'Performance\',t.perf)}${metric(\'Employee trust\',t.trust)}${metric(\'Culture\',t.culture)}</div></div>`;\nif(s.phase===\'finished\')return parts+`<div class="card center"><div class="confetti">🏆</div><h1>Game complete!</h1><p>${esc(s.teams[0]?.name||\'\')} earned the highest total score.</p></div><div class="card"><h2>Final Leaderboard</h2>${scoreboard()}</div>`;\nif(s.phase==="lobby")return parts+rulesCard();\nlet label=isOpen?`<span class="badge">TIME LEFT <span data-timer>${timerText()}</span></span>`:`<span class="badge">${esc(s.phase.toUpperCase())}</span>`;\nlet body=`<div class="card"><div class="row">${label}<span class="quiet">${esc(sc.concept)}</span></div><h1>${esc(sc.title)}</h1><p>${esc(sc.story)}</p><h3>Choose your strategy</h3>${choices(sc,isOpen&&!hasSubmitted)}${!isOpen?`<div class="notice">${s.phase===\'lobby\'?\'Waiting for the host to start…\':s.phase===\'ready\'?\'Next round is ready. Waiting for host…\':s.phase===\'locked\'?\'Time is up. Awaiting results…\':\'Scores revealed below.\'}</div>`:hasSubmitted?\'<div class="msg">✓ Decision locked. You cannot submit twice.</div>\':`<button class="btn" ${selected?\'\':\'disabled\'} onclick="submit()">Lock in ${selected||\'your\'} decision</button>`}</div>`;\nif(s.phase===\'revealed\'&&t.results){let r=t.results;body+=`<div class="card"><h2>Your consequences</h2><p>${esc(r.outcome)}</p>${r.extra?`<p>${esc(r.extra)}</p>`:\'\'}<div class="grid">${metric(\'Δ Performance\',fmtDelta(r.delta[0]))}${metric(\'Δ Trust\',fmtDelta(r.delta[1]))}${metric(\'Δ Culture\',fmtDelta(r.delta[2]))}</div><p class="callout">This round demonstrates <b>${esc(sc.concept)}</b>.</p></div><div class="card"><h2>Leaderboard</h2>${scoreboard()}</div>`}\nreturn parts+body}\nfunction render(){if(!state)return;const el=document.getElementById(\'app\');document.getElementById(\'status\').textContent=state.phase.toUpperCase();el.innerHTML=(flash?`<div class="msg ${flashError?\'error\':\'\'}">${esc(flash)} <button style="float:right;border:0;background:transparent" onclick="flash=\'\';render()">✕</button></div>`:\'\')+(MODE===\'host\'?renderHost():renderPlayer());tick()}\nsocket.on(\'state_update\',s=>{if(state&&state.round!==s.round)selected=null;if(state&&state.session!==s.session){captain=null;localStorage.removeItem(\'cc_captain\');selected=null}clockOffset=(s.server_now||Date.now()/1000)-Date.now()/1000;state=s;render()});\nsocket.on(\'connect\',()=>{document.getElementById(\'status\').textContent=\'CONNECTED\'});socket.on(\'disconnect\',()=>{document.getElementById(\'status\').textContent=\'RECONNECTING\'});setInterval(tick,250);\n</script></body></html>\n'

if __name__ == "__main__":
    socketio.run(app,host="0.0.0.0",port=int(os.environ.get("PORT","7860")),allow_unsafe_werkzeug=True,use_reloader=False)
