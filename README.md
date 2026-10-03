> **English** · [ภาษาไทย](README.th.md)

# Courtesy Parking OCR — a flood-response system

A handwriting-OCR and human-review system built during the 2026 Thailand floods, so that a
temple offering free parking to flood victims could find any car's owner again — across 8,401
hand-written paper slips, with a volunteer team far too small to type them all in.

It went from empty repository to running in production in a little over two hours, and the
most useful thing it eventually did was make its own OCR pipeline unnecessary.

---

## Why this exists

During the 2026 floods, a temple opened its grounds as free parking for people whose homes were
under water. Cars arrived faster than anyone had planned for, and I was there among the people
parking one.

The problem the volunteers were up against was simple to state and hard to solve. Every car that
came in got a hand-written paper slip — owner's name, phone number, licence plate, vehicle type,
and where it was parked. On the way out, someone had to find that slip again.

The slips were piling into the thousands. The plan was to key them into a spreadsheet by hand,
which needed volunteers who could type Thai quickly and accurately, and there were nowhere near
enough of them. Meanwhile every hour the data was not searchable was an hour in which an owner
arriving to collect their car might simply not be findable.

That is the gap this was built to close.

## Timeline

Commit timestamps, not reconstruction:

| When | What |
|---|---|
| **Day 1, 15:55** | Empty repository |
| **Day 1, 18:07** | Working end to end: preprocessing, LLM OCR, review queue, fuzzy search, roles, deploy config |
| **Day 1, 18:24** | Deployed, and already being fixed from real tablet and phone testing |
| **Day 1, evening** | iOS Safari `datalist` doesn't exist; cropping fails on white tables; mobile nav collapses — all found by people actually using it |
| **Day 2** | Benchmarked 11 vision models × 3 preprocessing variants on real handwriting; recruited photographers and a back-office labelling team; added per-person attribution |
| **Days 3-4** | The problems of a system in real use: duplicate uploads, two reviewers overwriting each other, a date parser that turned 2026 into 2083 |
| **Days 5-6** | Self-service entry and exit forms — the path that needs no OCR at all |

The first two hours produced something usable. Everything after that was the actual work:
finding out how it broke in the hands of tired volunteers on their own phones, in a car park,
in the rain.

## Scale and outcome

Figures from the live production dashboard:

| | |
|---|---|
| Slips in the system | **8,401** |
| Approved and searchable | 6,513 |
| Cars already collected and handed back | 1,110 |
| Busiest single day | **3,290 slips** |
| Rejected as unusable | 29 |
| **Total AI spend, entire deployment** | **฿335** (about US$10) |

At a typical Thai used-car value of around ฿500,000, those slips stand for roughly **฿4 billion**
of private property — for most of these families, the largest thing they still owned after the
home they had just lost.

The software did not save those cars from the water; the temple's land did. What it was
responsible for was narrower and still load-bearing: being the only index that could connect a
car back to its owner. On the busiest day 3,290 slips arrived — a volume no realistic number of
volunteers could have typed into a spreadsheet that day, which is exactly the gap it was built
to close.

The whole thing ran on ฿335 of inference.

## What it does

```
Photograph the slip → crop the paper → LLM reads Thai handwriting → human confirms
      → Postgres + the photo as evidence → fuzzy search when the owner returns
```

Owners come back and remember their own details imperfectly — a transposed digit, a different
spelling of their name. So retrieval is fuzzy by design: trigram and Levenshtein candidate
selection in Postgres, reranked in Python, searchable by name, phone or plate.

| Route | Purpose |
|---|---|
| `/in` | Self-service entry form, filled in by the driver (public) |
| `/out` | Self-service collection request, filled in by the driver (public) |
| `/` | Upload or photograph slips in bulk (HEIC from iPhone supported) |
| `/review` | The review queue: needs-review / fast-pass / approved / rejected, with search, sort and pagination |
| `/search` | Fuzzy search by name, phone or plate |
| `/table` | Full data table with filters and an Excel export matching exactly what is on screen |
| `/dashboard` | KPIs, slips per day, breakdowns by type, brand, location and volunteer, and OCR quality |
| `/dups` | Side-by-side comparison of duplicates that were both already approved |
| `/slips/{id}` | One slip, its photo, its full edit history, and the button that releases the car |

## Choosing the model by measuring, not by vibes

Thai handwriting OCR is not a solved problem, and the slips were filled in with faint
ballpoint by people who had just lost their homes. Rather than guess, I built a benchmark:
11 vision models × 3 preprocessing variants, scored against human labels, with every field
normalized first so the metric measured *did it read correctly* rather than *does it match
character for character*.

