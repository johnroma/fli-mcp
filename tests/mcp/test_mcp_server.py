"""Test MCP server functionality."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from fli.mcp.server import (
    DateSearchParams,
    FlightSearchParams,
)
from fli.mcp.server import (
    _search_dates_from_params as search_dates_fn,
)
from fli.mcp.server import (
    _search_flights_from_params as search_flights_fn,
)
from fli.models import (
    Airline,
    Airport,
    DateSearchFilters,
    FlightLeg,
    FlightResult,
    FlightSearchFilters,
)
from fli.search.dates import DatePrice


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail before any curl transport call; these are offline handler contracts."""

    def unexpected_request(*args, **kwargs):
        pytest.fail("MCP handler test attempted an unmocked HTTP request")

    monkeypatch.setattr("curl_cffi.requests.Session.request", unexpected_request)


@pytest.fixture
def flight_search():
    """Mock only the backend search, leaving filter building/serialization real."""
    with patch("fli.mcp.server.SearchFlights.search", autospec=True) as search:
        yield search


@pytest.fixture
def date_search():
    """Provide explicit synthetic results, not evidence of live Google behavior."""
    with patch("fli.mcp.server.SearchDates.search", autospec=True) as search:
        yield search


def mock_flight(origin: str, destination: str, travel_date: str) -> FlightResult:
    """Build an explicitly synthetic, typed flight fixture."""
    departure = datetime.fromisoformat(travel_date).replace(hour=10)
    return FlightResult(
        legs=[
            FlightLeg(
                airline=Airline.AA,
                flight_number="100",
                departure_airport=Airport[origin],
                arrival_airport=Airport[destination],
                departure_datetime=departure,
                arrival_datetime=departure + timedelta(hours=6),
                duration=360,
            )
        ],
        price=350.0,
        currency="USD",
        duration=360,
        stops=0,
    )


def assert_search_call(search, params, filter_type, trip_type, travel_dates):
    """Check the real handler's parameter mapping at the mocked search boundary."""
    search.assert_called_once()
    _, filters = search.call_args.args
    assert isinstance(filters, filter_type)
    assert search.call_args.kwargs == {"currency": None, "language": None, "country": None}
    assert filters.trip_type.name == trip_type
    assert filters.seat_type.name == params.cabin_class
    assert (
        filters.stops.name
        == {
            "ANY": "ANY",
            "NON_STOP": "NON_STOP",
            "ONE_STOP": "ONE_STOP_OR_FEWER",
        }[params.max_stops]
    )
    assert filters.passenger_info.adults == params.passengers
    assert filters.airlines == (
        [Airline[code] for code in params.airlines] if params.airlines else None
    )
    assert [segment.travel_date for segment in filters.flight_segments] == travel_dates
    for index, segment in enumerate(filters.flight_segments):
        origin, destination = (params.origin, params.destination)
        if index == 1:
            origin, destination = destination, origin
        assert segment.departure_airport == [[Airport[origin], 0]]
        assert segment.arrival_airport == [[Airport[destination], 0]]
    restrictions = filters.flight_segments[0].time_restrictions
    if params.departure_window:
        start, end = map(int, params.departure_window.split("-"))
        assert restrictions.earliest_departure == start
        assert restrictions.latest_departure == end
    else:
        assert restrictions is None
    if isinstance(filters, FlightSearchFilters):
        assert filters.sort_by.name == params.sort_by
        if len(filters.flight_segments) == 2:
            assert filters.flight_segments[1].time_restrictions is None
    else:
        assert filters.from_date == params.start_date
        assert filters.to_date == params.end_date
        assert filters.duration == (params.trip_duration if params.is_round_trip else None)


def get_future_date(days: int = 30) -> str:
    """Generate a future date string in YYYY-MM-DD format."""
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


