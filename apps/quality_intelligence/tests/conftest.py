import pytest


@pytest.fixture(autouse=True)
def enable_quality_intelligence(settings):
    settings.QUALITY_INTELLIGENCE_ENABLED = True
