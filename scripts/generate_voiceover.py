#!/usr/bin/env python3
"""
Generate voiceover audio and output duration manifest.
RUN THIS BEFORE video recording to drive timing.

Usage:
    uv run scripts/generate_voiceover.py product-demo
    uv run scripts/generate_voiceover.py product-demo --scene dashboard-intro
"""

import argparse
import json
from pathlib import Path

import yaml
from mutagen.mp3 import MP3
from openai import OpenAI

SCENARIO_DIR = Path(__file__).parent / "video-scenarios"


def load_scenario(name: str) -> dict:
    """Load scenario YAML file."""
    path = SCENARIO_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Scenario not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def generate_voiceover(scenario_name: str, scene_filter: str | None = None) -> dict[str, float]:
    """Generate audio and return {scene_id: duration_seconds}."""
    scenario = load_scenario(scenario_name)
    output_dir = Path("videos") / scenario_name / "audio"
    output_dir.mkdir(parents=True, exist_ok=True)

    voiceover_config = scenario.get("voiceover", {})
    voice = voiceover_config.get("voice", "alloy")
    model = voiceover_config.get("model", "tts-1-hd")

    client = OpenAI()
    durations: dict[str, float] = {}

    scenes = scenario["scenes"]
    if scene_filter:
        scenes = [s for s in scenes if s["id"] == scene_filter]

    print(f"Generating voiceover for {len(scenes)} scene(s)")
    print(f"  Voice: {voice}, Model: {model}")
    print()

    for scene in scenes:
        if script := scene.get("voiceover"):
            scene_id = scene["id"]
            print(f"  Generating: {scene_id}")

            # Generate speech using streaming response (correct SDK pattern)
            output_path = output_dir / f"{scene_id}.mp3"
            with client.audio.speech.with_streaming_response.create(
                model=model,
                voice=voice,  # type: ignore
                input=script.strip(),
            ) as response:
                response.stream_to_file(output_path)

            # Measure actual duration
            audio = MP3(str(output_path))
            durations[scene_id] = audio.info.length
            print(f"    Duration: {durations[scene_id]:.2f}s")
            print(f"    File: {output_path}")

    # Write duration manifest for recording script
    manifest_path = output_dir / "durations.json"
    with open(manifest_path, "w") as f:
        json.dump(durations, f, indent=2)

    print()
    print(f"Audio generated. Durations written to {manifest_path}")
    print(f"Total scenes: {len(durations)}")
    total_duration = sum(durations.values())
    print(f"Total audio duration: {total_duration:.1f}s ({total_duration/60:.1f}m)")

    return durations


def main():
    parser = argparse.ArgumentParser(description="Generate voiceover audio for video scenarios")
    parser.add_argument("scenario", default="product-demo", nargs="?", help="Scenario name (default: product-demo)")
    parser.add_argument("--scene", help="Generate audio for specific scene only")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    args = parser.parse_args()

    if args.list:
        print("Available scenarios:")
        for f in SCENARIO_DIR.glob("*.yaml"):
            print(f"  - {f.stem}")
        return

    generate_voiceover(args.scenario, args.scene)


if __name__ == "__main__":
    main()
