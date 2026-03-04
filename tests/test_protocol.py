"""Tests for protocol.py — pure IRC protocol helpers."""

import pytest
from protocol import (
    strip_tags,
    parse_isupport_chanmodes,
    parse_isupport_prefix,
    parse_mode_changes,
    parse_names_entry,
    sasl_plain_payload,
)


class TestStripTags:
    def test_no_tags(self):
        assert strip_tags(":server 001 nick :Welcome") == ":server 001 nick :Welcome"

    def test_with_tags(self):
        line = "@time=2026-01-01T00:00:00Z :server PRIVMSG #test :hello"
        assert strip_tags(line) == ":server PRIVMSG #test :hello"

    def test_empty(self):
        assert strip_tags("") == ""


class TestParseIsupportChanmodes:
    def test_standard(self):
        types = parse_isupport_chanmodes("beI,k,l,imntpsCR")
        assert types["b"] == "A"
        assert types["e"] == "A"
        assert types["I"] == "A"
        assert types["k"] == "B"
        assert types["l"] == "C"
        assert types["i"] == "D"
        assert types["m"] == "D"

    def test_chatnplay(self):
        """Real CHANMODES from ChatNPlay/ProvisionIRCd."""
        types = parse_isupport_chanmodes("beI,kL,lH,imtncSRMrsCTNVOzQ")
        assert types["L"] == "B"  # the mode that caused desync
        assert types["H"] == "C"
        assert types["b"] == "A"

    def test_empty(self):
        types = parse_isupport_chanmodes("")
        assert types == {}


class TestParseIsupportPrefix:
    def test_standard(self):
        modes, sym_map = parse_isupport_prefix("(qaohv)~&@%+")
        assert modes == {"q", "a", "o", "h", "v"}
        assert sym_map["~"] == "q"
        assert sym_map["@"] == "o"
        assert sym_map["+"] == "v"

    def test_minimal(self):
        modes, sym_map = parse_isupport_prefix("(ov)@+")
        assert modes == {"o", "v"}

    def test_invalid(self):
        modes, sym_map = parse_isupport_prefix("garbage")
        assert modes == set()
        assert sym_map == {}


class TestParseModeChanges:
    CHANMODES = parse_isupport_chanmodes("beI,kL,lH,imtncSRMrsCTNVOzQ")
    PREFIX = {"q", "a", "o", "h", "v"}

    def test_simple_op(self):
        changes = parse_mode_changes("+o", ["nick"], self.PREFIX, self.CHANMODES)
        assert changes == [(True, "o", "nick")]

    def test_deop(self):
        changes = parse_mode_changes("-o", ["nick"], self.PREFIX, self.CHANMODES)
        assert changes == [(False, "o", "nick")]

    def test_loq_desync_fix(self):
        """The bug that caused chanop tracking corruption."""
        changes = parse_mode_changes(
            "+Loq", ["#overflow", "admin", "owner"],
            self.PREFIX, self.CHANMODES,
        )
        # L is type B → consumes #overflow
        assert changes[0] == (True, "L", "#overflow")
        # o → consumes admin
        assert changes[1] == (True, "o", "admin")
        # q → consumes owner
        assert changes[2] == (True, "q", "owner")

    def test_minus_l_no_param(self):
        """Type C mode: param on set, no param on unset."""
        changes = parse_mode_changes("-lo", ["nick"], self.PREFIX, self.CHANMODES)
        assert changes[0] == (False, "l", None)  # -l: no param
        assert changes[1] == (False, "o", "nick")

    def test_plus_l_has_param(self):
        changes = parse_mode_changes("+lo", ["50", "nick"], self.PREFIX, self.CHANMODES)
        assert changes[0] == (True, "l", "50")
        assert changes[1] == (True, "o", "nick")

    def test_type_d_no_param(self):
        changes = parse_mode_changes("+im", [], self.PREFIX, self.CHANMODES)
        assert changes == [(True, "i", None), (True, "m", None)]

    def test_complex_multi_mode(self):
        changes = parse_mode_changes(
            "+bkoq-v",
            ["*!*@bad", "secret", "the_op", "the_owner", "demoted"],
            self.PREFIX, self.CHANMODES,
        )
        assert len(changes) == 5
        assert changes[0] == (True, "b", "*!*@bad")
        assert changes[1] == (True, "k", "secret")
        assert changes[2] == (True, "o", "the_op")
        assert changes[3] == (True, "q", "the_owner")
        assert changes[4] == (False, "v", "demoted")


class TestParseNamesEntry:
    def test_plain_nick(self):
        nick, is_op = parse_names_entry("someuser")
        assert nick == "someuser"
        assert not is_op

    def test_op(self):
        nick, is_op = parse_names_entry("@someuser")
        assert nick == "someuser"
        assert is_op

    def test_owner(self):
        nick, is_op = parse_names_entry("~someuser")
        assert nick == "someuser"
        assert is_op

    def test_multi_prefix(self):
        nick, is_op = parse_names_entry("~&@someuser")
        assert nick == "someuser"
        assert is_op

    def test_voice_not_op(self):
        nick, is_op = parse_names_entry("+someuser")
        assert nick == "someuser"
        assert not is_op

    def test_halfop_not_op(self):
        nick, is_op = parse_names_entry("%someuser")
        assert nick == "someuser"
        assert not is_op


class TestSaslPlainPayload:
    def test_basic(self):
        import base64
        payload = sasl_plain_payload("mybot", "secret123")
        decoded = base64.b64decode(payload).decode("utf-8")
        assert decoded == "\0mybot\0secret123"

    def test_unicode(self):
        import base64
        payload = sasl_plain_payload("bot", "pässwörd")
        decoded = base64.b64decode(payload).decode("utf-8")
        assert decoded == "\0bot\0pässwörd"
