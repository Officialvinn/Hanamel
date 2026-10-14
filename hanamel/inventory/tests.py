import json
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
            rate_per_board_foot=Decimal("280"), stock_qty=Decimal("50"))
        # (6 x 2 x 9) / 12 = 9 board feet, at 280 = 2,520
        self.assertEqual(p.board_feet, Decimal("9"))
        self.assertEqual(p.unit_price(), Decimal("2520.00"))

    def test_equal_board_feet_sizes_price_the_same(self):
        """12x2x9, 6x4x9 and 6x2x18 are all 18 board feet."""
        sizes = [(12, 2, 9), (6, 4, 9), (6, 2, 18)]
        prices = set()
        for w, t, l in sizes:
            p = Product.objects.create(
                type=Product.TIMBER, name="Cypress", species="Cypress",
                width=Decimal(w), thickness=Decimal(t), length=Decimal(l),
                rate_per_board_foot=Decimal("280"))
            prices.add(p.unit_price())
        self.assertEqual(len(prices), 1)

    def test_finished_product_uses_fixed_price(self):
        p = Product.objects.create(type=Product.FINISHED, name="Dining Table",
                                   price=Decimal("35000"))
        self.assertIsNone(p.board_feet)
        self.assertEqual(p.unit_price(), Decimal("35000"))


class SaleFlowTests(TestCase):
    def setUp(self):
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.timber = Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal("6"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_board_foot=Decimal("280"), stock_qty=Decimal("50"))
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
        # 10 pieces x 9 board feet x 280 = 25,200, plus a 35,000 table
        self.assertEqual(sale.total, Decimal("60200.00"))

        self.timber.refresh_from_db()
        self.table.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("40"))
        self.assertEqual(self.table.stock_qty, Decimal("3"))

    def test_rate_is_snapshotted(self):
        self.client.post(reverse("sale_create"), {
            "product_id": [str(self.timber.pk)], "qty": ["5"]})
        line = Sale.objects.get().items.get()

        self.timber.rate_per_board_foot = Decimal("400")
        self.timber.save()
        line.refresh_from_db()

        self.assertEqual(line.rate, Decimal("280"))         # history intact
        self.assertEqual(line.amount, Decimal("12600.00"))  # 5 x 9bf x 280

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
            rate_per_board_foot=Decimal("280"), stock_qty=Decimal("50"))

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


class BoardFootPricingTests(TestCase):
    """The yard's own formula: (width x thickness x length_ft) / 12 x rate."""

    def _timber(self, w, t, l, rate="280"):
        return Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal(w), thickness=Decimal(t), length=Decimal(l),
            rate_per_board_foot=Decimal(rate), stock_qty=Decimal("40"))

    def test_the_worked_example(self):
        """(12 x 2 x 9) / 12 x 280 = 5,040."""
        p = self._timber(12, 2, 9)
        self.assertEqual(p.board_feet, Decimal("18"))
        self.assertEqual(p.unit_price(), Decimal("5040.00"))

    def test_every_common_size(self):
        expected = {
            (12, 2, 9): ("18", "5040.00"),
            (12, 2, 18): ("36", "10080.00"),
            (12, 4, 18): ("72", "20160.00"),
            (8, 4, 9): ("24", "6720.00"),
            (8, 4, 18): ("48", "13440.00"),
            (6, 4, 9): ("18", "5040.00"),
            (6, 4, 18): ("36", "10080.00"),
            (6, 2, 9): ("9", "2520.00"),
            (6, 2, 18): ("18", "5040.00"),
        }
        for (w, t, l), (bf, price) in expected.items():
            p = self._timber(w, t, l)
            self.assertEqual(p.board_feet, Decimal(bf), f"{w}x{t}x{l} board feet")
            self.assertEqual(p.unit_price(), Decimal(price), f"{w}x{t}x{l} price")

    def test_board_feet_snapshot_survives_a_rate_change(self):
        User.objects.create_user("s2", password="pw12345!")
        self.client.login(username="s2", password="pw12345!")
        p = self._timber(12, 2, 9)
        self.client.post(reverse("sale_create"),
                         {"product_id": [str(p.pk)], "qty": ["3"]})
        line = Sale.objects.get().items.get()
        self.assertEqual(line.board_feet_per_piece, Decimal("18"))
        self.assertEqual(line.amount, Decimal("15120.00"))   # 3 x 18 x 280

        p.rate_per_board_foot = Decimal("320")
        p.save()
        line.refresh_from_db()
        self.assertEqual(line.rate, Decimal("280"))
        self.assertEqual(line.amount, Decimal("15120.00"))


