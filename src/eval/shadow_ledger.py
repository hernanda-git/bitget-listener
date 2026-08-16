"""ShadowLedger (reflexivity) — per-pair win-rate that flags and downsizes weak pairs.

Grafted from mexc-listener, the strongest self-correcting layer of the four. Two
deliberate conservatism rules:

- a rating is only *trusted* after ``min_trades`` samples, so three unlucky trades
  cannot switch off a pair;
- ``auto_downsize`` is OFF by default. Sizing is only reduced when the operator
  explicitly enables it (never silently, never up-sized).

``expectancy`` is fee-aware because it consumes ``net_usd`` from the ledger, which
is already gross minus both fees. It is one of the go-live gate conditions.
"""

from __future__ import annotations

from dataclasses import dataclass


class EvalStore:
    """Port: outcome history per pair."""

    def record(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        raise NotImplementedError

    def get(self, pair: str) -> list[bool]:
        raise NotImplementedError

    def nets(self, pair: str | None = None) -> list[float]:
        raise NotImplementedError


class InMemoryEvalStore(EvalStore):
    def __init__(self) -> None:
        self._wins: dict[str, list[bool]] = {}
        self._nets: dict[str, list[float]] = {}

    def record(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        p = pair.upper()
        self._wins.setdefault(p, []).append(bool(win))
        self._nets.setdefault(p, []).append(float(net_usd))

    def get(self, pair: str) -> list[bool]:
        return list(self._wins.get(pair.upper(), []))

    def nets(self, pair: str | None = None) -> list[float]:
        if pair is None:
            return [n for v in self._nets.values() for n in v]
        return list(self._nets.get(pair.upper(), []))


class LedgerEvalStore(EvalStore):
    """Adapts ``TradeLedger`` to the eval port so ShadowLedger reads real trades."""

    def __init__(self, ledger) -> None:
        self.ledger = ledger

    def record(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        self.ledger.record_outcome(pair, win, net_usd)

    def get(self, pair: str) -> list[bool]:
        return self.ledger.outcomes_for(pair)

    def nets(self, pair: str | None = None) -> list[float]:
        if pair is None:
            return self.ledger.net_pnls()
        return self.ledger.nets_for(pair)


@dataclass(frozen=True)
class Rating:
    pair: str
    trades: int
    wins: int
    losses: int
    winrate: float
    trusted: bool
    flag: bool
    expectancy: float = 0.0


class ShadowLedger:
    def __init__(self, store: EvalStore | None = None, min_trades: int = 30,
                 poor_winrate: float = 0.50, auto_downsize: bool = False,
                 downsize_factor: float = 0.5) -> None:
        self.store = store or InMemoryEvalStore()
        self.min_trades = min_trades
        self.poor_winrate = poor_winrate
        self.auto_downsize = auto_downsize
        self.downsize_factor = downsize_factor

    def record(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        self.store.record(pair, win, net_usd)

    def rating(self, pair: str) -> Rating:
        outcomes = self.store.get(pair)
        trades = len(outcomes)
        wins = sum(1 for o in outcomes if o)
        nets = self.store.nets(pair)
        return Rating(
            pair=pair.upper(), trades=trades, wins=wins, losses=trades - wins,
            winrate=(wins / trades) if trades else 0.0,
            trusted=trades >= self.min_trades,
            flag=trades >= self.min_trades and (wins / trades) < self.poor_winrate,
            expectancy=(sum(nets) / len(nets)) if nets else 0.0,
        )

    def size_multiplier(self, pair: str) -> float:
        """Never > 1.0: this layer can only reduce risk, never increase it."""
        if not self.auto_downsize:
            return 1.0
        return self.downsize_factor if self.rating(pair).flag else 1.0

    def expectancy(self, pair: str | None = None) -> float:
        nets = self.store.nets(pair)
        return (sum(nets) / len(nets)) if nets else 0.0

    def expectancy_positive(self, pair: str | None = None, min_samples: int = 1) -> bool:
        """Go-live condition (b): fee-aware expectancy must be strictly > 0."""
        nets = self.store.nets(pair)
        if len(nets) < max(1, min_samples):
            return False
        return (sum(nets) / len(nets)) > 0

    def weak_pairs(self) -> list[str]:
        pairs = set()
        for p in getattr(self.store, "_wins", {}):
            if self.rating(p).flag:
                pairs.add(p)
        return sorted(pairs)
