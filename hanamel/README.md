# Hanamel — Timber Sales & Inventory Tracker

Internal tool for Hanamel Timber: records sales, tracks stock, runs custom
(made-to-order) jobs with printable quotations, reconciles M-Pesa payments,
and exports monthly reports for the accountant. Staff-only (login required);
installable on a phone as a PWA.

## Setup (local development)
Requires Python 3.12+.

    python -m venv venv
    source venv/bin/activate        # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    export DJANGO_DEBUG=True        # Windows: set DJANGO_DEBUG=True
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py seed_products --species Cypress --rate 280   # optional
    python manage.py runserver

Then open http://127.0.0.1:8000/ and log in. Admin is at /admin/ — set the
invoice/quotation details (name, logo, KRA PIN, Till/Paybill, quote terms,
signee) under **Business profile**.

`DEBUG` is **off by default**, so every terminal you use for `runserver`,
`migrate` or `test` needs `export DJANGO_DEBUG=True` first. Without it Django
refuses to start (no `DJANGO_SECRET_KEY`) — that's deliberate, so a server
with a missing env file fails loudly instead of running in debug mode.

## Features
- **Dashboard** — at-a-glance stock and sales.
- **Products & stock** — stock take page pre-filled with current counts; every
  change is logged as a `StockMovement` with per-product history.
- **Sales** — three kinds of line:
  - *Catalog* items (timber or finished goods).
  - *Custom cuts* — one-off sizes not in the catalog, priced from the species'
    board-foot rate.
  - *Recuts* — a cracked piece sold at smaller dimensions. Priced at the sold
    size, stock comes off the source piece, and the wasted board feet/value are
    logged as a `RecutLoss` (see Products → Recut losses).

  Prices are always recomputed server-side; stock is locked and decremented in
  one transaction so a sale can't oversell. Sales are numbered `SAL-YYYY-0001`.
- **Doors** — ready-made doors kept in stock, each with height × width ×
  thickness in mm and a fixed price. The Add door form offers standard sizes
  (Kenya 2100 mm range incl. doubles, UK/EU, US) or a custom size, with a live
  proportional preview. Doors appear on the sale form, stock update and
  invoices like any other product. Odd one-off sizes go through Custom orders.
  Standard sizes live in `DOOR_STANDARD_SIZES` (inventory/models.py).
- **Invoices** — print-friendly, rendered on demand from the sale record.
- **Payments** — cash, M-Pesa or bank; partial payments and balances tracked.
- **Custom orders** — doors, tables, etc. Quoted → In progress → Ready →
  Collected (or Cancelled). Priced line items, deposits/payments kept separate
  from sales, and a printable quotation.
- **Reports** — per-month `.xlsx` (summary sheet + line items) and a monthly
  invoice pack (all invoices on one page → one PDF via browser print).
- **M-Pesa (Daraja C2B)** — see below.

## Data model (inventory/models.py)
- **Product** — timber or finished (`type` field).
  - Timber: width & thickness in INCHES, length in FEET, `rate_per_board_foot`.
    Price per piece = board feet × rate, where
    board feet = (width × thickness × length) / 12.
  - Finished: fixed `price`.
- **Sale / SaleItem** — items snapshot `rate` and `board_feet_per_piece` so past
  sales stay accurate when prices change. `custom_description` is set for
  custom cuts (no product).
- **Payment** — money received against a sale.
- **StockMovement** — ledger of every stock change (received, sold, count, correction).
- **RecutLoss** — offcut lost when a piece is sold smaller than stocked.
- **CustomOrder / CustomOrderItem / CustomOrderPayment** — made-to-order jobs.
- **MpesaTransaction** — raw Safaricom callbacks, matched to sales separately.
- **BusinessProfile** — single row of details printed on invoices/quotations.

## Units — IMPORTANT
Pricing uses board feet (inches × inches × feet / 12). Cubic metres are
computed too (`volume_m3`) for customers who ask, using INCH_TO_M / FOOT_TO_M
in inventory/models.py.

Sanity check: a 6x2x9 piece = 9 board feet. At 280/bd ft that's 2,520.

## Seeding the common sizes
    python manage.py seed_products --species Cypress --rate 280

Sizes are WIDTH x THICKNESS in inches, LENGTH in feet (6x2x9 = 6" x 2" x 9ft).
Re-running with a new `--rate` updates existing rows instead of duplicating
them, so it's also how you apply a price change. Sizes live in
inventory/management/commands/seed_products.py (`COMMON_SIZES`).

Loaded sizes: 12x2x9, 12x2x18, 12x4x18, 8x4x9, 8x4x18, 6x4x9, 6x4x18, 6x2x9, 6x2x18.
12x4x9 is commented out — uncomment if the yard stocks it.

## Other management commands
- `python manage.py make_icons` — builds favicons, app icons and the nav mark
  from `brand/logo-source.png`. Re-run when the logo changes, then collectstatic.
- `python manage.py register_mpesa --host https://your-domain` — registers the
  M-Pesa callback URLs with Safaricom (https only).

