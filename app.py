from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import json, os, random

app = Flask(__name__)
app.secret_key = 'stockapp-secret-2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///stock.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# ── Models ──────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='operator')  # superadmin, admin, operator, viewer
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    color = db.Column(db.String(20), default='blue')

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    category = db.relationship('Category', backref='products')
    description = db.Column(db.Text)
    unit = db.Column(db.String(20), default='pcs')
    reorder_point = db.Column(db.Integer, default=10)
    reorder_qty = db.Column(db.Integer, default=50)
    unit_cost = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(200))

class StockLevel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product', backref='stock_levels')
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    warehouse = db.relationship('Warehouse', backref='stock_levels')
    quantity = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20))  # receive, transfer, adjust, writeoff
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product')
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'))
    warehouse = db.relationship('Warehouse')
    quantity = db.Column(db.Integer)
    reference = db.Column(db.String(100))
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product')
    level = db.Column(db.String(20))  # critical, warning, info
    message = db.Column(db.Text)
    quantity = db.Column(db.Integer)
    resolved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)

class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), unique=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    product = db.relationship('Product')
    quantity = db.Column(db.Integer)
    status = db.Column(db.String(20), default='pending')  # pending, approved, received
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ── Helpers ──────────────────────────────────────────────────────────────────

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None

def check_alerts():
    """Generate alerts for low stock products"""
    levels = StockLevel.query.all()
    for sl in levels:
        p = sl.product
        existing = Alert.query.filter_by(product_id=p.id, resolved=False).first()
        if sl.quantity == 0:
            level = 'critical'
            msg = f'{p.name} is OUT OF STOCK in {sl.warehouse.name}'
        elif sl.quantity <= p.reorder_point:
            level = 'warning'
            msg = f'{p.name} is below reorder point ({sl.quantity} left, reorder at {p.reorder_point})'
        elif sl.quantity <= p.reorder_point * 1.5:
            level = 'info'
            msg = f'{p.name} is approaching reorder point ({sl.quantity} units)'
        else:
            if existing:
                existing.resolved = True
                existing.resolved_at = datetime.utcnow()
                db.session.commit()
            continue
        if not existing:
            alert = Alert(product_id=p.id, level=level, message=msg, quantity=sl.quantity)
            db.session.add(alert)
    db.session.commit()

