from django.contrib import admin
from django.urls import include, path

from inventory import views as inventory_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # both must sit at the site root: the worker's scope and the manifest's start_url
    path("site.webmanifest", inventory_views.site_manifest, name="site_manifest"),
    path("sw.js", inventory_views.service_worker, name="service_worker"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("inventory.urls")),
]