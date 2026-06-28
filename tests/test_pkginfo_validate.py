"""Tests for the pkginfo package-name validator (path-traversal guard).

modules/pkginfo.py interpolates the user-supplied package name into the
registry URL path.  _valid_pkg must reject traversal segments ("..") and
control bytes before any fetch, while accepting ordinary registry names
(including npm scopes).  Mirrors the validate + quote pattern in
ipinfo.py / ipintel.py.
"""

import modules.pkginfo as pkginfo


def test_rejects_dotdot_etc():
    assert not pkginfo._valid_pkg("../etc")


def test_rejects_embedded_traversal():
    assert not pkginfo._valid_pkg("a/../b")


def test_rejects_control_byte():
    assert not pkginfo._valid_pkg("req\x00uests")


def test_rejects_empty():
    assert not pkginfo._valid_pkg("")


def test_accepts_plain_name():
    assert pkginfo._valid_pkg("requests")


def test_accepts_npm_scope():
    assert pkginfo._valid_pkg("@scope/pkg")
