"""
wat_lookup.py — optional compatibility shim for inherited CJ/ATLAS analytics.

The A320/737 tracker does not require Tamarack WAT tables. If the private
`tamarack-wat-tables` package is unavailable, expose harmless placeholders so
legacy dashboard panels fail soft instead of preventing the app from starting.
"""

try:
    from tamarack_wat import (  # type: ignore
        MTOW,
        wat_max_weight,
        wat_analysis,
        oei_gradient_analysis,
        lookup_wat_max_weight,
        lookup_wat_max_temp,
        use_published_tables,
        use_corrected_tables,
    )
except ImportError:
    MTOW = {}

    def _unavailable(*_args, **_kwargs):
        return {
            "available": False,
            "reason": "tamarack-wat-tables is not installed in this A320/737 deployment",
        }

    wat_max_weight = _unavailable
    wat_analysis = _unavailable
    oei_gradient_analysis = _unavailable
    lookup_wat_max_weight = _unavailable
    lookup_wat_max_temp = _unavailable

    def use_published_tables():
        return False

    def use_corrected_tables():
        return False

__all__ = [
    "MTOW",
    "wat_max_weight", "wat_analysis", "oei_gradient_analysis",
    "lookup_wat_max_weight", "lookup_wat_max_temp",
    "use_published_tables", "use_corrected_tables",
]
