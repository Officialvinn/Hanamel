import json
import logging
import os
from datetime import datetime, timedelta
from itertools import groupby
from decimal import Decimal, InvalidOperation

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

from .models import (BusinessProfile, CustomOrder, CustomOrderPayment,
                     MpesaTransaction, Payment, Product, Sale, SaleItem,
                     StockMovement)
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
        "finished": finished, "q": q, "page": "stock"})


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

        changes, errors = [], []
        for p in products:
            raw = request.POST.get(f"qty_{p.pk}", "").strip()
            if raw == "":
                continue
            try:
                new_qty = Decimal(raw)
            except (InvalidOperation, ValueError):
                errors.append(f"{p} — '{raw}' isn't a number.")
                continue
            if new_qty < 0:
                errors.append(f"{p} — stock can't be negative.")
                continue
            if new_qty != p.stock_qty:
                changes.append((p, new_qty))

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
                for p, new_qty in changes:
                    prod = locked[p.pk]
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

        lines = []
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

        if not lines and not errors:
            errors.append("Add at least one item before recording the sale.")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "inventory/sale_form.html", {
                "timber_groups": _timber_groups(products),
                "finished": [p for p in products if p.type == Product.FINISHED],
                "catalog_json": _catalog_json(products), "methods": Payment.METHOD_CHOICES,
                "page": "new", "customer_name": customer})

        try:
            with transaction.atomic():
                # lock the rows we're about to decrement
                ids = [p.pk for p, _ in lines]
                locked = {p.pk: p for p in
                          Product.objects.select_for_update().filter(pk__in=ids)}

                short = [f"{locked[p.pk]} (have {locked[p.pk].stock_qty}, need {q})"
                         for p, q in lines if locked[p.pk].stock_qty < q]
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
                "catalog_json": _catalog_json(products), "methods": Payment.METHOD_CHOICES,
                "page": "new", "customer_name": customer})

        messages.success(request, f"Recorded {sale.number} — KES {sale.total:,.2f}")
        return redirect("sale_detail", pk=sale.pk)

    return render(request, "inventory/sale_form.html", {
        "timber_groups": _timber_groups(products),
        "finished": [p for p in products if p.type == Product.FINISHED],
        "catalog_json": _catalog_json(products), "page": "new",
        "methods": Payment.METHOD_CHOICES})


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
            size = (f"{p.width:g}x{p.thickness:g}x{p.length:g}"
                    if p.type == Product.TIMBER and p.width else "")
            values = [s.date.strftime("%Y-%m-%d"), s.number,
                      s.customer_name or "Walk-in", p.name, p.species or "", size,
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
        raw_price = request.POST.get("quoted_price", "").strip()
        errors = []
        if not name:
            errors.append("A customer name is needed.")
        if not desc:
            errors.append("Describe what's being made.")
        try:
            price = Decimal(raw_price)
            if price <= 0:
                errors.append("The quoted price must be more than zero.")
        except (InvalidOperation, ValueError):
            errors.append("The quoted price isn't a valid number.")
            price = None

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "inventory/order_form.html", {
                "methods": Payment.METHOD_CHOICES, "page": "orders",
                "form": request.POST})

        order = CustomOrder.objects.create(
            number=_next_order_number(), customer_name=name,
            customer_phone=request.POST.get("customer_phone", "").strip(),
            description=desc, quoted_price=price,
            target_date=request.POST.get("target_date") or None,
            photo=request.FILES.get("photo"))

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
    order = get_object_or_404(CustomOrder.objects.prefetch_related("payments"), pk=pk)
    return render(request, "inventory/order_detail.html", {
        "order": order, "methods": Payment.METHOD_CHOICES,
        "statuses": CustomOrder.STATUS_CHOICES, "page": "orders"})


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
