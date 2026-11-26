"""
Lumi - salon management (hairdresser): one SQLite database for the whole business.

  clients · staff · services · appointments · products / stock · suppliers · sales · photos · settings

Used by the voice tool "salon", the desktop wallpaper (today's summary) and, later, the web panel.
Data: memory/salon.db  (never sent in updates, never uploaded). A daily backup goes to memory/backups/.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "memory" / "salon.db"
BACKUP_DIR = BASE_DIR / "memory" / "backups"
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT DEFAULT 'Peluquero/a', phone TEXT DEFAULT '',
  color TEXT DEFAULT '#d9a6a1', commission_pct REAL DEFAULT 0, active INTEGER DEFAULT 1,
  schedule TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT DEFAULT '', duration_min INTEGER DEFAULT 30,
  price REAL DEFAULT 0, active INTEGER DEFAULT 1, products TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '', birthday TEXT DEFAULT '',
  allergies TEXT DEFAULT '', color_formula TEXT DEFAULT '', preferences TEXT DEFAULT '', notes TEXT DEFAULT '',
  consent_photos INTEGER DEFAULT 0, consent_marketing INTEGER DEFAULT 0, created TEXT, last_visit TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY, client_id INTEGER, staff_id INTEGER, service_id INTEGER, start TEXT NOT NULL, "end" TEXT NOT NULL,
  status TEXT DEFAULT 'booked', price REAL DEFAULT 0, notes TEXT DEFAULT '', reminded INTEGER DEFAULT 0, created TEXT);
CREATE INDEX IF NOT EXISTS idx_app_start ON appointments(start);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '', notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, brand TEXT DEFAULT '', category TEXT DEFAULT '', unit TEXT DEFAULT 'ud',
  stock REAL DEFAULT 0, min_stock REAL DEFAULT 0, cost REAL DEFAULT 0, price REAL DEFAULT 0, retail INTEGER DEFAULT 0,
  supplier_id INTEGER, barcode TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY, product_id INTEGER, qty REAL, reason TEXT, ref TEXT DEFAULT '', ts TEXT);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY, ts TEXT, client_id INTEGER, staff_id INTEGER, appointment_id INTEGER,
  total REAL, tip REAL DEFAULT 0, method TEXT DEFAULT 'efectivo', items TEXT DEFAULT '[]', notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY, client_id INTEGER, path TEXT, kind TEXT, note TEXT DEFAULT '', ts TEXT);
"""

DEFAULT_SERVICES = [
    ("Corte mujer", "Corte", 45, 25), ("Corte hombre", "Corte", 30, 15), ("Corte niño/a", "Corte", 25, 12),
    ("Lavado y secado", "Peinado", 30, 15), ("Peinado / recogido", "Peinado", 60, 35), ("Barba", "Barbería", 20, 10),
    ("Tinte raíz", "Color", 75, 35), ("Tinte completo", "Color", 90, 45), ("Mechas", "Color", 120, 70),
    ("Balayage", "Color", 150, 95), ("Matiz / baño de color", "Color", 30, 18),
    ("Tratamiento hidratante", "Tratamiento", 30, 20), ("Keratina / alisado", "Tratamiento", 150, 120),
]
DEFAULT_HOURS = {  # weekday (0=Monday) -> [open, close]; missing = closed
    "0": ["09:30", "20:00"], "1": ["09:30", "20:00"], "2": ["09:30", "20:00"],
    "3": ["09:30", "20:00"], "4": ["09:30", "20:00"], "5": ["09:00", "14:00"],
}
PAY_METHODS = {"efectivo": "efectivo", "cash": "efectivo", "tarjeta": "tarjeta", "card": "tarjeta",
               "bizum": "bizum", "transferencia": "transferencia", "transfer": "transferencia"}


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fday(d) -> str:
    """'miércoles 03/12' (Spanish, independent of the PC locale)."""
    return f"{DIAS[d.weekday()]} {d:%d/%m}"


# ───────────────────────── db ─────────────────────────
def db() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            if not _conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
                _conn.executemany("INSERT INTO services(name, category, duration_min, price) VALUES (?,?,?,?)",
                                  DEFAULT_SERVICES)
            if get_setting("hours", None, _conn) is None:
                set_setting("hours", DEFAULT_HOURS, _conn)
                set_setting("slot_min", 15, _conn)
                set_setting("salon_name", "", _conn)
        return _conn


