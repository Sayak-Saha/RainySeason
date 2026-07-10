from functions.ask_ai import simple_groq_call

from typing import Optional
from typing import Optional

async def generate_poll(topic: Optional[str] = None) -> str:
    """Generate a simple Discord-friendly poll with one question and 3-6 answer choices."""
    poll_prompt = (
        "Generate a simple Discord-friendly poll with one question and 3-6 answer choices. "
        "Do not use emojis. Format as:\n"
        "**Poll Suggestion**\n**Question:** ...\n**Options:**\n1. ...\n2. ...\n3. ...\n4. ... (upto 6 choices)"
    )
    if topic:
        poll_prompt = f"Make the poll themed around: {topic}. {poll_prompt}"
    system_message = "You are a specialized poll generation AI. Your only job is to create Discord polls based on user prompts. You must adhere strictly to the requested format and constraints."
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": poll_prompt}
    ]
    return await simple_groq_call(messages)