## M-Pesa
Customers pay the Paybill/Till and enter the invoice number as the account.
Safaricom POSTs to `/mpesa/confirmation/`; the transaction is stored first,
then auto-matched to the sale. Unmatched payments appear on the M-Pesa
reconcile page for manual matching. The M-Pesa nav link stays hidden until
credentials are configured.

## Environment variables
| Variable | Purpose |
|---|---|
| `DJANGO_DEBUG` | Off unless set to `True`. Set `True` locally only |
| `DJANGO_SECRET_KEY` | Required whenever DEBUG is off |
| `DJANGO_ALLOWED_HOST` | e.g. `hanamel.alvinn.tech` (also sets CSRF trusted origin) |
| `DJANGO_HSTS_SECONDS` | HSTS max-age, default `3600`; raise to `31536000` once HTTPS is confirmed |
| `DATABASE_URL` | e.g. `sqlite:////var/lib/hanamel/db.sqlite3`; repo `db.sqlite3` when unset |
| `DJANGO_MEDIA_ROOT` | Uploads (logo, order photos); repo `media/` when unset |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | Daraja credentials; enables M-Pesa |
| `MPESA_SHORTCODE` | Paybill / Till number |
| `MPESA_ENV` | `sandbox` (default) or `live` |
| `MPESA_ALLOWED_IPS` | Optional comma-separated allowlist for callbacks |

See `.env.example` in the repo root.

## Tests
    python manage.py test inventory

## Deployment (EC2: Ubuntu, Gunicorn, Nginx, Certbot)
Live at https://hanamel.alvinn.tech. Code in `/srv/hanamel` (the Django project
is the `hanamel/` subfolder), data in `/var/lib/hanamel`, settings in
`/etc/hanamel.env`, all run as the `hanamel` user.

**1. Code, venv, data folders**

    sudo apt install python3-venv nginx certbot python3-certbot-nginx
    sudo useradd --system --home /srv/hanamel hanamel
    sudo git clone https://github.com/Officialvinn/Hanamel.git /srv/hanamel
    sudo mkdir -p /var/lib/hanamel/media
    sudo chown -R hanamel:hanamel /srv/hanamel /var/lib/hanamel
    sudo -u hanamel python3 -m venv /srv/hanamel/hanamel/venv
    sudo -u hanamel /srv/hanamel/hanamel/venv/bin/pip install -r /srv/hanamel/hanamel/requirements.txt

**2. `/etc/hanamel.env`** (no quotes around values), readable by root and the
`hanamel` group only: `sudo chown root:hanamel /etc/hanamel.env && sudo chmod 640 /etc/hanamel.env`

    DJANGO_DEBUG=False
    DJANGO_SECRET_KEY=<python3 -c "import secrets;print(secrets.token_urlsafe(50))">
    DJANGO_ALLOWED_HOST=hanamel.alvinn.tech
    DATABASE_URL=sqlite:////var/lib/hanamel/db.sqlite3
    DJANGO_MEDIA_ROOT=/var/lib/hanamel/media

**3. Running manage.py on the server.** systemd gives the env file to
gunicorn only — a plain SSH shell doesn't have it, and without it Django
refuses to start. Always load it:

    sudo -u hanamel bash -c 'set -a; . /etc/hanamel.env; set +a; cd /srv/hanamel/hanamel && venv/bin/python manage.py migrate'

Do the same for `collectstatic --no-input` and `createsuperuser`.

**4. `/etc/systemd/system/hanamel.service`**

    [Unit]
    Description=Hanamel (gunicorn)
    After=network.target

    [Service]
    User=hanamel
    Group=hanamel
    WorkingDirectory=/srv/hanamel/hanamel
    EnvironmentFile=/etc/hanamel.env
    ExecStart=/srv/hanamel/hanamel/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

Then `sudo systemctl daemon-reload && sudo systemctl enable --now hanamel`.
Logs, including 500 tracebacks: `journalctl -u hanamel`.

**5. Nginx** (`/etc/nginx/sites-available/hanamel`, symlinked into `sites-enabled`)

    server {
        server_name hanamel.alvinn.tech;
        client_max_body_size 10M;
        location /media/ { alias /var/lib/hanamel/media/; }
        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }
    }

`X-Forwarded-Proto` is required — without it Django's HTTPS redirect loops.
Static files are served by WhiteNoise, so no `/static/` block is needed.
Then `sudo nginx -t && sudo systemctl reload nginx` and
`sudo certbot --nginx -d hanamel.alvinn.tech` (adds the 443 block and the
HTTP→HTTPS redirect).

**6. Updating**

    cd /srv/hanamel && sudo -u hanamel git pull
    sudo -u hanamel /srv/hanamel/hanamel/venv/bin/pip install -r hanamel/requirements.txt
    # then migrate + collectstatic --no-input as in step 3
    sudo systemctl restart hanamel

Back up `/var/lib/hanamel` (database + uploads) — it is the only state.

`render.yaml` and `build.sh` are for the older Render setup and aren't used on EC2.
