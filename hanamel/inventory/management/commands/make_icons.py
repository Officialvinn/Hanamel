"""Generate the nav logo, favicons and home-screen icons from the master logo.

    python manage.py make_icons

Put the full-resolution logo at brand/logo-source.png. Re-run this whenever
the logo changes, then collectstatic. Everything it writes lands in
inventory/static/img/ and is safe to commit.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build favicons, app icons and the nav mark from brand/logo-source.png"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="brand/logo-source.png")
        parser.add_argument("--out", default="inventory/static/img")

    def handle(self, *args, **opts):
        try:
            from PIL import Image
            import numpy as np
        except ImportError as exc:
            raise CommandError("Needs Pillow and numpy: pip install pillow numpy") from exc

        base = Path(settings.BASE_DIR)
        src_path = base / opts["source"]
        if not src_path.exists():
            raise CommandError(f"No logo at {src_path}")
        out = base / opts["out"]
        out.mkdir(parents=True, exist_ok=True)

        src = Image.open(src_path).convert("RGBA")

        def drop_white(img):
            """White background -> transparent, so the mark sits on any colour."""
            arr = np.array(img)
            white = ((arr[:, :, 0] > 244) & (arr[:, :, 1] > 244) & (arr[:, :, 2] > 244))
            arr[white] = [255, 255, 255, 0]
            return Image.fromarray(arr)

        # The logo is a stacked lockup: monogram, then wordmark, then taglines.
        # Only the monogram survives being shrunk to 30px, so find the top band
        # by looking for the first run of rows containing ink.
        grey = np.array(src.convert("RGB")).sum(axis=2)
        rows = (grey < 700).sum(axis=1)
        bands, start = [], None
        for i, v in enumerate(rows):
            if v > 3 and start is None:
                start = i
            elif v <= 3 and start is not None:
                if i - start > 12:
                    bands.append((start, i))
                start = None
        if start is not None:
            bands.append((start, len(rows)))
        if not bands:
            raise CommandError("Could not find any artwork in the logo")
        y0, y1 = bands[0]

        mark = src.crop((0, max(0, y0 - 7), src.width, min(src.height, y1 + 7)))
        m = (np.array(mark.convert("RGB")).sum(axis=2) < 700)
        cols = np.where(m.sum(axis=0) > 2)[0]
        mark = drop_white(mark.crop((max(0, cols.min() - 6), 0,
                                     min(mark.width, cols.max() + 7), mark.height)))
        mark.save(out / "logo-mark.png")

        h = 80                                   # 2x the 30px it renders at
        mark.resize((round(mark.width * h / mark.height), h),
                    Image.LANCZOS).save(out / "logo-mark-80.png")

        full = drop_white(src)
        full = full.crop(full.getbbox())
        full.resize((600, round(full.height * 600 / full.width)),
                    Image.LANCZOS).save(out / "logo-full.png")

        def square(size, pad, name):
            canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
            inner = int(size * (1 - pad * 2))
            scale = min(inner / mark.width, inner / mark.height)
            w, hh = round(mark.width * scale), round(mark.height * scale)
            small = mark.resize((w, hh), Image.LANCZOS)
            canvas.paste(small, ((size - w) // 2, (size - hh) // 2), small)
            canvas.save(out / name)
            self.stdout.write(f"  {name}  {size}x{size}")

        square(192, 0.10, "icon-192.png")
        square(512, 0.10, "icon-512.png")
        square(512, 0.22, "icon-maskable-512.png")   # Android crops to a circle
        square(180, 0.08, "apple-touch-icon.png")
        square(32, 0.04, "favicon-32.png")
        square(16, 0.02, "favicon-16.png")
        Image.open(out / "icon-512.png").save(
            out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        self.stdout.write(self.style.SUCCESS(f"\nIcons written to {out}"))