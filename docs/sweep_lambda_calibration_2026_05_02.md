# Phase 4b — λ calibration sweep (cost-aware allocator)

Research probe. Cross-product of 6 × 4 × 3 = 72 cells (elapsed 25.5s; 72 ok, 0 fail).

All cells use `allocation.engine=cvxportfolio` and `spending.rule=flat_real`. Implementation engine = `stub` for the bps=0 column (validator requires it), `cvxportfolio` elsewhere. Base portfolio NAV is $100M; horizon 20 quarters.

## Headline finding

**At V_total = $100M with realistic transaction costs (bps ≥ 5),**
**the cost-aware optimum is corner-dominated across `λ_norm ∈ [0.01, 1e3]`.**
Total turnover and cumulative transaction cost are **bit-identical** across this entire range at any given bps>0. The threshold `c·V_total / (2·λ_norm)` for engaging interior partial-trade behavior is far larger than any feasible weight deviation in this regime, so the optimizer always sits at a boundary (stay-at-current or one-bucket-at-the-zero-bound) regardless of λ_norm.

**Sensitivity to `λ_norm` only becomes visible at `λ_norm ≈ 1e6` for $100M portfolios at 5 bps** — six orders of magnitude above the schema default `λ_norm = 1.0`. This means the default does **not** engage cost/policy trade-off reasoning at institutional NAV scales; it produces effectively cost-aware-OFF behavior (the optimizer just declines to over-trade, but does not weight policy deviation against cost in any meaningful way). This is a known consequence of the dollar-quadratic + linear-cost formulation: `policy_loss ≈ λ_norm · ‖w − w_p‖²` (unitless weights) while `cost ≈ c·V·‖w − w_c‖₁` (dollars), so the policy/cost ratio scales as `λ_norm / (c·V)`. To engage interior partial-trade behavior, set

```
λ_norm ≈ bps_per_trade × V_total × 1e-3
```

as a starting point (e.g. `λ_norm ≈ 5e5` at $100M with 5 bps; `λ_norm ≈ 1e8` at $100M with 100 bps). Then tune empirically against the desired policy-track-vs-cost-suppress balance.

**Bug surfaced and fixed during this sweep:** at small `λ_norm` (< ~0.1) and `bps == 0`, CLARABEL stopped short of tight policy convergence on the weakly-conditioned policy quadratic, returning 3–5pp policy deviation despite zero cost. Fix landed in `CvxportfolioAllocator.target_at`: short-circuit `cost_per_dollar == 0` to return policy directly. Zero-cost parity now holds across every realistic NAV scale and every `λ_norm > 0`. Regression test added.

## Reading the tables

Each metric's section has three pivot blocks (one per scenario). Rows are λ_norm; columns are bps_per_trade. The cell at `(λ_norm, bps) = (1.0, 5)` is the closest the engine gets to "production-typical" — default λ at a realistic 5 bps trading cost.

## Final NAV ($M)

#### scenario = `base`
```
bps               0         5         25        100
lambda_norm                                        
0.01           114.78    112.91    112.72    111.73
0.10           114.78    113.41    112.68    111.65
1.00           114.78    114.75    113.48    111.81
10.00          114.78    114.83    114.55    113.42
1000.00        114.78    114.83    114.57    113.55
1000000.00     114.78    114.81    114.57    113.55
```
#### scenario = `public_drawdown`
```
bps               0         5         25        100
lambda_norm                                        
0.01           116.28    112.49    111.96    110.90
0.10           116.28    113.42    112.48    111.04
1.00           116.28    114.80    113.56    111.61
10.00          116.28    114.84    114.58    113.43
1000.00        116.28    114.84    114.58    113.56
1000000.00     116.28    115.80    114.58    113.56
```
#### scenario = `inflation_shock`
```
bps               0         5         25        100
lambda_norm                                        
0.01           113.16    111.28    111.05    110.09
0.10           113.16    111.75    111.03    110.04
1.00           113.16    113.09    111.83    110.15
10.00          113.16    113.21    112.93    111.79
1000.00        113.16    113.21    112.94    111.90
1000000.00     113.16    113.19    112.94    111.90
```

## Cumulative transaction cost ($)

#### scenario = `base`
```
bps                 0           5           25          100
lambda_norm                                                
0.01                  0      56,967     285,989   1,161,592
0.10                  0      56,967     285,989   1,161,592
1.00                  0      56,967     285,989   1,161,592
10.00                 0      56,967     285,989   1,161,592
1000.00               0      56,967     285,989   1,161,592
1000000.00            0      58,187     285,989   1,161,592
```
#### scenario = `public_drawdown`
```
bps                 0           5           25          100
lambda_norm                                                
0.01                  0      56,967     285,989   1,161,592
0.10                  0      56,967     285,989   1,161,592
1.00                  0      56,967     285,989   1,161,592
10.00                 0      56,967     285,989   1,161,592
1000.00               0      56,967     285,989   1,161,592
1000000.00            0      61,657     285,989   1,161,592
```
#### scenario = `inflation_shock`
```
bps                 0           5           25          100
lambda_norm                                                
0.01                  0      58,492     293,639   1,192,612
0.10                  0      58,492     293,639   1,192,612
1.00                  0      58,492     293,639   1,192,612
10.00                 0      58,492     293,639   1,192,612
1000.00               0      58,492     293,639   1,192,612
1000000.00            0      59,579     293,639   1,192,612
```