class TestMCPServer:
    """Test suite for MCP server tools."""

    def test_search_flights_one_way(self, flight_search):
        """Test the one-way handler with an explicit mocked search result."""
        params = FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=get_future_date(30),
            cabin_class="ECONOMY",
            max_stops="ANY",
            sort_by="CHEAPEST",
        )
        flight_search.return_value = [mock_flight("JFK", "LHR", params.departure_date)]

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["trip_type"] == "ONE_WAY"
        assert result["count"] == 1
        assert isinstance(result["flights"], list)
        assert result["flights"][0]["price"] == 350.0
        assert result["flights"][0]["currency"] == "USD"
        assert len(result["flights"][0]["segments"]) == 1
        assert result["flights"][0]["booking_url"].startswith("https://www.google.com/")
        assert_search_call(
            flight_search, params, FlightSearchFilters, "ONE_WAY", [params.departure_date]
        )

    def test_search_flights_round_trip(self, flight_search):
        """Test round-trip filter mapping and serialization with mocked results."""
        params = FlightSearchParams(
            origin="LAX",
            destination="JFK",
            departure_date=get_future_date(30),
            return_date=get_future_date(37),
            departure_window="8-20",
            airlines=["AA", "DL"],
            cabin_class="BUSINESS",
            max_stops="NON_STOP",
            sort_by="DURATION",
        )
        flight_search.return_value = [
            (
                mock_flight("LAX", "JFK", params.departure_date),
                mock_flight("JFK", "LAX", params.return_date),
            )
        ]

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["trip_type"] == "ROUND_TRIP"
        assert result["count"] == 1
        assert isinstance(result["flights"], list)
        assert result["flights"][0]["price"] == 350.0
        assert result["flights"][0]["currency"] == "USD"
        assert len(result["flights"][0]["segments"]) == 2
        assert result["flights"][0]["booking_url"].startswith("https://www.google.com/")
        assert_search_call(
            flight_search,
            params,
            FlightSearchFilters,
            "ROUND_TRIP",
            [params.departure_date, params.return_date],
        )

    def test_search_dates_one_way(self, date_search):
        """Test one-way date serialization and price sorting without network."""
        start_date = get_future_date(30)
        end_date = get_future_date(60)

        params = DateSearchParams(
            origin="JFK",
            destination="LHR",
            start_date=start_date,
            end_date=end_date,
            is_round_trip=False,
            cabin_class="ECONOMY",
            max_stops="ANY",
            sort_by_price=True,
        )
        date_search.return_value = [
            DatePrice(date=(datetime.fromisoformat(start_date),), price=500, currency="USD"),
            DatePrice(date=(datetime.fromisoformat(end_date),), price=300, currency="USD"),
        ]

        result = search_dates_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["trip_type"] == "ONE_WAY"
        assert result["count"] == 2
        assert result["date_range"] == f"{start_date} to {end_date}"
        assert result["duration"] is None
        assert isinstance(result["dates"], list)
        assert [date["price"] for date in result["dates"]] == [300, 500]
        assert [date["date"] for date in result["dates"]] == [end_date, start_date]
        assert all(date["return_date"] is None for date in result["dates"])
        assert all(date["currency"] == "USD" for date in result["dates"])
        assert all(
            date["booking_url"].startswith("https://www.google.com/") for date in result["dates"]
        )
        assert_search_call(date_search, params, DateSearchFilters, "ONE_WAY", [start_date])

    def test_search_dates_round_trip(self, date_search):
        """Test the round-trip date handler using a synthetic DatePrice fixture."""
        start_date = get_future_date(30)
        end_date = get_future_date(60)

        params = DateSearchParams(
            origin="LAX",
            destination="MIA",
            start_date=start_date,
            end_date=end_date,
            trip_duration=7,
            is_round_trip=True,
            airlines=["AA", "B6"],
            cabin_class="PREMIUM_ECONOMY",
            max_stops="ONE_STOP",
            departure_window="6-22",
            sort_by_price=True,
        )
        departure = datetime.fromisoformat(start_date)
        return_date = departure + timedelta(days=7)
        date_search.return_value = [
            DatePrice(date=(departure, return_date), price=400, currency="USD")
        ]

        result = search_dates_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["trip_type"] == "ROUND_TRIP"
        assert result["count"] == 1
        assert result["duration"] == 7
        assert result["date_range"] == f"{start_date} to {end_date}"
        assert isinstance(result["dates"], list)
        assert result["dates"][0]["date"] == start_date
        assert result["dates"][0]["return_date"] == return_date.date().isoformat()
        assert result["dates"][0]["price"] == 400
        assert result["dates"][0]["currency"] == "USD"
        assert result["dates"][0]["booking_url"].startswith("https://www.google.com/")
        assert_search_call(
            date_search,
            params,
            DateSearchFilters,
            "ROUND_TRIP",
            [start_date, return_date.date().isoformat()],
        )

    def test_invalid_airport_code(self):
        """Test error handling for invalid airport code."""
        params = FlightSearchParams(
            origin="INVALID", destination="LHR", departure_date=get_future_date(30)
        )

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "Invalid airport code" in result["error"]
        assert result["flights"] == []

    def test_invalid_departure_window(self):
        """Test error handling for invalid departure window."""
        params = FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=get_future_date(30),
            departure_window="invalid-time",
        )

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "time range" in result["error"].lower()
        assert result["flights"] == []

    def test_invalid_cabin_class(self):
        """Test error handling for invalid cabin class."""
        params = FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=get_future_date(30),
            cabin_class="INVALID_CLASS",
        )

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "cabin_class" in result["error"].lower()
        assert result["flights"] == []

    def test_invalid_max_stops(self):
        """Test error handling for invalid max stops."""
        params = FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=get_future_date(30),
            max_stops="INVALID_STOPS",
        )

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "max_stops" in result["error"].lower()
        assert result["flights"] == []

    def test_invalid_airline_code(self):
        """Test error handling for invalid airline code."""
        params = FlightSearchParams(
            origin="JFK",
            destination="LHR",
            departure_date=get_future_date(30),
            airlines=["INVALID_AIRLINE"],
        )

        result = search_flights_fn(params)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "airline" in result["error"].lower()
        assert result["flights"] == []

    def test_flight_search_params_validation(self):
        """Test FlightSearchParams validation."""
        future_date = get_future_date(30)
        params = FlightSearchParams(origin="JFK", destination="LHR", departure_date=future_date)
        assert params.origin == "JFK"
        assert params.destination == "LHR"
        assert params.departure_date == future_date
        assert params.cabin_class == "ECONOMY"  # default
        assert params.max_stops == "ANY"  # default
        assert params.sort_by == "CHEAPEST"  # default

    def test_date_search_params_validation(self):
        """Test DateSearchParams validation."""
        start_date = get_future_date(30)
        end_date = get_future_date(60)
        params = DateSearchParams(
            origin="JFK",
            destination="LHR",
            start_date=start_date,
            end_date=end_date,
        )
        assert params.origin == "JFK"
        assert params.destination == "LHR"
        assert params.start_date == start_date
        assert params.end_date == end_date
        assert params.trip_duration == 3  # default
        assert params.is_round_trip is False  # default
        assert params.cabin_class == "ECONOMY"  # default
        assert params.max_stops == "ANY"  # default
        assert params.sort_by_price is False  # default