def seed_data():
    if User.query.count() > 0:
        return
    # Users
    users = [
        User(name='Super Admin', email='admin@stock.io', password=bcrypt.generate_password_hash('admin123').decode(), role='superadmin'),
        User(name='Jane Manager', email='jane@stock.io', password=bcrypt.generate_password_hash('admin123').decode(), role='admin'),
        User(name='Bob Operator', email='bob@stock.io', password=bcrypt.generate_password_hash('admin123').decode(), role='operator'),
        User(name='Alice Viewer', email='alice@stock.io', password=bcrypt.generate_password_hash('admin123').decode(), role='viewer'),
    ]
    for u in users: db.session.add(u)

    # Categories
    cats = [
        Category(name='Electronics', color='blue'),
        Category(name='Clothing', color='purple'),
        Category(name='Food & Beverage', color='green'),
        Category(name='Hardware', color='amber'),
        Category(name='Office Supplies', color='teal'),
    ]
    for c in cats: db.session.add(c)
    db.session.commit()

    # Warehouses
    wh = [Warehouse(name='Main Warehouse', location='Building A'), Warehouse(name='East Wing', location='Building B')]
    for w in wh: db.session.add(w)
    db.session.commit()

    # Products + stock
    products_data = [
        ('PRD-001', 'Wireless Keyboard', 1, 'pcs', 15, 50, 29.99),
        ('PRD-002', 'USB-C Hub 7-Port', 1, 'pcs', 20, 40, 49.99),
        ('PRD-003', 'Mechanical Mouse', 1, 'pcs', 10, 30, 39.99),
        ('PRD-004', 'Monitor Stand Arm', 1, 'pcs', 8, 20, 79.99),
        ('PRD-005', '4K Webcam Pro', 1, 'pcs', 12, 25, 119.99),
        ('PRD-006', 'Blue Denim Jacket XL', 2, 'pcs', 5, 20, 89.99),
        ('PRD-007', 'White Cotton T-Shirt M', 2, 'pcs', 25, 100, 19.99),
        ('PRD-008', 'Running Shoes Size 10', 2, 'pcs', 10, 30, 129.99),
        ('PRD-009', 'Organic Coffee Beans 1kg', 3, 'kg', 30, 100, 24.99),
        ('PRD-010', 'Green Tea Matcha 500g', 3, 'g', 20, 80, 18.99),
        ('PRD-011', 'Protein Bar Mixed 24pk', 3, 'pks', 15, 60, 34.99),
        ('PRD-012', 'M8 Bolt Set 100pcs', 4, 'set', 10, 40, 12.99),
        ('PRD-013', 'Power Drill 18V', 4, 'pcs', 5, 15, 199.99),
        ('PRD-014', 'A4 Paper 500 Sheet Ream', 5, 'ream', 50, 200, 8.99),
        ('PRD-015', 'Ballpoint Pen Box 50', 5, 'box', 20, 80, 14.99),
    ]
    quantities = [3, 45, 8, 0, 22, 4, 60, 11, 35, 2, 18, 9, 3, 120, 40]
    wh1 = Warehouse.query.first()
    for i, (sku, name, cat_id, unit, rp, rq, cost) in enumerate(products_data):
        p = Product(sku=sku, name=name, category_id=cat_id, unit=unit, reorder_point=rp, reorder_qty=rq, unit_cost=cost)
        db.session.add(p)
        db.session.flush()
        sl = StockLevel(product_id=p.id, warehouse_id=wh1.id, quantity=quantities[i])
        db.session.add(sl)

    db.session.commit()

    # Movements (last 7 days)
    products = Product.query.all()
    users_list = User.query.all()
    types = ['receive', 'transfer', 'adjust']
    for i in range(20):
        p = random.choice(products)
        m = StockMovement(
            type=random.choice(types),
            product_id=p.id,
            warehouse_id=wh1.id,
            quantity=random.randint(1, 50),
            reference=f'REF-{1000+i}',
            user_id=random.choice(users_list).id,
            created_at=datetime.utcnow() - timedelta(days=random.randint(0, 7))
        )
        db.session.add(m)
    db.session.commit()
    check_alerts()

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect('/dashboard')
    return redirect('/login')

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
    data = request.json
    user = User.query.filter_by(email=data.get('email')).first()
    if user and bcrypt.check_password_hash(user.password, data.get('password', '')):
        session['user_id'] = user.id
        user.last_login = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'user': {'id': user.id, 'name': user.name, 'email': user.email, 'role': user.role}})
    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

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
    total_value = sum(sl.quantity * sl.product.unit_cost for sl in StockLevel.query.all())
    low_stock = StockLevel.query.filter(StockLevel.quantity <= 10).count()
    active_alerts = Alert.query.filter_by(resolved=False).count()
    critical = Alert.query.filter_by(resolved=False, level='critical').count()
    warning = Alert.query.filter_by(resolved=False, level='warning').count()
    pending_po = PurchaseOrder.query.filter_by(status='pending').count()
    recent_movements = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(8).all()

    # Stock trend (last 7 days movements)
    trend = []
    for i in range(7):
        day = datetime.utcnow() - timedelta(days=6-i)
        count = StockMovement.query.filter(
            StockMovement.created_at >= day.replace(hour=0,minute=0,second=0),
            StockMovement.created_at < day.replace(hour=23,minute=59,second=59)
        ).count()
        trend.append({'day': day.strftime('%a'), 'count': count})

    return jsonify({
        'total_products': total_products,
        'total_value': round(total_value, 2),
        'low_stock': low_stock,
        'active_alerts': active_alerts,
        'critical': critical,
        'warning': warning,
        'pending_po': pending_po,
        'trend': trend,
        'recent_movements': [{'id': m.id, 'type': m.type, 'product': m.product.name,
            'qty': m.quantity, 'ref': m.reference,
            'time': m.created_at.strftime('%b %d, %H:%M')} for m in recent_movements]
    })