class BrandingAndPwaTests(TestCase):
    def setUp(self):
        User.objects.create_user("s3", password="pw12345!")
        self.client.login(username="s3", password="pw12345!")

    def test_manifest_is_valid_json_with_icons(self):
        import json
        r = self.client.get(reverse("site_manifest"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("manifest+json", r["Content-Type"])
        data = json.loads(r.content)
        self.assertEqual(data["short_name"], "Hanamel")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["theme_color"], "#041E50")
        sizes = {i["sizes"] for i in data["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        purposes = {i["purpose"] for i in data["icons"]}
        self.assertIn("maskable", purposes)   # Android crops to a circle

    def test_service_worker_served_from_root_scope(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r["Content-Type"])

    def test_pages_carry_the_brand_mark_and_icons(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("logo-mark-80", html)
        self.assertIn("apple-touch-icon", html)
        self.assertIn('name="theme-color"', html)
        self.assertIn("site.webmanifest", html)

    def test_manifest_and_worker_do_not_require_login(self):
        """The browser fetches these without the user's session."""
        self.client.logout()
        self.assertEqual(self.client.get(reverse("site_manifest")).status_code, 200)
        self.assertEqual(self.client.get("/sw.js").status_code, 200)


class StockUpdateTests(TestCase):
    def setUp(self):
        from .models import StockMovement
        self.SM = StockMovement
        self.user = User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")
        self.timber = Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal("6"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_board_foot=Decimal("280"), stock_qty=Decimal("40"))
        self.table = Product.objects.create(
            type=Product.FINISHED, name="Door frame",
            price=Decimal("8500"), stock_qty=Decimal("5"))

    def test_page_loads_with_current_quantities(self):
        r = self.client.get(reverse("stock_update"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'value="40"')

    def test_delivery_adds_to_stock_on_hand(self):
        self.client.post(reverse("stock_update"), {
            "reason": "received", "note": "delivery from Kitale",
            f"qty_{self.timber.pk}": "25",         # pieces that arrived
            f"qty_{self.table.pk}": ""})           # nothing arrived
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("65"))

        m = self.SM.objects.get()                  # only the changed one is logged
        self.assertEqual(m.product, self.timber)
        self.assertEqual(m.delta, Decimal("25"))
        self.assertEqual(m.resulting_qty, Decimal("65"))
        self.assertEqual(m.reason, "received")
        self.assertEqual(m.note, "delivery from Kitale")
        self.assertEqual(m.user, self.user)

    def test_stock_take_sets_the_absolute_count(self):
        self.client.post(reverse("stock_update"), {
            "reason": "count",
            f"qty_{self.timber.pk}": "65",
            f"qty_{self.table.pk}": "5"})          # unchanged
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("65"))

        m = self.SM.objects.get()
        self.assertEqual(m.product, self.timber)
        self.assertEqual(m.delta, Decimal("25"))

    def test_reducing_stock_records_a_negative_delta(self):
        self.client.post(reverse("stock_update"),
                         {"reason": "count", f"qty_{self.timber.pk}": "12"})
        self.assertEqual(self.SM.objects.get().delta, Decimal("-28"))

    def test_negative_quantity_is_refused(self):
        self.client.post(reverse("stock_update"),
                         {"reason": "count", f"qty_{self.timber.pk}": "-5"})
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("40"))
        self.assertEqual(self.SM.objects.count(), 0)

    def test_rubbish_input_changes_nothing(self):
        self.client.post(reverse("stock_update"),
                         {"reason": "count", f"qty_{self.timber.pk}": "abc"})
        self.timber.refresh_from_db()
        self.assertEqual(self.timber.stock_qty, Decimal("40"))

    def test_a_sale_also_writes_a_movement(self):
        self.client.post(reverse("sale_create"),
                         {"product_id": [str(self.timber.pk)], "qty": ["4"]})
        m = self.SM.objects.get()
        self.assertEqual(m.reason, "sale")
        self.assertEqual(m.delta, Decimal("-4"))
        self.assertEqual(m.resulting_qty, Decimal("36"))
        self.assertIsNotNone(m.sale)

    def test_history_page_shows_movements(self):
        self.client.post(reverse("stock_update"),
                         {"reason": "received", f"qty_{self.timber.pk}": "70"})
        r = self.client.get(reverse("stock_history", args=[self.timber.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Delivery received")

    def test_stock_update_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("stock_update")).status_code, 302)


class MpesaC2BTests(TestCase):
    """Safaricom's callback is the only thing that may record money."""

    def setUp(self):
        from .models import MpesaTransaction
        self.MT = MpesaTransaction
        self.user = User.objects.create_user("yardstaff", password="pw12345!")
        self.product = Product.objects.create(
            type=Product.TIMBER, name="Cypress", species="Cypress",
            width=Decimal("12"), thickness=Decimal("2"), length=Decimal("9"),
            rate_per_board_foot=Decimal("280"), stock_qty=Decimal("40"))
        self.sale = Sale.objects.create(number="SAL-2026-0042",
                                        customer_name="Kamau Hardware")
        SaleItem.objects.create(sale=self.sale, product=self.product,
                                qty=Decimal("1"), board_feet_per_piece=Decimal("18"),
                                rate=Decimal("280"))
        self.sale.recalculate_total()          # 5,040

    def _post(self, **over):
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "SFG4H2K9LM",
            "TransTime": "20260724143000",
            "TransAmount": "5040.00",
            "BusinessShortCode": "123456",
            "BillRefNumber": "SAL-2026-0042",
            "MSISDN": "2547XXXXX678",
            "FirstName": "JOHN",
            "LastName": "KAMAU",
        }
        payload.update(over)
        return self.client.post(reverse("mpesa_confirmation"),
                                data=json.dumps(payload),
                                content_type="application/json")

    def test_confirmation_records_and_matches_the_payment(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ResultCode"], 0)

        txn = self.MT.objects.get()
        self.assertEqual(txn.trans_id, "SFG4H2K9LM")
        self.assertEqual(txn.amount, Decimal("5040.00"))
        self.assertEqual(txn.payer_name, "JOHN KAMAU")
        self.assertEqual(txn.sale, self.sale)
        self.assertTrue(txn.matched_automatically)

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, Decimal("5040.00"))
        self.assertTrue(self.sale.is_paid)
        payment = self.sale.payments.get()
        self.assertEqual(payment.method, "mpesa")
        self.assertEqual(payment.mpesa_receipt, "SFG4H2K9LM")

    def test_repeated_callbacks_do_not_double_pay(self):
        """Safaricom retries if it doesn't get a clean 200."""
        for _ in range(4):
            self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self.MT.objects.count(), 1)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.payments.count(), 1)
        self.assertEqual(self.sale.amount_paid, Decimal("5040.00"))

    def test_loosely_typed_account_numbers_still_match(self):
        for ref in ["sal 2026 0042", "SAL20260042", " sal-2026-0042 "]:
            Sale.objects.filter(number="SAL-2026-0042").update()
            self.MT.objects.all().delete()
            self.sale.payments.all().delete()
            self._post(TransID=f"T{abs(hash(ref)) % 10**8}", BillRefNumber=ref)
            self.assertTrue(self.MT.objects.get().is_matched, ref)

    def test_wrong_account_number_is_kept_but_unmatched(self):
        self._post(BillRefNumber="NONSENSE")
        txn = self.MT.objects.get()
        self.assertFalse(txn.is_matched)
        self.assertEqual(txn.amount, Decimal("5040.00"))   # money is never lost
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, Decimal("0.00"))

    def test_partial_payment_leaves_the_balance(self):
        self._post(TransAmount="2000.00")
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, Decimal("2000.00"))
        self.assertEqual(self.sale.balance, Decimal("3040.00"))
        self.assertFalse(self.sale.is_paid)

    def test_malformed_payload_still_answers_200(self):
        """Anything but a 200 makes Safaricom retry forever."""
        r = self.client.post(reverse("mpesa_confirmation"),
                             data="not json at all",
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.MT.objects.count(), 0)

    def test_missing_transid_is_ignored_not_stored(self):
        self._post(TransID="")
        self.assertEqual(self.MT.objects.count(), 0)

    def test_validation_endpoint_accepts(self):
        r = self.client.post(reverse("mpesa_validation"),
                             data=json.dumps({}), content_type="application/json")
        self.assertEqual(r.json()["ResultCode"], 0)

    def test_callbacks_need_no_login_or_csrf_token(self):
        """Safaricom has neither a session nor a CSRF token."""
        self.assertEqual(self._post().status_code, 200)

    def test_manual_matching_from_the_reconcile_page(self):
        self._post(BillRefNumber="TYPO")
        txn = self.MT.objects.get()
        self.assertFalse(txn.is_matched)

        self.client.login(username="yardstaff", password="pw12345!")
        self.client.post(reverse("mpesa_reconcile"),
                         {"txn": txn.pk, "sale_number": "SAL-2026-0042"})
        txn.refresh_from_db()
        self.assertTrue(txn.is_matched)
        self.assertFalse(txn.matched_automatically)
        self.assertEqual(txn.matched_by, self.user)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.amount_paid, Decimal("5040.00"))

    def test_cannot_match_the_same_payment_twice(self):
        self._post()
        txn = self.MT.objects.get()
        self.client.login(username="yardstaff", password="pw12345!")
        self.client.post(reverse("mpesa_reconcile"),
                         {"txn": txn.pk, "sale_number": "SAL-2026-0042"})
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.payments.count(), 1)

    def test_reconcile_page_requires_login(self):
        self.assertEqual(self.client.get(reverse("mpesa_reconcile")).status_code, 302)


