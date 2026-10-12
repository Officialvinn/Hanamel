from decimal import Decimal

from django.db import models
from django.utils import timezone


def _trim(value):
    """Decimal for display: drop trailing zeros (6.00 -> 6, 2.50 -> 2.5)
    without falling into scientific notation for whole numbers."""
    if value is None:
        return None
    d = Decimal(value).normalize()
    _, _, exponent = d.as_tuple()
    return d.quantize(Decimal(1)) if exponent > 0 else d


# Standard door leaf sizes (height, width in mm) the yard can stock, grouped
# by the market they come from. Kenyan joinery works to 2100mm height.
DOOR_STANDARD_SIZES = [
    ("Kenya / East Africa", [
        (2100, 900, "Main entrance (7' × 3')"),
        (2100, 800, "Bedroom"),
        (2100, 750, "Bathroom"),
        (2100, 700, "Toilet / store"),
        (2100, 1200, "Double, 2 × 600"),
        (2100, 1500, "Double, 2 × 750"),
        (2100, 1800, "Double, 2 × 900"),
    ]),
    ("UK / Europe", [
        (1981, 762, "UK 6'6\" × 2'6\""),
        (1981, 838, "UK 6'6\" × 2'9\""),
        (2040, 826, "UK metric"),
        (2000, 800, "EU interior"),
        (2000, 900, "EU entrance"),
    ]),
    ("North America", [
        (2032, 762, "US 80\" × 30\""),
        (2032, 813, "US 80\" × 32\""),
        (2032, 914, "US 80\" × 36\" entrance"),
        (2032, 1524, "US double 80\" × 60\""),
    ]),
]

# Leaf thickness in mm: 35 interior, 40 heavy interior, 45 exterior.
DOOR_STANDARD_THICKNESSES = [35, 40, 45]


def _feet_inches(mm):
    """2100 -> 6'11" (nearest inch). Approximate — the mm figure is the truth."""
    inches = round(mm / Decimal("25.4"))
    return f"{inches // 12}'{inches % 12}\""


class Product(models.Model):
    TIMBER = "timber"
    FINISHED = "finished"
    DOOR = "door"
    TYPE_CHOICES = [(TIMBER, "Timber"), (FINISHED, "Finished Product"), (DOOR, "Door")]

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    name = models.CharField(max_length=120)
    stock_qty = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    low_stock_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=10)

    # Finished products use this directly. Timber leaves it null.
    price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Fixed price. Finished products only.",
    )

    # Timber: the rate quoted per board foot. Finished products leave null.
    rate_per_board_foot = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Rate per board foot, e.g. 280. Timber only.",
    )

    # Timber dimensions. UNITS: thickness & width in INCHES, length in FEET.
    species = models.CharField(max_length=60, blank=True)
    thickness = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                    help_text="inches")
    width = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                help_text="inches")
    length = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                 help_text="feet")

    # Door leaf dimensions in MILLIMETRES. Doors only; priced with `price`.
    door_height = models.PositiveIntegerField(null=True, blank=True, help_text="mm")
    door_width = models.PositiveIntegerField(null=True, blank=True, help_text="mm")
    door_thickness = models.PositiveIntegerField(null=True, blank=True, help_text="mm")

    # --- conversion constants (change these if the yard's units differ) ---
    INCH_TO_M = Decimal("0.0254")
    FOOT_TO_M = Decimal("0.3048")

    @property
    def width_display(self):
        return _trim(self.width)

    @property
    def thickness_display(self):
        return _trim(self.thickness)

    @property
    def length_display(self):
        return _trim(self.length)

    @property
    def board_feet(self):
        """Board feet in ONE piece: (width_in x thickness_in x length_ft) / 12.
        This is how the yard quotes, so it is the basis for pricing."""
        if self.type != self.TIMBER:
            return None
        if None in (self.thickness, self.width, self.length):
            return None
        bf = (self.width * self.thickness * self.length) / Decimal("12")
        return bf.quantize(Decimal("0.001")).normalize()

    @property
    def volume_m3(self):
        """Cubic metres per piece. Not used for pricing — kept because
        customers occasionally ask for volume."""
        if self.type != self.TIMBER:
            return None
        if None in (self.thickness, self.width, self.length):
            return None
        t_m = self.thickness * self.INCH_TO_M
        w_m = self.width * self.INCH_TO_M
        l_m = self.length * self.FOOT_TO_M
        return (t_m * w_m * l_m).quantize(Decimal("0.00001"))

    def unit_price(self):
        """Price of one unit, whichever product type this is."""
        if self.type in (self.FINISHED, self.DOOR):
            return self.price
        bf = self.board_feet
        if bf is None or self.rate_per_board_foot is None:
            return None
        return (bf * self.rate_per_board_foot).quantize(Decimal("0.01"))

    @property
    def door_size(self):
        """'2100 × 900 mm' — height first, as joiners say it."""
        if self.type != self.DOOR or not (self.door_height and self.door_width):
            return ""
        return f"{self.door_height} × {self.door_width} mm"

    @property
    def door_size_imperial(self):
        if self.type != self.DOOR or not (self.door_height and self.door_width):
            return ""
        return f"≈ {_feet_inches(self.door_height)} × {_feet_inches(self.door_width)}"

    @property
    def door_icon_px(self):
        """Proportional door outline for lists: fixed height, true width."""
        if self.type != self.DOOR or not (self.door_height and self.door_width):
            return None
        h = 38
        return {"w": round(h * self.door_width / self.door_height, 1), "h": h,
                "double": self.door_width >= 1200}

    @property
    def door_standard_label(self):
        """Which standard this size matches, e.g. 'Kenya / East Africa · Bedroom'."""
        for region, sizes in DOOR_STANDARD_SIZES:
            for h, w, label in sizes:
                if (h, w) == (self.door_height, self.door_width):
                    return f"{region} · {label}"
        return "Custom size"

    def __str__(self):
        if self.type == self.TIMBER:
            return f"{self.name} {self.width_display}x{self.thickness_display}x{self.length_display}"
        if self.type == self.DOOR:
            name = f"{self.species} {self.name}" if self.species else self.name
            return f"{name} {self.door_height}×{self.door_width}mm"
        return self.name


