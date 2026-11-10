"""Query-parameter parsing for event, resource and archive lists (D2)."""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from .models import EventResource, PublicationStatus

try:
    LAGOS = ZoneInfo('Africa/Lagos')
except ZoneInfoNotFoundError:  # no tz database in the image; Lagos is fixed UTC+1
    LAGOS = dt_timezone(timedelta(hours=1), 'Africa/Lagos')

STATUS_ALL = 'all'
STATUS_VALUES = [*PublicationStatus.values, STATUS_ALL]
TRUE_VALUES = {'true', '1'}
FALSE_VALUES = {'false', '0'}


def _choice(params, name, choices, errors):
    value = params.get(name)
    if value is None or value == '':
        return None
    if value not in choices:
        errors[name] = [f'Select one of: {", ".join(choices)}.']
        return None
    return value


def _date(params, name, errors):
    value = params.get(name)
    if value is None or value == '':
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        errors[name] = ['Enter a valid date in YYYY-MM-DD format.']
        return None


def _boolean(params, name, errors):
    value = params.get(name)
    if value is None or value == '':
        return None
    lowered = value.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    errors[name] = ['Enter true or false.']
    return None


def lagos_day_start(day):
    """Start of a Lagos calendar day as an aware UTC datetime."""
    return datetime.combine(day, time.min, tzinfo=LAGOS).astimezone(dt_timezone.utc)


def parse_status(params, errors):
    return _choice(params, 'status', STATUS_VALUES, errors)


def parse_content_filters(params, errors):
    """Shared search/speaker/category/date filters, evaluated on the event."""
    filters = {
        'search': (params.get('search') or '').strip() or None,
        'speaker': (params.get('speaker') or '').strip() or None,
        'category': (params.get('category') or '').strip() or None,
        'date_from': _date(params, 'date_from', errors),
        'date_to': _date(params, 'date_to', errors),
    }
    if filters['date_from'] and filters['date_to'] and filters['date_from'] > filters['date_to']:
        errors['date_to'] = ['date_to must be on or after date_from.']
    return filters


def apply_content_filters(queryset, filters, prefix='', search_resource_title=False):
    """Apply shared filters. ``prefix`` is ``'event__'`` for resource querysets."""
    if filters['search']:
        term = filters['search']
        condition = (Q(**{f'{prefix}title__icontains': term})
                     | Q(**{f'{prefix}summary__icontains': term})
                     | Q(**{f'{prefix}tags__icontains': term}))
        if search_resource_title:
            condition |= Q(title__icontains=term)
        queryset = queryset.filter(condition)
    if filters['speaker']:
        queryset = queryset.filter(**{f'{prefix}speakers__icontains': filters['speaker']})
    if filters['category']:
        queryset = queryset.filter(**{f'{prefix}category__iexact': filters['category']})
    if filters['date_from']:
        queryset = queryset.filter(**{f'{prefix}start_time__gte': lagos_day_start(filters['date_from'])})
    if filters['date_to']:
        end = lagos_day_start(filters['date_to'] + timedelta(days=1))
        queryset = queryset.filter(**{f'{prefix}start_time__lt': end})
    return queryset


def parse_type(params, errors):
    return _choice(params, 'type', EventResource.Type.values, errors)


def parse_upcoming(params, errors):
    return _boolean(params, 'upcoming', errors)


def raise_if(errors):
    if errors:
        raise ValidationError(errors)
