"""Global bot state."""
import collections
import threading
from config import _config

class State:
    session_id = _config.get("session_id")
    selecting = False
    answering = False
    session_list = []
    pending_question = None
    claude_proc = None
    busy = False
    model = None
    total_cost = 0.0
    last_cost = 0.0
    global_tokens = 0
    waiting_token_input = False
    connect_prompt_msg_id = None
    message_queue = collections.deque()
    lock = threading.Lock()

state = State()
