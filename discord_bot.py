"""Discord bot: only works in servers and for members approved in the dashboard."""
import asyncio
import io
import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import db
import providers
from providers import LABELS, PROVIDERS, VIDEO_PROVIDERS

load_dotenv()
log = logging.getLogger("imagebot")

TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
DEFAULT_PROVIDER = os.getenv("IMAGE_PROVIDER", "pollinations").lower()  # claude | grok | pollinations
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = "pollinations"
DEFAULT_VIDEO_PROVIDER = os.getenv("VIDEO_PROVIDER", "grok").lower()
if DEFAULT_VIDEO_PROVIDER not in VIDEO_PROVIDERS:
    DEFAULT_VIDEO_PROVIDER = "grok"
MIN_INTERVAL_MIN = int(os.getenv("MIN_INTERVAL_MINUTES", "10"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT_PER_USER", "0"))  # 0 = unlimited

run = asyncio.to_thread


def clean(msg: str) -> str:
    """Never let a secret end up in logs or the dashboard."""
    for secret in (*providers.secrets_list(), TOKEN):
        if secret:
            msg = msg.replace(secret, "***")
    return msg[:300]


async def deny(interaction: discord.Interaction, text: str):
    if not interaction.response.is_done():
        await interaction.response.send_message(text, ephemeral=True)


async def log_use(interaction, command, prompt, status, ms, error=""):
    g = interaction.guild
    await run(
        db.log_usage, interaction.user.id, str(interaction.user), interaction.guild_id,
        g.name if g else "", command, prompt, status, ms, error,
    )


# ---------- access control ----------
class LockedTree(app_commands.CommandTree):
    """Runs before every command: server must be allowed AND member approved."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await run(db.allowed_guild_ids)
        if interaction.guild_id is None or interaction.guild_id not in allowed:
            await deny(interaction, "This bot is private and isn't available in this server.")
            return False

        uid = interaction.user.id
        if uid in OWNER_IDS:
            return True

        cmd = interaction.command.qualified_name if interaction.command else "?"
        if cmd == "register":
            return True

        status = await run(db.member_status, uid)
        if status == "approved":
            return True

        cmd = interaction.command.qualified_name if interaction.command else "?"
        if status == "denied":
            await log_use(interaction, cmd, "", "denied", 0, "access denied")
            await deny(interaction, "Your access to this bot was declined by the owner.")
            return False

        await run(db.request_access, uid, str(interaction.user), interaction.guild_id)
        await log_use(interaction, cmd, "", "denied", 0, "awaiting approval")
        await deny(
            interaction,
            "🔒 You're not approved yet. I sent an access request to the owner. "
            "You can use the bot once it's approved.",
        )
        return False


class ImageBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = LockedTree(self)

    async def setup_hook(self):
        reconcile.start()
        scheduler.start()

    async def on_ready(self):
        log.info("Bot logged in as %s", self.user)

    async def on_guild_join(self, guild: discord.Guild):
        await do_reconcile()


bot = ImageBot()
synced: set[int] = set()


async def do_reconcile():
    """Leave servers that aren't approved; sync commands into approved ones."""
    allowed = await run(db.allowed_guild_ids)
    for g in list(bot.guilds):
        if g.id not in allowed:
            log.warning("Leaving unapproved server %s (%s)", g.name, g.id)
            await g.leave()
            continue
        if g.id not in synced:
            try:
                obj = discord.Object(id=g.id)
                bot.tree.copy_global_to(guild=obj)
                await bot.tree.sync(guild=obj)
                synced.add(g.id)
                await run(db.set_guild_name, g.id, g.name)
                log.info("Commands synced to %s", g.name)
            except Exception:
                log.exception("Could not sync commands to %s", g.id)
    synced.intersection_update(allowed)


@tasks.loop(seconds=30)
async def reconcile():
    try:
        await do_reconcile()
    except Exception:
        log.exception("reconcile failed")


@reconcile.before_loop
async def _wait_reconcile():
    await bot.wait_until_ready()


# ---------- helpers ----------
IMAGE_CHOICES = [app_commands.Choice(name=LABELS[p], value=p) for p in PROVIDERS]
VIDEO_CHOICES = [app_commands.Choice(name=LABELS[p], value=p) for p in VIDEO_PROVIDERS]


def to_file(data: bytes, name: str = "image.png") -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


async def read_prompt(interaction: discord.Interaction, prompt, prompt_file) -> str | None:
    """Returns the prompt, or None after telling the user what is wrong."""
    if prompt_file:
        if not prompt_file.filename.lower().endswith(".txt"):
            await interaction.response.send_message("Please upload a .txt file.", ephemeral=True)
            return None
        try:
            prompt = (await prompt_file.read()).decode("utf-8-sig")
        except Exception:
            await interaction.response.send_message("Cannot read TXT file. Please upload UTF-8 text.", ephemeral=True)
            return None
        if len(prompt) > 50000:
            await interaction.response.send_message("TXT prompt is too long. Maximum is 50,000 characters.", ephemeral=True)
            return None
    elif prompt:
        if len(prompt) > 4000:
            await interaction.response.send_message("Direct prompt is too long. Maximum is 4,000 characters.", ephemeral=True)
            return None
    else:
        await interaction.response.send_message("Enter a prompt or upload a TXT file.", ephemeral=True)
        return None
    if not prompt.strip():
        await interaction.response.send_message("Prompt is empty.", ephemeral=True)
        return None
    return prompt


async def over_daily_limit(interaction, command: str, prompt: str) -> bool:
    uid = interaction.user.id
    if DAILY_LIMIT and uid not in OWNER_IDS:
        used = await run(db.count_recent_ok, uid, 24)
        if used >= DAILY_LIMIT:
            await log_use(interaction, command, prompt, "denied", 0, "daily limit")
            await interaction.response.send_message(
                f"You've reached the limit of {DAILY_LIMIT} creations per 24 hours.", ephemeral=True
            )
            return True
    return False


def ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


@bot.tree.command(name="register", description="Request access to this bot")
@app_commands.guild_only()
async def register(interaction: discord.Interaction):
    uid = interaction.user.id
    username = str(interaction.user)
    guild_id = interaction.guild_id

    status = await run(db.request_access, uid, username, guild_id)

    if status == "pending":
        message = "Registration submitted! Please wait for owner approval."
    elif status == "approved":
        message = "You are already approved! You can use the bot."
    elif status == "denied":
        message = "Your registration was declined by the owner."
    else:
        message = f"Registration status: {status}"

    await interaction.response.send_message(message, ephemeral=True)


# ---------- commands ----------
@bot.tree.command(name="imagine", description="Generate a picture (Claude, Grok or Pollinations)")
@app_commands.guild_only()
@app_commands.describe(
    prompt="Direct prompt text (max 4,000 characters)",
    provider="Who makes the picture",
    prompt_file="Optional TXT prompt file (max 50,000 characters)",
)
@app_commands.choices(provider=IMAGE_CHOICES)
@app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
async def imagine(
    interaction: discord.Interaction,
    prompt: str = None,
    provider: app_commands.Choice[str] = None,
    prompt_file: discord.Attachment = None,
):
    prompt = await read_prompt(interaction, prompt, prompt_file)
    if prompt is None:
        return
    prov = provider.value if provider else DEFAULT_PROVIDER
    cmd = f"imagine:{prov}"
    if await over_daily_limit(interaction, cmd, prompt):
        return

    await interaction.response.defer(thinking=True)
    t0 = time.monotonic()
    try:
        data = await providers.generate_image(prov, prompt)
    except Exception as e:
        await log_use(interaction, cmd, prompt, "error", ms_since(t0), clean(repr(e)))
        reason = f" ({e})" if isinstance(e, RuntimeError) else ""
        await interaction.followup.send(f"Generation failed{reason}. Please try again in a moment.")
        return

    await interaction.followup.send(
        f"**{LABELS[prov]}** · **Prompt:** {prompt[:500]}\n**Characters:** {len(prompt):,}",
        file=to_file(data),
    )
    await log_use(interaction, cmd, prompt, "ok", ms_since(t0))


@bot.tree.command(name="video", description="Generate a short video (Claude or Grok)")
@app_commands.guild_only()
@app_commands.describe(
    prompt="Direct prompt text (max 4,000 characters)",
    provider="Who makes the video",
    duration="Seconds (Grok only, 1-15). Claude makes a short animated clip.",
    prompt_file="Optional TXT prompt file (max 50,000 characters)",
)
@app_commands.choices(provider=VIDEO_CHOICES)
@app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id, i.user.id))
async def video(
    interaction: discord.Interaction,
    prompt: str = None,
    provider: app_commands.Choice[str] = None,
    duration: app_commands.Range[int, 1, 15] = 8,
    prompt_file: discord.Attachment = None,
):
    prompt = await read_prompt(interaction, prompt, prompt_file)
    if prompt is None:
        return
    prov = provider.value if provider else DEFAULT_VIDEO_PROVIDER
    cmd = f"video:{prov}"
    if await over_daily_limit(interaction, cmd, prompt):
        return

    await interaction.response.defer(thinking=True)
    t0 = time.monotonic()
    try:
        data = await providers.generate_video(prov, prompt, duration)
        limit = interaction.guild.filesize_limit if interaction.guild else 8 * 1024 * 1024
        if len(data) > limit:
            raise RuntimeError("The video is bigger than this server's upload limit")
    except Exception as e:
        await log_use(interaction, cmd, prompt, "error", ms_since(t0), clean(repr(e)))
        reason = f" ({e})" if isinstance(e, RuntimeError) else ""
        await interaction.followup.send(f"Video failed{reason}. Please try again in a moment.")
        return

    await interaction.followup.send(
        f"**{LABELS[prov]}** · **Prompt:** {prompt[:500]}",
        file=to_file(data, "video.mp4"),
    )
    await log_use(interaction, cmd, prompt, "ok", ms_since(t0))


