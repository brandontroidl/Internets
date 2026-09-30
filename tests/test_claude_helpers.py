"""Tests for modules/_claude_state.py and modules/_claude_intent.py."""

import json
import os
import sys

import pytest

from modules import _claude_intent as intent
from modules._claude_state import (BACKLOG_LINES, BACKLOG_MAX_AGE, HISTORY_IDLE,
                                   HISTORY_KEYS, HISTORY_TURNS, MAX_FACTS,
                                   ClaudeState, Conversations)

BOT = "Internets"


# --- addressing ------------------------------------------------------------------

@pytest.mark.parametrize("line,body", [
    (f"{BOT}: hi there", "hi there"),
    (f"{BOT}, hi", "hi"),
    (f"{BOT} hi", "hi"),
    (f"internets: case", "case"),
    (f"@{BOT} yo", "yo"),
    (f"{BOT}:", ""),
])
def test_addressed_at_line_start(line, body):
    assert intent.addressed_text(line, BOT, "heuristic") == body


@pytest.mark.parametrize("line", [
    f"I think {BOT} is broken",
    f"{BOT}s are cool",
    f"{BOT}-fan here",
    "hello",
])
def test_not_addressed_in_heuristic_mode(line):
    assert intent.addressed_text(line, BOT, "heuristic") is None


def test_off_mode_answers_any_mention():
    line = f"I think {BOT} is broken"
    assert intent.addressed_text(line, BOT, "off") == line
    assert intent.addressed_text(f"{BOT}s are cool", BOT, "off") is None


def test_no_botnick_means_never_addressed():
    assert intent.addressed_text("x: hi", "", "off") is None


def test_emotes():
    assert intent.emote_verb(f"pets {BOT}", BOT) == "pets"
    assert intent.emote_verb(f"waves at {BOT}", BOT) == "waves"
    assert intent.emote_verb("pets the cat", BOT) is None
    assert intent.emote_verb(f"stares at {BOT}", BOT) is None
    assert intent.plain_emote(f"* alice hugs {BOT}", BOT) == f"hugs {BOT}"
    assert intent.plain_emote(f"alice hugs {BOT}", BOT) is None


# --- local intents ---------------------------------------------------------------

@pytest.mark.parametrize("text,kind,value,target", [
    ("remember I live in Florida", "remember", "I live in Florida", ""),
    ("forget about Florida", "forget", "Florida", ""),
    ("forget everything", "forget_all", "", ""),
    ("what do you remember about me?", "list_facts", "", "me"),
    ("what do you know about burnout", "list_facts", "", "burnout"),
    ("reply as a drunken pirate", "persona_channel", "a drunken pirate", ""),
    ("from now on be a grumpy wizard", "persona_channel", "a grumpy wizard", ""),
    ("reply as a pirate for everyone", "persona_channel", "a pirate", "channel"),
    ("reply to me as a helpful butler", "persona_user", "a helpful butler", ""),
    ("speak to burnout like a drill sergeant", "persona_target", "a drill sergeant", "burnout"),
    ("reset personality", "persona_reset", "", ""),
    ("stop acting in this channel", "persona_reset", "", "channel"),
    ("I'm in EST", "timezone", "EST", ""),
    ("set my timezone to America/Chicago", "timezone", "America/Chicago", ""),
    ("I live in Pacific time", "timezone", "Pacific", ""),
    ("I prefer 12hr", "clock", "12", ""),
    ("use 24 hour time", "clock", "24", ""),
    ("what time is it?", "time", "", ""),
    ("hey what's the date", "time", "", ""),
])
def test_local_intents(text, kind, value, target):
    assert intent.local_intent(text) == intent.Intent(kind, value, target)


@pytest.mark.parametrize("text", [
    "remember when we went fishing",
    "remember how bad that was",
    "forget it",
    "I'm in the kitchen making tea",
    "what time is it in tokyo when the market opens there tomorrow morning",
    "what's a quasar",
    "remember",
])
def test_conversation_is_not_an_intent(text):
    assert intent.local_intent(text) is None


def test_review_sources_laughter_greeting():
    assert intent.is_review("catch me up")
    assert intent.is_review("^^")
    assert intent.is_review("tl;dr please")
    assert not intent.is_review("what's a quasar")
    assert intent.wants_sources("show me the links")
    assert not intent.wants_sources("what's the weather")
    assert intent.is_laughter("lmao that's great")
    assert not intent.is_laughter("lol" + "ipop")
    assert intent.is_greeting("hey owo")
    assert not intent.is_greeting("hey, did anyone fix the build yet")


# --- persistent state ------------------------------------------------------------

