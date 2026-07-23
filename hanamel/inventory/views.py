import json
from datetime import timedelta
from itertools import groupby
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Product, Sale, SaleItem


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
def sale_list(request):
    sales = Sale.objects.order_by("-date").prefetch_related("items__product")
    return render(request, "inventory/sale_list.html", {"sales": sales, "page": "sales"})


@login_required
def sale_detail(request, pk):
    sale = get_object_or_404(Sale.objects.prefetch_related("items__product"), pk=pk)
    return render(request, "inventory/sale_detail.html", {"sale": sale, "page": "sales"})


@login_required
def sale_create(request):
    """Record a sale. Prices are ALWAYS recomputed server-side from the DB —
    the browser's running total is a convenience, never the source of truth."""
    products = Product.objects.filter(is_active=True).order_by("type", "species", "-width")

    if request.method == "POST":
        product_ids = request.POST.getlist("product_id")
        quantities = request.POST.getlist("qty")
        customer = request.POST.get("customer_name", "").strip()

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
                "catalog_json": _catalog_json(products),
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
                        volume_per_piece=prod.volume_m3 if is_timber else None,
                        rate=prod.rate_per_m3 if is_timber else prod.price,
                    )
                    prod.stock_qty = prod.stock_qty - qty
                    prod.save(update_fields=["stock_qty"])
                sale.recalculate_total()
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, "inventory/sale_form.html", {
                "timber_groups": _timber_groups(products),
                "finished": [p for p in products if p.type == Product.FINISHED],
                "catalog_json": _catalog_json(products),
                "page": "new", "customer_name": customer})

        messages.success(request, f"Recorded {sale.number} — KES {sale.total:,.2f}")
        return redirect("sale_detail", pk=sale.pk)

    return render(request, "inventory/sale_form.html", {
        "timber_groups": _timber_groups(products),
        "finished": [p for p in products if p.type == Product.FINISHED],
        "catalog_json": _catalog_json(products), "page": "new"})


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
        "volume": float(p.volume_m3) if p.volume_m3 else None,
    } for p in products])