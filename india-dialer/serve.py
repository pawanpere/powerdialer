#!/usr/bin/env python3
"""India dialer: the laptop cockpit for calls placed on Pawan's own phone.

    python3 india-dialer/serve.py              # http://localhost:8766, this machine only
    python3 india-dialer/serve.py --lan        # also serve the phone page /m on the local network
    python3 india-dialer/serve.py --demo       # empty database: load a few sample companies

No carrier, no recording, nothing sent on anyone's behalf. The cockpit hands
each number to the phone (tel: link or QR code), times the call, runs the
script, and records what happened. Python 3.9 compatible, stdlib server.
"""

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

common.ensure_deps()

import db  # noqa: E402
import intake  # noqa: E402
import phones  # noqa: E402
import policy  # noqa: E402

HERE = common.HERE
STATIC = os.path.join(HERE, "static")
CFG = {}
LAN = {"on": False, "token": ""}
LIVE = {}                     # agent -> what the phone companion should show
LIVE_LOCK = threading.Lock()


# ------------------------------------------------------------------ config --

def load_cfg():
    cfg = common.load_config()
    cfg["_raw_scripts"] = common.deep_copy(cfg.get("scripts") or {})
    apply_script_edits(cfg)
    return cfg


def scripts_path():
    return os.path.join(common.DATA_DIR, "scripts.json")


def apply_script_edits(cfg):
    """config.yaml ships the scripts; edits made in the cockpit live in
    data/scripts.json and are laid over them key by key."""
    try:
        with open(scripts_path(), encoding="utf-8") as fh:
            edits = json.load(fh)
    except (OSError, ValueError):
        edits = {}
    scripts = common.deep_copy(cfg.get("_raw_scripts") or {})
    for version, steps in (edits.get("tree") or {}).items():
        base = scripts.setdefault("tree", {}).setdefault(version, {"extends": steps.get("_extends")} if steps.get("_extends") else {})
        for step_id, fields in steps.items():
            if step_id.startswith("_"):
                continue
            base.setdefault("steps", {})
            base["steps"][step_id] = dict(base["steps"].get(step_id) or {}, **fields)
    for kind, fields in (edits.get("templates") or {}).items():
        scripts.setdefault("templates", {})[kind] = dict((scripts.get("templates") or {}).get(kind) or {}, **fields)
    objs = {o.get("key"): o for o in scripts.get("objections") or []}
    for key, fields in (edits.get("objections") or {}).items():
        if key in objs:
            objs[key].update(fields, edited=True)
        else:
            scripts.setdefault("objections", []).append(dict(fields, key=key, edited=True))
    if edits.get("objection_rule"):
        scripts["objection_rule"] = edits["objection_rule"]
    scripts["tree"] = resolve_tree(scripts.get("tree") or {})
    cfg["scripts"] = scripts
    cfg["_edited"] = {"steps": {v: sorted(k for k in s if not k.startswith("_")) for v, s in (edits.get("tree") or {}).items()},
                      "templates": sorted(edits.get("templates") or {}), "objections": sorted(edits.get("objections") or {}),
                      "rule": bool(edits.get("objection_rule")),
                      "custom_versions": [v for v in (edits.get("tree") or {}) if v not in ((cfg.get("_raw_scripts") or {}).get("tree") or {})]}
    return edits


def resolve_tree(tree):
    """Flatten `extends:` so the cockpit gets complete versions."""
    out = {}

    def build(name, seen=()):
        if name in out:
            return out[name]
        node = tree.get(name) or {}
        base = {"order": {}, "steps": {}}
        parent = node.get("extends")
        if parent and parent in tree and parent not in seen:
            got = build(parent, seen + (name,))
            base = {"order": dict(got["order"]), "steps": {k: dict(v) for k, v in got["steps"].items()}}
        base["order"].update(node.get("order") or {})
        for step_id, step in (node.get("steps") or {}).items():
            base["steps"][step_id] = dict(base["steps"].get(step_id, {}), **(step or {}))
        out[name] = base
        return base

    for version in tree:
        build(version)
    return out


def public_config():
    out = {k: v for k, v in CFG.items() if not k.startswith("_") and k != "import"}
    out = common.deep_copy(out)
    out["script_versions"] = list(((CFG.get("scripts") or {}).get("tree") or {}).keys()) or ["v1"]
    out["scripts_edited"] = CFG.get("_edited") or {}
    out["lan"] = {"on": LAN["on"], "url": lan_url() if LAN["on"] else ""}
    out["phone_type_source"] = "phonenumbers" if phones.PRECISE else "heuristic"
    return out


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def lan_url():
    return f"http://{lan_ip()}:{LAN.get('port', 8766)}/m?k={LAN['token']}"


# -------------------------------------------------------------------- http --

