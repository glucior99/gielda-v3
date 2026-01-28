import sqlite3
import statistics
import requests
import os
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, flash, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "super_tajny_klucz_kierownika_v5_0_necks"
DB_NAME = "database.db"
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- KONFIGURACJA EMAIL ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_EMAIL = "twoj_email@gmail.com"
SMTP_PASSWORD = "twoje_haslo_aplikacji"
# --------------------------

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
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT UNIQUE, value TEXT)")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mail_template', 'Giełda {GIEŁDA} została zaktualizowana. Termin: {DATA}.')")

    # Users
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, is_active INTEGER DEFAULT 1, category TEXT DEFAULT 'Spedycja')")
    
    # Exchanges (admin_file1/2 - pliki globalne dla spedycji)
    cur.execute("""CREATE TABLE IF NOT EXISTS exchanges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, category TEXT, deadline DATETIME, 
        is_locked INTEGER DEFAULT 0, currency TEXT DEFAULT 'PLN', eur_rate REAL DEFAULT 1.0, 
        usd_rate REAL DEFAULT 1.0, admin_file1 TEXT, admin_file2 TEXT, description TEXT,
        notify_enabled INTEGER DEFAULT 0,
        is_archived INTEGER DEFAULT 0,
        archive_folder TEXT DEFAULT '')""")
    
    # Materials
    cur.execute("""CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT, exchange_id INTEGER, name TEXT, 
        net_weight REAL, gross_weight REAL, volume REAL, quantity INTEGER, 
        kg_per_m REAL, length_m REAL,
        hs_code TEXT, item_admin_file TEXT,
        customs_code_18 TEXT)""")
    
    # Prices
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, material_id INTEGER, 
        price REAL, currency TEXT DEFAULT 'PLN', 
        user_file1 TEXT, user_file2 TEXT, user_file3 TEXT, 
        substitute_note TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    
    # Shipping Bids
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

def get_live_rate(code):
    try:
        res = requests.get(f"http://api.nbp.pl/api/exchangerates/rates/a/{code}/?format=json", timeout=1)
        return round(res.json()['rates'][0]['mid'], 4)
    except: return 4.30 if code == 'EUR' else 4.00

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

# --- LOGOWANIE ---
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u, p = request.form["username"], request.form["password"]
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM users WHERE username=?", (u,))
        row = cur.fetchone()
        con.close()
        
        if row and check_password_hash(row[2], p):
            if row[3] == 0: 
                flash("Konto nieaktywne."); return render_template("login.html")
            session["user"] = u
            session["category"] = row[4]
            if u == "admin": return redirect("/admin")
            return redirect("/user")
        flash("Błędny login lub hasło")
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

# --- ZARZĄDZANIE USERAMI ---
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
        new_user = request.form.get("username")
        new_pass = request.form.get("password")
        
        try:
            if new_user:
                cur.execute("UPDATE users SET username=? WHERE id=?", (new_user, uid))
            if new_pass:
                pw = generate_password_hash(new_pass)
                cur.execute("UPDATE users SET password=? WHERE id=?", (pw, uid))
        except sqlite3.IntegrityError:
            pass # Login zajęty
            
    elif action == "delete":
        uid = request.form.get("uid")
        cur.execute("DELETE FROM users WHERE id=?", (uid,))
        
    elif action == "toggle":
        uid = request.form.get("uid")
        cur.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))

    con.commit(); con.close()
    return redirect("/admin?view=users&user_cat=" + request.form.get("category_filter", "Spedycja"))

