# Comparing crop strategies: locating the slip before reading it

Test set of 49 slips — 36 shot close up, 13 shot from a distance
Identical model throughout: `google/gemini-3-flash-preview`. Only the image sent differs.

The primary metric is **CER** (the share of characters read wrongly; lower is better), because
exact match is far too coarse for Thai handwriting — most reads are wrong by only 1-2
characters, which fuzzy search still finds and a reviewer corrects quickly.

| strategy | core CER | near | exact | all 3 fields | name≈ | phone | plate≈ | $/1000 slips | p50 | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| full frame, no crop | **0.161** | 77% | 53% | 14% | 49% | 78% | 84% | $2.16 | 3.7s | - |
| OpenCV crop | **0.124** | 82% | 59% | 18% | 61% | 86% | 88% | $1.86 | 3.4s | quad not found on 1 slip |
| OpenCV crop + deskew | **0.131** | 82% | 59% | 18% | 65% | 86% | 84% | $1.84 | 3.4s | quad not found on 1 slip |
| OpenCV, falling back to full frame | **0.124** | 82% | 59% | 18% | 61% | 86% | 88% | $1.86 | 3.4s | quad not found on 1 slip |
| LLM finds the quad, then crop | **0.184** | 73% | 57% | 24% | 57% | 80% | 73% | $2.72 | 7.1s | quad not found on 1 slip |

## Slips shot close up only (36 slips)

| strategy | core CER | near | exact | all 3 fields |
|---|---|---|---|---|
| full frame, no crop | **0.125** | 82% | 56% | 19% |
| OpenCV crop | **0.122** | 82% | 58% | 19% |
| OpenCV crop + deskew | **0.120** | 83% | 58% | 22% |
| OpenCV, falling back to full frame | **0.122** | 82% | 58% | 19% |
| LLM finds the quad, then crop | **0.131** | 80% | 61% | 28% |

## Slips shot from a distance only (13 slips)

| strategy | core CER | near | exact | all 3 fields |
|---|---|---|---|---|
| full frame, no crop | **0.260** | 62% | 44% | 0% |
| OpenCV crop | **0.132** | 79% | 62% | 15% |
| OpenCV crop + deskew | **0.161** | 77% | 59% | 8% |
| OpenCV, falling back to full frame | **0.132** | 79% | 62% | 15% |
| LLM finds the quad, then crop | **0.332** | 54% | 46% | 15% |

## Conclusions

1. **Cropping before reading pays off** — CER 0.161 -> 0.124, and it is cheaper, because a
   smaller image means fewer tokens. The effect is starkest on slips shot from a distance: CER
   0.260 -> 0.132 (halved), and "all 3 core fields correct" goes from 0% to 15%. Uncropped,
   distant shots are essentially unusable.

2. **Having an LLM find the quad instead of OpenCV is worse** — CER 0.184, worse than both
   OpenCV and the full frame. It costs 46% more and takes twice as long (two API calls). The LLM
   gets the corner coordinates roughly right but not to pixel accuracy, and once those are used
   for a warp, an error of a few percent becomes a skewed image with whole lines sheared away.
   The further the shot, the worse it gets (CER 0.332).

3. **Deskewing after the crop does not help** — close-up shots improve marginally, within the
   noise, while distant shots get clearly worse (0.132 -> 0.161), because rotating means
   interpolating afresh and text that was already small blurs further. The model itself
   tolerates this much skew anyway.

## Caveats when reading these figures

- Of the 49 slips, only 13 were shot from a distance, so figures for that group can swing widely
- The ground truth comes from values staff confirmed or corrected in the live system (39 of 49
  slips were actually corrected). A slip left uncorrected may mean the reviewer agreed with what
  the model read, or simply that they clicked through
- Every strategy uses the same model and differs only in the image sent, so they compare directly
