import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


class Config:
    # ── Required ───────────────────────────────────────────────────────────────
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    GROQ_API_KEY:  str = os.getenv("GROQ_API_KEY", "")

    # Text channel where Q&A is posted and students can type questions
    QUESTIONS_CHANNEL_ID: int = int(os.getenv("QUESTIONS_CHANNEL_ID", "1470571362287358090"))

    # General channel — Tony only replies if @mentioned here
    GENERAL_CHANNEL_ID: int = int(os.getenv("GENERAL_CHANNEL_ID", "1470571362287358089"))

    # ── Voice channels Tony auto-joins ─────────────────────────────────────────
    # Both "1 on 1 with Tony" slots; add more IDs separated by commas if needed
    _vc_env = os.getenv("VOICE_CHANNEL_IDS", "1470571362572701770,1470571362572701771")
    VOICE_CHANNEL_IDS: set[int] = {int(i) for i in _vc_env.split(",") if i.strip()}

    # Legacy single-channel env var still respected if set
    _legacy = os.getenv("VOICE_CHANNEL_ID", "0")
    if int(_legacy):
        VOICE_CHANNEL_IDS.add(int(_legacy))

    # ── Figurate personality ───────────────────────────────────────────────────
    FIGURATE_API_KEY:      str = os.getenv("FIGURATE_API_KEY", "")
    FIGURATE_CHARACTER_ID: str = os.getenv("FIGURATE_CHARACTER_ID", "cmo36774e00dcqq0cpin053mi")

    # ── ElevenLabs TTS ────────────────────────────────────────────────────────
    ELEVENLABS_API_KEY:  str = os.getenv("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "")

    # ── Optional ───────────────────────────────────────────────────────────────
    GROQ_MODEL:    str = os.getenv("GROQ_MODEL", "llama3-8b-8192")
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    DB_PATH:       str = os.getenv("DB_PATH", "qa_questions.db")
    CHUNK_SECONDS: int = int(os.getenv("CHUNK_SECONDS", "5"))

    START_PHRASE: str = "i have a question"
    STOP_PHRASE:  str = "thank you"

    # Professor Discord user IDs (comma-separated in .env)
    _prof_env = os.getenv("PROFESSOR_IDS", "182644353137770497")
    PROFESSOR_IDS: set[int] = {int(i) for i in _prof_env.split(",") if i.strip()}
