"""Tests for modules/encode.py - pure offline codecs & generators.

Exercises the module-level pure functions directly (no bot needed):
happy path, bad input, and edge cases for each command.
"""

import re
import sys
sys.path.insert(0, ".")

from modules.encode import (
    _unicode, _hash, _crc, _b32, _slug, _ulid, _ascii, _ds,
    _defang, _entropy, _pw, _lorem, _block_of, _human_time,
)


class TestUnicode:
    def test_single_char(self):
        out = _unicode("A")
        assert "U+0041" in out
        assert "LATIN CAPITAL LETTER A" in out
        assert "cat Lu" in out
        assert "UTF-8 41" in out
        assert "Basic Latin" in out

    def test_u_plus_form(self):
        assert "U+1F600" in _unicode("U+1F600")
        assert "Emoticons" in _unicode("U+1F600")

    def test_bare_hex(self):
        assert "U+00E9" in _unicode("e9")

    def test_by_name(self):
        out = _unicode("SNOWMAN")
        assert "U+2603" in out

    def test_empty(self):
        assert _unicode("").startswith("usage:")

    def test_unknown_name(self):
        assert "no character named" in _unicode("notarealname zzz")

    def test_multibyte_utf8(self):
        # é is 2 UTF-8 bytes
        out = _unicode("é")
        assert "U+00E9" in out
        assert "C3 A9" in out


class TestHash:
    def test_default_sha256(self):
        out = _hash("hello")
        assert out.startswith("sha256:")
        assert "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in out

    def test_md5(self):
        out = _hash("md5 hello")
        assert out == "md5: 5d41402abc4b2a76b9719d911017c592"

    def test_sha1(self):
        assert _hash("sha1 abc").startswith("sha1: a9993e364706816aba3e25717850c26c9cd0d89d")

    def test_blake2b(self):
        assert _hash("blake2b x").startswith("blake2b: ")

    def test_empty(self):
        assert _hash("").startswith("usage:")

    def test_algo_only_no_text(self):
        assert _hash("sha256").startswith("usage:")

    def test_non_algo_first_token_hashed_whole(self):
        # "hello world" - first token not an algo, hash whole string sha256
        out = _hash("hello world")
        assert out.startswith("sha256:")


class TestCrc:
    def test_known(self):
        out = _crc("hello")
        assert "CRC32 3610a686" in out
        assert "Adler-32 062c0215" in out

    def test_empty(self):
        assert _crc("").startswith("usage:")

    def test_format(self):
        out = _crc("x")
        assert re.search(r"CRC32 [0-9a-f]{8}", out)
        assert re.search(r"Adler-32 [0-9a-f]{8}", out)


class TestB32:
    def test_encode(self):
        # "foobar" -> MZXW6YTBOI======
        assert _b32("foobar") == "MZXW6YTBOI======"

    def test_decode_roundtrip(self):
        enc = _b32("hello world")
        assert _b32(enc) == "hello world"

    def test_decode_known(self):
        assert _b32("MZXW6YTBOI======") == "foobar"

    def test_empty(self):
        assert _b32("").startswith("usage:")

    def test_non_b32_encoded(self):
        # contains chars not in b32 alphabet -> treated as plaintext, encoded
        out = _b32("Hi!")
        assert out and out == _b32("Hi!")
        # encoding is reversible
        assert _b32(out) == "Hi!"


class TestSlug:
    def test_basic(self):
        assert _slug("Hello World") == "hello-world"

    def test_accents(self):
        assert _slug("Crème Brûlée") == "creme-brulee"

    def test_collapse_and_trim(self):
        assert _slug("  --Foo___Bar!!  ") == "foo-bar"

    def test_empty(self):
        assert _slug("").startswith("usage:")

    def test_only_symbols(self):
        assert _slug("!!!@@@") == "(empty)"


class TestUlid:
    def test_length_and_alphabet(self):
        u = _ulid()
        assert len(u) == 26
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]+", u)

    def test_unique(self):
        assert _ulid() != _ulid()


