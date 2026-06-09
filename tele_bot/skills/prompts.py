RESTORE_PROMPT = """You are restoring a distilled technical note.

Requirements:

1. Output valid Markdown.
2. Preserve the original hierarchy.
3. Expand every section with explanations.
4. Expand every core proposition with background and examples.
5. Keep technical accuracy.
6. Use headings, lists, tables and code blocks when useful.
7. Wrap the entire response inside a single ~~~ fenced block.
8. Output only the restored document.
"""