| Model | Variant | Mean core | Phone | Name≈ | $/1000 slips | p50 |
|---|---|---|---|---|---|---|
| **google/gemini-3-flash-preview** | **v1_crop** | **78%** | 100% | 90% | **$1.55** | 2.7s |
| google/gemini-3.1-pro-preview | v1_crop | 77% | 100% | 70% | $7.05 | 4.7s |
| qwen/qwen3-vl-235b-a22b-instruct | v1_crop | 70% | 100% | 10% | $0.69 | 10.5s |
| anthropic/claude-opus-5.5 | v0_raw | 58% | 60% | 60% | $41.86 | 30.8s |

Gemini 3 Flash won on accuracy *and* was among the cheapest. Full results in
[bench/report.md](bench/report.md).

Three findings that changed the implementation:

1. **Cropping the paper before reading helps; "enhancing" it hurts.** Crop lifted the mean
   from 69% to 71%, while shadow removal and contrast enhancement dropped it to 68% — lifting
   contrast distorts thin pen strokes. A second benchmark
   ([bench/crop_report.md](bench/crop_report.md)) showed the effect is starkest on photos taken
   from a distance: character error rate halved, and "all three core fields correct" went from
   0% to 15%. Uncropped, a distant shot is essentially unusable.

2. **Letting an LLM find the paper's corners is worse than OpenCV.** It costs 46% more, takes
   twice as long, and scores worse than not cropping at all. The LLM gets corners roughly
   right but not to pixel accuracy, and once those feed a perspective warp, an error of a few
   percent shears whole lines away.

3. **Exact match is the wrong metric.** The field that failed most was the name — but usually
   by one or two characters, which fuzzy search still finds and a reviewer corrects in seconds.
   Measuring character error rate instead of exact match is what made the comparison
   meaningful.

**Cost in production: about 0.05 THB per slip.** A thousand slips for roughly 51 THB.

## The part I'd want to be judged on

### OCR never writes to the real data

Thai handwriting OCR will never be 100% accurate, so no slip is ever trusted. Every one lands
in a `pending` queue and is automatically flagged when the model reports low confidence, a
required field is unreadable, a value is malformed, or it looks like a duplicate. A human
confirms before anything becomes searchable. The dashboard then tracks which fields humans
correct most often, from the real audit log — so the system reports its own error rate rather
than asserting an accuracy figure.

### Three reviewers, one queue

Once a back-office team was labelling in parallel, the queue handed all of them the same
head-of-line slip, and they spent their time redoing each other's work. The fix is two layers,
because they guard different things:

- **Claiming** (`FOR UPDATE SKIP LOCKED` plus a lease) makes collisions rare. It is advisory,
  not a lock — people claim a slip and close the tab constantly, and a hard lock would leave
  the queue full of untouchable slips.
- **An optimistic write guard** (`require_status` inside the `UPDATE`) makes a collision
  harmless. This is the layer that actually guarantees nobody's work is silently overwritten.

The same reasoning applies at the checkout point, where several staff work from separate
screens: the "car collected" write carries its precondition *inside* the `UPDATE`, so pressing
the same slip twice cannot rewrite who released the car.

### Duplicates, and why guessing wrong is asymmetric

The upload page didn't clear its file input after a successful upload, so anyone who thought it
hadn't gone through pressed again. That produced **650 surplus slips across 548 groups** — up
to 9 copies of one slip — and reviewers kept being handed work a colleague had already done.

Closing the hole was easy. Deciding what counts as a duplicate was not, because the two ways of
being wrong do not cost the same:

- Guess "not a duplicate" wrongly → a reviewer loses the time to process one extra slip.
- Guess "duplicate" wrongly → a car is genuinely parked with **no active slip**, which surfaces
  only when the owner arrives to collect and cannot be found.

So the rule is deliberately biased toward leaving things in the queue, and distinguishes a
*re-photographed slip* from a *genuinely new parking round* on the parking bay — because a new
round always gets a new bay, while two photos of one paper slip necessarily carry the same one.
Where both copies were already approved, the system refuses to decide at all and instead lays
the evidence out for a human: one photo, every copy's values side by side, conflicting fields
highlighted, resolved in a click. On launch, **56 of 273 such groups had nothing to decide**
and were cleared in bulk; the rest needed eyes on the photo.

### The bug that split one car into two deposits

A two-digit year rule — add 2600 if under 50, treat as Buddhist Era, subtract 543 — turned the
"26" people wrote for 2026 into **2083**, on more than 300 slips. The visible symptom was that
re-photographing one slip counted as a separate parking round, because the dates disagreed.

The fix resolves a year by picking the reading *closest to today*, since a parking slip is
always dated the day it was written. And where that date cannot exist at all (29 February in a
non-leap year), it returns nothing rather than walking to the next candidate: a reviewer can
fix an empty date, but nobody will ever catch a slip quietly showing the year 2068.

