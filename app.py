import sqlite3
import statistics
import requests
import os
import urllib.parse
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, flash, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "super_tajny_klucz_kierownika_v5_4_final"
DB_NAME = "database.db"
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ['pdf', 'jpg', 'png', 'zip']

def db(): return sqlite3.connect(DB_NAME, timeout=10)

def safe_float(value):
    if not value: return 0.0
    try: return float(str(value).replace(',', '.').strip())
    except: return 0.0

def init_db():
    con = db(); cur = con.cursor()
    
    default_mail = """Dzień dobry,

Zapraszamy do składania ofert w giełdzie: {GIEŁDA}.

Szczegóły:
Termin składania ofert: {DATA}
{WARUNKI_LOGISTYCZNE}

Link do portalu: http://twoj-adres-serwera:10000

Pozdrawiamy,
Dział Logistyki"""
    
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT UNIQUE, value TEXT)")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('mail_template', default_mail))

    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, is_active INTEGER DEFAULT 1, category TEXT DEFAULT 'Spedycja')")
    
    # EXCHANGES: Dodano customs_code_global
    cur.execute("""CREATE TABLE IF NOT EXISTS exchanges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, category TEXT, deadline DATETIME, 
        is_locked INTEGER DEFAULT 0, currency TEXT DEFAULT 'PLN', eur_rate REAL DEFAULT 1.0, 
        usd_rate REAL DEFAULT 1.0, admin_file1 TEXT, admin_file2 TEXT, description TEXT,
        notify_enabled INTEGER DEFAULT 0,
        is_archived INTEGER DEFAULT 0, archive_folder TEXT DEFAULT '',
        incoterms TEXT, port_loading TEXT, pickup_date TEXT,
        customs_code_global TEXT)""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT, exchange_id INTEGER, name TEXT, 
        net_weight REAL, gross_weight REAL, volume REAL, quantity INTEGER, 
        kg_per_m REAL, length_m REAL,
        hs_code TEXT, item_admin_file TEXT,
        customs_code_18 TEXT)""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, material_id INTEGER, 
        price REAL, currency TEXT DEFAULT 'PLN', 
        user_file1 TEXT, user_file2 TEXT, user_file3 TEXT, 
        substitute_note TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    
    cur.execute("""CREATE TABLE IF NOT EXISTS shipping_bids (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, exchange_id INTEGER,
        val_pln REAL DEFAULT 0, val_eur REAL DEFAULT 0, val_usd REAL DEFAULT 0,
        total_usd_calc REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")

    admin_pw = generate_password_hash("admin")
    cur.execute("INSERT OR IGNORE INTO users (username, password, is_active, category) VALUES ('admin', ?, 1, 'ADMIN')", (admin_pw,))
    con.commit(); con.close()

init_db()

# --- HELPERY ---
def get_setting(key):
    con = db(); cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else ""

def is_exchange_open(ex_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT deadline, is_locked, is_archived FROM exchanges WHERE id=?", (ex_id,))
    row = cur.fetchone()
    con.close()
    if not row: return False
    if row[2] == 1: return False 
    if row[1] == 1: return False 
    if row[0] and datetime.now().strftime('%Y-%m-%dT%H:%M') > row[0]: return False
    return True

def get_live_rate(code):
    try:
        res = requests.get(f"http://api.nbp.pl/api/exchangerates/rates/a/{code}/?format=json", timeout=1)
        return round(res.json()['rates'][0]['mid'], 4)
    except: return 4.30 if code == 'EUR' else 4.00

# --- AUTH ---
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u, p = request.form["username"], request.form["password"]
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", (u,))
        row = cur.fetchone()
        con.close()
        
        if row and check_password_hash(row[2], p):
            if row[3] == 0: flash("Konto nieaktywne."); return render_template("login.html")
            session["user"] = u
            session["category"] = row[4]
            if u == "admin": return redirect("/admin")
            return redirect("/user")
        flash("Błędny login lub hasło")
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

# --- USER VIEW ---
@app.route("/user")
def user():
    if "user" not in session or session["user"] == "admin": return redirect("/")
    con = db(); cur = con.cursor()
    user_cat = session.get("category", "Spedycja")
    
    cur.execute("SELECT * FROM exchanges WHERE is_archived=0 AND category=? ORDER BY deadline DESC", (user_cat,))
    all_ex = cur.fetchall()
    exchange_data = []
    
    for ex in all_ex:
        if not is_exchange_open(ex[0]): continue

        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        materials = []
        
        shipping_bid, shipping_rank = None, "-"
        
        if ex[2] == 'Spedycja':
            cur.execute("SELECT val_pln, val_eur, val_usd, total_usd_calc FROM shipping_bids WHERE exchange_id=? AND user=? ORDER BY id DESC LIMIT 1", (ex[0], session['user']))
            shipping_bid = cur.fetchone()
            if shipping_bid:
                my_total = shipping_bid[3]
                cur.execute("SELECT total_usd_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id DESC", (ex[0],))
                cur.execute("SELECT user, total_usd_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id ASC", (ex[0],))
                hist = cur.fetchall()
                latest = {}
                for u, v in hist: latest[u] = v
                sorted_vals = sorted(latest.values())
                try: shipping_rank = sorted_vals.index(my_total) + 1
                except: pass

        for m in mats_raw:
            saved_price = None
            item_rank = "-"
            if ex[2] != 'Spedycja':
                cur.execute("SELECT price, currency, user_file1, user_file2, user_file3, substitute_note FROM prices WHERE material_id=? AND user=? ORDER BY id DESC LIMIT 1", (m[0], session["user"]))
                saved_price = cur.fetchone()
                if saved_price:
                    cur.execute("SELECT user, price FROM prices WHERE material_id=? ORDER BY id ASC", (m[0],))
                    p_hist = cur.fetchall()
                    p_latest = {}
                    for u, p in p_hist: p_latest[u] = p
                    sorted_p = sorted(p_latest.values())
                    try: item_rank = sorted_p.index(saved_price[0]) + 1
                    except: pass

            materials.append({
                "id": m[0], "name": m[2], "net": m[3], "gross": m[4], "vol": m[5], 
                "qty": m[6], "kg_m": m[7], "len": m[8], "hs": m[9], "admin_file": m[10],
                "saved": saved_price, "rank": item_rank
            })
        
        exchange_data.append({
            "id": ex[0], "name": ex[1], "cat": ex[2], "deadline": ex[3],
            "eur": ex[6], "usd": ex[7], "desc": ex[10],
            "incoterms": ex[14], "port": ex[15], "pickup": ex[16],
            "f1": ex[8], "f2": ex[9],
            "mats": materials, "is_open": True,
            "shipping_bid": shipping_bid, "shipping_rank": shipping_rank
        })
    con.close()
    return render_template("user.html", exchange_data=exchange_data)

@app.route("/save_offer/<int:eid>", methods=["POST"])
def save_offer(eid):
    if "user" not in session: return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("SELECT category, eur_rate, usd_rate FROM exchanges WHERE id=?", (eid,))
    row = cur.fetchone()
    cat, rate_e, rate_u = row[0], row[1], row[2]

    if not is_exchange_open(eid): con.close(); return "Zamknięte", 403

    if cat == 'Spedycja':
        pln = safe_float(request.form.get("sp_pln"))
        eur = safe_float(request.form.get("sp_eur"))
        usd = safe_float(request.form.get("sp_usd"))
        val_in_pln = pln + (eur * rate_e) + (usd * rate_u)
        total_usd = val_in_pln / rate_u if rate_u > 0 else 0
        cur.execute("INSERT INTO shipping_bids (user, exchange_id, val_pln, val_eur, val_usd, total_usd_calc) VALUES (?,?,?,?,?,?)",
                   (session['user'], eid, pln, eur, usd, round(total_usd, 2)))
    else:
        cur.execute("SELECT id FROM materials WHERE exchange_id=?", (eid,))
        mids = [r[0] for r in cur.fetchall()]
        for mid in mids:
            price_key = f"price_{mid}"
            if price_key in request.form:
                val = safe_float(request.form[price_key])
                sub_note = request.form.get(f"sub_note_{mid}", "")
                cur.execute("INSERT INTO prices (user, material_id, price, currency, substitute_note) VALUES (?,?,?,?,?)", 
                           (session['user'], mid, val, "PLN", sub_note))
    con.commit(); con.close()
    return redirect("/user")

# --- ADMIN ROUTES ---

@app.route("/manage_user", methods=["POST"])
def manage_user():
    if session.get("user") != "admin": return redirect("/")
    action = request.form.get("action")
    con = db(); cur = con.cursor()
    if action == "add":
        try:
            pw = generate_password_hash(request.form["password"])
            cur.execute("INSERT INTO users (username, password, is_active, category) VALUES (?,?,?,?)", 
                       (request.form["username"], pw, 1, request.form["category"]))
        except: pass
    elif action == "edit":
        uid = request.form.get("uid")
        new_u = request.form.get("username")
        new_p = request.form.get("password")
        if new_u: cur.execute("UPDATE users SET username=? WHERE id=?", (new_u, uid))
        if new_p: cur.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new_p), uid))
    elif action == "delete": cur.execute("DELETE FROM users WHERE id=?", (request.form.get("uid"),))
    elif action == "toggle": cur.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (request.form.get("uid"),))
    con.commit(); con.close()
    return redirect(f"/admin?view=users&user_cat={request.form.get('category_filter')}&status_tab={request.form.get('status_tab')}")

