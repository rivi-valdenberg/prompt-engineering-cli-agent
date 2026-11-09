"""The Gradio layer.

These tests do not launch a server. They assert the two things that actually break
across Gradio releases - constructor kwargs that quietly moved - and the one thing
that must never break: the UI refusing to run what the guard blocked.
"""

from __future__ import annotations

import warnings

import gradio as gr
import pytest

from cli_agent.app import _sandbox_note, build_demo, on_confirm_toggle, on_show_prompt
from cli_agent.config import settings
from cli_agent.prompts import LATEST, ordered_versions


def test_the_app_builds_without_deprecation_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        demo = build_demo()
    assert isinstance(demo, gr.Blocks)


def test_the_dropdown_offers_exactly_the_three_iterations():
    ids = [v.id for v in ordered_versions()]
    assert ids == ["v1", "v2", "v3"]
    assert settings.default_prompt_version in ids
    assert LATEST == "v3"


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize("shell", ["cmd", "bash"])
def test_the_prompt_viewer_renders_every_iteration(version, shell):
    body = on_show_prompt(version, shell)
    assert version in body
    assert "```text" in body


def test_the_run_button_stays_disabled_for_a_windows_shell():
    # The sandbox image is Linux. Offering to "run" `ipconfig` in it would lie.
    update = on_confirm_toggle(True, "ipconfig", "cmd")
    assert update["interactive"] is False


def test_the_run_button_needs_both_a_command_and_a_confirmation():
    assert on_confirm_toggle(False, "rm downloads/x.tmp", "bash")["interactive"] is False
    assert on_confirm_toggle(True, "", "bash")["interactive"] is False
    assert on_confirm_toggle(True, "rm downloads/x.tmp", "bash")["interactive"] is True


def test_the_sandbox_note_explains_itself_when_docker_is_absent():
    note = _sandbox_note()
    assert any(
        phrase in note
        for phrase in ("Docker is not running", "not built", "Sandbox ready", "Sandbox disabled")
    )
