import logging
import threading
import webbrowser
from flask import Flask, render_template
from flask_socketio import SocketIO

app = Flask(__name__)
app.config["SECRET_KEY"] = "poker-ui-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Suppress Flask/Werkzeug request logs
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Shared state so the UI can render on first load
_current_state = {}


@app.route("/")
def index():
    return render_template("table.html")


@socketio.on("connect")
def handle_connect():
    # Send current state to newly connected client
    if _current_state:
        socketio.emit("full_state", _current_state)


def emit_event(event_type, data=None):
    """Emit an event to all connected browser clients."""
    if data is None:
        data = {}
    # Track latest state for new connections
    if event_type in ("hand_start", "street", "turn", "result", "connected"):
        _current_state[event_type] = data
    if event_type == "hand_start":
        # Clear previous hand data
        _current_state.pop("result", None)
        _current_state.pop("street", None)
    socketio.emit(event_type, data)


def start(port=5050, open_browser=True):
    """Start the Flask-SocketIO server in a daemon thread."""
    def run():
        socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True, log_output=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    if open_browser:
        webbrowser.open(f"http://localhost:{port}")

    return thread
