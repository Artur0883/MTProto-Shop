from reconcile_logic import diff_users, present_usernames


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


def test_present_usernames_users_list():
    resp = {"users": [{"username": "tg_1"}, {"username": "shop_bootstrap"}]}
    assert present_usernames(resp) == {"tg_1", "shop_bootstrap"}


def test_present_usernames_data_list():
    resp = {"data": [{"username": "tg_1"}, {"name": "tg_2"}]}
    assert present_usernames(resp) == {"tg_1", "tg_2"}


def test_present_usernames_bare_list():
    assert present_usernames([{"username": "tg_1"}, "tg_2"]) == {"tg_1", "tg_2"}


def test_present_usernames_dict_keyed_by_username():
    resp = {"tg_1": "aabb", "shop_bootstrap": "ccdd"}
    assert present_usernames(resp) == {"tg_1", "shop_bootstrap"}


def test_present_usernames_empty_and_none():
    assert present_usernames(None) == set()
    assert present_usernames({"users": []}) == set()