def q(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def q1(sql: str, args=()) -> dict | None:
    r = q(sql, args)
    return r[0] if r else None


def run(sql: str, args=()) -> int:
    with _lock:
        cur = db().execute(sql, args)
        return cur.lastrowid


def get_setting(key, default=None, conn=None):
    c = conn or db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def set_setting(key, value, conn=None):
    (conn or db()).execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(value, ensure_ascii=False)))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def backup():
    """One copy per day, keeps 30."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f"salon_{date.today().isoformat()}.db"
        if not dst.exists():
            with _lock:
                b = sqlite3.connect(str(dst))
                db().backup(b)
                b.close()
        for old in sorted(BACKUP_DIR.glob("salon_*.db"))[:-30]:
            old.unlink(missing_ok=True)
    except Exception as e:
        print(f"[Lumi] Salon backup failed: {e}")


# ───────────────────────── fuzzy finding ─────────────────────────
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@+]", " ", s)).strip()


def _score(query: str, text: str) -> float:
    a, b = norm(query), norm(text)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b:
        return 85 - (len(b) - len(a)) * .2
    aw, bw = set(a.split()), set(b.split())
    common = sum(1 for w in aw if any(x.startswith(w) or w.startswith(x) for x in bw))
    return common * 30 / max(len(aw), 1)


def _best(rows: list[dict], query: str, field="name", minimum=25):
    if not query:
        return None
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 6:
        for r in rows:
            if digits[-9:] and re.sub(r"\D", "", r.get("phone") or "").endswith(digits[-9:]):
                return r
    best = max(rows, key=lambda r: _score(query, r.get(field, "")), default=None)
    return best if best and _score(query, best.get(field, "")) >= minimum else None


def find_client(query: str):
    return _best(q("SELECT * FROM clients"), query)


def find_staff(query: str):
    return _best(q("SELECT * FROM staff WHERE active=1"), query)


def find_service(query: str):
    return _best(q("SELECT * FROM services WHERE active=1"), query, minimum=20)


def find_product(query: str):
    rows = q("SELECT * FROM products WHERE active=1")
    for r in rows:
        if query and r.get("barcode") and r["barcode"] == query.strip():
            return r
    for r in rows:
        r["_full"] = f"{r['brand']} {r['name']}"
    return _best(rows, query, field="_full", minimum=20)


def parse_dt(text: str | None) -> datetime | None:
    """'2026-11-25 17:30', '2026-11-25T17:30', '17:30' (today), 'mañana 17:30' (simple cases)."""
    if not text:
        return None
    t = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m %H:%M"):
        try:
            d = datetime.strptime(t, fmt)
            return d.replace(year=date.today().year) if fmt == "%d/%m %H:%M" else d
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[:.h](\d{2})", t) or re.search(r"\b(\d{1,2})\s*(?:h|horas?)\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
        base = date.today()
        n = norm(t)
        if "pasado manana" in n:
            base += timedelta(days=2)
        elif "manana" in n or "tomorrow" in n:
            base += timedelta(days=1)
        return datetime.combine(base, datetime.min.time()).replace(hour=hh, minute=mm)
    return None


def parse_day(text: str | None) -> date:
    if not text:
        return date.today()
    n = norm(text)
    if n in ("hoy", "today"):
        return date.today()
    if n in ("manana", "tomorrow"):
        return date.today() + timedelta(days=1)
    try:
        return datetime.fromisoformat(text.strip()[:10]).date()
    except ValueError:
        d = parse_dt(text + " 00:00")
        return d.date() if d else date.today()


# ───────────────────────── clients ─────────────────────────
CLIENT_FIELDS = ("name", "phone", "email", "birthday", "allergies", "color_formula", "preferences", "notes",
                 "consent_photos", "consent_marketing")


def add_client(name: str, **fields) -> dict:
    existing = find_client(fields.get("phone") or "") if fields.get("phone") else None
    if existing:
        update_client(existing["id"], **{"name": name, **fields})
        return q1("SELECT * FROM clients WHERE id=?", (existing["id"],))
    cid = run("INSERT INTO clients(name, created) VALUES(?, ?)", (name.strip(), now_iso()))
    update_client(cid, **fields)
    return q1("SELECT * FROM clients WHERE id=?", (cid,))


def update_client(cid: int, **fields):
    for k, v in fields.items():
        if k in CLIENT_FIELDS and v not in (None, ""):
            if k in ("notes", "preferences", "allergies", "color_formula") and k != "name":
                old = (q1(f"SELECT {k} FROM clients WHERE id=?", (cid,)) or {}).get(k) or ""
                if old and str(v) not in old and k == "notes":
                    v = f"{old}\n{date.today():%d/%m/%Y}: {v}"
            run(f"UPDATE clients SET {k}=? WHERE id=?", (v, cid))


def client_card(c: dict) -> str:
    visits = q("""SELECT a.start, s.name service, st.name staff FROM appointments a
                  LEFT JOIN services s ON s.id=a.service_id LEFT JOIN staff st ON st.id=a.staff_id
                  WHERE a.client_id=? AND a.status='done' ORDER BY a.start DESC LIMIT 5""", (c["id"],))
    spent = q1("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM sales WHERE client_id=?", (c["id"],))
    nxt = q1("""SELECT a.start, s.name service FROM appointments a LEFT JOIN services s ON s.id=a.service_id
                WHERE a.client_id=? AND a.status IN ('booked','confirmed') AND a.start>=? ORDER BY a.start LIMIT 1""",
             (c["id"], now_iso()))
    parts = [f"{c['name']}" + (f" · {c['phone']}" if c["phone"] else "")]
    if c["allergies"]:
        parts.append(f"⚠ ALERGIAS: {c['allergies']}")
    if c["color_formula"]:
        parts.append(f"Fórmula de color: {c['color_formula']}")
    if c["preferences"]:
        parts.append(f"Preferencias: {c['preferences']}")
    if visits:
        parts.append("Últimas visitas: " + "; ".join(f"{v['start'][:10]} {v['service'] or ''} ({v['staff'] or '-'})" for v in visits))
    if spent and spent["n"]:
        parts.append(f"Ha gastado {spent['t']:.2f} € en {spent['n']} compra(s).")
    if nxt:
        parts.append(f"Próxima cita: {nxt['start'][:16].replace('T', ' ')} ({nxt['service']}).")
    if c["notes"]:
        parts.append(f"Notas: {c['notes'][-300:]}")
    return "\n".join(parts)


# ───────────────────────── staff & services ─────────────────────────
def add_staff(name: str, role: str = "", commission_pct=None, phone: str = "") -> dict:
    ex = find_staff(name)
    if ex and norm(ex["name"]) == norm(name):
        return ex
    colors = ["#d9a6a1", "#9fc9b8", "#c9b3e6", "#f0c987", "#8fb8de", "#e8a0bf", "#b5c99a"]
    n = q1("SELECT COUNT(*) n FROM staff")["n"]
    sid = run("INSERT INTO staff(name, role, phone, color, commission_pct) VALUES(?,?,?,?,?)",
              (name.strip(), role or "Peluquero/a", phone, colors[n % len(colors)], float(commission_pct or 0)))
    return q1("SELECT * FROM staff WHERE id=?", (sid,))


def set_service(name: str, duration_min=None, price=None, category: str = "") -> dict:
    s = find_service(name)
    if s and _score(name, s["name"]) >= 85:
        if duration_min:
            run("UPDATE services SET duration_min=? WHERE id=?", (int(duration_min), s["id"]))
        if price is not None:
            run("UPDATE services SET price=? WHERE id=?", (float(price), s["id"]))
        return q1("SELECT * FROM services WHERE id=?", (s["id"],))
    sid = run("INSERT INTO services(name, category, duration_min, price) VALUES(?,?,?,?)",
              (name.strip(), category, int(duration_min or 30), float(price or 0)))
    return q1("SELECT * FROM services WHERE id=?", (sid,))


def link_product_to_service(service: dict, product: dict, qty: float):
    items = json.loads(service.get("products") or "[]")
    items = [i for i in items if i["product_id"] != product["id"]] + [{"product_id": product["id"], "qty": float(qty)}]
    run("UPDATE services SET products=? WHERE id=?", (json.dumps(items), service["id"]))


# ───────────────────────── agenda ─────────────────────────
def _hours_for(day: date):
    h = get_setting("hours", DEFAULT_HOURS).get(str(day.weekday()))
    if not h:
        return None
    o = datetime.combine(day, datetime.strptime(h[0], "%H:%M").time())
    c = datetime.combine(day, datetime.strptime(h[1], "%H:%M").time())
    return o, c


def appointments_on(day: date, staff_id=None) -> list[dict]:
    a, b = datetime.combine(day, datetime.min.time()).isoformat(), datetime.combine(day + timedelta(days=1), datetime.min.time()).isoformat()
    sql = """SELECT a.*, c.name client, c.phone client_phone, c.allergies, s.name service, st.name staff, st.color staff_color
             FROM appointments a LEFT JOIN clients c ON c.id=a.client_id LEFT JOIN services s ON s.id=a.service_id
             LEFT JOIN staff st ON st.id=a.staff_id
             WHERE a.start>=? AND a.start<? AND a.status!='cancelled'"""
    args = [a, b]
    if staff_id:
        sql += " AND a.staff_id=?"
        args.append(staff_id)
    return q(sql + " ORDER BY a.start", args)


def _busy(staff_id, start: datetime, end: datetime, ignore_id=None) -> bool:
    rows = q("""SELECT id FROM appointments WHERE status IN ('booked','confirmed','done') AND staff_id IS ?
                AND start < ? AND "end" > ?""", (staff_id, end.isoformat(), start.isoformat()))
    return any(r["id"] != ignore_id for r in rows)


def free_slots(day: date, duration_min: int, staff_id=None, limit: int = 12) -> list[tuple[datetime, dict | None]]:
    hours = _hours_for(day)
    if not hours:
        return []
    step = int(get_setting("slot_min", 15))
    staff = [q1("SELECT * FROM staff WHERE id=?", (staff_id,))] if staff_id else (q("SELECT * FROM staff WHERE active=1") or [None])
    out, t = [], hours[0]
    now = datetime.now()
    while t + timedelta(minutes=duration_min) <= hours[1] and len(out) < limit:
        if t > now:
            for s in staff:
                if not _busy(s["id"] if s else None, t, t + timedelta(minutes=duration_min)):
                    out.append((t, s))
                    break
        t += timedelta(minutes=step)
    return out


def book(client_q: str, service_q: str, when: str, staff_q: str = "", phone: str = "", notes: str = "") -> str:
    svc = find_service(service_q)
    if not svc:
        return f"No tengo el servicio «{service_q}». Servicios: " + ", ".join(s["name"] for s in q("SELECT name FROM services WHERE active=1"))
    start = parse_dt(when)
    if not start:
        return "Dime el día y la hora de la cita (por ejemplo 2026-12-03 17:30)."
    end = start + timedelta(minutes=int(svc["duration_min"]))
    hours = _hours_for(start.date())
    if not hours or start < hours[0] or end > hours[1]:
        return f"A esa hora el salón está cerrado. " + _slots_text(start.date(), svc)
    client = find_client(phone or client_q) or add_client(client_q, phone=phone)
    staff = find_staff(staff_q) if staff_q else None
    if staff_q and not staff:
        return f"No tengo a «{staff_q}» en el equipo. Equipo: " + ", ".join(s["name"] for s in q("SELECT name FROM staff WHERE active=1"))
    if staff is None:
        for s in q("SELECT * FROM staff WHERE active=1") or [None]:
            if not _busy(s["id"] if s else None, start, end):
                staff = s
                break
        else:
            return "A esa hora no hay nadie libre. " + _slots_text(start.date(), svc)
    elif _busy(staff["id"], start, end):
        return f"{staff['name']} está ocupado/a a esa hora. " + _slots_text(start.date(), svc, staff["id"])
    aid = run("""INSERT INTO appointments(client_id, staff_id, service_id, start, "end", price, notes, created)
                 VALUES(?,?,?,?,?,?,?,?)""", (client["id"], staff["id"] if staff else None, svc["id"], start.isoformat(),
                                               end.isoformat(), svc["price"], notes, now_iso()))
    warn = f" ⚠ Ojo: alergias de {client['name']}: {client['allergies']}." if client.get("allergies") else ""
    return (f"Cita #{aid} reservada: {client['name']}, {svc['name']}, {fday(start)} a las {start:%H:%M}"
            + (f" con {staff['name']}" if staff else "") + f" ({svc['duration_min']} min, {svc['price']:.2f} €).{warn}")


def _slots_text(day: date, svc: dict, staff_id=None) -> str:
    sl = free_slots(day, int(svc["duration_min"]), staff_id, limit=6)
    if not sl:
        return f"No quedan huecos el {day:%d/%m} para {svc['name']}."
    return f"Huecos libres el {day:%d/%m} para {svc['name']}: " + ", ".join(
        f"{t:%H:%M}" + (f" ({s['name']})" if s else "") for t, s in sl) + "."


def _find_appointment(client_q: str = "", when: str = "", appt_id=None) -> dict | None:
    if appt_id:
        return q1("SELECT * FROM appointments WHERE id=?", (int(appt_id),))
    c = find_client(client_q) if client_q else None
    if not c:
        return None
    rows = q("""SELECT * FROM appointments WHERE client_id=? AND status IN ('booked','confirmed')
                AND start>=? ORDER BY start""", (c["id"], (datetime.now() - timedelta(hours=3)).isoformat()))
    if when and rows:
        d = parse_dt(when)
        if d:
            rows.sort(key=lambda r: abs((datetime.fromisoformat(r["start"]) - d).total_seconds()))
    return rows[0] if rows else None


def cancel(client_q="", when="", appt_id=None, noshow=False) -> str:
    a = _find_appointment(client_q, when, appt_id)
    if not a:
        return "No encuentro esa cita."
    run("UPDATE appointments SET status=? WHERE id=?", ("noshow" if noshow else "cancelled", a["id"]))
    return f"Cita del {a['start'][:16].replace('T', ' ')} {'marcada como no presentada' if noshow else 'cancelada'}."


def move(client_q="", new_when="", appt_id=None, staff_q="") -> str:
    a = _find_appointment(client_q, "", appt_id)
    if not a:
        return "No encuentro esa cita."
    start = parse_dt(new_when)
    if not start:
        return "Dime la nueva fecha y hora."
    dur = datetime.fromisoformat(a["end"]) - datetime.fromisoformat(a["start"])
    staff = find_staff(staff_q) if staff_q else q1("SELECT * FROM staff WHERE id=?", (a["staff_id"],)) if a["staff_id"] else None
    if _busy(staff["id"] if staff else None, start, start + dur, ignore_id=a["id"]):
        svc = q1("SELECT * FROM services WHERE id=?", (a["service_id"],))
        return "A esa hora no se puede. " + _slots_text(start.date(), svc, staff["id"] if staff else None)
    run('UPDATE appointments SET start=?, "end"=?, staff_id=?, reminded=0 WHERE id=?',
        (start.isoformat(), (start + dur).isoformat(), staff["id"] if staff else None, a["id"]))
    return f"Cita movida al {fday(start)} a las {start:%H:%M}."


def agenda_text(day: date, staff_q: str = "") -> str:
    staff = find_staff(staff_q) if staff_q else None
    rows = appointments_on(day, staff["id"] if staff else None)
    if not rows:
        return f"No hay citas el {fday(day)}."
    return f"{len(rows)} cita(s) el {fday(day)}:\n" + "\n".join(
        f"- {r['start'][11:16]} {r['client'] or '¿?'} · {r['service'] or ''}" + (f" · {r['staff']}" if r["staff"] else "")
        + (" ✓" if r["status"] == "done" else "") + (f" ⚠ {r['allergies']}" if r["allergies"] else "") for r in rows)


# ───────────────────────── inventory ─────────────────────────
def add_product(name: str, brand="", category="", stock=0, min_stock=0, cost=0, price=0, retail=False,
                unit="ud", supplier="", barcode="") -> dict:
    p = find_product(f"{brand} {name}".strip())
    if p and _score(f"{brand} {name}", p["_full"]) >= 85:
        return p
    sup = None
    if supplier:
        sup = _best(q("SELECT * FROM suppliers"), supplier) or {"id": run("INSERT INTO suppliers(name) VALUES(?)", (supplier,))}
    pid = run("""INSERT INTO products(name, brand, category, unit, stock, min_stock, cost, price, retail, supplier_id, barcode)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (name.strip(), brand, category, unit, float(stock or 0), float(min_stock or 0),
                                                    float(cost or 0), float(price or 0), 1 if retail else 0,
                                                    sup["id"] if sup else None, barcode))
    if stock:
        run("INSERT INTO stock_moves(product_id, qty, reason, ts) VALUES(?,?,?,?)", (pid, float(stock), "alta", now_iso()))
    return q1("SELECT * FROM products WHERE id=?", (pid,))


