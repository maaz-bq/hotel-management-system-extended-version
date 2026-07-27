# -*- coding: utf-8 -*-

def migrate(cr, version):
    """Ensure folio line stay dates are independent stored fields, not related."""
    cr.execute(
        """
        UPDATE ir_model_fields
        SET related = NULL,
            depends = NULL
        WHERE model = 'hotel.booking.line'
          AND name IN ('check_in', 'check_out')
        """
    )