# ── Products API ──────────────────────────────────────────────────────────────

@app.route('/api/products')
@login_required
def api_products():
    search = request.args.get('q', '')
    cat_filter = request.args.get('cat', '')
    products = Product.query
    if search:
        products = products.filter(Product.name.ilike(f'%{search}%') | Product.sku.ilike(f'%{search}%'))
    if cat_filter:
        products = products.filter(Product.category_id == int(cat_filter))
    products = products.all()
    result = []
    for p in products:
        sl = StockLevel.query.filter_by(product_id=p.id).first()
        qty = sl.quantity if sl else 0
        if qty == 0: status = 'critical'
        elif qty <= p.reorder_point: status = 'warning'
        elif qty <= p.reorder_point * 1.5: status = 'low'
        else: status = 'ok'
        result.append({
            'id': p.id, 'sku': p.sku, 'name': p.name,
            'category': p.category.name if p.category else '',
            'cat_color': p.category.color if p.category else 'gray',
            'unit': p.unit, 'quantity': qty,
            'reorder_point': p.reorder_point, 'reorder_qty': p.reorder_qty,
            'unit_cost': p.unit_cost, 'value': round(qty * p.unit_cost, 2),
            'status': status
        })
    return jsonify(result)

@app.route('/api/products', methods=['POST'])
@login_required
def api_add_product():
    data = request.json
    p = Product(
        sku=data['sku'], name=data['name'],
        category_id=data.get('category_id'),
        unit=data.get('unit', 'pcs'),
        reorder_point=int(data.get('reorder_point', 10)),
        reorder_qty=int(data.get('reorder_qty', 50)),
        unit_cost=float(data.get('unit_cost', 0))
    )
    db.session.add(p)
    db.session.flush()
    wh = Warehouse.query.first()
    sl = StockLevel(product_id=p.id, warehouse_id=wh.id, quantity=int(data.get('initial_qty', 0)))
    db.session.add(sl)
    db.session.commit()
    check_alerts()
    return jsonify({'success': True, 'id': p.id})

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def api_delete_product(pid):
    p = Product.query.get_or_404(pid)
    StockLevel.query.filter_by(product_id=pid).delete()
    StockMovement.query.filter_by(product_id=pid).delete()
    Alert.query.filter_by(product_id=pid).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True})

# ── Stock Movement API ────────────────────────────────────────────────────────

@app.route('/api/stock/move', methods=['POST'])
@login_required
def api_stock_move():
    data = request.json
    pid = data['product_id']
    qty = int(data['quantity'])
    move_type = data['type']
    wh = Warehouse.query.first()
    sl = StockLevel.query.filter_by(product_id=pid, warehouse_id=wh.id).first()
    if not sl:
        sl = StockLevel(product_id=pid, warehouse_id=wh.id, quantity=0)
        db.session.add(sl)
    if move_type == 'receive':
        sl.quantity += qty
    elif move_type in ('transfer', 'writeoff', 'adjust'):
        sl.quantity = max(0, sl.quantity - qty) if move_type != 'adjust' else qty
    sl.updated_at = datetime.utcnow()
    m = StockMovement(type=move_type, product_id=pid, warehouse_id=wh.id,
                      quantity=qty, reference=data.get('reference', ''),
                      notes=data.get('notes', ''), user_id=session['user_id'])
    db.session.add(m)
    db.session.commit()
    check_alerts()
    return jsonify({'success': True, 'new_qty': sl.quantity})

