from app.market_data.provider import FixtureMarketDataProvider


def test_fixture_provider_resolves_a_share_company_alias():
    provider = FixtureMarketDataProvider()

    result = provider.resolve_company("贵州茅台")

    assert result.symbol == "600519.SH"
    assert result.name == "贵州茅台"
    assert result.exchange == "SH"
    assert result.source_provider == "fixture"


def test_fixture_provider_returns_financial_metrics_with_source_metadata():
    provider = FixtureMarketDataProvider()

    result = provider.get_financial_metrics(symbol="600519.SH", period="2024")

    assert result.symbol == "600519.SH"
    assert result.period == "2024"
    assert result.metrics["revenue"] == 150560000000
    assert result.metrics["net_profit_parent"] == 74734000000
    assert result.source_provider == "fixture"
    assert result.as_of_date == "2025-04-03"


def test_fixture_provider_returns_empty_risk_events_for_unknown_symbol():
    provider = FixtureMarketDataProvider()

    result = provider.get_risk_events(symbol="000000.SZ", date_range="6m")

    assert result.symbol == "000000.SZ"
    assert result.events == []
    assert result.warnings == ["fixture_symbol_not_found"]