@app.route("/settings", methods=["POST"])
def save_settings():
    if session.get("user") != "admin": return redirect("/")
    txt = request.form.get("mail_template")
    con = db(); cur = con.cursor()
    cur.execute("UPDATE settings SET value=? WHERE key='mail_template'", (txt,))
    con.commit(); con.close()
    return redirect("/admin?view=settings")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    
    sel_id = request.args.get('exchange_id', type=int)
    view = request.args.get('view', 'dashboard') 
    archive_folder = request.args.get('folder', 'Styczeń')
    user_cat_tab = request.args.get('user_cat', 'Spedycja')
    status_tab = request.args.get('status_tab', 'active')
    ex_cat_tab = request.args.get('ex_cat', 'Spedycja')

    if request.method == "POST":
        form_type = request.form.get("form_type")
        
        if form_type == "create_exchange":
            af_names = ["", ""]
            cat = request.form["category"]
            if cat == 'Spedycja':
                for i, f in enumerate([request.files.get('af1'), request.files.get('af2')]):
                    if f and allowed_file(f.filename):
                        fn = secure_filename(f"spedycja_{i}_{f.filename}")
                        f.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                        af_names[i] = fn
            e_r = safe_float(request.form.get("eur_rate", "4.30"))
            u_r = safe_float(request.form.get("usd_rate", "4.00"))
            notify = 1 if request.form.get("notify_enabled") else 0
            
            inco = request.form.get("incoterms") if cat == 'Spedycja' else ""
            port = request.form.get("port_loading") if cat == 'Spedycja' else ""
            pick = request.form.get("pickup_date") if cat == 'Spedycja' else ""

            cur.execute("""INSERT INTO exchanges (name, category, deadline, currency, eur_rate, usd_rate, admin_file1, admin_file2, description, notify_enabled, incoterms, port_loading, pickup_date) 
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", 
                       (request.form["exchange_name"], cat, request.form["deadline"], "PLN", e_r, u_r, af_names[0], af_names[1], request.form.get("desc",""), notify, inco, port, pick))
            con.commit()
            return redirect(f"/admin?view=open&ex_cat={cat}")

        elif form_type == "add_item":
            eid = request.form["exchange_id"]
            cat_redirect = request.form.get("category_redirect")
            
            qty = safe_float(request.form.get("qty"))
            kg_m = safe_float(request.form.get("kg_m"))
            length = safe_float(request.form.get("len"))
            net_w = safe_float(request.form.get("net"))
            
            # AUTOMAT DLA MATERIAŁU
            if kg_m > 0 and length > 0 and qty > 0:
                net_w = round(qty * length * kg_m, 2)
            
            item_file = ""
            if "item_file" in request.files:
                f = request.files["item_file"]
                if f and allowed_file(f.filename):
                    item_file = secure_filename(f"item_{eid}_{f.filename}")
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], item_file))
            
            cur.execute("""INSERT INTO materials (exchange_id, name, net_weight, gross_weight, volume, quantity, kg_per_m, length_m, hs_code, item_admin_file) 
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (eid, request.form.get("name"), net_w, request.form.get("gross",0), request.form.get("vol",0), int(qty), kg_m, length, request.form.get("hs",""), item_file))
            con.commit()
            return redirect(f"/admin?view={view}&exchange_id={eid}&ex_cat={cat_redirect}")

        elif form_type == "edit_exchange_details":
            eid = request.form["eid"]
            cat_redirect = request.form.get("category_redirect")
            
            # Globalny kod celny i inne
            cur.execute("UPDATE exchanges SET description=?, incoterms=?, port_loading=?, pickup_date=?, customs_code_global=? WHERE id=?", 
                       (request.form.get("desc"), request.form.get("incoterms"), request.form.get("port"), request.form.get("pickup"), request.form.get("customs_code_global"), eid))
            
            m_ids = request.form.getlist("m_id")
            for mid in m_ids:
                # Logika edycji zależna od kategorii
                if cat_redirect == 'Spedycja':
                    net = request.form.get(f"net_{mid}")
                    gross = request.form.get(f"gross_{mid}")
                    vol = request.form.get(f"vol_{mid}")
                    hs = request.form.get(f"hs_{mid}")
                    cur.execute("UPDATE materials SET net_weight=?, gross_weight=?, volume=?, hs_code=? WHERE id=?", (net, gross, vol, hs, mid))
                elif cat_redirect == 'Material':
                    qty = safe_float(request.form.get(f"qty_{mid}"))
                    laga = safe_float(request.form.get(f"len_{mid}"))
                    kgm = safe_float(request.form.get(f"kgm_{mid}"))
                    # Przelicz wagę
                    net = round(qty * laga * kgm, 2)
                    cur.execute("UPDATE materials SET quantity=?, length_m=?, kg_per_m=?, net_weight=? WHERE id=?", (qty, laga, kgm, net, mid))
                else: # Wycena
                    qty = request.form.get(f"qty_{mid}")
                    cur.execute("UPDATE materials SET quantity=? WHERE id=?", (qty, mid))

            con.commit()
            return redirect(f"/admin?view={view}&exchange_id={eid}&ex_cat={cat_redirect}")

        elif form_type == "archive_exchange":
            eid = request.form["eid"]
            # Sprawdź czy jest wpisany GLOBALNY kod celny (18 znaków)
            cur.execute("SELECT customs_code_global FROM exchanges WHERE id=?", (eid,))
            row = cur.fetchone()
            code = row[0] if row else ""
            
            if not code or len(code.strip()) != 18:
                flash("BŁĄD: Aby zarchiwizować, musisz wpisać poprawny Kod Odprawy Celnej (18 znaków) w edycji giełdy.")
                return redirect(f"/admin?view={view}&exchange_id={eid}&ex_cat={request.form.get('category_redirect')}")
            
            cur.execute("UPDATE exchanges SET is_archived=1, archive_folder=? WHERE id=?", (request.form["folder_name"], eid))
            con.commit(); return redirect(f"/admin?view=archive&ex_cat={request.form.get('category_redirect')}")

        elif form_type == "delete_exchange":
            eid = request.form["eid"]
            cur.execute("DELETE FROM exchanges WHERE id=?", (eid,))
            cur.execute("DELETE FROM materials WHERE exchange_id=?", (eid,))
            cur.execute("DELETE FROM shipping_bids WHERE exchange_id=?", (eid,))
            con.commit(); return redirect(f"/admin?view={view}&ex_cat={request.form.get('category_redirect')}")

    # --- DANE ---
    cur.execute("SELECT id, username, is_active, category FROM users WHERE username != 'admin' ORDER BY username")
    all_users_list = cur.fetchall()

    cur.execute("SELECT * FROM exchanges ORDER BY deadline DESC")
    all_ex = cur.fetchall()
    
    open_ex = [e for e in all_ex if is_exchange_open(e[0]) and e[12] == 0]
    closed_ex = [e for e in all_ex if not is_exchange_open(e[0]) and e[12] == 0]
    
    archived_raw = [e for e in all_ex if e[12] == 1]
    archive_folders = {}
    for e in archived_raw:
        f_name = e[13] if e[13] else "Inne"
        if f_name not in archive_folders: archive_folders[f_name] = []
        archive_folders[f_name].append(e)
    
    raw_list = open_ex if view == 'open' else (closed_ex if view == 'closed' else archive_folders.get(archive_folder, []))
    target_list = [e for e in raw_list if e[2] == ex_cat_tab] if view in ['open', 'closed', 'archive'] else raw_list
    process_ex = [e for e in target_list if e[0] == sel_id] if sel_id else []

    ex_details = []
    for ex in process_ex:
        cat = ex[2]
        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        
        # --- MAILTO GENERATION ---
        cur.execute("SELECT username FROM users WHERE category=? AND is_active=1", (cat,))
        emails = [u[0] for u in cur.fetchall()]
        email_str = ";".join(emails)
        
        template = get_setting("mail_template")
        deadline_str = ex[3].replace("T", " ")
        body = template.replace("{GIEŁDA}", ex[1]).replace("{DATA}", deadline_str)
        logistics = ""
        if cat == 'Spedycja':
            logistics = f"Warunki: {ex[14] or '-'}\nPort: {ex[15] or '-'}\nGotowość: {ex[16] or '-'}"
        body = body.replace("{WARUNKI_LOGISTYCZNE}", logistics)
        subject = f"Zaproszenie do giełdy: {ex[1]}"
        mailto_link = f"mailto:?bcc={email_str}&subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"
        # -------------------------

        shipping_stats = []
        if cat == 'Spedycja':
            cur.execute("SELECT user, val_pln, val_eur, val_usd, total_usd_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id ASC", (ex[0],))
            rows = cur.fetchall()
            u_hist = {}
            for r in rows:
                if r[0] not in u_hist: u_hist[r[0]] = []
                u_hist[r[0]].append(r)
            for u, h in u_hist.items():
                start = h[0][4]
                curr = h[-1][4]
                drop = round((start - curr)/start*100, 1) if start>0 else 0
                shipping_stats.append({'user': u, 'desc': f"USD:{h[-1][3]}", 'start': start, 'curr': curr, 'drop': drop})
            shipping_stats.sort(key=lambda x: x['curr'])
            if shipping_stats:
                min_v = shipping_stats[0]['curr']
                remis = [s['curr'] for s in shipping_stats].count(min_v) > 1
                for s in shipping_stats:
                    s['is_best'] = (s['curr'] == min_v)
                    s['label'] = "REMIS" if (s['is_best'] and remis) else ("L1" if s['is_best'] else "")

        mats_with_offers = []
        for m in mats_raw:
            cur.execute("SELECT user, price, substitute_note FROM prices WHERE material_id=? ORDER BY id ASC", (m[0],))
            p_hist = cur.fetchall()
            u_hist = {}
            for r in p_hist:
                if r[0] not in u_hist: u_hist[r[0]] = []
                u_hist[r[0]].append(r)
            offers = []
            for u, h in u_hist.items():
                start = h[0][1]
                curr = h[-1][1]
                drop = round((start - curr)/start*100, 1) if start>0 else 0
                offers.append({'user': u, 'start': start, 'curr': curr, 'drop': drop, 'sub': h[-1][2]})
            offers.sort(key=lambda x: x['curr'])
            if offers:
                min_p = offers[0]['curr']
                remis = [o['curr'] for o in offers].count(min_p) > 1
                for o in offers:
                    o['is_best'] = (o['curr'] == min_p)
                    o['label'] = "REMIS" if (o['is_best'] and remis) else ("L1" if o['is_best'] else "")
            mats_with_offers.append({'data': m, 'offers': offers})

        ex_details.append({'info': ex, 'mats_offers': mats_with_offers, 'shipping_stats': shipping_stats, 'mailto': mailto_link})

    email_template = get_setting("mail_template")
    l_eur, l_usd = get_live_rate('EUR'), get_live_rate('USD')
    con.close()
    
    return render_template("admin.html", 
                         ex_details=ex_details, all_users_list=all_users_list,
                         open_ex=open_ex, closed_ex=closed_ex, archive_folders=archive_folders,
                         sel_id=sel_id, view=view, current_folder=archive_folder,
                         live_eur=l_eur, live_usd=l_usd, email_template=email_template,
                         user_cat_tab=user_cat_tab, status_tab=status_tab, ex_cat_tab=ex_cat_tab)

@app.route("/toggle_lock/<int:eid>")
def toggle_lock(eid):
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("UPDATE exchanges SET is_locked = 1 - is_locked WHERE id=?", (eid,))
    con.commit(); con.close()
    return redirect(request.referrer)

@app.route("/delete_material/<int:mid>")
def delete_material(mid):
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM materials WHERE id=?", (mid,))
    con.commit(); con.close()
    return redirect(request.referrer)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
