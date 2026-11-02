"""Shop journey: a full purchase on a fake store, repeated with different data until the budget is used up.

The store at https://shop.poc.test is served by the check itself (FakeSite): a JSON catalog of 20 products,
a home page whose debounced search filters the catalog on the client, product pages, a cart kept in
localStorage and synced to the server with POST /api/cart, and a checkout with client-side validation, a
coupon code and POST /api/orders, whose order id is shown on a confirmation page.

Each round buys a different product mix and quantities (derived from the round number). It searches,
navigates back and forward, edits the cart and reloads it, trips the checkout's validation before placing
the order, and checks that the subtotal, discount, tax and total the page shows are the ones this check
computes, and that the order the fake server received is the one the check expects. This proves that a
grid browser runs a realistic single-page workload (scripts, storage, history, forms, fetch) reliably
for minutes on end.
"""
import collections
import re
from urllib.parse import quote, unquote, urlsplit

from playwright.sync_api import Browser, Page, Route, Request, expect

from conftest import FakeSite, html

ORIGIN = "https://shop.poc.test"
TIMEOUT_MS = 15 * 1000
TAX_BP = 825  # 8.25 %, in basis points
DEBOUNCE_MS = 250
MAX_QTY = 10
PRODUCTS = [
    {"id": "p{:02d}".format(i + 1), "name": name, "price": price, "category": category}
    for i, (name, price, category) in enumerate([
        ("Alpine Water Bottle", 1899, "outdoor"),
        ("Basalt Coffee Mug", 1250, "kitchen"),
        ("Cedar Cutting Board", 3475, "kitchen"),
        ("Delta Trail Backpack", 8900, "outdoor"),
        ("Ember Scented Candle", 1599, "home"),
        ("Fjord Wool Blanket", 6420, "home"),
        ("Granite Travel Mug", 2199, "kitchen"),
        ("Harbor Rain Jacket", 11950, "outdoor"),
        ("Indigo Linen Napkins", 2400, "kitchen"),
        ("Juniper Desk Lamp", 4599, "home"),
        ("Kestrel Camping Stove", 7325, "outdoor"),
        ("Lagoon Beach Towel", 2875, "outdoor"),
        ("Meadow Herb Planter", 3150, "home"),
        ("Nimbus Throw Pillow", 2699, "home"),
        ("Onyx Chef Knife", 9999, "kitchen"),
        ("Pebble Bluetooth Speaker", 5450, "home"),
        ("Quartz Wall Clock", 3825, "home"),
        ("Ridge Hiking Socks", 1475, "outdoor"),
        ("Summit Thermos Flask", 3299, "outdoor"),
        ("Tundra Picnic Blanket", 4150, "outdoor"),
    ])
]
BY_ID = {p["id"]: p for p in PRODUCTS}
COUPONS = {"SAVE10": ("percent", 10), "SAVE20": ("percent", 20), "TAKE5": ("fixed", 500)}
# Strides coprime with 20, so every round picks distinct products.
STRIDES = (3, 7, 9, 11, 13)

# --- The store's client code -------------------------------------------------------------------------
# Money is integer cents everywhere; rounding is half up, written so JS and Python agree exactly.

