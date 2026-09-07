"""Persistent trip recorder for the Polestar Home Assistant integration.

Consumes existing coordinator updates only.
It performs no additional Polestar/Volvo API requests.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from homeassistant.components import zone
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfEnergyDistance,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import StateType

from .entity import PolestarEntity


_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_PREFIX = "polestar.trip_history"


# ----------------------------------------------------------------------
# USER-TUNABLE SETTINGS
# ----------------------------------------------------------------------

# Trip begins at or above this speed.
START_SPEED_KMH = 3.0

# Vehicle is considered stationary at or below this speed.
STOP_SPEED_KMH = 1.0

# A stop shorter than this remains part of the same trip.
STOP_DEBOUNCE = timedelta(minutes=5)

# Route point thinning.
# A new point is retained when either condition is reached.
ROUTE_POINT_INTERVAL_S = 10.0
ROUTE_POINT_DISTANCE_KM = 0.025  # 25 metres

# Ignore tiny GPS/driveway movements.
MIN_TRIP_DISTANCE_KM = 0.10

# Protect against clearly broken odometer/GPS values.
MAX_VALID_ODOMETER_DELTA_KM = 1500.0
MAX_GPS_STEP_KM = 20.0
MAX_SAMPLE_GAP_S = 300.0

# If HA loses fresh location data during a trip for this long,
# close the trip at the last valid location.
STALE_ACTIVE_TRIP = timedelta(minutes=30)

# Don't start a trip from an hours-old cached location frame.
LOCATION_FRESHNESS = timedelta(minutes=15)

# Independent of Home Assistant Recorder retention.
RETENTION_DAYS = 730
MAX_STORED_TRIPS = 2000

# Persist an unfinished trip every minute so an HA restart doesn't lose it.
ACTIVE_PERSIST_INTERVAL_S = 60.0

# Updates duration/end detection while the car is stationary.
TICK_INTERVAL = timedelta(seconds=30)


CSV_FIELDS = (
    "id",
    "started_at",
    "ended_at",
    "start_zone",
    "end_zone",
    "distance_km",
    "gps_distance_km",
    "duration_min",
    "moving_time_min",
    "average_speed_kmh",
    "average_moving_speed_kmh",
    "mean_reported_speed_kmh",
    "max_speed_kmh",
    "soc_start_pct",
    "soc_end_pct",
    "soc_used_pct",
    "range_start_km",
    "range_end_km",
    "range_change_km",
    "odometer_start_km",
    "odometer_end_km",
    "average_consumption_kwh_per_100km",
    "estimated_energy_kwh",
    "altitude_start_m",
    "altitude_end_m",
    "altitude_min_m",
    "altitude_max_m",
    "ascent_m",
    "descent_m",
    "start_latitude",
    "start_longitude",
    "end_latitude",
    "end_longitude",
    "route_points",
    "end_reason",
    "json_file",
    "gpx_file",
    "geojson_file",
)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result if math.isfinite(result) else None


def _rounded(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None

    return round(value, digits)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _haversine_km(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
) -> float:
    if any(value is None for value in (lat1, lon1, lat2, lon2)):
        return 0.0

    if not -90 <= lat1 <= 90 or not -90 <= lat2 <= 90:
        return 0.0

    if not -180 <= lon1 <= 180 or not -180 <= lon2 <= 180:
        return 0.0

    radius = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlon / 2) ** 2
    )

    return radius * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )


@dataclass(slots=True)
class TripSample:
    ts: datetime

    latitude: float | None
    longitude: float | None

    speed_kmh: float | None
    heading_deg: float | None
    altitude_m: float | None

    soc_pct: float | None
    range_km: float | None

    odometer_km: float | None
    consumption_kwh_100km: float | None

    def route_point(self) -> dict[str, Any]:
        return {
            "t": _iso(self.ts),
            "lat": _rounded(self.latitude, 7),
            "lon": _rounded(self.longitude, 7),
            "speed_kmh": _rounded(self.speed_kmh, 1),
            "heading_deg": _rounded(self.heading_deg, 1),
            "altitude_m": _rounded(self.altitude_m, 1),
            "soc_pct": _rounded(self.soc_pct, 1),
            "range_km": _rounded(self.range_km, 1),
            "odometer_km": _rounded(self.odometer_km, 3),
            "consumption_kwh_per_100km": _rounded(
                self.consumption_kwh_100km,
                2,
            ),
        }


@dataclass
class TripSession:
    id: str
    started_at: datetime

    soc_start_pct: float | None = None
    range_start_km: float | None = None
    odometer_start_km: float | None = None

    altitude_start_m: float | None = None

    start_latitude: float | None = None
    start_longitude: float | None = None

    latest_soc_pct: float | None = None
    latest_range_km: float | None = None
    latest_odometer_km: float | None = None
    latest_consumption: float | None = None

    latest_altitude_m: float | None = None

    last_latitude: float | None = None
    last_longitude: float | None = None

    last_sample_ts: datetime | None = None
    last_speed_kmh: float | None = None
    last_altitude_m: float | None = None

    gps_distance_km: float = 0.0

    moving_time_s: float = 0.0

    speed_weighted_sum: float = 0.0
    speed_weight_s: float = 0.0

    max_speed_kmh: float = 0.0

    consumption_sum: float = 0.0
    consumption_count: int = 0

    altitude_min_m: float | None = None
    altitude_max_m: float | None = None

    ascent_m: float = 0.0
    descent_m: float = 0.0

    route: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_sample(cls, sample: TripSample) -> "TripSession":
        session = cls(
            id=(
                f"{sample.ts.strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid.uuid4().hex[:8]}"
            ),
            started_at=sample.ts,

            soc_start_pct=sample.soc_pct,
            range_start_km=sample.range_km,
            odometer_start_km=sample.odometer_km,

            altitude_start_m=sample.altitude_m,

            start_latitude=sample.latitude,
            start_longitude=sample.longitude,
        )

        session.add_sample(
            sample,
            force_route=True,
        )

        return session

    def add_sample(
        self,
        sample: TripSample,
        *,
        force_route: bool = False,
    ) -> None:

        if self.last_sample_ts is not None:

            elapsed = (
                sample.ts - self.last_sample_ts
            ).total_seconds()

            if 0 < elapsed <= MAX_SAMPLE_GAP_S:

                previous_speed = self.last_speed_kmh or 0.0
                current_speed = sample.speed_kmh or 0.0

                if max(
                    previous_speed,
                    current_speed,
                ) > STOP_SPEED_KMH:

                    self.moving_time_s += elapsed

                self.speed_weighted_sum += (
                    (previous_speed + current_speed)
                    / 2
                    * elapsed
                )

                self.speed_weight_s += elapsed

            step = _haversine_km(
                self.last_latitude,
                self.last_longitude,
                sample.latitude,
                sample.longitude,
            )

            # 3 m suppresses ordinary GPS jitter.
            if 0.003 <= step <= MAX_GPS_STEP_KM:
                self.gps_distance_km += step

            if (
                self.last_altitude_m is not None
                and sample.altitude_m is not None
            ):

                delta = (
                    sample.altitude_m
                    - self.last_altitude_m
                )

                # Ignore tiny altitude fluctuations.
                if delta >= 3:
                    self.ascent_m += delta

                elif delta <= -3:
                    self.descent_m += -delta

        if sample.speed_kmh is not None:

            self.max_speed_kmh = max(
                self.max_speed_kmh,
                sample.speed_kmh,
            )

        if (
            sample.consumption_kwh_100km is not None
            and 0
            < sample.consumption_kwh_100km
            < 200
        ):

            self.consumption_sum += (
                sample.consumption_kwh_100km
            )

            self.consumption_count += 1

        if sample.altitude_m is not None:

            if self.altitude_min_m is None:
                self.altitude_min_m = sample.altitude_m
            else:
                self.altitude_min_m = min(
                    self.altitude_min_m,
                    sample.altitude_m,
                )

            if self.altitude_max_m is None:
                self.altitude_max_m = sample.altitude_m
            else:
                self.altitude_max_m = max(
                    self.altitude_max_m,
                    sample.altitude_m,
                )

        self.latest_soc_pct = sample.soc_pct
        self.latest_range_km = sample.range_km
        self.latest_odometer_km = sample.odometer_km
        self.latest_consumption = (
            sample.consumption_kwh_100km
        )
        self.latest_altitude_m = sample.altitude_m

        self.last_latitude = sample.latitude
        self.last_longitude = sample.longitude

        add_route_point = (
            force_route
            or not self.route
        )

        if self.route and not add_route_point:

            previous = self.route[-1]

            previous_time = _parse_iso(
                previous.get("t")
            )

            elapsed_route = (
                (sample.ts - previous_time).total_seconds()
                if previous_time
                else ROUTE_POINT_INTERVAL_S
            )

            route_step = _haversine_km(
                _float(previous.get("lat")),
                _float(previous.get("lon")),
                sample.latitude,
                sample.longitude,
            )

            add_route_point = (
                elapsed_route
                >= ROUTE_POINT_INTERVAL_S
                or route_step
                >= ROUTE_POINT_DISTANCE_KM
            )

        if add_route_point:
            self.route.append(
                sample.route_point()
            )

        self.last_sample_ts = sample.ts
        self.last_speed_kmh = sample.speed_kmh
        self.last_altitude_m = sample.altitude_m

    def ensure_final_point(
        self,
        sample: TripSample,
    ) -> None:

        point = sample.route_point()

        if not self.route:
            self.route.append(point)
            return

        previous = self.route[-1]

        if (
            previous.get("t") != point.get("t")
            or previous.get("lat") != point.get("lat")
            or previous.get("lon") != point.get("lon")
        ):
            self.route.append(point)

    def distance_km(self) -> float:

        if (
            self.odometer_start_km is not None
            and self.latest_odometer_km is not None
        ):

            delta = (
                self.latest_odometer_km
                - self.odometer_start_km
            )

            if (
                0.01
                <= delta
                <= MAX_VALID_ODOMETER_DELTA_KM
            ):
                return delta

        return self.gps_distance_km

    def to_store_dict(self) -> dict[str, Any]:

        result = asdict(self)

        result["started_at"] = _iso(
            self.started_at
        )

        result["last_sample_ts"] = _iso(
            self.last_sample_ts
        )

        return result

    @classmethod
    def from_store_dict(
        cls,
        data: dict[str, Any],
    ) -> "TripSession | None":

        try:

            data = dict(data)

            started_at = _parse_iso(
                data.pop(
                    "started_at",
                    None,
                )
            )

            last_sample_ts = _parse_iso(
                data.pop(
                    "last_sample_ts",
                    None,
                )
            )

            if started_at is None:
                return None

            result = cls(
                started_at=started_at,
                **data,
            )

            result.last_sample_ts = (
                last_sample_ts
            )

            return result

        except (TypeError, ValueError):
            return None


class TripRecorder:
    """Persistent trip recorder for one Polestar vehicle."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        entry_id: str,
    ) -> None:

        self.hass = hass
        self.coordinator = coordinator
        self.entry_id = entry_id

        self.vin = (
            coordinator.vehicle.vin
        )

        # Private=True => 0600 HA storage.
        self._store = Store[dict[str, Any]](
            hass,
            _STORAGE_VERSION,
            f"{_STORAGE_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )

        self._trips: list[
            dict[str, Any]
        ] = []

        self._active: (
            TripSession | None
        ) = None

        self._stationary_since: (
            datetime | None
        ) = None

        self._total_trip_count = 0
        self._lifetime_distance_km = 0.0

        self._listeners: list[
            Callable[[], None]
        ] = []

        self._unsub_coordinator = None
        self._unsub_tick = None

        self._lock = asyncio.Lock()

        self._last_fingerprint = None

        self._last_persist_monotonic = 0.0

        self._stopped = False

        # Deliberately NOT in /config/www.
        self._export_dir = Path(
            hass.config.path(
                "polestar_trips",
                self.vin[-6:],
            )
        )

    # ------------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------------

    async def async_start(self) -> None:

        payload = (
            await self._store.async_load()
            or {}
        )

        trips = payload.get("trips")

        if isinstance(trips, list):
            self._trips = [
                trip
                for trip in trips
                if isinstance(trip, dict)
            ]

        self._total_trip_count = int(
            payload.get(
                "total_trip_count",
                len(self._trips),
            )
        )

        self._lifetime_distance_km = float(
            payload.get(
                "lifetime_distance_km",
                sum(
                    _float(
                        trip.get("distance_km")
                    )
                    or 0
                    for trip in self._trips
                ),
            )
        )

        active = payload.get(
            "active"
        )

        if isinstance(active, dict):

            self._active = (
                TripSession.from_store_dict(
                    active.get("session")
                    or {}
                )
            )

            self._stationary_since = (
                _parse_iso(
                    active.get(
                        "stationary_since"
                    )
                )
            )

        self._unsub_coordinator = (
            self.coordinator.async_add_listener(
                self._coordinator_updated
            )
        )

        self._unsub_tick = (
            async_track_time_interval(
                self.hass,
                self._async_tick,
                TICK_INTERVAL,
            )
        )

        # Recover an unfinished pre-restart trip.
        if (
            self._active is not None
            and self._active.last_sample_ts
            is not None
            and datetime.now(UTC)
            - self._active.last_sample_ts
            > STALE_ACTIVE_TRIP
        ):

            await self._async_finalize(
                self._active.last_sample_ts,
                "recovered_stale",
            )

        else:

            await self.async_process_snapshot()

        self._notify()

    async def async_stop(self) -> None:

        self._stopped = True

        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None

        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None

        # Do NOT end a drive merely because HA is restarting.
        # The active trip is restored on startup.
        await self._async_save()

    @classmethod
    async def async_remove_storage(
        cls,
        hass: HomeAssistant,
        entry_id: str,
    ) -> None:

        store = Store[dict[str, Any]](
            hass,
            _STORAGE_VERSION,
            f"{_STORAGE_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )

        await store.async_remove()

    # ------------------------------------------------------------------
    # UPDATE HANDLING
    # ------------------------------------------------------------------

    @callback
    def _coordinator_updated(
        self,
    ) -> None:

        if not self._stopped:

            self.hass.async_create_task(
                self.async_process_snapshot()
            )

    async def _async_tick(
        self,
        _now: datetime,
    ) -> None:

        if self._stopped:
            return

        async with self._lock:

            now = datetime.now(UTC)

            if self._active:

                if (
                    self._stationary_since
                    and now
                    - self._stationary_since
                    >= STOP_DEBOUNCE
                ):

                    await self._async_finalize(
                        self._stationary_since,
                        "parked",
                    )

                    self._notify()
                    return

                if (
                    self._active.last_sample_ts
                    and now
                    - self._active.last_sample_ts
                    >= STALE_ACTIVE_TRIP
                ):

                    await self._async_finalize(
                        self._active.last_sample_ts,
                        "location_stale",
                    )

                    self._notify()
                    return

                await (
                    self._async_maybe_persist_active()
                )

            self._notify()

    async def async_process_snapshot(
        self,
    ) -> None:

        async with self._lock:

            sample = self._snapshot()

            if sample is None:
                return

            fingerprint = (
                int(sample.ts.timestamp()),
                sample.latitude,
                sample.longitude,
                sample.speed_kmh,
                sample.soc_pct,
                sample.range_km,
                sample.odometer_km,
                sample.consumption_kwh_100km,
            )

            if (
                fingerprint
                == self._last_fingerprint
            ):
                return

            self._last_fingerprint = (
                fingerprint
            )

            now = datetime.now(UTC)

            fresh = (
                abs(now - sample.ts)
                <= LOCATION_FRESHNESS
            )

            speed = (
                sample.speed_kmh
                or 0
            )

            # --------------------------
            # START
            # --------------------------

            if self._active is None:

                if (
                    fresh
                    and speed
                    >= START_SPEED_KMH
                ):

                    self._active = (
                        TripSession.from_sample(
                            sample
                        )
                    )

                    self._stationary_since = (
                        None
                    )

                    _LOGGER.info(
                        "Trip started for Polestar …%s",
                        self.vin[-6:],
                    )

                    await self._async_save()

                    self._last_persist_monotonic = (
                        time.monotonic()
                    )

                    self._notify()

                return

            # Never merge an old cached backend position
            # into an active route.
            if not fresh:
                return

            self._active.add_sample(
                sample
            )

            # --------------------------
            # MOVING / STOPPING
            # --------------------------

            if speed > STOP_SPEED_KMH:

                self._stationary_since = (
                    None
                )

            elif (
                self._stationary_since
                is None
            ):

                self._stationary_since = (
                    sample.ts
                )

            if (
                self._stationary_since
                and now
                - self._stationary_since
                >= STOP_DEBOUNCE
            ):

                self._active.ensure_final_point(
                    sample
                )

                await self._async_finalize(
                    self._stationary_since,
                    "parked",
                )

            else:

                await (
                    self._async_maybe_persist_active()
                )

            self._notify()

    # ------------------------------------------------------------------
    # COORDINATOR -> SAMPLE
    # ------------------------------------------------------------------

    def _snapshot(
        self,
    ) -> TripSample | None:

        data = (
            self.coordinator.data
        )

        if (
            data is None
            or data.location is None
        ):
            return None

        location = data.location

        timestamp = getattr(
            location,
            "timestamp",
            None,
        )

        if (
            timestamp is not None
            and getattr(
                timestamp,
                "seconds",
                0,
            )
        ):

            seconds = int(
                timestamp.seconds
            )

            nanos = int(
                getattr(
                    timestamp,
                    "nanos",
                    0,
                )
                or 0
            )

            sample_time = (
                datetime.fromtimestamp(
                    seconds
                    + nanos
                    / 1_000_000_000,
                    UTC,
                )
            )

        else:

            sample_time = (
                datetime.now(UTC)
            )

        now = datetime.now(UTC)

        # Ignore impossible future timestamps.
        if (
            sample_time
            > now
            + timedelta(minutes=5)
        ):
            sample_time = now

        coordinate = getattr(
            location,
            "coordinate",
            None,
        )

        latitude = _float(
            getattr(
                coordinate,
                "latitude",
                None,
            )
            if coordinate
            else None
        )

        longitude = _float(
            getattr(
                coordinate,
                "longitude",
                None,
            )
            if coordinate
            else None
        )

        battery = data.battery
        odometer = data.odometer

        return TripSample(
            ts=sample_time,

            latitude=latitude,
            longitude=longitude,

            speed_kmh=_float(
                getattr(
                    location,
                    "speed",
                    None,
                )
            ),

            heading_deg=_float(
                getattr(
                    location,
                    "heading",
                    None,
                )
            ),

            altitude_m=_float(
                getattr(
                    location,
                    "altitude",
                    None,
                )
            ),

            soc_pct=_float(
                getattr(
                    battery,
                    "charge_level",
                    None,
                )
                if battery
                else None
            ),

            range_km=_float(
                getattr(
                    battery,
                    "range_km",
                    None,
                )
                if battery
                else None
            ),

            odometer_km=_float(
                getattr(
                    odometer,
                    "odometer_km",
                    None,
                )
                if odometer
                else None
            ),

            consumption_kwh_100km=_float(
                getattr(
                    battery,
                    "avg_consumption_auto",
                    None,
                )
                if battery
                else None
            ),
        )

    # ------------------------------------------------------------------
    # COMPLETE TRIP
    # ------------------------------------------------------------------

    async def _async_finalize(
        self,
        ended_at: datetime,
        reason: str,
    ) -> None:

        session = self._active

        if session is None:
            return

        if (
            ended_at
            < session.started_at
        ):

            ended_at = (
                session.last_sample_ts
                or datetime.now(UTC)
            )

        distance = (
            session.distance_km()
        )

        duration_s = max(
            0,
            (
                ended_at
                - session.started_at
            ).total_seconds(),
        )

        # False movement / GPS noise.
        if (
            distance
            < MIN_TRIP_DISTANCE_KM
        ):

            _LOGGER.debug(
                "Discarding short Polestar movement: %.3f km",
                distance,
            )

            self._active = None
            self._stationary_since = None

            await self._async_save()

            return

        average_speed = (
            distance
            / (duration_s / 3600)
            if duration_s > 0
            else None
        )

        average_moving_speed = (
            distance
            / (
                session.moving_time_s
                / 3600
            )
            if session.moving_time_s > 0
            else None
        )

        mean_reported_speed = (
            session.speed_weighted_sum
            / session.speed_weight_s
            if session.speed_weight_s > 0
            else None
        )

        average_consumption = (
            session.consumption_sum
            / session.consumption_count
            if session.consumption_count
            else session.latest_consumption
        )

        # Estimate based on the vehicle's reported
        # kWh/100 km consumption.
        estimated_energy = (
            distance
            * average_consumption
            / 100
            if average_consumption
            is not None
            else None
        )

        soc_change = (
            session.latest_soc_pct
            - session.soc_start_pct
            if (
                session.latest_soc_pct
                is not None
                and session.soc_start_pct
                is not None
            )
            else None
        )

        range_change = (
            session.latest_range_km
            - session.range_start_km
            if (
                session.latest_range_km
                is not None
                and session.range_start_km
                is not None
            )
            else None
        )

        start_zone = self._zone_name(
            session.start_latitude,
            session.start_longitude,
        )

        end_zone = self._zone_name(
            session.last_latitude,
            session.last_longitude,
        )

        relative_base = (
            Path("polestar_trips")
            / self.vin[-6:]
            / session.id
        )

        summary = {
            "schema_version": 1,

            "id": session.id,

            "started_at": _iso(
                session.started_at
            ),

            "ended_at": _iso(
                ended_at
            ),

            "start_zone": start_zone,
            "end_zone": end_zone,

            "distance_km": _rounded(
                distance,
                3,
            ),

            "gps_distance_km": _rounded(
                session.gps_distance_km,
                3,
            ),

            "duration_min": _rounded(
                duration_s / 60,
                1,
            ),

            "moving_time_min": _rounded(
                session.moving_time_s / 60,
                1,
            ),

            "average_speed_kmh": _rounded(
                average_speed,
                1,
            ),

            "average_moving_speed_kmh":
                _rounded(
                    average_moving_speed,
                    1,
                ),

            "mean_reported_speed_kmh":
                _rounded(
                    mean_reported_speed,
                    1,
                ),

            "max_speed_kmh": _rounded(
                session.max_speed_kmh,
                1,
            ),

            "soc_start_pct": _rounded(
                session.soc_start_pct,
                1,
            ),

            "soc_end_pct": _rounded(
                session.latest_soc_pct,
                1,
            ),

            "soc_change_pct": _rounded(
                soc_change,
                1,
            ),

            "soc_used_pct": (
                _rounded(
                    max(
                        0,
                        -soc_change,
                    ),
                    1,
                )
                if soc_change
                is not None
                else None
            ),

            "range_start_km": _rounded(
                session.range_start_km,
                1,
            ),

            "range_end_km": _rounded(
                session.latest_range_km,
                1,
            ),

            "range_change_km":
                _rounded(
                    range_change,
                    1,
                ),

            "odometer_start_km":
                _rounded(
                    session.odometer_start_km,
                    3,
                ),

            "odometer_end_km":
                _rounded(
                    session.latest_odometer_km,
                    3,
                ),

            "average_consumption_kwh_per_100km":
                _rounded(
                    average_consumption,
                    2,
                ),

            "estimated_energy_kwh":
                _rounded(
                    estimated_energy,
                    2,
                ),

            "altitude_start_m":
                _rounded(
                    session.altitude_start_m,
                    1,
                ),

            "altitude_end_m":
                _rounded(
                    session.latest_altitude_m,
                    1,
                ),

            "altitude_min_m":
                _rounded(
                    session.altitude_min_m,
                    1,
                ),

            "altitude_max_m":
                _rounded(
                    session.altitude_max_m,
                    1,
                ),

            "ascent_m": _rounded(
                session.ascent_m,
                1,
            ),

            "descent_m": _rounded(
                session.descent_m,
                1,
            ),

            "start_latitude":
                _rounded(
                    session.start_latitude,
                    7,
                ),

            "start_longitude":
                _rounded(
                    session.start_longitude,
                    7,
                ),

            "end_latitude":
                _rounded(
                    session.last_latitude,
                    7,
                ),

            "end_longitude":
                _rounded(
                    session.last_longitude,
                    7,
                ),

            "route_points":
                len(
                    session.route
                ),

            "end_reason":
                reason,

            "json_file":
                str(
                    relative_base.with_suffix(
                        ".json"
                    )
                ),

            "gpx_file":
                str(
                    relative_base.with_suffix(
                        ".gpx"
                    )
                ),

            "geojson_file":
                str(
                    relative_base.with_suffix(
                        ".geojson"
                    )
                ),
        }

        route = list(
            session.route
        )

        self._trips.append(
            summary
        )

        self._total_trip_count += 1

        self._lifetime_distance_km += (
            distance
        )

        self._active = None
        self._stationary_since = None

        await self._async_prune()
        await self._async_save()

        try:

            await self.hass.async_add_executor_job(
                self._export_trip_sync,
                summary,
                route,
            )

            await self.hass.async_add_executor_job(
                self._write_summary_csv_sync
            )

        except OSError:

            _LOGGER.exception(
                "Failed to export Polestar trip files"
            )

        _LOGGER.info(
            "Polestar trip completed: %.1f km, %.0f min",
            distance,
            duration_s / 60,
        )

    # ------------------------------------------------------------------
    # STORAGE
    # ------------------------------------------------------------------

    async def _async_maybe_persist_active(
        self,
    ) -> None:

        now = time.monotonic()

        if (
            now
            - self._last_persist_monotonic
            >= ACTIVE_PERSIST_INTERVAL_S
        ):

            await self._async_save()

            self._last_persist_monotonic = (
                now
            )

    async def _async_save(
        self,
    ) -> None:

        active = None

        if self._active:

            active = {
                "session":
                    self._active.to_store_dict(),

                "stationary_since":
                    _iso(
                        self._stationary_since
                    ),
            }

        await self._store.async_save(
            {
                "trips":
                    self._trips,

                "active":
                    active,

                "total_trip_count":
                    self._total_trip_count,

                "lifetime_distance_km":
                    self._lifetime_distance_km,
            }
        )

    async def _async_prune(
        self,
    ) -> None:

        cutoff = (
            datetime.now(UTC)
            - timedelta(
                days=RETENTION_DAYS
            )
        )

        keep = []
        remove = []

        for trip in self._trips:

            ended = _parse_iso(
                trip.get("ended_at")
            )

            if (
                ended
                and ended < cutoff
            ):
                remove.append(
                    trip
                )
            else:
                keep.append(
                    trip
                )

        if (
            len(keep)
            > MAX_STORED_TRIPS
        ):

            excess = (
                len(keep)
                - MAX_STORED_TRIPS
            )

            remove.extend(
                keep[:excess]
            )

            keep = keep[excess:]

        self._trips = keep

        if remove:

            await self.hass.async_add_executor_job(
                self._delete_trip_files_sync,
                remove,
            )

    # ------------------------------------------------------------------
    # HOME ASSISTANT ZONES
    # ------------------------------------------------------------------

    def _zone_name(
        self,
        latitude: float | None,
        longitude: float | None,
    ) -> str | None:

        if (
            latitude is None
            or longitude is None
        ):
            return None

        try:

            state = (
                zone.async_active_zone(
                    self.hass,
                    latitude,
                    longitude,
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

        if state is None:
            return None

        return state.name

    # ------------------------------------------------------------------
    # FILE EXPORT
    # ------------------------------------------------------------------

    def _ensure_export_dir_sync(
        self,
    ) -> None:

        self._export_dir.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )

        try:
            os.chmod(
                self._export_dir,
                0o700,
            )
        except OSError:
            pass

    @staticmethod
    def _atomic_write(
        path: Path,
        contents: str,
    ) -> None:

        temp = path.with_name(
            f".{path.name}.tmp"
        )

        with temp.open(
            "w",
            encoding="utf-8",
        ) as handle:

            handle.write(
                contents
            )

            handle.flush()

            os.fsync(
                handle.fileno()
            )

        os.chmod(
            temp,
            0o600,
        )

        os.replace(
            temp,
            path,
        )

    def _export_trip_sync(
        self,
        summary: dict[str, Any],
        route: list[dict[str, Any]],
    ) -> None:

        self._ensure_export_dir_sync()

        trip_id = summary["id"]

        # --------------------------
        # JSON
        # --------------------------

        self._atomic_write(
            self._export_dir
            / f"{trip_id}.json",

            json.dumps(
                {
                    "summary":
                        summary,

                    "route":
                        route,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )

        # --------------------------
        # GEOJSON
        # --------------------------

        coordinates = []

        for point in route:

            if (
                point.get("lat")
                is None
                or point.get("lon")
                is None
            ):
                continue

            coordinates.append(
                [
                    point["lon"],
                    point["lat"],
                    point.get(
                        "altitude_m"
                    )
                    or 0,
                ]
            )

        geojson = {
            "type":
                "FeatureCollection",

            "features": [
                {
                    "type":
                        "Feature",

                    "properties": {
                        key: value
                        for key, value
                        in summary.items()
                        if key not in {
                            "json_file",
                            "gpx_file",
                            "geojson_file",
                        }
                    },

                    "geometry": {
                        "type":
                            "LineString",

                        "coordinates":
                            coordinates,
                    },
                }
            ],
        }

        self._atomic_write(
            self._export_dir
            / f"{trip_id}.geojson",

            json.dumps(
                geojson,
                indent=2,
                ensure_ascii=False,
            ),
        )

        # --------------------------
        # GPX
        # --------------------------

        self._atomic_write(
            self._export_dir
            / f"{trip_id}.gpx",

            self._gpx(
                summary,
                route,
            ),
        )

    def _gpx(
        self,
        summary: dict[str, Any],
        route: list[dict[str, Any]],
    ) -> str:

        route_name = escape(
            f"{summary.get('start_zone') or 'Start'}"
            " → "
            f"{summary.get('end_zone') or 'End'}"
        )

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',

            '<gpx version="1.1" '
            'creator="Home Assistant Polestar Trip Recorder" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:ps="https://local.invalid/polestar-trip-recorder/1">',

            "  <trk>",

            f"    <name>{route_name}</name>",

            "    <trkseg>",
        ]

        for point in route:

            lat = point.get("lat")
            lon = point.get("lon")

            if (
                lat is None
                or lon is None
            ):
                continue

            lines.append(
                f'      <trkpt lat="{lat}" lon="{lon}">'
            )

            if (
                point.get("altitude_m")
                is not None
            ):

                lines.append(
                    f"        <ele>"
                    f"{point['altitude_m']}"
                    f"</ele>"
                )

            if point.get("t"):

                lines.append(
                    f"        <time>"
                    f"{point['t']}"
                    f"</time>"
                )

            lines.append(
                "        <extensions>"
            )

            for tag, key in (
                (
                    "speed_kmh",
                    "speed_kmh",
                ),
                (
                    "heading_deg",
                    "heading_deg",
                ),
                (
                    "soc_pct",
                    "soc_pct",
                ),
                (
                    "range_km",
                    "range_km",
                ),
                (
                    "odometer_km",
                    "odometer_km",
                ),
                (
                    "consumption_kwh_per_100km",
                    "consumption_kwh_per_100km",
                ),
            ):

                value = point.get(
                    key
                )

                if value is not None:

                    lines.append(
                        f"          "
                        f"<ps:{tag}>"
                        f"{value}"
                        f"</ps:{tag}>"
                    )

            lines.append(
                "        </extensions>"
            )

            lines.append(
                "      </trkpt>"
            )

        lines.extend(
            [
                "    </trkseg>",
                "  </trk>",
                "</gpx>",
                "",
            ]
        )

        return "\n".join(
            lines
        )

    def _write_summary_csv_sync(
        self,
    ) -> None:

        self._ensure_export_dir_sync()

        path = (
            self._export_dir
            / "trips.csv"
        )

        temp = (
            self._export_dir
            / ".trips.csv.tmp"
        )

        with temp.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:

            writer = (
                csv.DictWriter(
                    handle,
                    fieldnames=CSV_FIELDS,
                    extrasaction="ignore",
                )
            )

            writer.writeheader()

            for trip in self._trips:
                writer.writerow(
                    trip
                )

        os.chmod(
            temp,
            0o600,
        )

        os.replace(
            temp,
            path,
        )

    def _delete_trip_files_sync(
        self,
        trips: list[dict[str, Any]],
    ) -> None:

        for trip in trips:

            trip_id = trip.get(
                "id"
            )

            if not isinstance(
                trip_id,
                str,
            ):
                continue

            for suffix in (
                ".json",
                ".gpx",
                ".geojson",
            ):

                try:

                    (
                        self._export_dir
                        / f"{trip_id}{suffix}"
                    ).unlink(
                        missing_ok=True
                    )

                except OSError:

                    _LOGGER.warning(
                        "Could not remove old trip file %s%s",
                        trip_id,
                        suffix,
                    )

    # ------------------------------------------------------------------
    # DASHBOARD DATA
    # ------------------------------------------------------------------

    @property
    def status(
        self,
    ) -> str:

        if self._active is None:
            return "parked"

        if (
            self._stationary_since
            is not None
        ):
            return "stopping"

        return "driving"

    @property
    def current_distance_km(
        self,
    ) -> float | None:

        if self._active is None:
            return None

        return _rounded(
            self._active.distance_km(),
            3,
        )

    @property
    def current_duration_min(
        self,
    ) -> float | None:

        if self._active is None:
            return None

        end = (
            self._stationary_since
            or datetime.now(UTC)
        )

        return _rounded(
            max(
                0,
                (
                    end
                    - self._active.started_at
                ).total_seconds()
                / 60,
            ),
            1,
        )

    @property
    def current_average_speed_kmh(
        self,
    ) -> float | None:

        distance = (
            self.current_distance_km
        )

        duration = (
            self.current_duration_min
        )

        if (
            distance is None
            or not duration
        ):
            return None

        return _rounded(
            distance
            / (duration / 60),
            1,
        )

    @property
    def last_trip(
        self,
    ) -> dict[str, Any] | None:

        if not self._trips:
            return None

        return self._trips[-1]

    @property
    def total_trip_count(
        self,
    ) -> int:

        return self._total_trip_count

    @property
    def lifetime_distance_km(
        self,
    ) -> float:

        return (
            _rounded(
                self._lifetime_distance_km,
                3,
            )
            or 0
        )

    def _local_timezone(
        self,
    ):

        try:
            return ZoneInfo(
                self.hass.config.time_zone
            )
        except Exception:
            return UTC

    def _today_trips(
        self,
    ) -> list[dict[str, Any]]:

        timezone = (
            self._local_timezone()
        )

        today = (
            datetime.now(
                timezone
            ).date()
        )

        result = []

        for trip in self._trips:

            started = _parse_iso(
                trip.get(
                    "started_at"
                )
            )

            if (
                started
                and started
                .astimezone(timezone)
                .date()
                == today
            ):

                result.append(
                    trip
                )

        return result

    def _active_started_today(
        self,
    ) -> bool:

        if self._active is None:
            return False

        timezone = (
            self._local_timezone()
        )

        return (
            self._active.started_at
            .astimezone(timezone)
            .date()
            ==
            datetime.now(
                timezone
            ).date()
        )

    @property
    def trips_today(
        self,
    ) -> int:

        count = len(
            self._today_trips()
        )

        if (
            self._active_started_today()
        ):
            count += 1

        return count

    @property
    def distance_today_km(
        self,
    ) -> float:

        distance = sum(
            _float(
                trip.get(
                    "distance_km"
                )
            )
            or 0
            for trip
            in self._today_trips()
        )

        if (
            self._active_started_today()
            and self._active
        ):

            distance += (
                self._active.distance_km()
            )

        return (
            _rounded(
                distance,
                3,
            )
            or 0
        )

    @property
    def driving_time_today_min(
        self,
    ) -> float:

        duration = sum(
            _float(
                trip.get(
                    "duration_min"
                )
            )
            or 0
            for trip
            in self._today_trips()
        )

        if (
            self._active_started_today()
        ):

            duration += (
                self.current_duration_min
                or 0
            )

        return (
            _rounded(
                duration,
                1,
            )
            or 0
        )

    def last_trip_attributes(
        self,
    ) -> dict[str, Any]:

        if self.last_trip is None:
            return {}

        return {
            key: value
            for key, value
            in self.last_trip.items()
            if key
            != "schema_version"
        }

    def status_attributes(
        self,
    ) -> dict[str, Any]:

        if self._active is None:

            return {
                "stored_trips":
                    len(
                        self._trips
                    ),

                "retention_days":
                    RETENTION_DAYS,

                "export_directory":
                    str(
                        Path(
                            "polestar_trips"
                        )
                        / self.vin[-6:]
                    ),
            }

        return {
            "trip_id":
                self._active.id,

            "started_at":
                _iso(
                    self._active.started_at
                ),

            "stationary_since":
                _iso(
                    self._stationary_since
                ),

            "route_points":
                len(
                    self._active.route
                ),

            "soc_start_pct":
                self._active.soc_start_pct,

            "soc_current_pct":
                self._active.latest_soc_pct,

            "range_start_km":
                self._active.range_start_km,

            "range_current_km":
                self._active.latest_range_km,

            "max_speed_kmh":
                _rounded(
                    self._active.max_speed_kmh,
                    1,
                ),
        }

    # ------------------------------------------------------------------
    # SENSOR LISTENERS
    # ------------------------------------------------------------------

    def async_add_listener(
        self,
        listener: Callable[[], None],
    ) -> Callable[[], None]:

        self._listeners.append(
            listener
        )

        @callback
        def unsubscribe() -> None:

            if (
                listener
                in self._listeners
            ):

                self._listeners.remove(
                    listener
                )

        return unsubscribe

    @callback
    def _notify(
        self,
    ) -> None:

        for listener in tuple(
            self._listeners
        ):

            listener()


# ======================================================================
# HOME ASSISTANT SENSOR ENTITIES
# ======================================================================

@dataclass(
    frozen=True,
    kw_only=True,
)
class TripSensorDescription(
    SensorEntityDescription
):

    value_fn: Callable[
        [TripRecorder],
        StateType | None,
    ]

    attrs_fn: (
        Callable[
            [TripRecorder],
            dict[str, Any],
        ]
        | None
    ) = None


TRIP_SENSORS = (

    TripSensorDescription(
        key="trip_status",
        name="Trip recorder status",
        icon="mdi:map-marker-path",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "parked",
            "driving",
            "stopping",
        ],
        value_fn=lambda recorder:
            recorder.status,
        attrs_fn=lambda recorder:
            recorder.status_attributes(),
    ),

    TripSensorDescription(
        key="current_trip_distance",
        name="Current trip distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfLength.KILOMETERS,
        value_fn=lambda recorder:
            recorder.current_distance_km,
    ),

    TripSensorDescription(
        key="current_trip_duration",
        name="Current trip duration",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfTime.MINUTES,
        value_fn=lambda recorder:
            recorder.current_duration_min,
    ),

    TripSensorDescription(
        key="current_trip_average_speed",
        name="Current trip average speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda recorder:
            recorder.current_average_speed_kmh,
    ),

    TripSensorDescription(
        key="last_trip",
        name="Last trip",
        icon="mdi:map-clock",
        value_fn=lambda recorder:
            (
                recorder.last_trip.get("id")
                if recorder.last_trip
                else None
            ),
        attrs_fn=lambda recorder:
            recorder.last_trip_attributes(),
    ),

    TripSensorDescription(
        key="last_trip_distance",
        name="Last trip distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfLength.KILOMETERS,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "distance_km"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_duration",
        name="Last trip duration",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfTime.MINUTES,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "duration_min"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_average_speed",
        name="Last trip average speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "average_speed_kmh"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_average_moving_speed",
        name="Last trip average moving speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "average_moving_speed_kmh"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_max_speed",
        name="Last trip maximum speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "max_speed_kmh"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_soc_used",
        name="Last trip SOC used",
        icon="mdi:battery-minus",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "soc_used_pct"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_consumption",
        name="Last trip average consumption",
        device_class=
            SensorDeviceClass.ENERGY_DISTANCE,
        state_class=
            SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfEnergyDistance
            .KILO_WATT_HOUR_PER_100_KM,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "average_consumption_kwh_per_100km"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="last_trip_energy",
        name="Last trip estimated energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=
            SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda recorder:
            (
                recorder.last_trip.get(
                    "estimated_energy_kwh"
                )
                if recorder.last_trip
                else None
            ),
    ),

    TripSensorDescription(
        key="trips_today",
        name="Trips today",
        icon="mdi:counter",
        value_fn=lambda recorder:
            recorder.trips_today,
    ),

    TripSensorDescription(
        key="distance_today",
        name="Distance today",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=
            SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfLength.KILOMETERS,
        value_fn=lambda recorder:
            recorder.distance_today_km,
    ),

    TripSensorDescription(
        key="driving_time_today",
        name="Driving time today",
        device_class=SensorDeviceClass.DURATION,
        state_class=
            SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=
            UnitOfTime.MINUTES,
        value_fn=lambda recorder:
            recorder.driving_time_today_min,
    ),

    TripSensorDescription(
        key="recorded_trip_count",
        name="Recorded trip count",
        icon="mdi:counter",
        state_class=
            SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda recorder:
            recorder.total_trip_count,
    ),

    TripSensorDescription(
        key="recorded_distance_total",
        name="Recorded distance total",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=
            SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=
            UnitOfLength.KILOMETERS,
        value_fn=lambda recorder:
            recorder.lifetime_distance_km,
    ),
)


class PolestarTripSensor(
    PolestarEntity,
    SensorEntity,
):
    """Sensor backed by the persistent trip recorder."""

    entity_description: TripSensorDescription

    def __init__(
        self,
        recorder: TripRecorder,
        description: TripSensorDescription,
    ) -> None:

        super().__init__(
            recorder.coordinator
        )

        self.recorder = recorder

        self.entity_description = (
            description
        )

        # triprec_ avoids collisions with any
        # future official integration entities.
        self._attr_unique_id = (
            f"{self._vehicle.vin}_"
            f"triprec_"
            f"{description.key}"
        )

    async def async_added_to_hass(
        self,
    ) -> None:

        await super().async_added_to_hass()

        self.async_on_remove(
            self.recorder.async_add_listener(
                self.async_write_ha_state
            )
        )

    @property
    def available(
        self,
    ) -> bool:

        # Historical trip information remains
        # available even while the vehicle/cloud
        # connection is unavailable.
        return True

    @property
    def native_value(
        self,
    ) -> StateType | None:

        return (
            self.entity_description
            .value_fn(
                self.recorder
            )
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:

        if (
            self.entity_description
            .attrs_fn
            is None
        ):
            return {}

        return (
            self.entity_description
            .attrs_fn(
                self.recorder
            )
        )


def create_trip_sensor_entities(
    recorder: TripRecorder,
) -> list[PolestarTripSensor]:

    return [
        PolestarTripSensor(
            recorder,
            description,
        )
        for description
        in TRIP_SENSORS
    ]