class Sale(models.Model):
    number = models.CharField(max_length=20, unique=True, blank=True)
    customer_name = models.CharField(max_length=120, blank=True)
    date = models.DateTimeField(default=timezone.now)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def recalculate_total(self):
        self.total = sum((line.amount for line in self.items.all()), Decimal("0.00"))
        self.save(update_fields=["total"])

    @property
    def amount_paid(self):
        return sum((p.amount for p in self.payments.all()), Decimal("0.00"))

    @property
    def balance(self):
        return self.total - self.amount_paid

    @property
    def is_paid(self):
        return self.balance <= 0

    @property
    def payment_summary(self):
        methods = sorted({p.get_method_display() for p in self.payments.all()})
        return ", ".join(methods) if methods else "Unpaid"

    def __str__(self):
        return self.number or f"Sale #{self.pk}"


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT,
                                null=True, blank=True)
    # set only for one-off custom cuts that aren't in the catalog
    custom_description = models.CharField(max_length=140, blank=True)
    qty = models.DecimalField(max_digits=10, decimal_places=2)

    # snapshots at time of sale — these freeze the sale's history
    board_feet_per_piece = models.DecimalField(max_digits=10, decimal_places=3,
                                               null=True, blank=True)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        if self.board_feet_per_piece:  # timber
            self.amount = (self.qty * self.board_feet_per_piece
                           * self.rate).quantize(Decimal("0.01"))
        else:                          # finished product
            self.amount = (self.qty * self.rate).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    @property
    def label(self):
        """What to show on invoices and lists, product or custom cut."""
        return self.custom_description if self.custom_description else str(self.product)

    def __str__(self):
        return f"{self.qty} x {self.label}"


# --- presentational helpers -------------------------------------------------

def _cross_section_px(product, scale=7, cap=84):
    """Pixel size of a proportional cross-section swatch (width x thickness)."""
    if product.type != Product.TIMBER or not (product.width and product.thickness):
        return None
    w = float(product.width) * scale
    h = float(product.thickness) * scale
    longest = max(w, h)
    if longest > cap:
        factor = cap / longest
        w, h = w * factor, h * factor
    return {"w": round(w, 1), "h": round(h, 1)}


Product.cross_section_px = property(_cross_section_px)