Still outstanding, and visible on the dashboard: the parser is fixed, but the rows written
before the fix were never backfilled from `raw_ocr`. 1,333 slips still carry an impossible year,
which is most of the 2,009 currently falling outside the 30-day chart. The dashboard reports
that number rather than hiding it, which is the point of counting what falls off the edges —
but it is a backfill still owed, not a solved problem.

### Then we deleted the need for OCR

Once the initial surge settled, the obvious question was why the slips were being transcribed
at all. The person who knows the data is the person standing next to the car.

So `/in` lets the driver fill the form in on their own phone and hand the device to a staff
member, who types a short passcode to attest "I saw this car parked here". That attestation
replaces the review step entirely — there is no "can we read this?" question left to ask. The
slip is approved immediately and searchable at once.

**On that path the AI cost is zero and the labelling bottleneck does not exist.** The review
queue went back to handling only what genuinely needed it: the hand-written slips that had
already been photographed. The best outcome of the OCR pipeline was identifying the cases where
it wasn't needed.

The same logic produced `/out`, where drivers request their own car back using their plate and
phone number, with the plate as the key and the phone as confirmation — and the passcode is
checked *before* any database lookup, so `/out` cannot be used to probe which plates are parked
here.

### Built for the actual conditions

Decisions that only make sense having watched people use it:

- **No JavaScript dependency on the public forms.** They are opened over mobile data in the
  middle of a car park; if htmx fails to load, a plain POST still works. htmx is vendored
  locally rather than pulled from a CDN, because a captive portal or broken DNS in a flood zone
  would otherwise cause files to vanish silently on submit.
- **44px minimum tap targets and 16px minimum font** on small screens — the latter because iOS
  Safari zooms the viewport when a smaller field takes focus, destroying the layout. A test
  asserts that every input type the forms use is actually covered by that rule, because it once
  silently omitted `tel` and `date`: precisely the phone and date fields, filled in one-thumbed
  beside a car.
- **Server-side rendering throughout, no chart library.** The dashboard summarises everything
  in a handful of queries so it opens on a phone in a field.
- **Mobile layout tested in a real browser over CDP**, because an `auto-fit` grid short by a
  fraction of a pixel collapses to one column while the HTML stays identical character for
  character.
- **A day-colour band on the driver's saved screenshot**, so staff can check at a glance that a
  screenshot is from the day it claims. Colours cycle every 7 days and quotes every 13, so the
  pair doesn't repeat for 91 days. It is an eyeball check and the README says so: a screenshot
  can always be doctored, and the real check is still looking the slip up by reference number.

## Stack

Python 3.14 · FastAPI · Postgres (`pg_trgm`, `fuzzystrmatch`) · OpenCV · Pillow · htmx ·
server-rendered Jinja2 · OpenRouter for model access · deployed on Render via Docker.

No ORM, no JS framework, no chart library, no background worker. 11.5k lines, five tables,
223 tests.

```
ocrslip/
  preprocess.py   paper detection, perspective warp, tone
  ocr.py          OpenRouter call, structured output, retries
  schema.py       field schema + the Thai OCR prompt
  review.py       which slips need a human
  dedup.py        duplicate detection (union-find)
  normalize.py    Thai normalization: numerals, honorifics, plates, dates
  search.py       fuzzy retrieval
  db.py           queries, claiming, audit log
  web/            FastAPI routes, templates, pipeline
bench/            the accuracy and crop-strategy benchmarks
tests/            223 tests
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set OPENROUTER_API_KEY and DATABASE_URL
.venv/bin/python -m ocrslip.db init
.venv/bin/uvicorn ocrslip.web.main:app --reload
```

Tests, including the ones that need real Postgres semantics:

```bash
OCRSLIP_TEST_DATABASE_URL=postgresql://user@127.0.0.1:5432/ocrslip_test \
  .venv/bin/python -m pytest -q
```

Deployment, configuration and operational detail are in
[README.th.md](README.th.md) (Thai), which is the fuller document.

## Privacy

This repository contains **no real slip data**. The slips held flood victims' names, phone
numbers and licence plates, so every fixture and sample value here is fabricated, the sample
images are synthesised in code, and the git history has been rewritten to remove the
benchmark's ground-truth files. Several test files say so explicitly at the top, because the
constraint is easy to forget six months later.

## A note on the name

In Thai the system is called *ระบบเอื้อเฟื้อที่จอดรถ* — "courtesy parking", deliberately not
"vehicle custody". What was being offered was space, not legal custody of anybody's property,
and the Thai word for the latter carries obligations nobody intended to take on. Naming it
accurately mattered to the people running it.

## Licence

MIT — see [LICENSE](LICENSE).
