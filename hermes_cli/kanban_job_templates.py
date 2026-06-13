"""Job templates for the kanban dispatcher (Olympus Dispatcher → Ultracode, S1).

Additive substrate: declarative per-board / per-lane policy that the dispatcher
reads at claim time to decide *how* a worker runs (its workspace kind, whether an
adversarial refuter must uphold the result before merge, what before/after proof
fields to require, and — later, in S4 — fan-out/join). Templates live in the
shared Hindsight bank (``hermes-swarm-shared``) as **directives** tagged
``content_type:job-template`` + ``project:olympus-dispatcher`` so every agent on
the platform can see them and a reader that doesn't understand the tag simply
ignores them.

Why directives and not memories
-------------------------------
Hindsight *memories* (``retain``/``recall``) run an extraction pipeline that
rephrases content into facts — a JSON template stored that way comes back
mangled and unparseable. *Directives* store their ``content`` **verbatim** and
are tag-filterable via ``list_directives(tags=...)``, which is exactly what a
deterministic, machine-read config store needs. ``create_directive`` is purely
additive and ``delete_directive`` makes the rollout reversible (HB8).

Design contract (S1)
--------------------
* ``dispatch_once`` is UNCHANGED — this module is not yet wired into the hot
  path (S2 does that). Importing it has no side effects.
* :func:`select_job_template` is PURE: deterministic precedence over
  already-fetched templates, no I/O.
* :func:`load_job_template` fetches from Hindsight then selects. It **fails safe
  to ``None`` on ANY read error** (service down, timeout, malformed payload, bad
  JSON) — a memory outage must degrade to today's untemplated behavior, never
  fail closed and stall the board (HB5).

Precedence (highest first): task-tag match > assignee match > board-default >
``None`` (no template → today's behavior).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# ── HB8 schema / wiring constants ───────────────────────────────────────────
# Overridable by env so tests and alternate deployments never touch the live
# shared bank by accident.
HINDSIGHT_BANK = os.environ.get("HERMES_HINDSIGHT_BANK", "hermes-swarm-shared")
HINDSIGHT_BASE_URL = os.environ.get("HINDSIGHT_BASE_URL", "http://127.0.0.1:8888")
# A directive is a job-template iff it carries BOTH of these tags.
TEMPLATE_TAGS: tuple[str, ...] = ("content_type:job-template", "project:olympus-dispatcher")

# Tight: a slow/hung memory service must never stall a dispatch tick (HB5).
_FETCH_TIMEOUT_SECONDS = 1.5
# Refresh roughly once per dispatch tick (dispatch_interval_seconds default 60).
_CACHE_TTL_SECONDS = 55

VALID_WORKSPACE_KINDS = frozenset({"scratch", "worktree", "dir"})
_VALID_SELECTOR_KINDS = frozenset({"tag", "assignee", "board-default"})
# Higher rank wins when multiple templates match the same task.
_PRECEDENCE = {"tag": 3, "assignee": 2, "board-default": 1}

# A fetcher returns a list of raw template payload dicts (already JSON-parsed).
FetcherT = Callable[[], "list[dict]"]


@dataclass(frozen=True)
class JobTemplate:
    """Declarative per-lane dispatcher policy.

    Immutable (frozen) so a fetched template can be shared across the tick
    without any caller mutating it. YAGNI: only fields the named sprints consume
    are present — ``fan_out`` is intentionally deferred to S4.
    """

    board: str
    selector_kind: str  # one of _VALID_SELECTOR_KINDS
    selector_value: Optional[str]  # tag/assignee string; None for board-default
    workspace_kind: str = "scratch"  # S2
    branch_prefix: Optional[str] = None  # S2
    require_refuter: bool = False  # S3
    refuter_skill: Optional[str] = None  # S3
    proof_fields: tuple[str, ...] = ()  # S4

    @property
    def precedence(self) -> int:
        return _PRECEDENCE.get(self.selector_kind, 0)


# ── Parsing (lenient, fail-safe) ────────────────────────────────────────────
def _coerce_template(raw: Any) -> Optional[JobTemplate]:
    """Build a :class:`JobTemplate` from a raw dict. Returns ``None`` on any
    validation problem (never raises) so one malformed entry can't poison the
    whole fetch.
    """
    if not isinstance(raw, dict):
        return None

    board = raw.get("board")
    if not isinstance(board, str) or not board.strip():
        return None

    # Selector: nested {"kind","value"} preferred; flat keys tolerated.
    selector = raw.get("selector")
    if isinstance(selector, dict):
        kind = selector.get("kind")
        value = selector.get("value")
    else:
        kind = raw.get("selector_kind")
        value = raw.get("selector_value")
    if kind not in _VALID_SELECTOR_KINDS:
        return None
    if kind == "board-default":
        value = None
    else:
        # tag / assignee selectors require a non-empty string value.
        if not isinstance(value, str) or not value.strip():
            return None

    workspace_kind = raw.get("workspace_kind", "scratch")
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        return None

    branch_prefix = raw.get("branch_prefix")
    if branch_prefix is not None and not isinstance(branch_prefix, str):
        return None

    refuter_skill = raw.get("refuter_skill")
    if refuter_skill is not None and not isinstance(refuter_skill, str):
        return None

    proof_raw = raw.get("proof_fields") or ()
    if not isinstance(proof_raw, (list, tuple)):
        return None
    proof_fields = tuple(str(p) for p in proof_raw)

    try:
        return JobTemplate(
            board=board.strip(),
            selector_kind=kind,
            selector_value=value,
            workspace_kind=workspace_kind,
            branch_prefix=branch_prefix,
            require_refuter=bool(raw.get("require_refuter", False)),
            refuter_skill=refuter_skill,
            proof_fields=proof_fields,
        )
    except (TypeError, ValueError):  # defensive — shouldn't happen post-validation
        return None


# ── Selection (PURE) ────────────────────────────────────────────────────────
def select_job_template(
    templates: Sequence[JobTemplate],
    board: str,
    assignee: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> Optional[JobTemplate]:
    """Deterministically pick the winning template for a task. Pure — no I/O.

    Precedence: task-tag match > assignee match > board-default > ``None``.
    Among same-precedence matches (e.g. two tag selectors), the tie is broken
    deterministically by ``selector_value`` so the result never depends on fetch
    order.
    """
    if not board:
        return None
    tagset = set(tags or ())
    best: Optional[JobTemplate] = None
    for t in templates:
        if t.board != board:
            continue
        if t.selector_kind == "tag":
            if t.selector_value not in tagset:
                continue
        elif t.selector_kind == "assignee":
            if t.selector_value != assignee:
                continue
        elif t.selector_kind != "board-default":
            continue  # unknown selector kind — ignore defensively
        if best is None or _beats(t, best):
            best = t
    return best


def _beats(candidate: JobTemplate, incumbent: JobTemplate) -> bool:
    """True if ``candidate`` should replace ``incumbent`` (higher precedence, or
    equal precedence with a deterministic tiebreak)."""
    if candidate.precedence != incumbent.precedence:
        return candidate.precedence > incumbent.precedence
    return (candidate.selector_value or "") < (incumbent.selector_value or "")


# ── Fetch (Hindsight directives, fail-safe) ─────────────────────────────────
def _safe_close(client: Any) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 — close failures are never fatal
        pass


def _iter_directive_items(items: Any):
    """Yield directive objects from whatever shape ``list_directives`` returns
    (a list, an object with ``.items``/``.directives``, or a ``{"items": [...]}``
    dict)."""
    if items is None:
        return
    if isinstance(items, dict):
        items = items.get("items") or items.get("directives") or []
    elif hasattr(items, "items") and not isinstance(items, (list, tuple)):
        attr = getattr(items, "items")
        items = attr() if callable(attr) else attr
    elif hasattr(items, "directives"):
        items = getattr(items, "directives")
    if not isinstance(items, (list, tuple)):
        return
    yield from items


def _get_field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _run_coro(coro: Any) -> Any:
    """Run an async coroutine to completion from a sync context.

    The dispatcher executes ``dispatch_once`` inside a worker thread (the gateway
    wraps the sweep in ``asyncio.to_thread``), so there is normally no running
    loop here and ``asyncio.run`` is correct. If a running loop IS present
    (defensive), run the coroutine on a throwaway thread so this never raises
    "cannot be called from a running event loop"."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _default_fetcher() -> list[dict]:
    """Fetch raw job-template payloads from Hindsight **inactive** directives.

    Templates are stored as *inactive* directives: they hold verbatim JSON the
    dispatcher parses and are tag-filterable, yet stay INERT for every other
    agent — active directives get injected into ``reflect``/``recall``, inactive
    ones don't, so the shared bank's behavior for everyone else is unchanged
    (HB8). Raises on transport failure — the caller wraps this in the fail-safe
    boundary."""
    from hindsight_client import Hindsight  # lazy: optional dependency

    client = Hindsight(
        base_url=HINDSIGHT_BASE_URL,
        timeout=_FETCH_TIMEOUT_SECONDS,
        user_agent="hermes-kanban-dispatcher/1.0",
    )
    try:
        # Low-level call: the convenience wrapper can't pass active_only, and we
        # need the inactive (inert) template directives.
        items = _run_coro(
            client.directives.list_directives(
                bank_id=HINDSIGHT_BANK,
                tags=list(TEMPLATE_TAGS),
                active_only=False,
            )
        )
    finally:
        _safe_close(client)

    raw: list[dict] = []
    for directive in _iter_directive_items(items):
        content = _get_field(directive, "content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            logger.debug("job-template directive has non-JSON content; skipping")
            continue
        if isinstance(payload, dict):
            raw.append(payload)
        elif isinstance(payload, list):
            raw.extend(p for p in payload if isinstance(p, dict))
    return raw


def _fetch_templates(fetcher: Optional[FetcherT] = None) -> tuple[JobTemplate, ...]:
    """Run the fetcher and coerce results. Fails safe to ``()`` on any error."""
    fetch = fetcher or _default_fetcher
    try:
        raw = fetch()
    except Exception as exc:  # noqa: BLE001 — memory outage must not stall the board
        logger.debug("job-template fetch failed (fail-safe to none): %s", exc)
        return ()
    if not raw:
        return ()
    out: list[JobTemplate] = []
    for item in raw:
        tmpl = _coerce_template(item)
        if tmpl is not None:
            out.append(tmpl)
    return tuple(out)


# ── Per-tick cache ──────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: "dict[str, tuple[float, tuple[JobTemplate, ...]]]" = {}


def _fetch_cached() -> tuple[JobTemplate, ...]:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get("default")
        if entry is not None and entry[0] > now:
            return entry[1]
    templates = _fetch_templates(fetcher=None)
    with _cache_lock:
        _cache["default"] = (now + _CACHE_TTL_SECONDS, templates)
    return templates


def clear_cache() -> None:
    """Drop the per-tick template cache (used by tests and after a template
    write)."""
    with _cache_lock:
        _cache.clear()


# ── Public entry point ──────────────────────────────────────────────────────
def load_job_template(
    board: str,
    assignee: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    *,
    fetcher: Optional[FetcherT] = None,
    use_cache: bool = True,
) -> Optional[JobTemplate]:
    """Fetch (cached, fail-safe) + pure-select the winning template for a task.

    Returns ``None`` when no template applies OR on any failure (unreadable
    memory service, malformed payload, selection error) — the dispatcher then
    behaves exactly as it does today for that task.

    ``fetcher`` overrides the Hindsight reader (tests / S2 injection) and bypasses
    the shared cache. ``use_cache=False`` forces a fresh read.
    """
    if not board:
        return None
    try:
        if fetcher is not None:
            templates = _fetch_templates(fetcher=fetcher)
        elif use_cache:
            templates = _fetch_cached()
        else:
            templates = _fetch_templates(fetcher=None)
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders fail-safe
        logger.debug("load_job_template fetch error (fail-safe none): %s", exc)
        return None
    try:
        return select_job_template(templates, board, assignee=assignee, tags=tags)
    except Exception as exc:  # noqa: BLE001
        logger.debug("select_job_template error (fail-safe none): %s", exc)
        return None
