from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Product, Sale, SaleItem


class TimberMathTests(TestCase):
    def test_volume_and_price(self):
        p = Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal("6"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_m3=Decimal("90000"), stock_qty=Decimal("50"))
        self.assertEqual(p.volume_m3, Decimal("0.02124"))
        self.assertEqual(p.unit_price(), Decimal("1911.60"))

    def test_equal_volume_sizes_price_the_same(self):
        """12x2x9, 6x4x9 and 6x2x18 share a volume, so volume pricing ties them."""
        sizes = [(12, 2, 9), (6, 4, 9), (6, 2, 18)]
        prices = set()
        for w, t, l in sizes:
            p = Product.objects.create(
                type=Product.TIMBER, name="Cypress", species="Cypress",
                width=Decimal(w), thickness=Decimal(t), length=Decimal(l),
                rate_per_m3=Decimal("90000"))
            prices.add(p.unit_price())
        self.assertEqual(len(prices), 1)

    def test_finished_product_uses_fixed_price(self):
        p = Product.objects.create(type=Product.FINISHED, name="Dining Table",
                                   price=Decimal("35000"))
        self.assertIsNone(p.volume_m3)
        self.assertEqual(p.unit_price(), Decimal("35000"))


class SaleFlowTests(TestCase):
    def setUp(self):
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.timber = Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal("6"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_m3=Decimal("90000"), stock_qty=Decimal("50"))
        self.table = Product.objects.create(
            type=Product.FINISHED, name="Dining Table",
            price=Decimal("35000"), stock_qty=Decimal("4"))

    def test_pages_load(self):
        for name in ["dashboard", "product_list", "sale_list", "sale_create"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_login_required(self):
        self.client.logout()
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r.url)

    def test_recording_a_sale(self):
        r = self.client.post(reverse("sale_create"), {
            "customer_name": "Kamau Hardware",
            "product_id": [str(self.timber.pk), str(self.table.pk)],
            "qty": ["10", "1"]}, follow=True)
        self.assertEqual(r.status_code, 200)

        sale = Sale.objects.get()
        self.assertEqual(sale.number, f"SAL-{sale.date.year}-0001")
        # 10 * 0.02124 m3 * 90000 = 19116.00, plus a 35000 table
        self.assertEqual(sale.total, Decimal("54116.00"))

        self.timber.refresh_from_db()
        self.table.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("40"))
        self.assertEqual(self.table.stock_qty, Decimal("3"))

    def test_rate_is_snapshotted(self):
        self.client.post(reverse("sale_create"), {
            "product_id": [str(self.timber.pk)], "qty": ["5"]})
        line = Sale.objects.get().items.get()

        self.timber.rate_per_m3 = Decimal("120000")
        self.timber.save()
        line.refresh_from_db()

        self.assertEqual(line.rate, Decimal("90000"))       # history intact
        self.assertEqual(line.amount, Decimal("9558.00"))

    def test_oversell_is_refused_and_nothing_changes(self):
        r = self.client.post(reverse("sale_create"), {
            "product_id": [str(self.timber.pk)], "qty": ["999"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Sale.objects.count(), 0)
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("50"))

    def test_partial_failure_rolls_back_entirely(self):
        """Good line + oversold line = no sale, no stock moved."""
        self.client.post(reverse("sale_create"), {
            "product_id": [str(self.timber.pk), str(self.table.pk)],
            "qty": ["5", "999"]})
        self.assertEqual(Sale.objects.count(), 0)
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("50"))

    def test_empty_sale_refused(self):
        self.client.post(reverse("sale_create"), {"product_id": [], "qty": []})
        self.assertEqual(Sale.objects.count(), 0)

    def test_sale_numbers_increment(self):
        for _ in range(3):
            self.client.post(reverse("sale_create"), {
                "product_id": [str(self.timber.pk)], "qty": ["1"]})
        numbers = list(Sale.objects.order_by("pk").values_list("number", flat=True))
        year = Sale.objects.first().date.year
        self.assertEqual(numbers, [f"SAL-{year}-000{i}" for i in (1, 2, 3)])