class FeatureFlagTests(TestCase):
    def setUp(self):
        User.objects.create_user("s4", password="pw12345!")
        self.client.login(username="s4", password="pw12345!")

    def test_mpesa_hidden_from_nav_until_configured(self):
        import os
        old = os.environ.pop("MPESA_CONSUMER_KEY", None)
        try:
            html = self.client.get(reverse("dashboard")).content.decode()
            self.assertNotIn(">M-Pesa<", html)
        finally:
            if old is not None:
                os.environ["MPESA_CONSUMER_KEY"] = old

    def test_mpesa_appears_once_credentials_are_set(self):
        import os
        os.environ["MPESA_CONSUMER_KEY"] = "test-key"
        try:
            html = self.client.get(reverse("dashboard")).content.decode()
            self.assertIn(">M-Pesa<", html)
        finally:
            os.environ.pop("MPESA_CONSUMER_KEY", None)

    def test_the_page_itself_still_works_when_hidden(self):
        """Hidden from the nav, not disabled — you can still reach it directly."""
        import os
        os.environ.pop("MPESA_CONSUMER_KEY", None)
        self.assertEqual(self.client.get(reverse("mpesa_reconcile")).status_code, 200)

class CustomOrderTests(TestCase):
    def setUp(self):
        from .models import CustomOrder, CustomOrderPayment
        self.CO = CustomOrder
        self.COP = CustomOrderPayment
        self.user = User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")

    def _create(self, **over):
        data = {"customer_name": "Jane Wanjiru",
                "description": "Mahogany panel door 3x7ft",
                "quoted_price": "25000"}
        data.update(over)
        return self.client.post(reverse("order_create"), data, follow=True)

    def test_create_order_assigns_number(self):
        self._create()
        order = self.CO.objects.get()
        self.assertEqual(order.number, f"CO-{order.created_at.year}-0001")
        self.assertEqual(order.status, "quoted")
        self.assertEqual(order.quoted_price, Decimal("25000"))
        self.assertEqual(order.balance, Decimal("25000"))

    def test_deposit_at_creation_is_recorded(self):
        self._create(deposit="10000", deposit_method="mpesa",
                     deposit_mpesa="SFG4H2K9LM")
        order = self.CO.objects.get()
        self.assertEqual(order.amount_paid, Decimal("10000"))
        self.assertEqual(order.balance, Decimal("15000"))
        p = order.payments.get()
        self.assertTrue(p.is_deposit)
        self.assertEqual(p.mpesa_receipt, "SFG4H2K9LM")

    def test_missing_fields_are_rejected(self):
        self.client.post(reverse("order_create"),
                         {"customer_name": "", "description": "", "quoted_price": "x"})
        self.assertEqual(self.CO.objects.count(), 0)

    def test_adding_a_payment_reduces_balance(self):
        self._create()
        order = self.CO.objects.get()
        self.client.post(reverse("order_payment", args=[order.pk]),
                         {"amount": "5000", "method": "cash"})
        order.refresh_from_db()
        self.assertEqual(order.amount_paid, Decimal("5000"))
        self.assertEqual(order.balance, Decimal("20000"))

    def test_payment_cannot_exceed_balance(self):
        self._create()
        order = self.CO.objects.get()
        self.client.post(reverse("order_payment", args=[order.pk]),
                         {"amount": "999999", "method": "cash"})
        order.refresh_from_db()
        self.assertEqual(order.amount_paid, Decimal("25000"))
        self.assertTrue(order.is_paid)

    def test_status_transitions(self):
        self._create()
        order = self.CO.objects.get()
        for status in ["in_progress", "ready", "collected"]:
            self.client.post(reverse("order_status", args=[order.pk]),
                             {"status": status})
            order.refresh_from_db()
            self.assertEqual(order.status, status)

    def test_invalid_status_is_ignored(self):
        self._create()
        order = self.CO.objects.get()
        self.client.post(reverse("order_status", args=[order.pk]),
                         {"status": "nonsense"})
        order.refresh_from_db()
        self.assertEqual(order.status, "quoted")

    def test_open_filter_excludes_collected(self):
        self._create()
        o = self.CO.objects.get()
        self._create(customer_name="Second", description="Table", quoted_price="8000")
        o.status = "collected"; o.save()
        r = self.client.get(reverse("order_list") + "?show=open")
        self.assertNotContains(r, o.number)
        self.assertContains(r, "Second")

    def test_pages_load(self):
        self._create()
        order = self.CO.objects.get()
        for url in [reverse("order_list"), reverse("order_create"),
                    reverse("order_detail", args=[order.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_orders_require_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 302)


class DoorTests(TestCase):
    def setUp(self):
        from .models import StockMovement
        self.SM = StockMovement
        self.user = User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")

    def make_door(self, **kw):
        fields = dict(type=Product.DOOR, name="Panel door", species="Mahogany",
                      door_height=2100, door_width=900, door_thickness=45,
                      price=Decimal("25000"), stock_qty=Decimal("4"))
        fields.update(kw)
        return Product.objects.create(**fields)

    def test_door_prices_and_labels(self):
        d = self.make_door()
        self.assertEqual(d.unit_price(), Decimal("25000"))
        self.assertIsNone(d.board_feet)
        self.assertEqual(str(d), "Mahogany Panel door 2100×900mm")
        self.assertEqual(d.door_size_imperial, "≈ 6'11\" × 2'11\"")
        self.assertIn("Main entrance", d.door_standard_label)
        self.assertEqual(self.make_door(door_width=777).door_standard_label, "Custom size")

    def test_pages_load(self):
        d = self.make_door()
        for url in [reverse("door_list"), reverse("door_create"),
                    reverse("door_edit", args=[d.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(reverse("door_list"))
        self.assertContains(r, "900 × 2100 mm")
        self.assertContains(r, 'KSh 25,000')
        self.assertContains(r, reverse("order_create"))      # custom doors route

    def test_add_standard_size_logs_opening_stock(self):
        self.client.post(reverse("door_create"), {
            "name": "Flush door", "species": "Cypress", "size": "2100x800",
            "thickness": "35", "price": "9500", "opening_stock": "6",
            "low_stock_threshold": "2"})
        d = Product.objects.get(type=Product.DOOR)
        self.assertEqual((d.door_height, d.door_width, d.door_thickness), (2100, 800, 35))
        self.assertEqual(d.stock_qty, Decimal("6"))
        m = self.SM.objects.get(product=d)
        self.assertEqual((m.delta, m.reason), (Decimal("6"), "received"))

    def test_add_custom_size(self):
        self.client.post(reverse("door_create"), {
            "name": "Store door", "size": "custom", "height": "1950",
            "width": "650", "thickness": "40", "price": "7000"})
        d = Product.objects.get(type=Product.DOOR)
        self.assertEqual((d.door_height, d.door_width), (1950, 650))
        self.assertFalse(self.SM.objects.exists())     # no opening stock, no movement

    def test_implausible_size_is_refused(self):
        self.client.post(reverse("door_create"), {
            "name": "Odd", "size": "custom", "height": "210", "width": "90",
            "thickness": "45", "price": "7000"})
        self.assertFalse(Product.objects.filter(type=Product.DOOR).exists())

    def test_edit_keeps_stock(self):
        d = self.make_door()
        self.client.post(reverse("door_edit", args=[d.pk]), {
            "name": "Panel door", "species": "Mahogany", "size": "2100x900",
            "thickness": "45", "price": "27000", "opening_stock": "99",
            "low_stock_threshold": "1"})
        d.refresh_from_db()
        self.assertEqual(d.price, Decimal("27000"))
        self.assertEqual(d.stock_qty, Decimal("4"))

    def test_sell_button_only_when_in_stock(self):
        d = self.make_door()
        gone = self.make_door(name="Flush door", stock_qty=Decimal("0"))
        r = self.client.get(reverse("door_list"))
        self.assertContains(r, f'{reverse("sale_create")}?add={d.pk}"')
        self.assertNotContains(r, f'?add={gone.pk}"')
        self.assertContains(r, "Out of stock")

    def test_selling_a_door(self):
        d = self.make_door()
        self.assertContains(self.client.get(reverse("sale_create")), "Doors")
        self.client.post(reverse("sale_create"), {
            "product_id": [str(d.pk)], "qty": ["2"]})
        item = SaleItem.objects.get()
        self.assertEqual(item.amount, Decimal("50000.00"))
        self.assertIsNone(item.board_feet_per_piece)
        d.refresh_from_db()
        self.assertEqual(d.stock_qty, Decimal("2"))


    def test_inch_size_matches_how_customers_ask(self):
        d = self.make_door(door_height=2032, door_width=813)
        self.assertEqual(d.door_size_inches, '32" × 80"')

    def test_thickness_is_optional(self):
        self.client.post(reverse("door_create"), {
            "name": "Panel door", "size": "2032x813", "thickness": "",
            "price": "35000"})
        d = Product.objects.get(type=Product.DOOR)
        self.assertIsNone(d.door_thickness)
        self.assertEqual(self.client.get(reverse("door_edit", args=[d.pk])).status_code, 200)


class DoorPhotoTests(TestCase):
    def setUp(self):
        import tempfile
        from django.test import override_settings
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        User.objects.create_user("yardstaff", password="pw12345!")
        self.client.login(username="yardstaff", password="pw12345!")

    def tearDown(self):
        import shutil
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def png(self, w, h):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (w, h), (200, 140, 100)).save(buf, "PNG")
        return SimpleUploadedFile("door.png", buf.getvalue(), content_type="image/png")

    def post(self, **extra):
        data = {"name": "Panel door", "species": "Mahogany", "size": "2032x813",
                "thickness": "40", "price": "35000"}
        data.update(extra)
        return self.client.post(reverse("door_create"), data)

    def test_photo_is_saved_as_a_shrunk_jpeg(self):
        from PIL import Image
        self.post(photo=self.png(1800, 2400))
        d = Product.objects.get(type=Product.DOOR)
        self.assertTrue(d.photo.name.endswith(".jpg"))
        with Image.open(d.photo.path) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertLessEqual(max(img.size), 1400)
        self.assertContains(self.client.get(reverse("door_list")), d.photo.url)

    def test_non_image_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.post(photo=SimpleUploadedFile("door.png", b"not an image"))
        self.assertFalse(Product.objects.filter(type=Product.DOOR).exists())

    def test_photo_can_be_replaced_and_removed(self):
        import os
        self.post(photo=self.png(300, 400))
        d = Product.objects.get(type=Product.DOOR)
        first = d.photo.path
        edit = {"name": "Panel door", "size": "2032x813", "thickness": "40", "price": "35000"}
        self.client.post(reverse("door_edit", args=[d.pk]), {**edit, "photo": self.png(300, 400)})
        d.refresh_from_db()
        self.assertFalse(os.path.exists(first))          # old file cleaned up
        self.client.post(reverse("door_edit", args=[d.pk]), {**edit, "remove_photo": "on"})
        d.refresh_from_db()
        self.assertFalse(d.photo)
