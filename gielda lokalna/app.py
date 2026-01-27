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

app = Flask(__name__)
app.secret_key = "super_tajny_klucz_kierownika_v3_0_final"
DB_NAME = "database.db"
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- KONFIGURACJA EMAIL (UZUPEŁNIJ TO ABY DZIAŁAŁY AUTOMATY) ---
SMTP_SERVER = "smtp.gmail.com"      # Dla Gmaila. Dla Outlooka: smtp-mail.outlook.com
SMTP_PORT = 587
SMTP_EMAIL = "twoj_email@gmail.com" # <--- WPISZ SWÓJ EMAIL
SMTP_PASSWORD = "twoje_haslo_aplikacji" # <--- WPISZ HASŁO APLIKACJI
# ---------------------------------------------------------------

if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'

def db(): return sqlite3.connect(DB_NAME, timeout=10)

def safe_float(value):
    if not value: return 0.0
    try: return float(str(value).replace(',', '.').strip())
    except: return 0.0

def init_db():
    con = db(); cur = con.cursor()
    # USERS
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, is_active INTEGER DEFAULT 1, category TEXT DEFAULT 'Spedycja')")
    
    # EXCHANGES (z notify_enabled)
    cur.execute("""CREATE TABLE IF NOT EXISTS exchanges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, category TEXT, deadline DATETIME, 
        is_locked INTEGER DEFAULT 0, currency TEXT DEFAULT 'PLN', eur_rate REAL DEFAULT 1.0, 
        usd_rate REAL DEFAULT 1.0, admin_file1 TEXT, admin_file2 TEXT, description TEXT,
        notify_enabled INTEGER DEFAULT 0)""")
    
    # MATERIALS
    cur.execute("""CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT, exchange_id INTEGER, name TEXT, 
        net_weight REAL, gross_weight REAL, volume REAL, quantity INTEGER, 
        kg_per_m REAL, length_m REAL,
        hs_code TEXT, item_admin_file TEXT)""")
    
    # PRICES (Material/Wycena)
    cur.execute("""CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, material_id INTEGER, 
        price REAL, currency TEXT DEFAULT 'PLN', 
        user_file1 TEXT, user_file2 TEXT, user_file3 TEXT, 
        substitute_note TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    
    # SHIPPING BIDS (Spedycja)
    cur.execute("""CREATE TABLE IF NOT EXISTS shipping_bids (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, exchange_id INTEGER,
        val_pln REAL DEFAULT 0, val_eur REAL DEFAULT 0, val_usd REAL DEFAULT 0,
        total_pln_calc REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    
    # ADMIN USER
    cur.execute("INSERT OR IGNORE INTO users (username, password, is_active, category) VALUES ('admin', 'admin', 1, 'ADMIN')")
    con.commit(); con.close()

init_db()

# --- FUNKCJE POMOCNICZE (MAIL, RANKING, WALUTY) ---

def send_outbid_email(to_email, exchange_name, deadline, current_rank):
    if not to_email or "@" not in to_email: return 
    if "twoj_email" in SMTP_EMAIL: return # Zabezpieczenie przed wysyłką z domyślnych danych
    
    subject = f"Alert Giełdowy: Twoja oferta została przebita ({exchange_name})"
    body = f"""
    Witaj,
    
    Twoja oferta w giełdzie "{exchange_name}" została przebita przez innego dostawcę.
    
    Giełda jest otwarta do: {deadline.replace('T', ' ')}.
    Zapraszamy do poprawy oferty.
    
    Pozdrawiamy,
    Zespół Zakupów
    """
    
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = SMTP_EMAIL
        msg['To'] = to_email

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, [to_email], msg.as_string())
        server.quit()
        print(f"MAIL WYSŁANY DO: {to_email}")
    except Exception as e:
        print(f"BŁĄD WYSYŁANIA MAILA: {e}")

def get_current_leader_and_ranks(eid, category):
    con = db(); cur = con.cursor()
    user_totals = {}
    
    if category == 'Spedycja':
        # Dla spedycji bierzemy najnowsze oferty z shipping_bids
        cur.execute("SELECT user, total_pln_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id ASC", (eid,))
        rows = cur.fetchall()
        # Nadpisujemy w słowniku, więc zostanie ostatnia (najnowsza) oferta usera
        for u, val in rows:
            user_totals[u] = val
    else:
        # Dla Materiału/Wyceny musimy zsumować koszyk
        cur.execute("SELECT id FROM materials WHERE exchange_id=?", (eid,))
        mids = [r[0] for r in cur.fetchall()]
        if not mids: 
            con.close(); return None, {}
            
        temp_carts = {} 
        for mid in mids:
            cur.execute("SELECT user, price FROM prices WHERE material_id=? ORDER BY id ASC", (mid,))
            p_rows = cur.fetchall()
            mid_prices = {}
            for u, p in p_rows:
                mid_prices[u] = p # Najnowsza cena usera za ten materiał
            
            for u, p in mid_prices.items():
                temp_carts[u] = temp_carts.get(u, 0) + p
        user_totals = temp_carts

    con.close()
    
    if not user_totals: return None, {}
    
    # Sortujemy rosnąco (najniższa cena wygrywa)
    sorted_users = sorted(user_totals.items(), key=lambda x: x[1])
    
    leader = sorted_users[0][0]
    ranks = {}
    for i, (u, val) in enumerate(sorted_users):
        ranks[u] = i + 1
        
    return leader, ranks

def get_live_rate(code):
    try:
        res = requests.get(f"http://api.nbp.pl/api/exchangerates/rates/a/{code}/?format=json", timeout=1)
        return round(1 / res.json()['rates'][0]['mid'], 4)
    except: return 0.23 if code == 'EUR' else 0.25

def is_exchange_open(ex_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT deadline, is_locked FROM exchanges WHERE id=?", (ex_id,))
    row = cur.fetchone()
    con.close()
    if not row: return False
    if row[1] == 1: return False
    if row[0] and datetime.now().strftime('%Y-%m-%dT%H:%M') > row[0]: return False
    return True

# --- ROUTING: LOGIN / LOGOUT / REGISTER ---

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u, p = request.form["username"], request.form["password"]
        if u == "admin" and p == "admin":
            session["user"] = "admin"
            return redirect("/admin")
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM users WHERE username=? AND password=?", (u, p))
        row = cur.fetchone()
        con.close()
        if row:
            if row[3] == 0: 
                flash("Konto nieaktywne.")
                return render_template("login.html")
            session["user"] = u
            session["category"] = row[4]
            return redirect("/user")
        flash("Błąd logowania")
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

@app.route("/register", methods=["POST"])
def register():
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    try:
        cur.execute("INSERT INTO users (username, password, is_active, category) VALUES (?,?,1,?)", 
                   (request.form["username"], request.form["password"], request.form["category"]))
        con.commit()
    except: pass
    con.close()
    return redirect(request.referrer or "/admin")

@app.route("/toggle_user/<int:uid>")
def toggle_user(uid):
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))
    con.commit(); con.close()
    return redirect(request.referrer or "/admin")

