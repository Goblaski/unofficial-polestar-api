"""Persistent trip recorder for the Polestar Home Assistant integration.

v3 is tuned for the behaviour observed on a Polestar 2 where availability
usage_mode provides timely DRIVING/INACTIVE boundaries while odometer and GPS
can be delayed or stale.

Primary boundaries:
- start immediately when usage_mode becomes DRIVING;
- end when usage_mode leaves DRIVING and remains non-driving for a short
  confirmation period. The stored end timestamp remains the first transition.

Odometer is used for total distance, battery for SOC/energy progression, and
location only for genuine backend GPS points. No route interpolation is done.
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

# Primary Polestar 2 trip signal observed in real HA history.
DRIVING_MODE = "driving"
NON_DRIVING_MODES = {"abandoned", "inactive", "engine_off"}
STOP_CONFIRMATION = timedelta(minutes=2)

# Fallback arming/detection for cars/backends where usage_mode is unavailable.
ARM_WINDOW = timedelta(minutes=20)
START_ODOMETER_DELTA_KM = 0.10
START_GPS_DELTA_KM = 0.10
START_SOC_DROP_PCT = 1.0

# Selective refreshes mainly keep availability/battery fresh and cause the
# coordinator to reopen cleanly-ended streams. Odometer is deliberately not
# directly probed because get_odometer itself opens a stream and can block until
# the backend emits a value.
ARMED_PROBE_INTERVAL_S = 60.0
ACTIVE_PROBE_INTERVAL_S = 60.0

# Fallback trip end only when usage_mode is absent/unusable.
FALLBACK_STOP_WITH_PARK_SIGNAL = timedelta(minutes=10)
FALLBACK_HARD_STOP = timedelta(minutes=30)

MIN_TRIP_DISTANCE_KM = 0.10
MAX_VALID_ODOMETER_DELTA_KM = 1500.0
MAX_VALID_GPS_STEP_KM = 20.0
ROUTE_MIN_DISTANCE_KM = 0.015

# Do not present sparse 5-45 minute odometer deltas as an instantaneous/peak
# speed. Only derive a segment speed when source samples are reasonably close.
MAX_SPEED_SEGMENT_INTERVAL_S = 180.0
MAX_DERIVED_SPEED_KMH = 260.0

ACTIVE_PERSIST_INTERVAL_S = 60.0
TICK_INTERVAL = timedelta(seconds=15)
RETENTION_DAYS = 730
MAX_STORED_TRIPS = 2000

CSV_FIELDS = (
    "id",
    "started_at",
    "ended_at",
    "start_reason",
    "end_reason",
    "boundary_confidence",
    "start_zone",
    "end_zone",
    "distance_km",
    "gps_distance_km",
    "duration_min",
    "average_speed_kmh",
    "max_derived_speed_kmh",
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
    "route_points",
    "evidence_points",
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
    return None if value is None else round(value, digits)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _timestamp_to_datetime(timestamp: Any) -> datetime | None:
    seconds = getattr(timestamp, "seconds", 0) if timestamp is not None else 0
    nanos = getattr(timestamp, "nanos", 0) if timestamp is not None else 0
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(int(seconds) + int(nanos or 0) / 1_000_000_000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    return str(value).lower()


def _haversine_km(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
) -> float:
    if any(value is None for value in (lat1, lon1, lat2, lon2)):
        return 0.0
    assert lat1 is not None and lon1 is not None and lat2 is not None and lon2 is not None
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90):
        return 0.0
    if not (-180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        return 0.0
    radius = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass(slots=True)
class TripSample:
    observed_at: datetime
    location_at: datetime | None
    odometer_at: datetime | None
    battery_at: datetime | None
    availability_at: datetime | None
    latitude: float | None
    longitude: float | None
    altitude_m: float | None
    heading_deg: float | None
    reported_speed: float | None
    odometer_km: float | None
    trip_auto_km: float | None
    soc_pct: float | None
    range_km: float | None
    consumption_kwh_100km: float | None
    usage_mode: str | None
    unavailable_reason: str | None
    available: bool | None
    charging: bool | None
    locked: bool | None
    any_door_open: bool | None

    def store_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("observed_at", "location_at", "odometer_at", "battery_at", "availability_at"):
            result[key] = _iso(result[key])
        return result

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> "TripSample | None":
        try:
            values = dict(data)
            observed_at = _parse_iso(values.pop("observed_at", None))
            if observed_at is None:
                return None
            # v2 store compatibility: the new timestamp/status fields may not exist.
            return cls(
                observed_at=observed_at,
                location_at=_parse_iso(values.pop("location_at", None)),
                odometer_at=_parse_iso(values.pop("odometer_at", None)),
                battery_at=_parse_iso(values.pop("battery_at", None)),
                availability_at=_parse_iso(values.pop("availability_at", None)),
                available=values.pop("available", None),
                charging=values.pop("charging", None),
                **values,
            )
        except (TypeError, ValueError):
            return None

    def route_point(self, derived_speed_kmh: float | None = None) -> dict[str, Any]:
        timestamp = self.location_at or self.observed_at
        return {
            "t": _iso(timestamp),
            "observed_at": _iso(self.observed_at),
            "lat": _rounded(self.latitude, 7),
            "lon": _rounded(self.longitude, 7),
            "altitude_m": _rounded(self.altitude_m, 1),
            "heading_deg": _rounded(self.heading_deg, 1),
            "derived_speed_kmh": _rounded(derived_speed_kmh, 1),
            "reported_speed_raw": _rounded(self.reported_speed, 1),
            "odometer_km": _rounded(self.odometer_km, 3),
            "soc_pct": _rounded(self.soc_pct, 1),
            "range_km": _rounded(self.range_km, 1),
            "consumption_kwh_per_100km": _rounded(self.consumption_kwh_100km, 2),
        }

    def evidence_point(self, reason: str) -> dict[str, Any]:
        return {
            "reason": reason,
            "observed_at": _iso(self.observed_at),
            "availability_at": _iso(self.availability_at),
            "battery_at": _iso(self.battery_at),
            "odometer_at": _iso(self.odometer_at),
            "location_at": _iso(self.location_at),
            "usage_mode": self.usage_mode,
            "available": self.available,
            "charging": self.charging,
            "locked": self.locked,
            "any_door_open": self.any_door_open,
            "soc_pct": _rounded(self.soc_pct, 1),
            "range_km": _rounded(self.range_km, 1),
            "odometer_km": _rounded(self.odometer_km, 3),
            "latitude": _rounded(self.latitude, 7),
            "longitude": _rounded(self.longitude, 7),
        }


@dataclass
class TripSession:
    id: str
    started_at: datetime
    start_reason: str
    start_sample: TripSample
    last_sample: TripSample
    odometer_sample: TripSample
    location_sample: TripSample
    gps_distance_km: float = 0.0
    max_derived_speed_kmh: float = 0.0
    last_derived_speed_kmh: float | None = None
    valid_speed_segments: int = 0
    consumption_sum: float = 0.0
    consumption_count: int = 0
    route: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_baseline(cls, baseline: TripSample, start_reason: str) -> "TripSession":
        session = cls(
            id=f"{baseline.observed_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            started_at=baseline.observed_at,
            start_reason=start_reason,
            start_sample=baseline,
            last_sample=baseline,
            odometer_sample=baseline,
            location_sample=baseline,
        )
        session.evidence.append(baseline.evidence_point(f"trip_start:{start_reason}"))
        if baseline.latitude is not None and baseline.longitude is not None:
            session.route.append(baseline.route_point())
        return session

    @staticmethod
    def _odometer_delta(previous: TripSample, current: TripSample) -> float | None:
        if previous.odometer_km is None or current.odometer_km is None:
            return None
        delta = current.odometer_km - previous.odometer_km
        if 0 <= delta <= MAX_VALID_ODOMETER_DELTA_KM:
            return delta
        return None

    def add_sample(self, sample: TripSample) -> bool:
        previous = self.last_sample
        odo_previous = self.odometer_sample
        loc_previous = self.location_sample

        odo_changed = (
            sample.odometer_km != odo_previous.odometer_km
            or sample.odometer_at != odo_previous.odometer_at
        )
        gps_changed = (
            sample.location_at != loc_previous.location_at
            or sample.latitude != loc_previous.latitude
            or sample.longitude != loc_previous.longitude
        )

        moved = False
        derived_speed = None

        if odo_changed:
            odo_delta = self._odometer_delta(odo_previous, sample)
            if odo_delta is not None and odo_delta >= 0.01:
                moved = True
                # Use backend/source timestamps when present. Do not turn a
                # sparse multi-minute/multi-hour delta into a "peak speed".
                start_t = odo_previous.odometer_at or odo_previous.observed_at
                end_t = sample.odometer_at or sample.observed_at
                elapsed_s = max(0.0, (end_t - start_t).total_seconds())
                if 0 < elapsed_s <= MAX_SPEED_SEGMENT_INTERVAL_S:
                    candidate = odo_delta / (elapsed_s / 3600)
                    if 0 <= candidate <= MAX_DERIVED_SPEED_KMH:
                        derived_speed = candidate
                        self.last_derived_speed_kmh = candidate
                        self.max_derived_speed_kmh = max(self.max_derived_speed_kmh, candidate)
                        self.valid_speed_segments += 1
            self.odometer_sample = sample
            self.evidence.append(sample.evidence_point("odometer_changed"))

        if gps_changed:
            gps_delta = _haversine_km(
                loc_previous.latitude,
                loc_previous.longitude,
                sample.latitude,
                sample.longitude,
            )
            if gps_delta <= MAX_VALID_GPS_STEP_KM:
                if gps_delta >= 0.003:
                    self.gps_distance_km += gps_delta
                if gps_delta >= 0.03:
                    moved = True
            self.location_sample = sample
            self._maybe_add_route_point(sample, derived_speed)
            self.evidence.append(sample.evidence_point("location_changed"))

        if sample.soc_pct != previous.soc_pct:
            self.evidence.append(sample.evidence_point("soc_changed"))
        if sample.usage_mode != previous.usage_mode:
            self.evidence.append(sample.evidence_point("usage_mode_changed"))
        if sample.available != previous.available:
            self.evidence.append(sample.evidence_point("availability_changed"))
        if sample.locked != previous.locked or sample.any_door_open != previous.any_door_open:
            self.evidence.append(sample.evidence_point("exterior_changed"))

        consumption = sample.consumption_kwh_100km
        if consumption is not None and 0 < consumption < 200 and consumption != previous.consumption_kwh_100km:
            self.consumption_sum += consumption
            self.consumption_count += 1

        self.last_sample = sample
        return moved

    def _maybe_add_route_point(self, sample: TripSample, derived_speed: float | None) -> None:
        if sample.latitude is None or sample.longitude is None:
            return
        if not self.route:
            self.route.append(sample.route_point(derived_speed))
            return
        previous = self.route[-1]
        backend_time = _iso(sample.location_at or sample.observed_at)
        same_backend_time = previous.get("t") == backend_time
        distance = _haversine_km(
            _float(previous.get("lat")),
            _float(previous.get("lon")),
            sample.latitude,
            sample.longitude,
        )
        if not same_backend_time and distance >= ROUTE_MIN_DISTANCE_KM:
            self.route.append(sample.route_point(derived_speed))

    def odometer_distance_km(self, end_sample: TripSample | None = None) -> float | None:
        start = self.start_sample.odometer_km
        end = (end_sample or self.last_sample).odometer_km
        if start is None or end is None:
            return None
        delta = end - start
        if 0 <= delta <= MAX_VALID_ODOMETER_DELTA_KM:
            return delta
        return None

    def distance_km(self, end_sample: TripSample | None = None) -> float:
        odometer = self.odometer_distance_km(end_sample)
        if odometer is not None and odometer >= 0.01:
            return odometer
        return self.gps_distance_km

    def to_store_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": _iso(self.started_at),
            "start_reason": self.start_reason,
            "start_sample": self.start_sample.store_dict(),
            "last_sample": self.last_sample.store_dict(),
            "odometer_sample": self.odometer_sample.store_dict(),
            "location_sample": self.location_sample.store_dict(),
            "gps_distance_km": self.gps_distance_km,
            "max_derived_speed_kmh": self.max_derived_speed_kmh,
            "last_derived_speed_kmh": self.last_derived_speed_kmh,
            "valid_speed_segments": self.valid_speed_segments,
            "consumption_sum": self.consumption_sum,
            "consumption_count": self.consumption_count,
            "route": self.route,
            "evidence": self.evidence,
        }

    @classmethod
    def from_store_dict(cls, data: dict[str, Any]) -> "TripSession | None":
        try:
            started_at = _parse_iso(data.get("started_at"))
            start_sample = TripSample.from_store_dict(data.get("start_sample") or {})
            last_sample = TripSample.from_store_dict(data.get("last_sample") or {})
            odometer_sample = TripSample.from_store_dict(data.get("odometer_sample") or {}) or last_sample
            location_sample = TripSample.from_store_dict(data.get("location_sample") or {}) or last_sample
            if not all((started_at, start_sample, last_sample, odometer_sample, location_sample)):
                return None
            return cls(
                id=str(data["id"]),
                started_at=started_at,
                start_reason=str(data.get("start_reason") or "restored_session"),
                start_sample=start_sample,
                last_sample=last_sample,
                odometer_sample=odometer_sample,
                location_sample=location_sample,
                gps_distance_km=float(data.get("gps_distance_km", 0.0)),
                max_derived_speed_kmh=float(data.get("max_derived_speed_kmh", 0.0)),
                last_derived_speed_kmh=_float(data.get("last_derived_speed_kmh")),
                valid_speed_segments=int(data.get("valid_speed_segments", 0)),
                consumption_sum=float(data.get("consumption_sum", 0.0)),
                consumption_count=int(data.get("consumption_count", 0)),
                route=list(data.get("route") or []),
                evidence=list(data.get("evidence") or []),
            )
        except (KeyError, TypeError, ValueError):
            return None


class TripRecorder:
    """Persistent trip recorder for one Polestar vehicle."""

    def __init__(self, hass: HomeAssistant, coordinator, entry_id: str) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.entry_id = entry_id
        self.vin = coordinator.vehicle.vin
        self._store = Store[dict[str, Any]](
            hass,
            _STORAGE_VERSION,
            f"{_STORAGE_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._export_dir = Path(hass.config.path("polestar_trips", self.vin[-6:]))
        self._trips: list[dict[str, Any]] = []
        self._active: TripSession | None = None
        self._armed_at: datetime | None = None
        self._armed_until: datetime | None = None
        self._armed_baseline: TripSample | None = None
        self._stop_candidate_at: datetime | None = None
        self._stop_candidate_sample: TripSample | None = None
        self._last_observed: TripSample | None = None
        self._last_motion_at: datetime | None = None
        self._last_probe_at: datetime | None = None
        self._last_probe_error: str | None = None
        self._probe_failures = 0
        self._last_persist_monotonic = 0.0
        self._total_trip_count = 0
        self._lifetime_distance_km = 0.0
        self._listeners: list[Callable[[], None]] = []
        self._unsub_coordinator = None
        self._unsub_tick = None
        self._lock = asyncio.Lock()
        self._processing_scheduled = False
        self._stopped = False

    async def async_start(self) -> None:
        payload = await self._store.async_load() or {}
        self._trips = [trip for trip in payload.get("trips", []) if isinstance(trip, dict)]
        self._total_trip_count = int(payload.get("total_trip_count", len(self._trips)))
        self._lifetime_distance_km = float(
            payload.get(
                "lifetime_distance_km",
                sum(_float(trip.get("distance_km")) or 0 for trip in self._trips),
            )
        )
        active = payload.get("active")
        if isinstance(active, dict):
            self._active = TripSession.from_store_dict(active.get("session") or {})
            self._last_motion_at = _parse_iso(active.get("last_motion_at"))
            self._stop_candidate_at = _parse_iso(active.get("stop_candidate_at"))
            self._stop_candidate_sample = TripSample.from_store_dict(active.get("stop_candidate_sample") or {})
        armed = payload.get("armed")
        if isinstance(armed, dict):
            self._armed_at = _parse_iso(armed.get("armed_at"))
            self._armed_until = _parse_iso(armed.get("armed_until"))
            self._armed_baseline = TripSample.from_store_dict(armed.get("baseline") or {})

        self._unsub_coordinator = self.coordinator.async_add_listener(self._coordinator_updated)
        self._unsub_tick = async_track_time_interval(self.hass, self._async_tick, TICK_INTERVAL)
        await self.async_process_snapshot()
        await self._async_save()
        self._notify()

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        await self._async_save()

    @classmethod
    async def async_remove_storage(cls, hass: HomeAssistant, entry_id: str) -> None:
        store = Store[dict[str, Any]](
            hass,
            _STORAGE_VERSION,
            f"{_STORAGE_PREFIX}.{entry_id}",
            private=True,
            atomic_writes=True,
        )
        await store.async_remove()

    @callback
    def _coordinator_updated(self) -> None:
        if self._stopped or self._processing_scheduled:
            return
        self._processing_scheduled = True
        self.hass.async_create_task(self._async_process_from_listener())

    async def _async_process_from_listener(self) -> None:
        try:
            await self.async_process_snapshot()
        finally:
            self._processing_scheduled = False

    async def _async_tick(self, _now: datetime) -> None:
        if self._stopped:
            return
        async with self._lock:
            now = datetime.now(UTC)
            self._expire_arm(now)
            await self._maybe_probe(now)
            await self._maybe_finish_trip(now)
            if self._active and time.monotonic() - self._last_persist_monotonic >= ACTIVE_PERSIST_INTERVAL_S:
                await self._async_save()
                self._last_persist_monotonic = time.monotonic()
            self._notify()

    async def async_process_snapshot(self) -> None:
        async with self._lock:
            sample = self._snapshot()
            if sample is None:
                return
            previous = self._last_observed
            self._last_observed = sample

            if self._active is not None:
                moved = self._active.add_sample(sample)
                if moved:
                    self._last_motion_at = sample.observed_at
                self._update_stop_candidate(sample, previous)
                await self._maybe_finish_trip(sample.observed_at)
                self._notify()
                return

            # Primary authoritative boundary for the observed Polestar 2.
            if sample.usage_mode == DRIVING_MODE:
                await self._async_start_trip(sample, sample, "usage_mode_driving")
                self._notify()
                return

            # Fallback mode for backends that do not expose a useful usage_mode.
            if self._should_arm(sample, previous):
                self._arm(sample)

            baseline = self._armed_baseline
            if baseline is not None and self._armed_until is not None:
                if self._fallback_movement_confirmed(baseline, sample):
                    await self._async_start_trip(baseline, sample, "fallback_motion")
                elif self._fallback_soc_confirmed(baseline, sample):
                    await self._async_start_trip(baseline, sample, "fallback_soc_drop")

            self._notify()

    def _snapshot(self) -> TripSample | None:
        data = self.coordinator.data
        if data is None:
            return None
        location = data.location
        coordinate = getattr(location, "coordinate", None) if location else None
        latitude = _float(getattr(coordinate, "latitude", None)) if coordinate else None
        longitude = _float(getattr(coordinate, "longitude", None)) if coordinate else None
        location_at = _timestamp_to_datetime(getattr(location, "timestamp", None)) if location else None
        if location_at is None and latitude == 0.0 and longitude == 0.0:
            latitude = None
            longitude = None

        odometer = data.odometer
        battery = data.battery
        availability = data.availability
        exterior = data.exterior
        return TripSample(
            observed_at=datetime.now(UTC),
            location_at=location_at,
            odometer_at=_timestamp_to_datetime(getattr(odometer, "timestamp", None)) if odometer else None,
            battery_at=_timestamp_to_datetime(getattr(battery, "timestamp", None)) if battery else None,
            availability_at=_timestamp_to_datetime(getattr(availability, "timestamp", None)) if availability else None,
            latitude=latitude,
            longitude=longitude,
            altitude_m=_float(getattr(location, "altitude", None)) if location else None,
            heading_deg=_float(getattr(location, "heading", None)) if location else None,
            reported_speed=_float(getattr(location, "speed", None)) if location else None,
            odometer_km=_float(getattr(odometer, "odometer_km", None)) if odometer else None,
            trip_auto_km=_float(getattr(odometer, "trip_meter_automatic_km", None)) if odometer else None,
            soc_pct=_float(getattr(battery, "charge_level", None)) if battery else None,
            range_km=_float(getattr(battery, "range_km", None)) if battery else None,
            consumption_kwh_100km=_float(getattr(battery, "avg_consumption_auto", None)) if battery else None,
            usage_mode=_enum_name(getattr(availability, "usage_mode", None)) if availability else None,
            unavailable_reason=_enum_name(getattr(availability, "unavailable_reason", None)) if availability else None,
            available=bool(getattr(availability, "is_available", False)) if availability is not None else None,
            charging=bool(getattr(battery, "is_charging", False)) if battery is not None else None,
            locked=bool(getattr(exterior, "is_locked", False)) if exterior is not None else None,
            any_door_open=bool(getattr(exterior, "any_door_open", False)) if exterior is not None else None,
        )

    def _should_arm(self, sample: TripSample, previous: TripSample | None) -> bool:
        if sample.unavailable_reason == "car_in_use":
            return True
        if sample.any_door_open:
            return True
        if sample.locked is False:
            return previous is None or previous.locked is True or self._armed_until is None
        if sample.usage_mode in {"convenience", "active", "engine_on"}:
            return True
        return False

    def _arm(self, sample: TripSample) -> None:
        now = sample.observed_at
        if self._armed_at is None:
            self._armed_at = now
            # v3: freeze the candidate baseline. Never roll it forward.
            self._armed_baseline = sample
            _LOGGER.debug("Trip recorder armed for Polestar …%s", self.vin[-6:])
        self._armed_until = now + ARM_WINDOW

    def _expire_arm(self, now: datetime) -> None:
        if self._active is None and self._armed_until is not None and now >= self._armed_until:
            self._armed_at = None
            self._armed_until = None
            self._armed_baseline = None

    @staticmethod
    def _movement_deltas(previous: TripSample, current: TripSample) -> tuple[float, float]:
        odo_delta = 0.0
        if previous.odometer_km is not None and current.odometer_km is not None:
            candidate = current.odometer_km - previous.odometer_km
            if 0 <= candidate <= MAX_VALID_ODOMETER_DELTA_KM:
                odo_delta = candidate
        gps_delta = _haversine_km(previous.latitude, previous.longitude, current.latitude, current.longitude)
        if gps_delta > MAX_VALID_GPS_STEP_KM:
            gps_delta = 0.0
        return odo_delta, gps_delta

    def _fallback_movement_confirmed(self, baseline: TripSample, sample: TripSample) -> bool:
        odo_delta, gps_delta = self._movement_deltas(baseline, sample)
        return odo_delta >= START_ODOMETER_DELTA_KM or gps_delta >= START_GPS_DELTA_KM

    @staticmethod
    def _fallback_soc_confirmed(baseline: TripSample, sample: TripSample) -> bool:
        if baseline.soc_pct is None or sample.soc_pct is None:
            return False
        if baseline.charging is True or sample.charging is True:
            return False
        return baseline.soc_pct - sample.soc_pct >= START_SOC_DROP_PCT

    async def _async_start_trip(
        self,
        baseline: TripSample,
        current: TripSample,
        start_reason: str,
    ) -> None:
        self._active = TripSession.from_baseline(baseline, start_reason)
        if current.observed_at != baseline.observed_at:
            self._active.add_sample(current)
        self._last_motion_at = current.observed_at
        self._armed_at = None
        self._armed_until = None
        self._armed_baseline = None
        self._stop_candidate_at = None
        self._stop_candidate_sample = None
        self._last_persist_monotonic = time.monotonic()
        await self._async_save()
        _LOGGER.info(
            "Trip started for Polestar …%s (%s)",
            self.vin[-6:],
            start_reason,
        )

    def _update_stop_candidate(self, sample: TripSample, previous: TripSample | None) -> None:
        if self._active is None:
            return
        if sample.usage_mode == DRIVING_MODE:
            self._stop_candidate_at = None
            self._stop_candidate_sample = None
            return
        if sample.usage_mode in NON_DRIVING_MODES:
            if self._stop_candidate_at is None:
                self._stop_candidate_at = sample.observed_at
                self._stop_candidate_sample = sample
                _LOGGER.debug(
                    "Trip stop candidate for Polestar …%s: usage_mode=%s",
                    self.vin[-6:],
                    sample.usage_mode,
                )

    def _has_park_signal(self, sample: TripSample | None) -> bool:
        if sample is None:
            return False
        return sample.locked is True or sample.usage_mode in NON_DRIVING_MODES or sample.available is True

    async def _maybe_finish_trip(self, now: datetime) -> None:
        if self._active is None:
            return

        if self._stop_candidate_at is not None:
            latest = self._last_observed or self._active.last_sample
            if latest.usage_mode == DRIVING_MODE:
                self._stop_candidate_at = None
                self._stop_candidate_sample = None
                return
            if now - self._stop_candidate_at >= STOP_CONFIRMATION:
                ended_at = self._stop_candidate_at
                end_sample = self._stop_candidate_sample or latest
                await self._async_finalize(
                    ended_at,
                    "usage_mode_left_driving",
                    end_sample=end_sample,
                    boundary_confidence="high",
                )
                return

        # Fallback only if usage_mode is absent. Do not let sparse odometer/GPS
        # overwrite a perfectly good DRIVING session boundary.
        latest = self._last_observed or self._active.last_sample
        if latest.usage_mode is not None:
            return
        if self._last_motion_at is None:
            return
        idle = now - self._last_motion_at
        if idle >= FALLBACK_STOP_WITH_PARK_SIGNAL and self._has_park_signal(latest):
            await self._async_finalize(now, "fallback_parked_or_inactive", boundary_confidence="medium")
        elif idle >= FALLBACK_HARD_STOP:
            await self._async_finalize(now, "fallback_no_motion_timeout", boundary_confidence="low")

    async def _maybe_probe(self, now: datetime) -> None:
        interval = None
        attrs: tuple[str, ...] = ()
        if self._active is not None:
            interval = ACTIVE_PROBE_INTERVAL_S
            attrs = ("availability", "battery", "location")
        elif self._armed_until is not None and now < self._armed_until:
            interval = ARMED_PROBE_INTERVAL_S
            attrs = ("availability", "battery", "location")
        if interval is None:
            return
        if self._last_probe_at is not None and (now - self._last_probe_at).total_seconds() < interval:
            return

        self._last_probe_at = now
        try:
            await self.coordinator.async_request_attrs_refresh(*attrs)
            self._last_probe_error = None
            self._probe_failures = 0
        except Exception as err:  # noqa: BLE001
            self._probe_failures += 1
            message = str(err).replace("\r", " ").replace("\n", " ")[:160]
            self._last_probe_error = f"{type(err).__name__}: {message}"
            _LOGGER.debug(
                "Polestar adaptive trip probe failed for …%s: %s",
                self.vin[-6:],
                self._last_probe_error,
            )

    async def _async_finalize(
        self,
        ended_at: datetime,
        reason: str,
        *,
        end_sample: TripSample | None = None,
        boundary_confidence: str = "medium",
    ) -> None:
        session = self._active
        if session is None:
            return
        final_sample = end_sample or session.last_sample
        distance = session.distance_km(final_sample)
        if distance < MIN_TRIP_DISTANCE_KM:
            self._active = None
            self._last_motion_at = None
            self._stop_candidate_at = None
            self._stop_candidate_sample = None
            await self._async_save()
            return

        duration_s = max(0.0, (ended_at - session.started_at).total_seconds())
        average_speed = distance / (duration_s / 3600) if duration_s > 0 else None
        average_consumption = (
            session.consumption_sum / session.consumption_count
            if session.consumption_count
            else final_sample.consumption_kwh_100km
        )
        estimated_energy = distance * average_consumption / 100 if average_consumption is not None else None
        soc_start = session.start_sample.soc_pct
        soc_end = final_sample.soc_pct
        soc_used = max(0.0, soc_start - soc_end) if soc_start is not None and soc_end is not None else None
        range_start = session.start_sample.range_km
        range_end = final_sample.range_km
        range_change = range_end - range_start if range_start is not None and range_end is not None else None
        start_zone = self._zone_name(session.start_sample.latitude, session.start_sample.longitude)
        end_zone = self._zone_name(final_sample.latitude, final_sample.longitude)
        relative_base = Path("polestar_trips") / self.vin[-6:] / session.id

        max_segment_speed = session.max_derived_speed_kmh if session.valid_speed_segments else None
        summary = {
            "schema_version": 3,
            "id": session.id,
            "started_at": _iso(session.started_at),
            "ended_at": _iso(ended_at),
            "start_reason": session.start_reason,
            "end_reason": reason,
            "boundary_confidence": boundary_confidence,
            "start_zone": start_zone,
            "end_zone": end_zone,
            "distance_km": _rounded(distance, 3),
            "gps_distance_km": _rounded(session.gps_distance_km, 3),
            "duration_min": _rounded(duration_s / 60, 1),
            "moving_time_min": None,
            "average_speed_kmh": _rounded(average_speed, 1),
            "average_moving_speed_kmh": None,
            "max_derived_speed_kmh": _rounded(max_segment_speed, 1),
            "valid_speed_segments": session.valid_speed_segments,
            "soc_start_pct": _rounded(soc_start, 1),
            "soc_end_pct": _rounded(soc_end, 1),
            "soc_used_pct": _rounded(soc_used, 1),
            "range_start_km": _rounded(range_start, 1),
            "range_end_km": _rounded(range_end, 1),
            "range_change_km": _rounded(range_change, 1),
            "odometer_start_km": _rounded(session.start_sample.odometer_km, 3),
            "odometer_end_km": _rounded(final_sample.odometer_km, 3),
            "average_consumption_kwh_per_100km": _rounded(average_consumption, 2),
            "estimated_energy_kwh": _rounded(estimated_energy, 2),
            "start_latitude": _rounded(session.start_sample.latitude, 7),
            "start_longitude": _rounded(session.start_sample.longitude, 7),
            "end_latitude": _rounded(final_sample.latitude, 7),
            "end_longitude": _rounded(final_sample.longitude, 7),
            "start_availability_at": _iso(session.start_sample.availability_at),
            "end_availability_at": _iso(final_sample.availability_at),
            "start_battery_at": _iso(session.start_sample.battery_at),
            "end_battery_at": _iso(final_sample.battery_at),
            "start_odometer_at": _iso(session.start_sample.odometer_at),
            "end_odometer_at": _iso(final_sample.odometer_at),
            "route_points": len(session.route),
            "evidence_points": len(session.evidence) + 1,
            "json_file": str(relative_base.with_suffix(".json")),
            "gpx_file": str(relative_base.with_suffix(".gpx")),
            "geojson_file": str(relative_base.with_suffix(".geojson")),
        }

        route = list(session.route)
        evidence = list(session.evidence)
        evidence.append(final_sample.evidence_point(f"trip_end:{reason}"))
        self._trips.append(summary)
        self._total_trip_count += 1
        self._lifetime_distance_km += distance
        self._active = None
        self._last_motion_at = None
        self._stop_candidate_at = None
        self._stop_candidate_sample = None
        await self._async_prune()
        await self._async_save()
        try:
            await self.hass.async_add_executor_job(self._export_trip_sync, summary, route, evidence)
            await self.hass.async_add_executor_job(self._write_summary_csv_sync)
        except OSError:
            _LOGGER.exception("Failed to export Polestar trip files")
        _LOGGER.info(
            "Polestar trip completed for …%s: %.1f km, %.0f min (%s)",
            self.vin[-6:],
            distance,
            duration_s / 60,
            reason,
        )

    async def _async_save(self) -> None:
        active = None
        if self._active is not None:
            active = {
                "session": self._active.to_store_dict(),
                "last_motion_at": _iso(self._last_motion_at),
                "stop_candidate_at": _iso(self._stop_candidate_at),
                "stop_candidate_sample": (
                    self._stop_candidate_sample.store_dict() if self._stop_candidate_sample else None
                ),
            }
        armed = None
        if self._armed_until is not None:
            armed = {
                "armed_at": _iso(self._armed_at),
                "armed_until": _iso(self._armed_until),
                "baseline": self._armed_baseline.store_dict() if self._armed_baseline else None,
            }
        await self._store.async_save(
            {
                "trips": self._trips,
                "active": active,
                "armed": armed,
                "total_trip_count": self._total_trip_count,
                "lifetime_distance_km": self._lifetime_distance_km,
            }
        )

    async def _async_prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        keep: list[dict[str, Any]] = []
        remove: list[dict[str, Any]] = []
        for trip in self._trips:
            ended = _parse_iso(trip.get("ended_at"))
            if ended is not None and ended < cutoff:
                remove.append(trip)
            else:
                keep.append(trip)
        if len(keep) > MAX_STORED_TRIPS:
            excess = len(keep) - MAX_STORED_TRIPS
            remove.extend(keep[:excess])
            keep = keep[excess:]
        self._trips = keep
        if remove:
            await self.hass.async_add_executor_job(self._delete_trip_files_sync, remove)

    def _zone_name(self, latitude: float | None, longitude: float | None) -> str | None:
        if latitude is None or longitude is None:
            return None
        try:
            state = zone.async_active_zone(self.hass, latitude, longitude, 0)
        except (TypeError, ValueError):
            return None
        return state.name if state is not None else None

    def _ensure_export_dir_sync(self) -> None:
        self._export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self._export_dir, 0o700)
        except OSError:
            pass

    @staticmethod
    def _atomic_write(path: Path, contents: str) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    def _export_trip_sync(
        self,
        summary: dict[str, Any],
        route: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> None:
        self._ensure_export_dir_sync()
        trip_id = summary["id"]
        self._atomic_write(
            self._export_dir / f"{trip_id}.json",
            json.dumps(
                {"summary": summary, "route": route, "evidence": evidence},
                indent=2,
                ensure_ascii=False,
            ),
        )
        coordinates = [
            [point["lon"], point["lat"], point.get("altitude_m") or 0]
            for point in route
            if point.get("lat") is not None and point.get("lon") is not None
        ]
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        key: value
                        for key, value in summary.items()
                        if key not in {"json_file", "gpx_file", "geojson_file"}
                    },
                    "geometry": {"type": "LineString", "coordinates": coordinates},
                }
            ],
        }
        self._atomic_write(
            self._export_dir / f"{trip_id}.geojson",
            json.dumps(geojson, indent=2, ensure_ascii=False),
        )
        self._atomic_write(self._export_dir / f"{trip_id}.gpx", self._gpx(summary, route))

    def _gpx(self, summary: dict[str, Any], route: list[dict[str, Any]]) -> str:
        route_name = escape(f"{summary.get('start_zone') or 'Start'} → {summary.get('end_zone') or 'End'}")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="Home Assistant Polestar Trip Recorder" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:ps="https://local.invalid/polestar-trip-recorder/3">',
            "  <trk>",
            f"    <name>{route_name}</name>",
            "    <trkseg>",
        ]
        for point in route:
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is None or lon is None:
                continue
            lines.append(f'      <trkpt lat="{lat}" lon="{lon}">')
            if point.get("altitude_m") is not None:
                lines.append(f"        <ele>{point['altitude_m']}</ele>")
            if point.get("t"):
                lines.append(f"        <time>{point['t']}</time>")
            lines.append("        <extensions>")
            for tag in (
                "derived_speed_kmh",
                "reported_speed_raw",
                "heading_deg",
                "soc_pct",
                "range_km",
                "odometer_km",
                "consumption_kwh_per_100km",
            ):
                value = point.get(tag)
                if value is not None:
                    lines.append(f"          <ps:{tag}>{value}</ps:{tag}>")
            lines.append("        </extensions>")
            lines.append("      </trkpt>")
        lines.extend(["    </trkseg>", "  </trk>", "</gpx>", ""])
        return "\n".join(lines)

    def _write_summary_csv_sync(self) -> None:
        self._ensure_export_dir_sync()
        path = self._export_dir / "trips.csv"
        temp = self._export_dir / ".trips.csv.tmp"
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for trip in self._trips:
                writer.writerow(trip)
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    def _delete_trip_files_sync(self, trips: list[dict[str, Any]]) -> None:
        for trip in trips:
            trip_id = trip.get("id")
            if not isinstance(trip_id, str):
                continue
            for suffix in (".json", ".gpx", ".geojson"):
                try:
                    (self._export_dir / f"{trip_id}{suffix}").unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("Could not remove old Polestar trip file %s%s", trip_id, suffix)

    @property
    def status(self) -> str:
        if self._active is not None:
            return "stopping" if self._stop_candidate_at is not None else "driving"
        if self._armed_until is not None and datetime.now(UTC) < self._armed_until:
            return "armed"
        return "parked"

    @property
    def current_distance_km(self) -> float | None:
        return _rounded(self._active.distance_km(), 3) if self._active else None

    @property
    def current_duration_min(self) -> float | None:
        if self._active is None:
            return None
        boundary = self._stop_candidate_at or datetime.now(UTC)
        return _rounded((boundary - self._active.started_at).total_seconds() / 60, 1)

    @property
    def current_average_speed_kmh(self) -> float | None:
        if self._active is None:
            return None
        duration_min = self.current_duration_min
        distance_km = self.current_distance_km
        if not duration_min or distance_km is None:
            return None
        return _rounded(distance_km / (duration_min / 60), 1)

    @property
    def current_derived_segment_speed_kmh(self) -> float | None:
        if self._active is None or not self._active.valid_speed_segments:
            return None
        return _rounded(self._active.last_derived_speed_kmh, 1)

    @property
    def last_trip(self) -> dict[str, Any] | None:
        return self._trips[-1] if self._trips else None

    @property
    def total_trip_count(self) -> int:
        return self._total_trip_count

    @property
    def lifetime_distance_km(self) -> float:
        return _rounded(self._lifetime_distance_km, 3) or 0.0

    def _local_timezone(self):
        try:
            return ZoneInfo(self.hass.config.time_zone)
        except Exception:
            return UTC

    def _today_trips(self) -> list[dict[str, Any]]:
        timezone = self._local_timezone()
        today = datetime.now(timezone).date()
        result = []
        for trip in self._trips:
            started = _parse_iso(trip.get("started_at"))
            if started is not None and started.astimezone(timezone).date() == today:
                result.append(trip)
        return result

    def _active_started_today(self) -> bool:
        if self._active is None:
            return False
        timezone = self._local_timezone()
        return self._active.started_at.astimezone(timezone).date() == datetime.now(timezone).date()

    @property
    def trips_today(self) -> int:
        return len(self._today_trips()) + (1 if self._active_started_today() else 0)

    @property
    def distance_today_km(self) -> float:
        distance = sum(_float(trip.get("distance_km")) or 0 for trip in self._today_trips())
        if self._active_started_today() and self._active is not None:
            distance += self._active.distance_km()
        return _rounded(distance, 3) or 0.0

    @property
    def driving_time_today_min(self) -> float:
        duration = sum(_float(trip.get("duration_min")) or 0 for trip in self._today_trips())
        if self._active_started_today():
            duration += self.current_duration_min or 0
        return _rounded(duration, 1) or 0.0

    @property
    def stream_health(self) -> str:
        diagnostics = getattr(self.coordinator, "stream_diagnostics", {}) or {}
        statuses = {
            name: (diagnostics.get(name) or {}).get("status")
            for name in ("location", "odometer", "exterior", "battery")
        }
        if not any(statuses.values()):
            return "unknown"
        if any(status in {"error", "unsupported"} for status in statuses.values()):
            return "degraded"
        # Clean EOF is common on this backend. It is a finite/limited push feed,
        # not necessarily a fault, because successful probes/polls reopen it.
        if any(status in {"ended", "retrying", "connecting", "stopped"} for status in statuses.values()):
            return "limited"
        return "healthy"

    def status_attributes(self) -> dict[str, Any]:
        sample = self._last_observed
        return {
            "primary_boundary_signal": "availability.usage_mode == driving",
            "armed_at": _iso(self._armed_at),
            "armed_until": _iso(self._armed_until),
            "stop_candidate_at": _iso(self._stop_candidate_at),
            "last_motion_at": _iso(self._last_motion_at),
            "last_probe_at": _iso(self._last_probe_at),
            "last_probe_error": self._last_probe_error,
            "probe_failures": self._probe_failures,
            "armed_probe_interval_s": ARMED_PROBE_INTERVAL_S,
            "active_probe_interval_s": ACTIVE_PROBE_INTERVAL_S,
            "last_odometer_km": _rounded(sample.odometer_km, 3) if sample else None,
            "last_location_at": _iso(sample.location_at) if sample else None,
            "last_odometer_at": _iso(sample.odometer_at) if sample else None,
            "last_battery_at": _iso(sample.battery_at) if sample else None,
            "last_availability_at": _iso(sample.availability_at) if sample else None,
            "usage_mode": sample.usage_mode if sample else None,
            "available": sample.available if sample else None,
            "locked": sample.locked if sample else None,
            "any_door_open": sample.any_door_open if sample else None,
            "stream_health": self.stream_health,
            "stored_trips": len(self._trips),
            "retention_days": RETENTION_DAYS,
            "export_directory": str(Path("polestar_trips") / self.vin[-6:]),
        }

    def stream_attributes(self) -> dict[str, Any]:
        diagnostics = getattr(self.coordinator, "stream_diagnostics", {}) or {}
        return {
            "streams": diagnostics,
            "location_note": (
                "Frames received can contain duplicate/stale backend data. "
                "Use fresh_frames/source_timestamp rather than frames_received alone."
            ),
        }

    def last_trip_attributes(self) -> dict[str, Any]:
        return dict(self.last_trip or {})

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()


@dataclass(frozen=True, kw_only=True)
class TripSensorDescription(SensorEntityDescription):
    value_fn: Callable[[TripRecorder], StateType | None]
    attrs_fn: Callable[[TripRecorder], dict[str, Any]] | None = None


TRIP_SENSORS = (
    TripSensorDescription(
        key="trip_status",
        name="Trip recorder status",
        icon="mdi:map-marker-path",
        device_class=SensorDeviceClass.ENUM,
        options=["parked", "armed", "driving", "stopping"],
        value_fn=lambda r: r.status,
        attrs_fn=lambda r: r.status_attributes(),
    ),
    TripSensorDescription(
        key="stream_health",
        name="Stream health",
        icon="mdi:cloud-sync",
        device_class=SensorDeviceClass.ENUM,
        options=["healthy", "limited", "degraded", "unknown"],
        value_fn=lambda r: r.stream_health,
        attrs_fn=lambda r: r.stream_attributes(),
    ),
    TripSensorDescription(
        key="current_trip_distance",
        name="Current trip distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda r: r.current_distance_km,
    ),
    TripSensorDescription(
        key="current_trip_duration",
        name="Current trip duration",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda r: r.current_duration_min,
    ),
    TripSensorDescription(
        key="current_trip_average_speed",
        name="Current trip average speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda r: r.current_average_speed_kmh,
    ),
    TripSensorDescription(
        key="current_trip_derived_segment_speed",
        name="Current trip derived segment speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda r: r.current_derived_segment_speed_kmh,
    ),
    TripSensorDescription(
        key="last_trip",
        name="Last trip",
        icon="mdi:map-clock",
        value_fn=lambda r: r.last_trip.get("id") if r.last_trip else None,
        attrs_fn=lambda r: r.last_trip_attributes(),
    ),
    TripSensorDescription(
        key="last_trip_distance",
        name="Last trip distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda r: r.last_trip.get("distance_km") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_duration",
        name="Last trip duration",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda r: r.last_trip.get("duration_min") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_average_speed",
        name="Last trip average speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda r: r.last_trip.get("average_speed_kmh") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_average_moving_speed",
        name="Last trip average moving speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda r: r.last_trip.get("average_moving_speed_kmh") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_max_speed",
        name="Last trip maximum speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=lambda r: r.last_trip.get("max_derived_speed_kmh") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_soc_used",
        name="Last trip SOC used",
        icon="mdi:battery-minus",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda r: r.last_trip.get("soc_used_pct") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_consumption",
        name="Last trip average consumption",
        device_class=SensorDeviceClass.ENERGY_DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergyDistance.KILO_WATT_HOUR_PER_100_KM,
        value_fn=lambda r: r.last_trip.get("average_consumption_kwh_per_100km") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="last_trip_energy",
        name="Last trip estimated energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda r: r.last_trip.get("estimated_energy_kwh") if r.last_trip else None,
    ),
    TripSensorDescription(
        key="trips_today",
        name="Trips today",
        icon="mdi:counter",
        value_fn=lambda r: r.trips_today,
    ),
    TripSensorDescription(
        key="distance_today",
        name="Distance today",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda r: r.distance_today_km,
    ),
    TripSensorDescription(
        key="driving_time_today",
        name="Driving time today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda r: r.driving_time_today_min,
    ),
    TripSensorDescription(
        key="recorded_trip_count",
        name="Recorded trip count",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda r: r.total_trip_count,
    ),
    TripSensorDescription(
        key="recorded_distance_total",
        name="Recorded distance total",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda r: r.lifetime_distance_km,
    ),
)


class PolestarTripSensor(PolestarEntity, SensorEntity):
    entity_description: TripSensorDescription

    def __init__(self, recorder: TripRecorder, description: TripSensorDescription) -> None:
        super().__init__(recorder.coordinator)
        self.recorder = recorder
        self.entity_description = description
        self._attr_unique_id = f"{self._vehicle.vin}_triprec_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.recorder.async_add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> StateType | None:
        return self.entity_description.value_fn(self.recorder)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self.recorder) if attrs_fn is not None else {}


def create_trip_sensor_entities(recorder: TripRecorder) -> list[PolestarTripSensor]:
    return [PolestarTripSensor(recorder, description) for description in TRIP_SENSORS]