def move_stock(product: dict, qty: float, reason: str, ref: str = "") -> dict:
    run("UPDATE products SET stock=stock+? WHERE id=?", (float(qty), product["id"]))
    run("INSERT INTO stock_moves(product_id, qty, reason, ref, ts) VALUES(?,?,?,?,?)",
        (product["id"], float(qty), reason, ref, now_iso()))
    return q1("SELECT * FROM products WHERE id=?", (product["id"],))


def low_stock() -> list[dict]:
    return q("""SELECT p.*, s.name supplier FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id
                WHERE p.active=1 AND p.stock<=p.min_stock ORDER BY s.name, p.name""")


def order_list() -> str:
    rows = low_stock()
    if not rows:
        return "No hace falta pedir nada: todo el stock está por encima del mínimo."
    by = {}
    for r in rows:
        need = max(r["min_stock"] * 2 - r["stock"], 1)
        by.setdefault(r["supplier"] or "Sin proveedor", []).append(f"{r['brand']} {r['name']}".strip() + f": {need:g} {r['unit']} (quedan {r['stock']:g})")
    return "Lista de pedido:\n" + "\n".join(f"{s}:\n  - " + "\n  - ".join(items) for s, items in by.items())


def inventory_text(query: str = "") -> str:
    rows = q("SELECT * FROM products WHERE active=1 ORDER BY category, name")
    if query:
        rows = [r for r in rows if _score(query, f"{r['brand']} {r['name']} {r['category']}") >= 20]
    if not rows:
        return "No hay productos en el inventario todavía." if not query else f"No encuentro productos de «{query}»."
    value = sum(r["stock"] * r["cost"] for r in rows)
    return f"{len(rows)} producto(s), valor a coste {value:.2f} €:\n" + "\n".join(
        f"- {r['brand']} {r['name']}".replace("-  ", "- ") + f": {r['stock']:g} {r['unit']}" + (" ⚠ BAJO" if r["stock"] <= r["min_stock"] else "")
        for r in rows[:40])


