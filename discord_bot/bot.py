"""
Tony Discord Bot
----------------
Voice flow  : "I have a question" → Whisper → Groq auto-answer → posted in tonys-chat-room
Text flow   : any message in tonys-chat-room → Groq reply (checks past Q&A first)
General     : Tony only replies when @mentioned
1-on-1 voice: auto-joins both "1 on 1 with Tony" channels when a student enters
"""

import asyncio
import json as _json
import os
import tempfile
import time as _time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# py-cord uses get_event_loop() which requires an explicit loop in Python 3.14+
asyncio.set_event_loop(asyncio.new_event_loop())

import discord
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings
from discord.ext import commands, tasks

from config import Config
from figurate_client import fetch_tony_system_prompt
from groq_client import TonyGroq
from qa_store import QAStore
from transcriber import Transcriber

# ── Load opus ─────────────────────────────────────────────────────────────────
if not discord.opus.is_loaded():
    for _opus_path in [
        "libopus.dylib",
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.dylib",
    ]:
        try:
            discord.opus.load_opus(_opus_path)
            print(f"[Tony] Opus loaded from {_opus_path}")
            break
        except Exception:
            continue
    if not discord.opus.is_loaded():
        print("[Tony] WARNING: opus not found — voice recording disabled. Run: brew install opus")

# ── Bootstrap ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
qa = QAStore(Config.DB_PATH)
transcriber = Transcriber()

# Load Tony's personality from Figurate, then pass it to Groq
print("[Tony] Fetching personality from Figurate…")
_system_prompt = fetch_tony_system_prompt(
    api_key=Config.FIGURATE_API_KEY,
    character_id=Config.FIGURATE_CHARACTER_ID,
)
tony = TonyGroq(api_key=Config.GROQ_API_KEY, model=Config.GROQ_MODEL, system_prompt=_system_prompt)

_executor = ThreadPoolExecutor(max_workers=1)
_MIN_PCM_BYTES = 48000 * 2 * 2 // 2

# ── Creature movement ─────────────────────────────────────────────────────────
_CREATURE_CMD = "/tmp/tony_cmd.json"

# Student-accessible moves: display name → (internal cmd, duration seconds)
_STUDENT_MOVES = {
    "wave":    ("wave_once",    1.8),
    "wiggle":  ("wiggle",       1.2),
    "stretch": ("stretch",      2.2),
    "flinch":  ("flinch",       1.2),
    "excited": ("excited_once", 2.0),
}

_system_busy:    asyncio.Event | None = None  # set while Tony is processing a question
_creature_queue: asyncio.Queue | None = None  # student fun-move queue (max 10)

def creature_cmd(state: str):
    """Write a state change to /tmp/tony_cmd.json for tony_creature.py to pick up."""
    try:
        with open(_CREATURE_CMD, "w") as f:
            _json.dump({"cmd": state, "ts": _time.time()}, f)
    except Exception:
        pass

def _set_system_busy(busy: bool):
    if _system_busy is not None:
        _system_busy.set() if busy else _system_busy.clear()

async def _creature_worker():
    """Process student move commands one at a time, waiting if Tony is busy."""
    while True:
        cmd, duration = await _creature_queue.get()
        while _system_busy.is_set():
            await asyncio.sleep(0.5)
        creature_cmd(cmd)
        await asyncio.sleep(duration)
        if not _system_busy.is_set():
            creature_cmd("idle")
        _creature_queue.task_done()

# Per-voice-channel state: channel_id → {vc, sink}
active_vcs: dict[int, dict] = {}
_joining = False

# Per-user voice session state
def _blank_session():
    return {"state": "idle", "buffer": bytearray(), "transcript": "", "display_name": "Student", "channel_id": None}

sessions: dict[int, dict] = defaultdict(_blank_session)


# ── Groq helper ────────────────────────────────────────────────────────────────
async def _tony_reply(question: str) -> str:
    """Check past Q&A for a similar answer, then ask Groq."""
    loop = asyncio.get_event_loop()
    similar = await loop.run_in_executor(None, qa.find_similar, question)
    prior = similar.answer_text if similar else ""
    answer = await loop.run_in_executor(None, tony.ask, question, prior)
    return answer, bool(similar)


