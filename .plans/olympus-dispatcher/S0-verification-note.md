# S0 — Cold-start verification note (Olympus Dispatcher → Ultracode)

**Captured:** 2026-06-13 · **Live HEAD:** `f617c46c5` · **Branch:** `chore/olympus-dispatcher-s0-baseline`
**Worktree:** `/Users/chas-studio/repos/wt-olympus-dispatcher` (outside the live checkout — live `~/.hermes/hermes-agent` never reset/edited)
**Target repo decision (operator, 2026-06-13):** land in **hermes-agent** (worktree off main → PR to fork `hugginsc10/hermes-agent`); **port to `hugginsc10/Olympus` as a documented follow-on** once proven on production. If/when ported, re-anchor every file:line against the Olympus tree first.

---

## 1. Live state confirmed

- `~/.hermes/hermes-agent` HEAD = **`f617c46c5`** (`main`) — exactly the expected post-deploy baseline. ✓
- In-history (verified `git merge-base --is-ancestor`): PR7 `da80c3141` ✓, verification gate `daee7cb41` ✓, PR8 `bae1cc8ad` ✓, approvals `6d29d4979`/`60ad18b48` ✓.
- Fork remote = `hugginsc10/hermes-agent`; origin = `NousResearch/hermes-agent`. PRs go to **fork** (precedent #3/#4/#7/#8). `check-attribution` CI = KNOWN FALSE POSITIVE.

## 2. HB7 foreign-WIP assessment — CLEAR to proceed

- Live `main` **tracked files clean**. Only untracked item: `hermes-agent-wt/` — a **stale leftover worktree** from the now-merged PR #9 (`feat/kanban-complete-verification-gate`, `a06b522a5`, already in main). Not active foreign work.
- **Zero open PRs** on the fork → no active concurrent session. The worker-reliability session that motivated HB7 is DONE.
- `t_*` worktrees under `agent-workspace-ops/workspaces/` are the **live dispatcher's own worker worktrees** (normal board state, incl. known stranded WIPs in `t_67af0739` / `t_1cf3796b` — handled separately; **do not touch**).
- Two stale leftover worktrees noted, **NOT deleted** (never edit the live checkout): `hermes-agent/hermes-agent-wt` and `~/hermes-agent-approvals-wt`.

## 3. Re-anchored file:line (plan anchors were PRE-deploy → corrected against `f617c46c5`)

`hermes_cli/kanban_db.py` is now **8129 lines**.

| Symbol | Plan (pre-deploy) | Live `f617c46c5` |
|---|---|---|
| `dispatch_once(` def | 6208 / 6211 | **6362** |
| `_default_spawn(` def | 6886 | **7040** |
| `resolve_workspace(` | 4724 | **4878** |
| `_classify_worker_exit(` | 4990 | **5144** |
| `_spawn = spawn_fn …` + `inspect.signature(_spawn)` | one site (6493/6500) | **TWO sites — normal: 6647/6654 · review-lane: 6752/6756** |
| `claimed.skills = ["sdlc-review"]` (force-set) | 6597 | **6751** |
| review-column dispatch block | 6533–6620 | **6687–6774** |
| `_merge_current_run_metadata(` def | — | **2805** |
| run-metadata write sites (S4 `proof` target) | 6486–6492 / 7039–7044 | **6641, 6739 (dispatch_once) · 7193 (evidence path)** |
| `_default_spawn` env pins (`HERMES_KANBAN_*`) | — | **7089 TASK · 7090 WORKSPACE · 7092 BRANCH · 7094 RUN_ID · 7096 CLAIM_LOCK · 7123 WORKSPACES_ROOT · 7128 BOARD** |
| `detect_crashed_workers(` (HB3 reaper hook) | — | **5700** |
| `VALID_WORKSPACE_KINDS = {"scratch","worktree","dir"}` | — | **102** |
| `gateway/run.py` `create_task(self._kanban_dispatcher_watcher())` | 5107 | **5164** |
| `gateway/kanban_watchers.py` `def _kanban_dispatcher_watcher` | 559 | **559** (unchanged) |
| `gateway/kanban_watchers.py` single-owner `-shm` note (HB7) | 49 | **50** |
| `hermes_cli/kanban_decompose.py` `decompose_task(` | 271 | **271** (unchanged); children build ~399–447 |
| `hermes_cli/profiles.py` `profile_exists(` | 307 | **307** (unchanged) |
| config block `"kanban": {` | (other file) | **`hermes_cli/config.py:2103`** |
| `dispatch_in_gateway` | 521 | **config.py:2109** (True) |
| `dispatch_interval_seconds` | 522 | **config.py:2112** (60) |
| `failure_limit` | 523 → **3** | **config.py:2116 → `2`** ⚠️ default changed |
| `auto_decompose` | 529 | **config.py:2145** (True); `auto_decompose_per_tick` 2149 (3) |
| `dispatch_stale_timeout_seconds` | 531 | **config.py:2155** (14400 / 4h) |

## 4. Material drift the later sprints MUST act on

1. **Two spawn + board-introspection sites, not one** (kanban_db.py **6647–6660** normal dispatch + **6752–6762** review lane) — byte-identical blocks:
   ```python
   _spawn = spawn_fn if spawn_fn is not None else _default_spawn
   sig = inspect.signature(_spawn)
   if "board" in sig.parameters:
       pid = _spawn(claimed, str(workspace), board=board)
   else:
       pid = _spawn(claimed, str(workspace))
   ```
   → **HB4 (board-pin survival) and S2's spawn wrapper must cover BOTH sites.** Any wrapper that drops `board` from `_default_spawn`'s effective signature silently kills the pin at both → cross-board DB corruption.
2. **`failure_limit` default is now `2` (was 3).** HB2 reuses `failure_limit`/the PR8 breaker for bounded refuter attempts → budget bounded-attempt logic against **2**, not 3.
3. **Worktree creation is WORKER-SIDE today** — `resolve_workspace` returns a worktree path but "Worker skill creates it" (kanban_db.py:4893, 4931). `materialize_worktree` / `cleanup_worktree` **do not exist** → confirms S2's gap (move creation dispatcher-side, idempotent + reaped).
4. **HB1 pre-scout (the #1 trap) — the real PASS/merge gate is NOT in `kanban_db.py`.** `kanban_db.py:6687–6774` only *spawns* the review agent (force-sets `skills=['sdlc-review']` at 6751). The verdict/PASS/merge decision lives **inside the sdlc-review skill content**, which is **not in the repo** but at:
   - `~/.hermes/skills/software-development/sdlc-review` (canonical)
   - `~/.hermes/profiles/{reviewer,reviewer-grok,strategist,builder}/skills/software-development/sdlc-review` (per-profile copies)
   **S3 must locate and cite the real verdict/merge file:line HERE before changing anything — editing `kanban_db.py` would "fix" the wrong file and ship green tests over a still-fakeable gate.**
5. S1 target `hermes_cli/kanban_job_templates.py` and S2 target `hermes_cli/kanban_worktree.py` both **ABSENT** ✓ (no partial prior work).

## 5. Frozen empirical baseline → `baseline.json`

Read-only (`PRAGMA query_only=ON`), replicating `platform-self-audit.py` metric definitions. **Zero kanban.db writes.**

**`agent-workspace-ops`, since-overhaul window (the immovable "before" for S5):**
- **crash_rate = 17.3%** · **failure_share = 18.1%** (237 runs, 118 completed, runs/completion 2.008) — matches plan's expected ~17.8% / ~18.7%.
- 7d rolling = **29.9% / 30.4%** (pre-overhaul-contaminated — NOT the baseline, exactly as the plan warns).

**HB6 stratification (decisive for S5) — the 17.3% aggregate is dominated by two pathological lanes:**

| Lane | runs | crash_rate | note |
|---|---|---|---|
| `inbox-triage` | 21 | **100%** (21/21) | totally broken lane — **separate platform bug, flag as opportunity** |
| `reviewer` | 44 | **43.2%** (19/44) | the sdlc-review lane S3 touches |
| `builder` | 105 | **0.95%** (1/105) | the actual repo-work lane — healthy |
| strategist / reviewer-grok / researcher / others | — | ~0% | |

→ **S5's improvement claim MUST be stratified by lane/profile** or the aggregate is noise dominated by inbox-triage/reviewer. The dispatcher never observes success directly — proof must come from dispatcher artifacts or the independent refuter, never worker self-attestation.

Default board (`~/.hermes/kanban.db`): effectively idle (0 runs in since-overhaul window) — informational only.

## 6. DO-NOT-REDO (verified LIVE in `main` @ `f617c46c5`)

PR7 `da80c3141` (sticky `provider_auth_required`, redaction, `safe_getcwd`) · PR8 `bae1cc8ad` (non-zero exit on fatal abort, EX_TEMPFAIL 75 requeue, systemic-trip breaker) · verification gate `daee7cb41` (worker self-completion gate + verification artifacts) · approval fan-out + telegram approval callbacks (`6d29d4979`/`60ad18b48`) · kanban workspace-rescue/resume (PR #2) · hermetic gateway tests (PR #3) · OAuth single-source (PR #4) · failure-share telemetry (ops#6) · CLI exit-code propagation. **`completed_adopted` reconciliation proven unnecessary — do NOT add.** The verification gate is the *worker-side* gate; S3's refuter is the *reviewer-side* gate — **complementary, build on it, do not duplicate.**

## 7. S0 acceptance checklist

- [x] Live HEAD confirmed `f617c46c5` (≥ expected); PR7/PR8/verification-gate/fan-out confirmed in main (SHAs recorded).
- [x] Anchors re-grepped & corrected for all later sprints (§3).
- [x] Worktree created **outside** `~/.hermes/hermes-agent`; live checkout **byte-for-byte unchanged** (`main` still `f617c46c5`, tracked files clean).
- [x] `baseline.json` captured from `query_only` reads; **zero kanban.db writes**.
- [x] Do-NOT-redo note written (incl. `f617c46c5` deploy).
- [x] No dispatcher PR (S0 = docs/artifacts only).

## 8. Open decisions — status (ask before the sprint that needs each)

| Decision | Needed by | Status |
|---|---|---|
| Target repo | S0 / all PRs | **RESOLVED** → hermes-agent now, port to Olympus later |
| Which boards get templates first | S1 | proceeding with plan default (**agent-workspace-ops only**; default board = no template) — confirm/​widen at S1 ⛳ |
| Refuter compute budget / sampling | S3 | **pending** — ask before S3 |
| Worktree teardown vs PR lifecycle | S2 | **pending** — ask before S2 |
| Restart-serialization owner | S2 (first restart) | **pending** — ask before S2 ⛳ |
| Fan-out join on a refuted child | S4 | **pending** — ask before S4 |
