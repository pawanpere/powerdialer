"""SQLite state for the India dialer.

One file (india-dialer/data/india.db, or $INDIA_DATA_DIR) holds the leads,
their numbers, every call, the sample / demo pipeline, follow-ups, DNC,
sessions and pause events. Times are stored in UTC as 'YYYY-MM-DD HH:MM:SS';
everything the caller sees is IST. Python 3.9 compatible.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import common
import intake

DB_PATH = os.path.join(common.DATA_DIR, "india.db")
CHECKOUT_TTL_MIN = 15
UNDO_WINDOW_SEC = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, company_key TEXT UNIQUE NOT NULL,
  tier TEXT DEFAULT 'B', city TEXT DEFAULT '', state TEXT DEFAULT '',
  type_raw TEXT DEFAULT '', segment TEXT DEFAULT '',
  ask_for TEXT DEFAULT '', why TEXT DEFAULT '',
  est_drawings_month TEXT DEFAULT '', est_volume INTEGER DEFAULT 0,
  website TEXT DEFAULT '', flags TEXT DEFAULT '', source TEXT DEFAULT '', phone_source TEXT DEFAULT '',
  hold INTEGER DEFAULT 0, hold_reason TEXT DEFAULT '', cleared_at TEXT, cleared_by TEXT,
  dm_name TEXT DEFAULT '', dm_title TEXT DEFAULT '', dm_mobile TEXT DEFAULT '',
  email TEXT DEFAULT '', whatsapp TEXT DEFAULT '', language_pref TEXT DEFAULT '',
  actual_drawings_month TEXT DEFAULT '', current_method TEXT DEFAULT '', software_used TEXT DEFAULT '',
  pain TEXT DEFAULT '', notes TEXT DEFAULT '',
  status TEXT DEFAULT 'NEW',          -- NEW OUT PIPELINE DONE DNC EXHAUSTED INTL NO_PHONE
  list_tag TEXT DEFAULT '',           -- whatsapp_linkedin once exhausted
  attempts INTEGER DEFAULT 0, last_outcome TEXT, last_called_at TEXT,
  next_action_at TEXT, next_action_type TEXT, next_half TEXT DEFAULT '',
  dm_ever INTEGER DEFAULT 0, interested_before INTEGER DEFAULT 0,
  raw_score INTEGER DEFAULT 0, rank INTEGER DEFAULT 0,
  checked_out_by TEXT, checked_out_at TEXT,
  referred_by INTEGER, international TEXT DEFAULT '',
  source_file TEXT DEFAULT '', created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS phones (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, e164 TEXT NOT NULL, dial TEXT NOT NULL,
  kind TEXT NOT NULL, raw TEXT DEFAULT '', position INTEGER DEFAULT 0,
  origin TEXT DEFAULT 'list',          -- list | dm | added | referral
  bad INTEGER DEFAULT 0, last_dialed_at TEXT, last_result TEXT,
  UNIQUE(lead_id, e164)
);
CREATE INDEX IF NOT EXISTS idx_phones_e164 ON phones(e164);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, phone_id INTEGER, number TEXT, number_kind TEXT,
  outcome TEXT NOT NULL, attempt_no INTEGER, continued INTEGER DEFAULT 0,
  dialed_at TEXT NOT NULL, connected_at TEXT, ended_at TEXT,
  ring_seconds INTEGER DEFAULT 0, talk_seconds INTEGER DEFAULT 0,
  pickup INTEGER DEFAULT 0, dm INTEGER DEFAULT 0, pitched INTEGER DEFAULT 0,
  interested INTEGER DEFAULT 0, sample_asked INTEGER DEFAULT 0, demo_booked INTEGER DEFAULT 0,
  objections TEXT, pain TEXT DEFAULT '', notes TEXT DEFAULT '',
  actual_drawings_month TEXT DEFAULT '', current_method TEXT DEFAULT '', software_used TEXT DEFAULT '',
  demo_at TEXT, callback_at TEXT,
  script_version TEXT DEFAULT '', session_id INTEGER, agent TEXT DEFAULT '',
  tier TEXT DEFAULT '', segment TEXT DEFAULT '', state TEXT DEFAULT '', city TEXT DEFAULT '',
  hour_ist INTEGER, prev_state TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_dialed ON calls(dialed_at);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);

-- The sample / demo pipeline. Everything here is credited to the dial that
-- created it (origin_call_id), not to the day the stage moved.
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL, origin_call_id INTEGER, kind TEXT DEFAULT 'sample',   -- sample | demo
  stage TEXT DEFAULT 'asked',          -- demo asked received delivered feedback quote won lost
  channel TEXT DEFAULT '',             -- whatsapp | email
  asked_at TEXT, demo_at TEXT, received_at TEXT, delivered_at TEXT, turnaround_min INTEGER,
  feedback TEXT, feedback_note TEXT DEFAULT '', feedback_at TEXT, quote_sent_at TEXT,
  won_at TEXT, lost_at TEXT, lost_reason TEXT DEFAULT '',
  deal_value_inr INTEGER DEFAULT 0, drawings_committed INTEGER DEFAULT 0, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_lead ON samples(lead_id);

CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, sample_id INTEGER,
  channel TEXT, template TEXT, to_addr TEXT DEFAULT '', at TEXT, agent TEXT DEFAULT ''
);

-- Permanent. `key` is an E.164 number or 'company:<company_key>'.
CREATE TABLE IF NOT EXISTS dnc (
  key TEXT PRIMARY KEY, reason TEXT DEFAULT '', added_at TEXT, added_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, agent TEXT, script_version TEXT DEFAULT '',
  target_dials INTEGER, target_minutes INTEGER, started_at TEXT, ended_at TEXT, active_seconds INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY, agent TEXT, event TEXT, reason TEXT DEFAULT '', at TEXT
);
"""

