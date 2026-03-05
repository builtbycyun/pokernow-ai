"""
Self-learning poker bot training launcher.

Spawns 3 bot instances into the same PokerNow game. Each bot has its own
strategy file that a ReviewAgent updates after interesting hands.

Usage:
    python run_training.py <game_id>
    python run_training.py              # prompts for game_id
"""

import sys
import time
import threading
import signal

from game import PokerGame
from llm import LLMPlayer
from review import ReviewAgent

BOTS = [
    {"name": "Alpha",   "seat": 1, "strategy": "strategies/bot1.txt"},
    {"name": "Bravo",   "seat": 3, "strategy": "strategies/bot2.txt"},
    {"name": "Charlie", "seat": 5, "strategy": "strategies/bot3.txt"},
]

CONNECTION_STAGGER = 3  # seconds between bot connections


def make_hand_complete_callback(bot_name, strategy_file, reviewer):
    """Create a per-bot on_hand_complete callback."""
    def on_hand_complete(game, game_result, hand_history, my_cards, community_cards):
        try:
            reviewer.review_hand(
                bot_name, strategy_file,
                game_result, hand_history, my_cards, community_cards,
            )
        except Exception as e:
            print(f"  [{bot_name}] Review failed: {e}")
    return on_hand_complete


def spawn_bot(game_id, bot_config, reviewer):
    """Set up and connect a single bot (blocking — run in a thread)."""
    name = bot_config["name"]
    seat = bot_config["seat"]
    strategy = bot_config["strategy"]
    log_file = f"ws_{name.lower()}.log"

    llm = LLMPlayer(strategy_file=strategy)
    game = PokerGame(game_id, name, seat=seat, stack=20, log_file=log_file)
    game.on_turn = llm.decide
    game.on_run_it_twice = llm.decide_run_it_twice
    game.on_hand_complete = make_hand_complete_callback(name, strategy, reviewer)

    print(f"  [{name}] Connecting (seat {seat}, strategy: {strategy})...")
    game.connect()  # blocks forever (reconnect loop)


def main():
    game_id = sys.argv[1] if len(sys.argv) > 1 else input("Game ID: ").strip()
    if not game_id:
        print("Error: game_id is required")
        sys.exit(1)

    print(f"\n  Training mode — 3 bots joining game {game_id}")
    print(f"  Bots: {', '.join(b['name'] for b in BOTS)}")
    print(f"  Strategy files: {', '.join(b['strategy'] for b in BOTS)}")
    print()

    reviewer = ReviewAgent()
    threads = []

    for i, bot_config in enumerate(BOTS):
        if i > 0:
            time.sleep(CONNECTION_STAGGER)

        t = threading.Thread(
            target=spawn_bot,
            args=(game_id, bot_config, reviewer),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Keep main thread alive until Ctrl+C
    def handle_sigint(sig, frame):
        print("\n  Shutting down training...")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    # Wait on threads (daemon threads die with main, but this keeps main alive)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down training...")


if __name__ == "__main__":
    main()