SHOP_JS = """
const TAX_BP = %(tax_bp)d, MAX_QTY = %(max_qty)d;
function money(c) {
  const sign = c < 0 ? '-' : '';
  c = Math.abs(c);
  return sign + '$' + Math.floor(c / 100) + '.' + String(c %% 100).padStart(2, '0');
}
function loadCart() { try { return JSON.parse(localStorage.getItem('cart')) || []; } catch (e) { return []; } }
function cartCount(cart) { return cart.reduce((n, l) => n + l.qty, 0); }
function renderBadge() { document.getElementById('cart-count').textContent = String(cartCount(loadCart())); }
let syncSeq = 0;
async function saveCart(cart) {
  localStorage.setItem('cart', JSON.stringify(cart));
  renderBadge();
  const seq = ++syncSeq, status = document.getElementById('sync-status');
  status.textContent = 'Saving...';
  const r = await fetch('/api/cart', {method: 'POST', headers: {'content-type': 'application/json'},
                                      body: JSON.stringify({items: cart})});
  const body = await r.json();
  if (seq === syncSeq) status.textContent = r.ok ? 'Saved, ' + body.count + ' in cart' : 'Sync failed';
}
async function getJson(url) { const r = await fetch(url); return {ok: r.ok, body: await r.json()}; }
async function catalogById() {
  const byId = {};
  for (const p of (await getJson('/api/products')).body.products) byId[p.id] = p;
  return byId;
}
function totals(cart, byId, coupon) {
  const subtotal = cart.reduce((s, l) => s + byId[l.id].price * l.qty, 0);
  let discount = 0;
  if (coupon) discount = coupon.kind === 'percent' ? Math.floor((subtotal * coupon.value + 50) / 100)
                                                   : Math.min(coupon.value, subtotal);
  const tax = Math.floor(((subtotal - discount) * TAX_BP + 5000) / 10000);
  return {subtotal, discount, tax, total: subtotal - discount + tax};
}
function showTotals(t) {
  for (const k of ['subtotal', 'discount', 'tax', 'total'])
    document.getElementById(k).textContent = money(k === 'discount' ? -t[k] : t[k]);
}
function ready() { document.body.dataset.ready = '1'; }
// A page restored from the back/forward cache keeps its old badge; refresh it.
window.addEventListener('pageshow', renderBadge);
""" % {"tax_bp": TAX_BP, "max_qty": MAX_QTY}

HEADER = "<header><a href='/'>POC Shop</a> | <a href='/cart'>Cart (<span id='cart-count'>0</span>)</a></header>"
TOTALS = ("<dl><dt>Subtotal</dt><dd id='subtotal'></dd><dt>Discount</dt><dd id='discount'></dd>"
          "<dt>Tax (8.25%)</dt><dd id='tax'></dd><dt>Total</dt><dd id='total'></dd></dl>")


def shop_page(title, body, script):
    return html(HEADER + "<main>" + body + "</main><script src='/static/shop.js'></script><script>"
                + script + "</script>", title=title + " - POC Shop")


HOME_PAGE = shop_page("Home", """
<h1>Products</h1>
<label>Search products <input id='search' type='search' autocomplete='off'></label>
<p id='result-info' role='status'></p><ul id='results'></ul>""", """
const input = document.getElementById('search'), list = document.getElementById('results');
let products = [], timer = null, renders = 0;
function render(q) {
  document.body.dataset.renders = String(++renders);
  const term = q.trim(), t = term.toLowerCase();
  const hits = products.filter(p => p.name.toLowerCase().includes(t));
  list.replaceChildren(...hits.map(p => {
    const li = document.createElement('li'), a = document.createElement('a'), price = document.createElement('span');
    a.href = '/product/' + p.id; a.textContent = p.name;
    price.className = 'price'; price.textContent = ' ' + money(p.price);
    li.append(a, price);
    return li;
  }));
  document.getElementById('result-info').textContent = hits.length === 0 ? 'No products match "' + term + '"'
    : hits.length + (hits.length === 1 ? ' product' : ' products') + (term ? ' for "' + term + '"' : '');
}
input.addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    const term = input.value.trim();
    history.replaceState(null, '', term ? '/?q=' + encodeURIComponent(term) : '/');
    render(input.value);
  }, %d);
});
(async () => {
  products = Object.values(await catalogById());
  input.value = new URLSearchParams(location.search).get('q') || '';
  render(input.value);
  ready();
})();""" % DEBOUNCE_MS)

PRODUCT_PAGE = shop_page("Product", """
<h1 id='title'>Loading...</h1><p id='price'></p>
<div id='buy'><label>Quantity <input id='qty' type='number' min='1' value='1'></label>
<button id='add' type='button'>Add to cart</button></div>
<p id='added' role='status'></p><p id='sync-status'></p>""", """
(async () => {
  const id = decodeURIComponent(location.pathname.split('/').pop());
  const r = await getJson('/api/products/' + encodeURIComponent(id));
  const title = document.getElementById('title'), added = document.getElementById('added');
  if (!r.ok) { title.textContent = 'Product not found'; document.getElementById('buy').hidden = true; return; }
  const p = r.body;
  document.getElementById('add').onclick = () => {
    const qty = Number(document.getElementById('qty').value);
    if (!Number.isInteger(qty) || qty < 1 || qty > MAX_QTY) {
      added.textContent = 'Choose a quantity from 1 to ' + MAX_QTY;
      return;
    }
    const cart = loadCart(), line = cart.find(l => l.id === p.id);
    if (line) line.qty += qty; else cart.push({id: p.id, qty});
    added.textContent = 'Added ' + qty + ' x ' + p.name;
    saveCart(cart);
  };
  document.getElementById('price').textContent = money(p.price);
  title.textContent = p.name;
  ready();
})();""")

