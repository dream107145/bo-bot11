"""Digital pricers for the 5m up/down markets.

The Student-t(4) default is troll-poly-bot's pricing/digital.py prior (crypto
returns at 1-5 minute horizons are fat-tailed, so the Gaussian overstates
confidence exactly where these markets trade). That module evaluated it with
scipy's ``stdtr``; four degrees of freedom has a closed form, so this one needs
no scipy -- the same formula Bo-BOTT's console draws with (``t4cdf``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def t4_cdf(z: float) -> float:
    """Unit-variance Student-t(4) CDF: t = z * sqrt(nu / (nu - 2)) = z * sqrt(2)."""
    t = z * SQRT2
    return 0.5 + t * (t * t + 6.0) / (2.0 * (t * t + 4.0) ** 1.5)


@dataclass(slots=True)
class FairValue:
    """A fair price plus how much to trust it (same fields as troll-poly-bot's)."""
    p_up: float
    z: float
    sigma_per_sec: float
    effective_horizon_s: float
    #: 1-sigma band on p_up implied by a 25% error in the vol estimate
    uncertainty: float

    @property
    def p_down(self) -> float:
        return 1.0 - self.p_up


class Pricer(Protocol):
    def cdf(self, z: float) -> float: ...


class GaussianPricer:
    def cdf(self, z: float) -> float:
        return norm_cdf(z)


class StudentT4Pricer:
    def cdf(self, z: float) -> float:
        return min(max(t4_cdf(z), 0.0), 1.0)


DEFAULT_PRICER: Pricer = StudentT4Pricer()