auto = app_commands.Group(
    name="autoimage",
    description="Automatic scheduled image posting",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@auto.command(name="start", description="Post a generated image on a schedule")
@app_commands.describe(
    channel="Where to post",
    every_minutes="Interval in minutes",
    prompt="Direct prompt text (max 4,000 characters)",
    provider="Who makes the pictures",
    prompt_file="Optional TXT prompt file (max 50,000 characters)",
)
@app_commands.choices(provider=IMAGE_CHOICES)
async def auto_start(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    every_minutes: app_commands.Range[int, 1, 10080],
    prompt: str = None,
    provider: app_commands.Choice[str] = None,
    prompt_file: discord.Attachment = None,
):
    prompt = await read_prompt(interaction, prompt, prompt_file)
    if prompt is None:
        return
    if every_minutes < MIN_INTERVAL_MIN:
        await interaction.response.send_message(f"Minimum interval is {MIN_INTERVAL_MIN} minutes.", ephemeral=True)
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.attach_files):
        await interaction.response.send_message(
            f"I need Send Messages and Attach Files in {channel.mention}.", ephemeral=True
        )
        return
    prov = provider.value if provider else DEFAULT_PROVIDER
    await run(db.upsert_job, interaction.guild_id, channel.id, prompt, every_minutes * 60, interaction.user.id, prov)
    await interaction.response.send_message(
        f"Automation set: {LABELS[prov]} posting in {channel.mention} every {every_minutes} min."
    )