CART_PAGE = shop_page("Cart", """
<h1>Your cart</h1><p id='empty' hidden>Your cart is empty</p>
<table><tbody id='cart-lines'></tbody></table>
<p>Subtotal: <span id='cart-subtotal'></span></p><p id='sync-status'></p>
<a href='/checkout'>Checkout</a>""", """
let byId = {};
function renderCart() {
  const cart = loadCart();
  document.getElementById('cart-lines').replaceChildren(...cart.map(l => {
    const p = byId[l.id], tr = document.createElement('tr');
    tr.dataset.id = l.id;
    tr.innerHTML = '<td class="name"></td><td class="unit"></td><td><input type="number" min="1"></td>'
      + '<td class="line-total"></td><td><button type="button">Update</button> <button type="button">Remove</button></td>';
    tr.querySelector('.name').textContent = p.name;
    tr.querySelector('.unit').textContent = money(p.price);
    tr.querySelector('.line-total').textContent = money(p.price * l.qty);
    const qty = tr.querySelector('input'), [update, remove] = tr.querySelectorAll('button');
    qty.value = String(l.qty);
    qty.setAttribute('aria-label', 'Quantity for ' + p.name);
    update.onclick = () => {
      const n = Number(qty.value);
      if (!Number.isInteger(n) || n < 1 || n > MAX_QTY) return;
      const cart = loadCart();
      cart.find(x => x.id === l.id).qty = n;
      saveCart(cart);
      renderCart();
    };
    remove.onclick = () => { saveCart(loadCart().filter(x => x.id !== l.id)); renderCart(); };
    return tr;
  }));
  document.getElementById('empty').hidden = cart.length > 0;
  document.getElementById('cart-subtotal').textContent = money(totals(cart, byId, null).subtotal);
}
(async () => { byId = await catalogById(); renderCart(); ready(); })();""")

CHECKOUT_PAGE = shop_page("Checkout", """
<h1>Checkout</h1><ul id='summary'></ul>
<div><label>Coupon code <input id='coupon' autocomplete='off'></label>
<button id='apply' type='button'>Apply</button><p id='coupon-msg' role='status'></p></div>
""" + TOTALS + """
<form id='order-form' novalidate>
<p><label>Full name <input id='name'></label></p><p class='error' id='name-error'></p>
<p><label>Email <input id='email' type='email'></label></p><p class='error' id='email-error'></p>
<p><label>Address <textarea id='address'></textarea></label></p><p class='error' id='address-error'></p>
<button type='submit'>Place order</button><p id='order-msg' role='alert'></p>
</form>""", """
const EMAIL = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;
let byId = {}, coupon = null;
const cart = loadCart(), msg = document.getElementById('coupon-msg');
const refresh = () => showTotals(totals(cart, byId, coupon));
document.getElementById('apply').onclick = async () => {
  const code = document.getElementById('coupon').value.trim().toUpperCase();
  if (!code) { msg.textContent = 'Enter a coupon code'; return; }
  const r = await getJson('/api/coupons/' + encodeURIComponent(code));
  coupon = r.ok ? r.body : null;
  msg.textContent = !r.ok ? 'Coupon ' + code + ' is not valid'
    : code + ' applied: ' + (coupon.kind === 'percent' ? coupon.value + '%' : money(coupon.value)) + ' off';
  refresh();
};
document.getElementById('order-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = {};
  for (const k of ['name', 'email', 'address']) v[k] = document.getElementById(k).value.trim();
  const errors = {name: v.name ? '' : 'Name is required',
                  email: !v.email ? 'Email is required' : EMAIL.test(v.email) ? '' : 'Enter a valid email address',
                  address: v.address ? '' : 'Address is required'};
  let first = null;
  for (const k of Object.keys(errors)) {
    const field = document.getElementById(k);
    document.getElementById(k + '-error').textContent = errors[k];
    field.setAttribute('aria-invalid', errors[k] ? 'true' : 'false');
    if (errors[k] && !first) first = field;
  }
  if (first) { first.focus(); return; }
  const button = e.submitter, out = document.getElementById('order-msg');
  button.disabled = true;
  const r = await fetch('/api/orders', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({customer: v, items: cart, coupon: coupon ? coupon.code : null,
                          totals: totals(cart, byId, coupon)})});
  const body = await r.json();
  if (!r.ok) { out.textContent = body.error; button.disabled = false; return; }
  localStorage.removeItem('cart');
  location.assign('/order/' + encodeURIComponent(body.orderId));
});
(async () => {
  byId = await catalogById();
  document.getElementById('summary').replaceChildren(...cart.map(l => {
    const li = document.createElement('li'), p = byId[l.id];
    li.textContent = l.qty + ' x ' + p.name + ': ' + money(p.price * l.qty);
    return li;
  }));
  refresh();
  ready();
})();""")

