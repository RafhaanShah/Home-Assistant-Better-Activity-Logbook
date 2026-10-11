# Activity (Fixed Filters)

A custom Home Assistant Logbook integration that keeps Activity useful without
changing the history you record.

An entity appears in the default Activity feed only when **both Recorder and
Logbook allow it**. Keep motion history in Recorder while hiding it from Activity,
without Recorder's includes overriding Logbook's exclusions. Entities that
Recorder does not record are also hidden from the live feed, so they do not
appear temporarily and disappear when you refresh.

## Why this exists

Recorder and Activity serve different purposes. You may want detailed motion
history for troubleshooting or automations, while keeping frequent motion
changes out of the Activity feed. Excluding a sensor from Recorder would also
remove the history you want to retain, so hiding it should be a Logbook decision.

Core Logbook [combines Recorder and Logbook filter lists before evaluating them](https://github.com/home-assistant/core/blob/2026.9.3/homeassistant/components/logbook/__init__.py).
Some include rules take precedence over exclusions. When the two configurations
are combined, a Recorder include can therefore keep an entity visible even
though Logbook excludes it. For example, including `binary_sensor` in Recorder
and excluding that domain in Logbook can still leave motion events in Activity.
This behaviour is straight up wrong to any normal user's expectation, and 
undocumented as a pitfall.

The filtering history and reports of unexpected results are documented upstream:

- [Issue #73058](https://github.com/home-assistant/core/issues/73058) reports
  Activity showing state changes outside the configured Logbook includes after
  upgrading to Home Assistant 2022.6.
- [Issue #80716](https://github.com/home-assistant/core/issues/80716) reports
  entities excluded by Logbook still appearing when its filters are combined
  with Recorder's filters.
- [PR #74080](https://github.com/home-assistant/core/pull/74080) changed entity
  filter precedence in 2022, giving includes priority in specified cases. This
  provides context for how combined rules are evaluated. This component keeps
  that precedence within each separate filter.

This component addresses both needs by requiring an entity to pass Recorder's
filter **and** Logbook's filter. Recorder decides which entity history is stored;
Logbook can further narrow what appears in the default Activity feed. The same
intersection is applied to historical queries and live updates, without changing
Recorder or its stored history. Home Assistant's normal precedence is preserved
within each separate filter.

## Features

- Applies Recorder and Logbook entity filters together in historical and live Activity.
- Keeps your existing `recorder:` and `logbook:` YAML configuration.
- Leaves Recorder's recording policy and stored history unchanged.
- Retains the built-in Activity panel and `logbook.log` action.
- Logs a startup warning so you can confirm the custom component is loaded.

| Recorder allows the entity | Logbook allows the entity | Default Activity |
| --- | --- | --- |
| Yes | Yes | Shown |
| Yes | No | Hidden |
| No | Yes | Hidden |
| No | No | Hidden |

## Requirements

- Home Assistant **20XX.X.X or newer**, as declared in the HACS metadata.
- Recorder and Logbook enabled.
- [HACS](https://www.hacs.dev/) installed if using the HACS method.

Newer releases need compatibility review; the minimum version is not 
a guarantee that every future Core release will work.

This component overrides the built-in `logbook` integration. It uses internal
Home Assistant interfaces, so check project release notes before upgrading Core.

## Installation

### HACS custom repository

These instructions apply once the GitHub repository is published. Replace
`YOUR_GITHUB_USERNAME/YOUR_REPOSITORY` with the project's repository.

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and select **Custom repositories**.
3. Enter `https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY`.
4. Select **Integration** as the repository type, then select **Add**.
5. Find **Activity (Fixed Filters)** in HACS, open it, and download the integration.
6. Configure your Logbook filters as described below, then restart Home Assistant.
7. Check **Settings → System → Logs** for:

   ```text
   Using a custom Logbook component with intersected Recorder and Logbook filters
   ```

HACS documents this process in its [custom repository guide](https://www.hacs.dev/docs/faq/custom_repositories/).
This project is distributed through a custom repository: HACS does not accept
core integration overrides into its [default catalog](https://www.hacs.dev/docs/publish/include/).

No additional **Add integration** step is required. The component uses the
existing `logbook:` YAML configuration.

### Manual installation

1. Download the project from
   [GitHub](https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY).
2. Copy its `custom_components/logbook/` directory into your Home Assistant
   configuration directory, producing `/config/custom_components/logbook/`.
   If that directory already contains another Logbook override, back it up first.
3. Configure your filters, restart Home Assistant, and check the startup warning.

## Configuration

Use Home Assistant's normal Recorder and Logbook options. There are no additional
configuration keys for this component. Edit existing sections instead of creating
duplicate `recorder:` or `logbook:` keys.

For example, record lights and binary sensors but hide binary sensors from Activity:

```yaml
recorder:
  include:
    domains:
      - light
      - binary_sensor

logbook:
  exclude:
    domains:
      - binary_sensor
```

With this configuration:

- A motion sensor such as `binary_sensor.living_room_motion_presence` keeps its
  Recorder history but is hidden from Activity.
- Lights appear in both History and Activity.
- Media-player state changes are hidden from Activity because Recorder's include
  list does not permit them.

To hide one entity instead of a whole domain, use an entity exclusion:

```yaml
logbook:
  exclude:
    entities:
      - binary_sensor.living_room_motion_presence
```

Normal include/exclude precedence applies separately within each filter. See the
[Recorder documentation](https://www.home-assistant.io/integrations/recorder/#configure-filter)
and [Logbook documentation](https://www.home-assistant.io/integrations/logbook/#configure-filter)
for supported domains, entities and entity globs.


## Limitations

- Explicitly targeted entity, device or context requests can bypass configured
  Activity filters, following core behavior. This component is not an access-control mechanism.
- Events without entity IDs follow Recorder's normal event behavior and Logbook's
  domain filtering. They are not treated as entity state changes.
- Recorder's `exclude.event_types` is not applied to live streams by this component.
- Recorder commit delays and core's normal exclusions of continuous sensors still apply.
- Bundled custom action translations are currently English only.

## Troubleshooting

**The startup warning is missing:** confirm the files are installed at
`/config/custom_components/logbook/`, Logbook is enabled, and Home Assistant has
been restarted. Custom integrations are not loaded in safe mode.

**Setup logs an error:** check the accompanying message or traceback in Home
Assistant's logs. Report your Core version and relevant log excerpt; remove
secrets and personal information. Unexpected filter/setup failures are logged
at ERROR rather than silently relaxing the intersection.

**An entity is unexpectedly hidden:** it must pass both sets of rules. Check
Recorder's includes/excludes as well as Logbook's and the Activity UI filters.

**An entity still appears in a card or entity view:** test the default Activity
feed without targets. Explicitly targeted views can use core's filter bypass.

## Updates and removal

For HACS installations, download project updates through HACS and restart Home
Assistant. For manual installations, replace the component files and restart.
Recheck the verification steps after project or Core updates.

To restore built-in Logbook, uninstall the downloaded integration through HACS,
or remove `/config/custom_components/logbook/`, then restart Home Assistant.
Removing only the custom-repository entry from HACS does not remove installed
files. If keeping a backup, move it outside `custom_components/`; renaming the
folder inside that directory is insufficient because it still declares `logbook`.
Recorder configuration and stored history need no rollback.

## License

[MIT License](https://choosealicense.com/licenses/mit/)
