"""Generate a deterministic mock friend group's shared content.

Five college friends, ten months of DMs, reels and photos. Planted in the data:
- moments: Maya's spite trip to Lisbon (a DM with Sam), the 3am fire alarm, finals week
- inside jokes: Kofi's rice cooker, the Mario Kart beef (Dev, Kofi and Ana only)
- noise: one-off messages that shouldn't form any moment
- unsafe: realistic but sensitive messages, flagged safe_for_demo=false

Run: uv run python scripts/seed_group.py
"""

import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

NAMESPACE = uuid.UUID("6f1c2a52-8d0e-4b7a-9c3e-1f2a3b4c5d6e")
PEOPLE = ["Maya", "Dev", "Sam", "Ana", "Kofi"]
OUT = Path(__file__).resolve().parent.parent / "seed" / "group_items.json"


def pid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"profile:{name}"))


def at(y: int, m: int, d: int, h: int = 12, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


# (sender, text, content_type)
LISBON = [
    ("Maya", "bro my internship just got rescinded 💀 a week before my start date", "message"),
    ("Maya", "im actually crashing out rn", "message"),
    ("Maya", "fuck it im booking a one way ticket to lisbon", "message"),
    ("Sam", "WAIT ur deadass going to lisbon alone??", "message"),
    ("Maya", "one way ticket to portugal. no return flight. im a free woman", "message"),
    ("Maya", "lisbon hostel has a rooftop and a guy named joão who plays guitar at 2am istg", "message"),
    ("Maya", "the trams in lisbon are so steep i thought i was gonna die", "message"),
    ("Maya", "ate 6 pastel de natas today and im not even sorry", "photo"),
    ("Maya", "sam look at this sunset in alfama 😭😭", "photo"),
    ("Sam", "ur literally living in a movie in portugal rn im so jealous", "message"),
    ("Maya", "lisbon has me thinking abt dropping out and selling sardines", "message"),
    ("Sam", "bffr you are not selling sardines in lisbon", "message"),
    ("Maya", "the lisbon hostel ppl adopted me im their emotional support american", "message"),
    ("Maya", "last night in lisbon im gonna cry", "message"),
    ("Maya", "flight home from lisbon delayed 6 hrs. portugal doesn't want me to leave fr", "message"),
    ("Sam", "u better bring me back a pastel de nata from portugal or dont come back at all", "message"),
]

FIRE_ALARM = [
    ("Ana", "WHO SET OFF THE FIRE ALARM ITS 3AM", "message"),
    ("Sam", "im standing outside in a towel rn for this fire alarm i hate it here", "message"),
    ("Ana", "it was a ROBE stop saying towel", "message"),
    ("Maya", "someone burnt popcorn on the 4th floor again and set off the alarm i KNOW it was 402", "message"),
    ("Kofi", "the fire truck actually came 💀💀", "photo"),
    ("Sam", "firefighter said 'who microwaved popcorn for 9 minutes' and nobody said shit", "message"),
    ("Maya", "we been outside for 40 min because of the fire alarm my feet are frozen", "message"),
    ("Ana", "a 3am fire drill is a hate crime", "message"),
    ("Sam", "they finally let us back in after the fire alarm im never microwaving anything again", "message"),
    ("Maya", "the RA looked so done with all of us at 3am lmaooo", "message"),
    ("Ana", "fire alarm going off at 3am and dev slept thru the whole thing how", "message"),
    ("Dev", "i genuinely did not hear a fire alarm. i sleep like the dead", "message"),
]

FINALS = [
    ("Sam", "culc library till 2 for finals whos coming", "message"),
    ("Ana", "finals week got me looking like a raccoon", "photo"),
    ("Dev", "i have 3 exams in 2 days i'm actually cooked", "message"),
    ("Maya", "who took the good study room on the 3rd floor of the library im ab to fight", "message"),
    ("Dev", "pulled an all nighter studying for this exam and forgot everything", "message"),
    ("Sam", "the linear algebra final was a war crime", "message"),
    ("Kofi", "energy drink #4 of the day studying for finals. my heart is doing parkour", "message"),
    ("Ana", "prof curved the exam 12 points i'm literally alive", "message"),
    ("Maya", "library study group turned into a 3 hour yap session again, zero studying", "message"),
    ("Dev", "can finals just be over pls i'm begging", "message"),
    ("Sam", "my gpa is in the trenches after this finals week", "message"),
    ("Ana", "just submitted my last final exam i'm free bitches", "message"),
    ("Kofi", "finals week brain rot i just called my ta mom", "message"),
    ("Maya", "studying for finals and i've read the same exam slide 40 times", "message"),
]

RICE_COOKER = [
    ("Ana", "kofi really grabbed his rice cooker and NOTHING else during the fire alarm 💀", "message"),
    ("Kofi", "you don't leave the rice cooker behind. that's family", "message"),
    ("Maya", "kofi's rice cooker has more aura than all of us", "message"),
    ("Kofi", "someone moved my rice cooker i'm crashing out", "message"),
    ("Sam", "kofi's rice cooker makes better decisions than dev", "message"),
    ("Ana", "thanksgiving plan: kofi brings the rice cooker, that's it, that's the whole plan", "message"),
    ("Kofi", "the rice cooker is coming home with me for break i can't leave her alone", "photo"),
    ("Kofi", "new semester same rice cooker 🙏", "photo"),
    ("Dev", "ana tried to put pasta in the rice cooker and kofi hasn't spoken to her since", "message"),
    ("Kofi", "valentine's day plans? me and the rice cooker", "message"),
    ("Kofi", "rice cooker slander will not be tolerated in this gc", "message"),
    ("Maya", "kofi if the building was on fire again would you save me or the rice cooker", "message"),
    ("Kofi", "the rice cooker. no hesitation", "message"),
    ("Sam", "pouring one out for the rice cooker's 1 year anniversary with kofi", "message"),
    ("Dev", "rice cooker 2 when", "message"),
    ("Ana", "kofi said the rice cooker has a name and it's 'mama' i'm crying", "message"),
]

MARIO_KART = [
    ("Dev", "mario kart rematch tonight kofi you're getting cooked", "message"),
    ("Kofi", "who blue shelled me on the last lap i'm deadass throwing my controller", "message"),
    ("Ana", "rainbow road is not a racetrack it's a hate crime", "message"),
    ("Kofi", "dev picked baby peach in mario kart again. unserious behavior", "message"),
    ("Dev", "i've never lost at mario kart i just let yall win", "message"),
    ("Kofi", "ana using tilt steering in mario kart like a grandma and still winning i hate it", "message"),
    ("Ana", "mario kart tournament in the lounge, loser buys wings", "message"),
    ("Ana", "kofi rage quit mario kart and went to bed at 9pm 💀", "message"),
    ("Dev", "a blue shell on the final lap should be illegal", "message"),
    ("Ana", "dev plays mario kart like he has a gambling addiction", "message"),
    ("Kofi", "we need a mario kart rematch this is not over", "message"),
    ("Dev", "the mario kart beef is back on, kofi said something about my kart", "message"),
    ("Ana", "dev still hasn't recovered from the rainbow road incident", "message"),
    ("Kofi", "mario kart night got so heated the RA came up", "message"),
]

NOISE = [
    ("Sam", "anyone want chick fil a", "message"),
    ("Ana", "it's so hot outside i'm melting", "message"),
    ("Dev", "who has an iphone charger", "message"),
    ("Maya", "my professor just said 'as you all know' and i did not know", "message"),
    ("Kofi", "the wifi in this building is ass", "message"),
    ("Sam", "is the dining hall open rn", "message"),
    ("Dev", "gym at 7?", "message"),
    ("Ana", "i just watched a squirrel eat a whole bagel", "reel"),
    ("Maya", "someone's car alarm has been going for 20 min", "message"),
    ("Kofi", "rate my fit", "photo"),
    ("Sam", "it's giving broke college student", "reel"),
    ("Dev", "i need a nap so bad", "message"),
    ("Ana", "why is everyone on campus sick", "message"),
    ("Maya", "who's going to the game saturday", "message"),
    ("Kofi", "uber prices are criminal", "message"),
    ("Sam", "my roommate is on the phone at 1am again", "message"),
    ("Dev", "the laundry machine ate my clothes and my soul", "message"),
    ("Ana", "the vending machine stole my dollar", "message"),
    ("Maya", "we should get boba later", "message"),
    ("Kofi", "chat is this real", "reel"),
    ("Sam", "this reel is literally me", "reel"),
    ("Dev", "the group project guy ghosted us again", "message"),
    ("Ana", "my mom just texted 'k' im scared", "message"),
    ("Maya", "the weather cannot make up its mind", "message"),
    ("Kofi", "who stole the traffic cone from outside", "photo"),
    ("Sam", "why do people send venmo requests for $2", "message"),
    ("Dev", "i've watched this reel 30 times", "reel"),
    ("Ana", "someone come to target with me", "message"),
]

UNSAFE = [
    ("Sam", "i'm so hungover i want to die", "message"),
    ("Ana", "not me crying over my ex at 2am again", "message"),
    ("Dev", "my parents are fighting about money again i hate being home", "message"),
    ("Maya", "therapist said i need boundaries lmao", "message"),
]


def spread(rng: random.Random, start: datetime, end: datetime, n: int) -> list[datetime]:
    """n sorted timestamps between start and end."""
    span = (end - start).total_seconds()
    return sorted(start + timedelta(seconds=rng.uniform(0, span)) for _ in range(n))


def build() -> dict:
    rng = random.Random(13)
    everyone = PEOPLE
    themes = [
        ("lisbon_trip", "moment", LISBON, ["Maya", "Sam"],
         spread(rng, at(2025, 7, 2), at(2025, 7, 28), len(LISBON))),
        ("fire_alarm", "moment", FIRE_ALARM, everyone,
         spread(rng, at(2025, 9, 18, 3, 1), at(2025, 9, 18, 4, 10), len(FIRE_ALARM))),
        ("finals_week", "moment", FINALS, everyone,
         spread(rng, at(2025, 12, 8), at(2025, 12, 14), len(FINALS))),
        ("rice_cooker", "inside_joke", RICE_COOKER, everyone,
         [at(2025, 9, 18, 3, 30)] + spread(rng, at(2025, 10, 1), at(2026, 4, 20), len(RICE_COOKER) - 1)),
        ("mario_kart", "inside_joke", MARIO_KART, ["Dev", "Kofi", "Ana"],
         spread(rng, at(2025, 8, 20), at(2026, 3, 15), len(MARIO_KART))),
    ]

    items = []
    for theme, kind, lines, participants, times in themes:
        for (sender, text, ctype), when in zip(lines, times):
            items.append({
                "theme": theme,
                "planted_kind": kind,
                "sender": sender,
                "participants": participants,
                "body": text,
                "content_type": ctype,
                "occurred_at": when,
                "safe_for_demo": True,
            })

    for (sender, text, ctype), when in zip(NOISE, spread(rng, at(2025, 7, 1), at(2026, 4, 30), len(NOISE))):
        others = [p for p in PEOPLE if p != sender]
        participants = PEOPLE if rng.random() < 0.6 else [sender, rng.choice(others)]
        items.append({"theme": "noise", "planted_kind": None, "sender": sender,
                      "participants": participants, "body": text, "content_type": ctype,
                      "occurred_at": when, "safe_for_demo": True})

    for (sender, text, ctype), when in zip(UNSAFE, spread(rng, at(2025, 8, 1), at(2026, 3, 1), len(UNSAFE))):
        items.append({"theme": "unsafe", "planted_kind": None, "sender": sender,
                      "participants": PEOPLE, "body": text, "content_type": ctype,
                      "occurred_at": when, "safe_for_demo": False})

    items.sort(key=lambda i: i["occurred_at"])
    out_items = []
    for n, item in enumerate(items):
        out_items.append({
            "id": str(uuid.uuid5(NAMESPACE, f"item:{n}:{item['body']}")),
            "content_type": item["content_type"],
            "body": item["body"],
            "sender_profile_id": pid(item["sender"]),
            "participant_profile_ids": sorted(pid(p) for p in set(item["participants"]) | {item["sender"]}),
            "occurred_at": item["occurred_at"].isoformat(),
            "safe_for_demo": item["safe_for_demo"],
            # Ground truth for tests only. Never used by the pipeline.
            "planted_theme": item["theme"],
            "planted_kind": item["planted_kind"],
        })

    return {
        "profiles": [{"id": pid(p), "display_name": p} for p in PEOPLE],
        "items": out_items,
    }


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(data['items'])} items for {len(data['profiles'])} profiles to {OUT}")


if __name__ == "__main__":
    main()
