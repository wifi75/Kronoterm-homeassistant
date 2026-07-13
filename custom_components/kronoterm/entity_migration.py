"""Entity registry migrations for historical Kronoterm unique IDs."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SENSOR_DEFINITIONS

_LOGGER = logging.getLogger(__name__)

_BINARY_SENSOR_NAMES = {
    (2038, None): "main_pump_status",
    (2045, None): "circulation_loop_1",
    (2055, None): "circulation_loop_2",
    (2065, None): "circulation_loop_3",
    (2075, None): "circulation_loop_4",
    (2028, 0): "circulation_pump",
    (2028, 1): "dhw_circulation_pump",
    (2011, None): "defrost_status",
    (2003, None): "reserve_source_status",
    (2004, None): "alternative_source_status",
    (2088, None): "alternative_source_pump",
    (2002, 0): "additional_source",
}

_SWITCH_NAMES = {
    2012: "heatpump_switch",
    2015: "fast_heating_switch",
    2016: "additional_source_switch",
    2018: "reserve_source_switch",
    2301: "antilegionella_switch",
    2328: "dhw_circulation_switch",
}

_ENERGY_SUFFIXES = (
    "daily_CompHeating",
    "daily_CompTapWater",
    "daily_CPLoops",
    "daily_CPAddSource",
    "daily_combined_CompHeating_CompTapWater_CPLoops_CPAddSource",
    "calculated_power_CompHeating_CompTapWater_CPLoops_CPAddSource",
)


def _move_unique_id(
    registry: er.EntityRegistry,
    config_entry_id: str,
    platform: str,
    old_unique_ids: Iterable[str],
    new_unique_id: str,
) -> bool:
    """Move a legacy registry entry, preferring its established entity ID."""
    current_entity_id = registry.async_get_entity_id(platform, DOMAIN, new_unique_id)

    for old_unique_id in old_unique_ids:
        if old_unique_id == new_unique_id:
            continue
        old_entity_id = registry.async_get_entity_id(platform, DOMAIN, old_unique_id)
        if not old_entity_id:
            continue

        old_entry = registry.async_get(old_entity_id)
        if not old_entry or old_entry.config_entry_id != config_entry_id:
            continue

        if current_entity_id and current_entity_id != old_entity_id:
            current_entry = registry.async_get(current_entity_id)
            if not current_entry or current_entry.config_entry_id != config_entry_id:
                _LOGGER.warning(
                    "Cannot migrate %s: target unique ID belongs to another entry",
                    old_entity_id,
                )
                return False
            # The target is the recently-created duplicate. Keep the older entity ID
            # because it owns the user's customizations and recorder history.
            registry.async_remove(current_entity_id)

        registry.async_update_entity(old_entity_id, new_unique_id=new_unique_id)
        _LOGGER.info("Migrated entity registry entry %s", old_entity_id)
        return True

    return False


def _merge_alias_into_canonical(
    registry: er.EntityRegistry,
    config_entry_id: str,
    platform: str,
    alias_unique_id: str,
    canonical_unique_id: str,
) -> bool:
    """Remove a recent alias while preserving an older canonical entity."""
    alias_entity_id = registry.async_get_entity_id(platform, DOMAIN, alias_unique_id)
    if not alias_entity_id:
        return False
    alias_entry = registry.async_get(alias_entity_id)
    if not alias_entry or alias_entry.config_entry_id != config_entry_id:
        return False

    canonical_entity_id = registry.async_get_entity_id(
        platform, DOMAIN, canonical_unique_id
    )
    if canonical_entity_id:
        canonical_entry = registry.async_get(canonical_entity_id)
        if not canonical_entry or canonical_entry.config_entry_id != config_entry_id:
            return False
        registry.async_remove(alias_entity_id)
        _LOGGER.info(
            "Removed duplicate alias %s in favour of %s",
            alias_entity_id,
            canonical_entity_id,
        )
    else:
        registry.async_update_entity(alias_entity_id, new_unique_id=canonical_unique_id)
        _LOGGER.info("Migrated entity registry alias %s", alias_entity_id)
    return True


async def async_migrate_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> None:
    """Migrate all known pre-1.7 unique IDs without losing entity history."""
    registry = er.async_get(hass)
    entry_prefix = f"{entry.entry_id}_{DOMAIN}_"

    address_names = {
        definition.address: definition.key for definition in SENSOR_DEFINITIONS
    }
    register_map = getattr(coordinator, "register_map", None)
    if register_map:
        address_names.update(
            {
                definition.address: definition.name_en
                for definition in register_map.get_all()
            }
        )

    for address, name in address_names.items():
        _move_unique_id(
            registry,
            entry.entry_id,
            "sensor",
            (
                f"{DOMAIN}_modbus_{address}",
                f"{entry_prefix}modbus_{address}",
            ),
            f"{entry_prefix}{name}",
        )

    for (address, bit), name in _BINARY_SENSOR_NAMES.items():
        bit_suffix = f"_{bit}" if bit is not None else ""
        _move_unique_id(
            registry,
            entry.entry_id,
            "binary_sensor",
            (
                f"{DOMAIN}_binary_{address}{bit_suffix}",
                f"{entry_prefix}binary_{address}{bit_suffix}",
            ),
            f"{entry_prefix}{name}{bit_suffix}",
        )

    for address, name in _SWITCH_NAMES.items():
        _move_unique_id(
            registry,
            entry.entry_id,
            "switch",
            (f"{entry_prefix}modbus_{address}",),
            f"{entry_prefix}{name}",
        )

    for loop_number in range(1, 5):
        _merge_alias_into_canonical(
            registry,
            entry.entry_id,
            "sensor",
            f"{entry_prefix}loop_{loop_number}_temp",
            f"{entry_prefix}loop_{loop_number}_temperature",
        )

    for suffix in _ENERGY_SUFFIXES:
        _move_unique_id(
            registry,
            entry.entry_id,
            "sensor",
            (f"{DOMAIN}_{suffix}",),
            f"{entry_prefix}{suffix}",
        )

    # The Cloud exposes the reservoir page even when no buffer tank exists.
    # A 500°C current value is its sentinel; remove the previously-created
    # phantom registry entity so it does not remain as an unavailable duplicate.
    reservoir_data = ((coordinator.data or {}).get("reservoir") or {}).get(
        "HeatingCircleData"
    ) or {}
    try:
        reservoir_sentinel = float(reservoir_data.get("circle_calc_temp")) >= 500.0
    except (TypeError, ValueError):
        reservoir_sentinel = False

    if reservoir_sentinel and not getattr(coordinator, "reservoir_installed", False):
        unique_id = f"{entry_prefix}reservoir_climate"
        entity_id = registry.async_get_entity_id("climate", DOMAIN, unique_id)
        entity_entry = registry.async_get(entity_id) if entity_id else None
        if entity_entry and entity_entry.config_entry_id == entry.entry_id:
            registry.async_remove(entity_id)
            _LOGGER.info("Removed phantom reservoir entity %s", entity_id)