@auto.command(name="stop", description="Stop the automation in a channel")
async def auto_stop(interaction: discord.Interaction, channel: discord.TextChannel):
    if await run(db.remove_job, interaction.guild_id, channel.id):
        await interaction.response.send_message(f"Stopped automation in {channel.mention}.")
    else:
        await interaction.response.send_message("No automation in that channel.", ephemeral=True)


@auto.command(name="list", description="Show active automations")
async def auto_list(interaction: discord.Interaction):
    jobs = await run(db.list_jobs, interaction.guild_id)
    if not jobs:
        await interaction.response.send_message("No active automations.", ephemeral=True)
        return
    lines = [f"<#{j.channel_id}> every {j.interval_s // 60} min ({LABELS.get(j.provider, j.provider)}): {j.prompt[:80]}" for j in jobs]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(auto)


@bot.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down, try again in {error.retry_after:.0f}s."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------- scheduler ----------
@tasks.loop(seconds=30)
async def scheduler():
    try:
        allowed = await run(db.allowed_guild_ids)
        now = time.time()
        for job in await run(db.due_jobs, now):
            await run(db.bump_job, job.id, now + job.interval_s)
            if job.guild_id not in allowed:
                continue
            if job.created_by not in OWNER_IDS and await run(db.member_status, job.created_by) != "approved":
                continue  # creator lost access, so skip
            channel = bot.get_channel(job.channel_id)
            if channel is None:
                continue
            t0 = time.monotonic()
            try:
                data = await providers.generate_image(job.provider or DEFAULT_PROVIDER, job.prompt)
                await channel.send(f"**Auto:** {job.prompt[:500]}", file=to_file(data))
                status, err = "ok", ""
            except Exception as e:
                status, err = "error", clean(repr(e))
            await run(
                db.log_usage, job.created_by, "automation", job.guild_id, channel.guild.name,
                "auto", job.prompt, status, ms_since(t0), err,
            )
    except Exception:
        log.exception("scheduler failed")


@scheduler.before_loop
async def _wait_scheduler():
    await bot.wait_until_ready()
