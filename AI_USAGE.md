# AI Usage Disclosure

## Tools used

- **Antigravity**
- **OpenCode Cli**

## What I used them for

- **Antigravity:** cloning the assignment repository, creating my private
  repo, and walking me through the project structure.
- **OpenCOde cli:** I used it to draft the implementation code and tests, and
  to explain the spec's rules and the design options as I worked through
  them. I then ran and checked everything locally.

## What I wrote or decided myself

I ran this as a directed, step-by-step AI-assisted build - the AI drafted
code, and I owned the engineering process around it:

- **Scope and sequencing.** I chose to build and verify the database and
  ingestion layers before touching reports, and took the work in small
  reviewed pieces instead of one big dump, so I could run and check each
  stage before the next was built on it.
- **Requirements decisions.** I pushed on the requirements I needed to
  understand: how drops combine, how orphan events behave, how to store
  phones without storing full numbers in plaintext, and how the hashing
  key is handled. I chose SQLite for the store.
- **Spec verification.** I checked the AI's explanations against the
  README instead of trusting them - which caught a real error in the
  merge rule (details below).
- **Local verification.** I ran the seed-42 pipeline locally and checked
  the reported counts before proceeding: the test suites, the full
  pipeline run, the re-ingestion idempotence checks, and the Git hygiene
  checks.
- **Repository decisions.** What gets committed, what stays local, and
  what the submission says.

## Where the AI was wrong or unhelpful

- **Merge rule (the important catch):** the AI first explained duplicate
  merging as "fill in only missing fields". The spec says later valid
  rows overwrite fields where their value is non-null - last write wins.
  I noticed the mismatch, pasted the README's exact wording, and the AI
  corrected its explanation; the implementation that followed used the
  corrected rule. This is exactly the kind of silent data bug that
  checking against the spec can catch.
- **Guessed filename:** it once referred to `events_0002.jsonl`; the real
  drop-2 file is `events_1001.jsonl`. Visible in my own run output, and
  it corrected its run notes.
- **Verification proved its worth:** `git status --short` showed the
  secret `.hmac_key` as untracked, so it was added to `.gitignore` before
  anything was staged or committed.

## How I verified AI-generated code

- Ran the module test suites on my machine and watched them pass
  (observed: lead ingestion, event linking, report generator, pipeline),
  plus the database consistency check and the call-summary run on the
  real database.
- Ran the full pipeline on the real seed-42 data and compared the numbers
  against the references the AI computed separately - all matched.
- Re-ran ingest and report to confirm idempotence: every file skipped,
  all report checks passing again.
- Some additional verification was done with AI assistance and not
  personally run by me, disclosed here for completeness: on a fresh
  seed-7 dataset, reports came out byte-identical across same-order
  separate, all-at-once, and repeated ingestion, and the report
  aggregates were independently recomputed in SQL as a cross-check.
