"""Override Logbook so Activity uses the intersection of two entity filters.

Purpose and behavior
--------------------
Core normally merges Recorder's and Logbook's include/exclude lists, allowing
Recorder includes to defeat Logbook exclusions. This component evaluates each
configuration separately and requires both to permit an entity. Historical SQL
queries and the default live Activity stream therefore use the same entity
eligibility: an unrecorded media-player state change cannot appear only in the
live feed, and a recorded motion sensor can still be hidden by Logbook.

Core Logbook performs normal validation, panel/API/service registration and
platform event discovery. We delegate setup with a shallow configuration copy
without Recorder, then replace only Logbook's SQL and live filter fields with
their intersections. Recorder receives its original configuration; we do not
modify its filter objects, recording policy, implementation or database. Core's
include/exclude precedence is retained within each separate filter.

Boundaries and error handling
-----------------------------
This controls the default Activity feed. Core's explicitly targeted entity,
device and context requests may bypass configured filters. Recorder commit
delays and core's other display rules still apply. Events without entity IDs
retain Recorder's normal treatment and Logbook's domain filtering, including
custom logbook.log entries. Recorder's exclude.event_types is not added to live
stream filtering by this component.

Absent filters are normal when no restrictions exist. Unexpected missing
filters/configuration or invalid live predicates log ERROR and abort setup;
core setup exceptions log a traceback and propagate. The custom-component
startup notice is WARNING. These guards detect some structural failures, but
cannot detect every change in core's filtering behavior.

Core upgrade review (source checked against 2026.9.3)
---------------------------------------------------
These are internal Python interfaces, not a supported filter-extension API.
Review CONFIG_SCHEMA and async_setup; Recorder's extraction/SQL builders;
EntityFilter.empty_filter/get_filter; Filters construction, has_config and SQL
predicate methods; and the mutable LogbookConfig fields in hass.data["logbook"].
In particular, REST/WebSocket consumers must still read the replaced fields
after setup rather than cache the original filters. Core 2026.9.3's REST view
stores original filter fields, but its request handler does not consume them.
Review SQL query-cache identity/immutability requirements, synthetic domain._
checks, entity-less event handling and targeted-request bypass behavior too.

The manifest must still override the built-in logbook domain while allowing
direct imports of homeassistant.components.logbook. Review its dependencies and
the copied services.yaml, icons.json and translations/en.json on upgrades.
Repeat local checks and manually verify motion History, motion exclusion,
unrecorded media-player exclusion live/after refresh, included entities and
custom logbook.log entries. See README.md for checks and rollback instructions.
"""

from collections.abc import Callable
import logging

