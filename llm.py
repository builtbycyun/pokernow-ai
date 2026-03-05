import json
import os
import re

from dotenv import load_dotenv
from anthropic import Anthropic

from game import format_cards

PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")


def _load_base_prompt():
    """Load the base system prompt from prompt.txt."""
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"Missing prompt file: {PROMPT_FILE}")

STREET_ABBREV = {"Preflop": "Pre", "Flop": "Flop", "Turn": "Turn", "River": "River"}


class LLMPlayer:
    def __init__(self, strategy_file=None):
        load_dotenv()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or api_key == "your-key-here":
            raise RuntimeError("Set ANTHROPIC_API_KEY in .env file")
        self.client = Anthropic(api_key=api_key)
        self.strategy_file = strategy_file
        self.ui = None

    def _get_system_prompt(self):
        """Return system prompt from prompt.txt, appending strategy file contents if available."""
        base = _load_base_prompt()
        if self.strategy_file:
            try:
                with open(self.strategy_file, "r", encoding="utf-8") as f:
                    strategy = f.read().strip()
                if strategy:
                    base += f"\n\n--- LEARNED STRATEGY ---\n{strategy}\n--- END STRATEGY ---"
            except FileNotFoundError:
                pass
        return base

    def decide(self, game):
        """Called as on_turn callback. Queries Claude and executes the action."""
        state = game.summary()
        prompt = self._build_prompt(game, state)

        print(f"\n  [LLM] Prompt:\n{prompt}")
        print(f"\n  [LLM] Thinking...")
        if self.ui:
            self.ui.emit_event("llm_thinking")

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self._get_system_prompt(),
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

        text = response.content[0].text
        print(f"  [LLM] Raw response:\n{text}")
        decision = self._parse_response(text)
        action = decision["action"]
        amount = decision.get("amount", 0)
        reasoning = decision.get("reasoning", "(no reasoning)")
        reads = decision.get("reads", {})
        print(f"  [LLM] {action}{f' {amount}' if action == 'RAISE' else ''} — {reasoning}")
        if reads:
            reads_str = ", ".join(f"{k}={v}" for k, v in reads.items())
            print(f"  [LLM] Reads: {reads_str}")
        if self.ui:
            action_str = f"{action} {amount}" if action == "RAISE" else action
            self.ui.emit_event("llm_decision", {"action": action_str, "reasoning": reasoning, "reads": reads})

        self._execute(game, decision, state)

    def _build_prompt(self, game, state):
        positions = state.get("positions", {})
        my_pos = positions.get(game.my_id, "?")

        # Build players list in seat order
        players = []
        for seat_num, pid in game.seats:
            p = game.players.get(pid, {})
            if p.get("status") != "inGame":
                continue
            pgs = game.player_game_statuses.get(pid, "")
            entry = {
                "name": game._name(pid),
                "pos": positions.get(pid, "?"),
                "stack": p.get("stack", 0),
            }
            if pgs == "fold":
                entry["folded"] = True
            players.append(entry)

        # Build history grouped by street
        history = state.get("hand_history", [])
        hist_obj = {}
        if history:
            for street, name, action, amount in history:
                abbrev = STREET_ABBREV.get(street, street)
                if abbrev not in hist_obj:
                    hist_obj[abbrev] = []
                entry = [name, action]
                if amount:
                    entry.append(amount)
                hist_obj[abbrev].append(entry)

        # Deterministic draw analysis
        draws = self._analyze_draws(state["my_cards"], state["community"] or [])

        # Suit counts across hole cards + board
        all_cards = list(state["my_cards"]) + list(state["community"] or [])
        suit_names = {"s": "spades", "h": "hearts", "d": "diamonds", "c": "clubs"}
        suit_counts = {}
        for c in all_cards:
            if c:
                s = suit_names.get(c[-1], c[-1])
                suit_counts[s] = suit_counts.get(s, 0) + 1

        prompt_obj = {
            "hand": state["hand"],
            "street": state["round"],
            "blinds": [game.small_blind, game.big_blind],
            "pot": state["pot"],
            "you": {
                "pos": my_pos,
                "cards": state["my_cards"],
                "stack": state["my_stack"],
            },
            "players": players,
            "board": state["community"] or [],
            "suit_counts": suit_counts,
            "draws": draws,
            "history": hist_obj,
            "actions": {
                "call": state["current_bet"],
                "raise": [state["min_raise"], game.max_bet],
            },
        }

        return json.dumps(prompt_obj, separators=(",", ":"))

    @staticmethod
    def _analyze_draws(hole_cards, board):
        """Deterministic draw/made-hand analysis. Returns a compact dict."""
        if not hole_cards or not board:
            return {}

        RANK_ORDER = "23456789TJQKA"

        def rank_val(card):
            r = card[:-1].upper()
            if r == "10":
                r = "T"
            return RANK_ORDER.index(r) if r in RANK_ORDER else -1

        def suit(card):
            return card[-1].lower()

        all_cards = list(hole_cards) + list(board)
        hole_suits = [suit(c) for c in hole_cards]
        hole_ranks = [rank_val(c) for c in hole_cards]

        result = {}

        # --- Flush analysis ---
        suit_counts = {}
        for c in all_cards:
            s = suit(c)
            suit_counts[s] = suit_counts.get(s, 0) + 1

        # Only count flushes/draws where we contribute at least one hole card
        for s in set(hole_suits):
            total = suit_counts.get(s, 0)
            hole_contribution = hole_suits.count(s)
            if total >= 5 and hole_contribution >= 1:
                result["flush"] = True
            elif total == 4 and hole_contribution >= 1:
                result["flush_draw"] = True

        # --- Straight analysis ---
        all_ranks = sorted(set(rank_val(c) for c in all_cards))
        # Add low ace
        if 12 in all_ranks:
            all_ranks = [-1] + all_ranks

        # Check all 5-card windows
        has_straight = False
        has_oesd = False
        has_gutshot = False
        for low in range(-1, 10):
            window = [low, low + 1, low + 2, low + 3, low + 4]
            present = [r in all_ranks for r in window]
            count = sum(present)
            # Must use at least one hole card
            uses_hole = any(r in hole_ranks or (r == -1 and 12 in hole_ranks) for r in window if r in all_ranks)
            if count == 5 and uses_hole:
                has_straight = True
            elif count == 4 and uses_hole:
                missing_idx = present.index(False)
                if 0 < missing_idx < 4:
                    has_gutshot = True
                else:
                    has_oesd = True

        if has_straight:
            result["straight"] = True
        elif has_oesd:
            result["oesd"] = True
        elif has_gutshot:
            result["gutshot"] = True

        # --- Pair/set analysis ---
        # Count each rank across all cards
        rank_counts = {}
        for c in all_cards:
            r = rank_val(c)
            rank_counts[r] = rank_counts.get(r, 0) + 1

        pocket_pair = len(hole_ranks) == 2 and hole_ranks[0] == hole_ranks[1]
        board_rank_list = [rank_val(c) for c in board]

        # Find best made hand involving hole cards
        hole_quads = any(rank_counts.get(r, 0) >= 4 for r in set(hole_ranks))
        hole_trips = any(rank_counts.get(r, 0) >= 3 for r in set(hole_ranks))

        # Count distinct pair ranks we participate in
        our_pair_ranks = [r for r in set(hole_ranks) if rank_counts.get(r, 0) >= 2]
        # Also count board pairs we don't participate in (for full house detection)
        board_only_pairs = [r for r, cnt in rank_counts.items() if cnt >= 2 and r not in set(hole_ranks)]

        if hole_quads:
            result["quads"] = True
        elif hole_trips:
            # Full house if there's another pair anywhere
            other_pair = len(our_pair_ranks) > 1 or len(board_only_pairs) > 0
            if pocket_pair:
                # KK on K-7-7: trips from pocket + board, plus board pair = full house
                # KK on K-7-3: just trips
                other_pair = len(board_only_pairs) > 0
            if other_pair:
                result["full_house"] = True
            else:
                result["trips"] = True
        elif len(our_pair_ranks) == 2 and not pocket_pair:
            # Both hole cards pair with the board
            result["two_pair"] = True
        elif pocket_pair:
            # Pocket pair — check if board has a pair too (two pair)
            if len(board_only_pairs) > 0:
                result["two_pair"] = True
            elif board_rank_list and all(r > max(board_rank_list) for r in hole_ranks):
                result["overpair"] = True
            else:
                result["pocket_pair"] = True
        elif len(our_pair_ranks) == 1:
            # One hole card pairs with board
            r = our_pair_ranks[0]
            board_sorted = sorted(board_rank_list, reverse=True)
            if board_sorted and r == board_sorted[0]:
                result["top_pair"] = True
            elif board_sorted and r == board_sorted[-1]:
                result["bottom_pair"] = True
            else:
                result["mid_pair"] = True

        # --- Overcards ---
        if board:
            max_board = max(rank_val(c) for c in board)
            overcards = sum(1 for r in hole_ranks if r > max_board)
            if overcards > 0:
                result["overcards"] = overcards

        return result

    def _parse_response(self, text):
        """Parse JSON response from the LLM."""
        cleaned = text.strip()
        # Strip markdown code blocks
        if "```" in cleaned:
            cleaned = re.sub(r"```(?:json)?\s*", "", cleaned).replace("```", "").strip()

        # Find the JSON object — grab from first { to last }
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_str = cleaned[start:end + 1]
            try:
                data = json.loads(json_str)
                data["action"] = data.get("action", "FOLD").upper()
                data["amount"] = int(data.get("amount", 0))
                data["reasoning"] = data.get("reasoning", "(no reasoning)")
                return data
            except (json.JSONDecodeError, ValueError):
                pass

        # Handle truncated JSON — extract action with regex as last resort
        action_m = re.search(r'"action"\s*:\s*"(\w+)"', cleaned)
        amount_m = re.search(r'"amount"\s*:\s*(\d+)', cleaned)
        reason_m = re.search(r'"reasoning"\s*:\s*"([^"]*)', cleaned)
        if action_m:
            return {
                "action": action_m.group(1).upper(),
                "amount": int(amount_m.group(1)) if amount_m else 0,
                "reasoning": reason_m.group(1) if reason_m else "(truncated)",
            }

        print(f"  [LLM] Failed to parse response, folding")
        return {"action": "FOLD", "amount": 0, "reasoning": "(parse error)"}

    def _execute(self, game, decision, state):
        """Map parsed decision to game method."""
        action = decision["action"]
        amount = decision.get("amount", 0)

        if action == "FOLD":
            game.fold()
        elif action == "CHECK":
            if state["current_bet"] > 0:
                game.call()
            else:
                game.check()
        elif action == "CALL":
            if state["current_bet"] == 0:
                game.check()
            else:
                game.call()
        elif action == "ALL_IN":
            game.all_in()
        elif action in ("RAISE", "BET"):
            amount = max(amount, state["min_raise"])
            amount = min(amount, game.max_bet)
            game.raise_to(amount)
        else:
            print(f"  [LLM] Unknown action '{action}', folding")
            game.fold()

    def decide_run_it_twice(self, game):
        """Called when run-it-twice decision is needed."""
        state = game.summary()
        prompt = json.dumps({
            "decision": "run_it_twice",
            "you": {"cards": state["my_cards"], "stack": state["my_stack"]},
            "board": state["community"] or [],
            "pot": state["pot"],
        }, separators=(",", ":"))

        print(f"\n  [LLM] Run it twice decision...")
        if self.ui:
            self.ui.emit_event("llm_thinking")

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=128,
            system='You are asked whether to "Run It Twice" in poker (deal remaining board cards twice, splitting the pot). Respond with ONLY a JSON object, no other text: {"accept":true,"reasoning":"1 sentence"}. accept is true or false. Output raw JSON only. No markdown, no explanation outside the JSON.',
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

        text = response.content[0].text
        print(f"  [LLM] RIT raw: {text}")
        try:
            start = text.find("{")
            end = text.rfind("}")
            data = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
            accept = data.get("accept", True)
            reasoning = data.get("reasoning", "")
        except (json.JSONDecodeError, ValueError):
            accept = True
            reasoning = "(parse error, defaulting to accept)"

        print(f"  [LLM] Run it twice: {'Accept' if accept else 'Decline'} — {reasoning}")
        if self.ui:
            self.ui.emit_event("llm_decision", {
                "action": f"RIT: {'Accept' if accept else 'Decline'}",
                "reasoning": reasoning,
            })
        game.run_it_twice(accept)