class Handler(BaseHTTPRequestHandler):
    server_version = "IndiaDialer/1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj, default=str))

    def _err(self, message, code=400):
        return self._json({"error": message}, code)

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _q(self, name, default=""):
        return (self._query().get(name) or [default])[0]

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def _allowed(self):
        """Loopback is trusted. Anything else needs the LAN token."""
        host = self.client_address[0]
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        if not LAN["on"]:
            return False
        cookie = dict(p.strip().split("=", 1) for p in (self.headers.get("Cookie") or "").split(";") if "=" in p)
        return secrets.compare_digest(self._q("k") or cookie.get("k", ""), LAN["token"])

    def _agent(self, data=None):
        raw = (data or {}).get("agent") or self._q("agent") or (CFG.get("agent") or {}).get("id") or "pawan"
        return re.sub(r"[^A-Za-z0-9_-]", "", str(raw))[:24] or "pawan"

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC, rel))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".woff2"):
            ctype = "font/woff2"
        with open(path, "rb") as fh:
            return self._send(200, fh.read(), ctype)

    # ---------------------------------------------------------------- GET --

    def do_GET(self):
        if not self._allowed():
            return self._send(403, "Forbidden", "text/plain")
        route = urllib.parse.urlparse(self.path).path
        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route in ("/m", "/m/"):
            headers = {"Set-Cookie": f"k={LAN['token']}; Path=/; HttpOnly; SameSite=Strict"} if LAN["on"] else None
            with open(os.path.join(STATIC, "m.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8", headers)
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])
        if not route.startswith("/api/"):
            return self._err("not found", 404)
        return self._api_get(route)

    def _api_get(self, route):
        agent = self._agent()
        t = db.now()
        if route == "/api/config":
            return self._json(public_config())
        if route == "/api/next":
            lead_id, reason = db.checkout(agent, CFG)
            return self._json({"lead": db.lead_payload(lead_id, CFG) if lead_id else None, "reason": reason,
                               "window": policy.window_status(policy.to_ist(t), CFG.get("calling"))})
        if route == "/api/lead":
            lead = db.lead_payload(int(self._q("id") or 0), CFG)
            return self._json({"lead": lead}) if lead else self._err("not found", 404)
        if route == "/api/queue":
            q = self._q("q").strip()
            if q:
                return self._json({"leads": db.search(q), "search": True})
            rows = db.queue(t, CFG, agent)
            return self._json({"leads": [dict(lead, group=policy.GROUP_LABEL[g]) for g, _r, lead in rows[:120]], "total": len(rows),
                               "window": policy.window_status(policy.to_ist(t), CFG.get("calling"))})
        if route == "/api/callbacks":
            return self._json({"callbacks": db.callbacks_list()})
        if route == "/api/samples":
            return self._json({"samples": db.samples_board()})
        if route == "/api/followups":
            return self._json({"due": db.followups_due(CFG)})
        if route == "/api/calls":
            start = policy.ist_day_start_utc(t) if self._q("range", "today") == "today" else None
            return self._json({"calls": db.calls_between(start, limit=300)})
        if route == "/api/window":
            return self._json(policy.window_status(policy.to_ist(t), CFG.get("calling")))
        if route == "/api/m/current":
            with LIVE_LOCK:
                return self._json(LIVE.get(agent) or {})
        return self._api_get_more(route, agent, t)

    def _api_get_more(self, route, agent, t):
        return self._err("not found", 404)

    # --------------------------------------------------------------- POST --

    def do_POST(self):
        if not self._allowed():
            return self._send(403, "Forbidden", "text/plain")
        route = urllib.parse.urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._err("bad json")
        agent = self._agent(data)
        try:
            return self._api_post(route, data, agent)
        except db.SaveError as e:
            return self._err(str(e))

    def _api_post(self, route, data, agent):
        lead_id = int(data.get("lead_id") or 0)
        if route == "/api/checkout":
            error = db.checkout_specific(lead_id, agent)
            return self._err(error, 409) if error else self._json({"lead": db.lead_payload(lead_id, CFG)})
        if route == "/api/release":
            db.release(lead_id, agent)
            return self._json({"ok": True})
        if route == "/api/skip":
            hours = float((CFG.get("dialing") or {}).get("skip_hours", 2))
            with db.connect() as con:
                con.execute("UPDATE leads SET checked_out_by=NULL, skip_until=? WHERE id=?",
                            (db.iso(db.now() + timedelta(hours=hours)), lead_id))
            return self._json({"ok": True})
        if route == "/api/dial":
            payload = db.lead_payload(lead_id, CFG)
            if payload is None:
                return self._err("That lead no longer exists.", 404)
            phone = next((p for p in payload["phones"] if p["id"] == int(data.get("phone_id") or 0)), None)
            if payload["dial_block"]:
                return self._err(payload["dial_block"], 409)
            if phone is None or phone["blocked"]:
                return self._err(phone["blocked"] if phone else "That number is not on this lead.", 409)
            return self._json({"ok": True, "dialed_at": db.log_dial(lead_id, phone["id"])})
        if route == "/api/call":
            call_id = db.save_call(data, CFG, agent)
            return self._json({"ok": True, "id": call_id, "lead": db.lead_payload(lead_id, CFG) if data.get("continued") else None})
        if route == "/api/undo":
            lead, notes = db.undo(int(data.get("id") or 0), agent, CFG)
            if lead is None:
                return self._err(notes, 409)
            return self._json({"ok": True, "lead": db.lead_payload(lead, CFG), "notes": notes})
        if route == "/api/lead/update":
            db.update_lead(lead_id, data.get("fields") or {})
            return self._json({"ok": True})
        if route == "/api/lead/clear-hold":
            db.clear_hold(lead_id, agent)
            return self._json({"ok": True, "lead": db.lead_payload(lead_id, CFG)})
        if route == "/api/lead/phone":
            pid, error = db.add_phone(lead_id, data.get("number"))
            return self._err(error) if error else self._json({"ok": True, "lead": db.lead_payload(lead_id, CFG)})
        if route == "/api/referral":
            new_id, error = db.add_referral(lead_id, data, CFG, agent)
            return self._err(error) if error else self._json({"ok": True, "id": new_id})
        if route == "/api/lead/add":
            new_id, error = db.add_referral(None, data, CFG, agent)
            return self._err(error) if error else self._json({"ok": True, "id": new_id})
        if route == "/api/sample":
            error = db.sample_stage(int(data.get("id") or 0), data.get("stage"), data)
            return self._err(error) if error else self._json({"ok": True, "samples": db.samples_board()})
        if route == "/api/followup":
            db.log_followup(lead_id, data.get("channel"), data.get("template"), data.get("to"), agent, data.get("sample_id"))
            return self._json({"ok": True})
        if route == "/api/session/start":
            versions = list(((CFG.get("scripts") or {}).get("tree") or {}).keys()) or ["v1"]
            version = data.get("script_version") if data.get("script_version") in versions else versions[0]
            return self._json({"ok": True, "id": db.session_start(agent, version, data.get("target_dials"), data.get("target_minutes")),
                               "script_version": version})
        if route == "/api/session/end":
            return self._json({"ok": True, "session": db.session_end(int(data.get("id") or 0), data.get("active_seconds"))})
        if route == "/api/agent-event":
            db.agent_event(agent, data.get("event", ""), data.get("reason", ""))
            return self._json({"ok": True})
        if route == "/api/m/live":
            with LIVE_LOCK:
                LIVE[agent] = {k: data.get(k) for k in ("state", "company", "person", "number", "dial", "kind", "others", "timer_from")}
            return self._json({"ok": True})
        return self._api_post_more(route, data, agent)

    def _api_post_more(self, route, data, agent):
        return self._err("not found", 404)


