import pytest
from app.agent.formulas import (
    FormulaError,
    compile_formula_sql,
    evaluate_formula,
    extract_formula_variables,
    formula_metadata_cache,
)


def test_cached_formula_never_caches_values_or_mutable_variable_sets():
    with formula_metadata_cache(True):
        assert evaluate_formula("pts + 2 * ast", {"pts": 10, "ast": 2}) == 14
        assert evaluate_formula("pts + 2 * ast", {"pts": 20, "ast": 4}) == 28
        first = extract_formula_variables("pts + ast")
        first.add("invented")
        assert extract_formula_variables("pts + ast") == {"pts", "ast"}
        assert compile_formula_sql("pts", {"pts": "a"}) != compile_formula_sql(
            "pts", {"pts": "b"}
        )
        with pytest.raises(FormulaError):
            evaluate_formula('__import__("os")', {})


def test_request_formula_flag_is_independent_and_resets():
    from dataclasses import replace

    from app.agent.execution import request_policy
    from app.agent.formulas import FORMULA_CACHE
    from tests.test_semantic_migration import settings

    assert not FORMULA_CACHE.get()
    with request_policy(replace(settings(), ask_formula_cache_enabled=True), "openai"):
        assert FORMULA_CACHE.get()
    assert not FORMULA_CACHE.get()
