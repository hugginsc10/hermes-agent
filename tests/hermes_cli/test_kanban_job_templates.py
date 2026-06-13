"""Unit tests for the kanban job-template reader (Olympus Dispatcher → Ultracode, S1).

Covers the plan's required cases — present / absent / malformed→None / precedence
— plus the fail-safe contract (Hindsight outage → None, never raise) and
deterministic selection. The reader is exercised through injected fetchers so no
live Hindsight service is required.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_job_templates as jt

BOARD = "agent-workspace-ops"


def _board_default(**over) -> dict:
    base = {
        "board": BOARD,
        "selector": {"kind": "board-default", "value": None},
        "workspace_kind": "worktree",
        "branch_prefix": "builder/",
        "require_refuter": True,
        "refuter_skill": "sdlc-refuter",
        "proof_fields": ["before", "after", "refuter_verdict", "captured_at"],
    }
    base.update(over)
    return base


def _assignee_tmpl(assignee: str, **over) -> dict:
    base = {
        "board": BOARD,
        "selector": {"kind": "assignee", "value": assignee},
        "workspace_kind": "worktree",
    }
    base.update(over)
    return base


def _tag_tmpl(tag: str, **over) -> dict:
    base = {
        "board": BOARD,
        "selector": {"kind": "tag", "value": tag},
        "workspace_kind": "worktree",
    }
    base.update(over)
    return base


def _fetcher(*payloads):
    """Return a fetcher callable yielding the given raw payload dicts."""
    return lambda: list(payloads)


@pytest.fixture(autouse=True)
def _clear_cache():
    jt.clear_cache()
    yield
    jt.clear_cache()


# ── present ─────────────────────────────────────────────────────────────────
def test_board_default_present_returns_template():
    tmpl = jt.load_job_template(BOARD, assignee="builder", fetcher=_fetcher(_board_default()))
    assert tmpl is not None
    assert tmpl.board == BOARD
    assert tmpl.selector_kind == "board-default"
    assert tmpl.workspace_kind == "worktree"
    assert tmpl.require_refuter is True
    assert tmpl.refuter_skill == "sdlc-refuter"
    assert tmpl.proof_fields == ("before", "after", "refuter_verdict", "captured_at")


def test_flat_selector_keys_are_tolerated():
    raw = {
        "board": BOARD,
        "selector_kind": "board-default",
        "selector_value": None,
        "workspace_kind": "worktree",
    }
    tmpl = jt.load_job_template(BOARD, fetcher=_fetcher(raw))
    assert tmpl is not None and tmpl.selector_kind == "board-default"


# ── absent ──────────────────────────────────────────────────────────────────
def test_no_templates_returns_none():
    assert jt.load_job_template(BOARD, fetcher=_fetcher()) is None


def test_template_for_other_board_does_not_match():
    other = _board_default(board="some-other-board")
    assert jt.load_job_template(BOARD, fetcher=_fetcher(other)) is None


def test_empty_board_arg_returns_none():
    assert jt.load_job_template("", fetcher=_fetcher(_board_default())) is None


# ── malformed → None / skipped ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        {"selector": {"kind": "board-default"}},  # missing board
        {"board": "", "selector": {"kind": "board-default"}},  # empty board
        {"board": BOARD, "selector": {"kind": "nonsense"}},  # bad selector kind
        {"board": BOARD, "selector": {"kind": "tag"}},  # tag selector w/o value
        {"board": BOARD, "selector": {"kind": "assignee", "value": ""}},  # empty value
        {"board": BOARD, "selector": {"kind": "board-default"}, "workspace_kind": "vm"},  # bad ws kind
        {"board": BOARD, "selector": {"kind": "board-default"}, "proof_fields": "notalist"},
        "not-a-dict",
        42,
        None,
    ],
)
def test_malformed_payload_is_skipped(bad):
    # A malformed entry yields no template (and must never raise).
    assert jt.load_job_template(BOARD, fetcher=_fetcher(bad)) is None


def test_malformed_entry_does_not_poison_valid_one():
    tmpl = jt.load_job_template(
        BOARD,
        fetcher=_fetcher("garbage", {"board": BOARD, "selector": {"kind": "bogus"}}, _board_default()),
    )
    assert tmpl is not None and tmpl.selector_kind == "board-default"


# ── precedence: tag > assignee > board-default ──────────────────────────────
def test_assignee_beats_board_default():
    tmpl = jt.load_job_template(
        BOARD,
        assignee="reviewer",
        fetcher=_fetcher(_board_default(), _assignee_tmpl("reviewer", branch_prefix="rev/")),
    )
    assert tmpl is not None
    assert tmpl.selector_kind == "assignee"
    assert tmpl.branch_prefix == "rev/"


def test_tag_beats_assignee_and_board_default():
    tmpl = jt.load_job_template(
        BOARD,
        assignee="reviewer",
        tags=["urgent"],
        fetcher=_fetcher(_board_default(), _assignee_tmpl("reviewer"), _tag_tmpl("urgent", branch_prefix="hot/")),
    )
    assert tmpl is not None
    assert tmpl.selector_kind == "tag"
    assert tmpl.branch_prefix == "hot/"


def test_assignee_template_ignored_when_assignee_differs():
    tmpl = jt.load_job_template(
        BOARD,
        assignee="builder",
        fetcher=_fetcher(_board_default(), _assignee_tmpl("reviewer")),
    )
    assert tmpl is not None and tmpl.selector_kind == "board-default"


def test_tag_template_ignored_when_tag_absent():
    tmpl = jt.load_job_template(
        BOARD,
        assignee="builder",
        tags=["routine"],
        fetcher=_fetcher(_board_default(), _tag_tmpl("urgent")),
    )
    assert tmpl is not None and tmpl.selector_kind == "board-default"


def test_same_precedence_tiebreak_is_deterministic():
    # Two tag selectors both match; tie broken by selector_value, regardless of order.
    a = _tag_tmpl("aaa", branch_prefix="a/")
    b = _tag_tmpl("bbb", branch_prefix="b/")
    t1 = jt.load_job_template(BOARD, tags=["aaa", "bbb"], fetcher=_fetcher(a, b))
    t2 = jt.load_job_template(BOARD, tags=["aaa", "bbb"], fetcher=_fetcher(b, a))
    assert t1 is not None and t2 is not None
    assert t1.selector_value == t2.selector_value == "aaa"


# ── fail-safe: outage / bad transport → None, never raise ───────────────────
def test_fetcher_raising_returns_none():
    def boom():
        raise ConnectionError("hindsight down")

    assert jt.load_job_template(BOARD, fetcher=boom) is None


def test_fetcher_returning_none_returns_none():
    assert jt.load_job_template(BOARD, fetcher=lambda: None) is None


# ── pure selection ──────────────────────────────────────────────────────────
def test_select_job_template_is_pure_and_filters_board():
    templates = [
        jt._coerce_template(_board_default()),
        jt._coerce_template(_board_default(board="other")),
    ]
    templates = [t for t in templates if t is not None]
    assert len(templates) == 2
    chosen = jt.select_job_template(templates, BOARD)
    assert chosen is not None and chosen.board == BOARD
    # Pure: calling again yields the identical object, inputs unmutated.
    assert jt.select_job_template(templates, BOARD) is chosen


def test_select_empty_board_returns_none():
    templates = [jt._coerce_template(_board_default())]
    assert jt.select_job_template([t for t in templates if t], "") is None


# ── directive content parsing (the real default-fetcher shape) ──────────────
def test_default_fetcher_parses_verbatim_directive_json():
    # Simulate what list_directives returns: objects whose .content is verbatim JSON.
    class _Directive:
        def __init__(self, content, tags):
            self.content = content
            self.tags = tags

    directives = [
        _Directive(json.dumps(_board_default()), list(jt.TEMPLATE_TAGS)),
        _Directive("not json at all", list(jt.TEMPLATE_TAGS)),  # skipped, not fatal
        _Directive("", list(jt.TEMPLATE_TAGS)),  # empty, skipped
    ]

    raw = []
    for d in jt._iter_directive_items(directives):
        content = jt._get_field(d, "content")
        if isinstance(content, str) and content.strip():
            try:
                raw.append(json.loads(content))
            except ValueError:
                pass

    tmpl = jt.load_job_template(BOARD, fetcher=lambda: raw)
    assert tmpl is not None and tmpl.workspace_kind == "worktree"


def test_directive_list_payload_is_flattened():
    # A single directive may carry a JSON array of templates.
    payload = [_assignee_tmpl("builder"), _board_default()]
    tmpl = jt.load_job_template(BOARD, assignee="builder", fetcher=lambda: payload)
    assert tmpl is not None and tmpl.selector_kind == "assignee"


# ── per-tick cache ──────────────────────────────────────────────────────────
def test_cache_hits_avoid_a_second_fetch(monkeypatch):
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return [_board_default()]

    monkeypatch.setattr(jt, "_default_fetcher", counting_fetch)
    # use_cache=True (default) and no injected fetcher → goes through the cache.
    first = jt.load_job_template(BOARD)
    second = jt.load_job_template(BOARD)
    assert first is not None and second is not None
    assert calls["n"] == 1  # second call served from cache

    jt.clear_cache()
    jt.load_job_template(BOARD)
    assert calls["n"] == 2  # cache cleared → fetched again


def test_use_cache_false_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return [_board_default()]

    monkeypatch.setattr(jt, "_default_fetcher", counting_fetch)
    jt.load_job_template(BOARD, use_cache=False)
    jt.load_job_template(BOARD, use_cache=False)
    assert calls["n"] == 2


# ── default fetcher against a fake Hindsight client (no network) ─────────────
class _FakeDirective:
    def __init__(self, content, tags):
        self.content = content
        self.tags = tags


class _FakeListResponse:
    def __init__(self, items):
        self.items = items


class _FakeDirectivesApi:
    last_call = None

    def __init__(self, directives):
        self._directives = directives

    async def list_directives(self, bank_id, tags, active_only=None):
        type(self).last_call = {"bank_id": bank_id, "tags": tags, "active_only": active_only}
        return _FakeListResponse(self._directives)


class _FakeHindsight:
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.closed = False
        # Verbatim content round-trips — the whole reason we use directives.
        self._directives_api = _FakeDirectivesApi(
            [
                _FakeDirective(json.dumps(_board_default()), list(jt.TEMPLATE_TAGS)),
                _FakeDirective("definitely not json", list(jt.TEMPLATE_TAGS)),  # skipped, not fatal
            ]
        )

    @property
    def directives(self):
        return self._directives_api

    def close(self):
        self.closed = True


def test_default_fetcher_parses_directive_content(monkeypatch):
    import hindsight_client

    monkeypatch.setattr(hindsight_client, "Hindsight", _FakeHindsight)
    raw = jt._default_fetcher()
    assert isinstance(raw, list) and len(raw) == 1
    assert raw[0]["board"] == BOARD
    # Honored the tight timeout + namespaced bank.
    assert _FakeHindsight.last_kwargs["timeout"] == jt._FETCH_TIMEOUT_SECONDS
    assert _FakeHindsight.last_kwargs["base_url"] == jt.HINDSIGHT_BASE_URL
    # Fetched INACTIVE (inert) directives with the namespaced tag filter.
    assert _FakeDirectivesApi.last_call["active_only"] is False
    assert _FakeDirectivesApi.last_call["tags"] == list(jt.TEMPLATE_TAGS)


def test_default_fetcher_close_failure_is_swallowed(monkeypatch):
    import hindsight_client

    class _ExplodingClose(_FakeHindsight):
        def close(self):
            raise RuntimeError("close blew up")

    monkeypatch.setattr(hindsight_client, "Hindsight", _ExplodingClose)
    # _safe_close swallows the error; fetch still returns the parsed template.
    raw = jt._default_fetcher()
    assert len(raw) == 1


def test_load_job_template_end_to_end_through_default_fetcher(monkeypatch):
    import hindsight_client

    monkeypatch.setattr(hindsight_client, "Hindsight", _FakeHindsight)
    tmpl = jt.load_job_template(BOARD, assignee="builder", use_cache=False)
    assert tmpl is not None and tmpl.require_refuter is True


def test_has_template_tags_is_strict():
    assert jt._has_template_tags(_FakeDirective("{}", list(jt.TEMPLATE_TAGS))) is True
    # Missing one of the required tags → not a template.
    assert jt._has_template_tags(_FakeDirective("{}", ["project:olympus-dispatcher"])) is False
    assert jt._has_template_tags(_FakeDirective("{}", [])) is False
    assert jt._has_template_tags(_FakeDirective("{}", None)) is False


def test_default_fetcher_skips_untagged_json_directive(monkeypatch):
    import hindsight_client

    class _MixedHindsight(_FakeHindsight):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._directives_api = _FakeDirectivesApi(
                [
                    _FakeDirective(json.dumps(_board_default()), list(jt.TEMPLATE_TAGS)),
                    # Valid JSON, but NOT namespaced as a job-template → must be ignored,
                    # even though Hindsight's loose tag filter returned it.
                    _FakeDirective(json.dumps(_assignee_tmpl("builder")), ["some:other-tag"]),
                ]
            )

    monkeypatch.setattr(hindsight_client, "Hindsight", _MixedHindsight)
    raw = jt._default_fetcher()
    assert len(raw) == 1
    assert raw[0]["selector"]["kind"] == "board-default"