# --- WIDOK DOSTAWCY (USER) ---

@app.route("/user")
def user():
    if "user" not in session or session["user"] == "admin": return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM exchanges ORDER BY deadline DESC")
    all_ex = cur.fetchall()
    exchange_data = []
    
    user_cat = session.get("category", "Spedycja")

    for ex in all_ex:
        # Filtrujemy widok dla usera
        if ex[2] != user_cat: continue
        if not is_exchange_open(ex[0]): continue

        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        materials = []
        
        shipping_bid = None
        shipping_rank = "-"
        
        # LOGIKA SPEDYCJA
        if ex[2] == 'Spedycja':
            cur.execute("SELECT val_pln, val_eur, val_usd, total_pln_calc FROM shipping_bids WHERE exchange_id=? AND user=? ORDER BY id DESC LIMIT 1", (ex[0], session['user']))
            shipping_bid = cur.fetchone()
            
            if shipping_bid:
                my_total = shipping_bid[3]
                # Pobieramy najnowsze oferty konkurencji do rankingu
                cur.execute("SELECT user, total_pln_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id DESC", (ex[0],))
                all_raw = cur.fetchall()
                latest_bids = {}
                for u, val in all_raw:
                    if u not in latest_bids: latest_bids[u] = val
                
                sorted_vals = sorted(latest_bids.values())
                try:
                    rank_pos = sorted_vals.index(my_total) + 1
                    if rank_pos == 1 and list(latest_bids.values()).count(sorted_vals[0]) > 1:
                        shipping_rank = "REMIS"
                    else:
                        shipping_rank = rank_pos
                except: pass

        # LOGIKA MATERIAŁ / WYCENA
        for m in mats_raw:
            saved_price = None
            item_rank = "-"
            
            if ex[2] != 'Spedycja':
                cur.execute("SELECT price, currency, user_file1, user_file2, user_file3, substitute_note FROM prices WHERE material_id=? AND user=? ORDER BY id DESC LIMIT 1", (m[0], session["user"]))
                saved_price = cur.fetchone()
                
                if saved_price and saved_price[0] > 0:
                    my_val = saved_price[0]
                    cur.execute("SELECT user, price FROM prices WHERE material_id=? ORDER BY id DESC", (m[0],))
                    all_raw = cur.fetchall()
                    latest_prices = {}
                    for u, val in all_raw:
                        if u not in latest_prices: latest_prices[u] = val
                    
                    sorted_vals = sorted(latest_prices.values())
                    try:
                        rank_pos = sorted_vals.index(my_val) + 1
                        if rank_pos == 1 and list(latest_prices.values()).count(sorted_vals[0]) > 1:
                            item_rank = "REMIS"
                        else:
                            item_rank = rank_pos
                    except: item_rank = "-"

            materials.append({
                "id": m[0], "name": m[2], 
                "net": m[3], "gross": m[4], "vol": m[5], "qty": m[6], 
                "kg_m": m[7], "len": m[8],
                "hs": m[9], "admin_file": m[10],
                "saved": saved_price,
                "rank": item_rank
            })
        
        exchange_data.append({
            "id": ex[0], "name": ex[1], "cat": ex[2], "deadline": ex[3],
            "eur": ex[6], "usd": ex[7], "desc": ex[10],
            "mats": materials, "is_open": True,
            "f1": ex[8], "f2": ex[9],
            "shipping_bid": shipping_bid,
            "shipping_rank": shipping_rank
        })
    con.close()
    return render_template("user.html", exchange_data=exchange_data)

