# -*- coding: utf-8 -*-

def migrate(cr, version):
    """Break inherited related fields so folio lines keep their own stay dates."""
    cr.execute(
        """
        UPDATE ir_model_fields
        SET related = NULL,
            depends = NULL
        WHERE model = 'hotel.booking.line'
          AND name IN ('check_in', 'check_out')
        """
    )