def test_fact_limits(tmp_path):
    s = ClaudeState(tmp_path / "c.json")
    assert s.add_fact("A", "tiny") == "length"
    assert s.add_fact("A", "x" * 301) == "length"
    assert s.add_fact("A", "likes  tea\n") == "added"
    assert s.add_fact("a", "LIKES TEA") == "duplicate"
    for i in range(MAX_FACTS - 1):
        assert s.add_fact("a", f"fact number {i}") == "added"
    assert s.add_fact("a", "one too many") == "full"
    assert s.get_facts("A")[0] == "likes tea"


def test_forget_fact_picks_the_best_match(tmp_path):
    s = ClaudeState(tmp_path / "c.json")
    s.add_fact("a", "I live in Florida")
    s.add_fact("a", "my dog is called Rex")
    assert s.forget_fact("a", "florida") == "I live in Florida"
    assert s.forget_fact("a", "nothing matches this") is None
    assert s.get_facts("a") == ["my dog is called Rex"]


def test_round_trip_and_file_mode(tmp_path):
    path = tmp_path / "c.json"
    s = ClaudeState(path)
    s.add_fact("a", "likes tea a lot")
    s.set_pref("a", "tz", "UTC")
    s.set_persona("#c", "a pirate")
    s.set_persona("#c", "a butler", "b")
    s.set_switch("#c", "talkback", True)
    s.log_line("#c", "a", "hello")
    s.save()
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    t = ClaudeState(path)
    t.load()
    assert t.get_facts("A") == ["likes tea a lot"]
    assert t.get_pref("a", "tz") == "UTC"
    assert t.persona_for("#C", "B") == ("a pirate", "a butler")
    assert t.switch("#c", "talkback", False) is True
    assert [r[2] for r in t.recent("#c")] == ["hello"]


def test_corrupt_file_loads_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    s = ClaudeState(path)
    s.load()
    assert s.facts == {}
    path.write_text(json.dumps({"facts": ["wrong shape"]}))
    s.load()
    assert s.facts == {}


def test_save_without_changes_writes_nothing(tmp_path):
    path = tmp_path / "c.json"
    ClaudeState(path).save()
    assert not path.exists()


def test_backlog_is_bounded_by_count_and_age(tmp_path):
    s = ClaudeState(tmp_path / "c.json")
    for i in range(BACKLOG_LINES + 10):
        s.log_line("#c", "a", f"m{i}", now=1000.0 + i)
    rows = s.recent("#c")
    assert len(rows) == BACKLOG_LINES and rows[-1][2] == f"m{BACKLOG_LINES + 9}"
    s.prune(now=1000.0 + BACKLOG_LINES + 10 + BACKLOG_MAX_AGE)
    assert s.recent("#c") == []


def test_recent_filters_by_nick(tmp_path):
    s = ClaudeState(tmp_path / "c.json")
    s.log_line("#c", "Bob", "one")
    s.log_line("#c", "alice", "two")
    assert [r[2] for r in s.recent("#c", nick="bob")] == ["one"]


def test_state_forget_counts_and_spares_others(tmp_path):
    s = ClaudeState(tmp_path / "c.json")
    s.add_fact("a", "likes tea a lot")
    s.set_pref("a", "tz", "UTC")
    s.set_persona("#c", "a butler", "a")
    s.log_line("#c", "A", "hi")
    s.log_line("#c", "b", "hi")
    assert s.forget("a") == 4
    assert s.recent("#c") == [s.recent("#c")[0]] and s.recent("#c")[0][1] == "b"
    assert s.forget("a") == 0


# --- conversations --------------------------------------------------------------

def test_history_is_bounded_and_starts_with_a_user_turn():
    c = Conversations()
    for i in range(HISTORY_TURNS):
        c.append("#c", "a", f"q{i}", f"r{i}", now=0)
    turns = c.get("#c", "a", now=0)
    assert len(turns) == HISTORY_TURNS
    assert turns[0]["role"] == "user"
    assert turns[-1] == {"role": "assistant", "content": f"r{HISTORY_TURNS - 1}"}


def test_history_expires_when_idle():
    c = Conversations()
    c.append("#c", "a", "q", "r", now=0)
    assert c.get("#c", "A", now=HISTORY_IDLE - 1)
    assert c.get("#c", "a", now=HISTORY_IDLE + 1) == []


def test_history_key_count_is_bounded():
    c = Conversations()
    for i in range(HISTORY_KEYS + 5):
        c.append("#c", f"n{i}", "q", "r", now=0)
    assert c.get("#c", "n0", now=0) == []
    assert c.get("#c", f"n{HISTORY_KEYS + 4}", now=0)


def test_history_reset_and_forget():
    c = Conversations()
    c.append("#c", "a", "q", "r", now=0)
    c.append("#c", "b", "q", "r", now=0)
    c.append("#d", "a", "q", "r", now=0)
    assert c.reset("#c", "a") == 1
    assert c.forget("a") == 1
    assert c.reset("#c") == 1
