# Material recurrence bound — the third rung, and why it is a blocker rather than a harness action

**Date:** 2026-08-13
**Status:** implemented (`ab3f13e`); evidence base corrected same day — see below

> **Evidence correction (2026-08-13, post-implementation).** Every count in
> §1 came from `grep -c` over console logs, which re-render context history —
> old error lines included — on every save. The authoritative
> `control_events.jsonl` shows rocketmq-externals produced **5**
> ENV_EXECUTABLE_NOT_FOUND events in a 34-turn, 4-minute run, not ~689; the
> other five projects produced 1–2 each, and none looped until spent. The runs
> died of single-point aborts and honest fast blocks (task #45), not of this
> loop. The bound stands as cheap defense-in-depth — its mechanism is sound
> and its cost is near zero — but it was NOT the fix for D2's failures, and
> its live acceptance criterion ("single digits instead of ~689") was already
> true before it landed.
**Scope:** the remaining half of task #42. The wrapper-naming half landed in
`a07b1d3`.

## 1. The observation this exists to answer

D2 (2026-08-12) burned six runs to zero compiled classes on one path.
rocketmq-externals refused **~689 times across three Maven paths**
(`/usr/share/maven/bin/mvn` 260, `/opt/maven/bin/mvn` 225, `/usr/bin/mvn` 204).
Alongside it: spark-kubernetes-operator ×151, geode ×142, samza-hello-samza
×139, gora ×104, tapestry-5 ×71.

The instinct is "the guidance is missing." The evidence says otherwise. In
that same rocketmq log the provision route rendered **5 times** and
"already registered" **zero** times. The advice fires. The model continues.

So the harness already had two rungs and both worked as designed:

1. **The refusal names a productive move** — every time.
2. **Recurrence ≥ 2 arms an advisor redirect** (`react_engine.py:6041-6047`,
   via `LoopMemory`, which does record tool failures as recurrence candidates —
   `_is_recurrence_candidate` returns True for `operation_outcome == "failed"`).

There is no third rung. `LoopMemory`'s only hard cap — `completion_claim_cap`
— governs no-op *completion claims*, not material actions. A material action
may therefore recur without bound, and did, until the wall clock ended the run.

**The defect is not what the harness says. It is that nothing ever stops
saying it.**

## 2. The decision

Two options were on the table.

**(a) The harness performs the provision itself**, on the precedent of the
test-phase floor, where the controller executes a registered action when the
phase cannot otherwise close honestly.

**(b) The recurrence converges to a typed blocker** that the existing gate and
verdict machinery already know how to carry.

**We choose (b).** Three reasons, in order of weight:

1. **(a) would re-introduce what WS3 deliberately deleted.** That workstream
   removed `repair_contracts.py`, `retry_authority.py` and `tool_recovery.py`
   precisely to stop the harness from substituting its own action for the
   model's. The test-floor precedent does not extend here: that action is
   *mechanically determined* by the survey (a registered test command for a
   surveyed test root). "Which package installs Gradle" is not mechanically
   determined — it is a guess, and on these projects usually the wrong one,
   since apt's Gradle is typically too old and the right answer is the wrapper
   the project already ships (`a07b1d3`).
2. **The harm is the burn, not the missing action.** Six runs did not fail for
   want of one apt call; they failed because nothing converted a known-hopeless
   repetition into a stated fact. Ending the repetition recovers the wall clock
   for work that can succeed.
3. **A blocker is the honest object.** P2: no basis is its own answer. "This
   registration has been refused N times and will not succeed" is a fact the
   verdict can carry; a guessed installation is a new claim the harness would
   have to defend.

## 3. The contract

**Bound.** Three identical refusals, matching `completion_claim_cap`'s existing
precedent rather than inventing a second number. Identity is
`(tool, error_code)` — deliberately NOT the exact executable path, because
rocketmq cycled three paths for one tool and a path-keyed bound would never
have fired.

**On the third refusal:**

- the result stays a refusal — the model is never told an action succeeded;
- a typed blocker is recorded once in `RunEvidenceState`, naming the tool, the
  paths already refused, and the moves that remain (the project's wrapper when
  one was found, and the provision route);
- the tool stops re-probing the container for the paths that identity has
  already refused. The probe cannot change its answer for those, and the round
  trip is pure cost.

> **Correction (2026-08-13, found in implementation).** This clause first read
> "stops re-probing for that identity", which deadlocks the run. After the
> bound, `env register tool=maven <anything>` would be refused unseen —
> including the project wrapper the same refusal recommends first, and
> including a path a successful provision just installed. The reset below
> ("when a registration for that tool succeeds") then becomes unreachable, and
> nothing else resolves the blocker. That contradicts both this spec's own
> "a run that recovers by another route is never cut short" and rung 1's
> promise that a refusal names a move that can succeed.
>
> The bound and the count stay keyed on `(tool, error_code)` — that is what
> makes rocketmq's three paths trip it. Only the probe suppression is
> path-scoped, because "the probe cannot change its answer" is true of a path
> already refused and false of one never tried. On the D2 shape this still
> removes 686 of the 689 round trips; a first look at a new path is probed
> once, joins the refused set, and does not restate the wall.

**What does NOT change:** no phase is force-closed and no action is
synthesized. The blocker makes the state legible; the existing convergence
machinery decides what to do with it, exactly as it does for every other
blocker. A run that recovers by another route is never cut short.

**Counting scope:** per run, per `(tool, error_code)`. The counter resets when
a registration for that tool succeeds — a later failure after a real success is
new information, not the same wall.

## 4. Acceptance

- Unit: the third identical refusal records exactly one blocker naming the
  tool, the refused paths, and the remaining moves; the first two do not.
- Unit: three refusals across three DIFFERENT paths for one tool trip the bound
  (the rocketmq shape); three refusals across three different TOOLS do not.
- Unit: a successful registration resets the counter.
- Unit: after the bound, no further container probe is issued for that identity
  and the returned result is still a refusal, never a success.
- Live: a re-run of rocketmq-externals under a new campaign lock shows the
  refusal count bounded in the low single digits instead of ~689, and the run
  spends its remaining wall clock elsewhere.

## 5. Out of scope

Whether the model *should* have used the wrapper is `a07b1d3`'s question and is
already answered. Whether `search` can find a file by name is task #43. Neither
is relitigated here.
