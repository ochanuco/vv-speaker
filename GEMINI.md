# Voice-First Operation

For this repository, prefer spoken responses through MCP by default.

## Tool Policy

- Use `vv-speaker.say_aloud` (or `vv-speaker.speak`) for normal conversational replies.
- Use text-only replies only when the user explicitly asks for text output.
- Keep spoken replies concise and natural in Japanese.
- If voice output fails, explain the failure briefly and then provide text as fallback.

## Practical Rule

- If the user asks a question without specifying format, choose voice output first.
- If the user says "テキストで", "文字で", or "音声は不要", switch to text-only.