# ───────────────────────── sales / cash ─────────────────────────
def finish_appointment(a: dict):
    """Mark as done, use the service's products from stock, update the client's last visit."""
    run("UPDATE appointments SET status='done' WHERE id=?", (a["id"],))
    run("UPDATE clients SET last_visit=? WHERE id=?", (a["start"][:10], a["client_id"]))
    svc = q1("SELECT * FROM services WHERE id=?", (a["service_id"],))
    for it in json.loads((svc or {}).get("products") or "[]"):
        p = q1("SELECT * FROM products WHERE id=?", (it["product_id"],))
        if p:
            move_stock(p, -float(it["qty"]), "servicio", f"cita {a['id']}")


def charge(client_q: str = "", amount=None, method: str = "efectivo", tip=0, products: str = "", staff_q: str = "",
           appt_id=None) -> str:
    method = PAY_METHODS.get(norm(method), "efectivo")
    a = _find_appointment("", "", appt_id) if appt_id else None
    if a is None and client_q:
        c = find_client(client_q)
        if c:      # only today's appointment is charged together with the payment
            a = q1("""SELECT * FROM appointments WHERE client_id=? AND status IN ('booked','confirmed') AND substr(start,1,10)=?
                      ORDER BY start LIMIT 1""", (c["id"], date.today().isoformat()))
    client = q1("SELECT * FROM clients WHERE id=?", (a["client_id"],)) if a else (find_client(client_q) if client_q else None)
    items, total = [], 0.0
    if a:
        svc = q1("SELECT * FROM services WHERE id=?", (a["service_id"],))
        items.append({"type": "service", "name": svc["name"] if svc else "Servicio", "price": a["price"]})
        total += a["price"] or 0
        finish_appointment(a)
    for name in [x.strip() for x in re.split(r"[,;]| y ", products or "") if x.strip()]:
        p = find_product(name)
        if p:
            items.append({"type": "product", "name": f"{p['brand']} {p['name']}".strip(), "price": p["price"], "product_id": p["id"]})
            total += p["price"] or 0
            move_stock(p, -1, "venta", "caja")
    if amount not in (None, ""):
        total = float(amount)
    if total <= 0:
        return "¿Cuánto cobro?"
    staff = find_staff(staff_q) if staff_q else (q1("SELECT * FROM staff WHERE id=?", (a["staff_id"],)) if a and a["staff_id"] else None)
    sid = run("""INSERT INTO sales(ts, client_id, staff_id, appointment_id, total, tip, method, items)
                 VALUES(?,?,?,?,?,?,?,?)""", (now_iso(), client["id"] if client else None, staff["id"] if staff else None,
                                               a["id"] if a else None, total, float(tip or 0), method, json.dumps(items, ensure_ascii=False)))
    return f"Cobrado: {total:.2f} € en {method}" + (f" + {float(tip):.2f} € de propina" if tip else "") + \
        (f" a {client['name']}" if client else "") + f" (ticket #{sid})."