@app.route('/api/movements')
@login_required
def api_movements():
    limit = int(request.args.get('limit', 50))
    movements = StockMovement.query.order_by(StockMovement.created_at.desc()).limit(limit).all()
    return jsonify([{
        'id': m.id, 'type': m.type,
        'product': m.product.name, 'sku': m.product.sku,
        'quantity': m.quantity, 'reference': m.reference,
        'notes': m.notes,
        'user': m.user.name if m.user else 'System',
        'time': m.created_at.strftime('%b %d %Y, %H:%M')
    } for m in movements])

# ── Alerts API ────────────────────────────────────────────────────────────────

@app.route('/api/alerts')
@login_required
def api_alerts():
    check_alerts()
    alerts = Alert.query.filter_by(resolved=False).order_by(Alert.created_at.desc()).all()
    return jsonify([{
        'id': a.id, 'level': a.level, 'message': a.message,
        'product': a.product.name, 'quantity': a.quantity,
        'time': a.created_at.strftime('%b %d, %H:%M')
    } for a in alerts])

@app.route('/api/alerts/<int:aid>/resolve', methods=['POST'])
@login_required
def api_resolve_alert(aid):
    a = Alert.query.get_or_404(aid)
    a.resolved = True
    a.resolved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

# ── Users API ─────────────────────────────────────────────────────────────────

@app.route('/api/users')
@login_required
def api_users():
    users = User.query.all()
    return jsonify([{
        'id': u.id, 'name': u.name, 'email': u.email, 'role': u.role,
        'active': u.active, 'created_at': u.created_at.strftime('%b %d %Y'),
        'last_login': u.last_login.strftime('%b %d %Y, %H:%M') if u.last_login else 'Never'
    } for u in users])

@app.route('/api/users', methods=['POST'])
@login_required
def api_add_user():
    data = request.json
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already exists'}), 400
    u = User(name=data['name'], email=data['email'],
             password=bcrypt.generate_password_hash(data.get('password', 'changeme')).decode(),
             role=data.get('role', 'operator'))
    db.session.add(u)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/users/<int:uid>/toggle', methods=['POST'])
@login_required
def api_toggle_user(uid):
    u = User.query.get_or_404(uid)
    u.active = not u.active
    db.session.commit()
    return jsonify({'success': True, 'active': u.active})

# ── Categories & Warehouses ───────────────────────────────────────────────────

@app.route('/api/categories')
@login_required
def api_categories():
    cats = Category.query.all()
    return jsonify([{'id': c.id, 'name': c.name, 'color': c.color} for c in cats])

@app.route('/api/warehouses')
@login_required
def api_warehouses():
    whs = Warehouse.query.all()
    return jsonify([{'id': w.id, 'name': w.name, 'location': w.location} for w in whs])

# ── Reports API ───────────────────────────────────────────────────────────────

@app.route('/api/reports/summary')
@login_required
def api_reports_summary():
    products = Product.query.all()
    by_cat = {}
    for p in products:
        cat = p.category.name if p.category else 'Other'
        sl = StockLevel.query.filter_by(product_id=p.id).first()
        qty = sl.quantity if sl else 0
        val = qty * p.unit_cost
        if cat not in by_cat:
            by_cat[cat] = {'count': 0, 'value': 0}
        by_cat[cat]['count'] += 1
        by_cat[cat]['value'] += val

    # ABC analysis
    stock_values = []
    for p in products:
        sl = StockLevel.query.filter_by(product_id=p.id).first()
        qty = sl.quantity if sl else 0
        stock_values.append({'name': p.name, 'value': round(qty * p.unit_cost, 2), 'qty': qty})
    stock_values.sort(key=lambda x: x['value'], reverse=True)
    total = sum(s['value'] for s in stock_values) or 1
    running = 0
    for s in stock_values:
        running += s['value']
        pct = running / total * 100
        s['abc'] = 'A' if pct <= 70 else ('B' if pct <= 90 else 'C')
        s['pct'] = round(pct, 1)

    return jsonify({'by_category': by_cat, 'abc': stock_values[:15]})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_data()
    app.run(debug=True, port=5000)
