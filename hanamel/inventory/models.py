from decimal import Decimal

from django.db import models
from django.utils import timezone


class Product(models.Model):
    TIMBER = "timber"
    FINISHED = "finished"
    TYPE_CHOICES = [(TIMBER, "Timber"), (FINISHED, "Finished Product")]

    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    name = models.CharField(max_length=120)
    stock_qty = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    # Finished products use this directly. Timber leaves it null.
    price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Fixed price. Finished products only.",
    )

    # Timber: the rate quoted per cubic metre. Finished products leave null.
    rate_per_m3 = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Rate per cubic metre. Timber only.",
    )

    # Timber dimensions. UNITS: thickness & width in INCHES, length in FEET.
    species = models.CharField(max_length=60, blank=True)
    thickness = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                    help_text="inches")
    width = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                help_text="inches")
    length = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                 help_text="feet")

    # --- conversion constants (change these if the yard's units differ) ---
    INCH_TO_M = Decimal("0.0254")
    FOOT_TO_M = Decimal("0.3048")

    @property
    def volume_m3(self):
        """Volume of ONE piece in m3. Width & thickness in INCHES, length in FEET."""
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
        if self.type == self.FINISHED:
            return self.price
        vol = self.volume_m3
        if vol is None or self.rate_per_m3 is None:
            return None
        return (vol * self.rate_per_m3).quantize(Decimal("0.01"))

    def __str__(self):
        if self.type == self.TIMBER:
            return f"{self.name} {self.width}x{self.thickness}x{self.length}"
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

    def __str__(self):
        return self.number or f"Sale #{self.pk}"


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    qty = models.DecimalField(max_digits=10, decimal_places=2)

    # snapshots at time of sale — these freeze the sale's history
    volume_per_piece = models.DecimalField(max_digits=10, decimal_places=5, null=True, blank=True)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        if self.volume_per_piece:  # timber
            self.amount = (self.qty * self.volume_per_piece * self.rate).quantize(Decimal("0.01"))
        else:                      # finished product
            self.amount = (self.qty * self.rate).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.qty} x {self.product}"
