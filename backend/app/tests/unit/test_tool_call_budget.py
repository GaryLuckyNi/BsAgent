from concurrent.futures import ThreadPoolExecutor


def test_parallel_search_reservations_do_not_exceed_limit():
    from app.tools.call_budget import SearchCallBudget

    budget = SearchCallBudget(limit=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(
            pool.map(lambda i: budget.reserve("run", f"call-{i}", existing=()), range(8))
        )
    assert sum(allowed) == 2


def test_budget_restores_existing_calls_and_isolates_runs():
    from app.tools.call_budget import SearchCallBudget

    budget = SearchCallBudget(limit=1)
    assert not budget.reserve("run", "new", existing=("old",))
    assert budget.reserve("other", "new", existing=())
    assert budget.reserve("run", "old", existing=("old",))