# ── TTS helper ────────────────────────────────────────────────────────────────
_eleven = ElevenLabs(api_key=Config.ELEVENLABS_API_KEY)

async def speak_in_vc(channel_id: int, text: str):
    state = active_vcs.get(channel_id)
    if not state or not state["vc"].is_connected():
        return
    vc = state["vc"]

    try:
        vc.stop_recording()
    except Exception:
        pass

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name

    try:
        loop = asyncio.get_event_loop()
        audio = await loop.run_in_executor(None, lambda: b"".join(
            _eleven.text_to_speech.convert(
                text=text,
                voice_id=Config.ELEVENLABS_VOICE_ID,
                model_id="eleven_monolingual_v1",
                voice_settings=VoiceSettings(stability=0.5, similarity_boost=0.75),
            )
        ))
        with open(tmp_path, "wb") as f:
            f.write(audio)

        if not vc.is_playing():
            source = discord.FFmpegPCMAudio(tmp_path)
            vc.play(source)
            while vc.is_playing():
                await asyncio.sleep(0.5)
    finally:
        os.unlink(tmp_path)

    if hasattr(vc, "start_recording"):
        sink = TonySink()
        state["sink"] = sink
        try:
            vc.start_recording(sink, _on_recording_done)
        except Exception:
            pass


# ── Custom PCM sink ────────────────────────────────────────────────────────────
_SinkBase = getattr(discord, "sinks", None)
_SinkBase = _SinkBase.Sink if _SinkBase else object

class TonySink(_SinkBase):
    def write(self, data: bytes, user) -> None:
        uid = user.id if hasattr(user, "id") else int(user)
        sessions[uid]["buffer"] += data

    def cleanup(self) -> None:
        pass


# ── Transcription loop ────────────────────────────────────────────────────────
@tasks.loop(seconds=Config.CHUNK_SECONDS)
async def transcription_loop():
    if not active_vcs:
        return
    loop = asyncio.get_event_loop()
    for uid, sess in list(sessions.items()):
        if len(sess["buffer"]) < _MIN_PCM_BYTES:
            continue
        pcm = bytes(sess["buffer"])
        sess["buffer"] = bytearray()
        try:
            text = await loop.run_in_executor(_executor, transcriber.transcribe, pcm)
        except Exception as exc:
            print(f"[Whisper] Error (user {uid}): {exc}")
            continue
        if not text:
            continue
        print(f"[{sess['display_name']}] {text}")
        await _process_transcript(uid, sess, text.lower())


async def _process_transcript(uid: int, sess: dict, text: str):
    user = bot.get_user(uid)

    if sess["state"] == "idle":
        if Config.START_PHRASE in text:
            sess["state"] = "recording"
            sess["transcript"] = ""
        else:
            # Not a question — DM the user a markdown transcript of what they said
            if user:
                try:
                    await user.send(f"📝 **Transcript:**\n> {text}")
                except discord.Forbidden:
                    pass

    elif sess["state"] == "recording":
        clean = text.replace(Config.START_PHRASE, "").strip()
        if Config.STOP_PHRASE in clean:
            question_part = clean.split(Config.STOP_PHRASE)[0].strip(" .,!?")
            sess["transcript"] += " " + question_part
            question_text = sess["transcript"].strip()
            sess["state"] = "idle"
            sess["transcript"] = ""

            if question_text and user:
                qid = qa.add_question(sess["display_name"], uid, question_text)
                _set_system_busy(True)
                creature_cmd("loading")
                answer, from_cache = await _tony_reply(question_text)
                qa.answer_question(qid, answer, "Tony (AI)")
                cache_note = " *(answered before)*" if from_cache else ""
                try:
                    await user.send(
                        f"🎙️ **You asked:** {question_text}\n\n"
                        f"💡 **Tony's answer**{cache_note}:\n{answer}"
                    )
                except discord.Forbidden:
                    pass
                creature_cmd("responding")
                if sess.get("channel_id"):
                    await speak_in_vc(sess["channel_id"], answer)
                await asyncio.sleep(4)
                creature_cmd("idle")
                _set_system_busy(False)
        else:
            sess["transcript"] += " " + clean.strip(" .,!?")


