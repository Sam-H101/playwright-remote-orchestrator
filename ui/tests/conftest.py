import pytest

from ui import auth


@pytest.fixture(autouse=True)
def _fresh_sign_in_limit():
    """Wrong admin tokens in one test must not count towards another test's guessing limit."""
    auth.reset_failures()
    yield
    auth.reset_failures()