# --- ZAPIS OFERTY (CORE LOGIC + MAILING) ---

@app.route("/save_offer/<int:eid>", methods=["POST"])
def save_offer(eid):
    if "user" not in session: return redirect("/")
    con = db(); cur = con.cursor()
    cur.execute("SELECT category, eur_rate, usd_rate, notify_enabled, name, deadline FROM exchanges WHERE id=?", (eid,))
    ex_row = cur.fetchone()
    if not ex_row: con.close(); return "Błąd", 404
    cat, rate_e, rate_u, notify_enabled, ex_name, ex_deadline = ex_row

    if not is_exchange_open(eid): 
        con.close(); return "Zamknięte", 403

    # 1. Sprawdzamy starego lidera (przed zmianą)
    old_leader = None
    if notify_enabled:
        old_leader, _ = get_current_leader_and_ranks(eid, cat)

    # 2. Zapisujemy nową ofertę (Historia - zawsze INSERT)
    if cat == 'Spedycja':
        pln = safe_float(request.form.get("sp_pln"))
        eur = safe_float(request.form.get("sp_eur"))
        usd = safe_float(request.form.get("sp_usd"))
        total = pln + (eur / rate_e) + (usd / rate_u)
        
        cur.execute("INSERT INTO shipping_bids (user, exchange_id, val_pln, val_eur, val_usd, total_pln_calc) VALUES (?,?,?,?,?,?)",
                   (session['user'], eid, pln, eur, usd, round(total, 2)))

    else:
        # Materiał/Wycena
        cur.execute("SELECT id FROM materials WHERE exchange_id=?", (eid,))
        mids = [r[0] for r in cur.fetchall()]
        for mid in mids:
            price_key = f"price_{mid}"
            if price_key in request.form:
                val = safe_float(request.form[price_key])
                curr = "PLN"
                sub_check = request.form.get(f"sub_check_{mid}")
                sub_note = request.form.get(f"sub_note_{mid}", "") if sub_check else ""

                # Też zawsze INSERT dla historii
                cur.execute("""INSERT INTO prices (user, material_id, price, currency, substitute_note) 
                            VALUES (?,?,?,?,?)""", (session['user'], mid, val, curr, sub_note))
    
    con.commit()

    # 3. Sprawdzamy nowego lidera i wysyłamy maila
    if notify_enabled:
        new_leader, new_ranks = get_current_leader_and_ranks(eid, cat)
        
        # Jeśli był lider, to nie ja, i przestał być liderem -> alert
        if old_leader and old_leader != session['user'] and old_leader != new_leader:
            new_rank_of_old = new_ranks.get(old_leader, "N/A")
            # Mail idzie na username (zakładamy, że to email)
            send_outbid_email(old_leader, ex_name, ex_deadline, new_rank_of_old)

    con.close()
    return redirect("/user")

