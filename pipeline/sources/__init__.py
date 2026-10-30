"""Trade source adapters. Each one turns a source's format into ``RawTrade`` records."""
from .base import Member, RawTrade, TradeSource
from .capitol_api import CapitolApiSource
from .senate_efd import SenateEfdSource

__all__ = ["Member", "RawTrade", "TradeSource", "CapitolApiSource", "SenateEfdSource"]
