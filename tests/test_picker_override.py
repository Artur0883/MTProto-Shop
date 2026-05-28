from tls_domains import TLSDomainPicker


def test_active_primary_defaults_to_none():
    p = TLSDomainPicker()
    assert p.active_primary is None


def test_set_and_clear_active_primary():
    p = TLSDomainPicker()
    p.set_active_primary("b.com")
    assert p.active_primary == "b.com"
    p.clear_active_primary()
    assert p.active_primary is None