def report(period: str = "today") -> str:
    today = date.today()
    if period in ("week", "semana"):
        a, label = today - timedelta(days=today.weekday()), "esta semana"
    elif period in ("month", "mes"):
        a, label = today.replace(day=1), "este mes"
    elif period in ("yesterday", "ayer"):
        a, label = today - timedelta(days=1), "ayer"
        today = a
    else:
        a, label = today, "hoy"
    s, e = a.isoformat(), (today + timedelta(days=1)).isoformat()
    tot = q1("SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(tip),0) tip, COUNT(*) n FROM sales WHERE ts>=? AND ts<?", (s, e))
    by_m = q("SELECT method, SUM(total) t FROM sales WHERE ts>=? AND ts<? GROUP BY method", (s, e))
    by_s = q("""SELECT st.name, SUM(sa.total) t, st.commission_pct pct FROM sales sa JOIN staff st ON st.id=sa.staff_id
                WHERE sa.ts>=? AND sa.ts<? GROUP BY st.id ORDER BY t DESC""", (s, e))
    top = q("""SELECT s.name, COUNT(*) n FROM appointments ap JOIN services s ON s.id=ap.service_id
               WHERE ap.status='done' AND ap.start>=? AND ap.start<? GROUP BY s.id ORDER BY n DESC LIMIT 3""", (s, e))
    noshow = q1("SELECT COUNT(*) n FROM appointments WHERE status='noshow' AND start>=? AND start<?", (s, e))["n"]
    new = q1("SELECT COUNT(*) n FROM clients WHERE created>=? AND created<?", (s, e))["n"]
    lines = [f"Caja {label}: {tot['t']:.2f} € en {tot['n']} ticket(s)" + (f" + {tot['tip']:.2f} € de propinas" if tot["tip"] else "") + "."]
    if by_m:
        lines.append("Por forma de pago: " + ", ".join(f"{m['method']} {m['t']:.2f} €" for m in by_m) + ".")
    if by_s:
        lines.append("Por persona: " + ", ".join(f"{x['name']} {x['t']:.2f} €" + (f" (comisión {x['t'] * x['pct'] / 100:.2f} €)" if x["pct"] else "") for x in by_s) + ".")
    if top:
        lines.append("Servicios más hechos: " + ", ".join(f"{t['name']} ({t['n']})" for t in top) + ".")
    if new or noshow:
        lines.append(f"Clientes nuevos: {new}. No presentados: {noshow}.")
    return "\n".join(lines)


