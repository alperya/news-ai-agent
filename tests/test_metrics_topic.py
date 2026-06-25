"""Tests for MetricsCollector._topic() thematic classification.

_topic is a staticmethod, so no Instagram/AWS env is needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from metrics_collector import MetricsCollector

topic = MetricsCollector._topic


def test_weather():
    assert topic("⛈️ The Netherlands experienced an extraordinary night of severe storms") == "Weather"
    assert topic("🔥 The KNMI has issued a code orange warning") == "Weather"


def test_transport():
    assert topic("🛫 SCHIPHOL AIRPORT CHAOS: hundreds missed their flights") == "Transport"
    assert topic("🚆 Dutch public transport will come to a complete standstill") == "Transport"


def test_crime_security():
    assert topic("🚨 BREAKING: the suspect arrested after an explosion in Amsterdam") == "Crime/Security"


def test_health():
    assert topic("🇳🇱 For the first time, euthanasia has been granted to ...") == "Health"


def test_events():
    assert topic("📅 THIS WEEK IN THE NETHERLANDS — festival and concert highlights") == "Events"


def test_sports():
    assert topic("Ajax wins the Eredivisie title in a dramatic final match") == "Sports"


def test_society_royal():
    assert topic("The King celebrated Koningsdag with crowds in Amsterdam") == "Society/Royal"


def test_geography_is_not_a_topic():
    # An NL-heavy caption with no theme keyword no longer collapses to "Netherlands"
    assert topic("Amsterdam and Rotterdam residents shared their views") == "Other"


def test_empty():
    assert topic("") == "Other"
