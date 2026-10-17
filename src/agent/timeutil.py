"""Time helpers. All internal times are aware UTC datetimes; IST is only for calendar-day and quiet-hour rules."""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ts(s):
    if s is None or isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt):
    return ts(dt).astimezone(IST).date().isoformat()


def age_s(then, now):
    return None if then is None else round((ts(now) - ts(then)).total_seconds(), 1)
