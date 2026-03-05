import websocket
import json
import requests
import datetime
import threading

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Engine.IO / Socket.IO protocol constants
EIO_PING = "2"
EIO_PONG = "3"
EIO_MESSAGE = "4"
SIO_CONNECT = "0"
SIO_EVENT = "2"

DELETE = "<D>"

PHASE_NAMES = {0: "Preflop", 1: "Flop", 2: "Turn", 3: "River"}


def _card(c):
    """Format a card value like '6d' into a readable string."""
    if not c:
        return "??"
    suits = {"s": "\u2660", "h": "\u2665", "d": "\u2666", "c": "\u2663"}
    rank = c[:-1]
    suit = suits.get(c[-1], c[-1])
    return f"{rank}{suit}"


def format_cards(cards):
    """Format a list of card values."""
    return " ".join(_card(c) for c in cards) if cards else "none"


def _deep_merge(base, update):
    """Merge update into base, handling PokerNow's <D> delete marker."""
    for key, value in update.items():
        if value == DELETE:
            base.pop(key, None)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class PokerGame:
    def __init__(self, game_id, player_name, seat=3, stack=1000, log_file="ws_messages.log"):
        self.game_id = game_id
        self.player_name = player_name
        self.seat = seat
        self.stack = stack
        self.log_file = log_file

        # Connection state
        self._session = None
        self._npt = None
        self._ws = None

        # Identity
        self.my_id = None

        # Full game state (merged from registered + gC deltas)
        self.state = {}

        # Tracking for display diffing
        self._prev_hand_id = None
        self._prev_phase = -1
        self._prev_community = []
        self._announced_actions = set()  # track which cRPI entries we've printed
        self._turn_acted = False  # prevent duplicate LLM calls per turn

        # Hand history: [(street, player_name, action, amount)]
        self.hand_history = []

        # Callback: called when it's our turn, signature: callback(game) -> None
        self.on_turn = None

        # Callback: called on every game state change, signature: callback(game) -> None
        self.on_state_change = None

        # Callback: called when run-it-twice decision needed, signature: callback(game) -> None
        self.on_run_it_twice = None

        # Callback: called when a hand completes, signature: callback(game, result, history, my_cards, community) -> None
        self.on_hand_complete = None

        # UI module reference (set externally); if set, emits events to the web UI
        self.ui = None

    # -- Derived state properties --

    @property
    def status(self):
        return self.state.get("status", "waiting")

    @property
    def players(self):
        return self.state.get("players", {})

    @property
    def seats(self):
        return self.state.get("seats", [])

    @property
    def my_cards(self):
        pc = self.state.get("pC") or {}
        mine = pc.get(self.my_id) or {}
        cards = mine.get("cards") or []
        return [c["value"] for c in cards if isinstance(c, dict) and c.get("value")]

    @property
    def community_cards(self):
        otc = self.state.get("oTC") or {}
        return otc.get("1") or []

    @property
    def pot(self):
        return self.state.get("pot", 0)

    @property
    def current_bets(self):
        return {k: v for k, v in self.state.get("tB", {}).items() if v != DELETE}

    @property
    def big_blind(self):
        return self.state.get("bigBlind", 0)

    @property
    def small_blind(self):
        return self.state.get("smallBlind", 0)

    @property
    def dealer_id(self):
        return self.state.get("dealerID")

    @property
    def sb_player_id(self):
        return self.state.get("sBPI")

    @property
    def bb_player_id(self):
        return self.state.get("bBPI")

    @property
    def current_player_id(self):
        return self.state.get("cPI")

    @property
    def is_my_turn(self):
        return self.current_player_id == self.my_id and self.my_id is not None

    @property
    def current_highest_bet(self):
        return self.state.get("cHB", 0)

    @property
    def min_raise(self):
        return self.state.get("mR", 0)

    @property
    def max_bet(self):
        return self.state.get("mAVTB", 0)

    @property
    def hand_number(self):
        return self.state.get("gN", 0)

    @property
    def hand_id(self):
        return self.state.get("hI")

    @property
    def game_turn(self):
        return self.state.get("gT") or [0, 0]

    @property
    def phase(self):
        gt = self.game_turn
        return gt[1] if len(gt) > 1 else 0

    @property
    def round_name(self):
        return PHASE_NAMES.get(self.phase, "unknown")

    @property
    def player_game_statuses(self):
        return self.state.get("pGS", {})

    @property
    def in_hand_player_ids(self):
        return self.state.get("iHPI", [])

    @property
    def game_result(self):
        return self.state.get("gameResult")

    @property
    def my_stack(self):
        me = self.players.get(self.my_id, {})
        return me.get("stack", 0)

    def player_name_by_id(self, pid):
        if not pid:
            return "?"
        return self.players.get(pid, {}).get("name", pid[:8])

    def _name(self, pid):
        """Short helper — returns 'You' for our own id."""
        if not pid:
            return "?"
        if pid == self.my_id:
            return "You"
        return self.player_name_by_id(pid)

    # -- Actions --

    def fold(self):
        self._send_action({"type": "PLAYER_FOLD"})

    def check(self):
        self._send_action({"type": "PLAYER_CHECK"})

    def call(self):
        self._send_action({"type": "PLAYER_CALL"})

    def raise_to(self, amount):
        self._send_action({"type": "PLAYER_RAISE", "value": amount})

    def all_in(self):
        self._send_action({"type": "PLAYER_RAISE", "value": self.max_bet})

    def run_it_twice(self, accept=True):
        self._send_action({"type": "DRITD", "decision": accept})

    def _send_action(self, action):
        msg = f'42["action",{json.dumps(action)}]'
        self._log(">>>", msg)
        if self._ws:
            self._ws.send(msg)

    # -- Connection --

    def connect(self):
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write(f"=== Session {datetime.datetime.now().isoformat()} ===\n")
            f.write(f"Game: {self.game_id} | Player: {self.player_name}\n\n")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Origin": "https://www.pokernow.com",
        })

        resp = self._session.get(
            f"https://www.pokernow.com/games/{self.game_id}",
            headers={"Accept": "text/html"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        self._npt = self._session.cookies.get("npt")
        if not self._npt:
            raise RuntimeError("Failed to get npt cookie")

        url = (
            f"wss://www.pokernow.com/socket.io/"
            f"?gameID={self.game_id}"
            f"&firstConnection=true"
            f"&layout=m"
            f"&EIO=3"
            f"&transport=websocket"
        )
        self._ws = websocket.WebSocketApp(
            url,
            header={
                "Origin": "https://www.pokernow.com",
                "User-Agent": USER_AGENT,
            },
            cookie=f"npt={self._npt}",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        import time
        while True:
            self._ws.run_forever(ping_interval=10, ping_timeout=5)
            print("\n  Connection lost, reconnecting in 3s...")
            if self.ui:
                self.ui.emit_event("error", {"message": "Connection lost, reconnecting..."})
            time.sleep(3)
            self._ws = websocket.WebSocketApp(
                url,
                header={
                    "Origin": "https://www.pokernow.com",
                    "User-Agent": USER_AGENT,
                },
                cookie=f"npt={self._npt}",
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

    def _join(self):
        resp = self._session.post(
            f"https://www.pokernow.com/games/{self.game_id}/request_ingress",
            json={
                "playerName": self.player_name,
                "seat": self.seat,
                "stack": self.stack,
                "allowSpectator": False,
            },
            headers={
                "Accept": "application/json",
                "Referer": f"https://www.pokernow.com/games/{self.game_id}",
            },
        )
        if resp.status_code == 200:
            print(f"  Joined as '{self.player_name}' at seat {self.seat}")
        else:
            print(f"  Failed to join: {resp.text[:200]}")

    # -- WebSocket handlers --

    def _on_open(self, ws):
        # Start Engine.IO heartbeat — client must send "2" pings
        def heartbeat():
            import time
            while ws.sock and ws.sock.connected:
                time.sleep(15)
                try:
                    ws.send(EIO_PING)
                    self._log(">>>", EIO_PING)
                except Exception:
                    break
        threading.Thread(target=heartbeat, daemon=True).start()

    def _on_error(self, ws, error):
        msg = str(error)
        # Suppress normal WebSocket close frames
        if "opcode=8" in msg:
            return
        print(f"[error] {msg}")
        if self.ui:
            self.ui.emit_event("error", {"message": msg})

    def _on_close(self, ws, code, msg):
        pass  # Reconnect loop in connect() handles this

    def _on_message(self, ws, message):
        self._log("<<<", message)

        if not message:
            return

        if message in (EIO_PING, EIO_PONG):
            self._log("<<<", message)
            return

        if not message.startswith(EIO_MESSAGE):
            return

        sio_data = message[1:]
        if sio_data == SIO_CONNECT or not sio_data.startswith(SIO_EVENT):
            return

        try:
            payload = json.loads(sio_data[1:])
        except json.JSONDecodeError:
            return

        event = payload[0] if isinstance(payload, list) else payload
        data = payload[1] if isinstance(payload, list) and len(payload) > 1 else None

        if event == "registered":
            self._handle_registered(data)
        elif event == "gC":
            self._handle_gc(data)

    def _handle_registered(self, data):
        first_connect = self.my_id is None
        self.my_id = data["currentPlayer"]["id"]
        self.state = data.get("gameState", {})
        if first_connect:
            self._join()

        if self.ui:
            self.ui.emit_event("connected", {
                "my_id": self.my_id,
                "player_name": self.player_name,
            })

        if self.status == "inProgress":
            self._print_hand_header()
            self._prev_hand_id = self.hand_id
            self._prev_phase = self.phase
            self._prev_community = list(self.community_cards)

    def _handle_gc(self, data):
        # Detect what changed BEFORE merging
        old_hand_id = self.state.get("hI")
        old_phase = self.state.get("gT") or [0, 0]
        old_phase_num = old_phase[1] if len(old_phase) > 1 else 0
        old_pgs = dict(self.state.get("pGS", {}))
        old_community = list(self.community_cards)
        old_result = self.state.get("gameResult")
        old_cpi = self.state.get("cPI")

        # Merge the delta
        _deep_merge(self.state, data)

        # --- Display logic ---

        # Reset turn flag when current player changes
        if self.state.get("cPI") != old_cpi:
            self._turn_acted = False

        # New hand?
        if "hI" in data and data["hI"] != old_hand_id and data.get("hI") != DELETE:
            self._announced_actions = set()
            self._turn_acted = False
            self._prev_phase = 0
            self._prev_community = []
            self.hand_history = []
            self._print_hand_header()

        # New street?
        if "gT" in data:
            new_phase = self.phase
            if new_phase > self._prev_phase and new_phase > 0:
                self._turn_acted = False
                self._print_street(new_phase)
                self._prev_phase = new_phase

        # Player actions (from NSAA transitions)
        if data.get("sNA") == "NSAA" and "cRPI" in data:
            self._print_player_actions(data, old_pgs)

        # Run it twice — ask callback
        if data.get("wRITD") and self.my_id not in self.state.get("rITD", {}):
            if self.on_run_it_twice:
                threading.Thread(target=self.on_run_it_twice, args=(self,), daemon=True).start()
            else:
                self.run_it_twice(True)

        # Game result
        if "gameResult" in data and data["gameResult"] and not old_result:
            self._print_result(data["gameResult"])
            if self.on_hand_complete:
                threading.Thread(
                    target=self.on_hand_complete,
                    args=(self, data["gameResult"], list(self.hand_history),
                          list(self.my_cards), list(self.community_cards)),
                    daemon=True,
                ).start()

        # Whose turn prompt
        if data.get("sNA") == "FCPFS" and "cPI" in data:
            self._print_turn_prompt()
            # Send turn acknowledgment
            if self.is_my_turn:
                self._send_action({"type": "ATB"})

        # Fire callbacks
        if self.on_state_change:
            self.on_state_change(self)

        if self.is_my_turn and self.on_turn and not self._turn_acted:
            self._turn_acted = True
            # Run in a thread so the WS thread can keep handling pings
            threading.Thread(target=self.on_turn, args=(self,), daemon=True).start()

    # -- Display methods --

    def _print_hand_header(self):
        dealer = self._name(self.dealer_id)
        sb_name = self._name(self.sb_player_id)
        bb_name = self._name(self.bb_player_id)

        print(f"\n{'='*50}")
        print(f"  Hand #{self.hand_number}  |  Blinds {self.small_blind}/{self.big_blind}")
        print(f"  Dealer: {dealer}  |  SB: {sb_name}  |  BB: {bb_name}")

        # Print seated players and stacks
        players_str = "  Players: "
        parts = []
        for seat_num, pid in self.seats:
            p = self.players.get(pid, {})
            if p.get("status") == "inGame":
                name = self._name(pid)
                stack = p.get("stack", "?")
                parts.append(f"{name}({stack})")
        print(players_str + ", ".join(parts))

        # Print our cards
        if self.my_cards:
            print(f"  Your cards: {format_cards(self.my_cards)}")

        # Print blinds posted
        tb = self.current_bets
        if tb:
            for pid, amt in tb.items():
                if isinstance(amt, (int, float)):
                    print(f"  {self._name(pid)} posts {amt}")
                    self.hand_history.append(("Preflop", self._name(pid), "post", amt))

        print(f"{'='*50}")

        if self.ui:
            players_data = {}
            for pid, p in self.players.items():
                if p.get("status") == "inGame":
                    players_data[pid] = {"name": p.get("name", pid[:8]), "stack": p.get("stack", 0), "status": p.get("status")}
            self.ui.emit_event("hand_start", {
                "hand_number": self.hand_number,
                "small_blind": self.small_blind,
                "big_blind": self.big_blind,
                "dealer_id": self.dealer_id,
                "sb_id": self.sb_player_id,
                "bb_id": self.bb_player_id,
                "players": players_data,
                "seats": list(self.seats),
                "my_cards": self.my_cards,
                "pot": self.pot,
                "bets": {pid: amt for pid, amt in tb.items() if isinstance(amt, (int, float))},
            })
            # Emit blind posts as actions
            for pid, amt in tb.items():
                if isinstance(amt, (int, float)):
                    self.ui.emit_event("action", {
                        "player_id": pid,
                        "player_name": self._name(pid),
                        "action": "post",
                        "amount": amt,
                    })

    def _print_street(self, phase_num):
        name = PHASE_NAMES.get(phase_num, "?")
        cc = self.community_cards
        print(f"\n--- {name}: {format_cards(cc)} --- (pot: {self.pot})")

        if self.ui:
            self.ui.emit_event("street", {
                "street": name,
                "community": cc,
                "pot": self.pot,
            })

    def _print_player_actions(self, data, old_pgs):
        new_pgs = self.state.get("pGS", {})
        crpi = data.get("cRPI", [])
        tb = data.get("tB", {})
        chb = data.get("cHB")

        for pid in crpi:
            if pid in self._announced_actions:
                continue
            self._announced_actions.add(pid)

            name = self._name(pid)
            action_type = "check"
            action_amount = None

            # Check if folded
            if new_pgs.get(pid) == "fold" and old_pgs.get(pid) != "fold":
                print(f"  {name} folds")
                action_type = "fold"
            else:
                # Check bet amount
                bet = tb.get(pid)
                if bet is not None and isinstance(bet, (int, float)):
                    p = self.players.get(pid, {})
                    stack = p.get("stack", 0)

                    if stack == 0:
                        print(f"  {name} goes ALL IN ({bet})")
                        action_type = "all_in"
                        action_amount = bet
                    elif chb is not None and bet > 0:
                        old_chb = self._prev_chb if hasattr(self, '_prev_chb') else 0
                        if chb > old_chb and old_chb > 0:
                            print(f"  {name} raises to {bet}")
                            action_type = "raise"
                            action_amount = bet
                        elif chb > 0:
                            print(f"  {name} calls {bet}")
                            action_type = "call"
                            action_amount = bet
                        else:
                            print(f"  {name} bets {bet}")
                            action_type = "bet"
                            action_amount = bet
                    else:
                        print(f"  {name} checks")
                else:
                    print(f"  {name} checks")

            self.hand_history.append((self.round_name, name, action_type, action_amount))

            if self.ui:
                self.ui.emit_event("action", {
                    "player_id": pid,
                    "player_name": name,
                    "action": action_type,
                    "amount": action_amount,
                    "pot": self.pot,
                    "bets": {k: v for k, v in self.current_bets.items() if isinstance(v, (int, float))},
                    "players": {p_id: {"stack": p_data.get("stack", 0)} for p_id, p_data in self.players.items()},
                })

        # Track for next comparison
        if chb is not None:
            self._prev_chb = chb

        # Reset action tracking on new round
        if not crpi:
            self._announced_actions = set()

    def _print_result(self, result):
        print(f"\n{'*'*50}")

        ui_winners = []

        if isinstance(result, dict):
            winners = result.get("winners", [])
            if isinstance(winners, list):
                for w in winners:
                    pid = w.get("playerID", w.get("id", ""))
                    name = self._name(pid)
                    amount = w.get("amount", w.get("winAmount", "?"))
                    hand_name = w.get("handName", w.get("name", ""))
                    cards = w.get("cards", [])
                    cards_str = f" with {format_cards(cards)}" if cards else ""
                    hand_str = f" - {hand_name}" if hand_name else ""
                    print(f"  {name} wins {amount}{hand_str}{cards_str}")
                    ui_winners.append({"player_id": pid, "player_name": name, "amount": amount, "hand_name": hand_name})
            elif isinstance(result, dict):
                for pid, info in result.items():
                    if isinstance(info, dict):
                        name = self._name(pid)
                        amount = info.get("amount", info.get("winAmount", "?"))
                        hand_name = info.get("handName", info.get("name", ""))
                        print(f"  {name} wins {amount} - {hand_name}")
                        ui_winners.append({"player_id": pid, "player_name": name, "amount": amount, "hand_name": hand_name})

        print(f"{'*'*50}")

        if self.ui:
            self.ui.emit_event("result", {"winners": ui_winners})

    def _print_turn_prompt(self):
        if self.is_my_turn:
            to_call = self.current_highest_bet
            print(f"\n>> YOUR TURN | Cards: {format_cards(self.my_cards)} | Board: {format_cards(self.community_cards)}")
            print(f"   Pot: {self.pot} | To call: {to_call} | Min raise: {self.min_raise} | Stack: {self.my_stack}")
        else:
            name = self._name(self.current_player_id)
            print(f"  Waiting for {name}...")

        if self.ui:
            self.ui.emit_event("turn", {
                "player_id": self.current_player_id,
                "player_name": self._name(self.current_player_id),
                "is_my_turn": self.is_my_turn,
                "pot": self.pot,
                "my_cards": self.my_cards,
                "community": self.community_cards,
            })

    # -- Logging --

    def _log(self, direction, message):
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {direction} {message}\n")

    # -- Positions --

    def get_positions(self):
        """Derive position labels from seats relative to dealer.

        Returns dict {pid: label} for in-game players.
        """
        # Collect in-game player ids in seat order
        active = []
        for seat_num, pid in self.seats:
            p = self.players.get(pid, {})
            if p.get("status") == "inGame":
                active.append(pid)

        n = len(active)
        if n == 0:
            return {}

        # Find dealer index
        dealer_idx = 0
        for i, pid in enumerate(active):
            if pid == self.dealer_id:
                dealer_idx = i
                break

        # Rotate so dealer is first
        ordered = active[dealer_idx:] + active[:dealer_idx]

        positions = {}
        if n == 2:
            labels = ["BTN", "BB"]
        elif n == 3:
            labels = ["BTN", "SB", "BB"]
        else:
            # D, SB, BB, then positional names
            labels = ["D", "SB", "BB"]
            remaining = n - 3
            if remaining == 1:
                labels.append("UTG")
            elif remaining == 2:
                labels += ["UTG", "CO"]
            elif remaining == 3:
                labels += ["UTG", "MP", "CO"]
            else:
                for i in range(remaining):
                    if i == 0:
                        labels.append("UTG")
                    elif i == remaining - 1:
                        labels.append("CO")
                    else:
                        labels.append(f"MP{i}" if remaining > 4 else "MP")

        for i, pid in enumerate(ordered):
            positions[pid] = labels[i] if i < len(labels) else f"S{i}"

        return positions

    # -- Debug --

    def summary(self):
        active = {pid: p for pid, p in self.players.items() if p.get("status") == "inGame"}
        positions = self.get_positions()
        return {
            "status": self.status,
            "hand": self.hand_number,
            "round": self.round_name,
            "my_cards": self.my_cards,
            "community": self.community_cards,
            "pot": self.pot,
            "my_stack": self.my_stack,
            "is_my_turn": self.is_my_turn,
            "current_bet": self.current_highest_bet,
            "min_raise": self.min_raise,
            "players": {p["name"]: p.get("stack", "?") for p in active.values()},
            "positions": positions,
            "hand_history": list(self.hand_history),
        }
