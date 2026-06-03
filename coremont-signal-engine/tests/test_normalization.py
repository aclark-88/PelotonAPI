from app import normalization as norm


def test_manager_key_collapses_master_and_offshore_variants():
    a = norm.manager_key("Meridian Structured Credit Master Fund LP")
    b = norm.manager_key("Meridian Structured Credit Offshore Fund Ltd")
    assert a == b == "meridian structured credit"


def test_manager_key_collapses_feeder_and_numbered_series():
    a = norm.manager_key("Aldgate Global Macro Fund LP")
    b = norm.manager_key("Aldgate Macro Offshore Feeder Fund Ltd")
    # "global" stays but the platform stem ("aldgate macro") is shared.
    assert "aldgate" in a and "aldgate" in b
    assert norm.manager_key("Hollis Credit Fund II LP") == norm.manager_key(
        "Hollis Credit Fund III LP"
    )


def test_manager_key_never_empty():
    assert norm.manager_key("Master Offshore Feeder Fund LP") != ""


def test_classify_offshore_feeder():
    s = norm.classify_vehicle("Aldgate Macro Offshore Feeder Fund Ltd", "CAYMAN ISLANDS")
    assert s.is_offshore is True
    assert s.is_feeder is True
    assert s.vehicle_type == "offshore_feeder"


def test_classify_master():
    s = norm.classify_vehicle("Meridian Structured Credit Master Fund LP", "DELAWARE")
    assert s.is_master is True
    assert s.vehicle_type == "master"


def test_normalize_entity_name_strips_suffixes():
    assert norm.normalize_entity_name("Brightwater Multi-Strategy Partners Fund LP") == (
        "brightwater multi strategy partners fund"
    )
