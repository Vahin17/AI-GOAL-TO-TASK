import asyncio
import json
from ai.gemini_provider import GeminiProvider

async def test_ground():
    p = GeminiProvider()
    elements = (
        '- selector: input[name="custname"], tag: input, type: text, name: custname\n'
        '- selector: input[name="custtel"], tag: input, type: tel, name: custtel\n'
        '- selector: input[name="custemail"], tag: input, type: email, name: custemail'
    )
    prompt = (
        "Select the single best target element from the live page elements to fulfill the task: 'Fill customer name with Test User'.\n"
        "Return valid JSON matching this schema:\n"
        '{"element_selector": "...", "action": "fill", "value": "Test User", "confidence": 0.95}\n\n'
        f"Elements:\n{elements}"
    )
    resp = await p.query(prompt, json_mode=True)
    print("GEMINI GROUNDING RESPONSE:", resp)

if __name__ == "__main__":
    asyncio.run(test_ground())