# --- ADMIN VIEW ---

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if session.get("user") != "admin": return redirect("/")
    con = db(); cur = con.cursor()
    
    sel_id = request.args.get('exchange_id', type=int)
    view = request.args.get('view', 'active') 
    user_cat_tab = request.args.get('user_cat', 'Spedycja')

    if request.method == "POST":
        if "exchange_name" in request.form:
            af_names = ["", ""]
            for i, f in enumerate([request.files.get('af1'), request.files.get('af2')]):
                if f and allowed_file(f.filename):
                    fn = secure_filename(f"admin_{i}_{f.filename}")
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                    af_names[i] = fn

            e_r = safe_float(request.form.get("eur_rate", "1"))
            u_r = safe_float(request.form.get("usd_rate", "1"))
            notify = 1 if request.form.get("notify_enabled") else 0
            
            cur.execute("""INSERT INTO exchanges (name, category, deadline, currency, eur_rate, usd_rate, admin_file1, admin_file2, description, notify_enabled) 
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", 
                       (request.form["exchange_name"], request.form["category"], request.form["deadline"], "PLN", e_r, u_r, af_names[0], af_names[1], request.form.get("desc",""), notify))
            con.commit()
            return redirect(url_for('admin'))
            
        elif "add_item" in request.form:
            eid = request.form["exchange_id"]
            qty = safe_float(request.form.get("qty"))
            kg_m = safe_float(request.form.get("kg_m"))
            length = safe_float(request.form.get("len"))
            net_w = safe_float(request.form.get("net"))
            gross_w = safe_float(request.form.get("gross"))
            vol = safe_float(request.form.get("vol"))
            
            if qty > 0 and kg_m > 0 and length > 0:
                net_w = round(qty * length * kg_m, 2)

            item_file_name = ""
            if "item_file" in request.files:
                f = request.files["item_file"]
                if f and allowed_file(f.filename):
                    item_file_name = secure_filename(f"item_{eid}_{f.filename}")
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], item_file_name))

            cur.execute("""INSERT INTO materials (exchange_id, name, net_weight, gross_weight, volume, quantity, kg_per_m, length_m, hs_code, item_admin_file) 
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (eid, request.form.get("name"), net_w, gross_w, vol, int(qty), kg_m, length, request.form.get("hs",""), item_file_name))
            con.commit()
            sel_id = int(eid)

    cur.execute("SELECT * FROM exchanges ORDER BY deadline DESC"); all_ex = cur.fetchall()
    cur.execute("SELECT id, username, is_active, category FROM users WHERE username != 'admin' ORDER BY username"); 
    all_users_list = cur.fetchall()

    open_ex = [e for e in all_ex if is_exchange_open(e[0])]
    closed_ex = [e for e in all_ex if not is_exchange_open(e[0])]
    process_ex = [e for e in all_ex if e[0] == sel_id] if sel_id else []

    ex_details = []
    for ex in process_ex:
        cat = ex[2]
        cur.execute("SELECT * FROM materials WHERE exchange_id=?", (ex[0],))
        mats_raw = cur.fetchall()
        
        cur.execute("SELECT username FROM users WHERE category=? AND is_active=1", (cat,))
        target_emails = [r[0] for r in cur.fetchall()]
        
        # --- NOWA TREŚĆ MAILA (MAILTO DLA ADMINA) ---
        deadline_pretty = ex[3].replace('T', ' ')
        mail_subject = f"{ex[1]} jest otwarta. Zapraszamy do składania ofert"
        mail_body = f"{ex[1]} jest otwarta do {deadline_pretty} - po tym terminie nie będzie można dodawać ani edytować cen.\n\nZapraszamy do składania ofert."
        mailto_link = f"mailto:?bcc={','.join(target_emails)}&subject={urllib.parse.quote(mail_subject)}&body={urllib.parse.quote(mail_body)}"
        # ---------------------------------------------

        shipping_stats = []
        if cat == 'Spedycja':
            cur.execute("SELECT user, val_pln, val_eur, val_usd, total_pln_calc FROM shipping_bids WHERE exchange_id=? ORDER BY id ASC", (ex[0],))
            history_rows = cur.fetchall()
            
            user_history = {}
            for row in history_rows:
                u, p, e, u_usd, total = row[0], row[1], row[2], row[3], row[4]
                if u not in user_history: user_history[u] = []
                user_history[u].append({'p':p, 'e':e, 'usd':u_usd, 'total':total})
            
            current_offers = []
            for u, h in user_history.items():
                start_offer = h[0]['total']
                curr_offer = h[-1]['total']
                drop_pct = round((start_offer - curr_offer) / start_offer * 100, 1) if start_offer > 0 else 0
                
                current_offers.append({
                    'user': u,
                    'desc': f"PLN: {h[-1]['p']} | EUR: {h[-1]['e']} | USD: {h[-1]['usd']}",
                    'start_total': start_offer,
                    'curr_total': curr_offer,
                    'drop_pct': drop_pct
                })
            
            current_offers.sort(key=lambda x: x['curr_total'])
            if current_offers:
                min_total = current_offers[0]['curr_total']
                is_remis = [o['curr_total'] for o in current_offers].count(min_total) > 1
                for o in current_offers:
                    o['is_best'] = (o['curr_total'] == min_total)
                    o['label'] = "REMIS" if (o['is_best'] and is_remis) else ("L1" if o['is_best'] else "")
                    shipping_stats.append(o)
        
        mats_with_offers = []
        for m in mats_raw:
            cur.execute("SELECT user, price, substitute_note FROM prices WHERE material_id=? ORDER BY id ASC", (m[0],))
            price_history = cur.fetchall()
            
            user_history = {}
            for p in price_history:
                u, price, sub = p[0], p[1], p[2]
                if u not in user_history: user_history[u] = []
                user_history[u].append({'price': price, 'sub': sub})
            
            offers_list = []
            for u, h in user_history.items():
                start_price = h[0]['price']
                curr_price = h[-1]['price']
                drop_pct = 0
                if start_price > 0:
                    drop_pct = round((start_price - curr_price) / start_price * 100, 1)
                
                offers_list.append({
                    'user': u,
                    'pln': curr_price,
                    'start_pln': start_price,
                    'drop_pct': drop_pct,
                    'sub': h[-1]['sub']
                })
            
            offers_list.sort(key=lambda x: x['pln'])
            
            curr_prices = [o['pln'] for o in offers_list if o['pln'] > 0]
            item_stats = {'avg': 0, 'std': 0}
            if curr_prices:
                item_stats['avg'] = round(statistics.mean(curr_prices), 2)
                if len(curr_prices) > 1:
                    item_stats['std'] = round(statistics.stdev(curr_prices), 2)

            if offers_list:
                min_pln = offers_list[0]['pln']
                is_remis = [o['pln'] for o in offers_list].count(min_pln) > 1
                for o in offers_list:
                    o['is_best'] = (o['pln'] == min_pln)
                    o['label'] = "REMIS" if (o['is_best'] and is_remis) else ("L1" if o['is_best'] else "")

            mats_with_offers.append({'data': m, 'offers': offers_list, 'stats': item_stats})

        ex_details.append({'info': ex, 'mats_offers': mats_with_offers, 'shipping_stats': shipping_stats, 'mailto': mailto_link})

    l_eur = get_live_rate('EUR')
    l_usd = get_live_rate('USD')
    con.close()
    
    return render_template("admin.html", 
                         ex_details=ex_details, 
                         all_users_list=all_users_list, 
                         open_ex=open_ex, closed_ex=closed_ex,
                         sel_id=sel_id, view=view, user_cat_tab=user_cat_tab,
                         live_eur=l_eur, live_usd=l_usd)

# ... TRASY DELETE / TOGGLE ...
@app.route("/delete_user/<int:uid>")
def delete_user(uid):
    if session.get("user") == "admin":
        con = db(); cur = con.cursor()
        cur.execute("DELETE FROM users WHERE id=?", (uid,))
        con.commit(); con.close()
    return redirect(request.referrer or "/admin")

@app.route("/delete_material/<int:mid>")
def delete_material(mid):
    if session.get("user") == "admin":
        con = db(); cur = con.cursor()
        cur.execute("DELETE FROM materials WHERE id=?", (mid,))
        con.commit(); con.close()
    return redirect(request.referrer or "/admin")

@app.route("/toggle_lock/<int:eid>")
def toggle_lock(eid):
    if session.get("user") == "admin":
        con = db(); cur = con.cursor()
        cur.execute("UPDATE exchanges SET is_locked = 1 - is_locked WHERE id=?", (eid,))
        con.commit(); con.close()
    return redirect(request.referrer or "/admin")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)