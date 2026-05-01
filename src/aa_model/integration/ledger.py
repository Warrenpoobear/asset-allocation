"""Quarterly ledger — the spine.

A single tidy long-format ``DataFrame`` of atomic flows. Every module produces or
consumes rows here. Flows are appended in any order; ``finalize()`` sorts them
into canonical intra-quarter order and chains ``nav_start_usd`` / ``nav_end_usd``
per (run_id, bucket). ``validate()`` enforces every invariant in SPEC §5.1.
"""

from __future__ import annotations

import pandas as pd

# Canonical intra-quarter order (SPEC §5.1, extended in P3b).
#
# ``transaction_cost`` is a Phase 3b extension. It models cost paid to the
# market when executing rebalance trades (brokerage, slippage). Mechanically
# it behaves like an external outflow on the cash bucket — a negative
# amount_usd on `cash`, no offset elsewhere — and counts toward the
# household's net external cash flow that quarter (alongside `inflow` and
# `spend`). Total NAV conservation has been relaxed to include
# transaction_cost in the contributing-flows set; see validate().
FLOW_ORDER: tuple[str, ...] = (
    "inflow",
    "return",
    "pe_call",
    "pe_distribution",
    "pe_nav_mark",
    "spend",
    "rebalance",
    "transaction_cost",
)
_FLOW_RANK: dict[str, int] = {f: i for i, f in enumerate(FLOW_ORDER)}

LEDGER_COLUMNS: tuple[str, ...] = (
    "quarter",
    "bucket",
    "flow_type",
    "amount_usd",
    "nav_start_usd",
    "nav_end_usd",
    "source",
    "run_id",
)


