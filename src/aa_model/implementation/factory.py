"""Implementation engine factory.

Resolves ``implementation.engine`` (from base config) into an
:class:`ImplementationAdapter` instance. Each non-stub adapter is imported
lazily so the package can run without optional optimizer dependencies.
"""

from __future__ import annotations

from aa_model.implementation.base import ImplementationAdapter
from aa_model.implementation.stub import StubImplementation


def make_implementation(*, engine: str) -> ImplementationAdapter:
    if engine == "stub":
        return StubImplementation()
    if engine == "cvxportfolio":
        # Lazy import: keeps cvxportfolio optional unless explicitly enabled.
        from aa_model.implementation.cvxportfolio_adapter import (
            CvxportfolioImplementation,
        )

        return CvxportfolioImplementation()
    raise ValueError(f"unknown implementation engine: {engine!r}")
