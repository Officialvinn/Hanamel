import json
import logging
import os
from datetime import datetime, timedelta
from itertools import groupby
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (DOOR_STANDARD_SIZES, DOOR_STANDARD_THICKNESSES,
                     BusinessProfile, CustomOrder, CustomOrderItem,
                     CustomOrderPayment, MpesaTransaction, Payment, Product,
                     RecutLoss, Sale, SaleItem, StockMovement)
from .mpesa import normalise_ref, parse_confirmation
from .templatetags.money import CURRENCY

mpesa_log = logging.getLogger("mpesa")


def _next_sale_number():
    """SAL-2026-0001, restarting each year."""
    year = timezone.now().year
    prefix = f"SAL-{year}-"
    last = (Sale.objects.filter(number__startswith=prefix)
            .order_by("-number").values_list("number", flat=True).first())
    seq = int(last.split("-")[-1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


@login_required
def dashboard(request):
    today = timezone.now().date()
    week_ago = timezone.now() - timedelta(days=7)

    todays = Sale.objects.filter(date__date=today)
    week = Sale.objects.filter(date__gte=week_ago)

    low_stock = (Product.objects.filter(is_active=True,
                                        stock_qty__lte=F("low_stock_threshold"))
                 .order_by("stock_qty"))

    return render(request, "inventory/dashboard.html", {
        "todays_count": todays.count(),
        "todays_total": todays.aggregate(t=Sum("total"))["t"] or Decimal("0"),
        "week_total": week.aggregate(t=Sum("total"))["t"] or Decimal("0"),
        "low_stock": low_stock,
        "recent_sales": Sale.objects.order_by("-date")[:8],
        "page": "dash",
    })


@login_required
def product_list(request):
    q = request.GET.get("q", "").strip()
    products = Product.objects.filter(is_active=True)
    if q:
        products = products.filter(Q(name__icontains=q) | Q(species__icontains=q))
    finished = products.filter(type=Product.FINISHED).order_by("name")
    return render(request, "inventory/product_list.html", {
        "timber_groups": _timber_groups(products),
        "finished": finished, "doors": _doors(products), "q": q, "page": "stock"})


@login_required
def stock_update(request):
    """Set stock levels. Pre-filled with current counts: change the ones that
    moved, leave the rest. Each change is logged as a movement."""
    products = (Product.objects.filter(is_active=True)
                .order_by("type", "species", "-width", "-thickness", "-length"))

    if request.method == "POST":
        reason = request.POST.get("reason", StockMovement.COUNT)
        if reason not in dict(StockMovement.REASON_CHOICES):
            reason = StockMovement.COUNT
        note = request.POST.get("note", "").strip()
        # Deliveries are entered as "how many arrived" and added to whatever
        # is on hand — never an absolute count, so a delivery can never be
        # the thing that makes stock go down.
        is_delivery = reason == StockMovement.RECEIVED

        changes, errors = [], []
        for p in products:
            raw = request.POST.get(f"qty_{p.pk}", "").strip()
            if raw == "":
                continue
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                errors.append(f"{p} — '{raw}' isn't a number.")
                continue
            if value < 0:
                label = "quantity received" if is_delivery else "stock"
                errors.append(f"{p} — {label} can't be negative.")
                continue
            if is_delivery:
                if value == 0:
                    continue
                changes.append((p, value))
            elif value != p.stock_qty:
                changes.append((p, value))

        if errors:
            for e in errors:
                messages.error(request, e)
        elif not changes:
            messages.error(request, "Nothing changed.")
        else:
            with transaction.atomic():
                ids = [p.pk for p, _ in changes]
                locked = {p.pk: p for p in
                          Product.objects.select_for_update().filter(pk__in=ids)}
                for p, value in changes:
                    prod = locked[p.pk]
                    new_qty = prod.stock_qty + value if is_delivery else value
                    delta = new_qty - prod.stock_qty
                    prod.stock_qty = new_qty
                    prod.save(update_fields=["stock_qty"])
                    StockMovement.objects.create(
                        product=prod, delta=delta, resulting_qty=new_qty,
                        reason=reason, note=note,
                        user=request.user if request.user.is_authenticated else None)
            messages.success(
                request,
                f"Updated {len(changes)} item{'s' if len(changes) != 1 else ''}.")
            return redirect("product_list")

    return render(request, "inventory/stock_update.html", {
        "timber_groups": _timber_groups(products),
        "finished": [p for p in products if p.type == Product.FINISHED],
        "doors": _doors(products),
        "reasons": StockMovement.REASON_CHOICES,
        "default_reason": StockMovement.COUNT,
        "page": "stock"})


@login_required
def stock_history(request, pk):
    product = get_object_or_404(Product, pk=pk)
    return render(request, "inventory/stock_history.html", {
        "product": product,
        "movements": product.movements.select_related("sale", "user")[:100],
        "page": "stock"})


@login_required
def recut_losses(request):
    """Every cracked piece sold at smaller dimensions than stocked, and how
    much board footage/value went to waste on each one."""
    losses = (RecutLoss.objects.select_related("source_product", "sale")
              .order_by("-created_at"))
    totals = losses.aggregate(bf=Sum("board_feet_lost"), value=Sum("value_lost"))
    return render(request, "inventory/recut_losses.html", {
        "losses": losses, "totals": totals, "page": "stock"})


@login_required
def door_list(request):
    doors = _doors(Product.objects.filter(type=Product.DOOR, is_active=True))
    return render(request, "inventory/door_list.html", {
        "doors": doors,
        "in_stock": sum((d.stock_qty for d in doors), Decimal("0")),
        "stock_value": sum((d.stock_qty * (d.price or 0) for d in doors), Decimal("0")),
        "page": "doors"})


def _door_initial(door):
    """Form values for a fresh form: the door's own, or sensible defaults."""
    if door is None:
        return {"thickness": "40", "opening_stock": "0", "low_stock_threshold": "2"}
    return {"name": door.name, "species": door.species,
            "height": door.door_height, "width": door.door_width,
            "thickness": door.door_thickness, "price": f"{door.price.normalize():f}",
            "low_stock_threshold": f"{door.low_stock_threshold.normalize():f}"}


@login_required
def door_form(request, pk=None):
    """Add or edit a stocked door. Size comes from the standard list or is
    typed in; opening stock is logged as a delivery so the ledger adds up."""
    door = get_object_or_404(Product, pk=pk, type=Product.DOOR) if pk else None
    form = request.POST if request.method == "POST" else {}

    if request.method == "POST":
        errors = []
        name = form.get("name", "").strip()
        species = form.get("species", "").strip()
        if not name:
            errors.append("Give the door a name, e.g. Panel door.")

        def whole(field, label, required=True):
            raw = form.get(field, "").strip()
            if not raw:
                if required:
                    errors.append(f"{label} is needed.")
                return None
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                errors.append(f"{label} isn't a number.")
                return None
            if value < 0 or (required and value == 0):
                errors.append(f"{label} must be more than zero.")
                return None
            return value

        size = form.get("size", "")
        if size and size != "custom":
            try:
                height, width = (int(v) for v in size.split("x"))
            except ValueError:
                errors.append("Pick a door size.")
                height = width = None
        else:
            height = whole("height", "Height")
            width = whole("width", "Width")
        thickness = whole("thickness", "Thickness")
        price = whole("price", "Price")
        threshold = whole("low_stock_threshold", "Low-stock alert", required=False)
        opening = whole("opening_stock", "Opening stock", required=False) if not door else None

        for value, label, lo, hi in ((height, "Height", 1000, 3000),
                                     (width, "Width", 400, 2400),
                                     (thickness, "Thickness", 20, 80)):
            if value is not None and not lo <= value <= hi:
                errors.append(f"{label} should be between {lo} and {hi} mm.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            with transaction.atomic():
                door = door or Product(type=Product.DOOR)
                door.name, door.species = name, species
                door.door_height, door.door_width = int(height), int(width)
                door.door_thickness = int(thickness)
                door.price = price
                if threshold is not None:
                    door.low_stock_threshold = threshold
                elif door.pk is None:
                    door.low_stock_threshold = Decimal("2")
                door.is_active = form.get("retire") != "on"
                is_new = door.pk is None
                if is_new and opening:
                    door.stock_qty = opening
                door.save()
                if is_new and opening:
                    StockMovement.objects.create(
                        product=door, delta=opening, resulting_qty=opening,
                        reason=StockMovement.RECEIVED, note="Opening stock",
                        user=request.user)
            messages.success(request, f"{'Added' if is_new else 'Saved'} {door}.")
            return redirect("door_list")

    species = (Product.objects.exclude(species="")
               .values_list("species", flat=True).distinct().order_by("species"))
    current = f"{door.door_height}x{door.door_width}" if door else ""
    standard = {f"{h}x{w}" for _, sizes in DOOR_STANDARD_SIZES for h, w, _ in sizes}
    return render(request, "inventory/door_form.html", {
        "door": door, "form": form, "page": "doors",
        "sizes": DOOR_STANDARD_SIZES, "thicknesses": DOOR_STANDARD_THICKNESSES,
        "species_options": species,
        "values": form or _door_initial(door),
        "selected_size": form.get("size") or (
            current if current in standard else ("custom" if door else "2100x900"))})


@login_required
def sale_list(request):
    sales = Sale.objects.order_by("-date").prefetch_related("items__product")
    return render(request, "inventory/sale_list.html", {"sales": sales, "page": "sales"})


@login_required
def sale_detail(request, pk):
    sale = get_object_or_404(Sale.objects.prefetch_related("items__product"), pk=pk)
    return render(request, "inventory/sale_detail.html", {"sale": sale, "page": "sales"})


@login_required
def sale_invoice(request, pk):
    """Print-friendly invoice. Rendered from the sale record on demand —
    nothing is stored on disk, so it can never go stale."""
    sale = get_object_or_404(
        Sale.objects.prefetch_related("items__product", "payments"), pk=pk)
    return render(request, "inventory/invoice.html",
                  {"sale": sale, "biz": BusinessProfile.get()})


@login_required
def sale_create(request):
    """Record a sale. Prices are ALWAYS recomputed server-side from the DB —
    the browser's running total is a convenience, never the source of truth."""
    products = Product.objects.filter(is_active=True).order_by("type", "species", "-width")

    if request.method == "POST":
        product_ids = request.POST.getlist("product_id")
        quantities = request.POST.getlist("qty")
        customer = request.POST.get("customer_name", "").strip()
        pay_method = request.POST.get("payment_method", Payment.CASH)
        raw_paid = request.POST.get("amount_paid", "").strip()
        mpesa_ref = request.POST.get("mpesa_receipt", "").strip()

        # Custom cuts: parallel lists of species/dimensions/qty for one-off sizes
        cut_species = request.POST.getlist("cut_species")
        cut_width = request.POST.getlist("cut_width")
        cut_thick = request.POST.getlist("cut_thick")
        cut_length = request.POST.getlist("cut_length")
        cut_qty = request.POST.getlist("cut_qty")

        # Recuts: a cracked/damaged piece sold at smaller dimensions than it was
        # stocked at. Priced at the smaller size, but stock comes off the piece
        # it actually was.
        recut_source = request.POST.getlist("recut_source")
        recut_width = request.POST.getlist("recut_width")
        recut_thick = request.POST.getlist("recut_thick")
        recut_length = request.POST.getlist("recut_length")
        recut_qty = request.POST.getlist("recut_qty")

        lines = []
        custom_lines = []   # (species, w, t, l, qty, rate)
        recut_lines = []    # (source_product, w, t, l, qty)
        errors = []
        for pid, raw_qty in zip(product_ids, quantities):
            if not pid or not raw_qty:
                continue
            try:
                qty = Decimal(str(raw_qty))
            except (InvalidOperation, ValueError):
                errors.append("One of the quantities isn't a number.")
                continue
            if qty <= 0:
                continue
            product = Product.objects.filter(pk=pid, is_active=True).first()
            if not product:
                errors.append("A product on this sale is no longer available.")
                continue
            lines.append((product, qty))

        # validate custom cuts: need a rate (from species) + three dimensions + qty
        rates = {p.species: p.rate_per_board_foot
                 for p in Product.objects.filter(type=Product.TIMBER, is_active=True)
                 if p.species and p.rate_per_board_foot}
        for sp, rw, rt, rl, rq in zip(cut_species, cut_width, cut_thick,
                                      cut_length, cut_qty):
            if not any([sp, rw, rt, rl, rq]):
                continue
            try:
                w, t, l, q = (Decimal(str(rw)), Decimal(str(rt)),
                              Decimal(str(rl)), Decimal(str(rq)))
            except (InvalidOperation, ValueError):
                errors.append("A custom cut has an invalid number.")
                continue
            if min(w, t, l, q) <= 0:
                errors.append("Custom cut dimensions and quantity must be positive.")
                continue
            rate = rates.get(sp)
            if rate is None:
                errors.append(f"No rate found for custom cut species '{sp}'.")
                continue
            custom_lines.append((sp, w, t, l, q, rate))

        # validate recuts: need an in-stock timber source + three dimensions + qty
        for pid, rw, rt, rl, rq in zip(recut_source, recut_width, recut_thick,
                                       recut_length, recut_qty):
            if not pid or not any([rw, rt, rl, rq]):
                continue
            try:
                w, t, l, q = (Decimal(str(rw)), Decimal(str(rt)),
                              Decimal(str(rl)), Decimal(str(rq)))
            except (InvalidOperation, ValueError):
                errors.append("A recut line has an invalid number.")
                continue
            if min(w, t, l, q) <= 0:
                errors.append("Recut dimensions and quantity must be positive.")
                continue
            source = Product.objects.filter(
                pk=pid, type=Product.TIMBER, is_active=True).first()
            if not source:
                errors.append("The piece a recut is coming from is no longer available.")
                continue
            recut_lines.append((source, w, t, l, q))

        if not lines and not custom_lines and not recut_lines and not errors:
            errors.append("Add at least one item before recording the sale.")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "inventory/sale_form.html", {
                "timber_groups": _timber_groups(products),
                "finished": [p for p in products if p.type == Product.FINISHED],
                "doors": _doors(products),
                "catalog_json": _catalog_json(products), "methods": Payment.METHOD_CHOICES,
                "total_pieces": _total_pieces(products),
                "page": "new", "customer_name": customer})

        try:
            with transaction.atomic():
                # lock every row we're about to decrement — a recut pulls from
                # the source piece's stock, so it counts against the same total
                # as a direct sale of that piece
                ids = {p.pk for p, _ in lines} | {s.pk for s, _, _, _, _ in recut_lines}
                locked = {p.pk: p for p in
                          Product.objects.select_for_update().filter(pk__in=ids)}

                demand = {}
                for p, qty in lines:
                    demand[p.pk] = demand.get(p.pk, Decimal("0")) + qty
                for s, _, _, _, qty in recut_lines:
                    demand[s.pk] = demand.get(s.pk, Decimal("0")) + qty

                short = [f"{locked[pk]} (have {locked[pk].stock_qty}, need {q})"
                         for pk, q in demand.items() if locked[pk].stock_qty < q]
                if short:
                    raise ValueError("Not enough stock: " + "; ".join(short))

                sale = Sale.objects.create(number=_next_sale_number(),
                                           customer_name=customer)
                for p, qty in lines:
                    prod = locked[p.pk]
                    is_timber = prod.type == Product.TIMBER
                    SaleItem.objects.create(
                        sale=sale,
                        product=prod,
                        qty=qty,
                        board_feet_per_piece=prod.board_feet if is_timber else None,
                        rate=prod.rate_per_board_foot if is_timber else prod.price,
                    )
                    prod.stock_qty = prod.stock_qty - qty
                    prod.save(update_fields=["stock_qty"])
                    StockMovement.objects.create(
                        product=prod, delta=-qty, resulting_qty=prod.stock_qty,
                        reason=StockMovement.SALE, sale=sale,
                        user=request.user if request.user.is_authenticated else None)

                # custom cuts: one-off lines, board-foot priced, no stock touched
                for sp, w, t, l, q, rate in custom_lines:
                    # whole board feet only — the company doesn't charge for cents
                    board_feet = ((w * t * l) / Decimal("12")).to_integral_value(rounding=ROUND_FLOOR)
                    SaleItem.objects.create(
                        sale=sale,
                        product=None,
                        custom_description=f"{sp} {w:g}x{t:g}x{l:g} (custom cut)",
                        qty=q,
                        board_feet_per_piece=board_feet,
                        rate=rate,
                    )

                # recuts: priced at the smaller size actually sold, but the
                # stock reduction hits the source piece it was cut from
                for source, w, t, l, qty in recut_lines:
                    src = locked[source.pk]
                    # whole board feet only — the company doesn't charge for cents
                    board_feet = ((w * t * l) / Decimal("12")).to_integral_value(rounding=ROUND_FLOOR)
                    SaleItem.objects.create(
                        sale=sale,
                        product=None,
                        custom_description=(f"{src.species} {w:g}x{t:g}x{l:g} "
                                            f"(recut from {src.width_display}x"
                                            f"{src.thickness_display}x{src.length_display}, cracked)"),
                        qty=qty,
                        board_feet_per_piece=board_feet,
                        rate=src.rate_per_board_foot,
                    )
                    src.stock_qty = src.stock_qty - qty
                    src.save(update_fields=["stock_qty"])
                    StockMovement.objects.create(
                        product=src, delta=-qty, resulting_qty=src.stock_qty,
                        reason=StockMovement.SALE, sale=sale,
                        note=f"Recut & sold as {w:g}x{t:g}x{l:g} (cracked)",
                        user=request.user if request.user.is_authenticated else None)

                    bf_lost = ((src.board_feet - board_feet) * qty).quantize(Decimal("0.001"))
                    if bf_lost > 0:
                        RecutLoss.objects.create(
                            sale=sale, source_product=src,
                            sold_width=w, sold_thickness=t, sold_length=l, qty=qty,
                            board_feet_lost=bf_lost,
                            value_lost=(bf_lost * src.rate_per_board_foot).quantize(Decimal("0.01")))

                sale.recalculate_total()

                # Record what was actually handed over. Blank means "pay in full";
                # a smaller figure leaves a balance owing (contractor deposits).
                if raw_paid == "":
                    paid = sale.total
                else:
                    try:
                        paid = Decimal(raw_paid)
                    except (InvalidOperation, ValueError):
                        paid = Decimal("0")
                if paid > 0:
                    Payment.objects.create(
                        sale=sale, method=pay_method, amount=min(paid, sale.total),
                        mpesa_receipt=mpesa_ref if pay_method == Payment.MPESA else "")
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, "inventory/sale_form.html", {
                "timber_groups": _timber_groups(products),
                "finished": [p for p in products if p.type == Product.FINISHED],
                "doors": _doors(products),
                "catalog_json": _catalog_json(products), "methods": Payment.METHOD_CHOICES,
                "total_pieces": _total_pieces(products),
                "page": "new", "customer_name": customer})

        messages.success(request, f"Recorded {sale.number} — KES {sale.total:,.2f}")
        return redirect("sale_detail", pk=sale.pk)

    return render(request, "inventory/sale_form.html", {
        "timber_groups": _timber_groups(products),
        "finished": [p for p in products if p.type == Product.FINISHED],
        "doors": _doors(products),
        "catalog_json": _catalog_json(products), "page": "new",
        "total_pieces": _total_pieces(products),
        "methods": Payment.METHOD_CHOICES})


def _total_pieces(products):
    """Every piece across the catalog that's actually in stock, ready to sell."""
    return sum((p.stock_qty for p in products), Decimal("0"))


def _doors(products):
    """Doors by wood, then tallest and widest first."""
    doors = [p for p in products if p.type == Product.DOOR]
    doors.sort(key=lambda p: (p.species, p.name, -(p.door_height or 0),
                              -(p.door_width or 0)))
    return doors


def _timber_groups(products):
    """Group timber by species so the picker stays readable with several species."""
    timber = [p for p in products if p.type == Product.TIMBER]
    timber.sort(key=lambda p: (p.species or p.name,
                               -float(p.width or 0), -float(p.thickness or 0),
                               -float(p.length or 0)))
    return [{"species": sp, "items": list(items)}
            for sp, items in groupby(timber, key=lambda p: p.species or p.name)]


def _catalog_json(products):
    """Server-computed unit prices, handed to the browser for instant totals."""
    return json.dumps([{
        "id": p.pk,
        "label": str(p),
        "type": p.type,
        "unit_price": float(p.unit_price() or 0),
        "stock": float(p.stock_qty),
        "board_feet": float(p.board_feet) if p.board_feet else None,
    } for p in products])


# --- monthly books export -------------------------------------------------

def _month_range(year, month):
    """Timezone-aware [start, end) for a calendar month in the yard's local time."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime(year, month, 1), tz)
    end = timezone.make_aware(
        datetime(year + (month == 12), (month % 12) + 1, 1), tz)
    return start, end


@login_required
def invoice_pack(request, year, month):
    """Every invoice for a month on one page, page-broken for printing.
    Browser print gives a single PDF of the lot."""
    start, end = _month_range(year, month)
    sales = (Sale.objects.filter(date__gte=start, date__lt=end)
             .prefetch_related("items__product", "payments").order_by("date"))
    return render(request, "inventory/invoice_pack.html", {
        "sales": sales, "biz": BusinessProfile.get(),
        "label": start.strftime("%B %Y"), "page": "reports"})


@login_required
def reports(request):
    """Pick a month to export. Lists months that actually have sales."""
    months = (Sale.objects.annotate(m=TruncMonth("date"))
              .values("m").annotate(n=Count("id"), total=Sum("total"))
              .order_by("-m"))
    return render(request, "inventory/reports.html", {"months": months, "page": "reports"})


@login_required
def export_month(request, year, month):
    """One .xlsx per month: a summary sheet the accountant can post from,
    plus a line-item sheet for anyone who needs the detail."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    start, end = _month_range(year, month)
    sales = (Sale.objects.filter(date__gte=start, date__lt=end)
             .prefetch_related("items__product", "payments").order_by("date"))
    biz = BusinessProfile.get()

    wb = Workbook()
    head_font = Font(name="Arial", bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="0F9455")
    body = Font(name="Arial")
    title = Font(name="Arial", bold=True, size=13)
    money = '#,##0.00'
    thin = Side(style="thin", color="D9D9D9")
    edge = Border(bottom=thin)

    def write_header(ws, headers, row=4):
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=row, column=c, value=h)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = edge

    # ---------- Sheet 1: one row per sale ----------
    ws = wb.active
    ws.title = "Sales"
    ws["A1"] = biz.name
    ws["A1"].font = title
    ws["A2"] = f"Sales for {start.strftime('%B %Y')}"
    ws["A2"].font = body
    if biz.kra_pin:
        ws["A3"] = f"KRA PIN: {biz.kra_pin}"
        ws["A3"].font = body

    cols = ["Date", "Invoice no.", "Customer", "Items",
            "Total (KES)", "Paid (KES)", "Balance (KES)", "Payment method", "M-Pesa ref"]
    write_header(ws, cols)

    r = 5
    for s in sales:
        refs = ", ".join(p.mpesa_receipt for p in s.payments.all() if p.mpesa_receipt)
        values = [s.date.strftime("%Y-%m-%d %H:%M"), s.number,
                  s.customer_name or "Walk-in", s.items.count(),
                  float(s.total), float(s.amount_paid), float(s.balance),
                  s.payment_summary, refs]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = body
            if c in (5, 6, 7):
                cell.number_format = money
        r += 1

    if sales:
        for c, label in ((4, "TOTAL"), (5, None), (6, None), (7, None)):
            cell = ws.cell(row=r, column=c)
            cell.font = Font(name="Arial", bold=True)
            if label:
                cell.value = label
                cell.alignment = Alignment(horizontal="right")
            else:
                letter = cell.column_letter
                cell.value = f"=SUM({letter}5:{letter}{r-1})"
                cell.number_format = money

    for col, w in zip("ABCDEFGHI", (17, 15, 24, 7, 14, 14, 14, 16, 16)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"

    # ---------- Sheet 2: one row per line item ----------
    ws2 = wb.create_sheet("Line items")
    ws2["A1"] = f"Line detail — {start.strftime('%B %Y')}"
    ws2["A1"].font = title
    cols2 = ["Date", "Invoice no.", "Customer", "Item", "Species", "Size",
             "Volume m³/pc", "Qty", "Rate (KES)", "Amount (KES)"]
    write_header(ws2, cols2)

    r = 5
    for s in sales:
        for it in s.items.all():
            p = it.product
            size = (f"{p.width_display}x{p.thickness_display}x{p.length_display}"
                    if p and p.type == Product.TIMBER and p.width
                    else p.door_size if p else "")
            values = [s.date.strftime("%Y-%m-%d"), s.number,
                      s.customer_name or "Walk-in", p.name if p else it.label,
                      (p.species if p else "") or "", size,
                      float(it.board_feet_per_piece) if it.board_feet_per_piece else "",
                      float(it.qty), float(it.rate), float(it.amount)]
            for c, v in enumerate(values, start=1):
                cell = ws2.cell(row=r, column=c, value=v)
                cell.font = body
                if c in (9, 10):
                    cell.number_format = money
                if c == 7 and v != "":
                    cell.number_format = '0.###'
            r += 1
    if r > 5:
        cell = ws2.cell(row=r, column=9, value="TOTAL")
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(horizontal="right")
        tot = ws2.cell(row=r, column=10, value=f"=SUM(J5:J{r-1})")
        tot.font = Font(name="Arial", bold=True)
        tot.number_format = money

    for col, w in zip("ABCDEFGHIJ", (12, 15, 22, 16, 13, 13, 13, 8, 13, 14)):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A5"

    filename = f"{biz.name.replace(' ', '_')}_sales_{year}_{month:02d}.xlsx"
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# --- installable web app ---------------------------------------------------

def site_manifest(request):
    """Rendered rather than served statically so the icon URLs pick up
    WhiteNoise's hashed filenames."""
    return render(request, "pwa/site.webmanifest",
                  content_type="application/manifest+json")


def service_worker(request):
    """Served from the site root so its scope covers the whole app."""
    return render(request, "pwa/sw.js", content_type="application/javascript")


# --- M-Pesa C2B ------------------------------------------------------------

def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")


def _safaricom_only(request):
    """Optional IP allowlist. Safaricom doesn't sign callbacks, so restricting
    by source address is the main defence against forged payments.
    Set MPESA_ALLOWED_IPS to a comma-separated list to switch it on."""
    allowed = [ip.strip() for ip in
               os.environ.get("MPESA_ALLOWED_IPS", "").split(",") if ip.strip()]
    if not allowed:
        return True
    return _client_ip(request) in allowed


@csrf_exempt
@require_POST
def mpesa_validation(request):
    """Safaricom asks whether to allow a payment. We accept everything —
    refusing money at the till would be worse than an unmatched record."""
    if not _safaricom_only(request):
        return JsonResponse({"ResultCode": "C2B00016", "ResultDesc": "Rejected"})
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


@csrf_exempt
@require_POST
def mpesa_confirmation(request):
    """Safaricom telling us money has arrived. Store it first, match second —
    and always answer 200, or Safaricom retries and we get duplicates."""
    try:
        payload = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = {}

    if not _safaricom_only(request):
        mpesa_log.warning("Rejected confirmation from %s", _client_ip(request))
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    try:
        fields = parse_confirmation(payload)
        if not fields["trans_id"] or fields["amount"] is None:
            mpesa_log.warning("Confirmation missing TransID or amount: %s", payload)
            return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

        with transaction.atomic():
            txn, created = MpesaTransaction.objects.get_or_create(
                trans_id=fields["trans_id"],
                defaults={**fields, "raw": payload},
            )
            if created:
                _try_match(txn)
    except Exception:                       # never let an error cause a retry storm
        mpesa_log.exception("Failed handling confirmation: %s", payload)

    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


def _try_match(txn, user=None, sale=None):
    """Attach a transaction to a sale and record the payment.
    Returns True if it matched."""
    if txn.sale_id:
        return True
    if sale is None:
        ref = normalise_ref(txn.bill_ref)
        if not ref:
            return False
        sale = next((s for s in Sale.objects.filter(number__isnull=False)
                     if normalise_ref(s.number) == ref), None)
        automatic = True
    else:
        automatic = False
    if sale is None:
        return False

    payment = Payment.objects.create(
        sale=sale, method=Payment.MPESA, amount=txn.amount,
        mpesa_receipt=txn.trans_id, received_at=txn.trans_time or timezone.now(),
        note=txn.payer_name[:140])
    txn.sale = sale
    txn.payment = payment
    txn.matched_automatically = automatic
    txn.matched_at = timezone.now()
    txn.matched_by = user
    txn.save(update_fields=["sale", "payment", "matched_automatically",
                            "matched_at", "matched_by"])
    return True


@login_required
def mpesa_reconcile(request):
    """Payments Safaricom told us about that aren't attached to a sale yet."""
    if request.method == "POST":
        txn = get_object_or_404(MpesaTransaction, pk=request.POST.get("txn"))
        sale_number = request.POST.get("sale_number", "").strip()
        sale = next((s for s in Sale.objects.all()
                     if normalise_ref(s.number) == normalise_ref(sale_number)), None)
        if txn.sale_id:
            messages.error(request, f"{txn.trans_id} is already matched.")
        elif sale is None:
            messages.error(request, f"No sale found matching '{sale_number}'.")
        else:
            _try_match(txn, user=request.user, sale=sale)
            messages.success(request, f"{txn.trans_id} matched to {sale.number}.")
        return redirect("mpesa_reconcile")

    unmatched = MpesaTransaction.objects.filter(sale__isnull=True)
    recent = (MpesaTransaction.objects.filter(sale__isnull=False)
              .select_related("sale")[:25])
    unpaid = [s for s in Sale.objects.order_by("-date")[:80] if s.balance > 0]
    return render(request, "inventory/mpesa_reconcile.html", {
        "unmatched": unmatched, "recent": recent, "unpaid": unpaid,
        "page": "mpesa"})

# --- custom orders ---------------------------------------------------------

def _next_order_number():
    year = timezone.now().year
    prefix = f"CO-{year}-"
    last = (CustomOrder.objects.filter(number__startswith=prefix)
            .order_by("-number").values_list("number", flat=True).first())
    seq = int(last.split("-")[-1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


@login_required
def order_list(request):
    show = request.GET.get("show", "open")
    orders = CustomOrder.objects.all()
    if show == "open":
        orders = orders.filter(status__in=CustomOrder.OPEN_STATUSES)
    elif show != "all":
        orders = orders.filter(status=show)
    open_count = CustomOrder.objects.filter(
        status__in=CustomOrder.OPEN_STATUSES).count()
    return render(request, "inventory/order_list.html", {
        "orders": orders, "show": show, "open_count": open_count,
        "statuses": CustomOrder.STATUS_CHOICES, "page": "orders"})


@login_required
def order_create(request):
    if request.method == "POST":
        name = request.POST.get("customer_name", "").strip()
        desc = request.POST.get("description", "").strip()

        item_desc = request.POST.getlist("item_description")
        item_detail = request.POST.getlist("item_detail")
        item_qty = request.POST.getlist("item_qty")
        item_unit = request.POST.getlist("item_unit")
        item_price = request.POST.getlist("item_unit_price")

        errors = []
        if not name:
            errors.append("A customer name is needed.")

        items = []   # (description, detail, qty, unit, unit_price)
        for d, det, q, u, p in zip(item_desc, item_detail, item_qty, item_unit, item_price):
            if not d.strip():
                continue
            try:
                qty = Decimal(q) if q.strip() else Decimal("1")
                price = Decimal(p)
            except (InvalidOperation, ValueError):
                errors.append(f"'{d}' has an invalid quantity or price.")
                continue
            if qty <= 0 or price <= 0:
                errors.append(f"'{d}' needs a positive quantity and price.")
                continue
            items.append((d.strip(), det.strip(), qty, u.strip() or "pcs", price))

        raw_price = request.POST.get("quoted_price", "").strip()
        manual_price = None
        if not items:
            if not desc:
                errors.append("Describe what's being made, or add priced line items.")
            try:
                manual_price = Decimal(raw_price)
                if manual_price <= 0:
                    errors.append("The quoted price must be more than zero.")
            except (InvalidOperation, ValueError):
                errors.append("The quoted price isn't a valid number.")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "inventory/order_form.html", {
                "methods": Payment.METHOD_CHOICES, "page": "orders",
                "form": request.POST})

        order = CustomOrder.objects.create(
            number=_next_order_number(), customer_name=name,
            customer_phone=request.POST.get("customer_phone", "").strip(),
            description=desc, quoted_price=manual_price or Decimal("0"),
            target_date=request.POST.get("target_date") or None,
            photo=request.FILES.get("photo"))

        for d, det, qty, unit, price in items:
            CustomOrderItem.objects.create(
                order=order, description=d, detail=det,
                qty=qty, unit=unit, unit_price=price)
        if items:
            order.recalculate_price()
        price = order.quoted_price

        raw_dep = request.POST.get("deposit", "").strip()
        if raw_dep:
            try:
                dep = Decimal(raw_dep)
                if dep > 0:
                    CustomOrderPayment.objects.create(
                        order=order, method=request.POST.get("deposit_method", Payment.CASH),
                        amount=min(dep, price), is_deposit=True,
                        mpesa_receipt=request.POST.get("deposit_mpesa", "").strip())
            except (InvalidOperation, ValueError):
                pass

        messages.success(request, f"Created {order.number}.")
        return redirect("order_detail", pk=order.pk)

    return render(request, "inventory/order_form.html",
                  {"methods": Payment.METHOD_CHOICES, "page": "orders"})


@login_required
def order_detail(request, pk):
    order = get_object_or_404(
        CustomOrder.objects.prefetch_related("payments", "items"), pk=pk)
    return render(request, "inventory/order_detail.html", {
        "order": order, "methods": Payment.METHOD_CHOICES,
        "statuses": CustomOrder.STATUS_CHOICES, "page": "orders"})


@login_required
def order_quote(request, pk):
    """Print-friendly quotation. Rendered on demand, same as sale invoices."""
    order = get_object_or_404(CustomOrder.objects.prefetch_related("items"), pk=pk)
    return render(request, "inventory/quotation.html",
                  {"order": order, "biz": BusinessProfile.get()})


@login_required
@require_POST
def order_status(request, pk):
    order = get_object_or_404(CustomOrder, pk=pk)
    new_status = request.POST.get("status")
    if new_status in dict(CustomOrder.STATUS_CHOICES):
        order.status = new_status
        order.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{order.number} is now {order.get_status_display()}.")
    return redirect("order_detail", pk=pk)


@login_required
@require_POST
def order_payment(request, pk):
    order = get_object_or_404(CustomOrder, pk=pk)
    try:
        amount = Decimal(request.POST.get("amount", "").strip())
    except (InvalidOperation, ValueError):
        messages.error(request, "That payment amount isn't a number.")
        return redirect("order_detail", pk=pk)
    if amount <= 0:
        messages.error(request, "The payment must be more than zero.")
        return redirect("order_detail", pk=pk)

    CustomOrderPayment.objects.create(
        order=order, method=request.POST.get("method", Payment.CASH),
        amount=min(amount, order.balance if order.balance > 0 else amount),
        mpesa_receipt=request.POST.get("mpesa_receipt", "").strip(),
        is_deposit=order.amount_paid == 0)
    messages.success(request, f"Recorded {CURRENCY} {amount:,.2f} on {order.number}.")
    return redirect("order_detail", pk=pk)
