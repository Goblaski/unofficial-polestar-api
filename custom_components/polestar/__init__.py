"""Polestar integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import CONF_DEMO, CONF_VIN, DOMAIN, PLATFORMS
from .coordinator import PolestarCoordinator
from .demo import DemoVehicle
from .polestar_api import PolestarApi, Vehicle
from .polestar_api.exceptions import ApiError, AuthError
from .services import async_register_services, async_unregister_services
from .token_store import HassTokenStore
from .trip_recorder import TripRecorder

_LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def _redact_vin(vin: str) -> str:
    """Return a log-safe VIN representation."""
    if len(vin) <= 6:
        return "***"
    return f"***{vin[-6:]}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Polestar from a config entry."""
    if entry.data.get(CONF_DEMO):
        return await _async_setup_demo(hass, entry)

    email = entry.data[CONF_EMAIL]
    # Password is intentionally optional after first successful setup. Persistent
    # authentication should use the private refresh-token store instead.
    password = entry.data.get(CONF_PASSWORD)
    configured_vin = entry.data[CONF_VIN]

    token_store = HassTokenStore(hass, entry.entry_id)
    api = PolestarApi(email, password, token_store=token_store)

    try:
        await api.async_init()

        # Once the OIDC exchange/refresh succeeds, remove the long-lived password
        # from Home Assistant's config-entry JSON. If refresh later fails, HA's
        # reauthentication flow asks for it again instead of storing it forever.
        if CONF_PASSWORD in entry.data:
            new_data = dict(entry.data)
            new_data.pop(CONF_PASSWORD, None)
            hass.config_entries.async_update_entry(entry, data=new_data)

        try:
            vehicles = await api.get_vehicles()
        except ApiError as err:
            failure = (
                f"HTTP {err.status_code}"
                if err.status_code is not None
                else "GraphQL/API error"
            )
            _LOGGER.warning("Vehicle list lookup failed (%s); using configured VIN", failure)
            vehicles = []
    except AuthError as err:
        await api.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception:
        await api.close()
        raise

    vehicle = next((v for v in vehicles if v.vin == configured_vin), None)

    if vehicle is None:
        # Guest / linked accounts don't appear in the VDMS vehicle list.
        # Create a Vehicle directly — gRPC access is validated at runtime.
        _LOGGER.info(
            "VIN %s not in VDMS vehicle list (guest/linked account), "
            "creating vehicle directly",
            _redact_vin(configured_vin),
        )
        vehicle = Vehicle(vin=configured_vin, connection=api._connection)

    coordinator = PolestarCoordinator(hass, vehicle, entry)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_streams()

    coordinators: dict[str, PolestarCoordinator] = {
        vehicle.vin: coordinator
    }

    trip_recorders = {}

    try:
        trip_recorder = TripRecorder(
            hass,
            coordinator,
            entry.entry_id,
        )

        await trip_recorder.async_start()

        trip_recorders[vehicle.vin] = trip_recorder

    except Exception:
        _LOGGER.exception(
            "Failed to start Polestar trip recorder"
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinators": coordinators,
        "trip_recorders": trip_recorders,
    }

    async_register_services(hass)
    await _async_register_static_path(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_setup_demo(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a demo vehicle with fake data."""
    vehicle = DemoVehicle()
    coordinator = PolestarCoordinator(hass, vehicle, entry)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_streams()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": None,
        "coordinators": {vehicle.vin: coordinator},
    }

    async_register_services(hass)
    await _async_register_static_path(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_register_static_path(hass: HomeAssistant) -> None:
    """Register the integration's static/ directory once."""
    key = f"{DOMAIN}_static_registered"
    if hass.data.get(key) or not STATIC_DIR.is_dir():
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"/{DOMAIN}/static", str(STATIC_DIR), cache_headers=False)]
    )
    hass.data[key] = True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        for recorder in data.get(
            "trip_recorders",
            {}
        ).values():
            await recorder.async_stop()
        for coordinator in data["coordinators"].values():
            await coordinator.async_shutdown()
        if data["api"] is not None:
            await data["api"].close()
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up stored tokens when entry is removed."""
    token_store = HassTokenStore(hass, entry.entry_id)
    await token_store.remove()
    await TripRecorder.async_remove_storage(
        hass,
        entry.entry_id,
    )