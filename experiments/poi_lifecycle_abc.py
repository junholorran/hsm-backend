"""Experimental POI lifecycle A/B/C audit for Kairos V2.2.

ISOLATED RESEARCH MODULE:
- never imported by app.py/scalp_engine.py
- no DB writes, Telegram, deploy hooks, or production flags
- intended to classify zone-selection transitions before changing strategy

Policies:
A CURRENT  = current selector may move across eligible causal POIs.
B FREEZE   = first POI for a thesis is frozen until it leaves the eligible set.
C LIFECYCLE= a replacement is accepted only when the previous POI has explicitly
             left the eligible causal set; transition reason is recorded.

A thesis is keyed by (sweep_ts, structure_ts, direction). This prevents a POI
from one structural thesis leaking into another.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

POLICIES = ("A_CURRENT", "B_FREEZE", "C_LIFECYCLE")


def _zone_id(z: Optional[Dict[str, Any]]) -> Optional[Tuple[Any, ...]]:
    if not z:
        return None
    return (
        z.get("tipo"), z.get("created_ts"), z.get("flip_ts"),
        z.get("bottom"), z.get("top"),
    )


def _thesis_key(sweep: Dict[str, Any], structure: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        sweep.get("sweep_ts"),
        structure.get("t"),
        sweep.get("direcao"),
    )


@dataclass
class Transition:
    ts: int
    policy: str
    thesis: Tuple[Any, ...]
    previous_zone: Optional[Tuple[Any, ...]]
    candidate_zone: Optional[Tuple[Any, ...]]
    selected_zone: Optional[Tuple[Any, ...]]
    reason: str


class PoiLifecycleABC:
    """State machine only. It does not alter Kairos selection mathematics."""

    def __init__(self, policy: str):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy: {policy}")
        self.policy = policy
        self.selected: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.transitions: List[Transition] = []

    def choose(
        self,
        ts: int,
        sweep: Dict[str, Any],
        structure: Dict[str, Any],
        current_candidate: Optional[Dict[str, Any]],
        eligible_zones: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        thesis = _thesis_key(sweep, structure)
        prev = self.selected.get(thesis)
        prev_id = _zone_id(prev)
        cand_id = _zone_id(current_candidate)
        eligible = {_zone_id(z): z for z in eligible_zones if z}
        prev_still_eligible = prev_id in eligible if prev_id else False

        if self.policy == "A_CURRENT":
            chosen = current_candidate
            reason = "CURRENT_SELECTOR"

        elif self.policy == "B_FREEZE":
            if prev is None:
                chosen = current_candidate
                reason = "FREEZE_FIRST_POI"
            elif prev_still_eligible:
                chosen = eligible[prev_id]
                reason = "KEEP_FROZEN_POI"
            else:
                # Freeze thesis: once its first POI ceases to be eligible,
                # do not chase a deeper replacement POI.
                chosen = None
                reason = "FIRST_POI_LEFT_ELIGIBLE_SET_THESIS_EXPIRED"

        else:  # C_LIFECYCLE
            if prev is None:
                chosen = current_candidate
                reason = "FIRST_POI"
            elif prev_still_eligible:
                chosen = eligible[prev_id]
                reason = "KEEP_PREVIOUS_WHILE_ELIGIBLE"
            else:
                chosen = current_candidate
                reason = (
                    "REPLACE_AFTER_PREVIOUS_LEFT_ELIGIBLE_SET"
                    if current_candidate else
                    "NO_REPLACEMENT_AFTER_PREVIOUS_LEFT_ELIGIBLE_SET"
                )

        if chosen is not None:
            self.selected[thesis] = dict(chosen)

        chosen_id = _zone_id(chosen)
        if prev_id != chosen_id or cand_id != chosen_id:
            self.transitions.append(Transition(
                ts=ts, policy=self.policy, thesis=thesis,
                previous_zone=prev_id, candidate_zone=cand_id,
                selected_zone=chosen_id, reason=reason,
            ))
        return chosen

    def report(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "theses_seen": len(self.selected),
            "transitions": [asdict(x) for x in self.transitions],
        }


def compare_snapshots(snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure deterministic test helper.

    Each snapshot must contain:
      ts, sweep, structure, current_candidate, eligible_zones.

    It allows us to prove lifecycle behavior independently before wiring the
    experiment into historical replay.
    """
    machines = {p: PoiLifecycleABC(p) for p in POLICIES}
    selections = {p: [] for p in POLICIES}

    for snap in snapshots:
        for p, machine in machines.items():
            z = machine.choose(
                ts=snap["ts"],
                sweep=snap["sweep"],
                structure=snap["structure"],
                current_candidate=snap.get("current_candidate"),
                eligible_zones=snap.get("eligible_zones") or [],
            )
            selections[p].append(_zone_id(z))

    return {
        "selections": selections,
        "reports": {p: m.report() for p, m in machines.items()},
        "production_impact": "NONE",
    }


if __name__ == "__main__":
    # SOL regression fixture from audited production logs.
    sweep = {"sweep_ts": 1, "direcao": "alta"}
    structure = {"t": 2}
    z112 = {"tipo":"IFVG_bullish","created_ts":10,"flip_ts":11,"bottom":111.95,"top":112.13}
    z110 = {"tipo":"FVG_bullish","created_ts":12,"flip_ts":None,"bottom":110.02,"top":110.13}
    z108 = {"tipo":"FVG_bullish","created_ts":13,"flip_ts":None,"bottom":108.16,"top":108.40}
    z107 = {"tipo":"FVG_bullish","created_ts":14,"flip_ts":None,"bottom":106.09,"top":107.51}
    fixture = [
        {"ts":100,"sweep":sweep,"structure":structure,"current_candidate":z112,"eligible_zones":[z112,z110,z108,z107]},
        {"ts":200,"sweep":sweep,"structure":structure,"current_candidate":z110,"eligible_zones":[z110,z108,z107]},
        {"ts":300,"sweep":sweep,"structure":structure,"current_candidate":z108,"eligible_zones":[z108,z107]},
        {"ts":400,"sweep":sweep,"structure":structure,"current_candidate":z107,"eligible_zones":[z107]},
    ]
    out = compare_snapshots(fixture)
    assert out["selections"]["A_CURRENT"][-1] == _zone_id(z107)
    assert out["selections"]["B_FREEZE"][1] is None
    assert out["selections"]["C_LIFECYCLE"][-1] == _zone_id(z107)
    print(out)
