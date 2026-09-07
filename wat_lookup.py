"""
wat_lookup.py -- thin shim, kept for backward compatibility with existing imports.

All WAT logic now lives in the shared `tamarack-wat-tables` package:
    https://github.com/volocchio/tamarack-wat-tables

A single source of truth across A320_737_Sightings, Tamarack_Mission_Analysis,
and any other Tamarack tool that needs WAT lookups. Update the tables there,
redeploy here, numbers refresh everywhere.

If you used to do:
    from wat_lookup import wat_max_weight, wat_analysis, MTOW, oei_gradient_analysis
…that still works — these names are re-exported below from the shared package.
"""

from tamarack_wat import (
    MTOW,
    wat_max_weight,
    wat_analysis,
    oei_gradient_analysis,
    # Also expose the low-level / TMA-style API in case future code needs flap-aware lookups
    lookup_wat_max_weight,
    lookup_wat_max_temp,
    use_published_tables,
    use_corrected_tables,
)

__all__ = [
    "MTOW",
    "wat_max_weight", "wat_analysis", "oei_gradient_analysis",
    "lookup_wat_max_weight", "lookup_wat_max_temp",
    "use_published_tables", "use_corrected_tables",
]
