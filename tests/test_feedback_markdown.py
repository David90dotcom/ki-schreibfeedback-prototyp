from __future__ import annotations

import unittest

from app.feedback_markdown import (
    render_feedback_inline_markdown,
    render_feedback_markdown,
)


class FeedbackMarkdownTests(unittest.TestCase):
    def test_supported_feedback_structure_is_rendered(self) -> None:
        rendered = str(
            render_feedback_markdown(
                """---

### **1. Gesamteindruck**

- **Stärke:** klare Gliederung
- verständliche Sprache

1. Erster Hinweis
2. Zweiter Hinweis
"""
            )
        )

        self.assertIn("<hr>", rendered)
        self.assertIn(
            "<h3><strong>1. Gesamteindruck</strong></h3>",
            rendered,
        )
        self.assertIn("<ul>", rendered)
        self.assertIn("<ol>", rendered)
        self.assertIn(
            "<strong>Stärke:</strong> klare Gliederung",
            rendered,
        )

    def test_html_links_images_and_code_are_not_activated(self) -> None:
        rendered = str(
            render_feedback_markdown(
                """<script>alert("xss")</script>

[Link](https://example.com)

![Bild](https://example.com/bild.png)

`Code`
"""
            )
        )

        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<a ", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<code", rendered)

    def test_inline_feedback_renders_emphasis_without_activating_html(
        self,
    ) -> None:
        rendered = str(
            render_feedback_inline_markdown(
                "**Wirkung** und *Aussagebezug* "
                "<script>alert('xss')</script>"
            )
        )

        self.assertIn("<strong>Wirkung</strong>", rendered)
        self.assertIn("<em>Aussagebezug</em>", rendered)
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<p>", rendered)


if __name__ == "__main__":
    unittest.main()