class TestAscii:
    def test_char(self):
        out = _ascii("A")
        assert "dec 65" in out
        assert "hex 41" in out
        assert "oct 101" in out

    def test_decimal(self):
        assert "hex 41" in _ascii("65")

    def test_hex_prefixed(self):
        assert "dec 65" in _ascii("0x41")

    def test_control_name(self):
        out = _ascii("0x00")  # NUL
        assert "NUL" in out
        out2 = _ascii("0x07")  # BEL
        assert "BEL" in out2

    def test_space(self):
        assert "SPACE" in _ascii("32")

    def test_no_arg_brief(self):
        assert "printable" in _ascii("").lower()

    def test_out_of_range(self):
        assert "single char" in _ascii("999")


class TestDs:
    def test_gb(self):
        out = _ds("1.5 GB")
        assert "1500000000 bytes" in out.replace(",", "")
        assert "GiB" in out

    def test_attached_unit(self):
        out = _ds("1GB")
        assert "1000000000 bytes" in out.replace(",", "")

    def test_binary_unit(self):
        out = _ds("1 GiB")
        assert "1073741824 bytes" in out.replace(",", "")

    def test_bad_value(self):
        assert "number" in _ds("foo GB")

    def test_unknown_unit(self):
        assert "unknown unit" in _ds("5 zz")

    def test_usage(self):
        assert _ds("").startswith("usage:")


class TestDefang:
    def test_defang_url(self):
        out = _defang("https://evil.com/x")
        assert out.startswith("defanged:")
        assert "hxxps" in out
        assert "[.]" in out

    def test_refang(self):
        out = _defang("hxxps[:]//evil[.]com")
        assert out.startswith("refanged:")
        assert "https://evil.com" in out

    def test_email(self):
        out = _defang("a@b.com")
        assert "[@]" in out
        assert "[.]" in out

    def test_roundtrip(self):
        original = "http://test.org"
        defanged = _defang(original).split(": ", 1)[1]
        refanged = _defang(defanged).split(": ", 1)[1]
        assert refanged == original

    def test_empty(self):
        assert _defang("").startswith("usage:")


class TestEntropy:
    def test_strong(self):
        out = _entropy("Tr0ub4dor&3xKvm9q")
        assert "bits" in out
        assert "pool" in out

    def test_weak(self):
        out = _entropy("abc")
        assert "very weak" in out or "weak" in out

    def test_empty(self):
        assert _entropy("").startswith("usage:")

    def test_pool_calculation(self):
        # all four classes present
        out = _entropy("aA1!aA1!aA1!")
        assert "pool 95" in out


class TestPw:
    def test_default_length(self):
        assert len(_pw("")) == 16

    def test_custom_length(self):
        assert len(_pw("24")) == 24

    def test_clamped_min(self):
        assert len(_pw("2")) == 8

    def test_clamped_max(self):
        assert len(_pw("999")) == 64

    def test_passphrase(self):
        out = _pw("-s")
        assert "-" in out
        # at least 3 words
        assert out.count("-") >= 2

    def test_unique(self):
        assert _pw("20") != _pw("20")


class TestLorem:
    def test_default(self):
        out = _lorem("")
        assert len(out.split()) == 20
        assert out.startswith("Lorem")
        assert out.endswith(".")

    def test_count(self):
        assert len(_lorem("5").split()) == 5

    def test_cap(self):
        assert len(_lorem("999").split()) == 60

    def test_bad_arg(self):
        assert _lorem("abc").startswith("usage:")


class TestHelpers:
    def test_block_of_known(self):
        assert _block_of(0x41) == "Basic Latin"

    def test_block_of_unknown(self):
        assert "Unknown" in _block_of(0x10FFFE)

    def test_human_time_instant(self):
        assert _human_time(0.5) == "instant"

    def test_human_time_years(self):
        assert _human_time(31_557_600 * 5).endswith("y")
