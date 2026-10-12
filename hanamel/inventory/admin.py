from django.contrib import admin

from .models import (BusinessProfile, CustomOrder, CustomOrderPayment, Payment,
                     Product, RecutLoss, Sale, SaleItem, StockMovement)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "species", "dimensions", "unit_price", "stock_qty", "is_active")
    list_filter = ("type", "species", "is_active")
    search_fields = ("name", "species")

    @admin.display(description="Dimensions (in x in x ft)")
    def dimensions(self, obj):
        if obj.type == Product.TIMBER:
            return f"{obj.thickness_display} x {obj.width_display} x {obj.length_display}"
        if obj.type == Product.DOOR:
            return obj.door_size or "-"
        return "-"


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    readonly_fields = ("amount",)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not BusinessProfile.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("number", "customer_name", "date", "total", "amount_paid", "balance")
    search_fields = ("number", "customer_name")
    inlines = [SaleItemInline, PaymentInline]
    readonly_fields = ("total",)


@admin.register(RecutLoss)
class RecutLossAdmin(admin.ModelAdmin):
    list_display = ("created_at", "source_product", "sold_width", "sold_thickness",
                    "sold_length", "qty", "board_feet_lost", "value_lost", "sale")
    list_filter = ("source_product__species",)
    search_fields = ("source_product__name", "sale__number")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "delta", "resulting_qty", "reason", "user")
    list_filter = ("reason", "created_at")
    search_fields = ("product__name", "product__species", "note")
    readonly_fields = ("created_at",)

class CustomOrderPaymentInline(admin.TabularInline):
    model = CustomOrderPayment
    extra = 0


@admin.register(CustomOrder)
class CustomOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "customer_name", "status", "quoted_price",
                    "amount_paid", "balance", "target_date")
    list_filter = ("status",)
    search_fields = ("number", "customer_name", "description")
    inlines = [CustomOrderPaymentInline]