class QuarterlyLedger:
    """Append-then-finalize ledger.

    Append ``flow_type`` rows in any order via :meth:`add`. Call :meth:`finalize`
    once at end of run to produce the chained ``DataFrame``. ``finalize`` is
    idempotent; further :meth:`add` calls after finalize raise ``RuntimeError``.
    """

    def __init__(
        self,
        run_id: str,
        *,
        initial_nav: dict[str, float],
        start_quarter: pd.Period,
    ) -> None:
        self.run_id = run_id
        self._initial_nav: dict[str, float] = {k: float(v) for k, v in initial_nav.items()}
        self._start_quarter = start_quarter
        self._rows: list[dict] = []
        self._finalized: bool = False
        self._frame: pd.DataFrame | None = None

    @property
    def initial_nav(self) -> dict[str, float]:
        return dict(self._initial_nav)

    def add(
        self,
        *,
        quarter: pd.Period,
        bucket: str,
        flow_type: str,
        amount_usd: float,
        source: str,
    ) -> None:
        if self._finalized:
            raise RuntimeError("ledger already finalized; cannot add more rows")
        if flow_type not in _FLOW_RANK:
            raise ValueError(f"unknown flow_type {flow_type!r}; valid: {list(FLOW_ORDER)}")
        amt = float(amount_usd)
        if amt != amt:  # NaN check
            raise ValueError(f"amount_usd is NaN for ({quarter}, {bucket}, {flow_type})")
        self._rows.append(
            {
                "quarter": quarter,
                "bucket": bucket,
                "flow_type": flow_type,
                "amount_usd": amt,
                "source": source,
                "run_id": self.run_id,
            }
        )

    def _compute_view(self, max_quarter: pd.Period | None = None) -> pd.DataFrame:
        """Internal: chained view of currently-appended rows up to ``max_quarter``.

        ``max_quarter=None`` returns every row. Used by :meth:`finalize` (which
        also flips the lock) and by :meth:`closed_through` (which does not).
        """
        if not self._rows:
            return self._empty_frame()

        df = pd.DataFrame(self._rows)
        if max_quarter is not None:
            df = df[df["quarter"] <= max_quarter]
            if df.empty:
                return self._empty_frame()

        df = df.copy()
        df["_flow_rank"] = df["flow_type"].map(_FLOW_RANK)
        df = df.sort_values(
            by=["quarter", "bucket", "_flow_rank", "source"], kind="mergesort"
        ).reset_index(drop=True)
        df = df.drop(columns="_flow_rank")

        # Vectorized nav chain: per bucket, nav_end = initial + cumulative sum of amounts.
        bucket_initial = df["bucket"].map(self._initial_nav).fillna(0.0).astype(float)
        cum = df.groupby("bucket", sort=False)["amount_usd"].cumsum()
        df["nav_end_usd"] = bucket_initial + cum
        df["nav_start_usd"] = df["nav_end_usd"] - df["amount_usd"]
        df["run_id"] = self.run_id

        df = df[list(LEDGER_COLUMNS)]
        return df

    def finalize(self) -> pd.DataFrame:
        if self._finalized:
            assert self._frame is not None
            return self._frame
        self._frame = self._compute_view()
        self._finalized = True
        return self._frame

    def closed_through(self, quarter: pd.Period) -> pd.DataFrame:
        """Read-only chained view of rows with ``quarter`` ≤ the given quarter.

        Phase 4a primitive (SPEC §Phase 4 design / state-flow contract). Does
        **not** mutate the ledger and does **not** lock further appends; the
        ledger continues to accept :meth:`add` calls afterward. The returned
        frame has the same schema as :meth:`finalize` and is safe to pass to
        downstream code that consumes ledger views.
        """
        return self._compute_view(max_quarter=quarter)

    def end_nav_through(self, quarter: pd.Period) -> pd.Series:
        """End-of-quarter NAV per bucket at ``quarter`` (or initial NAV if the
        bucket has no rows at or before ``quarter``).

        Phase 4a helper for path-dependent rules that need realized prior NAV
        without re-implementing the chain logic.
        """
        view = self._compute_view(max_quarter=quarter)
        nav: dict[str, float] = {b: float(v) for b, v in self._initial_nav.items()}
        if not view.empty:
            last = view.groupby("bucket").tail(1).set_index("bucket")["nav_end_usd"]
            for b in last.index:
                nav[b] = float(last[b])
        return pd.Series(nav, dtype=float, name=str(quarter))

    def _empty_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "quarter": pd.Series([], dtype="period[Q-DEC]"),
                "bucket": pd.Series([], dtype="object"),
                "flow_type": pd.Series([], dtype="object"),
                "amount_usd": pd.Series([], dtype="float64"),
                "nav_start_usd": pd.Series([], dtype="float64"),
                "nav_end_usd": pd.Series([], dtype="float64"),
                "source": pd.Series([], dtype="object"),
                "run_id": pd.Series([], dtype="object"),
            }
        )

    # ---- queries -----------------------------------------------------------

    def end_nav_by_quarter(self) -> pd.DataFrame:
        """End-of-quarter NAV per bucket. Rows = quarter, cols = bucket.

        Buckets that had no rows in a quarter inherit the prior quarter's
        end NAV (NAV unchanged); the first quarter inherits ``initial_nav``.
        """
        df = self.finalize()
        if df.empty:
            return pd.DataFrame()
        last = df.groupby(["quarter", "bucket"], sort=True).tail(1)
        wide = last.pivot(index="quarter", columns="bucket", values="nav_end_usd")
        # Ensure every bucket appears as a column (use union of stub + initial buckets).
        for b in self._initial_nav:
            if b not in wide.columns:
                wide[b] = float("nan")
        wide = wide.sort_index().reindex(sorted(wide.columns), axis=1)
        # Forward-fill missing values per bucket; fill remaining (i.e., before any
        # flow appeared) with initial_nav.
        wide = wide.ffill()
        for b in wide.columns:
            wide[b] = wide[b].fillna(self._initial_nav.get(b, 0.0))
        return wide

    # ---- invariants --------------------------------------------------------

    def validate(
        self,
        *,
        expected_externals_by_quarter: dict[pd.Period, float] | None = None,
        tol: float = 1e-6,
    ) -> None:
        """Raise ``AssertionError`` on any SPEC §5.1 invariant violation."""
        df = self.finalize()

        # No NaN.
        for col in ("amount_usd", "nav_start_usd", "nav_end_usd"):
            if df[col].isna().any():
                raise AssertionError(f"NaN found in {col}")

        # run_id uniqueness — single run object, single id.
        if not df.empty and df["run_id"].nunique() != 1:
            raise AssertionError(
                f"run_id is not unique within ledger: {df['run_id'].unique().tolist()}"
            )

        # Per-row: nav_end == nav_start + amount.
        diffs = (df["nav_end_usd"] - df["nav_start_usd"] - df["amount_usd"]).abs()
        if (diffs > tol).any():
            bad = df.loc[diffs > tol].head(3)
            raise AssertionError(f"per-row consistency failed:\n{bad}")

        # Chain consistency per bucket.
        for bucket, sub in df.groupby("bucket", sort=False):
            sub = sub.reset_index(drop=True)
            first_start = float(sub["nav_start_usd"].iloc[0])
            expected_first = float(self._initial_nav.get(bucket, 0.0))
            if abs(first_start - expected_first) > tol:
                raise AssertionError(
                    f"chain start mismatch for bucket {bucket!r}: "
                    f"first nav_start={first_start}, expected={expected_first}"
                )
            if len(sub) > 1:
                gaps = (
                    sub["nav_start_usd"].iloc[1:].to_numpy()
                    - sub["nav_end_usd"].iloc[:-1].to_numpy()
                )
                if (abs(gaps) > tol).any():
                    raise AssertionError(f"chain consistency failed within bucket {bucket!r}")

        # Per-bucket per-quarter tie-out (redundant given construction; surfaces
        # ordering / phantom-flow bugs).
        for (q, b), sub in df.groupby(["quarter", "bucket"], sort=False):
            sub = sub.reset_index(drop=True)
            amt_sum = float(sub["amount_usd"].sum())
            nav_change = float(sub["nav_end_usd"].iloc[-1] - sub["nav_start_usd"].iloc[0])
            if abs(amt_sum - nav_change) > tol:
                raise AssertionError(
                    f"flow tie-out failed at ({q}, {b}): "
                    f"sum(amount)={amt_sum}, nav_change={nav_change}"
                )

        # Spend uniqueness per (run_id, quarter, source). Phase 4a hardening:
        # path-dependent rules recover prior outflow by filtering spend rows
        # on their own SOURCE_ID; a duplicate row at the same key would
        # silently double-count and corrupt the recovery. run_id is constant
        # within a ledger, but include it in the key so the invariant reads
        # the same in multi-run contexts (comparison reports).
        sp = df[df["flow_type"] == "spend"]
        if not sp.empty:
            dups = sp.groupby(["run_id", "quarter", "source"], sort=False).size()
            dups = dups[dups > 1]
            if not dups.empty:
                first = dups.index[0]
                raise AssertionError(
                    f"duplicate spend row at (run_id, quarter, source)={first}: "
                    f"{int(dups.iloc[0])} rows"
                )

        # Rebalance zero-sum per quarter.
        rb = df[df["flow_type"] == "rebalance"]
        if not rb.empty:
            for q, sub in rb.groupby("quarter", sort=False):
                s = float(sub["amount_usd"].sum())
                if abs(s) > tol:
                    raise AssertionError(f"rebalance not zero-sum at {q}: sum={s}")

        # pe_call and pe_distribution must also be zero-sum across buckets per
        # quarter (capital moves between cash and pe sleeves; total preserved).
        for ftype in ("pe_call", "pe_distribution"):
            sub_all = df[df["flow_type"] == ftype]
            if sub_all.empty:
                continue
            for q, sub in sub_all.groupby("quarter", sort=False):
                s = float(sub["amount_usd"].sum())
                if abs(s) > tol:
                    raise AssertionError(f"{ftype} not zero-sum at {q}: sum={s}")

        # Total NAV conservation per quarter.
        end_by_q = self.end_nav_by_quarter()
        if not end_by_q.empty:
            quarters = list(end_by_q.index)
            initial_total = sum(self._initial_nav.values())
            for i, q in enumerate(quarters):
                end_now = float(end_by_q.loc[q].sum())
                end_prev = float(end_by_q.loc[quarters[i - 1]].sum()) if i > 0 else initial_total
                actual_delta = end_now - end_prev
                contrib = df[
                    (df["quarter"] == q)
                    & (
                        df["flow_type"].isin(
                            [
                                "return",
                                "pe_nav_mark",
                                "inflow",
                                "spend",
                                "transaction_cost",
                            ]
                        )
                    )
                ]
                expected_delta = float(contrib["amount_usd"].sum())
                if abs(actual_delta - expected_delta) > tol:
                    raise AssertionError(
                        f"total NAV conservation failed at {q}: "
                        f"actual_delta={actual_delta}, expected_delta={expected_delta}"
                    )

        # External cash flow tie-out (optional; orchestrator passes the ground
        # truth it generated). transaction_cost is included since it leaves
        # the household (paid to market-makers / brokers).
        if expected_externals_by_quarter is not None:
            ext = df[df["flow_type"].isin(["inflow", "spend", "transaction_cost"])]
            sums = ext.groupby("quarter")["amount_usd"].sum().to_dict()
            for q, expected in expected_externals_by_quarter.items():
                actual = float(sums.get(q, 0.0))
                if abs(actual - expected) > tol:
                    raise AssertionError(
                        f"external cash flow tie-out failed at {q}: "
                        f"actual={actual}, expected={expected}"
                    )
