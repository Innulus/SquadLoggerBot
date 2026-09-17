import os

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL")
API_SECRET_CODE = os.getenv("API_SECRET_CODE")

REQUIRED_ROLE_ID = int(os.getenv("REQUIRED_ROLE_ID", "0"))
_reviewer_id = os.getenv("REVIEWER_ROLE_ID")
REVIEWER_ROLE_ID = int(_reviewer_id) if _reviewer_id else REQUIRED_ROLE_ID

ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
ALERT_ROLE_ID = int(os.getenv("ALERT_ROLE_ID", "0"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "1"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

is_api_down = False
initial_connect_reported = False


# --- Helper: Parse FastAPI Error Responses ---

def extract_fastapi_error(response: httpx.Response) -> str:
    """Extracts a clear, human-readable error string from FastAPI responses."""
    try:
        data = response.json()
        detail = data.get("detail")

        # FastAPI 422 Unprocessable Entity format
        if isinstance(detail, list):
            errors = []
            for err in detail:
                loc = " -> ".join(str(item) for item in err.get("loc", []) if item != "body")
                msg = err.get("msg", "Invalid value")
                errors.append(f"• **{loc}**: {msg}" if loc else f"• {msg}")
            return "\n".join(errors)

        # Standard FastAPI HTTPException format {"detail": "Error string"}
        if isinstance(detail, str):
            return detail

        # Fallback for unexpected JSON structures
        return str(data)
    except Exception:
        return response.text.strip() or f"HTTP {response.status_code}"


async def handle_http_error(interaction: discord.Interaction, response: httpx.Response, action_desc: str):
    """Formats and responds to Discord interactions with appropriate HTTP status details."""
    error_detail = extract_fastapi_error(response)
    code = response.status_code

    if code == 400:
        msg = f"❌ **Bad Request while trying to {action_desc}:**\n{error_detail}"
    elif code in (401, 403):
        msg = f"🔒 **Permission Denied:** The bot's API credentials were rejected (`{code}`)."
    elif code == 404:
        msg = f"🔍 **Not Found:** {error_detail}"
    elif code == 422:
        msg = f"⚠️ **Data Validation Error:**\n{error_detail}"
    elif code >= 500:
        msg = f"💥 **Backend Server Error ({code}):** The API encountered an internal failure.\n```{error_detail[:500]}```"
    else:
        msg = f"⚠️ **Request Failed ({code}):** {error_detail}"

    await interaction.followup.send(msg, ephemeral=True)


# --- Permission Checks ---

def has_reviewer_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        user_role_ids = [role.id for role in interaction.user.roles]
        if REVIEWER_ROLE_ID in user_role_ids:
            return True
        await interaction.response.send_message(
            f"You lack permission to review logs (Requires role ID: `{REVIEWER_ROLE_ID}`).",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


def has_required_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message(
                "Commands can only be used inside a server.", ephemeral=True
            )
            return False

        user_role_ids = [role.id for role in interaction.user.roles]
        if REQUIRED_ROLE_ID in user_role_ids:
            return True

        await interaction.response.send_message(
            f"You do not have permission to run this command (Requires role ID: `{REQUIRED_ROLE_ID}`).",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


# --- Modal & UI Components for Adding Logs ---

class LogModal(discord.ui.Modal, title='Create Punishment Log'):
    username = discord.ui.TextInput(label='Username', placeholder='Target Player Name', required=True)
    steam_id = discord.ui.TextInput(label='SteamID', placeholder='STEAM_0:1:12345678', required=True)
    duration = discord.ui.TextInput(
        label='Duration',
        placeholder='e.g. 1d, 2h, permanent, 30m',
        required=True
    )
    server_name = discord.ui.TextInput(label='Server Name', placeholder='e.g. US-East 1', required=True)
    reason_and_review = discord.ui.TextInput(
        label='Reason',
        style=discord.TextStyle.paragraph,
        placeholder='Why were they punished?',
        required=False
    )

    def __init__(self, issuing_user: str):
        super().__init__()
        self.issuing_user = issuing_user

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        duration_val = self.duration.value.strip()

        payload = {
            "steam_id": self.steam_id.value.strip(),
            "username": self.username.value.strip(),
            "punishment_duration": duration_val,
            "server_name": self.server_name.value.strip(),
            "issued_by": self.issuing_user,
            "reason_given": self.reason_and_review.value.strip() or None
        }

        headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

        try:
            async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
                response = await client.post("/logs/", json=payload)

                if response.status_code in (200, 201):
                    data = response.json()
                    log_id = data.get("id", "N/A")
                    embed = discord.Embed(title=f"Log Created (ID: #{log_id})", color=discord.Color.green())
                    embed.add_field(name="Target", value=f"{self.username.value}\n(`{self.steam_id.value}`)", inline=True)
                    embed.add_field(name="Duration", value=duration_val, inline=True)
                    embed.add_field(name="Server", value=self.server_name.value, inline=True)
                    embed.add_field(name="Issued By", value=self.issuing_user, inline=True)
                    embed.add_field(name="Reason", value=self.reason_and_review.value or "None", inline=False)

                    # Send public embed to channel, clear interaction ephemerally
                    await interaction.channel.send(embed=embed)
                    await interaction.followup.send("✅ Log published successfully.", ephemeral=True)
                else:
                    await handle_http_error(interaction, response, "create the punishment log")
        except httpx.RequestError as exc:
            await interaction.followup.send(f"🚨 Could not connect to API server: `{exc}`", ephemeral=True)


class IssuerSelect(discord.ui.Select):
    def __init__(self, eligible_members):
        options = [
            discord.SelectOption(label=member.display_name, description=str(member), value=str(member))
            for member in eligible_members[:25]
        ]
        super().__init__(placeholder="Select the issuing admin...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LogModal(issuing_user=self.values[0]))


class IssuerSelectView(discord.ui.View):
    def __init__(self, eligible_members):
        super().__init__()
        self.add_item(IssuerSelect(eligible_members))


# --- Modal & UI Components for Updating Logs ---

class UpdateLogModal(discord.ui.Modal):
    def __init__(self, log_id: int, current_data: dict):
        super().__init__(title=f"Edit Log #{log_id}")
        self.log_id = log_id

        self.username = discord.ui.TextInput(
            label="Username",
            default=str(current_data.get("username") or ""),
            required=True
        )
        self.steam_id = discord.ui.TextInput(
            label="SteamID",
            default=str(current_data.get("SteamID") or current_data.get("steam_id") or ""),
            required=True
        )
        self.duration = discord.ui.TextInput(
            label="Duration",
            default=str(current_data.get("punishment_duration") or ""),
            placeholder="e.g. 1d, 2h, permanent, 30m",
            required=True
        )
        self.server_name = discord.ui.TextInput(
            label="Server Name",
            default=str(current_data.get("server_name") or ""),
            required=True
        )
        self.reason_given = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            default=str(current_data.get("reason_given") or ""),
            required=False
        )

        self.add_item(self.username)
        self.add_item(self.steam_id)
        self.add_item(self.duration)
        self.add_item(self.server_name)
        self.add_item(self.reason_given)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        duration_val = self.duration.value.strip()

        payload = {
            "steam_id": self.steam_id.value.strip(),
            "username": self.username.value.strip(),
            "punishment_duration": duration_val,
            "server_name": self.server_name.value.strip(),
            "reason_given": self.reason_given.value.strip() or None,
        }

        headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

        try:
            async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
                response = await client.put(f"/logs/{self.log_id}", json=payload)

                if response.status_code == 200:
                    embed = discord.Embed(
                        title=f"✅ Log #{self.log_id} Updated",
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="Target", value=f"{self.username.value}\n(`{self.steam_id.value}`)", inline=True)
                    embed.add_field(name="Duration", value=duration_val, inline=True)
                    embed.add_field(name="Server", value=self.server_name.value, inline=True)
                    embed.add_field(name="Reason", value=self.reason_given.value or "None", inline=False)

                    # Send public embed to channel, clear interaction ephemerally
                    await interaction.channel.send(embed=embed)
                    await interaction.followup.send(f"✅ Log #{self.log_id} update published.", ephemeral=True)
                else:
                    await handle_http_error(interaction, response, f"update log #{self.log_id}")
        except httpx.RequestError as exc:
            await interaction.followup.send(f"🚨 Could not connect to API server: `{exc}`", ephemeral=True)


# --- Modal & UI Components for Reviewing Logs ---

class ReviewModal(discord.ui.Modal, title='Review Punishment Log'):
    log_id_input = discord.ui.TextInput(
        label='Log ID',
        placeholder='Enter numeric Log ID (e.g. 5)',
        required=True
    )

    def __init__(self, reviewing_user: str):
        super().__init__()
        self.reviewing_user = reviewing_user

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            log_id_int = int(self.log_id_input.value.strip())
        except ValueError:
            await interaction.followup.send("⚠️ Log ID must be a valid number.", ephemeral=True)
            return

        payload = {"reviewer": self.reviewing_user}
        headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

        try:
            async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
                response = await client.patch(f"/logs/{log_id_int}/review", json=payload)

                if response.status_code == 200:
                    embed = discord.Embed(title="Log Marked as Reviewed", color=discord.Color.blue())
                    embed.add_field(name="Log ID", value=f"#{log_id_int}", inline=True)
                    embed.add_field(name="Reviewed By", value=self.reviewing_user, inline=True)
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await handle_http_error(interaction, response, f"review log #{log_id_int}")
        except httpx.RequestError as exc:
            await interaction.followup.send(f"🚨 Could not connect to API server: `{exc}`", ephemeral=True)


class ReviewerSelect(discord.ui.Select):
    def __init__(self, eligible_members):
        options = [
            discord.SelectOption(label=member.display_name, description=str(member), value=str(member))
            for member in eligible_members[:25]
        ]
        super().__init__(placeholder="Select who reviewed this log...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReviewModal(reviewing_user=self.values[0]))


class ReviewerSelectView(discord.ui.View):
    def __init__(self, eligible_members):
        super().__init__()
        self.add_item(ReviewerSelect(eligible_members))


# --- Slash Commands ---

@bot.tree.command(name="add_log", description="Log a user punishment to the dashboard.")
@has_required_role()
async def add_log_command(interaction: discord.Interaction):
    eligible_members = [
        m for m in interaction.guild.members
        if REQUIRED_ROLE_ID in [r.id for r in m.roles]
    ]

    if not eligible_members:
        await interaction.response.send_message(
            f"Could not find any members with role ID `{REQUIRED_ROLE_ID}`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Who is issuing this punishment?",
        view=IssuerSelectView(eligible_members),
        ephemeral=True
    )


@bot.tree.command(name="update_log", description="Edit an existing punishment log via an interactive form.")
@has_required_role()
@app_commands.describe(log_id="The numeric ID of the log to edit")
async def update_log_command(interaction: discord.Interaction, log_id: int):
    headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

    try:
        async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=5.0) as client:
            response = await client.get(f"/logs/{log_id}")

            if response.status_code == 200:
                current_data = response.json()
                modal = UpdateLogModal(log_id=log_id, current_data=current_data)
                await interaction.response.send_modal(modal)
            elif response.status_code == 404:
                await interaction.response.send_message(f"🔍 Log `#{log_id}` was not found.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"⚠️ API returned an error ({response.status_code}) while fetching Log `#{log_id}`.",
                    ephemeral=True
                )
    except httpx.RequestError as exc:
        await interaction.response.send_message(f"🚨 Could not connect to API server: `{exc}`", ephemeral=True)


@bot.tree.command(name="review_log", description="Mark an existing punishment log as reviewed.")
@has_reviewer_role()
async def review_log_command(interaction: discord.Interaction):
    eligible_members = [
        m for m in interaction.guild.members
        if REVIEWER_ROLE_ID in [r.id for r in m.roles]
    ]

    if not eligible_members:
        await interaction.response.send_message(
            f"No members found with role ID `{REVIEWER_ROLE_ID}`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Who is reviewing the log?",
        view=ReviewerSelectView(eligible_members),
        ephemeral=True
    )


@bot.tree.command(name="delete_log", description="Delete an existing log by ID.")
@has_required_role()
@app_commands.describe(log_id="The numeric ID of the log to delete")
async def delete_log(interaction: discord.Interaction, log_id: int):
    await interaction.response.defer(ephemeral=True)

    headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

    try:
        async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
            response = await client.delete(f"/logs/{log_id}")

            if response.status_code == 200:
                # Public notice to channel
                await interaction.channel.send(f"🗑️ Log **#{log_id}** was deleted by {interaction.user.mention}.")
                # Ephemeral response to dismiss the interaction
                await interaction.followup.send(f"Log #{log_id} deleted.", ephemeral=True)
            else:
                await handle_http_error(interaction, response, f"delete log #{log_id}")
    except httpx.RequestError as exc:
        await interaction.followup.send(f"🚨 Could not connect to API server: `{exc}`", ephemeral=True)


# --- Heartbeat Task Loop ---

@tasks.loop(minutes=HEARTBEAT_INTERVAL)
async def check_api_heartbeat():
    global is_api_down, initial_connect_reported

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        return

    is_healthy = False
    error_reason = ""

    try:
        async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, timeout=5.0) as client:
            response = await client.get("/logs/heartbeat")

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    is_healthy = True
                else:
                    error_reason = f"Unexpected payload received: `{data}`"
            else:
                detail = extract_fastapi_error(response)
                error_reason = f"HTTP {response.status_code}: {detail}"
    except httpx.RequestError as exc:
        error_reason = f"Connection error: `{exc}`"
    except Exception as exc:
        error_reason = f"Unexpected failure: `{exc}`"

    if not is_healthy:
        if not is_api_down:
            is_api_down = True
            embed = discord.Embed(
                title="⚠️ API Heartbeat Failure",
                description="The backend API is failing its health check.",
                color=discord.Color.red()
            )
            embed.add_field(name="Endpoint", value=f"`{FASTAPI_BASE_URL}/logs/heartbeat`", inline=False)
            embed.add_field(name="Details", value=error_reason, inline=False)

            role_mention = f"<@&{ALERT_ROLE_ID}>" if ALERT_ROLE_ID else "@here"
            await channel.send(
                content=f"{role_mention} **Warning:** API service health check failed!",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True)
            )
    else:
        # First-time successful connection notification
        if not initial_connect_reported:
            initial_connect_reported = True
            embed = discord.Embed(
                title=" Connected to API",
                description="Successfully established communication with the backend API service.",
                color=discord.Color.green()
            )
            embed.add_field(name="Endpoint", value=f"`{FASTAPI_BASE_URL}/logs/heartbeat`", inline=False)
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none()
            )
        # Recovery notification if the API was down previously
        elif is_api_down:
            is_api_down = False
            embed = discord.Embed(
                title="✅ API Restored",
                description="The API passed the health check and is operational again.",
                color=discord.Color.green()
            )
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none()
            )


@check_api_heartbeat.before_loop
async def before_heartbeat_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user} | Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not check_api_heartbeat.is_running():
        check_api_heartbeat.start()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    bot.run(DISCORD_TOKEN)