class BusinessProfile(models.Model):
    """Single row holding the details printed on invoices. Edit in admin."""
    name = models.CharField(max_length=120, default="Hanamel Timber")
    logo = models.ImageField(upload_to="business/", null=True, blank=True,
                             help_text="Shown on invoices and quotations")
    kra_pin = models.CharField(max_length=20, blank=True, help_text="Seller KRA PIN")
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=200, blank=True)
    till_or_paybill = models.CharField(max_length=40, blank=True,
                                       help_text="M-Pesa Till or Paybill number")
    footer_note = models.CharField(
        max_length=200, blank=True,
        default="Goods remain the property of the seller until paid in full.")

    quote_tagline = models.CharField(
        max_length=120, blank=True, default="Quality is our goal",
        help_text="Shown in the footer of quotations")
    quote_validity_days = models.PositiveIntegerField(
        default=30, help_text="How many days a quotation stays valid")
    quote_terms = models.TextField(
        blank=True, help_text="One term per line, shown as bullets on quotations",
        default=("All prices are in Kenya Shillings (KSh) and cover materials, "
                 "fittings and labour as listed.\n"
                 "This is made-to-order work. A deposit of 50% is required before "
                 "production begins; the balance is due on collection."))
    quote_signee_name = models.CharField(
        max_length=120, blank=True, help_text="Who signs off quotations, e.g. George Mbatia")
    quote_signee_phone = models.CharField(max_length=40, blank=True)

    class Meta:
        verbose_name = "Business profile"
        verbose_name_plural = "Business profile"

    def save(self, *args, **kwargs):
        self.pk = 1                      # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.name


class Payment(models.Model):
    CASH = "cash"
    MPESA = "mpesa"
    BANK = "bank"
    METHOD_CHOICES = [(CASH, "Cash"), (MPESA, "M-Pesa"), (BANK, "Bank transfer")]

    sale = models.ForeignKey(Sale, related_name="payments", on_delete=models.CASCADE)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=CASH)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mpesa_receipt = models.CharField(max_length=32, blank=True,
                                     help_text="M-Pesa confirmation code, e.g. SFG4H2K9LM")
    received_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=140, blank=True)

    class Meta:
        ordering = ["received_at"]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount}"


