"""Capital market assumptions.

Phase 1 introduced the empty data class; Phase 5 wires it to a config-side
:class:`CMAConfig` (loaded from ``configs/cma.yaml``) that the orchestrator
hands to the allocator. The stub allocator still ignores CMA; the
riskfolio adapter consumes the populated values directly. The Phase 4b
cost-aware allocator does **not** consume CMA — see
MODEL_DOCUMENTATION.md §Phase 5 design / decision C.

The default-constructed (empty) :class:`CMA` is reserved for **test-only**
paths where the riskfolio fallback to ``_DEFAULT_VOL_ANNUAL`` is still
valid. Production runs go through the orchestrator, which builds a
populated :class:`CMA` from the loaded :class:`CMAConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from aa_model.io.schemas import CMAConfig


@dataclass(frozen=True)
class CMA:
    """Annualized capital market assumptions for the public side.

    Attributes:
        expected_returns_annual: index = bucket, values = annual mean return.
        vol_annual: index = bucket, values = annual volatility.
        corr: bucket × bucket correlation matrix.
        liquidity: bucket → 'liquid' | 'semi_liquid' | 'illiquid' (optional).
    """

    expected_returns_annual: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    vol_annual: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    corr: pd.DataFrame = field(default_factory=pd.DataFrame)
    liquidity: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))

    @classmethod
    def from_config(cls, cfg: CMAConfig) -> CMA:
        """Construct a populated :class:`CMA` from a validated
        :class:`CMAConfig`. Index ordering is sorted by bucket name so two
        equivalent CMAConfigs produce byte-identical CMA dataclasses.
        """
        buckets = sorted(cfg.expected_returns_annual.keys())
        er = pd.Series(
            [float(cfg.expected_returns_annual[b]) for b in buckets],
            index=buckets,
            dtype=float,
        )
        vol = pd.Series(
            [float(cfg.vol_annual[b]) for b in buckets],
            index=buckets,
            dtype=float,
        )
        corr_arr = np.array(
            [[float(cfg.correlations[i][j]) for j in buckets] for i in buckets],
            dtype=float,
        )
        corr = pd.DataFrame(corr_arr, index=buckets, columns=buckets)
        liq = (
            pd.Series(
                {b: str(cfg.liquidity[b]) for b in buckets},
                dtype=object,
            )
            if cfg.liquidity is not None
            else pd.Series(dtype=object)
        )
        return cls(
            expected_returns_annual=er,
            vol_annual=vol,
            corr=corr,
            liquidity=liq,
        )
