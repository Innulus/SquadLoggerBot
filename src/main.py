import os
import discord
from discord import app_commands
from discord.ext import commands
import httpx
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL")
REQUIRED_ROLE_NAME = os.getenv("REQUIRED_ROLE_NAME")
API_SECRET_CODE = os.getenv("API_SECRET_CODE") 
REVIEWER_ROLE_NAME = os.getenv("REVIEWER_ROLE_NAME", REQUIRED_ROLE_NAME)

# IMPORTANT: You must enable Intents.members for the dropdown to find users
intents = discord.Intents.default()
intents.members = True 
bot = commands.Bot(command_prefix="!", intents=intents)


def has_reviewer_role():
    """Custom check for the reviewer role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        user_roles = [role.name for role in interaction.user.roles]
        if REVIEWER_ROLE_NAME in user_roles:
            return True
        await interaction.response.send_message(
            f"You lack permission to review logs (Requires: `{REVIEWER_ROLE_NAME}`).",
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)

def has_required_role():
    """Custom check to ensure the user has the specified role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message(
                "Commands can only be used inside a server.", ephemeral=True
            )
            return False

        user_roles = [role.name for role in interaction.user.roles]
        if REQUIRED_ROLE_NAME in user_roles:
            return True

        await interaction.response.send_message(
            f"You do not have permission to run this command (Requires role: `{REQUIRED_ROLE_NAME}`).",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user} | Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# --- Modal & UI Components for Adding Logs ---

class LogModal(discord.ui.Modal, title='Create Punishment Log'):
    username = discord.ui.TextInput(label='Username', placeholder='Target Player Name', required=True)
    steam_id = discord.ui.TextInput(label='SteamID', placeholder='STEAM_0:1:12345678', required=True)
    duration = discord.ui.TextInput(label='Duration (Minutes)', placeholder='Use 0 for permanent', required=True)
    server_name = discord.ui.TextInput(label='Server Name', placeholder='e.g. US-East 1', required=True)
    reason_and_review = discord.ui.TextInput(
        label='Reason & Review Notes', 
        style=discord.TextStyle.paragraph, 
        placeholder='Why were they punished? Add any internal admin review notes here.',
        required=False
    )

    def __init__(self, issuing_user: str):
        super().__init__()
        self.issuing_user = issuing_user

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            duration_int = int(self.duration.value)
        except ValueError:
            await interaction.followup.send("Duration must be a valid number.", ephemeral=True)
            return

        form_data = {
            "steam_id": self.steam_id.value,
            "username": self.username.value,
            "punishment_duration": duration_int,
            "server_name": self.server_name.value,
            "issued_by": self.issuing_user,
        }
        
        if self.reason_and_review.value:
            form_data["reason_given"] = self.reason_and_review.value

        headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

        try:
            async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
                response = await client.post("/logs/", data=form_data)

                if response.status_code in (200, 201):
                    embed = discord.Embed(title="Log Successfully Created", color=discord.Color.green())
                    embed.add_field(name="Target", value=f"{self.username.value}\n(`{self.steam_id.value}`)", inline=True)
                    embed.add_field(name="Duration", value=f"{duration_int} mins", inline=True)
                    embed.add_field(name="Server", value=self.server_name.value, inline=True)
                    embed.add_field(name="Issued By", value=self.issuing_user, inline=True)
                    embed.add_field(name="Reason/Notes", value=self.reason_and_review.value or "None", inline=False)
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                elif response.status_code == 401:
                    await interaction.followup.send("Failed: API authorization code was rejected.", ephemeral=True)
                else:
                    await interaction.followup.send(f"Failed to create log. FastAPI returned `{response.status_code}`.", ephemeral=True)
        except httpx.RequestError as exc:
            await interaction.followup.send(f"Could not reach the FastAPI server: {exc}", ephemeral=True)


