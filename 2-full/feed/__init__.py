"""Live BTC price feed for Polymarket 5-minute market research.

Polymarket's BTC Up/Down 5m markets resolve against Chainlink's BTC/USD data
stream, so this package treats Binance as a high-fidelity *proxy* and works to
make that proxy measurable rather than assumed: window opens and closes are
sampled on exchange-corrected time, a true 60s time-weighted average is tracked
alongside spot, and a second venue runs in parallel to quantify the residual
cross-venue basis.
"""

from .types import Side, Tick, VenueState
from .windows import FiveMinuteTracker, WindowResult, market_slug, window_start

__all__ = [
    "Side",
    "Tick",
    "VenueState",
    "FiveMinuteTracker",
    "WindowResult",
    "market_slug",
    "window_start",
]