ORDER_PAGE = shop_page("Order", "<h1 id='title'>Loading...</h1><p id='item-count'></p>" + TOTALS
                       + "<a href='/'>Continue shopping</a>", """
(async () => {
  const id = decodeURIComponent(location.pathname.split('/').pop());
  const r = await getJson('/api/orders/' + encodeURIComponent(id));
  if (!r.ok) { document.getElementById('title').textContent = 'Order not found'; return; }
  document.getElementById('item-count').textContent = r.body.units + ' items';
  showTotals(r.body.totals);
  document.getElementById('title').textContent = 'Order ' + id + ' confirmed';
  ready();
})();""")


# --- Money, as this check computes it ----------------------------------------------------------------

def money(cents):
    return "{}${}.{:02d}".format("-" if cents < 0 else "", abs(cents) // 100, abs(cents) % 100)


def order_totals(items, coupon):
    subtotal = sum(BY_ID[i["id"]]["price"] * i["qty"] for i in items)
    discount = 0
    if coupon:
        kind, value = COUPONS[coupon]
        discount = (subtotal * value + 50) // 100 if kind == "percent" else min(value, subtotal)
    tax = ((subtotal - discount) * TAX_BP + 5000) // 10000
    return {"subtotal": subtotal, "discount": discount, "tax": tax, "total": subtotal - discount + tax}


def units(items):
    return sum(i["qty"] for i in items)


# --- The store's server ------------------------------------------------------------------------------

class Shop:
    """The fake store's server for one round: pages, catalog, cart sync, coupons and orders."""

    def __init__(self, round_no):
        self.round_no = round_no
        self.cart = []
        self.cart_posts = 0
        self.orders = {}

    def handle(self, route: Route, request: Request):
        path, method = unquote(urlsplit(request.url).path), request.method
        if method == "GET":
            page = self._page(path)
            if page:
                return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page)
            if path == "/static/shop.js":
                return route.fulfill(status=200, content_type="text/javascript; charset=utf-8", body=SHOP_JS)
            if path == "/api/products":
                return reply(route, 200, {"products": PRODUCTS})
            m = re.fullmatch(r"/api/products/(.+)", path)
            if m:
                product = BY_ID.get(m.group(1))
                return reply(route, 200, product) if product else reply(route, 404, {"error": "no such product"})
            m = re.fullmatch(r"/api/coupons/(.+)", path)
            if m:
                if m.group(1) not in COUPONS:
                    return reply(route, 404, {"error": "no such coupon"})
                kind, value = COUPONS[m.group(1)]
                return reply(route, 200, {"code": m.group(1), "kind": kind, "value": value})
            m = re.fullmatch(r"/api/orders/(.+)", path)
            if m and m.group(1) in self.orders:
                order = self.orders[m.group(1)]
                return reply(route, 200, {"units": units(order["items"]),
                                          "totals": order_totals(order["items"], order["coupon"])})
        elif method == "POST" and path == "/api/cart":
            self.cart_posts += 1
            self.cart = request.post_data_json["items"]
            return reply(route, 200, {"count": units(self.cart)})
        elif method == "POST" and path == "/api/orders":
            return self._place_order(route, request.post_data_json)
        route.fulfill(status=404, content_type="text/plain", body="not found")

    @staticmethod
    def _page(path):
        if path == "/":
            return HOME_PAGE
        if path in ("/cart", "/checkout"):
            return CART_PAGE if path == "/cart" else CHECKOUT_PAGE
        if re.fullmatch(r"/product/[^/]+", path):
            return PRODUCT_PAGE
        if re.fullmatch(r"/order/[^/]+", path):
            return ORDER_PAGE
        return None

    def _place_order(self, route, order):
        items = order.get("items") or []
        if not items or any(i.get("id") not in BY_ID or not 1 <= i.get("qty", 0) <= MAX_QTY for i in items):
            return reply(route, 422, {"error": "invalid items"})
        if order.get("coupon") is not None and order["coupon"] not in COUPONS:
            return reply(route, 422, {"error": "invalid coupon"})
        order_id = "ORD-{:04d}-{:02d}".format(self.round_no, len(self.orders) + 1)
        self.orders[order_id] = order
        self.cart = []
        reply(route, 201, {"orderId": order_id})


