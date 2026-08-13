from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("products/", views.product_list, name="product_list"),
    path("products/update/", views.stock_update, name="stock_update"),
    path("products/<int:pk>/history/", views.stock_history, name="stock_history"),
    path("orders/", views.order_list, name="order_list"),
    path("orders/new/", views.order_create, name="order_create"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/status/", views.order_status, name="order_status"),
    path("orders/<int:pk>/payment/", views.order_payment, name="order_payment"),
    path("sales/", views.sale_list, name="sale_list"),
    path("sales/new/", views.sale_create, name="sale_create"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/invoice/", views.sale_invoice, name="sale_invoice"),
    path("mpesa/", views.mpesa_reconcile, name="mpesa_reconcile"),
    path("reports/", views.reports, name="reports"),
    path("reports/<int:year>/<int:month>/export/", views.export_month, name="export_month"),
    path("reports/<int:year>/<int:month>/invoices/", views.invoice_pack, name="invoice_pack"),
]