from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        if hasattr(queryset, 'query'):
            ordering = queryset.query.order_by or queryset.model._meta.ordering or ()
            pk = queryset.model._meta.pk.name
            if not any(str(field).lstrip('-') in {'pk', pk} for field in ordering):
                ordering = (*ordering, pk)
            queryset = queryset.order_by(*ordering)
        return super().paginate_queryset(queryset, request, view)
