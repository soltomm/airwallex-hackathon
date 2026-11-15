"""The Claude client shared by the seller, the email reader and the scripted buyer."""
from __future__ import annotations

import os

import anthropic


def make_client() -> anthropic.Anthropic:
    """User-level API keys are not tied to a workspace; they must name one per request."""
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