# ───────────────────────── summaries for the desktop ─────────────────────────
def today_summary() -> dict:
    today = date.today()
    apps = appointments_on(today)
    now = datetime.now().isoformat()
    nxt = next((a for a in apps if a["start"] >= now and a["status"] in ("booked", "confirmed")), None)
    tot = q1("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM sales WHERE substr(ts,1,10)=?", (today.isoformat(),))
    md = today.strftime("%m-%d")
    bdays = [c["name"] for c in q("SELECT name, birthday FROM clients WHERE birthday!=''") if (c["birthday"] or "")[5:10] == md]
    hours = _hours_for(today)
    return {
        "ok": True, "date": today.isoformat(), "salon": get_setting("salon_name", ""),
        "open": hours[0].strftime("%H:%M") if hours else None, "close": hours[1].strftime("%H:%M") if hours else None,
        "appointments": [{k: a.get(k) for k in ("id", "start", "end", "status", "client", "service", "staff", "staff_color", "allergies")} for a in apps],
        "next": {k: nxt.get(k) for k in ("start", "client", "service", "staff", "allergies")} if nxt else None,
        "takings": round(tot["t"], 2), "tickets": tot["n"],
        "low_stock": [{"name": f"{p['brand']} {p['name']}".strip(), "stock": p["stock"], "unit": p["unit"]} for p in low_stock()[:6]],
        "birthdays": bdays,
        "staff": [{"name": s["name"], "color": s["color"]} for s in q("SELECT name, color FROM staff WHERE active=1")],
    }


def panel_data(kind: str) -> dict:
    """Structured data for the desktop panels (Huecos, Caja, Stock)."""
    today = date.today()
    if kind == "slots":
        days = []
        d = today
        for _ in range(7):                      # today, and the next open days until 2 are shown
            hours = _hours_for(d)
            if hours:
                sl = free_slots(d, 30, limit=60)
                if sl or d != today:
                    days.append({"date": d.isoformat(), "label": fday(d) if d != today else "hoy",
                                 "open": hours[0].strftime("%H:%M"), "close": hours[1].strftime("%H:%M"),
                                 "slots": [{"t": t.strftime("%H:%M"), "staff": s["name"] if s else ""} for t, s in sl if t.minute % 30 == 0]})
            if len(days) >= 2:
                break
            d += timedelta(days=1)
        svcs = [{"name": s["name"], "min": s["duration_min"], "price": s["price"]}
                for s in q("SELECT name, duration_min, price FROM services WHERE active=1 ORDER BY name")]
        return {"ok": True, "kind": kind, "days": days, "services": svcs}
    if kind == "cash":
        def tot(a: date, b: date):
            r = q1("SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(tip),0) tip, COUNT(*) n FROM sales WHERE ts>=? AND ts<?",
                   (a.isoformat(), b.isoformat()))
            return {"total": round(r["t"], 2), "tips": round(r["tip"], 2), "tickets": r["n"]}
        tm = today + timedelta(days=1)
        s, e = today.isoformat(), tm.isoformat()
        sales = q("""SELECT sa.ts, sa.total, sa.tip, sa.method, sa.items, c.name client, st.name staff FROM sales sa
                     LEFT JOIN clients c ON c.id=sa.client_id LEFT JOIN staff st ON st.id=sa.staff_id
                     WHERE sa.ts>=? AND sa.ts<? ORDER BY sa.ts DESC""", (s, e))
        def what(js):
            try:
                return ", ".join(i.get("name", "") for i in json.loads(js or "[]"))
            except Exception:
                return ""
        return {"ok": True, "kind": kind,
                "today": tot(today, tm), "week": tot(today - timedelta(days=today.weekday()), tm),
                "month": tot(today.replace(day=1), tm),
                "by_method": [{"method": m["method"], "total": round(m["t"], 2)} for m in
                              q("SELECT method, SUM(total) t FROM sales WHERE ts>=? AND ts<? GROUP BY method ORDER BY t DESC", (s, e))],
                "by_staff": [{"name": x["name"], "total": round(x["t"], 2), "commission": round(x["t"] * (x["pct"] or 0) / 100, 2)} for x in
                             q("""SELECT st.name, SUM(sa.total) t, st.commission_pct pct FROM sales sa JOIN staff st ON st.id=sa.staff_id
                                  WHERE sa.ts>=? AND sa.ts<? GROUP BY st.id ORDER BY t DESC""", (s, e))],
                "sales": [{"t": r["ts"][11:16], "client": r["client"] or "", "staff": r["staff"] or "", "total": r["total"],
                           "tip": r["tip"], "method": r["method"], "what": what(r["items"])} for r in sales],
                "pending": [{"t": a["start"][11:16], "client": a["client"], "service": a["service"]}
                            for a in appointments_on(today) if a["status"] in ("booked", "confirmed")
                            and a["start"] <= datetime.now().isoformat()]}
    if kind == "stock":
        rows = q("""SELECT p.*, s.name supplier FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id
                    WHERE p.active=1 ORDER BY (p.stock<=p.min_stock) DESC, p.category, p.name""")
        items = [{"name": f"{r['brand']} {r['name']}".strip(), "category": r["category"] or "", "stock": r["stock"],
                  "min": r["min_stock"], "unit": r["unit"] or "", "low": r["stock"] <= r["min_stock"],
                  "supplier": r["supplier"] or "", "need": max(r["min_stock"] * 2 - r["stock"], 1)} for r in rows]
        return {"ok": True, "kind": kind, "items": items, "low": sum(i["low"] for i in items),
                "value": round(sum(r["stock"] * (r["cost"] or 0) for r in rows), 2)}
    return {"ok": False, "error": "unknown panel"}