class StockMovement(models.Model):
    """Every change to a stock level, so 'why is this number what it is?'
    always has an answer."""
    RECEIVED = "received"
    SALE = "sale"
    COUNT = "count"
    CORRECTION = "correction"
    REASON_CHOICES = [
        (RECEIVED, "Delivery received"),
        (SALE, "Sold"),
        (COUNT, "Stock take"),
        (CORRECTION, "Correction"),
    ]

    product = models.ForeignKey(Product, related_name="movements",
                                on_delete=models.CASCADE)
    delta = models.DecimalField(max_digits=10, decimal_places=2,
                                help_text="Positive for stock in, negative for out")
    resulting_qty = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=12, choices=REASON_CHOICES)
    note = models.CharField(max_length=140, blank=True)
    sale = models.ForeignKey(Sale, null=True, blank=True, related_name="movements",
                             on_delete=models.SET_NULL)
    user = models.ForeignKey("auth.User", null=True, blank=True,
                             on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sign = "+" if self.delta >= 0 else ""
        return f"{self.product}: {sign}{self.delta} ({self.get_reason_display()})"


class RecutLoss(models.Model):
    """Board feet lost every time a cracked piece is sold at smaller dimensions
    than it was stocked at — the offcut that never becomes revenue."""
    sale = models.ForeignKey(Sale, related_name="recut_losses", on_delete=models.CASCADE)
    source_product = models.ForeignKey(Product, related_name="recut_losses",
                                       on_delete=models.PROTECT)
    sold_width = models.DecimalField(max_digits=6, decimal_places=2)
    sold_thickness = models.DecimalField(max_digits=6, decimal_places=2)
    sold_length = models.DecimalField(max_digits=6, decimal_places=2)
    qty = models.DecimalField(max_digits=10, decimal_places=2)
    board_feet_lost = models.DecimalField(max_digits=10, decimal_places=3,
                                          help_text="Total across qty, not per piece")
    value_lost = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (f"{self.source_product} sold as "
                f"{self.sold_width:g}x{self.sold_thickness:g}x{self.sold_length:g}"
                f" — {self.board_feet_lost} bd ft lost")


class MpesaTransaction(models.Model):
    """Every payment Safaricom tells us about, stored exactly as received.

    This table is the record of what the bank says happened. Matching it to a
    sale is a separate, reversible step — so a mis-match never loses the money.
    """
    trans_id = models.CharField(max_length=32, unique=True,
                                help_text="Safaricom receipt, e.g. SFG4H2K9LM")
    trans_time = models.DateTimeField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    msisdn = models.CharField(max_length=32, blank=True)
    bill_ref = models.CharField(max_length=64, blank=True,
                                help_text="Account number the customer typed")
    payer_name = models.CharField(max_length=140, blank=True)
    short_code = models.CharField(max_length=16, blank=True)
    raw = models.JSONField(default=dict)

    sale = models.ForeignKey(Sale, null=True, blank=True, related_name="mpesa",
                             on_delete=models.SET_NULL)
    payment = models.ForeignKey(Payment, null=True, blank=True,
                                related_name="mpesa", on_delete=models.SET_NULL)
    matched_automatically = models.BooleanField(default=False)
    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-trans_time", "-received_at"]

    @property
    def is_matched(self):
        return self.sale_id is not None

    def __str__(self):
        return f"{self.trans_id} · {self.amount} · {self.bill_ref or 'no account'}"

class CustomOrder(models.Model):
    """A made-to-order job: a door, a table, custom-cut timber. Quoted, built
    over time, then collected. Priced by agreement, not from the catalog."""
    QUOTED = "quoted"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    COLLECTED = "collected"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (QUOTED, "Quoted"),
        (IN_PROGRESS, "In progress"),
        (READY, "Ready for collection"),
        (COLLECTED, "Collected"),
        (CANCELLED, "Cancelled"),
    ]
    OPEN_STATUSES = [QUOTED, IN_PROGRESS, READY]

    number = models.CharField(max_length=24, unique=True, blank=True)
    customer_name = models.CharField(max_length=140)
    customer_phone = models.CharField(max_length=40, blank=True)
    description = models.TextField(
        blank=True, help_text="Overview of what's being built (optional — the "
        "priced line items below carry the detail)")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUOTED)

    quoted_price = models.DecimalField(max_digits=12, decimal_places=2,
                                       help_text="Agreed total price")
    target_date = models.DateField(null=True, blank=True,
                                   help_text="When it should be ready")
    photo = models.ImageField(upload_to="custom_orders/", null=True, blank=True,
                              help_text="A reference photo or sketch")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def amount_paid(self):
        return sum((p.amount for p in self.payments.all()), Decimal("0.00"))

    @property
    def balance(self):
        return self.quoted_price - self.amount_paid

    @property
    def is_paid(self):
        return self.balance <= 0

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def recalculate_price(self):
        self.quoted_price = sum((it.amount for it in self.items.all()), Decimal("0.00"))
        self.save(update_fields=["quoted_price"])

    def __str__(self):
        return f"{self.number} — {self.customer_name}"


class CustomOrderItem(models.Model):
    """One priced line on a quotation, e.g. '10 sets of mahogany doors'."""
    order = models.ForeignKey(CustomOrder, related_name="items", on_delete=models.CASCADE)
    description = models.CharField(max_length=200)
    detail = models.CharField(
        max_length=300, blank=True,
        help_text="Extra breakdown shown under the description, e.g. fittings list")
    qty = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit = models.CharField(max_length=20, default="pcs", help_text="e.g. pcs, sets, hours")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        self.amount = (self.qty * self.unit_price).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.qty:g} {self.unit} x {self.description}"


class CustomOrderPayment(models.Model):
    """A deposit or settlement against a custom order. Kept separate from Sale
    payments so a half-built order never looks like a completed sale."""
    order = models.ForeignKey(CustomOrder, related_name="payments",
                              on_delete=models.CASCADE)
    method = models.CharField(max_length=10, choices=Payment.METHOD_CHOICES,
                              default=Payment.CASH)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mpesa_receipt = models.CharField(max_length=32, blank=True)
    is_deposit = models.BooleanField(default=False)
    received_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=140, blank=True)

    class Meta:
        ordering = ["received_at"]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount} on {self.order.number}"
