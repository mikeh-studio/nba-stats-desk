"""The evaluation server must evaluate the intended checkout, never a silent default."""

import pytest
from scripts.ask_eval.environment import select_code_root


def harness(root):
    module = root / "scripts" / "ask_eval" / "server.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    return module


def consumer_of(shared, consumer):
    (consumer / "scripts").mkdir(parents=True)
    (consumer / "scripts" / "ask_eval").symlink_to(
        shared / "scripts" / "ask_eval", target_is_directory=True
    )
    return consumer / "scripts" / "ask_eval" / "server.py"


def test_own_checkout_defaults_to_itself(tmp_path):
    repo = tmp_path / "repo"
    module = harness(repo)
    assert select_code_root(module, {}) == (repo.resolve(), repo.resolve())


def test_symlinked_parent_directory_is_still_the_same_checkout(tmp_path):
    repo = tmp_path / "repo"
    harness(repo)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    module = alias / "scripts" / "ask_eval" / "server.py"
    assert select_code_root(module, {}) == (repo.resolve(), repo.resolve())


def test_harness_reached_through_another_checkout_fails_closed(tmp_path):
    shared = tmp_path / "shared"
    harness(shared)
    module = consumer_of(shared, tmp_path / "consumer")
    with pytest.raises(RuntimeError, match="ASK_EVAL_CODE_ROOT"):
        select_code_root(module, {})


def test_explicit_code_root_is_used_as_given(tmp_path):
    shared = tmp_path / "shared"
    harness(shared)
    consumer = tmp_path / "consumer"
    module = consumer_of(shared, consumer)
    runner, code = select_code_root(module, {"ASK_EVAL_CODE_ROOT": str(consumer)})
    assert runner == shared.resolve()
    assert code == consumer
