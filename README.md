# Poker Bot

An AI-powered poker bot that plays on [PokerNow.com](https://www.pokernow.com) using Claude as its decision engine. Features a real-time cyberpunk web UI, deterministic hand analysis, and a self-learning training loop.

## Architecture

```
PokerNow.com (WebSocket)
        │
   ┌────┴────┐
   │ game.py │  ← WebSocket client, game state engine, action methods
   └────┬────┘
        │
   ┌────┴────┐         ┌────────────┐
   │ llm.py  │────────►│ prompt.txt │  (system prompt + learned strategy)
   └────┬────┘         └────────────┘
        │
   ┌────┴────┐         ┌─────────────────┐
   │  ui.py  │────────►│ table.html      │  (real-time browser UI)
   └─────────┘         └─────────────────┘

After each hand:
   ┌───────────┐       ┌──────────────────┐
   │ review.py │──────►│ strategies/*.txt  │  (evolving strategy files)
   └───────────┘       └──────────────────┘
```

### Core Files

| File | Purpose |
|------|---------|
| `main.py` | Single-bot entry point — joins a game with the web UI |
| `game.py` | WebSocket client for PokerNow. Manages connection, game state (via delta merging), position calculation, action dispatch, and hand history tracking |
| `llm.py` | Claude-powered decision engine. Builds compact JSON prompts, runs deterministic draw/hand analysis, parses LLM responses, and executes actions |
| `review.py` | Post-hand strategy reviewer. Uses Claude Opus to analyze completed hands and selectively update strategy files |
| `run_training.py` | Multi-bot training launcher. Spawns 3 bots (Alpha/Bravo/Charlie) into the same game for self-play |
| `ui.py` | Flask + SocketIO server that pushes real-time game events to the browser |
| `prompt.txt` | Base system prompt with poker instructions, GTO guidelines, and learned strategy rules |
| `templates/table.html` | Single-page cyberpunk poker table UI with live game state, AI reasoning panel, and opponent hand predictions |
| `test_draws.py` | Unit tests for the deterministic hand/draw analyzer |

## Setup

### Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- A [PokerNow.com](https://www.pokernow.com) game

### Install

```bash
pip install -r requirements.txt
```

### Configure

Create a `.env` file:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Play a Single Bot

```bash
python main.py
```

You'll be prompted for:
- **Game ID** — the ID from your PokerNow game URL (`pokernow.com/games/<GAME_ID>`)
- **Player name** — the name the bot joins with

The web UI opens automatically at `http://localhost:5050`.

### Train with Self-Play

```bash
python run_training.py <game_id>
```

Spawns 3 bots (Alpha, Bravo, Charlie) into the same PokerNow game at different seats. Each bot:
- Uses `claude-sonnet-4-6` for real-time play decisions
- Has its own strategy file (`strategies/bot1.txt`, `bot2.txt`, `bot3.txt`)
- Gets reviewed after every hand by `claude-opus-4-6` (the review agent)

The review agent only updates a strategy file when it identifies a genuinely useful lesson — costly mistakes, exploitable patterns, or sizing adjustments. Strategy files are capped at 30 lines and get consolidated over time.

### Run Tests

```bash
python test_draws.py
# or
pytest test_draws.py
```

## How It Works

### Decision Flow

1. PokerNow sends a game state delta over WebSocket
2. `game.py` merges the delta into the full state and detects it's the bot's turn
3. `llm.py` builds a compact JSON prompt containing: hand number, street, blinds, pot, hole cards, board, player stacks/positions, betting history, and a **deterministic draw analysis**
4. The draw analyzer (`_analyze_draws`) computes made hands (flush, straight, trips, two pair, etc.) and draws (flush draw, OESD, gutshot) so the LLM doesn't have to evaluate card combinations
5. Claude returns a JSON response: `{"action": "RAISE", "amount": 75, "reasoning": "...", "reads": {"Bob": "QQ+"}}`
6. The action is executed via WebSocket, and the decision + reads are pushed to the browser UI

### Training Loop

```
Play hand → Review with Opus → Update strategy file → Next hand uses updated strategy
```

The review agent (`review.py`) receives the complete hand history, hole cards, community cards, and result. It decides whether the hand reveals a lesson worth recording. If so, it rewrites the entire strategy file (not just appends) to keep rules consolidated and under the 30-line cap.

Strategy rules are injected into the system prompt on every hand, so improvements take effect immediately.

### Web UI

The browser UI shows:
- **Poker table** with player seats, stacks, and position labels
- **Community cards** and pot size
- **Per-player actions** for the current betting round (e.g., "CALL 20", "RAISE 75")
- **Hand predictions** on opponent seats after each LLM decision (e.g., "QQ+", "flush dr")
- **AI panel** with the bot's latest action and reasoning (typewriter effect)
- **Action log** with full hand history

### PokerNow Integration

The bot connects to PokerNow via their WebSocket protocol:
1. HTTP GET to get the `npt` session cookie
2. WebSocket connection using Engine.IO/Socket.IO
3. POST to `/request_ingress` to join the table
4. Incremental state updates via `gC` (game change) events with deep merge + `<D>` delete markers
5. Actions sent as Socket.IO events (`PLAYER_FOLD`, `PLAYER_CHECK`, `PLAYER_CALL`, `PLAYER_RAISE`)

## Models Used

| Purpose | Model | Why |
|---------|-------|-----|
| Play decisions | `claude-sonnet-4-6` | Fast enough for real-time play, strong reasoning |
| Strategy review | `claude-opus-4-6` | Better analytical depth for identifying patterns across hands |

## Project Structure

```
poker/
├── main.py              # Single-bot entry point
├── game.py              # WebSocket client + game state
├── llm.py               # AI decision engine
├── review.py            # Post-hand strategy reviewer
├── run_training.py      # Multi-bot training launcher
├── ui.py                # Web UI server
├── test_draws.py        # Hand analyzer tests
├── prompt.txt           # System prompt
├── requirements.txt     # Dependencies
├── .env                 # API key (gitignored)
├── templates/
│   └── table.html       # Browser UI
└── strategies/
    ├── bot1.txt          # Alpha's learned strategy
    ├── bot2.txt          # Bravo's learned strategy
    └── bot3.txt          # Charlie's learned strategy
```
