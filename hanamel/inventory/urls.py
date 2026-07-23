from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("products/", views.product_list, name="product_list"),
    path("sales/", views.sale_list, name="sale_list"),
    path("sales/new/", views.sale_create, name="sale_create"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/invoice/", views.sale_invoice, name="sale_invoice"),
    path("reports/", views.reports, name="reports"),
    path("reports/<int:year>/<int:month>/export/", views.export_month, name="export_month"),
]