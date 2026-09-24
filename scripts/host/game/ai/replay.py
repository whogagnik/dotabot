"""Run the real lane policy, retain every frame, and produce a visual test report.

python -m scripts.host.game.ai.replay --open
python -m scripts.host.game.ai.replay --recording runs/brain/1/trace.jsonl --open
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
import json
from pathlib import Path
import webbrowser

from .actions import ActionController
from .lane import (
    LaneConfig,
    LaneFarmPhase,
    LaneMemory,
    LaneObservation,
    REASONS,
    Unit,
    commit_lane_action,
    step_lane,
)
from .scenarios import SCENARIOS, Scenario
from .behavior_scenarios import BEHAVIOR_SCENARIOS, BehaviorScenario
from .engine import commit_action, step_engine


def memory_dict(memory: LaneMemory) -> dict:
    return {**asdict(memory), "phase": memory.phase.name}


def read_memory(data: dict) -> LaneMemory:
    return LaneMemory(
        **{
            **data,
            "phase": LaneFarmPhase[data["phase"]],
            "target": tuple(data["target"]) if data.get("target") else None,
        }
    )


def read_observation(data: dict) -> LaneObservation:
    return LaneObservation(
        **{
            **data,
            "enemies": tuple(Unit(**u) for u in data.get("enemies", [])),
            "allies": tuple(Unit(**u) for u in data.get("allies", [])),
        }
    )


def advance(obs, memory, now, cfg, gate):
    gate.begin_tick()
    decision = step_lane(obs, memory, now, cfg)
    sent = bool(
        decision.intent and gate.send(decision.intent, now, lambda _: None).sent
    )
    memory = commit_lane_action(decision.memory, decision.intent, sent, now, cfg)
    return memory, dict(
        time=now,
        observation=asdict(obs),
        phase=memory.phase.name,
        reason=decision.reason,
        explanation=REASONS[decision.reason],
        intent=asdict(decision.intent) if decision.intent else None,
        action=decision.intent.kind if sent else None,
        gate=gate.last_result.reason,
        after=memory_dict(memory),
    )


def play_scenario(scenario: Scenario) -> dict:
    memory, gate, frames = scenario.initial, ActionController(), []
    for f in scenario.frames:
        memory, row = advance(f.observation, memory, f.time, LaneConfig(), gate)
        expected = dict(reason=f.reason, action=f.action, phase=f.phase)
        errors = [
            f"{key}: expected {value!r}, got {row[key]!r}"
            for key, value in expected.items()
            if row[key] != value
        ]
        frames.append(
            {
                **row,
                "expected": expected,
                "caption": f.caption,
                "errors": errors,
                "passed": not errors,
            }
        )
    return dict(
        name=scenario.name,
        description=scenario.description,
        source="Сценарные наблюдения • рабочая логика Python",
        frames=frames,
        passed=all(f["passed"] for f in frames),
    )


def play_behavior_scenario(scenario: BehaviorScenario) -> dict:
    memory, gate, frames = scenario.initial, ActionController(), []
    for item in scenario.frames:
        gate.begin_tick()
        step = step_engine(scenario.state, item.world, memory, item.time)
        intent = step.decision.intent
        sent = bool(intent and gate.send(intent, item.time, lambda _: None).sent)
        step = commit_action(step, scenario.state, sent, item.time)
        memory = step.memory
        action = intent.kind if sent else None
        expected = dict(reason=item.reason, action=item.action, phase=item.phase)
        actual = dict(reason=step.decision.reason, action=action, phase=step.decision.phase)
        errors = [f"{key}: expected {value!r}, got {actual[key]!r}"
                  for key, value in expected.items() if actual[key] != value]
        hero = item.world.observed_hero
        target = getattr(getattr(memory, scenario.state.name.lower(), None), "target", None)
        frames.append({
            "time": item.time,
            "observation": {
                "hero_screen": list(hero.point) if hero else None,
                "hero_uv": list(item.world.self_uv) if item.world.self_uv else None,
                "target_uv": target,
                "hp_ratio": item.world.hp_ratio,
                "enemies": [asdict(u) for u in (*item.world.enemy_heroes, *item.world.enemy_creeps)],
                "allies": [asdict(u) for u in (*item.world.ally_heroes, *item.world.ally_creeps)],
            },
            "phase": actual["phase"], "reason": actual["reason"],
            "explanation": item.caption,
            "intent": asdict(intent) if intent else None,
            "action": action, "gate": gate.last_result.reason,
            "after": {"target": target}, "expected": expected,
            "caption": item.caption, "errors": errors, "passed": not errors,
        })
    return {"name": scenario.name, "description": scenario.description,
            "source": "Многоходовой сценарий • общий движок поведения",
            "phase_flow": list(scenario.phase_flow), "frames": frames,
            "passed": all(frame["passed"] for frame in frames)}


def play_recording(path: Path) -> dict:
    frames = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        if not all(key in raw for key in ("observation", "before", "config")):
            visual = raw.get("visual_observation") or {}
            intent = raw.get("intent")
            action = intent.get("kind") if raw.get("sent") and intent else None
            expected = {"reason": raw.get("reason"), "action": action,
                        "phase": raw.get("phase", "")}
            frames.append({
                "time": raw.get("now", 0.0), "observation": {
                    "hero_screen": visual.get("hero_screen"),
                    "hero_uv": visual.get("hero_uv"),
                    "target_uv": None, "hp_ratio": visual.get("hp_ratio"),
                    "enemies": visual.get("enemies", []),
                    "allies": visual.get("allies", []),
                }, "phase": raw.get("phase", ""), "reason": raw.get("reason", ""),
                "explanation": "Записанное решение общего движка.",
                "intent": intent, "action": action, "gate": raw.get("gate", "no_intent"),
                "after": {"target": None}, "expected": expected,
                "caption": "Кадр реальной записи: наблюдения, фаза и выбранная команда.",
                "errors": [], "passed": True, "image": None,
                "frame_size": raw.get("frame_size"),
                "brain_state_after": raw.get("brain_state_after"),
                "dry_run": raw.get("dry_run"),
            })
            continue
        obs, before = read_observation(raw["observation"]), read_memory(raw["before"])
        gate = ActionController()
        gate.restore(raw["gate_before"])
        memory, row = advance(
            obs, before, raw["now"], LaneConfig(**raw["config"]), gate
        )
        expected = dict(
            reason=raw["reason"],
            action=raw["intent"]["kind"] if raw["sent"] else None,
            phase=raw["after"]["phase"],
        )
        errors = [key for key, val in expected.items() if row[key] != val]
        # Compare all memory and intent fields, including coordinates and timers.
        if json.dumps(row["after"], sort_keys=True) != json.dumps(
            raw["after"], sort_keys=True
        ):
            errors.append("memory differs from recording")
        if json.dumps(row["intent"], sort_keys=True) != json.dumps(
            raw["intent"], sort_keys=True
        ):
            errors.append("intent differs from recording")
        image = None
        if raw.get("image"):
            image_path = (path.parent / raw["image"]).resolve()
            # A recording may only refer to its own local screenshots.
            if not image_path.is_relative_to(path.parent.resolve()):
                raise ValueError("Screenshot outside recording directory")
            image = "data:image/png;base64," + base64.b64encode(
                image_path.read_bytes()
            ).decode("ascii")
        frames.append(
            {
                **row,
                "expected": expected,
                "caption": "Сравнение с решением, сохранённым во время записи. Это эталон воспроизведения, а не доказательство правильной игры.",
                "errors": errors,
                "passed": not errors,
                "image": image,
                "frame_size": raw.get("frame_size"),
                "brain_state_after": raw.get("brain_state_after"),
                "dry_run": raw.get("dry_run"),
                "runtime_action": raw.get("runtime_action"),
            }
        )
    if not frames:
        raise ValueError("Recording contains no lane frames")
    return dict(
        name=path.parent.name,
        description="Реальные наблюдения: повторный расчёт решений с записанной памятью и временем.",
        source="Запись игры • сравнение с прежним решением",
        frames=frames,
        passed=all(f["passed"] for f in frames),
    )


def write_report(reports: list[dict], output: Path) -> None:
    template = Path(__file__).with_name("replay.html").read_text(encoding="utf-8")
    payload = json.dumps(reports, ensure_ascii=False).replace("<", "\\u003c")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("__REPORT_DATA__", payload), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("runs/brain-tests/index.html")
    )
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    reports = (
        [play_recording(args.recording)]
        if args.recording
        else ([play_scenario(s) for s in SCENARIOS]
              + [play_behavior_scenario(s) for s in BEHAVIOR_SCENARIOS])
    )
    write_report(reports, args.output)
    total = sum(len(r["frames"]) for r in reports)
    failures = sum(not f["passed"] for r in reports for f in r["frames"])
    print(f"{len(reports)} scenarios, {total} frames, {failures} failures")
    for report in reports:
        for i, frame in enumerate(report["frames"]):
            if frame["errors"]:
                print(report["name"], i + 1, frame["errors"])
    print(args.output.resolve())
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