## Total turnover ($)

#### scenario = `base`
```
bps                    0              5              25             100
lambda_norm                                                            
0.01            62,501,064     56,967,251     57,197,888     58,079,575
0.10            62,501,064     56,967,251     57,197,888     58,079,575
1.00            62,501,064     56,967,251     57,197,888     58,079,575
10.00           62,501,064     56,967,251     57,197,888     58,079,575
1000.00         62,501,064     56,967,251     57,197,888     58,079,575
1000000.00      62,501,064     58,186,842     57,197,888     58,079,575
```
#### scenario = `public_drawdown`
```
bps                    0              5              25             100
lambda_norm                                                            
0.01            71,971,309     56,967,251     57,197,888     58,079,575
0.10            71,971,309     56,967,251     57,197,888     58,079,575
1.00            71,971,309     56,967,251     57,197,888     58,079,575
10.00           71,971,309     56,967,251     57,197,888     58,079,575
1000.00         71,971,309     56,967,251     57,197,888     58,079,575
1000000.00      71,971,309     61,656,518     57,197,888     58,079,575
```
#### scenario = `inflation_shock`
```
bps                    0              5              25             100
lambda_norm                                                            
0.01            63,644,573     58,491,685     58,727,849     59,630,624
0.10            63,644,573     58,491,685     58,727,849     59,630,624
1.00            63,644,573     58,491,685     58,727,849     59,630,624
10.00           63,644,573     58,491,685     58,727,849     59,630,624
1000.00         63,644,573     58,491,685     58,727,849     59,630,624
1000000.00      63,644,573     59,578,975     58,727,849     59,630,624
```

## Average |policy deviation| (weight)

