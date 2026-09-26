"""Server-owned synchronization fixture, replaceable by the generation teammate.

Answers are synthetic, not assertions about real Instagram history. All snapshot
participants answer in this fixture. No provider or conversation context required.
"""


def build_rounds(players) -> list[dict]:
    options = [{"profile_id": str(p["id"]), "label": p["display_name"]} for p in players]
    captions = ["A dog stealing a picnic sandwich", "A spectacular pancake flip", "A sleepy cat DJ"]
    return [
        {
            "prompt": "Who sent this reel?",
            "options": options,
            "media": {"asset_key": f"sync-demo/reel-{i + 1}", "caption": caption},
            "answer": {"correct_profile_id": str(players[i % len(players)]["id"])},
            "reveal_copy": f"{players[i % len(players)]['display_name']} sent it!",
        }
        for i, caption in enumerate(captions)
    ]
