# NetzNÖ Smartmeter Integration for Home Assistant
![Hassfest](https://github.com/TobiKr/hass-NetzNOESmartMeter/actions/workflows/hassfest.yml/badge.svg)
![Validate](https://github.com/TobiKr/hass-NetzNOESmartMeter/actions/workflows/validate.yml/badge.svg)
![Release](https://github.com/TobiKr/hass-NetzNOESmartMeter/actions/workflows/release.yml/badge.svg)

## About

This repo contains a custom component for [Home Assistant](https://www.home-assistant.io) for exposing a sensor
providing information about a registered [NetzNÖ Smartmeter](https://www.netz-noe.at/smartmeter).

The integration syncs all consumption data in Home Assistant, which allows a hourly view on consumption data in Home Assistant:
![Screenshot of the energy dashboard, showing hourly consumption measured by a NetzNÖ SmartMeter](/docs/netznoe-energyusage.png)

## Acknowledgments

This integration is based on the excellent [Wiener Netze Smartmeter](https://github.com/DarwinsBuddy/WienerNetzeSmartmeter) integration by [DarwinsBuddy](https://github.com/DarwinsBuddy) and contributors. We are grateful for their work which served as the foundation for this Netz NÖ adaptation.

## Installation

### Manual

Copy `<project-dir>/custom_components/netznoe` into `<home-assistant-root>/config/custom_components`

### HACS
1. Add this repository as a custom repository in HACS
2. Search for `NetzNÖ Smartmeter` or `netznoe` in HACS
3. Install
4. Restart Home Assistant
5. Configure the integration

## Configure

You can choose between UI configuration or manual (by adding your credentials to `configuration.yaml` and `secrets.yaml` resp.)
After successful configuration you can add sensors to your favourite dashboard, or even to your energy dashboard to track your total consumption.

### UI
1. Navigate to Settings > Devices & Services > Add Integration
2. Search for "NetzNÖ" and add the integration
3. Enter your NetzNÖ SmartMeter Portal credentials and confirm
4. Adding the SmartMeter can take a couple of minutes as it syncs all existing data

### Manual
See [Example configuration files](example/configuration.yaml)

## Energy communities (Energiegemeinschaft)

If your metering point takes part in an *Erneuerbare-Energie-Gemeinschaft*
(EEG) or a *Bürgerenergiegemeinschaft* (BEG), the portal reports how much of
each quarter hour came from the community instead of from your supplier. The
integration imports three additional statistics for those meters:

| Statistic | Meaning |
|---|---|
| `netznoe:<meter>_self_coverage` | Eigendeckung — energy supplied by the community |
| `netznoe:<meter>_grid_leftover` | Restnetzbezug — what was bought from the supplier |
| `netznoe:<meter>_joint_tenancy` | Ideeller Anteil — the share allocated to you |

The plain consumption keeps its existing `netznoe:<meter>` id, unchanged.
Nothing is added for a metering point without a community: the portal returns
those series as arrays of `null`, and they are skipped rather than imported
as zeroes.

The interesting number is usually `self_coverage / metered` — the share of
your consumption the community covered. Comparing `joint_tenancy` against
`self_coverage` shows the other half of the story: what was allocated to you
but could not be used, because nothing in the house was drawing power at the
time.

These figures are the total across all of your communities. The response
also carries one row per community, so a per-community breakdown of the
self-coverage is possible; it is not exported yet.

Two things worth knowing, both measured rather than assumed:

- **`grid_leftover` is computed as `metered - self_coverage`,** not taken
  from the portal's own `gridUsageLeftoverValues`. On a settled day the two
  agree exactly. A day's figures arrive over several hours, though, and are
  briefly inconsistent while they do: on 2026-10-07 the portal reported a
  leftover of 7.839 kWh against a metered 28.355 in the morning, and a
  consistent 22.016 by the afternoon. The subtraction was right at both
  times.
- **Do not read a community row's `meteredValues` as consumption.** It is
  the consumption divided by the number of communities — with two
  memberships both rows carried exactly half the day's total. Only the
  aggregate row (`ec_id` null) holds the real figure, and only
  `selfCoverageValues` is meaningful per community.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
