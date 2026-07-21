"""
Generate a Pyrogram SESSION_STRING and save a copy to Telegram Saved Messages.

Run ONCE on your PC (needs phone login / code):

    pip install pyrogram tgcrypto
    python generate_session.py

Then paste SESSION_STRING into Koyeb env vars.
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
        in_memory=True,
    ) as client:
        session = await client.export_session_string()
        me = await client.get_me()
        name = (me.first_name or "") + (f" {me.last_name}" if me.last_name else "")

        print("\n" + "=" * 60)
        print(f"Logged in as: {name} (@{me.username or 'no_username'}) id={me.id}")
        print("YOUR SESSION STRING (also sent to Saved Messages):")
        print("=" * 60)
        print(session)
        print("=" * 60 + "\n")

        # Saved Messages = chat "me"
        await client.send_message(
            "me",
            (
                "🔐 **Telegram Streamer — SESSION_STRING**\n\n"
                "Paste this as `SESSION_STRING` on Koyeb.\n"
                "Keep private. Anyone with this string can use your account.\n\n"
                f"`{session}`"
            ),
            parse_mode="markdown",
        )
        print("Saved a copy to Telegram → Saved Messages.")
        print("Add SESSION_STRING in Koyeb Environment Variables, then restart the service.")


if __name__ == "__main__":
    asyncio.run(generate())