# ── Voice helpers ──────────────────────────────────────────────────────────────
async def join_voice(channel: discord.VoiceChannel):
    global _joining
    cid = channel.id
    if cid in active_vcs and active_vcs[cid]["vc"].is_connected():
        return
    _joining = True
    try:
        for vc_existing in list(bot.voice_clients):
            if vc_existing.guild == channel.guild:
                try:
                    await vc_existing.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(1)
        vc = await channel.connect()
    finally:
        _joining = False
    await asyncio.sleep(3)
    sink = TonySink()
    active_vcs[cid] = {"vc": vc, "sink": sink}
    print(f"[Tony] vc type: {type(vc)}, has start_recording: {hasattr(vc, 'start_recording')}, opus loaded: {discord.opus.is_loaded()}")
    print(f"[Tony] recording-related attrs: {[a for a in dir(vc) if 'record' in a.lower()]}")
    if vc.is_connected() and hasattr(vc, "start_recording"):
        try:
            vc.start_recording(sink, _on_recording_done)
            print("[Tony] Recording started.")
        except Exception as exc:
            print(f"[Tony] Recording failed: {exc}")
    if not transcription_loop.is_running():
        transcription_loop.start()
    print(f"[Tony] Joined '{channel.name}'")


async def leave_voice(channel_id: int):
    state = active_vcs.pop(channel_id, None)
    if state:
        vc = state["vc"]
        try:
            vc.stop_recording()
        except Exception:
            pass
        if vc.is_connected():
            await vc.disconnect()
    if not active_vcs and transcription_loop.is_running():
        transcription_loop.stop()
    sessions.clear()
    print(f"[Tony] Left voice channel {channel_id}")


async def _on_recording_done(sink, *args):
    pass


# ── Auto-join / auto-leave ─────────────────────────────────────────────────────
@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return

    if after.channel and after.channel.id in Config.VOICE_CHANNEL_IDS:
        sessions[member.id]["display_name"] = member.display_name
        sessions[member.id]["channel_id"] = after.channel.id
        try:
            await join_voice(after.channel)
        except Exception as exc:
            print(f"[Tony] Failed to join voice: {exc}")
        if _system_busy is not None and not _system_busy.is_set():
            creature_cmd("greeting")

    if before.channel and before.channel.id in Config.VOICE_CHANNEL_IDS:
        print(f"[Debug] {member.display_name} left {before.channel.name}, _joining={_joining}")
        await asyncio.sleep(2)
        if _joining:
            print(f"[Debug] Skipping leave — still joining")
            return
        non_bots = [m for m in before.channel.members if not m.bot]
        print(f"[Debug] Non-bots in channel: {[m.display_name for m in non_bots]}")
        if not non_bots:
            await leave_voice(before.channel.id)


# pending_questions: maps Tony's message ID → {student_message, question_text, qid}
pending_questions: dict[int, dict] = {}