# Upgrade: this direct import must remain separate from HA's custom-domain loader.
from homeassistant.components import logbook as core_logbook
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.filters import (
    Filters,
    extract_include_exclude_filter_conf,
    sqlalchemy_filter_from_include_exclude_conf,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entityfilter import convert_include_exclude_filter
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

# Keep core validation, API registration, event descriptions and services.
# Upgrade: confirm core still exports CONFIG_SCHEMA and accepts our setup config.
CONFIG_SCHEMA = core_logbook.CONFIG_SCHEMA


class IntersectionFilters(Filters):
    """Compose core SQL predicates without merging their include/exclude lists."""

    def __init__(self, recorder: Filters, logbook: Filters) -> None:
        # Upgrade: review Filters construction and query-cache requirements.
        # Keep both component filters unchanged after this object is constructed.
        super().__init__()
        self._recorder = recorder
        self._logbook = logbook

    @property
    def has_config(self) -> bool:
        # Upgrade: core's all-entity query must still use this flag to apply SQL.
        return True

    def states_metadata_entity_filter(self):
        # Upgrade: this method must still return an AND-composable SQL predicate.
        return (
            self._recorder.states_metadata_entity_filter()
            & self._logbook.states_metadata_entity_filter()
        )

    def events_entity_filter(self):
        # Upgrade: check event-ID columns and core's entity-less event semantics.
        return (
            self._recorder.events_entity_filter()
            & self._logbook.events_entity_filter()
        )


def _intersect_live_filters(
    recorder: Callable[[str], bool], logbook: Callable[[str], bool] | None
) -> Callable[[str], bool]:
    """Filter entities, retaining core's handling of events without entity IDs."""
    def allowed(entity_id: str) -> bool:
        if logbook is not None and not logbook(entity_id):
            return False
        # Core checks an event's domain using a synthetic domain._ entity ID.
        # Recorder's entity filter does not reject events without entity IDs;
        # its SQL event filter likewise permits them, including logbook.log.
        # Upgrade: recheck this synthetic marker and Recorder's event treatment.
        return entity_id.endswith("._") or recorder(entity_id)

    return allowed


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Delegate to core with a private configuration view for Logbook only."""
    _LOGGER.warning(
        "Using a custom Logbook component with intersected Recorder and Logbook filters"
    )
    # Upgrade: extraction must copy rules without changing Recorder's config.
    recorder_conf = extract_include_exclude_filter_conf(
        config.get(RECORDER_DOMAIN) or {}
    )
    # Upgrade: conversion must retain core precedence and the EntityFilter API.
    recorder_entities = convert_include_exclude_filter(recorder_conf)
    if recorder_entities is None:
        _LOGGER.error("Recorder entity filter builder returned None; Logbook setup aborted")
        return False

    recorder_sql = None
    recorder_live = None
    if not recorder_entities.empty_filter:
        # Upgrade: active rules must yield SQL filters and a callable live filter.
        recorder_sql = sqlalchemy_filter_from_include_exclude_conf(recorder_conf)
        recorder_live = recorder_entities.get_filter()
        if recorder_sql is None or not callable(recorder_live):
            _LOGGER.error(
                "Recorder has entity restrictions but did not provide SQL and live "
                "filters; Logbook setup aborted"
            )
            return False

    # Remove Recorder only from the private view passed to core Logbook setup.
    logbook_config = dict(config)
    logbook_config.pop(RECORDER_DOMAIN, None)
    try:
        # Upgrade: check the signature and that setup finishes registration here.
        setup_succeeded = await core_logbook.async_setup(hass, logbook_config)
    except Exception:
        _LOGGER.exception("Core Logbook setup raised an exception; custom setup aborted")
        raise
    if not setup_succeeded:
        _LOGGER.error("Core Logbook setup did not succeed; custom Logbook setup aborted")
        return False

    if recorder_entities.empty_filter:
        return True

    # Core REST queries and WebSocket queries/streams read this LogbookConfig.
    # Only Logbook's objects change; Recorder's filter/configuration is untouched.
    # Upgrade: key/fields must remain mutable and consumers must read them AFTER
    # replacement. Caching original filters during setup could silently bypass us.
    activity = hass.data.get("logbook")
    if activity is None:
        _LOGGER.error(
            "Core Logbook setup succeeded without a Logbook configuration; "
            "custom Logbook setup aborted"
        )
        return False
    try:
        logbook_sql = activity.sqlalchemy_filter
        logbook_live = activity.entity_filter
    except AttributeError:
        _LOGGER.exception(
            "Core Logbook configuration lacks the expected filter fields; "
            "custom Logbook setup aborted"
        )
        return False
    if logbook_live is not None and not callable(logbook_live):
        _LOGGER.error("Core Logbook live filter is not callable; custom setup aborted")
        return False

    # Upgrade: historical queries must consume these two Filters methods; targeted
    # entity/device/context queries currently retain core's bypass behavior.
    activity.sqlalchemy_filter = (
        IntersectionFilters(recorder_sql, logbook_sql)
        if logbook_sql is not None
        else recorder_sql
    )
    # Upgrade: the default stream must still use this callable for live events.
    activity.entity_filter = _intersect_live_filters(
        recorder_live, logbook_live
    )
    return True
