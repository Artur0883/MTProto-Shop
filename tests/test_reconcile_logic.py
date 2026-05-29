from reconcile_logic import diff_users


def test_add_missing_and_remove_stale():
    desired = {"tg_1", "tg_2"}
    present = {"tg_2", "tg_3"}
    protected = {"shop_bootstrap"}
    to_add, to_remove = diff_users(desired, present, protected)
    assert to_add == {"tg_1"}
    assert to_remove == {"tg_3"}


def test_protected_never_removed():
    to_add, to_remove = diff_users(set(), {"shop_bootstrap"}, {"shop_bootstrap"})
    assert to_remove == set()


def test_already_in_sync():
    to_add, to_remove = diff_users({"tg_1"}, {"tg_1", "shop_bootstrap"}, {"shop_bootstrap"})
    assert to_add == set()
    assert to_remove == set()