# ── Text message handler ───────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return

    # DM: answer directly with Groq
    if isinstance(message.channel, discord.DMChannel):
        question = message.content.strip()
        if question and not question.startswith("!"):
            _set_system_busy(True)
            creature_cmd("loading")
            async with message.channel.typing():
                answer, _ = await _tony_reply(question)
            await message.reply(f"💡 **Tony**:\n{answer}")
            creature_cmd("responding")
            await asyncio.sleep(4)
            creature_cmd("idle")
            _set_system_busy(False)
        await bot.process_commands(message)
        return

    # Professor replied to one of Tony's pending question messages
    if (message.author.id in Config.PROFESSOR_IDS
            and message.reference
            and message.reference.message_id in pending_questions):
        pending = pending_questions.pop(message.reference.message_id)
        answer = message.content.strip()
        qa.answer_question(pending["qid"], answer, message.author.display_name)
        student_msg = pending["student_message"]
        await student_msg.reply(f"💡 **Professor {message.author.display_name}** answered:\n{answer}")
        await bot.process_commands(message)
        return

    # General channel: Tony answers everything directly with Groq
    if message.channel.id == Config.GENERAL_CHANNEL_ID:
        question = message.content.strip()
        if not question or question.startswith("!"):
            await bot.process_commands(message)
            return
        _set_system_busy(True)
        creature_cmd("loading")
        async with message.channel.typing():
            answer, _ = await _tony_reply(question)
        await message.reply(f"💡 **Tony**:\n{answer}")
        creature_cmd("responding")
        await asyncio.sleep(4)
        creature_cmd("idle")
        _set_system_busy(False)
        await bot.process_commands(message)
        return

    # tonys-chat-room: professor Q&A flow
    if message.channel.id == Config.QUESTIONS_CHANNEL_ID:
        question = message.content.strip()
        if not question or question.startswith("!"):
            await bot.process_commands(message)
            return

        # Check cache first
        loop = asyncio.get_event_loop()
        similar = await loop.run_in_executor(None, qa.find_similar, question)
        if similar:
            creature_cmd("responding")
            await message.reply(f"💡 **Tony** *(answered before)*:\n{similar.answer_text}")
            await asyncio.sleep(4)
            creature_cmd("idle")
            await bot.process_commands(message)
            return

        # No cache — ping the professor
        qid = qa.add_question(message.author.display_name, message.author.id, question)
        await message.reply("⏳ Great question! Let me check with the professor — please wait a moment.")
        prof_mention = " ".join(f"<@{pid}>" for pid in Config.PROFESSOR_IDS)
        tony_msg = await message.channel.send(
            f"{prof_mention} **{message.author.display_name}** asks:\n> {question}\n"
            f"Please reply to this message with your answer."
        )
        pending_questions[tony_msg.id] = {
            "student_message": message,
            "question_text": question,
            "qid": qid,
        }

    await bot.process_commands(message)


# ── Welcome DM on member join ──────────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    try:
        await member.send(
            f"👋 Hey **{member.display_name}**, welcome! I'm **Tony**, your AI assistant.\n\n"
            f"You can ask me anything right here in this DM, or head to "
            f"**#tonys-chat-room** on the server to ask questions alongside your classmates.\n\n"
            f"For voice Q&A, join one of the **1 on 1 with Tony** voice channels and say "
            f"*\"I have a question\"* to start."
        )
    except discord.Forbidden:
        pass  # user has DMs disabled


@bot.event
async def on_ready():
    global _system_busy, _creature_queue
    if _system_busy is None:
        _system_busy = asyncio.Event()
        _creature_queue = asyncio.Queue(maxsize=10)
        asyncio.create_task(_creature_worker())
        print("[Tony] Creature movement system ready")

        # Announce in the Q&A channel that Tony is online
        ch = bot.get_channel(Config.QUESTIONS_CHANNEL_ID)
        if ch:
            await ch.send(
                "👋 **Tony is online and ready!**\n"
                "Ask me anything in this channel, join a voice channel, or try `!pose wave` to say hi."
            )
        creature_cmd("pulse")  # startup wave animation

    print(f"[Tony] Online as {bot.user} (ID: {bot.user.id})")
    print(f"[Tony] Q&A channel : {Config.QUESTIONS_CHANNEL_ID}")
    print(f"[Tony] General     : {Config.GENERAL_CHANNEL_ID}")
    print(f"[Tony] Voice slots : {Config.VOICE_CHANNEL_IDS}")
    print(f"[Tony] Groq model  : {Config.GROQ_MODEL}")


# ── Admin commands ─────────────────────────────────────────────────────────────
@bot.command(name="join")
@commands.has_permissions(manage_channels=True)
async def cmd_join(ctx: commands.Context):
    if not (ctx.author.voice and ctx.author.voice.channel):
        return await ctx.send("❌ You must be in a voice channel.")
    await join_voice(ctx.author.voice.channel)
    await ctx.send(f"✅ Joined **{ctx.author.voice.channel.name}**")


@bot.command(name="leave")
@commands.has_permissions(manage_channels=True)
async def cmd_leave(ctx: commands.Context):
    if not (ctx.author.voice and ctx.author.voice.channel):
        return await ctx.send("❌ You must be in a voice channel.")
    await leave_voice(ctx.author.voice.channel.id)
    await ctx.send("✅ Left voice channel.")


