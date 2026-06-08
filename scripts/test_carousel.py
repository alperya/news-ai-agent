"""Local carousel + Reels preview — generates output/ without AWS or Instagram."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from video.event_card import generate_carousel_slides, generate_reels_video

MOCK_EVENTS = [
    {
        "title": "Amsterdam Light Festival",
        "date_label": "Wed 10 Jun",
        "location": "Amsterdam",
        "venue": "Vondelpark",
        "emoji": "🎉",
    },
    {
        "title": "Rotterdam Jazz Festival",
        "date_label": "Fri–Sun 13–15 Jun",
        "location": "Rotterdam",
        "venue": "Ahoy Rotterdam",
        "emoji": "🎵",
    },
    {
        "title": "Dutch Open Tennis Championship",
        "date_label": "Mon–Sun 9–15 Jun",
        "location": "Rosmalen",
        "venue": "Autotron",
        "emoji": "🏃",
    },
    {
        "title": "Delft Market & Pottery Fair",
        "date_label": "Sat 14 Jun",
        "location": "Delft",
        "venue": "Markt Delft",
        "emoji": "🎨",
    },
    {
        "title": "Leiden Film Festival",
        "date_label": "Thu–Sun 12–15 Jun",
        "location": "Leiden",
        "venue": "Leidse Schouwburg",
        "emoji": "🎬",
    },
    {
        "title": "Hague International Theatre Night",
        "date_label": "Sat 14 Jun",
        "location": "The Hague",
        "venue": "Nationale Theater",
        "emoji": "🎭",
    },
    {
        "title": "Utrecht Food & Culture Market",
        "date_label": "Sat–Sun 14–15 Jun",
        "location": "Utrecht",
        "venue": "Stadhuisplein",
        "emoji": "🍽️",
    },
]

os.makedirs("output", exist_ok=True)

print("Generating carousel slides...")
slide_paths = generate_carousel_slides(MOCK_EVENTS, "9–15 Jun 2026", "output/test_event")
print(f"  {len(slide_paths)} slides:")
for p in slide_paths:
    size_kb = os.path.getsize(p) // 1024
    print(f"    {p}  ({size_kb} KB)")

print("\nGenerating Reels video with music...")
reel_path = "output/test_event_reel.mp4"
generate_reels_video(slide_paths, reel_path)
size_mb = os.path.getsize(reel_path) / (1024 * 1024)
print(f"  {reel_path}  ({size_mb:.1f} MB)")

print("\nDone. Open output/ to preview.")
