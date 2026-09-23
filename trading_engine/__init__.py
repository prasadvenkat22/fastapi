# Operator overrides (trading_overrides.env) go into os.environ before any
# module in this package reads a knob at import. See settings_overrides.py.
from . import settings_overrides as _settings_overrides

_settings_overrides.apply()
