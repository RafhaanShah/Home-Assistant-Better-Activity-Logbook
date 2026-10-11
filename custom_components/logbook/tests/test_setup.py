"""Check the wrapper's configuration boundary without a running HA instance."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


class SetupTests(unittest.IsolatedAsyncioTestCase):
    """Recorder must always retain the original configuration."""

    def setUp(self):
        self.core = ModuleType("homeassistant.components.logbook")
        self.core.CONFIG_SCHEMA = object()
        self.recorder_predicate = lambda entity: entity.startswith("binary_sensor.")
        self.logbook_predicate = lambda entity: not entity.startswith("binary_sensor.")
        self.recorder_sql = FakeFilters()

        async def setup(hass, config):
            hass.data["logbook"] = SimpleNamespace(
                sqlalchemy_filter=FakeFilters(), entity_filter=self.logbook_predicate
            )
            return True

        self.core.async_setup = AsyncMock(side_effect=setup)
        modules = {
            name: ModuleType(name)
            for name in (
                "homeassistant",
                "homeassistant.components",
                "homeassistant.components.recorder",
                "homeassistant.components.recorder.filters",
                "homeassistant.core",
                "homeassistant.helpers",
                "homeassistant.helpers.typing",
                "homeassistant.helpers.entityfilter",
            )
        }
        modules["homeassistant.components"].logbook = self.core
        modules["homeassistant.components.logbook"] = self.core
        modules["homeassistant.components.recorder"].DOMAIN = "recorder"
        modules["homeassistant.core"].HomeAssistant = SimpleNamespace
        modules["homeassistant.helpers.typing"].ConfigType = dict
        filters = modules["homeassistant.components.recorder.filters"]
        filters.Filters = FakeFilters
        filters.extract_include_exclude_filter_conf = lambda config: config
        filters.sqlalchemy_filter_from_include_exclude_conf = lambda config: self.recorder_sql
        modules["homeassistant.helpers.entityfilter"].convert_include_exclude_filter = (
            lambda config: SimpleNamespace(
                empty_filter=not config,
                get_filter=lambda: self.recorder_predicate,
            )
        )
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                "logbook_override_test", Path(__file__).parents[1] / "__init__.py"
            )
            self.wrapper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.wrapper)

    async def test_config_is_private_and_recorder_is_unchanged(self):
        recorder = {"include": {"domains": ["binary_sensor"]}}
        logbook = {"exclude": {"domains": ["binary_sensor"]}}
        config = {"recorder": recorder, "logbook": logbook, "other": {}}
        hass = SimpleNamespace(data={})
        self.assertTrue(await self.wrapper.async_setup(hass, config))
        self.core.async_setup.assert_awaited_once()
        delegated_hass, delegated_config = self.core.async_setup.call_args.args
        self.assertIs(delegated_hass, hass)
        self.assertIsNot(delegated_config, config)
        self.assertEqual(delegated_config, {"logbook": logbook, "other": {}})
        self.assertIs(delegated_config["logbook"], logbook)
        self.assertIs(config["recorder"], recorder)
        self.assertEqual(recorder, {"include": {"domains": ["binary_sensor"]}})
        delegated_config.clear()
        self.assertEqual(set(config), {"recorder", "logbook", "other"})
        self.assertIs(self.wrapper.CONFIG_SCHEMA, self.core.CONFIG_SCHEMA)
        self.assertFalse(hass.data["logbook"].entity_filter("binary_sensor.motion"))
        self.assertFalse(hass.data["logbook"].entity_filter("media_player.tv"))

    async def test_missing_recorder_and_logbook(self):
        config = {"other": {}}
        await self.wrapper.async_setup(SimpleNamespace(data={}), config)
        delegated_config = self.core.async_setup.call_args.args[1]
        self.assertEqual(delegated_config, config)
        self.assertIsNot(delegated_config, config)

    async def test_false_result_is_propagated(self):
        self.core.async_setup.side_effect = None
        self.core.async_setup.return_value = False
        self.assertFalse(await self.wrapper.async_setup(SimpleNamespace(), {}))

    async def test_core_failure_keeps_recorder_config(self):
        self.core.async_setup.side_effect = RuntimeError("setup failed")
        config = {"recorder": {"include": {"domains": ["binary_sensor"]}}}
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                await self.wrapper.async_setup(SimpleNamespace(), config)
        self.assertIn("Core Logbook setup raised an exception", logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertEqual(
            config, {"recorder": {"include": {"domains": ["binary_sensor"]}}}
        )

    async def test_none_recorder_section_is_unrestricted(self):
        hass = SimpleNamespace(data={})
        self.assertTrue(await self.wrapper.async_setup(hass, {"recorder": None}))
        self.assertIs(hass.data["logbook"].entity_filter, self.logbook_predicate)

    async def test_missing_recorder_sql_aborts_before_core_setup(self):
        self.recorder_sql = None
        config = {"recorder": {"include": {"domains": ["binary_sensor"]}}}
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR") as logs:
            self.assertFalse(await self.wrapper.async_setup(SimpleNamespace(data={}), config))
        self.assertIn("did not provide SQL and live filters", logs.output[0])
        self.core.async_setup.assert_not_awaited()
        self.assertEqual(config["recorder"]["include"]["domains"], ["binary_sensor"])

    async def test_missing_recorder_live_aborts_before_core_setup(self):
        self.recorder_predicate = None
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR"):
            self.assertFalse(await self.wrapper.async_setup(
                SimpleNamespace(data={}), {"recorder": {"include": {"domains": ["light"]}}}
            ))
        self.core.async_setup.assert_not_awaited()

    async def test_none_recorder_filter_builder_result(self):
        with patch.object(self.wrapper, "convert_include_exclude_filter", return_value=None):
            with self.assertLogs(self.wrapper._LOGGER, level="ERROR") as logs:
                self.assertFalse(await self.wrapper.async_setup(SimpleNamespace(data={}), {}))
        self.assertIn("entity filter builder returned None", logs.output[0])
        self.core.async_setup.assert_not_awaited()

    async def test_success_without_logbook_config_is_an_error(self):
        self.core.async_setup.side_effect = None
        self.core.async_setup.return_value = True
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR") as logs:
            self.assertFalse(await self.wrapper.async_setup(
                SimpleNamespace(data={}), {"recorder": {"include": {"domains": ["light"]}}}
            ))
        self.assertIn("without a Logbook configuration", logs.output[0])

    async def test_none_logbook_filters_use_recorder_only(self):
        async def setup(hass, config):
            hass.data["logbook"] = SimpleNamespace(sqlalchemy_filter=None, entity_filter=None)
            return True

        self.core.async_setup.side_effect = setup
        hass = SimpleNamespace(data={})
        self.assertTrue(await self.wrapper.async_setup(
            hass, {"recorder": {"include": {"domains": ["binary_sensor"]}}}
        ))
        self.assertIs(hass.data["logbook"].sqlalchemy_filter, self.recorder_sql)
        self.assertTrue(hass.data["logbook"].entity_filter("binary_sensor.motion"))
        self.assertFalse(hass.data["logbook"].entity_filter("media_player.tv"))

    async def test_missing_logbook_filter_fields_log_traceback(self):
        async def setup(hass, config):
            hass.data["logbook"] = SimpleNamespace()
            return True

        self.core.async_setup.side_effect = setup
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR") as logs:
            self.assertFalse(await self.wrapper.async_setup(
                SimpleNamespace(data={}), {"recorder": {"include": {"domains": ["light"]}}}
            ))
        self.assertIsNotNone(logs.records[0].exc_info)

    async def test_invalid_logbook_live_filter_is_not_replaced(self):
        self.logbook_predicate = "invalid"
        hass = SimpleNamespace(data={})
        with self.assertLogs(self.wrapper._LOGGER, level="ERROR"):
            self.assertFalse(await self.wrapper.async_setup(
                hass, {"recorder": {"include": {"domains": ["light"]}}}
            ))
        self.assertEqual(hass.data["logbook"].entity_filter, "invalid")

    def test_live_filter_requires_both_permissions(self):
        recorder = lambda entity: entity in {"light.allowed", "binary_sensor.motion"}
        logbook = lambda entity: entity != "binary_sensor.motion"
        allowed = self.wrapper._intersect_live_filters(recorder, logbook)
        self.assertTrue(allowed("light.allowed"))
        self.assertFalse(allowed("binary_sensor.motion"))
        self.assertFalse(allowed("media_player.tv"))
        self.assertTrue(allowed("logbook._"))
        self.assertFalse(
            self.wrapper._intersect_live_filters(recorder, lambda entity: False)("logbook._")
        )

    def test_sql_predicates_are_intersected(self):
        sql = self.wrapper.IntersectionFilters(FakeFilters(0b1100), FakeFilters(0b1010))
        self.assertTrue(sql.has_config)
        self.assertEqual(sql.states_metadata_entity_filter(), 0b1000)
        self.assertEqual(sql.events_entity_filter(), 0b1000)


class FakeFilters:
    """Provide bit masks for checking AND composition without dependencies."""

    def __init__(self, mask=0b1111):
        self.mask = mask

    def states_metadata_entity_filter(self):
        return self.mask

    def events_entity_filter(self):
        return self.mask


if __name__ == "__main__":
    unittest.main()