# -------------------------------------------------------------------- main --

def main():
    global CFG
    parser = argparse.ArgumentParser(description="Run the India dialer cockpit.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8766)))
    parser.add_argument("--lan", action="store_true", help="also listen on the local network for the phone page /m")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--demo", action="store_true", help="load sample companies into an empty database")
    args = parser.parse_args()

    CFG = load_cfg()
    db.init()
    if args.demo and db.lead_count() == 0:
        leads, _, _ = intake.collect(intake.read_rows(os.path.join(HERE, "demo_leads.csv"), CFG["import"]["aliases"]), CFG)
        db.import_leads(leads, "demo_leads.csv", CFG)
        print(f"  demo      loaded {len(leads)} sample companies")
    LAN["port"] = args.port
    if args.lan:
        token_file = os.path.join(common.DATA_DIR, "lan_token")
        if os.path.exists(token_file):
            LAN["token"] = open(token_file).read().strip()
        else:
            LAN["token"] = secrets.token_hex(8)
            with open(token_file, "w") as fh:
                fh.write(LAN["token"])
            os.chmod(token_file, 0o600)
        LAN["on"] = True
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    counts = db.summary_counts()
    status = counts["status"]
    window = policy.window_status(policy.to_ist(db.now()), CFG.get("calling"))
    print(f"\n  db        {db.DB_PATH}")
    print(f"  leads     {sum(status.values())} total, {status.get('NEW', 0)} to call, {status.get('PIPELINE', 0)} in the sample pipeline, "
          f"{counts['held']} held")
    print(f"  now       {window['ist']} IST, {window['label'].lower()}" + (f", cold calls open {window['next_open']}" if window.get("next_open") else ""))
    if db.CLOCK_SHIFT:
        print(f"  REHEARSAL clock moved {db.CLOCK_SHIFT} by INDIA_CLOCK_SHIFT_MIN. Unset it before real calls.")
    print(f"  cockpit   http://localhost:{args.port}")
    if LAN["on"]:
        print(f"  phone     {lan_url()}   (same Wi-Fi; keep this link private)")
    if not status:
        print("  empty     import a list:  python3 india-dialer/import.py Me/<list>.csv   (or run with --demo)")
    print()
    if not args.no_open:
        webbrowser.open(f"http://localhost:{args.port}/")
    try:
        ThreadingHTTPServer((host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