LEAD_UPDATABLE = ("tier", "city", "state", "type_raw", "segment", "ask_for", "why", "est_drawings_month",
                  "est_volume", "website", "flags", "source", "phone_source", "dm_name", "dm_title", "email",
                  "whatsapp", "language_pref")


# ------------------------------------------------------------------ basics --

def now():
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def iso(dt):
    return dt.isoformat(sep=" ") if dt else None


def parse(stamp):
    return datetime.fromisoformat(stamp) if stamp else None


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


def lead_count():
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


# --------------------------------------------------------------------- DNC --

def dnc_keys(con):
    return {r["key"] for r in con.execute("SELECT key FROM dnc")}


def add_dnc(con, lead_id, reason, agent):
    """Block every number on the lead and the company itself, for good."""
    t = iso(now())
    lead = con.execute("SELECT company_key FROM leads WHERE id=?", (lead_id,)).fetchone()
    keys = [r["e164"] for r in con.execute("SELECT e164 FROM phones WHERE lead_id=?", (lead_id,))]
    if lead is not None:
        keys.append("company:" + lead["company_key"])
    for key in keys:
        con.execute("INSERT OR IGNORE INTO dnc (key, reason, added_at, added_by) VALUES (?,?,?,?)",
                    (key, reason or "asked not to call", t, agent))
    con.execute("UPDATE leads SET status='DNC', checked_out_by=NULL, next_action_at=NULL, next_action_type=NULL "
                "WHERE id=?", (lead_id,))


# ------------------------------------------------------------------ import --

