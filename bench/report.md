# Accuracy benchmark: model x preprocessing

Test set: 10 slips from `example/`, scored against `example/label.json` (every field normalized before comparison)

| # | model | variant | mean core | whole slip | name | name≈ | phone | date | plate | plate≈ | brand | type | spot | name CER | $/1000 slips | p50 | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `google/gemini-3-flash-preview` | v1_crop | **78%** | 0% | 20% | 90% | 100% | 90% | 60% | 100% | 100% | 100% | 90% | 0.15 | $1.55 | 2.7s | 0 |
| 2 | `google/gemini-3.1-pro-preview` | v1_crop | **77%** | 10% | 20% | 70% | 100% | 80% | 60% | 100% | 100% | 100% | 90% | 0.16 | $7.05 | 4.7s | 0 |
| 3 | `google/gemini-3-flash-preview` | v2_enhanced | **75%** | 10% | 20% | 80% | 80% | 80% | 70% | 100% | 100% | 100% | 90% | 0.15 | $1.55 | 2.7s | 0 |
| 4 | `google/gemini-3.6-flash` | v1_crop | **75%** | 0% | 20% | 50% | 90% | 70% | 70% | 100% | 100% | 100% | 80% | 0.21 | $2.59 | 4.3s | 0 |
| 5 | `google/gemini-3.5-flash` | v1_crop | **75%** | 0% | 20% | 90% | 80% | 80% | 70% | 100% | 100% | 100% | 90% | 0.16 | $6.52 | 3.7s | 0 |
| 6 | `google/gemini-3.1-pro-preview` | v0_raw | **75%** | 0% | 20% | 80% | 100% | 80% | 50% | 100% | 100% | 100% | 90% | 0.15 | $7.02 | 8.1s | 0 |
| 7 | `google/gemini-3.1-pro-preview` | v2_enhanced | **75%** | 0% | 20% | 70% | 100% | 80% | 50% | 100% | 100% | 100% | 90% | 0.15 | $7.03 | 4.3s | 0 |
| 8 | `google/gemini-3.1-flash-lite` | v0_raw | **73%** | 0% | 10% | 80% | 90% | 80% | 60% | 100% | 100% | 100% | 90% | 0.16 | $1.02 | 8.4s | 0 |
| 9 | `google/gemini-3.8-flash` | v1_crop | **73%** | 0% | 20% | 90% | 100% | 70% | 50% | 100% | 100% | 100% | 80% | 0.15 | $2.23 | 4.9s | 0 |
| 10 | `google/gemini-3.7-flash` | v2_enhanced | **73%** | 0% | 20% | 80% | 90% | 70% | 60% | 100% | 100% | 100% | 90% | 0.17 | $2.45 | 4.7s | 0 |
| 11 | `google/gemini-3.6-flash` | v2_enhanced | **73%** | 10% | 30% | 50% | 80% | 70% | 60% | 100% | 100% | 100% | 90% | 0.21 | $2.48 | 3.9s | 0 |
| 12 | `google/gemini-3.7-flash` | v1_crop | **73%** | 0% | 20% | 70% | 90% | 70% | 60% | 100% | 100% | 100% | 90% | 0.16 | $2.75 | 5.2s | 0 |
| 13 | `google/gemini-3.5-flash` | v0_raw | **73%** | 0% | 20% | 80% | 80% | 70% | 70% | 90% | 100% | 100% | 90% | 0.16 | $5.28 | 6.7s | 0 |
| 14 | `google/gemini-3.5-flash` | v2_enhanced | **73%** | 0% | 20% | 80% | 80% | 70% | 70% | 90% | 100% | 100% | 90% | 0.17 | $5.98 | 3.1s | 0 |
| 15 | `google/gemini-3.1-flash-lite` | v1_crop | **72%** | 0% | 20% | 80% | 90% | 80% | 50% | 100% | 90% | 100% | 90% | 0.17 | $1.03 | 3.1s | 0 |
| 16 | `google/gemini-3.6-flash` | v0_raw | **72%** | 0% | 20% | 70% | 90% | 70% | 50% | 80% | 100% | 100% | 80% | 0.18 | $2.85 | 4.8s | 0 |
| 17 | `google/gemini-2.5-flash` | v1_crop | **72%** | 0% | 20% | 30% | 90% | 70% | 50% | 100% | 100% | 100% | 80% | 0.33 | $3.02 | 7.4s | 0 |
| 18 | `google/gemini-2.5-flash` | v0_raw | **70%** | 10% | 20% | 50% | 90% | 80% | 50% | 90% | 90% | 90% | 80% | 0.30 | $2.74 | 10.0s | 0 |
| 19 | `qwen/qwen3-vl-235b-a22b-instruct` | v1_crop | **70%** | 0% | 10% | 10% | 100% | 90% | 30% | 70% | 90% | 100% | 70% | 0.52 | $0.69 | 10.5s | 0 |
| 20 | `google/gemini-3-flash-preview` | v0_raw | **70%** | 0% | 30% | 80% | 80% | 50% | 60% | 90% | 100% | 100% | 90% | 0.17 | $1.54 | 8.0s | 0 |
| 21 | `google/gemini-2.5-pro` | v0_raw | **70%** | 0% | 40% | 60% | 90% | 80% | 50% | 90% | 100% | 60% | 80% | 0.15 | $11.55 | 12.4s | 0 |
| 22 | `google/gemini-3.1-flash-lite` | v2_enhanced | **68%** | 0% | 20% | 70% | 80% | 80% | 40% | 100% | 90% | 100% | 90% | 0.17 | $1.02 | 3.2s | 0 |
| 23 | `google/gemini-3.8-flash` | v2_enhanced | **68%** | 0% | 0% | 70% | 90% | 60% | 60% | 100% | 100% | 100% | 90% | 0.19 | $4.15 | 24.5s | 0 |
| 24 | `qwen/qwen3-vl-235b-a22b-instruct` | v2_enhanced | **67%** | 0% | 10% | 20% | 90% | 90% | 30% | 80% | 80% | 100% | 70% | 0.48 | $0.61 | 10.2s | 0 |
| 25 | `google/gemini-3.8-flash` | v0_raw | **67%** | 0% | 10% | 70% | 90% | 60% | 40% | 90% | 100% | 100% | 80% | 0.20 | $2.25 | 87.8s | 0 |
| 26 | `google/gemini-3.7-flash` | v0_raw | **67%** | 0% | 10% | 70% | 90% | 50% | 50% | 100% | 100% | 100% | 80% | 0.20 | $2.48 | 5.9s | 0 |
| 27 | `google/gemini-2.5-flash` | v2_enhanced | **65%** | 0% | 10% | 30% | 80% | 80% | 40% | 90% | 80% | 100% | 70% | 0.27 | $2.83 | 6.4s | 0 |
| 28 | `google/gemini-2.5-pro` | v2_enhanced | **65%** | 0% | 10% | 50% | 90% | 80% | 40% | 80% | 100% | 70% | 80% | 0.27 | $11.57 | 9.6s | 0 |
| 29 | `qwen/qwen3-vl-235b-a22b-instruct` | v0_raw | **63%** | 0% | 10% | 20% | 90% | 80% | 30% | 70% | 80% | 90% | 70% | 0.44 | $1.12 | 10.6s | 0 |
| 30 | `google/gemini-2.5-pro` | v1_crop | **63%** | 0% | 10% | 50% | 90% | 70% | 50% | 90% | 100% | 60% | 80% | 0.24 | $12.10 | 9.4s | 0 |
| 31 | `anthropic/claude-opus-5.5` | v0_raw | **58%** | 10% | 10% | 60% | 60% | 70% | 30% | 80% | 100% | 80% | 70% | 0.37 | $41.86 | 30.8s | 0 |
| 32 | `anthropic/claude-opus-5.5` | v1_crop | **55%** | 10% | 10% | 70% | 50% | 60% | 40% | 70% | 100% | 70% | 70% | 0.39 | $31.13 | 18.1s | 0 |
| 33 | `anthropic/claude-opus-5.5` | v2_enhanced | **50%** | 0% | 0% | 50% | 50% | 50% | 30% | 70% | 100% | 70% | 60% | 0.44 | $30.22 | 20.9s | 0 |

## Does preprocessing help? (averaged over all models)

| variant | mean core | whole slip |
|---|---|---|
| v0_raw | 69% | 2% |
| v1_crop | 71% | 2% |
| v2_enhanced | 68% | 2% |

## What to use

- **Most accurate**: `google/gemini-3-flash-preview` + `v1_crop` — 78% mean, $1.55 per 1000 slips, 2.7s per slip
- **Best value (accuracy per cost)**: `qwen/qwen3-vl-235b-a22b-instruct` + `v2_enhanced` — 67% mean, $0.61 per 1000 slips
- **Best preprocessing**: `v1_crop`

Note: the `name≈` and `plate≈` columns count reads that are wrong by no more than ~1-2 characters, which fuzzy search still finds and a reviewer corrects easily. They reflect real-world usability better than exact match does.
