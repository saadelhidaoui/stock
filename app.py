"""
IntelStock — Gestion de Stock Professionnelle
Backend Flask complet avec multi-entrepôts, alertes, graphiques d'utilisation, export CSV/PDF
"""

import os, csv, io, logging, random
from datetime import datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler
from collections import defaultdict

from flask import Flask, render_template, request, jsonify, session, redirect, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from dotenv import load_dotenv

load_dotenv()

# ── Application Setup ─────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'intelstock-dev-secret-2024-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///intelstock.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 300}

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs('logs', exist_ok=True)
handler = RotatingFileHandler('logs/intelstock.log', maxBytes=5_000_000, backupCount=3)
handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password     = db.Column(db.String(200), nullable=False)
    role         = db.Column(db.String(20), default='operator')  # superadmin|admin|operator|viewer
    active       = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_login   = db.Column(db.DateTime)

class Category(db.Model):
    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(20), default='blue')

class Warehouse(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(200))
    active   = db.Column(db.Boolean, default=True)

class Product(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False)
    category_id   = db.Column(db.Integer, db.ForeignKey('category.id'))
    category      = db.relationship('Category', backref='products')
    description   = db.Column(db.Text)
    unit          = db.Column(db.String(20), default='unité')
    reorder_point = db.Column(db.Integer, default=10)
    reorder_qty   = db.Column(db.Integer, default=50)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

class StockLevel(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    product_id   = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product      = db.relationship('Product', backref='stock_levels')
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    warehouse    = db.relationship('Warehouse', backref='stock_levels')
    quantity     = db.Column(db.Integer, default=0)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('product_id', 'warehouse_id'),)

class StockMovement(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    type         = db.Column(db.String(20))          # receive|transfer|adjust|writeoff|distribute
    product_id   = db.Column(db.Integer, db.ForeignKey('product.id'))
    product      = db.relationship('Product')
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    warehouse    = db.relationship('Warehouse')
    quantity     = db.Column(db.Integer)
    reference    = db.Column(db.String(100))
    notes        = db.Column(db.Text)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'))
    user         = db.relationship('User')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

class Alert(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('product.id'))
    product     = db.relationship('Product')
    warehouse_id= db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    warehouse   = db.relationship('Warehouse')
    level       = db.Column(db.String(20))   # critical|warning|info
    message     = db.Column(db.Text)
    quantity    = db.Column(db.Integer)
    resolved    = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)

