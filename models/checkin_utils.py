# -*- coding: utf-8 -*-

from odoo import fields


def truncate_minutes_seconds(value):
    """Return datetime with minute, second, and microsecond set to zero."""
    if not value:
        return value
    dt = fields.Datetime.to_datetime(value)
    return dt.replace(minute=0, second=0, microsecond=0)