class PaymentAndInvoiceTests(TestCase):
    def setUp(self):
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.timber = Product.objects.create(
            type=Product.TIMBER, name="Mahogany", species="Mahogany",
            width=Decimal("6"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_m3=Decimal("200000"), stock_qty=Decimal("50"))

    def _sale(self, **extra):
        data = {"product_id": [str(self.timber.pk)], "qty": ["2"]}
        data.update(extra)
        self.client.post(reverse("sale_create"), data)
        return Sale.objects.latest("pk")

    def test_blank_amount_means_paid_in_full(self):
        sale = self._sale(payment_method="cash")
        self.assertEqual(sale.amount_paid, sale.total)
        self.assertEqual(sale.balance, Decimal("0.00"))
        self.assertTrue(sale.is_paid)

    def test_partial_payment_leaves_a_balance(self):
        sale = self._sale(payment_method="cash", amount_paid="1000")
        self.assertEqual(sale.amount_paid, Decimal("1000.00"))
        self.assertEqual(sale.balance, sale.total - Decimal("1000.00"))
        self.assertFalse(sale.is_paid)

    def test_mpesa_reference_is_kept(self):
        sale = self._sale(payment_method="mpesa", mpesa_receipt="SFG4H2K9LM")
        payment = sale.payments.get()
        self.assertEqual(payment.method, "mpesa")
        self.assertEqual(payment.mpesa_receipt, "SFG4H2K9LM")

    def test_mpesa_code_ignored_for_cash(self):
        sale = self._sale(payment_method="cash", mpesa_receipt="SHOULDNOTSTICK")
        self.assertEqual(sale.payments.get().mpesa_receipt, "")

    def test_payment_never_exceeds_the_total(self):
        sale = self._sale(payment_method="cash", amount_paid="999999")
        self.assertEqual(sale.amount_paid, sale.total)

    def test_invoice_page_renders(self):
        sale = self._sale()
        r = self.client.get(reverse("sale_invoice", args=[sale.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, sale.number)

    def test_reports_page_lists_the_month(self):
        self._sale()
        r = self.client.get(reverse("reports"))
        self.assertEqual(r.status_code, 200)

    def test_monthly_export_returns_a_spreadsheet(self):
        sale = self._sale()
        r = self.client.get(reverse("export_month", args=[sale.date.year, sale.date.month]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        self.assertGreater(len(r.content), 4000)

    def test_export_of_an_empty_month_still_works(self):
        r = self.client.get(reverse("export_month", args=[2020, 1]))
        self.assertEqual(r.status_code, 200)


class MonthBoundaryTests(TestCase):
    """A sale just after local midnight must land in the right month."""

    def setUp(self):
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.product = Product.objects.create(
            type=Product.FINISHED, name="Door frame",
            price=Decimal("8500"), stock_qty=Decimal("99"))

    def _sale_at(self, when):
        """`when` is wall-clock time in the yard (Nairobi), pinned explicitly so
        this test is independent of the TIME_ZONE setting."""
        from zoneinfo import ZoneInfo
        sale = Sale.objects.create(number=f"SAL-X-{when:%Y%m%d%H%M}",
                                   date=when.replace(tzinfo=ZoneInfo("Africa/Nairobi")))
        SaleItem.objects.create(sale=sale, product=self.product,
                                qty=Decimal("1"), rate=Decimal("8500"))
        sale.recalculate_total()
        return sale

    def test_sale_just_after_midnight_belongs_to_the_new_month(self):
        import io
        from datetime import datetime
        import openpyxl

        self._sale_at(datetime(2026, 8, 1, 1, 0))    # 1am EAT on the 1st
        self._sale_at(datetime(2026, 7, 31, 23, 0))  # 11pm EAT on the 31st

        def numbers_in(year, month):
            r = self.client.get(reverse("export_month", args=[year, month]))
            wb = openpyxl.load_workbook(io.BytesIO(r.content))
            ws = wb["Sales"]
            return [row[1] for row in ws.iter_rows(min_row=5, values_only=True)
                    if row[1] and str(row[1]).startswith("SAL-X")]

        self.assertEqual(len(numbers_in(2026, 7)), 1, "July should hold only the 31st sale")
        self.assertEqual(len(numbers_in(2026, 8)), 1, "August should hold the 1am sale")


class InvoicePackTests(TestCase):
    def setUp(self):
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.product = Product.objects.create(
            type=Product.FINISHED, name="Door frame",
            price=Decimal("8500"), stock_qty=Decimal("99"))

    def _sale(self, number, when):
        from zoneinfo import ZoneInfo
        sale = Sale.objects.create(
            number=number, customer_name="Kamau Hardware",
            date=when.replace(tzinfo=ZoneInfo("Africa/Nairobi")))
        SaleItem.objects.create(sale=sale, product=self.product,
                                qty=Decimal("2"), rate=Decimal("8500"))
        sale.recalculate_total()
        return sale

    def test_pack_shows_every_invoice_for_the_month(self):
        from datetime import datetime
        self._sale("SAL-2026-0101", datetime(2026, 9, 3, 10, 0))
        self._sale("SAL-2026-0102", datetime(2026, 9, 20, 16, 0))
        self._sale("SAL-2026-0103", datetime(2026, 10, 1, 9, 0))   # next month

        r = self.client.get(reverse("invoice_pack", args=[2026, 9]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "SAL-2026-0101")
        self.assertContains(r, "SAL-2026-0102")
        self.assertNotContains(r, "SAL-2026-0103")

    def test_empty_month_pack_is_not_an_error(self):
        r = self.client.get(reverse("invoice_pack", args=[2019, 5]))
        self.assertEqual(r.status_code, 200)

    def test_single_invoice_and_pack_render_the_same_body(self):
        """Both use the shared partial, so they can't drift apart."""
        from datetime import datetime
        sale = self._sale("SAL-2026-0201", datetime(2026, 11, 5, 11, 0))
        one = self.client.get(reverse("sale_invoice", args=[sale.pk])).content.decode()
        pack = self.client.get(reverse("invoice_pack", args=[2026, 11])).content.decode()
        for token in ("SAL-2026-0201", "Kamau Hardware", "17,000.00", "Billed to"):
            self.assertIn(token, one)
            self.assertIn(token, pack)


class MoneyFilterTests(TestCase):
    def test_grouping_and_currency(self):
        from inventory.templatetags.money import amount, money
        self.assertEqual(amount(Decimal("1234567.5")), "1,234,567.50")
        self.assertEqual(amount(Decimal("0")), "0.00")
        self.assertEqual(money(Decimal("42480")), "KSh 42,480.00")
        self.assertEqual(money(Decimal("1234567.5"), 0), "KSh 1,234,568")

    def test_missing_values_do_not_crash(self):
        from inventory.templatetags.money import amount, money
        for bad in (None, "", "not a number", []):
            self.assertEqual(amount(bad), "—")
            self.assertEqual(money(bad), "—")

    def test_dashboard_shows_grouped_currency(self):
        User.objects.create_user("s", password="pw12345!")
        self.client.login(username="s", password="pw12345!")
        product = Product.objects.create(type=Product.FINISHED, name="Table",
                                         price=Decimal("1250000"), stock_qty=Decimal("5"))
        sale = Sale.objects.create(number="SAL-2026-0301")
        SaleItem.objects.create(sale=sale, product=product,
                                qty=Decimal("1"), rate=Decimal("1250000"))
        sale.recalculate_total()
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "KSh 1,250,000")