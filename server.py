"""Tiny web remote for the Hatch. Run: ./run.sh  then open http://localhost:8765
Reachable from your phone on the same Wi-Fi at http://<mac-ip>:8765
"""
import asyncio
import hmac
import os
import secrets
import time
from hashlib import sha256

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

from hatch import SOUNDS, Hatch

app = FastAPI()
hatch = Hatch(os.environ.get("HATCH_ADDR"))
lock = asyncio.Lock()  # BLE is not happy with concurrent commands

# ---- auth: single shared password, signed cookie session ----
PASSWORD = os.environ.get("HATCH_PASSWORD", "")
SECRET = os.environ.get("HATCH_SECRET") or secrets.token_hex(32)  # new secret per boot = logins reset on restart
SESSION_TTL = 30 * 24 * 3600
COOKIE = "hatch_session"


def _sign(exp: int) -> str:
    return hmac.new(SECRET.encode(), str(exp).encode(), sha256).hexdigest()


def _token() -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{_sign(exp)}"


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(int(exp)))


# ---- brute-force lockout ----
# A bare `await asyncio.sleep()` is NOT a throttle: attempts are concurrent, so
# 50 parallel guesses all sleep at once and finish together. Three things here:
# login attempts are serialized behind a lock, each IP gets a lockout after a
# few failures, and a global ceiling catches an attacker rotating IPs (the
# forwarded client IP is only as trustworthy as whatever sets it).
login_lock = asyncio.Lock()
FAIL_DELAY = 1.0
IP_MAX_FAILS = 5
IP_LOCK_SECS = 300
GLOBAL_MAX_FAILS = 20
GLOBAL_WINDOW = 300
GLOBAL_LOCK_SECS = 60
_ip_fails: dict[str, tuple[int, float]] = {}  # ip -> (fails, locked_until)
_global_fails: list[float] = []
_global_locked_until = 0.0


def _client_ip(request: Request) -> str:
    # cloudflared sets CF-Connecting-IP; it survives the tunnel and the client
    # cannot forge it when traffic actually comes through Cloudflare.
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "?")
    )


def _locked(ip: str) -> float:
    """Seconds remaining on a lockout, 0 if the attempt may proceed."""
    now = time.time()
    fails, until = _ip_fails.get(ip, (0, 0.0))
    return max(until - now, _global_locked_until - now, 0.0)


def _record_failure(ip: str):
    global _global_locked_until
    now = time.time()
    fails, _ = _ip_fails.get(ip, (0, 0.0))
    fails += 1
    _ip_fails[ip] = (fails, now + IP_LOCK_SECS if fails >= IP_MAX_FAILS else 0.0)

    _global_fails[:] = [t for t in _global_fails if now - t < GLOBAL_WINDOW]
    _global_fails.append(now)
    if len(_global_fails) >= GLOBAL_MAX_FAILS:
        _global_locked_until = now + GLOBAL_LOCK_SECS
        _global_fails.clear()

    # keep the dict from growing without bound
    if len(_ip_fails) > 1000:
        for k, (_, u) in list(_ip_fails.items()):
            if u < now:
                del _ip_fails[k]