def import_leads(leads, source_file, cfg):
    """Insert new leads, refresh untouched ones, never disturb worked ones.
    Returns a report dict for the CLI."""
    t = iso(now())
    report = {"added": [], "refreshed": [], "kept_worked": [], "dnc": [], "held": [], "intl": [], "no_phone": [],
              "numbers_added": 0}
    with connect() as con:
        blocked = dnc_keys(con)
        for lead in leads:
            row = con.execute("SELECT * FROM leads WHERE company_key=?", (lead["company_key"],)).fetchone()
            numbers = [n for n in lead["numbers"]]
            is_dnc = ("company:" + lead["company_key"]) in blocked or any(n["e164"] in blocked for n in numbers)
            if row is None:
                status = "DNC" if is_dnc else "INTL" if (not numbers and lead["international"]) else \
                    "NO_PHONE" if not numbers else "NEW"
                fields = {k: lead.get(k, "") for k in LEAD_UPDATABLE}
                fields.update(company=lead["company"], company_key=lead["company_key"], status=status,
                              hold=1 if lead["hold_reason"] else 0, hold_reason=lead["hold_reason"],
                              international=" / ".join(lead["international"]), source_file=source_file,
                              created_at=t, updated_at=t)
                cols = ", ".join(fields)
                cur = con.execute(f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(fields))})",
                                  list(fields.values()))
                lead_id = cur.lastrowid
                report["added"].append(lead["company"])
            else:
                lead_id = row["id"]
                if row["attempts"] == 0 and row["status"] in ("NEW", "NO_PHONE", "INTL"):
                    sets = {k: lead[k] for k in LEAD_UPDATABLE if lead.get(k) not in (None, "", 0)}
                    if lead["hold_reason"] and not row["cleared_at"]:
                        sets.update(hold=1, hold_reason=lead["hold_reason"])
                    sets.update(updated_at=t, source_file=source_file)
                    con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                                list(sets.values()) + [lead_id])
                    report["refreshed"].append(lead["company"])
                else:
                    report["kept_worked"].append(lead["company"])
                if is_dnc and row["status"] != "DNC":
                    con.execute("UPDATE leads SET status='DNC' WHERE id=?", (lead_id,))
            pos = con.execute("SELECT COALESCE(MAX(position), -1) FROM phones WHERE lead_id=?", (lead_id,)).fetchone()[0]
            for n in numbers:
                pos += 1
                cur = con.execute(
                    "INSERT OR IGNORE INTO phones (lead_id, e164, dial, kind, raw, position, origin) VALUES (?,?,?,?,?,?,?)",
                    (lead_id, n["e164"], n["dial"], n["kind"], n["raw"], pos, "list"))
                report["numbers_added"] += cur.rowcount
            has_numbers = con.execute("SELECT COUNT(*) FROM phones WHERE lead_id=?", (lead_id,)).fetchone()[0]
            if has_numbers:
                con.execute("UPDATE leads SET status='NEW' WHERE id=? AND status IN ('NO_PHONE','INTL')", (lead_id,))
            if is_dnc:
                report["dnc"].append(lead["company"])
            if lead["hold_reason"]:
                report["held"].append(lead["company"])
            if lead["international"]:
                report["intl"].append((lead["company"], lead["international"], bool(numbers)))
            if not numbers and not lead["international"]:
                report["no_phone"].append(lead["company"])
    rescore(cfg)
    return report


# ----------------------------------------------------------------- scoring --

def rescore(cfg):
    """Recompute raw points and the percentile rank for every lead that can
    still be dialled. Cheap at this size; called after imports and saves."""
    scoring = (cfg or {}).get("scoring") or {}
    with connect() as con:
        rows = con.execute(
            "SELECT l.*, EXISTS(SELECT 1 FROM phones p WHERE p.lead_id=l.id AND p.kind='mobile' AND p.bad=0) AS has_mobile "
            "FROM leads l").fetchall()
        raw = {}
        for r in rows:
            lead = dict(r)
            points = intake.raw_score(lead, scoring, bool(r["has_mobile"] or r["dm_mobile"]), bool(r["interested_before"]))
            con.execute("UPDATE leads SET raw_score=? WHERE id=?", (points, r["id"]))
            if r["status"] in ("NEW", "OUT", "PIPELINE"):
                raw[r["id"]] = points
        for lead_id, rank in intake.percentile_ranks(raw).items():
            con.execute("UPDATE leads SET rank=? WHERE id=?", (rank, lead_id))


# ------------------------------------------------------------------- reads --

def lead(lead_id, con=None):
    own = con is None
    con = con or connect()
    try:
        row = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["phones"] = [dict(p) for p in con.execute(
            "SELECT * FROM phones WHERE lead_id=? ORDER BY position, id", (lead_id,))]
        return out
    finally:
        if own:
            con.close()


def summary_counts():
    with connect() as con:
        by = lambda q: {r[0]: r[1] for r in con.execute(q)}
        return {
            "status": by("SELECT status, COUNT(*) FROM leads GROUP BY status"),
            "tier": by("SELECT tier, COUNT(*) FROM leads GROUP BY tier"),
            "kind": by("SELECT kind, COUNT(*) FROM phones GROUP BY kind"),
            "primary_kind": by("SELECT p.kind, COUNT(*) FROM phones p WHERE p.position = "
                               "(SELECT MIN(position) FROM phones q WHERE q.lead_id=p.lead_id) GROUP BY p.kind"),
            "held": con.execute("SELECT COUNT(*) FROM leads WHERE hold=1 AND cleared_at IS NULL").fetchone()[0],
            "multi": [(r[0], r[1]) for r in con.execute(
                "SELECT l.company, COUNT(p.id) n FROM leads l JOIN phones p ON p.lead_id=l.id "
                "GROUP BY l.id HAVING n > 1 ORDER BY n DESC, l.company")],
        }
