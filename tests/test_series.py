"""Tests for the ConsumptionRecord response model.

Plain Python, no pytest and no Home Assistant needed:

    python3 tests/test_series.py

The fixtures are shortened real responses from smartmeter.netz-noe.at,
recorded on 2026-10-08. Values are kept as measured; identifiers are
replaced. The three shapes that matter are all represented:

  * a metering point with no energy community   -> one row
  * one community                               -> two rows
  * two memberships, settled                    -> three rows, one per
                                                   community
  * two memberships, still arriving             -> the same day a few hours
                                                   earlier: one community row
                                                   missing and the aggregate
                                                   briefly inconsistent

The last one is why ``grid_leftover`` is computed instead of read. A day's
figures arrive over several hours; the subtraction is right throughout,
the portal's own series is not.
"""
import importlib.util
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "custom_components" / "netznoe" / "api"

# Load api/client.py without pulling in Home Assistant.
_pkg = types.ModuleType("_nnapi")
_pkg.__path__ = [str(BASE)]
sys.modules["_nnapi"] = _pkg
_spec = importlib.util.spec_from_file_location("_nnapi.client", BASE / "client.py")
client = importlib.util.module_from_spec(_spec)
sys.modules["_nnapi.client"] = client
_spec.loader.exec_module(client)


def _row(ec_id, metered, self_coverage, leftover, joint):
    """One ConsumptionRecord row, trimmed to four intervals."""
    return {
        "ec_id": ec_id,
        "meteredValues": metered,
        "selfCoverageValues": self_coverage,
        "gridUsageLeftoverValues": leftover,
        "jointTenancyProportionValues": joint,
        "peakDemandTimes": [
            "2026-10-06T22:15:00", "2026-10-06T22:30:00",
            "2026-10-06T22:45:00", "2026-10-06T23:00:00",
        ],
        # Present in every response and never populated on this meter.
        "estimatedValues": [None] * 4,
        "meteredPeakDemands": [None] * 4,
        "blindConsumptionValue": [None] * 4,
    }


NO_COMMUNITY = [
    _row(None, [0.287, 0.164, 0.201, 0.355], [None] * 4, [None] * 4, [None] * 4)
]

ONE_COMMUNITY = [
    _row(None, [0.640, 0.710, 0.541, 0.670],
         [0.320, 0.355, 0.270, 0.335],
         [0.320, 0.355, 0.271, 0.335],
         [0.420, 0.455, 0.370, 0.435]),
    _row("AT…EEG", [0.320, 0.355, 0.270, 0.335],
         [0.320, 0.355, 0.270, 0.335],
         [0.000, 0.000, 0.000, 0.000],
         [0.420, 0.455, 0.370, 0.435]),
]

# 2026-10-07, the first day with both an EEG and a BEG membership, once the
# figures had settled: one row per community. The BEG had not been allocated
# anything yet, hence its zero self-coverage. Note that both community rows
# carry exactly half the day's consumption — that number is an artefact, not
# a measurement.
TWO_COMMUNITIES = [
    _row(None, [0.640, 0.710, 0.541, 0.670],
         [0.160, 0.170, 0.130, 0.165],
         [0.480, 0.540, 0.411, 0.505],
         [0.420, 0.455, 0.370, 0.435]),
    _row("AT…EEG", [0.320, 0.355, 0.270, 0.335],
         [0.160, 0.170, 0.130, 0.165],
         [0.160, 0.185, 0.140, 0.170],
         [0.420, 0.455, 0.370, 0.435]),
    _row("ATCC…BEG", [0.320, 0.355, 0.270, 0.335],
         [0.000, 0.000, 0.000, 0.000],
         [0.320, 0.355, 0.270, 0.335],
         [0.000, 0.000, 0.000, 0.000]),
]

