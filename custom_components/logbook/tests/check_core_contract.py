"""Source-level contract check; this does not boot Home Assistant."""

from __future__ import annotations
import ast
import asyncio
from collections.abc import Callable, Collection, Iterable
from copy import deepcopy
import fnmatch
from functools import lru_cache, partial
import importlib.util
import json
import operator
from pathlib import Path
import re
from types import SimpleNamespace, ModuleType
from typing import Any
from unittest.mock import AsyncMock, Mock
import sys
import sqlalchemy as sa

import argparse

parser = argparse.ArgumentParser(description='Exercise selected core functions with stubbed HA plumbing.')
parser.add_argument('--core-dir', type=Path, required=True)
parser.add_argument('--recorder-filters', type=Path, required=True)
parser.add_argument('--entity-filter', type=Path, required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[3]
CORE = args.core_dir
ns = dict(globals())
ns.update({key: key.removeprefix('CONF_').lower() for key in ('CONF_DOMAINS', 'CONF_ENTITIES', 'CONF_INCLUDE', 'CONF_EXCLUDE', 'CONF_ENTITY_GLOBS', 'CONF_INCLUDE_DOMAINS', 'CONF_INCLUDE_ENTITIES', 'CONF_INCLUDE_ENTITY_GLOBS', 'CONF_EXCLUDE_DOMAINS', 'CONF_EXCLUDE_ENTITIES', 'CONF_EXCLUDE_ENTITY_GLOBS')})
ns.update(split_entity_id=lambda entity: entity.split('.', 1), MAX_EXPECTED_ENTITY_IDS=10000, override=lambda f: f, callback=lambda f: f, json_dumps=json.dumps, JSON_NULL='null', Column=sa.Column, Text=sa.Text, cast=sa.cast, not_=sa.not_, or_=sa.or_, StatesMeta=SimpleNamespace(entity_id=sa.column('entity_id')))
ns.update(ENTITY_ID_IN_EVENT=sa.column('entity_id_in_event'), OLD_ENTITY_ID_IN_EVENT=sa.column('old_entity_id_in_event'))

def load_nodes(path, predicate):
    tree = ast.parse(path.read_text())
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    nodes.extend(n for n in tree.body if predicate(n))
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), 'exec'), ns)

load_nodes(args.entity_filter, lambda n: isinstance(n, (ast.FunctionDef, ast.ClassDef)))
load_nodes(args.recorder_filters, lambda n: isinstance(n, (ast.FunctionDef, ast.ClassDef)) or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'GLOB_TO_SQL_CHARS', 'FILTER_TYPES', 'FITLER_MATCHERS'} for t in n.targets))