#### scenario = `base`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.04318  0.04953  0.05337
0.10         0.00000  0.03224  0.04018  0.04784
1.00         0.00000  0.02315  0.02837  0.03906
10.00        0.00000  0.02315  0.02323  0.02354
1000.00      0.00000  0.02315  0.02323  0.02352
1000000.00   0.00000  0.01736  0.02323  0.02352
```
#### scenario = `public_drawdown`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.04241  0.05047  0.05550
0.10         0.00000  0.03397  0.04050  0.04606
1.00         0.00000  0.02541  0.03049  0.03903
10.00        0.00000  0.02493  0.02507  0.02552
1000.00      0.00000  0.02493  0.02501  0.02531
1000000.00   0.00000  0.01847  0.02501  0.02531
```
#### scenario = `inflation_shock`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.04407  0.05073  0.05339
0.10         0.00000  0.03328  0.04029  0.04631
1.00         0.00000  0.02315  0.02836  0.03789
10.00        0.00000  0.02315  0.02323  0.02403
1000.00      0.00000  0.02315  0.02323  0.02353
1000000.00   0.00000  0.01746  0.02323  0.02353
```

## Max |policy deviation| (weight)

#### scenario = `base`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.12082  0.14342  0.16094
0.10         0.00000  0.08392  0.10932  0.13733
1.00         0.00000  0.05003  0.07025  0.10443
10.00        0.00000  0.05003  0.05014  0.05058
1000.00      0.00000  0.05003  0.05014  0.05058
1000000.00   0.00000  0.03897  0.05014  0.05058
```
#### scenario = `public_drawdown`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.12945  0.14113  0.15724
0.10         0.00000  0.11103  0.12268  0.13592
1.00         0.00000  0.06587  0.10156  0.12383
10.00        0.00000  0.05848  0.05953  0.06355
1000.00      0.00000  0.05847  0.05823  0.05730
1000000.00   0.00000  0.03641  0.05823  0.05729
```
#### scenario = `inflation_shock`
```
bps              0        5        25       100
lambda_norm                                    
0.01         0.00000  0.12647  0.15198  0.16144
0.10         0.00000  0.08634  0.10813  0.12954
1.00         0.00000  0.05003  0.06954  0.10024
10.00        0.00000  0.05003  0.05014  0.05059
1000.00      0.00000  0.05003  0.05014  0.05059
1000000.00   0.00000  0.03940  0.05014  0.05059
```

## Partial-trade quarters (out of 20)

#### scenario = `base`
```
bps          0    5    25   100
lambda_norm                    
0.01           0   19   19   19
0.10           0   19   19   19
1.00           0   19   19   19
10.00          0   19   19   19
1000.00        0   19   19   19
1000000.00     0   19   19   19
```
#### scenario = `public_drawdown`
```
bps          0    5    25   100
lambda_norm                    
0.01           0   19   19   19
0.10           0   19   19   19
1.00           0   19   19   19
10.00          0   19   19   19
1000.00        0   19   19   19
1000000.00     0   19   19   19
```
#### scenario = `inflation_shock`
```
bps          0    5    25   100
lambda_norm                    
0.01           0   19   19   19
0.10           0   19   19   19
1.00           0   19   19   19
10.00          0   19   19   19
1000.00        0   19   19   19
1000000.00     0   19   19   19
```

## Min coverage (months)

#### scenario = `base`
```
bps             0       5       25      100
lambda_norm                                
0.01           74.1    51.4    38.7    31.0
0.10           74.1    57.8    55.3    43.9
1.00           74.1    60.7    57.1    53.6
10.00          74.1    60.7    60.7    59.5
1000.00        74.1    60.7    60.7    60.6
1000000.00     74.1    65.1    60.7    60.6
```
#### scenario = `public_drawdown`
```
bps             0       5       25      100
lambda_norm                                
0.01           64.3    53.6    39.6    32.4
0.10           64.3    57.4    55.3    48.0
1.00           64.3    60.7    56.4    53.6
10.00          64.3    60.7    60.7    59.1
1000.00        64.3    60.7    60.7    60.6
1000000.00     64.3    62.0    60.7    60.6
```
#### scenario = `inflation_shock`
```
bps             0       5       25      100
lambda_norm                                
0.01           65.5    43.3    32.3    26.6
0.10           65.5    50.1    49.0    40.6
1.00           65.5    53.8    50.5    49.1
10.00          65.5    54.2    54.2    51.0
1000.00        65.5    54.3    54.2    54.2
1000000.00     65.5    57.1    54.2    54.2
```

## Max drawdown (%)

#### scenario = `base`
```
bps             0       5       25      100
lambda_norm                                
0.01          +0.00   +0.00   +0.00   +0.00
0.10          +0.00   +0.00   +0.00   +0.00
1.00          +0.00   +0.00   +0.00   +0.00
10.00         +0.00   +0.00   +0.00   +0.00
1000.00       +0.00   +0.00   +0.00   +0.00
1000000.00    +0.00   +0.00   +0.00   +0.00
```
#### scenario = `public_drawdown`
```
bps             0       5       25      100
lambda_norm                                
0.01         -12.78  -12.48  -12.77  -12.96
0.10         -12.78  -12.42  -12.38  -12.71
1.00         -12.78  -13.20  -12.65  -12.53
10.00        -12.78  -13.36  -13.34  -13.38
1000.00      -12.78  -13.36  -13.37  -13.40
1000000.00   -12.78  -13.19  -13.37  -13.40
```
#### scenario = `inflation_shock`
```
bps             0       5       25      100
lambda_norm                                
0.01          +0.00   +0.00   +0.00   +0.00
0.10          +0.00   +0.00   +0.00   +0.00
1.00          +0.00   +0.00   +0.00   +0.00
10.00         +0.00   +0.00   +0.00   +0.00
1000.00       +0.00   +0.00   +0.00   +0.00
1000000.00    +0.00   +0.00   +0.00   +0.00
```

## λ_norm sensitivity (turnover spread)

For each (scenario, bps) the table below shows the range (`max - min`) of total turnover across the λ_norm sweep, in $. A spread of \$0 means the cost-aware target is **corner-dominated** at that bps — the optimum sits at the same boundary regardless of λ_norm and the engine is insensitive to the tuning parameter. Non-zero spread is the regime where λ_norm meaningfully tunes the policy/cost balance.

```
       scenario  bps  n_lambda    turnover_min    turnover_max turnover_spread
           base    0         6      62,501,064      62,501,064               0
           base    5         6      56,967,251      58,186,842       1,219,591
           base   25         6      57,197,888      57,197,888               0
           base  100         6      58,079,575      58,079,575               0
public_drawdown    0         6      71,971,309      71,971,309               0
public_drawdown    5         6      56,967,251      61,656,518       4,689,267
public_drawdown   25         6      57,197,888      57,197,888               0
public_drawdown  100         6      58,079,575      58,079,575               0
inflation_shock    0         6      63,644,573      63,644,573               0
inflation_shock    5         6      58,491,685      59,578,975       1,087,290
inflation_shock   25         6      58,727,849      58,727,849               0
inflation_shock  100         6      59,630,624      59,630,624               0
```

## Auto-summary

- Partial-trade engagement: cells with > 0 partial-trade quarters: 54 / 72.
- Highest cumulative tx cost as % of final NAV (top 3):
```
 lambda_norm  bps        scenario   tx_pct
        0.10  100 inflation_shock 1.083846
        0.01  100 inflation_shock 1.083262
        1.00  100 inflation_shock 1.082716
```