# The same day a few hours earlier, while the figures were still arriving:
# the EEG row had not appeared and the aggregate's own leftover did not add
# up with its metered value.
TWO_COMMUNITIES_SETTLING = [
    _row(None, [0.640, 0.710, 0.541, 0.670],
         [0.160, 0.170, 0.130, 0.165],
         [0.160, 0.185, 0.140, 0.170],
         [0.420, 0.455, 0.370, 0.435]),
    _row("ATCC…BEG", [0.320, 0.355, 0.270, 0.335],
         [0.160, 0.170, 0.130, 0.165],
         [0.160, 0.185, 0.140, 0.170],
         [0.420, 0.455, 0.370, 0.435]),
]

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got}\n         want {want}")
        failures.append(name)


def close(name, got, want, tol=1e-9):
    if got is not None and abs(got - want) <= tol:
        print(f"  ok   {name}  ({got:.3f})")
    else:
        print(f"  FAIL {name}: got {got}, want {want}")
        failures.append(name)


def total(values):
    return sum(v for v in values if v is not None)


print("pick_aggregate picks the ec_id null row, never a community row")
check("no community", client.pick_aggregate(NO_COMMUNITY)["ec_id"], None)
check("one community", client.pick_aggregate(ONE_COMMUNITY)["ec_id"], None)
check("two memberships", client.pick_aggregate(TWO_COMMUNITIES)["ec_id"], None)
check("three rows seen", len(TWO_COMMUNITIES), 3)
check("still arriving", client.pick_aggregate(TWO_COMMUNITIES_SETTLING)["ec_id"], None)
check("aggregate not first", client.pick_aggregate(list(reversed(ONE_COMMUNITY)))["ec_id"], None)
check("empty response", client.pick_aggregate([]), None)
check("bare dict passes through", client.pick_aggregate({"ec_id": None})["ec_id"], None)

print("\na series the portal leaves empty is dropped, not reported as zero")
plain = client._extract_series(client.pick_aggregate(NO_COMMUNITY))
check("no community -> metered only", sorted(plain), ["metered"])
check("empty arrays are not kept", "self_coverage" in plain, False)

withec = client._extract_series(client.pick_aggregate(ONE_COMMUNITY))
check("one community -> three series", sorted(withec),
      ["joint_tenancy", "metered", "self_coverage"])

print("\nderive_series computes the residual, and skips it when it cannot")
plain_d = client.derive_series(plain)
check("no community -> no grid_leftover", "grid_leftover" in plain_d, False)

one_d = client.derive_series(withec)
close("one community: our residual", total(one_d["grid_leftover"]), 1.281)
close("one community: portal agrees",
      total(client.pick_aggregate(ONE_COMMUNITY)["gridUsageLeftoverValues"]), 1.281, 2e-3)

two = client.derive_series(client._extract_series(client.pick_aggregate(TWO_COMMUNITIES)))
close("two memberships, settled: our residual", total(two["grid_leftover"]), 1.936)
close("two memberships, settled: portal agrees",
      total(client.pick_aggregate(TWO_COMMUNITIES)["gridUsageLeftoverValues"]), 1.936, 2e-3)

# The point of computing rather than reading: the same day, hours earlier.
settling = client.derive_series(
    client._extract_series(client.pick_aggregate(TWO_COMMUNITIES_SETTLING))
)
close("still arriving: our residual is unchanged", total(settling["grid_leftover"]), 1.936)
portal_settling = total(
    client.pick_aggregate(TWO_COMMUNITIES_SETTLING)["gridUsageLeftoverValues"]
)
close("still arriving: portal is off by a factor", portal_settling, 0.655)
if abs(total(settling["grid_leftover"]) - portal_settling) > 0.5:
    print("  ok   computing survives the window the portal does not")
else:
    print("  FAIL the settling fixture no longer disagrees")
    failures.append("settling disagreement")

print("\nmetered stays the identity metered = leftover + self_coverage")
for label, series in (("one community", one_d), ("two memberships", two),
                      ("still arriving", settling)):
    rebuilt = [a + b for a, b in zip(series["grid_leftover"], series["self_coverage"])]
    close(f"{label}: rebuilt == metered", total(rebuilt), total(series["metered"]), 1e-9)

print("\nNone values survive the derivation")
gappy = client.derive_series({"metered": [1.0, None, 2.0], "self_coverage": [0.4, 0.1, None]})
check("None in either input -> None out", gappy["grid_leftover"], [0.6, None, None])

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
