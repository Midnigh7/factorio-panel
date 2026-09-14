"""HTML for the panel (login page + single-page UI)."""

LOGIN_HTML = """<!doctype html><html><head><title>Factorio Panel</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#1a1a24;color:#e8e8f0;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#242433;padding:2rem;border-radius:12px;box-shadow:0 4px 24px #0008}input{padding:.6rem;border-radius:8px;border:1px solid #444;background:#1a1a24;color:#fff;width:220px}
button{margin-top:.8rem;padding:.6rem 1.2rem;border-radius:8px;border:0;background:#e8902a;color:#fff;font-weight:600;cursor:pointer;width:100%}
h2{margin-top:0}.f{color:#f66;font-size:.85rem}</style></head><body>
<form method=post><h2>⚙️ Factorio Panel</h2>{% with m = get_flashed_messages() %}{% if m %}<p class=f>{{m[0]}}</p>{% endif %}{% endwith %}
<input type=text name=username placeholder="Username" autofocus style="margin-bottom:.5rem"><br>
<input type=password name=password placeholder="Password"><button>Enter</button></form></body></html>"""

PANEL_HTML = r"""<!doctype html><html><head><title>Factorio Panel</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{background:#1a1a24;color:#e8e8f0;font-family:system-ui;margin:0;padding:1rem;max-width:1000px;margin:auto}
.card{background:#242433;border-radius:12px;padding:1rem 1.2rem;margin-bottom:1rem}
h1{font-size:1.25rem}h3{margin:.2rem 0 .6rem;color:#e8902a}
button{padding:.45rem .9rem;border-radius:8px;border:0;color:#fff;font-weight:600;cursor:pointer;margin:.15rem .3rem .15rem 0;font-size:.85rem}
.g{background:#2e7d32}.r{background:#b3402a}.o{background:#e8902a}.b{background:#39508f}.gr{background:#3a3a4a}
#logs,#conout{background:#12121a;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.72rem;max-height:280px;overflow-y:auto;white-space:pre-wrap}
input[type=text],input[type=password]{padding:.5rem;border-radius:8px;border:1px solid #444;background:#1a1a24;color:#fff}
textarea{width:100%;min-height:340px;background:#12121a;color:#cde;border:1px solid #444;border-radius:8px;font-family:monospace;font-size:.75rem;padding:.6rem;box-sizing:border-box}
table{width:100%;border-collapse:collapse;font-size:.85rem}td,th{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #333}
.pill{display:inline-block;padding:.15rem .6rem;border-radius:999px;font-size:.8rem;font-weight:600}
.run{background:#2e7d3233;color:#7c6}.stop{background:#b3402a33;color:#e88}
.tabs{display:flex;gap:.3rem;margin-bottom:1rem;flex-wrap:wrap}
.tab{padding:.5rem 1rem;border-radius:8px;background:#242433;cursor:pointer;font-weight:600;font-size:.85rem}
.tab.active{background:#e8902a;color:#1a1a24}
.hidden{display:none}a{color:#8ab}.note{font-size:.75rem;color:#889}
.badge{font-size:.7rem;background:#39508f55;color:#9bd;border-radius:6px;padding:.1rem .4rem;margin-left:.3rem}
</style></head><body>
<h1>⚙️ Factorio Panel <span id=state class=pill></span> <span style="float:right;font-size:.8rem"><span id=whoami class=note></span> · <a href=/logout>logout</a></span></h1>
<div class=tabs>
<div class="tab active" data-t=dash>Dashboard</div><div class=tab data-t=chat>Chat</div><div class=tab data-t=mods>Mods</div>
<div class=tab data-t=settings>Settings</div><div class=tab data-t=world>World</div>
<div class=tab data-t=players>Players</div>
<div class=tab data-t=saves>Saves</div><div class=tab data-t=console>Console</div>
<div class=tab data-t=admin id=admintab style="display:none">Admin</div></div>

<div id=t-dash>
<div class=card><h3>Server</h3><div id=info>loading…</div>
<div style="margin-top:.6rem"><button class=g onclick="ctl('start')">Start</button>
<button class=o onclick="ctl('restart')">Restart</button><button class=r onclick="ctl('stop')">Stop</button></div>
<div id=cd style="color:#e8902a;margin-top:.5rem"></div></div>
<div class=card><h3>Online <span id=pcount></span></h3><div id=players>—</div>
<div style="margin-top:.6rem"><input type=text id=msg placeholder="Broadcast a message" style="width:60%"><button class=b onclick=say()>Say</button></div></div>
<div class=card><h3>Version</h3><div id=ver>…</div>
<div style="margin-top:.5rem"><input type=text id=newver placeholder="e.g. 2.1.18" style="width:110px"><button class=o onclick=upd()>Update</button>
<span class=note>backs up newest save, pins compose tag, pulls + recreates</span></div></div>
<div class=card><h3>Performance</h3><div id=perf>…</div></div>
<div class=card><h3>Logs</h3><div id=logs>…</div></div>
</div>

<div id=t-chat class=hidden>
<div class=card><h3>Game chat & events</h3>
<div id=chatlog style="background:#12121a;border-radius:8px;padding:.6rem;font-family:monospace;font-size:.78rem;max-height:420px;overflow-y:auto"></div>
<div style="margin-top:.6rem"><input type=text id=chatmsg placeholder="Send to game chat" style="width:70%" onkeydown="if(event.key==='Enter')chatSend()"><button class=b onclick=chatSend()>Send</button></div></div>
</div>

<div id=t-mods class=hidden>
<div class=card><h3>Installed mods</h3><div id=modlist>…</div>
<div style="margin-top:.6rem"><button class=b onclick=checkUpdates()>Check for updates</button><span id=updres class=note></span></div>
<p class=note>Enable/disable and installs apply after a server restart.</p></div>
<div class=card><h3>Install from mod portal</h3>
<input type=text id=modq placeholder="Search mods…" style="width:60%"><button class=b onclick=modSearch()>Search</button>
<div id=modresults style="margin-top:.6rem"></div></div>
<div class=card><h3>factorio.com credentials <span id=credstate class=badge></span></h3>
<p class=note>Needed for mod downloads. Username + token (from factorio.com → profile), or username + password (token fetched once, password not stored).</p>
<input type=text id=cuser placeholder="username" style="width:160px">
<input type=password id=ctoken placeholder="token (preferred)" style="width:220px">
<input type=password id=cpass placeholder="or password" style="width:160px">
<button class=o onclick=saveCreds()>Save</button></div>
</div>

<div id=t-settings class=hidden>
<div class=card><div style="display:flex;justify-content:space-between;align-items:center">
<h3 style="margin:0">Server settings</h3>
<label class=note style="cursor:pointer"><input type=checkbox id=advmode onchange=toggleAdv()> Advanced (raw JSON)</label></div>
<div id=settingsform style="margin-top:.8rem">loading…</div>
<div id=settingsadv class=hidden style="margin-top:.8rem">
<textarea id=settingsbox spellcheck=false></textarea>
<div style="margin-top:.5rem"><button class=o onclick=saveSettings()>Save raw JSON</button>
<button class=gr onclick=loadSettings()>Reload</button></div></div>
<div style="margin-top:.7rem"><button class=o id=formsave onclick=saveForm()>Save changes</button>
<button class=gr onclick=loadForm()>Reload</button>
<span class=note>a .bak is kept; restart the server to apply</span>
<div id=formerrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div></div>
</div>

<div id=t-world class=hidden>
<div class=card><h3>Current world behavior <span class=note>(map-settings — applies to the running map after restart)</span></h3>
<div id=worldform>loading…</div>
<div style="margin-top:.7rem"><button class=o onclick=saveWorld()>Save world settings</button>
<button class=gr onclick=loadWorld()>Reload</button>
<div id=worlderrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div></div>
<div class=card><h3>New map generation <span class=note>(map-gen — used only when creating a NEW map)</span></h3>
<div id=mapgenform>loading…</div>
<div style="margin-top:.7rem"><button class=o onclick=saveMapgen()>Save map-gen settings</button>
<button class=gr onclick=loadMapgen()>Reload</button>
<div id=mapgenerrors style="color:#f66;font-size:.8rem;margin-top:.4rem"></div></div>
<div style="margin-top:1rem;border-top:1px solid #333;padding-top:.8rem">
<input type=text id=newmapname placeholder="new map name" style="width:200px">
<button class=r onclick=newMap()>⚠ Generate new map & switch</button>
<span class=note>stops server, creates the map with the settings above, restarts into it (old saves kept)</span></div></div>
</div>

<div id=t-players class=hidden>
<div class=card><h3>Live players <span id=lpcount></span></h3>
<div style="display:flex;gap:1rem;flex-wrap:wrap;align-items:flex-start">
<canvas id=lpmap width=340 height=340 style="background:#12121a;border-radius:8px;flex-shrink:0"></canvas>
<div style="flex:1;min-width:260px"><div id=lptable>—</div></div></div>
<p class=note>Positions via RCON, refreshed every 5s while this tab is open. Map is centered on spawn (0,0); grid = 100 tiles.</p></div>
<div class=card><h3>Player actions</h3>
<input type=text id=pname placeholder="player name" style="width:160px">
<input type=text id=preason placeholder="reason (kick/ban)" style="width:200px"><br>
<button class=o onclick="pact('kick')">Kick</button><button class=r onclick="pact('ban')">Ban</button>
<button class=g onclick="pact('unban')">Unban</button><button class=b onclick="pact('promote')">Promote admin</button>
<button class=gr onclick="pact('demote')">Demote</button><button class=gr onclick="pact('mute')">Mute</button>
<button class=gr onclick="pact('unmute')">Unmute</button>
<div id=pactout class=note style="margin-top:.4rem"></div></div>
<div class=card><h3>Admin list</h3><div id=adminlist>…</div>
<input type=text id=newadmin placeholder="add player" style="width:160px"><button class=b onclick="listAdd('adminlist','newadmin')">Add</button></div>
<div class=card><h3>Ban list</h3><div id=banlist>…</div></div>
<div class=card><h3>Whitelist</h3><div id=whitelist>…</div>
<input type=text id=newwhite placeholder="add player" style="width:160px"><button class=b onclick="listAdd('whitelist','newwhite')">Add</button>
<p class=note>Whitelist only enforced if enabled in settings / with --use-server-whitelist.</p></div>
</div>

<div id=t-saves class=hidden>
<div class=card><h3>Boot save</h3><div id=bootsave>…</div>
<p class=note>Which save the server loads on start. "Latest" = newest file (autosaves win). Pinning a save recreates the container.</p></div>
<div class=card><h3>Saves</h3><button class=b onclick=backup()>Backup newest now</button>
<label class=gr style="padding:.45rem .9rem;border-radius:8px;font-weight:600;cursor:pointer;font-size:.85rem">Upload save<input type=file id=upfile accept=".zip" style="display:none" onchange=uploadSave()></label>
<div id=saves style="margin-top:.5rem">…</div></div>
</div>

<div id=t-console class=hidden>
<div class=card><h3>RCON console</h3>
<input type=text id=concmd placeholder="/players online  ·  /time  ·  /silent-command …" style="width:75%" onkeydown="if(event.key==='Enter')runCmd()">
<button class=o onclick=runCmd()>Run</button>
<div id=conout style="margin-top:.6rem">—</div>
<p class=note>⚠ /silent-command executes Lua with full game access and disables achievements. All commands audited.</p></div>
</div>

<div id=t-admin class=hidden>
<div class=card><h3>Panel users</h3><div id=userlist>…</div>
<div style="margin-top:.6rem">
<input type=text id=nu_name placeholder="username" style="width:130px">
<input type=password id=nu_pass placeholder="password (8+ chars)" style="width:170px">
<select id=nu_role style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">
<option>viewer</option><option>moderator</option><option selected>admin</option></select>
<button class=g onclick=userAdd()>Add user</button></div>
<p class=note>viewer: read-only · moderator: server control, players, saves, console · admin: everything</p></div>
<div class=card><h3>Change my password</h3>
<input type=password id=mypw placeholder="new password (8+ chars)" style="width:200px"><button class=o onclick=myPw()>Change</button></div>
<div class=card><h3>Discord notifications <span id=whstate class=badge></span></h3>
<p class=note>Webhook posts joins/leaves/chat to a Discord channel. Create one: channel settings → Integrations → Webhooks.</p>
<input type=password id=whurl placeholder="https://discord.com/api/webhooks/…" style="width:60%">
<label class=note><input type=checkbox id=ev_join checked> joins</label>
<label class=note><input type=checkbox id=ev_leave checked> leaves</label>
<label class=note><input type=checkbox id=ev_chat checked> chat</label>
<button class=o onclick=saveWebhook()>Save</button></div>
<div class=card><h3>Off-box backup</h3>
<button class=b onclick=offboxBackup()>Push newest save off-box now</button>
<span class=note>destination = backup_host in panel_config.json (SSH key auth required)</span>
<div id=offboxout class=note style="margin-top:.4rem"></div></div>
</div>

<script>
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
let MYROLE='viewer';
async function whoami(){const r=await j('/api/me');MYROLE=r.role;
 document.getElementById('whoami').textContent=r.username+' ('+r.role+')';
 if(r.role==='admin')document.getElementById('admintab').style.display='';}
whoami();
// chat
let chatSeen=0;
async function chatPoll(){const r=await j('/api/chat?since='+chatSeen);if(!r.ok)return;
 if(r.total<chatSeen){chatSeen=0;return chatPoll();}
 if(r.entries.length){const el=document.getElementById('chatlog');
  for(const e of r.entries){const c={'chat':'#cde','join':'#7c6','leave':'#e88'}[e.kind]||'#889';
   el.innerHTML+=`<div style="color:${c}">[${e.ts}] ${e.text.replace(/</g,'&lt;')}</div>`;}
  chatSeen=r.total;el.scrollTop=el.scrollHeight;}}
async function chatSend(){const m=document.getElementById('chatmsg').value.trim();if(!m)return;
 await post('/api/say',{message:m});document.getElementById('chatmsg').value='';}
setInterval(chatPoll,4000);chatPoll();
// perf
async function perf(){const r=await j('/api/metrics');if(!r.ok)return;
 const h=r.history;const last=h[h.length-1]||{};
 document.getElementById('perf').textContent=
 `UPS ${r.ups??'—'}/60 · CPU ${last.cpu??'—'}% · RAM ${last.mem??'—'} · players ${last.players??0}`;}
setInterval(perf,30000);perf();
// admin
async function usersUI(){const r=await j('/api/users');if(!r.ok)return;
 document.getElementById('userlist').innerHTML='<table><tr><th>user</th><th>role</th><th></th></tr>'+
 r.users.map(u=>`<tr><td>${u.username}</td><td>
 <select onchange="userRole('${u.username}',this.value)" style="padding:.3rem;border-radius:6px;background:#1a1a24;color:#fff;border:1px solid #444">
 ${['viewer','moderator','admin'].map(x=>`<option ${x===u.role?'selected':''}>${x}</option>`).join('')}</select></td>
 <td><a href=# onclick="userDel('${u.username}');return false" style="color:#e88">✕</a></td></tr>`).join('')+'</table>';}
async function userAdd(){const r=await post('/api/users',{action:'add',username:nu_name.value.trim(),password:nu_pass.value,role:nu_role.value});
 if(!r.ok)alert(r.error);nu_pass.value='';usersUI();}
async function userDel(u){if(!confirm('Delete user '+u+'?'))return;const r=await post('/api/users',{action:'delete',username:u});if(!r.ok)alert(r.error);usersUI();}
async function userRole(u,role){const r=await post('/api/users',{action:'setrole',username:u,role});if(!r.ok){alert(r.error);usersUI();}}
async function myPw(){const r=await post('/api/users',{action:'setpassword',password:mypw.value});alert(r.ok?'Changed.':r.error);mypw.value='';}
async function webhookUI(){const r=await j('/api/panelconfig');if(!r.ok)return;
 document.getElementById('whstate').textContent=r.discord_webhook_set?'set ✓':'not set';
 for(const e of['join','leave','chat'])document.getElementById('ev_'+e).checked=r.discord_events.includes(e);}
async function saveWebhook(){const evs=['join','leave','chat'].filter(e=>document.getElementById('ev_'+e).checked);
 const b={discord_events:evs};const u=whurl.value.trim();if(u)b.discord_webhook=u;
 const r=await post('/api/panelconfig',b);alert(r.ok?'Saved.':r.error);whurl.value='';webhookUI();}
async function offboxBackup(){document.getElementById('offboxout').textContent='pushing…';
 const r=await post('/api/backup-offbox',{});
 document.getElementById('offboxout').textContent=r.ok?'✓ '+r.dest:'Failed: '+r.error;}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
 document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
 ['dash','chat','mods','settings','world','players','saves','console','admin'].forEach(n=>document.getElementById('t-'+n).classList.toggle('hidden',n!==t.dataset.t));
 if(t.dataset.t==='mods')mods();if(t.dataset.t==='settings')loadForm();
 if(t.dataset.t==='world'){loadWorld();loadMapgen();}
 if(t.dataset.t==='admin'){usersUI();webhookUI();}
 if(t.dataset.t==='players'){lists();lpStart();}if(t.dataset.t==='saves'){saves();bootsaveUI();}});
async function refresh(){const s=await j('/api/status');
 const st=document.getElementById('state');st.textContent=s.container.state;
 st.className='pill '+(s.container.state==='running'?'run':'stop');
 document.getElementById('info').textContent=`image ${s.container.image||'—'} · up ${s.container.uptime||'—'} · v${s.version||'—'}`;
 document.getElementById('players').textContent=s.players.length?s.players.join(', '):'nobody online';
 document.getElementById('pcount').textContent=`(${s.players.length})`;
 document.getElementById('cd').textContent=s.countdown.countdown?`⏳ ${s.countdown.action} in ${s.countdown.seconds_left}s (players warned)`:'';}
async function logs(){const l=await j('/api/logs');const e=document.getElementById('logs');e.textContent=l.logs.join('\n');e.scrollTop=e.scrollHeight;}
async function ver(){const v=await j('/api/versions');document.getElementById('ver').textContent=`running ${v.running} · stable ${v.stable||'?'} · experimental ${v.experimental||'?'}`;}
async function ctl(a){if(a!=='start'&&!confirm(a+' the server?'))return;await post('/api/control',{action:a});setTimeout(refresh,1500);}
async function say(){const m=document.getElementById('msg').value;if(!m)return;await post('/api/say',{message:m});document.getElementById('msg').value='';}
async function upd(){const v=document.getElementById('newver').value.trim();if(!v||!confirm('Update to '+v+'?'))return;
 const r=await post('/api/update',{version:v});alert(r.ok?'Updated, server restarting.':'Failed: '+(r.error||r.output));ver();refresh();}
// mods
async function mods(){const d=await j('/api/mods');
 document.getElementById('credstate').textContent=d.creds_set?'set ✓':'not set';
 document.getElementById('modlist').innerHTML='<table><tr><th>mod</th><th>version</th><th>enabled</th><th></th></tr>'+
 d.mods.map(m=>`<tr><td>${m.name}${m.builtin?' <span class=badge>builtin</span>':''}${m.unlisted?' <span class=badge>unlisted</span>':''}</td>
 <td>${m.version||'—'}</td><td>${m.enabled?'✅':'—'}</td><td>
 <button class=gr onclick="modToggle('${m.name}')">${m.enabled?'disable':'enable'}</button>
 ${!m.builtin?`<button class=r onclick="modRemove('${m.name}')">remove</button><button class=b onclick="modUpdate('${m.name}')">update</button>`:''}
 </td></tr>`).join('')+'</table>';}
async function modToggle(n){await post('/api/mods/toggle',{name:n});mods();}
async function modRemove(n){if(!confirm('Remove mod '+n+'?'))return;const r=await post('/api/mods/remove',{name:n});if(!r.ok)alert(r.error);mods();}
async function modUpdate(n){const r=await post('/api/mods/update',{name:n});alert(r.ok?n+' → '+r.version:'Failed: '+r.error);mods();}
async function checkUpdates(){document.getElementById('updres').textContent='checking…';
 const r=await j('/api/mods/check-updates');
 document.getElementById('updres').textContent=r.updates.length?r.updates.map(u=>`${u.name} ${u.installed}→${u.available}`).join(' · '):'all current';}
async function modSearch(){const q=document.getElementById('modq').value.trim();if(!q)return;
 const r=await j('/api/mods/search?q='+encodeURIComponent(q));
 if(!r.ok){alert(r.error);return;}
 document.getElementById('modresults').innerHTML='<table><tr><th>mod</th><th>by</th><th>DLs</th><th>latest</th><th></th></tr>'+
 r.results.map(m=>`<tr><td title="${m.summary}">${m.title}<br><span class=note>${m.name}</span></td><td>${m.owner}</td>
 <td>${m.downloads}</td><td>${m.latest||'?'}</td><td><button class=g onclick="modInstall('${m.name}')">install</button></td></tr>`).join('')+'</table>';}
async function modInstall(n){const r=await post('/api/mods/install',{name:n});
 alert(r.ok?'Installed: '+r.installed.map(x=>x.name+' '+x.version).join(', ')+'. Restart to apply.':'Failed: '+r.error);mods();}
async function saveCreds(){const r=await post('/api/credentials',{username:cuser.value.trim(),token:ctoken.value.trim(),password:cpass.value});
 alert(r.ok?'Credentials saved.':'Failed: '+r.error);cpass.value='';mods();}
// world tab — shared schema-form renderer
function renderSchemaForm(el,sections,prefix){document.getElementById(el).innerHTML=sections.map(sec=>
 `<h3 style="margin-top:1rem;font-size:.95rem">${sec.section}</h3><table>`+sec.fields.map(f=>{
  const id=prefix+f.key.replace(/[.\-]/g,'__');let ctl='';
  const st='padding:.35rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444';
  if(f.type==='bool')ctl=`<input type=checkbox id=${id} ${f.value?'checked':''}>`;
  else if(f.type==='int'||f.type==='float')ctl=`<input type=number step=any id=${id} value="${f.value??''}" style="width:130px;${st}">`;
  else if(f.type==='int_or_null')ctl=`<input type=text id=${id} value="${f.value??''}" placeholder=random style="width:130px;${st}">`;
  else ctl=`<input type=text id=${id} value="${String(f.value??'')}" style="width:200px;${st}">`;
  return `<tr><td style="width:52%">${f.label}${f.help?`<br><span class=note>${f.help}</span>`:''}</td><td>${ctl}</td></tr>`;
 }).join('')+'</table>').join('');}
function collectSchemaForm(sections,prefix){const values={};
 for(const sec of sections)for(const f of sec.fields){
  const el=document.getElementById(prefix+f.key.replace(/[.\-]/g,'__'));if(!el)continue;
  values[f.key]=f.type==='bool'?el.checked:el.value;}
 return values;}
let worldSchema=null,mapgenSchema=null;
async function loadWorld(){const r=await j('/api/world');worldSchema=r.sections;renderSchemaForm('worldform',r.sections,'wf_');}
async function saveWorld(){const r=await post('/api/world',{values:collectSchemaForm(worldSchema,'wf_')});
 document.getElementById('worlderrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. '+r.note);}
async function loadMapgen(){const r=await j('/api/mapgen');mapgenSchema=r.sections;renderSchemaForm('mapgenform',r.sections,'mg_');}
async function saveMapgen(){const r=await post('/api/mapgen',{values:collectSchemaForm(mapgenSchema,'mg_')});
 document.getElementById('mapgenerrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. '+r.note);}
async function newMap(){const n=document.getElementById('newmapname').value.trim();if(!n)return;
 if(!confirm('Generate NEW map "'+n+'" and switch the server to it? Current world stays on disk but the server will boot the new one.'))return;
 const r=await post('/api/newmap',{name:n});alert(r.ok?'Generating '+r.file+' — server restarting into it (~30s).':'Failed: '+r.error);}
async function bootsaveUI(){const r=await j('/api/bootsave');const d=await j('/api/saves');
 document.getElementById('bootsave').innerHTML=
 `<select id=bootsel style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">
 <option value="">Latest save (default)</option>`+
 d.saves.map(s=>`<option value="${s.name}" ${r.save&&s.name.startsWith(r.save)?'selected':''}>${s.name}</option>`).join('')+
 `</select> <button class=o onclick=setBootsave()>Apply</button>
 <span class=note>current: ${r.mode==='latest'?'latest':r.save}</span>`;}
async function setBootsave(){const v=document.getElementById('bootsel').value;
 if(!confirm(v?'Pin boot save to '+v+'? (container recreates now)':'Boot latest save? (container recreates now)'))return;
 const r=await post('/api/bootsave',{save:v});alert(r.ok?'Applied.':'Failed: '+r.error);bootsaveUI();}
// settings — forms mode
let formSchema=null;
async function loadForm(){const r=await j('/api/settings-form');formSchema=r.sections;
 document.getElementById('settingsform').innerHTML=r.sections.map(sec=>
 `<h3 style="margin-top:1rem">${sec.section}</h3><table>`+sec.fields.map(f=>{
  const id='sf_'+f.key.replace(/\./g,'__');let ctl='';
  if(f.type==='bool')ctl=`<input type=checkbox id=${id} ${f.value?'checked':''}>`;
  else if(f.type==='enum')ctl=`<select id=${id} style="padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`+
    f.choices.map(c=>`<option ${c===f.value?'selected':''}>${c}</option>`).join('')+'</select>';
  else if(f.type==='int')ctl=`<input type=number id=${id} value="${f.value??''}" min="${f.min??''}" max="${f.max??''}" style="width:110px;padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  else if(f.type==='secret')ctl=`<input type=password id=${id} value="${f.value==='__SET__'?'__SET__':''}" placeholder="${f.value==='__SET__'?'(set — type to change)':'(empty)'}" style="width:220px;padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  else ctl=`<input type=text id=${id} value="${String(f.value??'').replace(/"/g,'&quot;')}" style="width:min(340px,90%);padding:.4rem;border-radius:8px;background:#1a1a24;color:#fff;border:1px solid #444">`;
  return `<tr><td style="width:45%">${f.label}${f.help?`<br><span class=note>${f.help}</span>`:''}</td><td>${ctl}</td></tr>`;
 }).join('')+'</table>').join('');}
async function saveForm(){if(!formSchema)return;const values={};
 for(const sec of formSchema)for(const f of sec.fields){
  const el=document.getElementById('sf_'+f.key.replace(/\./g,'__'));if(!el)continue;
  values[f.key]=f.type==='bool'?el.checked:el.value;}
 const r=await post('/api/settings-form',{values});
 document.getElementById('formerrors').textContent=r.ok?'':'Validation: '+(r.errors||[r.error]).join(' · ');
 if(r.ok)alert('Saved. Restart the server to apply.');}
function toggleAdv(){const adv=document.getElementById('advmode').checked;
 document.getElementById('settingsadv').classList.toggle('hidden',!adv);
 document.getElementById('settingsform').classList.toggle('hidden',adv);
 document.getElementById('formsave').style.display=adv?'none':'';
 if(adv)loadSettings();else loadForm();}
async function loadSettings(){const r=await j('/api/settings');document.getElementById('settingsbox').value=JSON.stringify(r.settings,null,2);}
async function saveSettings(){let v;try{v=JSON.parse(document.getElementById('settingsbox').value)}catch(e){alert('Invalid JSON: '+e);return;}
 const r=await post('/api/settings',{settings:v});alert(r.ok?'Saved. Restart to apply.':'Failed: '+r.error);}
// players
// live players
let lpTimer=null;
const LP_COLORS=['#e8902a','#6c9','#8ab','#e88','#c9d','#dd7'];
async function livePlayers(){const r=await j('/api/liveplayers');if(!r.ok)return;
 const ps=r.players;document.getElementById('lpcount').textContent='('+ps.length+')';
 document.getElementById('lptable').innerHTML=ps.length?
 '<table><tr><th></th><th>player</th><th>pos</th><th>surface</th><th>online</th><th>afk</th></tr>'+
 ps.map((p,i)=>`<tr><td><span style="color:${LP_COLORS[i%6]}">●</span></td><td>${p.name}${p.admin?' <span class=badge>admin</span>':''}</td>
 <td>${p.x}, ${p.y}</td><td>${p.surface}</td><td>${fmtMin(p.online)}</td><td>${p.afk>1?fmtMin(p.afk):'—'}</td></tr>`).join('')+'</table>'
 :'<span class=note>nobody online</span>';
 drawMap(ps);}
function fmtMin(m){return m>=60?Math.floor(m/60)+'h '+(m%60)+'m':m+'m';}
function drawMap(ps){const cv=document.getElementById('lpmap'),ctx=cv.getContext('2d');
 ctx.clearRect(0,0,340,340);
 // scale: fit all players + margin, min ±200 tiles
 let ext=200;for(const p of ps)ext=Math.max(ext,Math.abs(p.x)*1.2,Math.abs(p.y)*1.2);
 const sc=160/ext;
 ctx.strokeStyle='#1e1e2c';ctx.lineWidth=1;
 const step=100*sc;
 for(let g=170%step;g<340;g+=step){ctx.beginPath();ctx.moveTo(g,0);ctx.lineTo(g,340);ctx.stroke();
  ctx.beginPath();ctx.moveTo(0,g);ctx.lineTo(340,g);ctx.stroke();}
 ctx.strokeStyle='#333';ctx.beginPath();ctx.moveTo(170,0);ctx.lineTo(170,340);ctx.stroke();
 ctx.beginPath();ctx.moveTo(0,170);ctx.lineTo(340,170);ctx.stroke();
 ctx.fillStyle='#556';ctx.font='9px monospace';ctx.fillText('(0,0)',173,167);
 ps.forEach((p,i)=>{const x=170+p.x*sc,y=170+p.y*sc;
  ctx.fillStyle=LP_COLORS[i%6];ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();
  ctx.fillStyle='#cde';ctx.font='10px system-ui';ctx.fillText(p.name,x+7,y+3);});}
function lpStart(){livePlayers();if(!lpTimer)lpTimer=setInterval(()=>{
 if(document.getElementById('t-players').classList.contains('hidden')){clearInterval(lpTimer);lpTimer=null;return;}
 livePlayers();},5000);}
async function pact(a){const p=document.getElementById('pname').value.trim();if(!p)return;
 const r=await post('/api/player-action',{action:a,player:p,reason:document.getElementById('preason').value.trim()});
 document.getElementById('pactout').textContent=r.ok?(r.output||'ok'):'Failed: '+r.error;lists();}
async function lists(){for(const k of['adminlist','banlist','whitelist']){const r=await j('/api/list/'+k);
 const el=document.getElementById(k);
 if(!r.entries.length){el.innerHTML='<span class=note>empty</span>';continue;}
 el.innerHTML=r.entries.map((e,i)=>{const n=typeof e==='string'?e:(e.username||JSON.stringify(e));
 return `<span style="display:inline-block;background:#1a1a24;border-radius:8px;padding:.2rem .6rem;margin:.15rem">${n}
 <a href=# onclick="listDel('${k}',${i});return false" style="color:#e88">✕</a></span>`}).join('');}}
async function listDel(k,i){const r=await j('/api/list/'+k);r.entries.splice(i,1);
 await post('/api/list/'+k,{entries:r.entries});lists();}
async function listAdd(k,inp){const v=document.getElementById(inp).value.trim();if(!v)return;
 const r=await j('/api/list/'+k);r.entries.push(v);await post('/api/list/'+k,{entries:r.entries});
 document.getElementById(inp).value='';lists();}
// saves
async function saves(){const d=await j('/api/saves');document.getElementById('saves').innerHTML=
 '<table><tr><th>save</th><th>size</th><th>modified</th><th></th></tr>'+d.saves.map(s=>
 `<tr><td>${s.name}</td><td>${s.size_mb} MB</td><td>${s.mtime}</td>
 <td><a href="/saves/download/${s.name}">⬇</a> <a href=# onclick="delSave('${s.name}');return false" style="color:#e88">✕</a></td></tr>`).join('')+'</table>';}
async function delSave(n){if(!confirm('Delete '+n+'?'))return;const r=await post('/api/saves/delete',{name:n});if(!r.ok)alert(r.error);saves();}
async function backup(){const r=await post('/api/saves/backup',{});alert(r.ok?'Backed up: '+r.name:'Failed');saves();}
async function uploadSave(){const f=document.getElementById('upfile').files[0];if(!f)return;
 const fd=new FormData();fd.append('file',f);
 const r=await fetch('/api/saves/upload',{method:'POST',body:fd}).then(x=>x.json());
 alert(r.ok?'Uploaded '+r.name:'Failed: '+r.error);saves();}
// console
async function runCmd(){const c=document.getElementById('concmd').value.trim();if(!c)return;
 const r=await post('/api/rcon',{command:c});const o=document.getElementById('conout');
 o.textContent+=`\n> ${c}\n${r.ok?(r.output||'(no output)'):'ERROR: '+r.error}`;o.scrollTop=o.scrollHeight;
 document.getElementById('concmd').value='';}
refresh();logs();ver();setInterval(refresh,5000);setInterval(()=>{if(!document.getElementById('t-dash').classList.contains('hidden'))logs()},10000);
</script></body></html>"""
