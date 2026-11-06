"""J.A.R.V.I.S. platform layer built on top of Odysseus.

Everything here is additive: Jarvis keeps its own database (data/jarvis/jarvis.db)
and reads Odysseus state only through public helpers, so upstream Odysseus data
stays untouched and Jarvis data survives swapping the underlying engine.
"""