# ───────────────────────── automations ─────────────────────────
_auto_started = False


def _auto_loop():
    from actions.addons_notify import alert
    done = {}
    while True:
        try:
            now = datetime.now()
            key = now.date().isoformat()
            if now.hour >= 8 and done.get("morning") != key:
                done["morning"] = key
                backup()
                s = today_summary()
                msg = f"Buenos días. Hoy hay {len(s['appointments'])} cita(s)."
                if s["next"]:
                    msg += f" La primera: {s['next']['start'][11:16]} {s['next']['client']} ({s['next']['service']})."
                if s["birthdays"]:
                    msg += " Hoy cumple(n) años: " + ", ".join(s["birthdays"]) + "."
                if s["low_stock"]:
                    msg += f" Hay {len(s['low_stock'])} producto(s) con stock bajo."
                alert(msg, key="salon-morning-" + key, cooldown_s=20 * 3600)
            if now.hour >= 19 and done.get("evening") != key:
                done["evening"] = key
                tomorrow = now.date() + timedelta(days=1)
                n = len(appointments_on(tomorrow))
                alert(report("today") + f" Mañana hay {n} cita(s).", key="salon-evening-" + key, cooldown_s=20 * 3600)
            if now.weekday() == 0 and now.hour >= 10 and done.get("weekly") != key:
                done["weekly"] = key
                cutoff = (now.date() - timedelta(days=60)).isoformat()
                lost = q("SELECT name FROM clients WHERE last_visit!='' AND last_visit<? ORDER BY last_visit LIMIT 10", (cutoff,))
                if lost:
                    alert("Clientes que no vienen desde hace más de 2 meses: " + ", ".join(c["name"] for c in lost)
                          + ". ¿Les mando un mensaje?", key="salon-lost-" + key, cooldown_s=6 * 86400)
        except Exception as e:
            print(f"[Lumi] Salon automations: {e}")
        time.sleep(300)


def start():
    global _auto_started
    if _auto_started:
        return
    _auto_started = True
    db()
    threading.Thread(target=_auto_loop, daemon=True, name="salon-auto").start()


# ───────────────────────── voice tool ─────────────────────────
def salon_tool(parameters: dict, player=None, speak=None) -> str:
    a = parameters or {}
    act = (a.get("action") or "today").lower()
    try:
        if act in ("today", "agenda"):
            return agenda_text(parse_day(a.get("date")), a.get("staff", ""))
        if act == "book":
            return book(a.get("client", ""), a.get("service", ""), a.get("when", ""), a.get("staff", ""),
                        a.get("phone", ""), a.get("notes", ""))
        if act == "free_slots":
            svc = find_service(a.get("service", "")) or {"name": "30 min", "duration_min": 30}
            st = find_staff(a.get("staff", "")) if a.get("staff") else None
            return _slots_text(parse_day(a.get("date")), svc, st["id"] if st else None)
        if act == "cancel":
            return cancel(a.get("client", ""), a.get("when", ""), a.get("id"))
        if act == "noshow":
            return cancel(a.get("client", ""), a.get("when", ""), a.get("id"), noshow=True)
        if act == "move":
            return move(a.get("client", ""), a.get("when", ""), a.get("id"), a.get("staff", ""))
        if act == "client":
            c = find_client(a.get("client", "") or a.get("phone", ""))
            return client_card(c) if c else f"No tengo a «{a.get('client')}» como cliente."
        if act in ("add_client", "update_client"):
            fields = {k: a.get(k) for k in CLIENT_FIELDS if a.get(k) not in (None, "") and k != "name"}
            c = find_client(a.get("client", "")) if act == "update_client" else None
            if c:
                update_client(c["id"], **fields)
                return f"Ficha de {c['name']} actualizada."
            c = add_client(a.get("client", "") or "Cliente", **fields)
            return f"Cliente {c['name']} guardado."
        if act == "add_staff":
            s = add_staff(a.get("staff", ""), a.get("role", ""), a.get("commission_pct"), a.get("phone", ""))
            return f"{s['name']} añadido/a al equipo."
        if act == "set_service":
            s = set_service(a.get("service", ""), a.get("duration_min"), a.get("price"), a.get("category", ""))
            return f"Servicio {s['name']}: {s['duration_min']} min, {s['price']:.2f} €."
        if act == "services":
            return "Servicios: " + "; ".join(f"{s['name']} {s['duration_min']} min {s['price']:.0f} €"
                                             for s in q("SELECT * FROM services WHERE active=1 ORDER BY category, name"))
        if act == "add_product":
            p = add_product(a.get("product", ""), a.get("brand", ""), a.get("category", ""), a.get("qty") or 0,
                            a.get("min_stock") or 0, a.get("cost") or 0, a.get("price") or 0,
                            bool(a.get("retail")), a.get("unit") or "ud", a.get("supplier", ""), a.get("barcode", ""))
            return f"Producto {p['brand']} {p['name']} en inventario (stock {p['stock']:g}).".replace("  ", " ")
        if act in ("stock_in", "stock_use"):
            p = find_product(a.get("product", ""))
            if not p:
                return f"No encuentro «{a.get('product')}» en el inventario."
            qty = float(a.get("qty") or 1) * (1 if act == "stock_in" else -1)
            p = move_stock(p, qty, "entrada" if qty > 0 else "uso")
            return f"{p['brand']} {p['name']}: ahora quedan {p['stock']:g} {p['unit']}.".replace("  ", " ") + \
                (" ⚠ Por debajo del mínimo." if p["stock"] <= p["min_stock"] else "")
        if act == "inventory":
            return inventory_text(a.get("product", ""))
        if act == "low_stock":
            return order_list()
        if act == "link_product":
            s, p = find_service(a.get("service", "")), find_product(a.get("product", ""))
            if not (s and p):
                return "No encuentro ese servicio o producto."
            link_product_to_service(s, p, float(a.get("qty") or 1))
            return f"Cada {s['name']} gastará {float(a.get('qty') or 1):g} {p['unit']} de {p['name']}."
        if act == "charge":
            return charge(a.get("client", ""), a.get("amount"), a.get("method", "efectivo"), a.get("tip") or 0,
                          a.get("product", ""), a.get("staff", ""), a.get("id"))
        if act == "report":
            return report((a.get("period") or "today").lower())
        if act == "settings":
            if a.get("salon_name"):
                set_setting("salon_name", a["salon_name"])
            if a.get("hours"):
                hrs = get_setting("hours", DEFAULT_HOURS)
                for part in str(a["hours"]).split(";"):
                    m = re.match(r"\s*(\d)\s*=\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", part)
                    if m:
                        hrs[m.group(1)] = [m.group(2).zfill(5), m.group(3).zfill(5)]
                    m2 = re.match(r"\s*(\d)\s*=\s*(cerrado|closed)", part, re.I)
                    if m2:
                        hrs.pop(m2.group(1), None)
                set_setting("hours", hrs)
            return "Ajustes del salón guardados."
        if act == "open_panel":
            from actions import addons
            return "Panel del salón abierto." if addons.open_page("salon") else "No pude abrir el panel."
        return f"Acción desconocida: {act}"
    except Exception as e:
        return f"Error en la gestión del salón: {e}"


