"""Fixtures for integration tests -- require a live Istari platform."""

import os

import pytest
from dotenv import load_dotenv

from istari_fluent import IstariPlatform


@pytest.fixture(scope="session")
def platform():
    load_dotenv()
    if not os.getenv("ISTARI_PAT"):
        pytest.skip("ISTARI_PAT not set -- skipping integration tests")
    return IstariPlatform.from_env()
