"""Trial Crew grid: ring-column schedule view for user + friend runs."""

from __future__ import annotations

FRIEND_COLOR_CLASSES: list[tuple[str, str]] = [
    ("bg-sky-600", "text-white"),
    ("bg-sky-500", "text-white"),
    ("bg-sky-400", "text-white"),
    ("bg-cyan-600", "text-white"),
    ("bg-blue-500", "text-white"),
    ("bg-indigo-500", "text-white"),
]

USER_COLOR_CLASSES = ("bg-brand", "text-white")


def _resolve_ring(event_name: str, ring_number: str | None) -> str:
    from app.routers.schedule import _ring_of

    return _ring_of(event_name, ring_number)


def _sort_rings(rings: list[str]) -> list[str]:
    def sort_key(ring: str) -> tuple:
        if ring == "Agility":
            return (2, 0, ring)
        if ring == "Jumping":
            return (2, 1, ring)
        if ring.startswith("Ring "):
            try:
                return (0, int(ring[5:]), ring)
            except ValueError:
                pass
        return (1, 0, ring)

    return sorted(set(rings), key=sort_key)


def _legend_key(p: dict) -> tuple:
    if p.get("is_friend"):
        return ("friend", p.get("friend_id"), p.get("dog_name") or "")
    return ("user", p.get("entry_id"), p.get("dog_name") or "")


def _color_for_friend(friend_id: int | None) -> tuple[str, str]:
    idx = (friend_id or 0) % len(FRIEND_COLOR_CLASSES)
    return FRIEND_COLOR_CLASSES[idx]


def _icon_for_prediction(p: dict, color: tuple[str, str]) -> dict:
    handler = (p.get("handler_name") or "?").strip()
    dog = (p.get("dog_name") or "?").strip()
    title_parts = [handler, dog]
    if p.get("event_name"):
        title_parts.append(p["event_name"])
    if p.get("predicted_start_str"):
        title_parts.append(f"~{p['predicted_start_str']}")
    return {
        "handler_initial": (handler[:1] or "?").upper(),
        "dog_initial": (dog[:1] or "?").upper(),
        "is_friend": bool(p.get("is_friend")),
        "title": " · ".join(title_parts),
        "bg_class": color[0],
        "text_class": color[1],
        "conflict": bool(p.get("conflict")),
    }


def _collect_crew_predictions(
    predictions: list[dict],
    friend_groups: list[dict],
    selected_day: int,
) -> list[dict]:
    crew: list[dict] = []
    for p in predictions:
        if p.get("pending"):
            continue
        if (p.get("day") or 1) == selected_day:
            crew.append(p)
    for group in friend_groups:
        for p in group.get("predictions") or []:
            if p.get("pending"):
                continue
            if (p.get("day") or 1) == selected_day:
                crew.append(p)
    return crew


def build_crew_grid(
    day_blocks: list[dict],
    predictions: list[dict],
    friend_groups: list[dict],
    selected_day: int,
) -> dict:
    """Build ring-column grid data for the Trial Crew tab."""
    day_blocks = [b for b in day_blocks if b.get("day", 1) == selected_day]

    rings = _sort_rings([b["ring"] for b in day_blocks if b.get("ring")])

    crew_preds = _collect_crew_predictions(predictions, friend_groups, selected_day)

    color_by_key: dict[tuple, tuple[str, str]] = {}
    for p in crew_preds:
        key = _legend_key(p)
        if key not in color_by_key:
            if p.get("is_friend"):
                color_by_key[key] = _color_for_friend(p.get("friend_id"))
            else:
                color_by_key[key] = USER_COLOR_CLASSES

    crew_legend: list[dict] = []
    seen_legend: set[tuple] = set()
    for p in crew_preds:
        key = _legend_key(p)
        if key in seen_legend:
            continue
        seen_legend.add(key)
        handler = (p.get("handler_name") or "You").strip()
        dog = (p.get("dog_name") or "Unknown").strip()
        color = color_by_key[key]
        crew_legend.append({
            "handler_name": handler,
            "dog_name": dog,
            "is_friend": bool(p.get("is_friend")),
            "icon": _icon_for_prediction(p, color),
        })

    rows: list[dict] = []
    for block in day_blocks:
        ring = block.get("ring")
        if block.get("is_lunch_break"):
            rows.append({
                "time_str": block.get("first_run_str") or "",
                "last_run_str": block.get("last_run_str") or "",
                "is_lunch_break": True,
                "ring": ring,
                "duration_mins": block.get("duration_mins"),
            })
            continue

        cells: dict[str, dict | None] = {r: None for r in rings}
        crew_for_block = [
            p for p in crew_preds
            if p.get("event_name") == block.get("event_name")
            and p.get("height_group") == block.get("height_group")
            and _resolve_ring(p.get("event_name") or "", p.get("ring_number")) == ring
        ]
        cells[ring] = {
            "event_name": block.get("event_name"),
            "height_group": block.get("height_group"),
            "crew": [
                _icon_for_prediction(p, color_by_key[_legend_key(p)])
                for p in crew_for_block
            ],
        }

        rows.append({
            "time_str": block.get("first_run_str") or "",
            "is_lunch_break": False,
            "cells": cells,
        })

    return {
        "rings": rings,
        "rows": rows,
        "crew_legend": crew_legend,
    }