TOOL_DECLARATION = {
    "name": "salon",
    "description": (
        "Hair salon management (Lumi's business system). Use it for EVERYTHING about the salon: appointments "
        "(book, free slots, move, cancel, no-show, today's/any day's agenda), clients (card with history, allergies, "
        "colour formula, preferences, notes, birthday, consent), staff, services and prices, inventory (add products, "
        "stock in/use, low stock & order list, which products each service uses), charging (cash/card/bizum, tips, "
        "retail products) and reports (day/week/month cash, by staff with commissions, top services). "
        "For 'when' always send an exact local date and time 'YYYY-MM-DD HH:MM' (resolve 'mañana a las 5' yourself). "
        "action: today | book | free_slots | move | cancel | noshow | client | add_client | update_client | add_staff | "
        "set_service | services | add_product | stock_in | stock_use | inventory | low_stock | link_product | charge | "
        "report | settings | open_panel."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "See the list in the description"},
            "client": {"type": "STRING", "description": "Client name (or part of it)"},
            "phone": {"type": "STRING", "description": "Client or staff phone"},
            "service": {"type": "STRING", "description": "Service name, e.g. corte mujer, tinte, mechas"},
            "staff": {"type": "STRING", "description": "Hairdresser / staff name"},
            "when": {"type": "STRING", "description": "Exact date and time YYYY-MM-DD HH:MM"},
            "date": {"type": "STRING", "description": "Day YYYY-MM-DD (or hoy / mañana) for agenda and free slots"},
            "id": {"type": "INTEGER", "description": "Appointment id if known"},
            "notes": {"type": "STRING", "description": "Notes for the appointment or client"},
            "allergies": {"type": "STRING", "description": "Client allergies"},
            "color_formula": {"type": "STRING", "description": "Client colour formula (e.g. 7.1 + 6% 1:1.5)"},
            "preferences": {"type": "STRING", "description": "Client preferences"},
            "birthday": {"type": "STRING", "description": "Client birthday YYYY-MM-DD"},
            "email": {"type": "STRING", "description": "Client email"},
            "consent_photos": {"type": "INTEGER", "description": "1 if the client agreed to photos"},
            "consent_marketing": {"type": "INTEGER", "description": "1 if the client agreed to receive messages"},
            "role": {"type": "STRING", "description": "Staff role"},
            "commission_pct": {"type": "NUMBER", "description": "Staff commission %"},
            "duration_min": {"type": "INTEGER", "description": "Service duration in minutes"},
            "price": {"type": "NUMBER", "description": "Price (service or product sale price)"},
            "category": {"type": "STRING", "description": "Category"},
            "product": {"type": "STRING", "description": "Product name (with brand if known); several separated by commas when charging"},
            "brand": {"type": "STRING", "description": "Product brand"},
            "qty": {"type": "NUMBER", "description": "Quantity"},
            "unit": {"type": "STRING", "description": "Unit: ud, ml, g, tubo..."},
            "min_stock": {"type": "NUMBER", "description": "Minimum stock before ordering"},
            "cost": {"type": "NUMBER", "description": "Product cost price"},
            "retail": {"type": "BOOLEAN", "description": "True if the product is sold to clients"},
            "supplier": {"type": "STRING", "description": "Supplier name"},
            "barcode": {"type": "STRING", "description": "Product barcode"},
            "amount": {"type": "NUMBER", "description": "Amount to charge (if different from the service price)"},
            "method": {"type": "STRING", "description": "efectivo | tarjeta | bizum | transferencia"},
            "tip": {"type": "NUMBER", "description": "Tip"},
            "period": {"type": "STRING", "description": "today | yesterday | week | month"},
            "salon_name": {"type": "STRING", "description": "Salon name (settings)"},
            "hours": {"type": "STRING", "description": "Opening hours, e.g. '0=09:30-20:00;5=09:00-14:00;6=cerrado' (0=Monday)"},
        },
        "required": ["action"],
    },
}
