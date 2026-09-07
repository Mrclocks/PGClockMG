"""Gate rules for wizard goStep — must not block the result screen after migrate."""


def should_revalidate_before_step(current_step: int, next_step: int, *, force: bool = False) -> bool:
    """Mirror app.js goStep gating.

    Re-validate only when moving forward onto the run step (5).
    Never re-validate when opening the result step (6): post-migrate
    .env/DB changes often fail validate-migration and used to trap the UI
    at 100% on the run screen.
    """
    if force:
        return False
    if not (next_step > current_step):
        return False
    if next_step >= 6:
        return False
    return next_step == 5


def test_result_step_never_revalidates():
    assert should_revalidate_before_step(5, 6) is False
    assert should_revalidate_before_step(5, 6, force=True) is False
    assert should_revalidate_before_step(4, 6) is False


def test_entering_run_step_revalidates():
    assert should_revalidate_before_step(4, 5) is True


def test_force_skips_all_gates():
    assert should_revalidate_before_step(4, 5, force=True) is False
    assert should_revalidate_before_step(1, 5, force=True) is False


def test_backward_or_same_step_no_revalidate():
    assert should_revalidate_before_step(5, 5) is False
    assert should_revalidate_before_step(6, 5) is False


if __name__ == "__main__":
    test_result_step_never_revalidates()
    test_entering_run_step_revalidates()
    test_force_skips_all_gates()
    test_backward_or_same_step_no_revalidate()
    print("OK: wizard result step gate")