panels, websockets, rest = Mock(), Mock(), Mock()
ns.update(frontend=SimpleNamespace(async_register_built_in_panel=panels), websocket_api=SimpleNamespace(async_setup=websockets), rest_api=SimpleNamespace(async_setup=rest), RECORDER_DOMAIN='recorder', DOMAIN='logbook', LogbookConfig=lambda external, sql, entity: SimpleNamespace(external_events=external, sqlalchemy_filter=sql, entity_filter=entity), async_process_integration_platforms=AsyncMock(), _process_logbook_platform=Mock(), LOG_MESSAGE_SCHEMA=object(), async_log_entry=Mock(), ATTR_MESSAGE='message', ATTR_NAME='name', ATTR_DOMAIN='domain', ATTR_ENTITY_ID='entity_id')
load_nodes(CORE / '__init__.py', lambda n: isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_setup')
core = ModuleType('homeassistant.components.logbook')
core.async_setup = ns['async_setup']
core.CONFIG_SCHEMA = object()
for name in ('homeassistant', 'homeassistant.components', 'homeassistant.components.recorder', 'homeassistant.components.recorder.filters', 'homeassistant.core', 'homeassistant.helpers', 'homeassistant.helpers.typing', 'homeassistant.helpers.entityfilter'):
    sys.modules[name] = ModuleType(name)
sys.modules['homeassistant.components'].logbook = core
sys.modules['homeassistant.components.recorder'].DOMAIN = 'recorder'
sys.modules['homeassistant.core'].HomeAssistant = SimpleNamespace
sys.modules['homeassistant.helpers.typing'].ConfigType = dict
for name in ('Filters', 'extract_include_exclude_filter_conf', 'sqlalchemy_filter_from_include_exclude_conf'):
    setattr(sys.modules['homeassistant.components.recorder.filters'], name, ns[name])
sys.modules['homeassistant.helpers.entityfilter'].convert_include_exclude_filter = ns['convert_include_exclude_filter']
spec = importlib.util.spec_from_file_location('override', ROOT / 'custom_components/logbook/__init__.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)

# Parse current YAML, ignoring the db_url secret rather than exposing it.
import yaml
class Loader(yaml.SafeLoader): pass
Loader.add_constructor('!secret', lambda loader, node: '<secret>')
config = {}
for name in ('recorder', 'logbook'):
    config.update(yaml.load((ROOT / f'integrations/{name}.yaml').read_text(), Loader=Loader))
original = deepcopy(config)
entity = 'binary_sensor.living_room_motion_presence'
allowed = 'light.contract_check'
unrecorded = 'media_player.contract_check'
hass = SimpleNamespace(data={}, services=SimpleNamespace(async_register=Mock()))

async def check():
    # Demonstrate the actual pre-change failure using core setup.
    await core.async_setup(hass, config)
    assert hass.data['logbook'].entity_filter(entity), 'Baseline must reproduce included motion sensor'
    for mock in (panels, websockets, rest, hass.services.async_register, ns['async_process_integration_platforms']): mock.reset_mock()
    assert await wrapper.async_setup(hass, config)
    assert config == original
    state = hass.data['logbook']
    assert not state.entity_filter(entity)
    assert state.entity_filter(allowed)
    assert not state.entity_filter(unrecorded)
    for mock in (panels, websockets, rest, hass.services.async_register): mock.assert_called_once()
    ns['async_process_integration_platforms'].assert_awaited_once()
    assert 'recorder' not in rest.call_args.args[1]
    assert rest.call_args.args[2] is state.sqlalchemy_filter._logbook
    # Run core's SQL expression against real SQLite rows.
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE entities (entity_id TEXT)')
        conn.execute(sa.text('INSERT INTO entities VALUES (:entity)'), [{'entity': entity}, {'entity': allowed}, {'entity': unrecorded}])
        result = conn.execute(sa.select(sa.column('entity_id')).select_from(sa.table('entities')).where(state.sqlalchemy_filter.states_metadata_entity_filter())).scalars().all()
        assert result == [allowed], result
        # Core event predicates admit entity-less custom entries, while both
        # filters must permit events that reference an entity.
        conn.exec_driver_sql('CREATE TABLE events (entity_id_in_event TEXT, old_entity_id_in_event TEXT)')
        conn.execute(
            sa.text('INSERT INTO events VALUES (:entity, NULL)'),
            [{'entity': json.dumps(eid)} for eid in (entity, allowed, unrecorded)] + [{'entity': None}],
        )
        event_rows = conn.execute(
            sa.select(sa.column('entity_id_in_event')).select_from(sa.table('events')).where(state.sqlalchemy_filter.events_entity_filter())
        ).scalars().all()
        assert event_rows == [json.dumps(allowed), None], event_rows
    recorder_filter = ns['convert_include_exclude_filter'](ns['extract_include_exclude_filter_conf'](config['recorder']))
    assert recorder_filter(entity), 'Recorder still includes the test sensor'
    # Exercise the exact core subscription functions, stubbing only bus/HA plumbing.
    ns.update(is_callback=lambda f: True, EVENT_STATE_CHANGED='state_changed', extract_attr=lambda event_type, data, key: [data[key]] if key in data else [], ATTR_DEVICE_ID='device_id', _is_state_filtered=lambda new, old: False)
    load_nodes(CORE / 'helpers.py', lambda n: isinstance(n, ast.FunctionDef) and n.name in {'event_forwarder_filtered', 'async_subscribe_events'})
    listeners = {}
    hass.bus = SimpleNamespace(async_listen=lambda event_type, callback: listeners.setdefault(event_type, callback))
    delivered = []
    ns['async_subscribe_events'](hass, [], delivered.append, {'logbook_entry'}, state.entity_filter, None, None)
    for sensor in (entity, allowed, unrecorded):
        listeners['state_changed'](SimpleNamespace(data={'old_state': SimpleNamespace(state='off'), 'new_state': SimpleNamespace(state='on', entity_id=sensor)}))
    assert len(delivered) == 1 and delivered[0].data['new_state'].entity_id == allowed
    listeners['logbook_entry'](SimpleNamespace(event_type='logbook_entry', data={'domain': 'logbook', 'message': 'custom entry'}))
    assert len(delivered) == 2
    listeners['logbook_entry'](SimpleNamespace(event_type='logbook_entry', data={'entity_id': entity}))
    assert len(delivered) == 2
    listeners['logbook_entry'](SimpleNamespace(event_type='logbook_entry', data={'entity_id': unrecorded}))
    assert len(delivered) == 2
    callback = hass.services.async_register.call_args.args[2]
    callback(SimpleNamespace(data={'name': 'contract', 'message': 'check'}, context=None))
    assert ns['async_log_entry'].call_args.args[3] == 'logbook'
    # Missing filters remain a supported no-filter configuration.
    assert await wrapper.async_setup(hass, {'recorder': config['recorder']})
    assert hass.data['logbook'].entity_filter(entity)
    assert not hass.data['logbook'].entity_filter(unrecorded)
    assert await wrapper.async_setup(hass, {})
    assert hass.data['logbook'].entity_filter is None
    assert hass.data['logbook'].sqlalchemy_filter is None
    # Includes on either side cannot defeat exclusions or eligibility on the
    # other side, including glob rules and an unrestricted Recorder.
    cases = [
        ({'recorder': {'exclude': {'entities': [entity]}}, 'logbook': {'include': {'entities': [entity]}}}, set()),
        ({'recorder': {'include': {'domains': ['light']}}, 'logbook': {'include': {'entities': [unrecorded]}}}, set()),
        ({'recorder': {'include': {'entity_globs': ['*.contract_check']}}, 'logbook': {'exclude': {'domains': ['media_player']}}}, {allowed}),
        ({'logbook': {'exclude': {'domains': ['binary_sensor']}}}, {allowed, unrecorded}),
    ]
    for rules, expected in cases:
        saved = deepcopy(rules)
        assert await wrapper.async_setup(hass, rules)
        filters = hass.data['logbook']
        assert {eid for eid in (entity, allowed, unrecorded) if filters.entity_filter(eid)} == expected
        with engine.connect() as conn:
            rows = set(conn.execute(sa.select(sa.column('entity_id')).select_from(sa.table('entities')).where(filters.sqlalchemy_filter.states_metadata_entity_filter())).scalars())
            assert rows == expected, (rules, rows, expected)
        assert rules == saved
    assert config == original
    print('PASS: Core 2026.9.3 baseline, intersection, original Recorder config, SQL filtering of motion/media-player rows, default live state/custom-event filters, log service, Recorder-only filters, and empty filters')

asyncio.run(check())
