"""
Run this script ONCE locally to generate your Pyrogram/Hydrogram session string.
Then copy the printed SESSION_STRING into Koyeb environment variables.

Requirements:
    pip install hydrogram tgcrypto
"""

import asyncio
from pyrogram import Client

API_ID = int(input("Enter your API_ID: ").strip())
API_HASH = input("Enter your API_HASH: ").strip()

async def generate():
    async with Client(
        "session_generator",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    ) as client:
        session = await client.export_session_string()
        print("\n" + "="*60)
        print("✅ YOUR SESSION STRING (copy this to Koyeb):")
        print("="*60)
        print(session)
        print("="*60 + "\n")
        print("Add this as SESSION_STRING in Koyeb Environment Variables.")

asyncio.run(generate())
