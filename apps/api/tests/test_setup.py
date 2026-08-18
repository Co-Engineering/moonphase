"""First run, and the two gates it controls.

The point of this module is that installing does not end with "now edit a file
on the server". What is worth testing is the part that has teeth: which
hostnames the proxy may obtain a certificate for, and whether a stranger may
create an account.
"""

from __future__ import annotations

from moonphase.routers.setup import host_of


def test_a_url_reduces_to_the_hostname_a_certificate_is_for() -> None:
    assert host_of("https://moonphase.example.com") == "moonphase.example.com"
    assert host_of("http://moonphase.example.com/") == "moonphase.example.com"
    assert host_of("moonphase.example.com") == "moonphase.example.com"


def test_a_port_is_not_part_of_the_hostname() -> None:
    """Caddy asks about the name, never the port it arrived on."""
    assert host_of("https://moonphase.example.com:8471") == "moonphase.example.com"


def test_case_does_not_decide_whether_a_certificate_is_issued() -> None:
    assert host_of("HTTPS://Moonphase.Example.COM") == "moonphase.example.com"


def test_a_path_is_ignored() -> None:
    assert host_of("https://moonphase.example.com/setup?x=1") == "moonphase.example.com"


def test_nothing_reduces_to_nothing() -> None:
    # Which the endpoint answers 400 to, rather than approving.
    assert host_of("") == ""
    assert host_of("   ") == ""
