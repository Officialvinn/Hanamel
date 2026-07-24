from django.contrib import admin
from django.urls import include, path

from inventory import views as inventory_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # both must sit at the site root: the worker's scope and the manifest's start_url
    path("site.webmanifest", inventory_views.site_manifest, name="site_manifest"),
    path("sw.js", inventory_views.service_worker, name="service_worker"),
    # Safaricom posts here. Public by necessity, so they live at the root
    # rather than behind the app's login-required URLs.
    path("mpesa/validation/", inventory_views.mpesa_validation, name="mpesa_validation"),
    path("mpesa/confirmation/", inventory_views.mpesa_confirmation, name="mpesa_confirmation"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("inventory.urls")),
]