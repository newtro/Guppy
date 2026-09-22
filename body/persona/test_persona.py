"""Tests for the persona prompt: the voice rules the Reflex is built on must stay present and well-formed."""
from __future__ import annotations

from pathlib import Path

import pytest

PERSONA = Path(__file__).resolve().parent / "guppy.md"


@pytest.fixture(scope="module")
def persona() -> str:
    return PERSONA.read_text()


def test_persona_is_not_empty(persona: str) -> None:
    assert persona.strip(), "the kernel rejects an empty persona"


def test_addresses_the_user_as_admiral(persona: str) -> None:
    assert "Admiral" in persona


def test_mood_tags_are_listed(persona: str) -> None:
    for tag in ("[deadpan]", "[smug]", "[exasperated]", "[alarmed]", "[pleased]"):
        assert tag in persona


def test_thanks_rule_is_present(persona: str) -> None:
    """Thanks gets a dry deflection, not 'you're welcome'."""
    rule = next((ln for ln in persona.splitlines() if "thanks you" in ln), None)
    assert rule is not None, "no rule covering what to say when the Admiral says thank you"
    assert "Just doing my job, Admiral." in rule


def test_thanks_rule_asks_for_variation(persona: str) -> None:
    block = persona.split("thanks you", 1)[1][:400].lower()
    assert "vary" in block or "variation" in block, "the reply must vary, not be a canned line"


def test_thanks_rule_is_a_voice_rule(persona: str) -> None:
    """It belongs in the Voice rules block, above the section on delegating work."""
    voice = persona.index("Voice rules:")
    work = persona.index("How you get real work done:")
    assert voice < persona.index("thanks you") < work


def test_persona_stays_speakable(persona: str) -> None:
    """Everything here is spoken aloud, so keep characters a TTS engine handles plainly."""
    for bad in ("—", "…", "“", "”", "‘", "’"):
        assert bad not in persona, f"{bad!r} does not speak well"