def reply(route, status, body):
    route.fulfill(status=status, json=body)


# --- What one round does -----------------------------------------------------------------------------

class Plan:
    """The data of round `r`: 2-4 products with quantities, a decoy that is added and removed again, a
    quantity bump on the cart page, a coupon (or none) and the customer."""

    def __init__(self, r):
        start, stride, count = (r * 7) % len(PRODUCTS), STRIDES[r % len(STRIDES)], 2 + r % 3
        picks = [PRODUCTS[(start + k * stride) % len(PRODUCTS)] for k in range(count + 1)]
        self.lines = [(p, 1 + (r + 2 * k) % 3) for k, p in enumerate(picks[:count])]
        self.decoy = picks[count]
        self.bump = 1 + r % 2
        self.coupon = ("SAVE10", "TAKE5", None, "SAVE20")[r % 4]
        self.bad_coupon = "nope{}".format(r)
        self.customer = {"name": "Round {} Buyer".format(r), "email": "buyer{}@poc.test".format(r),
                         "address": "{} Test Street, Poc City".format(100 + r)}
        self.bad_email = ("buyer{}poc.test", "buyer{}@poc", "buyer {}@poc.test")[r % 3].format(r)
        self.round_no = r

    def term(self, product, k):
        """What to type into the search to find `product`; the kind of term varies per round and step."""
        words = product["name"].split()
        kind = (self.round_no + k) % 4
        if kind == 0:
            return words[0].lower()
        if kind == 1:
            return words[0][:4].upper()
        if kind == 2:
            return product["name"].lower()
        return words[-1].lower()  # often matches several products ("mug", "blanket")

    def final_items(self):
        items = [{"id": p["id"], "qty": q} for p, q in self.lines]
        items[0]["qty"] += self.bump
        return items


def search_hits(term):
    return [p for p in PRODUCTS if term.strip().lower() in p["name"].lower()]


def result_info(term, hits):
    if not hits:
        return 'No products match "{}"'.format(term)
    found = "{} product{}".format(len(hits), "" if len(hits) == 1 else "s")
    return found + (' for "{}"'.format(term) if term else "")


def wait_ready(page: Page, what):
    expect(page.locator("body"), "{} never finished loading".format(what)).to_have_attribute("data-ready", "1")


def expect_badge(page: Page, items, where):
    expect(page.locator("#cart-count"), "cart badge on {}".format(where)).to_have_text(str(units(items)))


def expect_results(page: Page, term):
    hits = search_hits(term)
    expect(page.locator("#result-info"), "search info for '{}'".format(term)).to_have_text(result_info(term, hits))
    expect(page.locator("#results a"), "search results for '{}'".format(term)).to_have_text(
        [p["name"] for p in hits])
    return hits


def search(page: Page, term, typed=False):
    box = page.get_by_label("Search products")
    if typed:
        # Type key by key, like a person; the debounce must turn that into far fewer renders. Clearing the
        # box schedules a debounced render of its own: wait for it, so it is never counted as typing.
        box.fill("")
        expect_results(page, "")
        expect(page, "cleared search not reflected in the URL").to_have_url(ORIGIN + "/")
        before = int(page.locator("body").get_attribute("data-renders"))
        box.press_sequentially(term, delay=30)
    else:
        box.fill(term)
    expect_results(page, term)
    expect(page, "search '{}' not kept in the URL".format(term)).to_have_url(ORIGIN + "/?q=" + quote(term, safe=""))
    if typed:
        renders = int(page.locator("body").get_attribute("data-renders")) - before
        assert 1 <= renders < len(term), "typing '{}' ({} keys) rendered the results {} times; the search is not " \
                                         "debounced".format(term, len(term), renders)


