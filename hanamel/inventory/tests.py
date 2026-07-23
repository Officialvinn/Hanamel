from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Product, Sale


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