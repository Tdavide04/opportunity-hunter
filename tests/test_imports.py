def test_public_api_imports_without_pch_or_marketplace_kit():
    import sys

    import opportunity_hunter
    import opportunity_hunter.cli
    import opportunity_hunter.sources.subito

    forbidden = ("pch", "pch_marketplace_kit")
    loaded = [name for name in sys.modules if name.split(".")[0] in forbidden]
    assert not loaded
    assert opportunity_hunter.__version__ == "0.1.0"
