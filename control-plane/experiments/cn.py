#!/usr/bin/env python3
"""Experiment client + logger against the black box.
Usage: python3 cn.py 'PING;64' ['ALTA;Lucia;82' ...]   | python3 cn.py --reset | python3 cn.py --raw METHOD PATH [BODY]
Each request/response is appended to experiments/log.jsonl with a line number (n) and an optional section (-s "topic")."""
import json, os, sys, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # control-plane/
REPO = os.path.dirname(ROOT)  # repository root, where .env lives
env = dict(l.strip().split("=",1) for l in open(os.path.join(REPO,".env")) if "=" in l)
URL, TOK = env["CAJA_NEGRA_URL"], env["CAJA_NEGRA_TOKEN"]
LOG = os.path.join(ROOT,"experiments","log.jsonl")
def nextn():
    try: return sum(1 for _ in open(LOG))+1
    except FileNotFoundError: return 1
def call(method, path, body=None, headers=None, section="", raw_body=None, token=TOK):
    h = {"Authorization": f"Bearer {token}"} if token is not None else {}
    if body is not None or raw_body is not None: h["Content-Type"]="application/json"
    if headers: h.update(headers)
    data = raw_body.encode() if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL+path, data=data, method=method, headers=h)
    t0=time.time()
    try:
        r = urllib.request.urlopen(req, timeout=15); status=r.status; rb=r.read().decode(); rh=dict(r.headers)
    except urllib.error.HTTPError as e:
        status=e.code; rb=e.read().decode(); rh=dict(e.headers)
    except (TimeoutError, OSError, urllib.error.URLError) as e:
        # Network failure (timeout, reset, DNS): logged as status 0 so the monitor
        # classifies it as infrastructure noise instead of crashing.
        status=0; rb=f"<network-error {type(e).__name__}: {str(e)[:80]}>"; rh={}
    ms=int((time.time()-t0)*1000)
    n=nextn()
    logged_body = raw_body if raw_body is not None else body
    if isinstance(logged_body,str) and len(logged_body)>2000:
        # NOTE: this placeholder string is kept in Spanish on purpose: it is
        # already written verbatim in log.jsonl/experimentos.jsonl and must stay
        # byte-compatible with those frozen files.
        logged_body = f"<cuerpo sintetico de {len(logged_body.encode())} bytes: {logged_body[:40]}...>"
    rec={"n":n,"ts":time.strftime("%H:%M:%S"),"section":section,"req":{"method":method,"path":path,"body":logged_body,"body_bytes":len(data) if data else 0,"headers":{k:v for k,v in h.items() if k!="Authorization"},"auth_kind":("none" if "Authorization" not in h else ("valid" if h["Authorization"]==f"Bearer {TOK}" else ("foreign-token" if h["Authorization"].startswith("Bearer ") else ("lowercase-bearer" if h["Authorization"].lower().startswith("bearer ") else "no-scheme"))))},"res":{"status":status,"body":rb,"content_type":rh.get("content-type") or rh.get("Content-Type"),"x_req":rh.get("x-req") or rh.get("X-Req")},"ms":ms}
    with open(LOG,"a") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")
    print(f"[{n:04d}] x-req={rec['res']['x_req']} {method} {path} {json.dumps(logged_body,ensure_ascii=False) if logged_body is not None else ''} -> {status} {rb[:200]}")
    return status, rb
def msg(m, section=""): return call("POST","/msg",{"m":m},section=section)
def reset(section=""): return call("POST","/__reset",section=section)
if __name__=="__main__":
    a=sys.argv[1:]; section=""
    if a and a[0]=="-s": section=a[1]; a=a[2:]
    if a and a[0]=="--reset": reset(section); sys.exit()
    if a and a[0]=="--raw": call(a[1],a[2],raw_body=(a[3] if len(a)>3 else None),section=section); sys.exit()
    for m in a: msg(m, section)
