"""TEXT-0: point-in-time SEC EDGAR text archive (collect only -- no trading
logic, no scanner, no AI calls, no scoring). See
``alphaos/text_archive/service.py`` for the fetch/store pipeline and
``alphaos/text_archive/forms.py`` for the form catalog.
"""

from alphaos.text_archive.forms import EDGAR_FORMS_V1

__all__ = ["EDGAR_FORMS_V1"]
