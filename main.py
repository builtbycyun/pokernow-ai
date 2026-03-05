from game import PokerGame
from llm import LLMPlayer
import ui

if __name__ == "__main__":
    game_id = input("Game ID: ").strip()
    name = input("Player name: ").strip()

    # Start web UI server (opens browser automatically)
    ui.start(port=5050)
    print(f"  Web UI running at http://localhost:5050")

    llm = LLMPlayer()
    llm.ui = ui

    game = PokerGame(game_id, name)
    game.ui = ui
    game.on_turn = llm.decide
    game.on_run_it_twice = llm.decide_run_it_twice

    print(f"  LLM player active (Claude Sonnet)")
    game.connect()