@bot.command(name="questions", aliases=["q", "pending"])
async def cmd_questions(ctx: commands.Context):
    pending = qa.get_pending()
    if not pending:
        return await ctx.send("✅ No pending questions right now.")
    lines = [f"**{len(pending)} pending question(s):**"]
    for q in pending:
        lines.append(f"**#{q.id}** `{q.timestamp}` — **{q.student_name}**: {q.question_text}")
    await ctx.send("\n".join(lines))


@bot.command(name="answer", aliases=["a"])
async def cmd_answer(ctx: commands.Context, question_id: int, *, answer: str):
    q = qa.get_question(question_id)
    if not q:
        return await ctx.send(f"❌ Question #{question_id} not found.")
    qa.answer_question(question_id, answer, ctx.author.display_name)
    ch = bot.get_channel(Config.QUESTIONS_CHANNEL_ID)
    if ch:
        await ch.send(
            f"✅ **Answer to #{question_id}** (asked by **{q.student_name}**):\n"
            f"> **Q:** {q.question_text}\n"
            f"> **A:** {answer}\n"
            f"*— {ctx.author.display_name}*"
        )
    await ctx.send(f"✅ Answered question #{question_id}.")


@bot.command(name="history")
async def cmd_history(ctx: commands.Context):
    all_q = qa.get_all(limit=10)
    if not all_q:
        return await ctx.send("No questions recorded yet.")
    lines = ["**Recent Questions:**"]
    for q in all_q:
        icon = "✅" if q.answer_text else "⏳"
        lines.append(f"{icon} **#{q.id}** **{q.student_name}**: {q.question_text}")
        if q.answer_text:
            lines.append(f"   ↳ *{q.teacher_name}*: {q.answer_text}")
    await ctx.send("\n".join(lines))


@bot.command(name="thelp")
async def cmd_thelp(ctx: commands.Context):
    await ctx.send(
        "**Tony Bot**\n\n"
        "**Text chat:**\n"
        "• Type anything in **#tonys-chat-room** → Tony answers instantly\n"
        "• Mention **@Tony** in **#general** → Tony replies\n\n"
        "**Voice chat:**\n"
        "1. Join a **1 on 1 with Tony** voice channel\n"
        '2. Say **"I have a question"** → Tony starts listening\n'
        "3. Ask your question\n"
        '4. Say **"Thank you"** → answer is posted in **#tonys-chat-room**\n\n'
        "**Fun commands:**\n"
        "`!pose <move>` — make Tony move! (`wave` | `wiggle` | `stretch` | `flinch` | `excited`)\n"
        "`!queue` — see how many moves are queued\n\n"
        "**Admin commands:**\n"
        "`!join` / `!leave` — manual voice control\n"
        "`!questions` — list unanswered questions\n"
        "`!answer <id> <text>` — override Tony's answer\n"
        "`!history` — last 10 Q&A entries\n"
    )


@bot.command(name="pose")
async def cmd_pose(ctx: commands.Context, move: str = None):
    if _creature_queue is None:
        return await ctx.send("❌ Tony's movement system isn't ready yet.")
    if move not in _STUDENT_MOVES:
        names = " | ".join(_STUDENT_MOVES)
        return await ctx.send(f"🦾 Valid moves: `{names}`")
    if _creature_queue.full():
        return await ctx.send("⏳ Tony's move queue is full, try again soon!")
    internal_cmd, duration = _STUDENT_MOVES[move]
    pos = _creature_queue.qsize() + 1
    await _creature_queue.put((internal_cmd, duration))
    if pos == 1:
        await ctx.send(f"🦾 Tony will `{move}`!")
    else:
        await ctx.send(f"⏳ `{move}` queued (#{pos} in line)")


@bot.command(name="queue")
async def cmd_queue(ctx: commands.Context):
    if _creature_queue is None:
        return await ctx.send("❌ Tony's movement system isn't ready yet.")
    size = _creature_queue.qsize()
    if size == 0:
        await ctx.send("✅ No moves queued — Tony is free!")
    else:
        await ctx.send(f"⏳ **{size}** move(s) queued.")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not Config.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    if not Config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    bot.run(Config.DISCORD_TOKEN)

