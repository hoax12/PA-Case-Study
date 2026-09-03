# Evaluation report: prior-authorization adjudication

The two errors are not symmetric. A **false affirmation** clears a request the guideline does not support and the payer owns the consequence. A **false referral** costs a reviewer a few minutes. So the false-affirmation rate is the primary metric and must be zero; coverage, the share of genuinely clear-cut approvals the system removes from the queue, is optimised only inside that constraint.

- Packets: 8
- Decision date: 2026-09-01
- Model: claude-opus-5
- Mode: replayed from cache

## 1. Outcome confusion matrix

| expected \ predicted | provisional_affirmation | refer_to_human |
|---|---|---|
| provisional_affirmation | 1 | 0 |
| refer_to_human | 0 | 7 |

## 2. False-affirmation rate (primary)

**FAR = 0/7 (0%)** of non-affirmable cases: target 0.

No packet was affirmed against its ground truth.

Eligible affirmations achieved: **1/1 (100%)**. Reporting this beside FAR matters: a system that referred everything would score a perfect FAR and be worthless.

## 3. Coverage vs. threshold (risk/coverage curve)

| τ | coverage (affirmed / expected-affirm) | false affirmations |
|---|---|---|
| 0.5 | 1/1 (100%) | 0 |
| 0.7 | 1/1 (100%) | 0 |
| 0.9 | 0/1 (0%) | 0 |

FAR stays 0 at every τ: the packets that must be referred fail on their predicate (NOT_MET or UNKNOWN), and no threshold can turn those into an affirmation. τ only removes coverage. The cliff between 0.7 and 0.9 is structural, not empirical: a satisfied boolean or attestation criterion is scored 0.8, and confidence is the minimum across criteria, so τ > 0.8 refers every packet that rests on documented attestations. That is the honest reading: on this set τ buys no safety, and 0.9 would cost all of it.

## 4. Per-criterion accuracy

**82/82 (100%)** criterion statuses match the ground truth.

No criterion mismatches.

## 5. Evidence traceability

**78/78 (100%)** of decided criteria cite a span that matches the transcript at ≥0.8 token overlap. A span that fails this gate is forced to UNKNOWN before it can reach the decision, so a low number here would mean the gate is doing work, not that ungrounded evidence was acted on.

## 6. Confidence reliability

| confidence band | criterion accuracy |
|---|---|
| low  (<0.5) | 4/4 (100%) |
| med  (0.5-0.8) | n/a |
| high (>=0.8) | 78/78 (100%) |

Confidence is an uncalibrated legibility signal, not a probability. This table is the check on that claim; it is not used to justify treating the number as one.

## 7. Cost and latency per packet

| packet | outcome | expected | seconds | in tokens | out tokens | USD | replayed |
|---|---|---|---|---|---|---|---|
| medicare_tore | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 2 stage(s) |
| out_of_domain | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 2 stage(s) |
| primary_rygb_delegated | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 2 stage(s) |
| tore_affirm | provisional_affirmation | provisional_affirmation | 0.1 | 0 | 0 | $0.000 | 3 stage(s) |
| tore_ambiguous_date | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 3 stage(s) |
| tore_bmi_missing | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 3 stage(s) |
| tore_temporal_fail | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 3 stage(s) |
| tore_tobacco_recent | refer_to_human | refer_to_human | 0.1 | 0 | 0 | $0.000 | 3 stage(s) |

Total: $0.00 over 1s, priced at $15.0/$75.0 per million input/output tokens.
A replayed stage cost nothing on this run, so any row with replays understates a cold packet. Measured cold on the same eight packets, the full pipeline (OCR of every page, extraction, evidence) ran **$0.79–$0.86 and 64–78 s per packet**. Latency is dominated by per-page OCR, which is trivially parallel, and nothing here is on an interactive path: a reviewer opens a packet the pipeline finished minutes earlier.
