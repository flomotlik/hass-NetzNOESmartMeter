"""Historical data importer for Netz NO Smartmeter."""
import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from operator import itemgetter
from typing import Optional

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api.client import derive_series
from .AsyncSmartmeter import AsyncSmartmeter
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: Extra statistics written for metering points in an energy community, on
#: top of the plain consumption. Keys are the suffix appended to the main
#: statistic id; values are the display name.
#:
#: ``grid_leftover`` is computed as ``metered - self_coverage`` rather than
#: read from the portal's own ``gridUsageLeftoverValues``, which agrees on
#: every settled day but not while a day's figures are still arriving. See
#: :func:`..api.client.derive_series`.
EXTRA_STATISTICS = {
    "self_coverage": "Eigendeckung (energy community)",
    "grid_leftover": "Restnetzbezug (grid residual)",
    "joint_tenancy": "Ideeller Anteil (allocated share)",
}

#: The plain consumption keeps the unsuffixed statistic id it always had.
MAIN_SERIES = "metered"


class Importer:
    """Import historical consumption data into Home Assistant statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        async_smartmeter: AsyncSmartmeter,
        metering_point_id: str,
        unit_of_measurement: str,
        has_ftm_meter_data: bool = True,
    ):
        """Initialize the importer.

        Args:
            hass: Home Assistant instance
            async_smartmeter: Async smartmeter client
            metering_point_id: Metering point ID
            unit_of_measurement: Unit of measurement for statistics
            has_ftm_meter_data: True for 15-min interval meters, False for daily meters
        """
        self.id = f"{DOMAIN}:{metering_point_id.lower()}"
        self.metering_point_id = metering_point_id
        self.unit_of_measurement = unit_of_measurement
        self.hass = hass
        self.async_smartmeter = async_smartmeter
        self.has_ftm_meter_data = has_ftm_meter_data

    def series_id(self, series: str) -> str:
        """Statistic id for one series. The main series keeps the bare id."""
        return self.id if series == MAIN_SERIES else f"{self.id}_{series}"

    def get_statistics_metadata(self, series: str = MAIN_SERIES) -> StatisticMetaData:
        """Get statistics metadata for one series."""
        if series == MAIN_SERIES:
            name = f"Netz NO {self.metering_point_id}"
        else:
            name = f"Netz NO {self.metering_point_id} {EXTRA_STATISTICS[series]}"
        return StatisticMetaData(
            source=DOMAIN,
            statistic_id=self.series_id(series),
            name=name,
            unit_of_measurement=self.unit_of_measurement,
            has_mean=False,
            has_sum=True,
        )

    def is_last_inserted_stat_valid(self, last_inserted_stat: dict, stat_id: str) -> bool:
        """Check if last inserted statistics are valid."""
        return (
            len(last_inserted_stat.get(stat_id, [])) == 1
            and "sum" in last_inserted_stat[stat_id][0]
            and "end" in last_inserted_stat[stat_id][0]
        )

    def _parse_end(self, entry: dict) -> Optional[datetime]:
        """Read the 'end' of a statistics entry as an aware datetime."""
        end = entry["end"]
        if isinstance(end, (int, float)):
            end = dt_util.utc_from_timestamp(end)
        if isinstance(end, str):
            end = dt_util.parse_datetime(end)
        if not isinstance(end, datetime):
            _LOGGER.error("Unexpected type for statistics end: %r", entry["end"])
            return None
        return end

    async def _resume_point(self, series: str) -> Optional[tuple[Optional[datetime], Decimal]]:
        """Where to continue one series.

        Returns (None, 0) for a series that has no statistics yet — that asks
        for a full backfill, which is what a series added by an upgrade needs
        so its history reaches as far back as the consumption already does.
        Returns None when the series is up to date and should be skipped.
        """
        stat_id = self.series_id(series)
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum", "state"}
        )
        if not self.is_last_inserted_stat_valid(last, stat_id):
            return (None, Decimal(0))

        entry = last[stat_id][0]
        start = self._parse_end(entry)
        if start is None:
            return (None, Decimal(0))

        # Don't query if less than 1h since last update
        min_wait = timedelta(hours=1)
        delta_t = datetime.now(timezone.utc) - start.replace(microsecond=0)
        if delta_t <= min_wait:
            _LOGGER.debug(
                "Series %s is current. Next update in %s", stat_id, min_wait - delta_t
            )
            return None

        return (start, Decimal(entry["sum"]))

    async def async_import(self) -> Optional[Decimal]:
        """Import historical data.

        Returns:
            The cumulative total of the consumption series after import, or
            None if nothing was imported.
        """
        try:
            resume: dict[str, tuple[Optional[datetime], Decimal]] = {}
            for series in [MAIN_SERIES, *EXTRA_STATISTICS]:
                point = await self._resume_point(series)
                if point is not None:
                    resume[series] = point

            if not resume:
                _LOGGER.debug("All series are current, skipping API query")
                return None

            if any(start is None for start, _ in resume.values()):
                _LOGGER.warning(
                    "Starting initial import for %s. This may take some time.",
                    ", ".join(s for s, (st, _) in resume.items() if st is None),
                )

            return await self._import_statistics(resume)

        except TimeoutError as e:
            _LOGGER.warning("Timeout during import: %s", e)
            return None
        except Exception as e:
            _LOGGER.exception("Error during import: %s", e)
            return None

    async def _import_statistics(
        self, resume: dict[str, tuple[Optional[datetime], Decimal]]
    ) -> Optional[Decimal]:
        """Import statistics from Netz NO API.

        Dispatches to the appropriate import method based on meter type.
        """
        now = datetime.now(timezone.utc)
        default_start = now - timedelta(days=365 * 3)

        # One pass over the API covers every series; fetch from the earliest
        # point any of them still needs.
        window_start = min(
            (start if start is not None else default_start) for start, _ in resume.values()
        )
        if window_start.tzinfo is None:
            raise ValueError("start datetime must be timezone-aware!")
        if window_start > now:
            _LOGGER.warning("Start date is after end date, skipping")
            return None

        starts = {
            series: (start if start is not None else default_start)
            for series, (start, _) in resume.items()
        }
        totals = {series: total for series, (_, total) in resume.items()}

        _LOGGER.debug(
            "Importing %s from %s to %s (FTM: %s)",
            ", ".join(resume), window_start, now, self.has_ftm_meter_data,
        )

        if self.has_ftm_meter_data:
            return await self._import_ftm_statistics(window_start, now, starts, totals)
        return await self._import_daily_statistics(window_start, now, starts, totals)

    def _write(
        self,
        readings: dict[str, dict[datetime, Decimal]],
        starts: dict[str, datetime],
        totals: dict[str, Decimal],
    ) -> Optional[Decimal]:
        """Turn accumulated readings into external statistics, one per series."""
        for series, buckets in readings.items():
            statistics = []
            total = totals[series]
            for ts, usage in sorted(buckets.items(), key=itemgetter(0)):
                if ts < starts[series]:
                    continue
                total += usage
                statistics.append(
                    StatisticData(start=ts, sum=float(total), state=float(usage))
                )
            totals[series] = total
            if not statistics:
                continue
            _LOGGER.debug(
                "Importing %d entries for %s from %s to %s",
                len(statistics), self.series_id(series),
                statistics[0]["start"], statistics[-1]["start"],
            )
            async_add_external_statistics(
                self.hass, self.get_statistics_metadata(series), statistics
            )
        return totals.get(MAIN_SERIES)

    async def _import_ftm_statistics(
        self,
        start: datetime,
        end: datetime,
        starts: dict[str, datetime],
        totals: dict[str, Decimal],
    ) -> Optional[Decimal]:
        """Import FTM (15-minute interval) statistics.

        Each day returns individual readings (e.g., 96 values for 15-min intervals)
        together with the timestamp of each one. We aggregate to hourly statistics
        (HA requires timestamps at top of hour).

        The API's timestamps carry no UTC offset but are UTC, and each marks the
        *end* of its interval. A day's readings therefore run from 22:15 on the
        previous day through 22:00 on the day itself.
        """
        readings: dict[str, dict[datetime, Decimal]] = {
            series: defaultdict(Decimal) for series in starts
        }
        current_date = start.date()
        end_date = end.date()

        while current_date <= end_date:
            try:
                times, series = await self.async_smartmeter.get_consumption_day_series(
                    current_date, self.metering_point_id
                )
                series = derive_series(series)

                for name, values in series.items():
                    if name not in readings or not values:
                        continue
                    if len(times) < len(values):
                        _LOGGER.warning(
                            "Netz NO returned %d values but only %d timestamps for %s, "
                            "skipping the surplus readings",
                            len(values), len(times), current_date,
                        )

                    # Aggregate readings to hourly buckets, using the timestamp
                    # the API reports for each reading rather than assuming the
                    # day starts at midnight UTC.
                    for i, value in enumerate(values):
                        if i >= len(times):
                            break
                        if value is None:
                            continue

                        reading_time = dt_util.parse_datetime(times[i])
                        if reading_time is None:
                            continue
                        if reading_time.tzinfo is None:
                            # The timestamps carry no offset and are UTC.
                            reading_time = reading_time.replace(tzinfo=timezone.utc)

                        # The timestamp is the end of the interval, so step back
                        # a minute before rounding down: a reading stamped 23:00
                        # covers 22:45-23:00 and belongs to the 22:00 hour.
                        hour_start = (reading_time - timedelta(minutes=1)).replace(
                            minute=0, second=0, microsecond=0
                        )
                        if hour_start < starts[name]:
                            continue
                        readings[name][hour_start] += Decimal(str(value))

            except Exception as e:
                _LOGGER.debug("Could not fetch data for %s: %s", current_date, e)

            current_date += timedelta(days=1)

        return self._write(readings, starts, totals)

    async def _import_daily_statistics(
        self,
        start: datetime,
        end: datetime,
        starts: dict[str, datetime],
        totals: dict[str, Decimal],
    ) -> Optional[Decimal]:
        """Import daily meter statistics using the Month endpoint.

        Each day's single consumption value becomes one statistics entry
        assigned to midnight UTC of that day.
        """
        readings: dict[str, dict[datetime, Decimal]] = {series: {} for series in starts}
        start_date = start.date()
        end_date = end.date()

        # Iterate month by month
        current_year = start_date.year
        current_month = start_date.month

        while date(current_year, current_month, 1) <= end_date:
            try:
                _, series = await self.async_smartmeter.get_consumption_month_series(
                    current_year, current_month, self.metering_point_id
                )
                series = derive_series(series)
                days_in_month = calendar.monthrange(current_year, current_month)[1]

                for name, values in series.items():
                    if name not in readings or not values:
                        continue
                    for day_index, value in enumerate(values):
                        if value is None:
                            continue
                        day_num = day_index + 1  # 1-based day of month
                        if day_num > days_in_month:
                            break
                        day_date = date(current_year, current_month, day_num)
                        # Skip days outside our requested range.
                        # Use <= for start_date because incremental imports set start
                        # to the END of the last stat (midnight + 1hr), so start_date
                        # equals the last already-imported day — skip it to avoid
                        # re-adding its value to the cumulative sum.
                        if day_date <= start_date or day_date > end_date:
                            continue
                        day_midnight = datetime.combine(
                            day_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                        readings[name][day_midnight] = Decimal(str(value))

            except Exception as e:
                _LOGGER.debug(
                    "Could not fetch monthly data for %d-%02d: %s",
                    current_year, current_month, e
                )

            # Advance to next month
            if current_month == 12:
                current_year += 1
                current_month = 1
            else:
                current_month += 1

        return self._write(readings, starts, totals)
