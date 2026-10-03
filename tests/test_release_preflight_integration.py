import pytest

from booking_bot.deployment.runtime import execute
from booking_bot.version import __version__

pytestmark = pytest.mark.integration


async def test_release_preflight_reads_real_database_revision():
    result = await execute("release-check")
    assert result["version"] == __version__
    assert result["heads"] == result["current"] == ["c75a01d29f10"]
    assert result["forward"]
