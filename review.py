import json
import os

from dotenv import load_dotenv
from anthropic import Anthropic

from game import format_cards

REVIEW_SYSTEM = """\
You are a poker strategy reviewer. You analyze completed hands and decide whether the bot's play reveals a lesson worth recording.

You will receive:
- The hand history (actions by street)
- The result (who won, how much)
- The bot's hole cards and the community cards
- The bot's current strategy rules (may be empty)

Your job: decide if this hand teaches something genuinely useful. Be VERY selective — most hands are routine and teach nothing new.

Only record lessons for:
- Costly mistakes a rule could prevent (e.g., calling large river bets with weak draws)
- Exploitable opponent patterns (e.g., a player always min-raises with premium hands)
- Sizing adjustments (e.g., bet bigger on wet boards)
- Notably good plays worth reinforcing

If you update the strategy, output the COMPLETE new strategy (not just the addition). Consolidate overlapping rules. Keep it under 30 lines.

Respond with ONLY a JSON object:
- No update needed: {"update": false}
- Update needed: {"update": true, "new_strategy": "line1\\nline2\\n..."}

Output raw JSON only. No markdown, no explanation outside the JSON.
"""

MAX_STRATEGY_LINES = 30


class ReviewAgent:
    def __init__(self):
        load_dotenv()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or api_key == "your-key-here":
            raise RuntimeError("Set ANTHROPIC_API_KEY in .env file")
        self.client = Anthropic(api_key=api_key)

    def review_hand(self, bot_name, strategy_file, game_result, hand_history, my_cards, community_cards):
        """Analyze a completed hand and optionally update the strategy file."""
        # Read current strategy
        current_strategy = ""
        try:
            with open(strategy_file, "r", encoding="utf-8") as f:
                current_strategy = f.read().strip()
        except FileNotFoundError:
            pass

        # Build review prompt
        prompt_obj = {
            "bot_name": bot_name,
            "my_cards": my_cards,
            "community_cards": community_cards,
            "hand_history": hand_history,
            "result": game_result,
            "current_strategy": current_strategy or "(empty — no rules yet)",
        }

        prompt = json.dumps(prompt_obj, separators=(",", ":"), default=str)

        try:
            response = self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                system=REVIEW_SYSTEM,
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )

            text = response.content[0].text
            decision = self._parse_response(text)

            if decision.get("update") and decision.get("new_strategy"):
                new_strategy = decision["new_strategy"].strip()
                # Enforce line cap
                lines = new_strategy.split("\n")
                if len(lines) > MAX_STRATEGY_LINES:
                    lines = lines[:MAX_STRATEGY_LINES]
                    new_strategy = "\n".join(lines)

                # Write updated strategy
                os.makedirs(os.path.dirname(strategy_file), exist_ok=True)
                with open(strategy_file, "w", encoding="utf-8") as f:
                    f.write(new_strategy + "\n")

                rule_count = len([l for l in lines if l.strip()])
                print(f"  [{bot_name}] Strategy updated ({rule_count} rules)")
                return True
            else:
                return False

        except Exception as e:
            print(f"  [{bot_name}] Review error: {e}")
            return False

    def _parse_response(self, text):
        """Parse JSON response from the review LLM."""
        cleaned = text.strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {"update": False}