class IssuerSelect(discord.ui.Select):
    def __init__(self, eligible_members):
        options = [
            discord.SelectOption(label=member.display_name, description=str(member), value=str(member))
            for member in eligible_members[:25]
        ]
        super().__init__(placeholder="Select the issuing admin...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_issuer = self.values[0]
        await interaction.response.send_modal(LogModal(issuing_user=selected_issuer))


class IssuerSelectView(discord.ui.View):
    def __init__(self, eligible_members):
        super().__init__()
        self.add_item(IssuerSelect(eligible_members))


# --- Modal & UI Components for Reviewing Logs ---

class ReviewModal(discord.ui.Modal, title='Review Punishment Log'):
    log_id_input = discord.ui.TextInput(
        label='Log ID', 
        placeholder='Enter the numeric Log ID (e.g. 5)', 
        required=True
    )

    def __init__(self, reviewing_user: str):
        super().__init__()
        self.reviewing_user = reviewing_user

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            log_id_int = int(self.log_id_input.value)
        except ValueError:
            await interaction.followup.send("Log ID must be a valid number.", ephemeral=True)
            return

        form_data = {
            "reviewer": self.reviewing_user
        }
        headers = {"Authorization": f"Bearer {API_SECRET_CODE}"}

        try:
            async with httpx.AsyncClient(base_url=FASTAPI_BASE_URL, headers=headers, timeout=10.0) as client:
                response = await client.patch(f"/logs/{log_id_int}/review", data=form_data)

                if response.status_code in (200, 204):
                    embed = discord.Embed(title="Log Marked as Reviewed", color=discord.Color.blue())
                    embed.add_field(name="Log ID", value=f"#{log_id_int}", inline=True)
                    embed.add_field(name="Reviewed By", value=self.reviewing_user, inline=True)
                    await interaction.followup.send(embed=embed, ephemeral=True)
                elif response.status_code == 404:
                    await interaction.followup.send(f"Log #{log_id_int} not found.", ephemeral=True)
                elif response.status_code == 401:
                    await interaction.followup.send("Failed: API authorization code was rejected.", ephemeral=True)
                else:
                    await interaction.followup.send(f"Failed to update log. API returned `{response.status_code}`.", ephemeral=True)
        except httpx.RequestError as exc:
            await interaction.followup.send(f"Could not reach FastAPI: {exc}", ephemeral=True)


class ReviewerSelect(discord.ui.Select):
    def __init__(self, eligible_members):
        options = [
            discord.SelectOption(label=member.display_name, description=str(member), value=str(member))
            for member in eligible_members[:25]
        ]
        super().__init__(placeholder="Select who reviewed this log...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_reviewer = self.values[0]
        await interaction.response.send_modal(ReviewModal(reviewing_user=selected_reviewer))


class ReviewerSelectView(discord.ui.View):
    def __init__(self, eligible_members):
        super().__init__()
        self.add_item(ReviewerSelect(eligible_members))


# --- Slash Commands ---

@bot.tree.command(name="add_log", description="Log a user punishment to the dashboard.")
@has_required_role()
async def add_log_command(interaction: discord.Interaction):
    eligible_members = [
        member for member in interaction.guild.members 
        if REQUIRED_ROLE_NAME in [role.name for role in member.roles]
    ]

    if not eligible_members:
        await interaction.response.send_message(
            f"Could not find any members with the `{REQUIRED_ROLE_NAME}` role to act as issuers.", 
            ephemeral=True
        )
        return

    view = IssuerSelectView(eligible_members)
    await interaction.response.send_message("Who is issuing this punishment?", view=view, ephemeral=True)


@bot.tree.command(name="review_log", description="Mark an existing punishment log as reviewed.")
@has_reviewer_role()
async def review_log_command(interaction: discord.Interaction):
    eligible_members = [
        member for member in interaction.guild.members 
        if REVIEWER_ROLE_NAME in [role.name for role in member.roles]
    ]

    if not eligible_members:
        await interaction.response.send_message(
            f"No members found with the `{REVIEWER_ROLE_NAME}` role.", 
            ephemeral=True
        )
        return

    view = ReviewerSelectView(eligible_members)
    await interaction.response.send_message("Who is reviewing the log?", view=view, ephemeral=True)


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
                await interaction.followup.send(
                    f"Log #{log_id} has been deleted.", ephemeral=True
                )
            elif response.status_code == 401:
                await interaction.followup.send(
                    "Failed: The bot's API authorization code was rejected by the server.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"Failed to delete log #{log_id}. Received status `{response.status_code}`.",
                    ephemeral=True,
                )
    except httpx.RequestError as exc:
        await interaction.followup.send(
            f"Could not reach the FastAPI server: {exc}", ephemeral=True
        )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set.")
    if not API_SECRET_CODE:
        raise ValueError("API_SECRET_CODE environment variable is not set.")
    bot.run(DISCORD_TOKEN)