def expect_home(page: Page, term, cart, how):
    wait_ready(page, "home page after " + how)
    expect(page, "URL after " + how).to_have_url(ORIGIN + "/?q=" + quote(term, safe=""))
    expect(page.get_by_label("Search products"), "search box after " + how).to_have_value(term)
    expect_results(page, term)
    expect_badge(page, cart, "home page after " + how)


def expect_product(page: Page, product):
    expect(page.locator("h1"), "product page heading").to_have_text(product["name"])
    wait_ready(page, "product page of " + product["name"])
    expect(page.locator("#price"), "price of " + product["name"]).to_have_text(money(product["price"]))


def reject_bad_quantity(page: Page, shop: Shop, cart):
    posts = shop.cart_posts
    page.get_by_label("Quantity").fill("0")
    page.get_by_role("button", name="Add to cart").click()
    expect(page.locator("#added"), "quantity 0 was not rejected").to_have_text(
        "Choose a quantity from 1 to {}".format(MAX_QTY))
    expect_badge(page, cart, "product page after a rejected quantity")
    assert shop.cart_posts == posts, "a rejected quantity still synced the cart"


def sync_cart(page: Page, shop: Shop, action, cart, what):
    """Run `action` (a click that changes the cart) and check that the cart reached the server."""
    with page.expect_response(lambda r: r.url == ORIGIN + "/api/cart" and r.request.method == "POST") as synced:
        action()
    assert synced.value.ok, "cart sync after {} returned HTTP {}".format(what, synced.value.status)
    expect(page.locator("#sync-status"), "sync status after " + what).to_have_text(
        "Saved, {} in cart".format(units(cart)))
    expect_badge(page, cart, "page after " + what)
    assert shop.cart == cart, "server cart after {} is {}, expected {}".format(what, shop.cart, cart)


def add_to_cart(page: Page, shop: Shop, product, qty, cart):
    cart.append({"id": product["id"], "qty": qty})
    page.get_by_label("Quantity").fill(str(qty))
    sync_cart(page, shop, page.get_by_role("button", name="Add to cart").click, cart,
              "adding {} x {}".format(qty, product["name"]))
    expect(page.locator("#added"), "add confirmation").to_have_text("Added {} x {}".format(qty, product["name"]))


def expect_cart_page(page: Page, cart, how):
    wait_ready(page, "cart page after " + how)
    rows = page.locator("#cart-lines tr")
    expect(rows, "cart lines after " + how).to_have_count(len(cart))
    for i, line in enumerate(cart):
        product, row = BY_ID[line["id"]], rows.nth(i)
        expect(row, "cart line {} after {}".format(i + 1, how)).to_have_attribute("data-id", line["id"])
        expect(row.locator(".name"), "cart line name").to_have_text(product["name"])
        expect(row.locator(".unit"), "unit price of " + product["name"]).to_have_text(money(product["price"]))
        expect(row.get_by_role("spinbutton"), "quantity of " + product["name"]).to_have_value(str(line["qty"]))
        expect(row.locator(".line-total"), "line total of " + product["name"]).to_have_text(
            money(product["price"] * line["qty"]))
    expect(page.locator("#cart-subtotal"), "cart subtotal after " + how).to_have_text(
        money(order_totals(cart, None)["subtotal"]))
    expect_badge(page, cart, "cart page after " + how)
    stored = page.evaluate("() => JSON.parse(localStorage.getItem('cart'))")
    assert stored == cart, "localStorage cart after {} is {}, expected {}".format(how, stored, cart)


def expect_totals(page: Page, totals, where):
    for key in ("subtotal", "discount", "tax", "total"):
        shown = money(-totals[key] if key == "discount" else totals[key])
        expect(page.locator("#" + key), "{} on {}".format(key, where)).to_have_text(shown)


