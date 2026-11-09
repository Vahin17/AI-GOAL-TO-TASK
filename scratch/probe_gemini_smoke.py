import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath("backend"))
from ai.gemini_provider import GeminiProvider

async def main():
    p = GeminiProvider()
    print("Querying Gemini with model:", p.model_name)
    res = await p.query("Say hello in one word", json_mode=False)
    print("Gemini response:", res.strip())

if __name__ == "__main__":
    asyncio.run(main())
