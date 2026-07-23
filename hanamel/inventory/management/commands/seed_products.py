"""Seed Hanamel's commonly-sold timber sizes.

Sizes are WIDTH x THICKNESS in inches, LENGTH in feet.
Run:  python manage.py seed_products --species Cypress --rate 90000
Safe to re-run: it updates existing rows rather than duplicating them.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from inventory.models import Product

# (width_in, thickness_in, length_ft)
COMMON_SIZES = [
    (12, 2, 9),
    (12, 2, 18),
    (12, 4, 18),
    (8, 4, 9),
    (8, 4, 18),
    (6, 4, 9),
    (6, 4, 18),
    (6, 2, 9),
    (6, 2, 18),
    # (12, 4, 9),  # uncomment if Hanamel stocks this one
]


class Command(BaseCommand):
    help = "Create/update the commonly-sold timber products."

    def add_arguments(self, parser):
        parser.add_argument("--species", default="Cypress")
        parser.add_argument("--rate", type=Decimal, required=True,
                            help="Rate per cubic metre, e.g. 90000")

    def handle(self, *args, **opts):
        species, rate = opts["species"], opts["rate"]
        for w, t, l in COMMON_SIZES:
            obj, created = Product.objects.update_or_create(
                type=Product.TIMBER,
                species=species,
                width=Decimal(w),
                thickness=Decimal(t),
                length=Decimal(l),
                defaults={"name": species, "rate_per_m3": rate},
            )
            verb = "created" if created else "updated"
            self.stdout.write(
                f"{verb:8} {obj}  vol={obj.volume_m3} m3  price/piece={obj.unit_price()}"
            )
        self.stdout.write(self.style.SUCCESS(f"\n{len(COMMON_SIZES)} sizes seeded for {species}."))
