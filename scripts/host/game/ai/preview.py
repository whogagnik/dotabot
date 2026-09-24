"""Live overlay and optional recording of the SAME frame used for a decision."""

from dataclasses import asdict
import json
from pathlib import Path

from .lane import REASONS


def record_brain_frame(directory: Path, brain, image, frame_id: int) -> None:
    trace = brain.last_trace
    if trace is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{frame_id:08d}.png"
    image.save(directory / filename)
    actual = brain.actions.sent_this_tick
    row = {
        **trace,
        "image": filename,
        "frame_size": list(image.size),
        "brain_state_after": brain.state.name,
        "dry_run": brain.dry_run,
        "runtime_action": asdict(actual) if actual else None,
    }
    with (directory / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


# Kept so existing planner imports do not break.
record_lane_frame = record_brain_frame


def draw_brain_overlay(img_bgr, brain, minimap_rect):
    # Imported only by the real preview; replay and policy tests need no imaging packages.
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    image = Image.fromarray(img_bgr[:, :, ::-1])
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    trace = brain.last_trace
    phase = brain.debug_phase
    reason = REASONS.get(
        brain.debug_reason,
        {
            "manual_pause": "Пауза: выберите поведение клавишами 2–9",
            "no_action": "Поведение не предложило новую команду",
        }.get(brain.debug_reason, brain.debug_reason),
    )
    mode = "ПРОСМОТР БЕЗ КЛИКОВ" if brain.dry_run else "УПРАВЛЕНИЕ ИГРОЙ"
    command = brain.actions.sent_this_tick
    action = (
        f"{command.kind} ({command.x:.0f}, {command.y:.0f})"
        if command
        else brain.actions.last_result.reason
    )
    lines = [
        f"{mode} | {brain.state.name}",
        f"Фаза: {phase}",
        reason,
        (
            f"Команда: {action}"
            if trace else "1 — пауза · 2 — лайнинг · 4 — фарм линии · 7 — отход"
        ),
    ]
    y = max(125, image.height - 120)
    # Keep the actual minimap visible next to the text panel.
    panel_right = max(200, minimap_rect[0] - 8)
    draw.rectangle((0, y, panel_right, min(image.height, y + 110)), fill=(14, 23, 35))
    for i, line in enumerate(lines):
        while (
            len(line) > 1
            and draw.textbbox((0, 0), line, font=font)[2] > panel_right - 24
        ):
            line = line[:-2] + "…"
        draw.text((12, y + 7 + i * 24), line, font=font, fill=(220, 234, 250))
    intent = command or brain.last_intent
    if intent:
        color = (255, 225, 110) if command else (145, 160, 180)
        if intent.kind == "move_minimap":
            x0, y0, w, h = minimap_rect
            x, target_y = x0 + intent.x / 100 * w, y0 + intent.y / 100 * h
        else:
            x, target_y = intent.x, intent.y
        draw.ellipse(
            (x - 12, target_y - 12, x + 12, target_y + 12), outline=color, width=3
        )
        draw.line((x - 18, target_y, x + 18, target_y), fill=color, width=2)
        draw.line((x, target_y - 18, x, target_y + 18), fill=color, width=2)
    return np.array(image)[:, :, ::-1].copy()
