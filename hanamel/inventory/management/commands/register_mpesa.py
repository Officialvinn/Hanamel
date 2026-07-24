"""Register this server's callback URLs with Safaricom. Run once per shortcode
(and again if the domain changes).

    python manage.py register_mpesa --host https://hanamel.alvinn.tech
"""
from django.core.management.base import BaseCommand, CommandError

from inventory import mpesa


class Command(BaseCommand):
    help = "Tell Safaricom where to send C2B payment notifications"

    def add_arguments(self, parser):
        parser.add_argument("--host", required=True,
                            help="Public https base URL, no trailing slash")
        parser.add_argument("--response-type", default="Completed",
                            choices=["Completed", "Cancelled"])

    def handle(self, *args, **opts):
        host = opts["host"].rstrip("/")
        if not host.startswith("https://"):
            raise CommandError("Safaricom only accepts https callback URLs")

        conf = f"{host}/mpesa/confirmation/"
        val = f"{host}/mpesa/validation/"
        cfg = mpesa.config()
        self.stdout.write(f"Shortcode : {cfg['shortcode'] or '(not set)'}")
        self.stdout.write(f"Endpoint  : {cfg['base']}")
        self.stdout.write(f"Confirm   : {conf}")
        self.stdout.write(f"Validate  : {val}\n")

        try:
            status, body = mpesa.register_urls(conf, val, opts["response_type"])
        except Exception as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"HTTP {status}\n{body}")
        if status == 200 and "success" in body.lower():
            self.stdout.write(self.style.SUCCESS("\nRegistered."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nSafaricom did not confirm success — check the response above."))