def apply_coupon(page: Page, code, cart, coupon_after, message):
    page.get_by_label("Coupon code").fill(code)
    with page.expect_response(lambda r: "/api/coupons/" in r.url):
        page.get_by_role("button", name="Apply").click()
    expect(page.locator("#coupon-msg"), "message for coupon " + code).to_have_text(message)
    expect_totals(page, order_totals(cart, coupon_after), "checkout after coupon " + code)


def coupon_message(code):
    kind, value = COUPONS[code]
    return "{} applied: {} off".format(code, "{}%".format(value) if kind == "percent" else money(value))


def fail_validation(page: Page, site: FakeSite, plan: Plan):
    """Submit the checkout twice with bad input; both must show the right errors and send nothing."""
    submit = page.get_by_role("button", name="Place order")
    submit.click()
    for field, message in (("name", "Name is required"), ("email", "Email is required"),
                           ("address", "Address is required")):
        expect(page.locator("#{}-error".format(field)), "empty-form error for " + field).to_have_text(message)
        expect(page.locator("#" + field), field + " not marked invalid").to_have_attribute("aria-invalid", "true")
    expect(page.get_by_label("Full name"), "focus after an empty submit").to_be_focused()

    page.get_by_label("Full name").fill(plan.customer["name"])
    page.get_by_label("Address").fill(plan.customer["address"])
    page.get_by_label("Email", exact=True).fill(plan.bad_email)
    submit.click()
    expect(page.locator("#email-error"), "error for email '{}'".format(plan.bad_email)).to_have_text(
        "Enter a valid email address")
    expect(page.locator("#name-error"), "name error not cleared").to_have_text("")
    expect(page.locator("#address-error"), "address error not cleared").to_have_text("")
    expect(page.get_by_label("Email", exact=True), "focus after a bad email").to_be_focused()
    assert site.count("POST", "/api/orders") == 0, "an invalid checkout form still posted an order"
    return 4  # validation errors shown


