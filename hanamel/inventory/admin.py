from django.contrib import admin

from .models import BusinessProfile, Payment, Product, Sale, SaleItem


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "species", "dimensions", "unit_price", "stock_qty", "is_active")
    list_filter = ("type", "species", "is_active")
    search_fields = ("name", "species")

    @admin.display(description="Dimensions (in x in x ft)")
    def dimensions(self, obj):
        if obj.type == Product.TIMBER:
            return f"{obj.thickness} x {obj.width} x {obj.length}"
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