class AuditLog(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'))
    user        = db.relationship('User')
    action      = db.Column(db.String(100))
    entity_type = db.Column(db.String(50))
    entity_id   = db.Column(db.Integer)
    details     = db.Column(db.Text)
    ip_address  = db.Column(db.String(45))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

# ── Helpers ───────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Non autorisé'}), 401
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({'error': 'Non autorisé'}), 401
            u = User.query.get(session['user_id'])
            if not u or u.role not in roles:
                return jsonify({'error': 'Permissions insuffisantes'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def get_current_user():
    if 'user_id' in session:
        return db.session.get(User, session['user_id'])
    return None

def audit(action, entity_type='', entity_id=None, details=''):
    """Record an audit log entry."""
    try:
        log = AuditLog(
            user_id=session.get('user_id'),
            action=action, entity_type=entity_type,
            entity_id=entity_id, details=details,
            ip_address=request.remote_addr
        )
        db.session.add(log)
    except Exception:
        pass  # Never block on audit failure

def check_alerts():
    """
    Deduplicated alert generation.
    For each product+warehouse pair:
      - Resolve any existing alert whose level no longer applies
      - Create a new alert only if none with that level already exists (unresolved)
    """
    try:
        levels = StockLevel.query.all()
        for sl in levels:
            p = sl.product
            qty = sl.quantity

            # Determine current alert level
            if qty == 0:
                new_level = 'critical'
                msg = f'"{p.name}" est EN RUPTURE DE STOCK ({sl.warehouse.name})'
            elif qty <= p.reorder_point:
                new_level = 'warning'
                msg = (f'"{p.name}" sous le seuil de réappro '
                       f'({qty} restant·s, seuil: {p.reorder_point}) — {sl.warehouse.name}')
            elif qty <= int(p.reorder_point * 1.5):
                new_level = 'info'
                msg = (f'"{p.name}" approche du seuil de réappro '
                       f'({qty} unités) — {sl.warehouse.name}')
            else:
                new_level = None

            # Resolve outdated alerts for this product+warehouse
            existing = (Alert.query
                        .filter_by(product_id=p.id, warehouse_id=sl.warehouse_id, resolved=False)
                        .all())
            for ex in existing:
                if new_level is None or ex.level != new_level:
                    ex.resolved = True
                    ex.resolved_at = datetime.utcnow()

            # Create new alert if needed and not already present
            if new_level:
                already = (Alert.query
                           .filter_by(product_id=p.id, warehouse_id=sl.warehouse_id,
                                      level=new_level, resolved=False)
                           .first())
                if not already:
                    db.session.add(Alert(
                        product_id=p.id, warehouse_id=sl.warehouse_id,
                        level=new_level, message=msg, quantity=qty
                    ))
        db.session.commit()
    except Exception as e:
        app.logger.error(f'check_alerts error: {e}')
        db.session.rollback()

def get_total_stock(product_id):
    """Total quantity across all warehouses."""
    result = (db.session.query(db.func.sum(StockLevel.quantity))
              .filter_by(product_id=product_id).scalar())
    return result or 0

# ── Seed Data ─────────────────────────────────────────────────────────────────

def seed_data():
    if User.query.count() > 0:
        return

    # Utilisateurs
    users = [
        User(name='Super Admin', email='admin@stock.io',
             password=bcrypt.generate_password_hash('admin123').decode(), role='superadmin'),
        User(name='Jean Responsable', email='jane@stock.io',
             password=bcrypt.generate_password_hash('admin123').decode(), role='admin'),
        User(name='Bob Opérateur', email='bob@stock.io',
             password=bcrypt.generate_password_hash('admin123').decode(), role='operator'),
        User(name='Alice Visionneuse', email='alice@stock.io',
             password=bcrypt.generate_password_hash('admin123').decode(), role='viewer'),
    ]
    for u in users: db.session.add(u)

    # Catégories métier réelles
    cats = [
        Category(name='Équipements de Protection', color='red'),
        Category(name='Rouleaux & Étiquettes',     color='blue'),
        Category(name='Papeterie & Écriture',      color='teal'),
        Category(name='Outillage',                 color='amber'),
        Category(name='Consommables Bureau',       color='purple'),
    ]
    for c in cats: db.session.add(c)
    db.session.commit()

    # Entrepôts
    warehouses = [
        Warehouse(name='Entrepôt Principal', location='Bâtiment A'),
        Warehouse(name='Réserve Est',        location='Bâtiment B'),
        Warehouse(name='Stock Urgent',       location='Bâtiment C'),
    ]
    for w in warehouses: db.session.add(w)
    db.session.commit()

    # Produits sans prix/SKU — produits métier réels
    products_data = [
        # (nom, cat_id, unité, seuil_réappro, qté_réappro, description)
        ('Gilets de sécurité jaune fluo',      1, 'pièce',   20, 100, 'Gilets haute visibilité EN471 classe 2'),
        ('Gants de sécurité anti-coupure',     1, 'paire',   30, 150, 'Gants Nitri-Solve niveau 5'),
        ('Gants latex jetables (boîte 100)',   1, 'boîte',   10,  50, 'Gants latex poudré taille M'),
        ('Casques de sécurité blanc',          1, 'pièce',    8,  40, 'Casques ABS conformes EN397'),
        ('Lunettes de protection claires',     1, 'pièce',   15,  60, 'Lunettes anti-rayures EN166'),
        ('Rouleaux Avery blancs 50mm',         2, 'rouleau', 15,  80, 'Étiquettes adhésives permanentes 50×30 mm'),
        ('Rouleaux étiquettes SPS 40mm',       2, 'rouleau', 20, 100, 'Étiquettes thermiques directes 40×25 mm'),
        ('Rouleaux thermiques SPS 80mm',       2, 'rouleau', 25, 120, 'Papier thermique pour imprimante SPS 80 mm'),
        ('Rouleaux étiquettes SPS 60mm',       2, 'rouleau', 10,  60, 'Étiquettes thermiques directes 60×40 mm'),
        ('Ruban d\'impression Zebra noir',     2, 'rouleau',  8,  40, 'Ruban résine 110×300 m'),
        ('Crayons à papier HB (boîte 12)',     3, 'boîte',   10,  50, 'Crayons HB standard'),
        ('Feutres marqueurs noir (boîte 10)',  3, 'boîte',   12,  60, 'Marqueurs permanents pointe fine'),
        ('Stylos bille bleu (boîte 50)',       3, 'boîte',   15,  80, 'Stylos bille Bic Cristal bleu'),
        ('Surligneur jaune (boîte 10)',        3, 'boîte',    8,  40, 'Surligneurs fluo pointe biseautée'),
        ('Couteaux Stanley 18mm',              4, 'pièce',   10,  40, 'Couteaux à lame rétractable 18 mm'),
        ('Lames de rechange 18mm (boîte 10)',  4, 'boîte',   15,  60, 'Lames breakaway acier inoxydable'),
        ('Cutter rotatif 45mm',               4, 'pièce',    5,  20, 'Cutter rotatif avec protection'),
        ('Ruban adhésif transparent 48mm',     5, 'rouleau', 20, 100, 'Scotch transparent 48×66 m'),
        ('Ruban adhésif emballage marron',     5, 'rouleau', 20, 100, 'Ruban kraft adhésif 50 mm'),
        ('Post-it jaune 76×76 (bloc 100f)',    5, 'bloc',    12,  60, 'Notes adhésives reposition­nables'),
        ('Classeurs A4 dos 8cm',              5, 'pièce',    6,  30, 'Classeurs à levier plastique rouge'),
        ('Papier A4 80g (rame 500f)',          5, 'rame',    30, 150, 'Papier reprographie blanc'),
        ('Trombones métalliques (boîte 100)', 5, 'boîte',    8,  40, 'Trombones 28 mm galvanisés'),
        ('Agrafes 26/6 (boîte 5000)',         5, 'boîte',    6,  30, 'Agrafes standard'),
    ]

    # Quantités initiales avec variété pour les alertes
    init_qtys_wh1 = [5, 35, 8, 0, 22, 4, 60, 12, 28, 2,
                     18, 7, 3, 45, 9, 30, 1, 50, 22, 14,
                     3, 80, 4, 2]
    init_qtys_wh2 = [10, 20, 5, 15, 8, 30, 10, 40, 5, 20,
                     5, 0, 10, 15, 4, 10, 2, 20, 8, 6,
                     0, 30, 2, 5]

    whs = Warehouse.query.all()
    wh1, wh2 = whs[0], whs[1]

    for i, (name, cat_id, unit, rp, rq, desc) in enumerate(products_data):
        p = Product(name=name, category_id=cat_id, unit=unit,
                    reorder_point=rp, reorder_qty=rq, description=desc)
        db.session.add(p)
        db.session.flush()
        sl1 = StockLevel(product_id=p.id, warehouse_id=wh1.id, quantity=init_qtys_wh1[i])
        sl2 = StockLevel(product_id=p.id, warehouse_id=wh2.id, quantity=init_qtys_wh2[i])
        db.session.add(sl1)
        db.session.add(sl2)

    db.session.commit()

    # Mouvements historiques sur 30 jours pour graphiques d'utilisation
    products = Product.query.all()
    users_list = User.query.all()
    move_types = ['receive', 'distribute', 'writeoff', 'adjust']
    weights    = [0.35, 0.45, 0.1, 0.1]

    for _ in range(120):
        p    = random.choice(products)
        mtyp = random.choices(move_types, weights=weights)[0]
        qty  = random.randint(1, 20)
        days_ago = random.randint(0, 29)
        m = StockMovement(
            type=mtyp, product_id=p.id, warehouse_id=wh1.id,
            quantity=qty, reference=f'REF-{random.randint(1000,9999)}',
            user_id=random.choice(users_list).id,
            created_at=datetime.utcnow() - timedelta(days=days_ago,
                                                      hours=random.randint(0,23))
        )
        db.session.add(m)

    db.session.commit()
    check_alerts()
    app.logger.info('Données initiales chargées avec succès.')

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect('/dashboard')

@app.route('/login')
def login_page():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')
    return render_template('index.html')

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.json or {}
    user = User.query.filter_by(email=data.get('email', '')).first()
    if user and user.active and bcrypt.check_password_hash(user.password, data.get('password', '')):
        session['user_id'] = user.id
        user.last_login = datetime.utcnow()
        db.session.commit()
        audit('login', 'user', user.id, f'Connexion depuis {request.remote_addr}')
        db.session.commit()
        app.logger.info(f'Login: {user.email}')
        return jsonify({'success': True, 'user': {
            'id': user.id, 'name': user.name, 'email': user.email, 'role': user.role
        }})
    app.logger.warning(f'Échec connexion: {data.get("email")}')
    return jsonify({'success': False, 'error': 'Identifiants invalides'}), 401

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/auth/me')
def api_me():
    u = get_current_user()
    if not u:
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify({'id': u.id, 'name': u.name, 'email': u.email, 'role': u.role})

# ── Dashboard API ─────────────────────────────────────────────────────────────

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    check_alerts()
    total_products = Product.query.count()
    total_stock    = db.session.query(db.func.sum(StockLevel.quantity)).scalar() or 0
    low_stock      = (StockLevel.query
                      .filter(StockLevel.quantity <= Product.reorder_point)
                      .join(Product)
                      .count())
    active_alerts  = Alert.query.filter_by(resolved=False).count()
    critical_count = Alert.query.filter_by(resolved=False, level='critical').count()
    warning_count  = Alert.query.filter_by(resolved=False, level='warning').count()

    # Activité 30 jours
    trend = []
    for i in range(30):
        day   = datetime.utcnow() - timedelta(days=29 - i)
        d_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        d_end   = day.replace(hour=23, minute=59, second=59)
        count = StockMovement.query.filter(
            StockMovement.created_at >= d_start,
            StockMovement.created_at <= d_end
        ).count()
        trend.append({'day': day.strftime('%d/%m'), 'count': count})

    recent = (StockMovement.query
              .order_by(StockMovement.created_at.desc())
              .limit(10).all())

    return jsonify({
        'total_products': total_products,
        'total_stock': total_stock,
        'low_stock': low_stock,
        'active_alerts': active_alerts,
        'critical': critical_count,
        'warning': warning_count,
        'trend': trend,
        'recent_movements': [{
            'id': m.id, 'type': m.type,
            'product': m.product.name,
            'qty': m.quantity,
            'ref': m.reference or '—',
            'warehouse': m.warehouse.name if m.warehouse else '—',
            'time': m.created_at.strftime('%d/%m %H:%M')
        } for m in recent]
    })

# ── Products API ──────────────────────────────────────────────────────────────

@app.route('/api/products')
@login_required
def api_products():
    search  = request.args.get('q', '').strip()
    cat_id  = request.args.get('cat', '')
    page    = int(request.args.get('page', 1))
    per_page= int(request.args.get('per_page', 50))

    q = Product.query
    if search:
        q = q.filter(Product.name.ilike(f'%{search}%'))
    if cat_id:
        q = q.filter(Product.category_id == int(cat_id))

    total   = q.count()
    products= q.order_by(Product.name).offset((page-1)*per_page).limit(per_page).all()

    result = []
    for p in products:
        total_qty = get_total_stock(p.id)
        wh_stocks = (StockLevel.query
                     .filter_by(product_id=p.id)
                     .join(Warehouse).all())
        if total_qty == 0:         status = 'critical'
        elif total_qty <= p.reorder_point: status = 'warning'
        elif total_qty <= int(p.reorder_point * 1.5): status = 'low'
        else:                      status = 'ok'

        result.append({
            'id': p.id, 'name': p.name,
            'category': p.category.name if p.category else '',
            'cat_color': p.category.color if p.category else 'gray',
            'unit': p.unit,
            'quantity': total_qty,
            'reorder_point': p.reorder_point,
            'reorder_qty': p.reorder_qty,
            'status': status,
            'description': p.description or '',
            'warehouses': [{'wh': sl.warehouse.name, 'qty': sl.quantity}
                           for sl in wh_stocks]
        })
    return jsonify({'products': result, 'total': total, 'page': page, 'per_page': per_page})

@app.route('/api/products', methods=['POST'])
@login_required
def api_add_product():
    data = request.json or {}
    try:
        p = Product(
            name=data['name'],
            category_id=data.get('category_id') or None,
            unit=data.get('unit', 'unité'),
            reorder_point=int(data.get('reorder_point', 10)),
            reorder_qty=int(data.get('reorder_qty', 50)),
            description=data.get('description', '')
        )
        db.session.add(p)
        db.session.flush()
        # Create stock entry for all active warehouses
        warehouses = Warehouse.query.filter_by(active=True).all()
        init_qty = int(data.get('initial_qty', 0))
        for wh in warehouses:
            qty = init_qty if wh == warehouses[0] else 0
            sl = StockLevel(product_id=p.id, warehouse_id=wh.id, quantity=qty)
            db.session.add(sl)
        db.session.commit()
        check_alerts()
        audit('create_product', 'product', p.id, p.name)
        db.session.commit()
        return jsonify({'success': True, 'id': p.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/products/<int:pid>', methods=['PUT'])
@login_required
def api_update_product(pid):
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Produit introuvable'}), 404
    data = request.json or {}
    p.name          = data.get('name', p.name)
    p.category_id   = data.get('category_id') or p.category_id
    p.unit          = data.get('unit', p.unit)
    p.reorder_point = int(data.get('reorder_point', p.reorder_point))
    p.reorder_qty   = int(data.get('reorder_qty', p.reorder_qty))
    p.description   = data.get('description', p.description)
    db.session.commit()
    check_alerts()
    return jsonify({'success': True})

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@role_required('superadmin', 'admin')
def api_delete_product(pid):
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Introuvable'}), 404
    audit('delete_product', 'product', pid, p.name)
    StockLevel.query.filter_by(product_id=pid).delete()
    StockMovement.query.filter_by(product_id=pid).delete()
    Alert.query.filter_by(product_id=pid).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True})

# ── CSV Import ────────────────────────────────────────────────────────────────

@app.route('/api/products/import', methods=['POST'])
@role_required('superadmin', 'admin')
def api_import_products():
    """
    Import CSV format: name,category,unit,reorder_point,reorder_qty,initial_qty,description
    """
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'Aucun fichier'}), 400
    try:
        stream = io.StringIO(f.stream.read().decode('utf-8-sig'))
        reader = csv.DictReader(stream)
        added, errors = 0, []
        wh = Warehouse.query.filter_by(active=True).first()
        for row in reader:
            try:
                name = row.get('name', '').strip()
                if not name:
                    continue
                cat_name = row.get('category', '').strip()
                cat = Category.query.filter_by(name=cat_name).first() if cat_name else None
                p = Product(
                    name=name, category_id=cat.id if cat else None,
                    unit=row.get('unit', 'unité'),
                    reorder_point=int(row.get('reorder_point', 10)),
                    reorder_qty=int(row.get('reorder_qty', 50)),
                    description=row.get('description', '')
                )
                db.session.add(p); db.session.flush()
                sl = StockLevel(product_id=p.id, warehouse_id=wh.id,
                                quantity=int(row.get('initial_qty', 0)))
                db.session.add(sl)
                added += 1
            except Exception as e:
                errors.append(str(e))
        db.session.commit()
        check_alerts()
        return jsonify({'success': True, 'added': added, 'errors': errors})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

# ── Stock Movement API ────────────────────────────────────────────────────────

@app.route('/api/stock/move', methods=['POST'])
@login_required
def api_stock_move():
    data = request.json or {}
    try:
        pid       = int(data['product_id'])
        qty       = int(data['quantity'])
        move_type = data['type']
        wh_id     = int(data.get('warehouse_id', Warehouse.query.first().id))

        if qty <= 0:
            return jsonify({'error': 'Quantité doit être > 0'}), 400

        sl = StockLevel.query.filter_by(product_id=pid, warehouse_id=wh_id).first()
        if not sl:
            sl = StockLevel(product_id=pid, warehouse_id=wh_id, quantity=0)
            db.session.add(sl)

        if move_type == 'receive':
            sl.quantity += qty
        elif move_type in ('distribute', 'transfer', 'writeoff'):
            if sl.quantity < qty:
                return jsonify({'error': f'Stock insuffisant ({sl.quantity} disponible)'}), 400
            sl.quantity -= qty
        elif move_type == 'adjust':
            sl.quantity = qty
        else:
            return jsonify({'error': 'Type de mouvement invalide'}), 400

        sl.updated_at = datetime.utcnow()
        m = StockMovement(
            type=move_type, product_id=pid, warehouse_id=wh_id,
            quantity=qty, reference=data.get('reference', ''),
            notes=data.get('notes', ''), user_id=session['user_id']
        )
        db.session.add(m)
        db.session.commit()
        check_alerts()
        audit('stock_move', 'product', pid,
              f'{move_type} x{qty} ({sl.warehouse.name})')
        db.session.commit()
        return jsonify({'success': True, 'new_qty': sl.quantity,
                        'total_qty': get_total_stock(pid)})
    except (KeyError, ValueError) as e:
        return jsonify({'error': f'Données invalides: {e}'}), 400
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'stock_move error: {e}')
        return jsonify({'error': 'Erreur serveur'}), 500