LOGIN = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Hatch</title>
<style>body{font:16px system-ui;background:#111;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{display:flex;flex-direction:column;gap:12px;width:260px}input,button{padding:14px;font-size:18px;border-radius:10px;border:0}
input{background:#222;color:#eee}button{background:#2a7;color:#fff}.err{color:#f66;text-align:center}</style>
<form method=post action=/login><h2 style="margin:0;text-align:center">Hatch remote</h2>
<input type=password name=password placeholder=Password autofocus>__ERR__<button>Sign in</button></form>"""


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not PASSWORD:
        return JSONResponse({"detail": "HATCH_PASSWORD not set"}, 500)
    if request.url.path == "/login" or _valid(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "unauthorized"}, 401)
    return RedirectResponse("/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN.replace("__ERR__", "")


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = _client_ip(request)

    async with login_lock:  # serialize: concurrent guesses must not slip past
        if wait := _locked(ip):
            return HTMLResponse(
                LOGIN.replace("__ERR__", f"<div class=err>locked, try again in {int(wait) // 60 + 1} min</div>"),
                429,
            )
        # compare_digest needs ASCII-only str operands; a non-ASCII guess is wrong by definition
        ok = password.isascii() and hmac.compare_digest(password, PASSWORD)
        if not ok:
            _record_failure(ip)
            await asyncio.sleep(FAIL_DELAY)
            return HTMLResponse(LOGIN.replace("__ERR__", "<div class=err>wrong password</div>"), 401)
        _ip_fails.pop(ip, None)

    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp = RedirectResponse("/", 303)
    resp.set_cookie(COOKIE, _token(), max_age=SESSION_TTL, httponly=True, samesite="lax", secure=https)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


async def run(coro_fn, *args):
    async with lock:
        try:
            await coro_fn(*args)
        except Exception as e:  # reconnect once on any BLE hiccup
            await hatch.disconnect()
            try:
                await hatch.connect()
                await coro_fn(*args)
            except Exception as e2:
                raise HTTPException(503, f"hatch error: {e2}") from e
        return hatch.state()


@app.get("/api/state")
async def state():
    return await run(hatch.refresh)


@app.post("/api/power/{on}")
async def power(on: str):
    return await run(hatch.power_on if on in ("on", "1", "true") else hatch.power_off)


class Color(BaseModel):
    r: int
    g: int
    b: int
    brightness: int | None = None


@app.post("/api/color")
async def color(c: Color):
    br = c.brightness
    if br is None and hatch.brightness == 0 and (c.r or c.g or c.b):
        br = 128  # picking a color with the light dark would be invisible
    return await run(hatch.set_color, c.r, c.g, c.b, br)


@app.post("/api/brightness/{v}")
async def brightness(v: int):
    return await run(hatch.set_brightness, v)


@app.post("/api/volume/{v}")
async def volume(v: int):
    return await run(hatch.set_volume, v)


@app.post("/api/sound/{name}")
async def sound(name: str):
    if name not in SOUNDS:
        raise HTTPException(400, f"unknown sound; one of {SOUNDS}")
    return await run(hatch.set_sound, name)


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Hatch</title>
<style>
body{font:16px system-ui;margin:0;padding:16px;background:#111;color:#eee;max-width:420px;margin:auto}
h1{font-size:20px;margin:8px 0;display:flex;justify-content:space-between;align-items:center}
#conn{font:12px monospace;color:#888}
#banner{display:flex;align-items:center;gap:14px;padding:14px;border-radius:12px;background:#222;margin:12px 0;border:2px solid #333}
#banner.on{border-color:#2a7}#banner.off{border-color:#a33}#banner.busy{opacity:.6}
#dot{width:48px;height:48px;border-radius:50%;background:#000;border:2px solid #444;flex:none}
#big{font-size:26px;font-weight:700}#sub{color:#aaa;font-size:14px}
.row{display:flex;gap:8px;align-items:center;margin:12px 0}
button{flex:1;padding:14px;font-size:18px;border:0;border-radius:10px;background:#333;color:#eee}
button.on{background:#2a7}button.off{background:#a33}button.sm{font-size:14px;padding:10px}
button:disabled{opacity:.5}
input[type=range]{flex:1}label{width:80px}.pct{width:44px;text-align:right;font:14px monospace;color:#aaa}
select{flex:1;padding:10px;font-size:16px;border-radius:8px;background:#222;color:#eee}
.sw{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
.sw div{height:40px;border-radius:8px;border:2px solid transparent;box-sizing:border-box}
.sw div.sel{border-color:#fff}
#toast{position:fixed;left:50%;top:16px;z-index:9;transform:translateX(-50%);padding:10px 18px;border-radius:10px;background:#2a7;color:#fff;font-weight:600;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.err{background:#a33}#toast.show{opacity:1}
#st{font:12px monospace;color:#666;white-space:pre-wrap;margin-top:16px}
</style>
<h1>Hatch remote <span id=conn>…</span></h1>
<div id=banner><div id=dot></div><div><div id=big>…</div><div id=sub></div></div></div>
<div class=row><button class=on onclick="post('/api/power/on')">Turn On</button><button class=off onclick="post('/api/power/off')">Turn Off</button></div>
<div class=row><label>Bright</label><input type=range id=br min=0 max=255 oninput="brp.textContent=pct(this.value)" onchange="post('/api/brightness/'+this.value)"><span class=pct id=brp></span></div>
<div class=row><label>Volume</label><input type=range id=vol min=0 max=255 oninput="volp.textContent=pct(this.value)" onchange="post('/api/volume/'+this.value)"><span class=pct id=volp></span></div>
<div class=row><label>Sound</label><select id=snd onchange="post('/api/sound/'+this.value)"></select></div>
<div class=sw id=sw></div>
<div class=row><label>Custom</label><input type=color id=col onchange="setColor(this.value)"><button class=sm onclick="post('/api/color',{r:0,g:0,b:0,brightness:0})">Light off</button></div>
<div id=toast></div>
<div id=st></div>
<script>
const SOUNDS=__SOUNDS__;
const SW=[[255,0,0],[255,90,0],[255,200,0],[0,200,0],[0,120,255],[160,0,255],[255,255,255],[255,150,150],[255,180,80],[0,255,180],[254,254,254],[93,211,158]];
const pct=v=>Math.round(v/255*100)+'%';
let cur=null,busy=false,tt;
snd.innerHTML=SOUNDS.map(s=>`<option>${s}</option>`).join('');
sw.innerHTML=SW.map(c=>`<div data-c="${c}" style="background:${c.join()=='254,254,254'?'linear-gradient(90deg,red,orange,yellow,green,blue,violet)':'rgb('+c+')'}" onclick="post('/api/color',{r:${c[0]},g:${c[1]},b:${c[2]}})"></div>`).join('');
function setColor(hex){const n=parseInt(hex.slice(1),16);post('/api/color',{r:n>>16,g:(n>>8)&255,b:n&255})}
function toast(msg,err){toast_.textContent=msg;toast_.className=(err?'err ':'')+'show';clearTimeout(tt);tt=setTimeout(()=>toast_.className='',err?4000:1500)}
const toast_=document.getElementById('toast');
function show(s){cur=s;
  conn.textContent=s.connected?'● connected':'○ disconnected';conn.style.color=s.connected?'#2a7':'#a33';
  banner.className=s.power?'on':'off';big.textContent=s.power?'ON':'OFF';
  const lightOn=s.brightness>0&&s.color.some(x=>x>0);
  sub.textContent=(s.power?`${s.sound} · vol ${pct(s.volume)}`:'sound + light off')+(s.power?(lightOn?` · light ${pct(s.brightness)}`:' · light off'):'');
  dot.style.background=s.power&&lightOn?`rgb(${s.color})`:'#000';dot.style.opacity=s.power&&lightOn?Math.max(.25,s.brightness/255):1;
  br.value=s.brightness;brp.textContent=pct(s.brightness);vol.value=s.volume;volp.textContent=pct(s.volume);snd.value=s.sound;
  col.value='#'+s.color.map(x=>x.toString(16).padStart(2,'0')).join('');
  document.querySelectorAll('.sw div').forEach(d=>d.classList.toggle('sel',d.dataset.c==s.color.join()));
  st.textContent=JSON.stringify(s)}
function label(u,body){const [k,v]=u.split('/api/')[1].split('/');
  if(k=='volume'||k=='brightness')return `${k} ${pct(v)}`;
  if(k=='power')return v=='on'?'turned on':'turned off';
  if(k=='color')return body.brightness===0?'light off':`color ${body.r},${body.g},${body.b}`;
  return `${k} ${v}`}
function setBusy(b){busy=b;banner.classList.toggle('busy',b);document.querySelectorAll('button,input,select').forEach(e=>e.disabled=b)}
async function post(u,body){if(busy)return;setBusy(true);
  try{const r=await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:body?JSON.stringify(body):null});
    const j=await r.json();if(!r.ok)throw new Error(j.detail||r.status);show(j);toast('✓ '+label(u,body))}
  catch(e){toast('✗ '+e.message,true);refresh()}
  finally{setBusy(false)}}
async function refresh(){if(busy)return;try{const r=await fetch('/api/state');const j=await r.json();if(!r.ok)throw new Error(j.detail);show(j)}
  catch(e){conn.textContent='○ '+e.message;conn.style.color='#a33'}}
refresh();setInterval(refresh,5000);
</script>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE.replace("__SOUNDS__", str(SOUNDS))