# --- USER VIEW ---
@app.route("/user")
def user():
    if "user" not in session or session["user"] == "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM exchanges WHERE is_archived=0 ORDER BY deadline DESC")
    all_ex = cur.fetchall()
    exchange_data = []
    
    user_cat = session.get("category", "Spedycja")

    for ex in all_ex:
        if ex[2] != user_cat: continue
        if not is_exchange_open(ex[0]): continue

        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        materials = []
        
        shipping_bid = None
        shipping_rank = "-"
        
        if ex[2] == 'Spedycja':
            cur.execute("SELECT val_pln, val_eur, val_usd, total_usd_calc FROM shipping_bids WHERE exchange_id=? AND user=? ORDER BY id DESC LIMIT 1", (ex[0], session['user']))
            shipping_bid = cur.fetchone()
            
            if shipping_bid:
                my_total = shipping_bid[3]
                cur.execute("SELECT total_usd_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id DESC", (ex[0],))
                all_raw = cur.fetchall()
                # Ranking (uproszczony dla USD)
                # ...
                
        for m in mats_raw:
            saved_price = None
            if ex[2] != 'Spedycja':
                cur.execute("SELECT price, currency, user_file1, user_file2, user_file3, substitute_note FROM prices WHERE material_id=? AND user=? ORDER BY id DESC LIMIT 1", (m[0], session["user"]))
                saved_price = cur.fetchone()

            materials.append({
                "id": m[0], "name": m[2], 
                "net": m[3], "gross": m[4], "vol": m[5], "qty": m[6], 
                "kg_m": m[7], "len": m[8], "hs": m[9], "admin_file": m[10],
                "saved": saved_price,
            })
        
        exchange_data.append({
            "id": ex[0], "name": ex[1], "cat": ex[2], "deadline": ex[3],
            "eur": ex[6], "usd": ex[7], "desc": ex[10],
            "f1": ex[8], "f2": ex[9], # Globalne pliki
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
    ex_row = cur.fetchone()
    if not ex_row: con.close(); return "Błąd", 404
    cat, rate_e, rate_u = ex_row

    if not is_exchange_open(eid): 
        con.close(); return "Zamknięte", 403

    if cat == 'Spedycja':
        pln = safe_float(request.form.get("sp_pln"))
        eur = safe_float(request.form.get("sp_eur"))
        usd = safe_float(request.form.get("sp_usd"))
        # Przeliczamy wszystko na USD
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
                sub_check = request.form.get(f"sub_check_{mid}")
                sub_note = request.form.get(f"sub_note_{mid}", "") if sub_check else ""
                cur.execute("INSERT INTO prices (user, material_id, price, currency, substitute_note) VALUES (?,?,?,?,?)", 
                           (session['user'], mid, val, "PLN", sub_note))
    
    con.commit(); con.close()
    return redirect("/user")

# --- ADMIN VIEW ---
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
    user_cat_tab = request.args.get('user_cat', 'Spedycja') # Do zakładek userów

    # --- OBSŁUGA FORMULARZY ---
    if request.method == "POST":
        form_type = request.form.get("form_type")
        
        if form_type == "create_exchange":
            af_names = ["", ""]
            # Pliki tylko dla Spedycji lądują tutaj w admin_file
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
            
            cur.execute("""INSERT INTO exchanges (name, category, deadline, currency, eur_rate, usd_rate, admin_file1, admin_file2, description, notify_enabled) 
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", 
                       (request.form["exchange_name"], cat, request.form["deadline"], "PLN", e_r, u_r, af_names[0], af_names[1], request.form.get("desc",""), notify))
            con.commit()
            return redirect("/admin?view=open") # Przekieruj do otwartych

        elif form_type == "add_item":
            # Naprawa: Pobieramy exchange_id z formularza
            eid = request.form["exchange_id"]
            qty = safe_float(request.form.get("qty"))
            kg_m = safe_float(request.form.get("kg_m"))
            length = safe_float(request.form.get("len"))
            net_w = safe_float(request.form.get("net"))
            if qty > 0 and kg_m > 0 and length > 0: net_w = round(qty * length * kg_m, 2)
            
            item_file_name = ""
            # Plik pozycji tylko jeśli NIE Spedycja (bo spedycja ma globalne)
            # Ale w sumie baza pozwala, zostawmy możliwość, ale w formularzu html ukryjemy dla spedycji
            if "item_file" in request.files:
                f = request.files["item_file"]
                if f and allowed_file(f.filename):
                    item_file_name = secure_filename(f"item_{eid}_{f.filename}")
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], item_file_name))
            
            cur.execute("""INSERT INTO materials (exchange_id, name, net_weight, gross_weight, volume, quantity, kg_per_m, length_m, hs_code, item_admin_file) 
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (eid, request.form.get("name"), net_w, request.form.get("gross",0), request.form.get("vol",0), int(qty), kg_m, length, request.form.get("hs",""), item_file_name))
            con.commit()
            # Przekierowanie z zachowaniem ID i widoku
            return redirect(f"/admin?view=open&exchange_id={eid}")

        # ... (archiwizacja, usuwanie, edycja detali - bez zmian z poprzedniej wersji)
        elif form_type == "archive_exchange":
            eid = request.form["eid"]
            folder = request.form["folder_name"]
            cur.execute("UPDATE exchanges SET is_archived=1, archive_folder=? WHERE id=?", (folder, eid))
            con.commit()
            return redirect("/admin?view=archive")

        elif form_type == "delete_exchange":
            eid = request.form["eid"]
            cur.execute("DELETE FROM exchanges WHERE id=?", (eid,))
            cur.execute("DELETE FROM materials WHERE exchange_id=?", (eid,))
            cur.execute("DELETE FROM shipping_bids WHERE exchange_id=?", (eid,))
            con.commit()
            return redirect("/admin")
            
        elif form_type == "edit_exchange_details":
            eid = request.form["eid"]
            desc = request.form.get("desc")
            cur.execute("UPDATE exchanges SET description=? WHERE id=?", (desc, eid))
            m_ids = request.form.getlist("m_id")
            for mid in m_ids:
                net = request.form.get(f"net_{mid}")
                gross = request.form.get(f"gross_{mid}")
                vol = request.form.get(f"vol_{mid}")
                code18 = request.form.get(f"code18_{mid}")
                cur.execute("UPDATE materials SET net_weight=?, gross_weight=?, volume=?, customs_code_18=? WHERE id=?", 
                           (net, gross, vol, code18, mid))
            con.commit()
            return redirect(f"/admin?view=closed&exchange_id={eid}")

    # --- POBIERANIE DANYCH ---
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
    
    # Wybór giełdy do wyświetlenia
    target_list = []
    if view == 'open': target_list = open_ex
    elif view == 'closed': target_list = closed_ex
    elif view == 'archive': target_list = archive_folders.get(archive_folder, [])
    
    process_ex = [e for e in target_list if e[0] == sel_id] if sel_id else []

    ex_details = []
    for ex in process_ex:
        cat = ex[2]
        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        
        shipping_stats = []
        if cat == 'Spedycja':
            cur.execute("SELECT user, val_pln, val_eur, val_usd, total_usd_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id ASC", (ex[0],))
            history_rows = cur.fetchall()
            user_history = {}
            for row in history_rows:
                u, p, e, u_usd, total = row[0], row[1], row[2], row[3], row[4]
                if u not in user_history: user_history[u] = []
                user_history[u].append({'p':p, 'e':e, 'usd':u_usd, 'total':total})
            
            for u, h in user_history.items():
                start_total = h[0]['total']
                curr_total = h[-1]['total']
                drop_pct = round((start_total - curr_total) / start_total * 100, 1) if start_total > 0 else 0
                shipping_stats.append({
                    'user': u,
                    'desc': f"PLN:{h[-1]['p']} | EUR:{h[-1]['e']} | USD:{h[-1]['usd']}",
                    'start_total': start_total, 'curr_total': curr_total, 'drop_pct': drop_pct
                })
            shipping_stats.sort(key=lambda x: x['curr_total'])
            if shipping_stats:
                min_val = shipping_stats[0]['curr_total']
                is_remis = [s['curr_total'] for s in shipping_stats].count(min_val) > 1
                for s in shipping_stats:
                    s['is_best'] = (s['curr_total'] == min_val)
                    s['label'] = "REMIS" if (s['is_best'] and is_remis) else ("L1" if s['is_best'] else "")

        mats_with_offers = []
        for m in mats_raw:
            cur.execute("SELECT user, price, substitute_note FROM prices WHERE material_id=? ORDER BY id ASC", (m[0],))
            p_hist = cur.fetchall()
            u_hist = {}
            for row in p_hist:
                u, pr, sub = row[0], row[1], row[2]
                if u not in u_hist: u_hist[u] = []
                u_hist[u].append({'p': pr, 's': sub})
            
            offers = []
            for u, h in u_hist.items():
                start = h[0]['p']
                curr = h[-1]['p']
                drop = round((start - curr)/start*100, 1) if start>0 else 0
                offers.append({'user': u, 'start': start, 'curr': curr, 'drop': drop, 'sub': h[-1]['s']})
            offers.sort(key=lambda x: x['curr'])
            if offers:
                min_p = offers[0]['curr']
                remis = [o['curr'] for o in offers].count(min_p) > 1
                for o in offers:
                    o['is_best'] = (o['curr'] == min_p)
                    o['label'] = "REMIS" if (o['is_best'] and remis) else ("L1" if o['is_best'] else "")
            mats_with_offers.append({'data': m, 'offers': offers})

        ex_details.append({'info': ex, 'mats_offers': mats_with_offers, 'shipping_stats': shipping_stats})

    email_template = get_setting("mail_template")
    l_eur, l_usd = get_live_rate('EUR'), get_live_rate('USD')
    con.close()
    
    return render_template("admin.html", 
                         ex_details=ex_details, all_users_list=all_users_list,
                         open_ex=open_ex, closed_ex=closed_ex, archive_folders=archive_folders,
                         sel_id=sel_id, view=view, current_folder=archive_folder,
                         live_eur=l_eur, live_usd=l_usd, email_template=email_template,
                         user_cat_tab=user_cat_tab) # Przekazujemy wybraną zakładkę userów

@app.route("/toggle_lock/<int:eid>")
def toggle_lock(eid):
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("UPDATE exchanges SET is_locked = 1 - is_locked WHERE id=?", (eid,))
    con.commit(); con.close()
    return redirect(request.referrer)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
