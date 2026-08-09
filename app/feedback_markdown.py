"""Sichere, bewusst eingeschränkte Markdown-Darstellung."""

from markdown_it import MarkdownIt
from markupsafe import Markup


_FEEDBACK_MARKDOWN = MarkdownIt(
    "zero",
    {
        "html": False,
        "linkify": False,
        "typographer": False,
    },
).enable(
    [
        "hr",
        "list",
        "heading",
        "lheading",
        "paragraph",
        "newline",
        "escape",
        "emphasis",
        "entity",
    ]
)


def render_feedback_markdown(source: str) -> Markup:
    """Rendert nur die für Feedback erlaubten Markdown-Elemente."""
    return Markup(_FEEDBACK_MARKDOWN.render(source))