@app.route('/api/movements')
@login_required
def api_movements():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    pid      = request.args.get('product_id')
    date_from= request.args.get('from')
    date_to  = request.args.get('to')
    mtype    = request.args.get('type')

    q = StockMovement.query
    if pid:
        q = q.filter_by(product_id=int(pid))
    if mtype:
        q = q.filter_by(type=mtype)
    if date_from:
        q = q.filter(StockMovement.created_at >= datetime.strptime(date_from, '%Y-%m-%d'))
    if date_to:
        dt = datetime.strptime(date_to, '%Y-%m-%d').replace(hour=23, minute=59)
        q = q.filter(StockMovement.created_at <= dt)

    total = q.count()
    mvs   = q.order_by(StockMovement.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()

    return jsonify({
        'movements': [{
            'id': m.id, 'type': m.type,
            'product': m.product.name,
            'warehouse': m.warehouse.name if m.warehouse else '—',
            'quantity': m.quantity,
            'reference': m.reference or '',
            'notes': m.notes or '',
            'user': m.user.name if m.user else 'Système',
            'time': m.created_at.strftime('%d/%m/%Y %H:%M')
        } for m in mvs],
        'total': total, 'page': page, 'per_page': per_page
    })

# ── Usage Chart API ───────────────────────────────────────────────────────────

@app.route('/api/usage/by-date')
@login_required
def api_usage_by_date():
    """
    Consommation journalière par produit sur N jours.
    Retourne données pour graphique Chart.js.
    """
    days    = int(request.args.get('days', 30))
    pid     = request.args.get('product_id')
    cat_id  = request.args.get('cat_id')

    # Build date range
    date_labels = []
    date_range  = []
    for i in range(days):
        day = (datetime.utcnow() - timedelta(days=days-1-i)).date()
        date_labels.append(day.strftime('%d/%m'))
        date_range.append(day)

    # Which products to show
    if pid:
        products = [Product.query.get(int(pid))]
    elif cat_id:
        products = Product.query.filter_by(category_id=int(cat_id)).limit(8).all()
    else:
        # Top 8 produits par volume de mouvement
        subq = (db.session.query(
                    StockMovement.product_id,
                    db.func.sum(StockMovement.quantity).label('total'))
                .filter(StockMovement.type.in_(['distribute', 'writeoff']))
                .group_by(StockMovement.product_id)
                .order_by(db.desc('total'))
                .limit(8).subquery())
        products = (Product.query
                    .join(subq, Product.id == subq.c.product_id)
                    .all())
        if not products:
            products = Product.query.limit(8).all()

    colors = ['#4f8ef7','#3fcf7a','#f5a623','#ef4444',
              '#a78bfa','#2dd4bf','#fb7185','#f97316']
    datasets = []

    for idx, p in enumerate(products):
        if not p:
            continue
        data_pts = []
        for day in date_range:
            d_start = datetime.combine(day, datetime.min.time())
            d_end   = datetime.combine(day, datetime.max.time())
            qty = (db.session.query(db.func.sum(StockMovement.quantity))
                   .filter(
                       StockMovement.product_id == p.id,
                       StockMovement.type.in_(['distribute', 'writeoff']),
                       StockMovement.created_at >= d_start,
                       StockMovement.created_at <= d_end
                   ).scalar() or 0)
            data_pts.append(qty)

        datasets.append({
            'label': p.name,
            'data': data_pts,
            'borderColor': colors[idx % len(colors)],
            'backgroundColor': colors[idx % len(colors)] + '22',
            'tension': 0.4,
            'fill': False,
        })

    # Totaux par produit
    totals = [{'name': ds['label'],
               'total': sum(ds['data']),
               'color': ds['borderColor']} for ds in datasets]
    totals.sort(key=lambda x: x['total'], reverse=True)

    return jsonify({'labels': date_labels, 'datasets': datasets, 'totals': totals})

@app.route('/api/usage/by-product')
@login_required
def api_usage_by_product():
    """Consommation totale par produit sur N jours (pour bar chart)."""
    days = int(request.args.get('days', 30))
    since = datetime.utcnow() - timedelta(days=days)

    results = (db.session.query(
                   Product.name,
                   db.func.sum(StockMovement.quantity).label('total'))
               .join(StockMovement, Product.id == StockMovement.product_id)
               .filter(StockMovement.type.in_(['distribute', 'writeoff']))
               .filter(StockMovement.created_at >= since)
               .group_by(Product.name)
               .order_by(db.desc('total'))
               .limit(15).all())

    return jsonify([{'name': r.name, 'total': int(r.total)} for r in results])

# ── Alerts API ────────────────────────────────────────────────────────────────

@app.route('/api/alerts')
@login_required
def api_alerts():
    check_alerts()
    alerts = (Alert.query.filter_by(resolved=False)
              .order_by(Alert.created_at.desc()).all())
    return jsonify([{
        'id': a.id, 'level': a.level, 'message': a.message,
        'product': a.product.name,
        'warehouse': a.warehouse.name if a.warehouse else '—',
        'quantity': a.quantity,
        'time': a.created_at.strftime('%d/%m/%Y %H:%M')
    } for a in alerts])

@app.route('/api/alerts/<int:aid>/resolve', methods=['POST'])
@login_required
def api_resolve_alert(aid):
    a = db.session.get(Alert, aid)
    if not a:
        return jsonify({'error': 'Introuvable'}), 404
    a.resolved = True
    a.resolved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

# ── Users API ─────────────────────────────────────────────────────────────────

@app.route('/api/users')
@role_required('superadmin', 'admin')
def api_users():
    users = User.query.order_by(User.name).all()
    return jsonify([{
        'id': u.id, 'name': u.name, 'email': u.email, 'role': u.role,
        'active': u.active,
        'created_at': u.created_at.strftime('%d/%m/%Y'),
        'last_login': u.last_login.strftime('%d/%m/%Y %H:%M') if u.last_login else 'Jamais'
    } for u in users])

@app.route('/api/users', methods=['POST'])
@role_required('superadmin')
def api_add_user():
    data = request.json or {}
    if User.query.filter_by(email=data.get('email', '')).first():
        return jsonify({'error': 'Email déjà utilisé'}), 400
    u = User(
        name=data['name'], email=data['email'],
        password=bcrypt.generate_password_hash(data.get('password', 'changeme')).decode(),
        role=data.get('role', 'operator')
    )
    db.session.add(u)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/users/<int:uid>/toggle', methods=['POST'])
@role_required('superadmin')
def api_toggle_user(uid):
    u = db.session.get(User, uid)
    if not u:
        return jsonify({'error': 'Introuvable'}), 404
    u.active = not u.active
    db.session.commit()
    return jsonify({'success': True, 'active': u.active})

# ── Categories & Warehouses ───────────────────────────────────────────────────

@app.route('/api/categories')
@login_required
def api_categories():
    cats = Category.query.order_by(Category.name).all()
    return jsonify([{'id': c.id, 'name': c.name, 'color': c.color} for c in cats])

@app.route('/api/warehouses')
@login_required
def api_warehouses():
    whs = Warehouse.query.filter_by(active=True).order_by(Warehouse.name).all()
    return jsonify([{'id': w.id, 'name': w.name, 'location': w.location} for w in whs])

@app.route('/api/warehouses', methods=['POST'])
@role_required('superadmin', 'admin')
def api_add_warehouse():
    data = request.json or {}
    w = Warehouse(name=data['name'], location=data.get('location', ''))
    db.session.add(w)
    db.session.commit()
    # Add zero stock for all products
    for p in Product.query.all():
        db.session.add(StockLevel(product_id=p.id, warehouse_id=w.id, quantity=0))
    db.session.commit()
    return jsonify({'success': True, 'id': w.id})

# ── Reports API ───────────────────────────────────────────────────────────────

@app.route('/api/reports/summary')
@login_required
def api_reports_summary():
    products = Product.query.all()
    by_cat   = defaultdict(lambda: {'count': 0, 'quantity': 0})

    for p in products:
        cat  = p.category.name if p.category else 'Autre'
        qty  = get_total_stock(p.id)
        by_cat[cat]['count']    += 1
        by_cat[cat]['quantity'] += qty

    # Classement par quantité totale (sans prix)
    stock_qty = []
    for p in products:
        qty = get_total_stock(p.id)
        stock_qty.append({
            'name': p.name, 'qty': qty,
            'status': ('critical' if qty == 0 else
                       'warning' if qty <= p.reorder_point else 'ok')
        })
    stock_qty.sort(key=lambda x: x['qty'], reverse=True)

    return jsonify({'by_category': dict(by_cat), 'top_stock': stock_qty[:20]})

# ── Export CSV ────────────────────────────────────────────────────────────────

@app.route('/api/export/products')
@login_required
def export_products_csv():
    products = Product.query.order_by(Product.name).all()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Nom', 'Catégorie', 'Unité', 'Stock Total',
                'Seuil Réappro', 'Qté Réappro', 'Statut', 'Description'])
    for p in products:
        qty = get_total_stock(p.id)
        status = ('Rupture' if qty == 0 else
                  'Faible' if qty <= p.reorder_point else 'OK')
        w.writerow([p.name, p.category.name if p.category else '',
                    p.unit, qty, p.reorder_point, p.reorder_qty,
                    status, p.description or ''])
    out.seek(0)
    return send_file(
        io.BytesIO(out.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'produits_{datetime.now().strftime("%Y%m%d")}.csv'
    )

@app.route('/api/export/movements')
@login_required
def export_movements_csv():
    days  = int(request.args.get('days', 30))
    since = datetime.utcnow() - timedelta(days=days)
    mvs   = (StockMovement.query
             .filter(StockMovement.created_at >= since)
             .order_by(StockMovement.created_at.desc()).all())
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Date', 'Type', 'Produit', 'Entrepôt', 'Quantité',
                'Référence', 'Notes', 'Utilisateur'])
    type_labels = {'receive':'Réception','distribute':'Distribution',
                   'transfer':'Transfert','writeoff':'Mise au rebut','adjust':'Ajustement'}
    for m in mvs:
        w.writerow([
            m.created_at.strftime('%d/%m/%Y %H:%M'),
            type_labels.get(m.type, m.type),
            m.product.name,
            m.warehouse.name if m.warehouse else '—',
            m.quantity,
            m.reference or '',
            m.notes or '',
            m.user.name if m.user else 'Système'
        ])
    out.seek(0)
    return send_file(
        io.BytesIO(out.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'mouvements_{datetime.now().strftime("%Y%m%d")}.csv'
    )

@app.route('/api/export/alerts')
@login_required
def export_alerts_csv():
    alerts = Alert.query.order_by(Alert.created_at.desc()).limit(500).all()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Date', 'Niveau', 'Produit', 'Entrepôt', 'Message', 'Quantité', 'Résolu'])
    for a in alerts:
        w.writerow([
            a.created_at.strftime('%d/%m/%Y %H:%M'),
            a.level, a.product.name,
            a.warehouse.name if a.warehouse else '—',
            a.message, a.quantity,
            'Oui' if a.resolved else 'Non'
        ])
    out.seek(0)
    return send_file(
        io.BytesIO(out.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'alertes_{datetime.now().strftime("%Y%m%d")}.csv'
    )

# ── Audit Log API ─────────────────────────────────────────────────────────────

@app.route('/api/audit')
@role_required('superadmin')
def api_audit():
    logs = (AuditLog.query
            .order_by(AuditLog.created_at.desc())
            .limit(100).all())
    return jsonify([{
        'id': l.id,
        'user': l.user.name if l.user else 'Système',
        'action': l.action,
        'entity_type': l.entity_type,
        'details': l.details or '',
        'ip': l.ip_address or '',
        'time': l.created_at.strftime('%d/%m/%Y %H:%M')
    } for l in logs])

# ── Health Check ──────────────────────────────────────────────────────────────

@app.route('/api/health')
def api_health():
    return jsonify({'status': 'ok', 'version': '2.0.0'})

# ── Error Handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Ressource introuvable'}), 404

@app.errorhandler(500)
def server_error(e):
    app.logger.error(f'500 error: {e}')
    return jsonify({'error': 'Erreur serveur interne'}), 500

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_data()
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(debug=debug, port=int(os.environ.get('PORT', 5000)))