# Hanamel — Timber Sales & Inventory Tracker

Internal tool for tracking timber and finished-product sales and stock.

## Setup
    python -m venv venv
    source venv/bin/activate        # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py runserver

Then open http://127.0.0.1:8000/admin/ and log in.

## Data model
- **Product** — one model for both timber and finished goods (`type` field).
  - Timber: dimensions (thickness & width in INCHES, length in FEET) + `rate_per_m3`.
    Price per piece is computed: volume_m3 × rate_per_m3.
  - Finished: fixed `price`.
- **Sale** — a sale with a `number`, customer, date, total.
- **SaleItem** — a line on a sale. Snapshots `rate` and `volume_per_piece`
  so past sales stay historically accurate when prices/dimensions change.

## Units — IMPORTANT
Volume assumes thickness & width in inches, length in feet, rate per m³.
If Hanamel enters dimensions differently, change INCH_TO_M / FOOT_TO_M
(and the field help_text) in inventory/models.py. Nothing else changes.

## Seeding the common sizes
Hanamel's most-bought timber sizes are pre-loaded via a management command:

    python manage.py seed_products --species Cypress --rate 90000

Sizes are WIDTH x THICKNESS in inches, LENGTH in feet (e.g. 6x2x9 = 6" x 2" x 9ft).
Re-running with a new --rate updates the existing rows instead of duplicating them,
so it's also how you apply a price change. Sizes live in
inventory/management/commands/seed_products.py (COMMON_SIZES).

Loaded sizes: 12x2x9, 12x2x18, 12x4x18, 8x4x9, 8x4x18, 6x4x9, 6x4x18, 6x2x9, 6x2x18.
12x4x9 is commented out — uncomment if the yard stocks it.

## Sanity check
A 2×4×12 piece = 0.01888 m³. At 90,000/m³ that's ~1,699 per piece.
Compute one piece by hand against a real Hanamel quote before trusting the app.

## Not built yet (v2)
Custom frontend, sale-recording view with live total (HTMX), stock decrement
on sale confirm (transaction.atomic), reports, StockMovement ledger, invoices.