def shop_round(browser: Browser, capture, r, stats):
    plan, shop = Plan(r), Shop(r)
    context = browser.new_context()
    context.set_default_timeout(TIMEOUT_MS)
    outside = []

    def block_outside(route, request):
        # Registered before the store, so it only sees requests that are not for the store.
        outside.append(request.url)
        route.abort()

    context.route("**/*", block_outside)
    site = FakeSite(context, ORIGIN, shop.handle)
    page = capture.add(context.new_page())

    page.goto(site.url("/product/p99"))
    expect(page.locator("h1"), "unknown product page").to_have_text("Product not found")
    expect(page.get_by_role("button", name="Add to cart"), "buy button on an unknown product").to_be_hidden()

    page.goto(site.url("/"))
    wait_ready(page, "home page")
    expect_results(page, "")
    search(page, "xyz{}".format(r))
    stats["searches"] += 1

    # Find, open and add each product, then come back to the results.
    cart = []
    for k, (product, qty) in enumerate(plan.lines + [(plan.decoy, 1)]):
        term = plan.term(product, k)
        search(page, term, typed=(k == 0))
        stats["searches"] += 1
        page.get_by_role("link", name=product["name"], exact=True).click()
        expect_product(page, product)
        if k == 0:
            reject_bad_quantity(page, shop, cart)
        add_to_cart(page, shop, product, qty, cart)
        page.go_back()
        expect_home(page, term, cart, "going back from " + product["name"])
        stats["history"] += 1
        if k == 0:
            page.go_forward()
            expect_product(page, product)
            expect_badge(page, cart, "product page after going forward")
            page.go_back()
            expect_home(page, term, cart, "going back again")
            stats["history"] += 2

    # Cart: bump the first line, drop the decoy, and reload; everything must survive.
    page.get_by_role("link", name=re.compile(r"^Cart \(")).click()
    expect_cart_page(page, cart, "opening the cart")
    first, decoy = cart[0], cart[-1]
    first_row = page.locator("#cart-lines tr[data-id='{}']".format(first["id"]))
    first["qty"] += plan.bump
    first_row.get_by_role("spinbutton").fill(str(first["qty"]))
    sync_cart(page, shop, first_row.get_by_role("button", name="Update").click, cart, "updating a quantity")
    cart.remove(decoy)
    decoy_row = page.locator("#cart-lines tr[data-id='{}']".format(decoy["id"]))
    sync_cart(page, shop, decoy_row.get_by_role("button", name="Remove").click, cart, "removing the decoy")
    expect_cart_page(page, cart, "editing the cart")
    page.reload()
    expect_cart_page(page, cart, "a reload")
    stats["reloads"] += 1
    assert cart == plan.final_items(), "cart {} differs from the plan {}".format(cart, plan.final_items())

    # Checkout: a wrong coupon, maybe a right one, validation errors, then the order.
    page.get_by_role("link", name="Checkout").click()
    wait_ready(page, "checkout page")
    expect(page.locator("#summary li"), "checkout summary").to_have_text(
        ["{} x {}: {}".format(i["qty"], BY_ID[i["id"]]["name"], money(BY_ID[i["id"]]["price"] * i["qty"]))
         for i in cart])
    expect_totals(page, order_totals(cart, None), "checkout")
    apply_coupon(page, plan.bad_coupon, cart, None, "Coupon {} is not valid".format(plan.bad_coupon.upper()))
    if plan.coupon:
        apply_coupon(page, plan.coupon.lower(), cart, plan.coupon, coupon_message(plan.coupon))
        stats["coupons"] += 1
    stats["validation_errors"] += fail_validation(page, site, plan)

    page.get_by_label("Email", exact=True).fill(plan.customer["email"])
    with page.expect_response(lambda resp: resp.url == ORIGIN + "/api/orders") as placed:
        page.get_by_role("button", name="Place order").click()
    # The page navigates away at once, which discards the response body, so the id comes from the server.
    assert placed.value.status == 201, "POST /api/orders returned HTTP {}".format(placed.value.status)
    order_id = "ORD-{:04d}-01".format(r)
    assert list(shop.orders) == [order_id], "the server holds orders {}, expected {}".format(list(shop.orders), order_id)
    expected = order_totals(cart, plan.coupon)
    received = shop.orders[order_id]
    want = {"customer": plan.customer, "items": cart, "coupon": plan.coupon, "totals": expected}
    assert received == want, "the server received order {}, expected {}".format(received, want)

    # Confirmation: the server's own totals, an empty cart everywhere.
    expect(page, "no confirmation page after ordering").to_have_url(site.url("/order/" + order_id))
    expect(page.locator("h1"), "confirmation heading").to_have_text("Order {} confirmed".format(order_id))
    expect(page.locator("#item-count"), "confirmed item count").to_have_text("{} items".format(units(cart)))
    expect_totals(page, expected, "confirmation page")
    expect_badge(page, [], "confirmation page")
    assert page.evaluate("() => localStorage.getItem('cart')") is None, "cart not cleared after the order"
    assert shop.cart == [], "server cart not cleared after the order: {}".format(shop.cart)
    page.get_by_role("link", name="Continue shopping").click()
    wait_ready(page, "home page after the order")
    expect_badge(page, [], "home page after the order")

    assert site.count("POST", "/api/orders") == 1, "expected exactly one order post this round"
    assert not outside, "the store page tried to reach outside the fake origin: {}".format(outside)
    stats["orders"] += 1
    stats["lines"] += len(cart)
    stats["units"] += units(cart)
    stats["cart_syncs"] += shop.cart_posts
    stats["requests"] += len(site.requests)
    stats["revenue"] += expected["total"]
    context.close()


def test_shop_journey(grid_browser, budget, capture):
    expect.set_options(timeout=TIMEOUT_MS)
    stats = collections.Counter()
    # A round takes about 10-15 s on the grid, so finishing the round in progress stays well inside the
    # 20 s the check may run past its budget.
    while budget.more():
        shop_round(grid_browser, capture, budget.rounds, stats)
    print("shop journey: {} rounds in {:.0f}s, {orders} orders, {lines} lines, {units} units, {revenue_s} total, "
          "{searches} searches, {history} back/forward, {reloads} reloads, {cart_syncs} cart syncs, {coupons} "
          "coupons, {validation_errors} validation errors, {requests} requests served".format(
              budget.rounds, budget.elapsed(), revenue_s=money(stats["revenue"]),
              **{k: stats[k] for k in ("orders", "lines", "units", "searches", "history", "reloads", "cart_syncs",
                                       "coupons", "validation_errors", "requests")}))
