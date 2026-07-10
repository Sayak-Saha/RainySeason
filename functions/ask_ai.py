import asyncio
import discord
from typing import Any, Dict, List, Optional, Sequence
from config import (
    AI_CHAT_CHANNEL_ID,
    AI_MAX_CONTEXT_MESSAGES,
    AI_MAX_RECENT_MESSAGES,
    AI_MODEL_TEMPERATURE,
    GROQ_PROXY_URL,
    GROQ_MODEL_PRIORITY_LIST,
    MONGO_URI,
)
import random
from groq import RateLimitError, APIError
import re
from functions.webhook import send_webhook_message
from functions.web_search import build_search_query, search_web_context, should_search
import time
import requests
from datetime import datetime
from pymongo import MongoClient

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["rainy_season_db"]
lockout_status_collection = mongo_db["lockout_status"]
model_feedback_collection = mongo_db["model_feedback"]
preferred_model_collection = mongo_db["preferred_model"]
server_emojis_collection = mongo_db["server_emojis"]


TPD_LOCKED_MODELS = {}
MODEL_FEEDBACK_CACHE: Optional[Dict[str, Dict[str, int]]] = None


class ProxyRateLimitError(Exception):
    def __init__(self, message: str, headers: Optional[dict] = None):
        super().__init__(message)
        self.headers = headers or {}


class ProxyAPIError(Exception):
    def __init__(self, message: str, status_code: int, headers: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class GlobalCooldownRequired(Exception):
    def __init__(self, reason: str, reset_timestamp: float):
        super().__init__(reason)
        self.reason = reason
        self.reset_timestamp = reset_timestamp

def lock_model_for_tpd(model_name: str, reason: str = "TPD limit reached."):
    """Locks a specific model for 24 hours."""
    # Set expiry for 24 hours + 5-minute buffer
    expiry_timestamp = time.time() + (60 * 60 * 24) + (60 * 5) 
    TPD_LOCKED_MODELS[model_name] = expiry_timestamp
    print(f"\033[93m[MODEL LOCK] {model_name} has been locked until {time.ctime(expiry_timestamp)}. Reason: {reason}\033[00m")

def is_model_tpd_locked(model_name: str) -> bool:
    """Checks if a specific model is currently locked for TPD."""
    if model_name not in TPD_LOCKED_MODELS:
        return False # Not locked
    
    # Check if the lock has expired
    if time.time() > TPD_LOCKED_MODELS[model_name]:
        del TPD_LOCKED_MODELS[model_name] # Remove expired lock
        print(f"\033[92m[MODEL UNLOCK] {model_name} TPD lock has expired.\033[00m")
        return False # Lock expired
        
    # If we're here, the model is still locked
    return True

def get_lock_status():
    """Retrieves the current lock status from MongoDB."""
    status = lockout_status_collection.find_one({"_id": "lockout_status"}, {"_id": 0})
    if not status:
        return {"is_locked": False, "reset_timestamp": 0.0, "lock_reason": None}
    return {
        "is_locked": bool(status.get("is_locked", False)),
        "reset_timestamp": float(status.get("reset_timestamp", 0.0) or 0.0),
        "lock_reason": status.get("lock_reason"),
    }

def set_lock_status(is_locked: bool, reset_timestamp: float = 0.0, reason: str = None):
    """Sets the lock status in MongoDB."""
    status = {"is_locked": is_locked, "reset_timestamp": reset_timestamp, "lock_reason": reason}
    lockout_status_collection.replace_one(
        {"_id": "lockout_status"},
        {"_id": "lockout_status", **status},
        upsert=True,
    )


def load_model_feedback() -> Dict[str, Dict[str, int]]:
    global MODEL_FEEDBACK_CACHE
    if MODEL_FEEDBACK_CACHE is not None:
        return MODEL_FEEDBACK_CACHE

    default_state = {
        model: {
            "score": 0,
            "successes": 0,
            "failures": 0,
            "dissatisfied": 0,
            "rescues": 0,
        }
        for model in GROQ_MODEL_PRIORITY_LIST
    }

    stored_doc = model_feedback_collection.find_one({"_id": "model_feedback"}, {"_id": 0})
    stored = stored_doc.get("models", {}) if stored_doc else {}
    for model_name, stats in stored.items():
        if model_name not in default_state or not isinstance(stats, dict):
            continue
        default_state[model_name].update(
            {
                "score": int(stats.get("score", 0)),
                "successes": int(stats.get("successes", 0)),
                "failures": int(stats.get("failures", 0)),
                "dissatisfied": int(stats.get("dissatisfied", 0)),
                "rescues": int(stats.get("rescues", 0)),
            }
        )

    MODEL_FEEDBACK_CACHE = default_state
    return MODEL_FEEDBACK_CACHE


def save_model_feedback(state: Dict[str, Dict[str, int]]) -> None:
    model_feedback_collection.replace_one(
        {"_id": "model_feedback"},
        {"_id": "model_feedback", "models": state},
        upsert=True,
    )


def update_model_feedback(model_name: Optional[str], outcome: str) -> None:
    if not model_name or model_name not in GROQ_MODEL_PRIORITY_LIST:
        return

    state = load_model_feedback()
    model_state = state.setdefault(
        model_name,
        {"score": 0, "successes": 0, "failures": 0, "dissatisfied": 0, "rescues": 0},
    )

    deltas = {
        "success": ("successes", 1, 1),
        "failure": ("failures", 1, -1),
        "dissatisfied": ("dissatisfied", 1, -2),
        "rescue": ("rescues", 1, 3),
    }
    bucket, count_delta, score_delta = deltas.get(outcome, ("successes", 0, 0))
    model_state[bucket] = int(model_state.get(bucket, 0)) + count_delta
    model_state["score"] = int(model_state.get("score", 0)) + score_delta
    save_model_feedback(state)


def get_preferred_model() -> Optional[str]:
    data = preferred_model_collection.find_one({"_id": "preferred_model"}, {"_id": 0})
    if not data:
        return None
    model_name = data.get("preferred_model")
    if model_name in GROQ_MODEL_PRIORITY_LIST:
        return model_name
    return None


def set_preferred_model(model_name: Optional[str]) -> None:
    payload = {
        "preferred_model": model_name if model_name in GROQ_MODEL_PRIORITY_LIST else None
    }
    preferred_model_collection.replace_one(
        {"_id": "preferred_model"},
        {"_id": "preferred_model", **payload},
        upsert=True,
    )


def load_server_emojis() -> dict:
    doc = server_emojis_collection.find_one({"_id": "server_emojis"}, {"_id": 0})
    if doc and isinstance(doc.get("emojis"), dict):
        return doc["emojis"]
    return {}

def get_model_candidates(
    preferred_model: Optional[str] = None,
    excluded_models: Optional[Sequence[str]] = None,
) -> List[str]:
    excluded = set(excluded_models or [])
    available_models = [
        model for model in GROQ_MODEL_PRIORITY_LIST
        if model not in excluded and not is_model_tpd_locked(model)
    ]
    if not available_models:
        return []

    feedback = load_model_feedback()
    priority_map = {model: index for index, model in enumerate(GROQ_MODEL_PRIORITY_LIST)}
    ranked_models = sorted(
        available_models,
        key=lambda model: (
            -(feedback.get(model, {}).get("score", 0)),
            priority_map.get(model, 999),
        ),
    )

    if preferred_model in ranked_models:
        ranked_models.remove(preferred_model)
        ranked_models.insert(0, preferred_model)
    elif preferred_model and preferred_model not in excluded and not is_model_tpd_locked(preferred_model):
        ranked_models.insert(0, preferred_model)

    deduped_models: List[str] = []
    for model_name in ranked_models:
        if model_name not in deduped_models:
            deduped_models.append(model_name)
    return deduped_models


def _proxy_or_direct_chat_completion(
    *,
    messages: list[dict],
    model: str,
    temperature: Optional[float] = None,
    include_headers: bool = False,
) -> tuple[dict, dict]:
    payload: dict[str, Any] = {
        "messages": messages,
        "model": model,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    if not GROQ_PROXY_URL:
        raise ProxyAPIError("GROQ_PROXY_URL is not configured.", 503, headers={})

    response = requests.post(
        GROQ_PROXY_URL,
        json=payload,
        timeout=45,
    )
    headers = dict(response.headers)
    if response.status_code == 429:
        raise ProxyRateLimitError(response.text, headers=headers)
    if response.status_code >= 400:
        raise ProxyAPIError(response.text, response.status_code, headers=headers)
    return response.json(), headers if include_headers else {}


def get_ai_limit_status() -> discord.Embed:
    """
    Performs a minimal Rainy API call using the FIRST AVAILABLE model, 
    retrieves rate limit headers, and returns a styled discord.Embed
    showing Global TPM status AND individual Model TPD status.
    """
    
    current_unix_time = int(time.time())
    
    embed = discord.Embed(
        title="<a:winds_Joy:1423334991873572876> Rainy AI Service Status",
        color=0x8854ff,
        timestamp=discord.utils.utcnow() 
    )

    # --- Find first available model to check ---
    available_models = get_model_candidates()
    
    # --- 1. Handle ALL MODELS LOCKED case ---
    if not available_models:
        embed.title = "❌ All Models TPD Locked"
        embed.color = 0xAA0000 
        embed.description = "All available AI models have hit their daily token limits. The service is offline until the limits reset."
        
        # Build status string showing all locked models
        model_status_string = ""
        for model_name in GROQ_MODEL_PRIORITY_LIST:
            if is_model_tpd_locked(model_name):
                expiry_ts = TPD_LOCKED_MODELS.get(model_name, time.time() + 86400) # Get with 24h fallback
                model_status_string += f"❌ **{model_name}**: Resets <t:{int(expiry_ts)}:R>\n"
            else:
                # This shouldn't happen if available_models is empty, but good to have
                model_status_string += f"✅ **{model_name}**: Online\n" 
                
        embed.add_field(name="📚 Model Daily Status (TPD)", value=model_status_string.strip(), inline=False)
        embed.set_footer(text="Status: OFFLINE")
        return embed

    # --- If at least one model is online, proceed ---
    model_to_check = available_models[0]

    def get_reset_timestamp(reset_header_value: str) -> str:
        """
        Converts a complex reset duration string (e.g., '2m59.56s', '6s', '380ms') 
        into a future Discord timestamp string.
        """
        current_unix_time = int(time.time()) 
        try:
            if not reset_header_value: return 'Now'
            value = reset_header_value.strip().lower()
            total_seconds = 0.0
            minute_match = re.search(r'(\d+)m(?!s)', value)
            if minute_match: total_seconds += int(minute_match.group(1)) * 60
            ms_match = re.search(r'(\d+)ms', value)
            if ms_match: total_seconds += int(ms_match.group(1)) / 1000.0
            seconds_match = re.search(r'(\d+\.?\d*)s(?!ms)', value)
            if seconds_match: total_seconds += float(seconds_match.group(1))
            if total_seconds == 0 and re.match(r'^\d+(\.\d+)?$', value): total_seconds = float(value)
            if total_seconds < 1.0: return 'Now'
            reset_seconds = int(total_seconds) 
            reset_future_time = current_unix_time + reset_seconds
            return f"<t:{reset_future_time}:R>"
        except Exception as e:
            return f'N/A (Parse Error)'


    # --- 2. API Call ---
    try:
        _, headers = _proxy_or_direct_chat_completion(
            messages=[{"role": "user", "content": "status check"}],
            model=model_to_check,
            include_headers=True,
        )
        
        # --- 2a. Requests Per Day (RPD) Field ---
        rpd_limit = headers.get('x-ratelimit-limit-requests', 'N/A')
        rpd_remaining = headers.get('x-ratelimit-remaining-requests', 'N/A')
        rpd_reset_ts = get_reset_timestamp(headers.get('x-ratelimit-reset-requests', '0'))
        rpd_value = (
            f"**Limit:** `{rpd_limit}`\n"
            f"**Remaining:** `{rpd_remaining}`\n"
            f"**Resets:** {rpd_reset_ts}"
        )
        embed.add_field(name="📅 Daily Request Limits (RPD)", value=rpd_value, inline=True)

        # --- 2b. Tokens Per Minute (TPM) Field ---
        tpm_limit = headers.get('x-ratelimit-limit-tokens', 'N/A')
        tpm_remaining = headers.get('x-ratelimit-remaining-tokens', 'N/A')
        tpm_reset_ts = get_reset_timestamp(headers.get('x-ratelimit-reset-tokens', '0'))
        tpm_value = (
            f"**Limit:** `{tpm_limit}`\n"
            f"**Remaining:** `{tpm_remaining}`\n"
            f"**Resets:** {tpm_reset_ts}"
        )
        embed.add_field(name="⏱️ Minute Token Limits (TPM)", value=tpm_value, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False) # Separator
        
        # --- 2c. Global TPM Lock Status ---
        status = get_lock_status()
        if status["is_locked"]:
            lock_reset_time = int(status.get("reset_timestamp", current_unix_time))
            reset_ts = f"<t:{lock_reset_time}:F> (in <t:{lock_reset_time}:R>)"
            status_title = "🚨 Global TPM Lockdown"
            status_value = (
                f"**Status:** **LOCKED**\n"
                f"**Reason:** {status['lock_reason']}\n"
                f"**Back Online:** {reset_ts}"
            )
            embed.color = 0xAA0000 
        else:
            status_title = "✅ Global Status"
            status_value = f"**Status:** **ONLINE**\n*The service is running normally.*"
            embed.color = 0x8854ff
        embed.add_field(name=status_title, value=status_value, inline=False)

        # --- 2d. NEW: Model Daily (TPD) Status ---
        model_status_string = ""
        all_online = True
        for model_name in GROQ_MODEL_PRIORITY_LIST:
            if is_model_tpd_locked(model_name):
                all_online = False
                expiry_ts = TPD_LOCKED_MODELS[model_name]
                model_status_string += f"❌ **{model_name}**: Resets <t:{int(expiry_ts)}:R>\n"
            else:
                model_status_string += f"✅ **{model_name}**: Online\n"
        
        model_status_title = "📚 Model Daily Status (TPD)" if all_online else "⚠️ Model Daily Status (TPD)"
        embed.add_field(name=model_status_title, value=model_status_string.strip(), inline=False)
        
        embed.set_footer(text=f"Headers via: {model_to_check} | Status data from live API call.")
        return embed

    # --- 3. Error Handling ---
    except (RateLimitError, ProxyRateLimitError) as e:
        headers = getattr(getattr(e, 'response', None), 'headers', None) or getattr(e, 'headers', {}) or {}
        retry_after = headers.get("retry-after", "Unknown")
        
        embed.title = "⚠️ API Rate Limit Error (429)"
        embed.color = 0xFFCC4D 
        embed.description = f"I hit the rate limit while checking the status. Try again in: **{retry_after} seconds**"
        
        rpd_reset_ts = get_reset_timestamp(headers.get('x-ratelimit-reset-requests', '0'))
        tpm_reset_ts = get_reset_timestamp(headers.get('x-ratelimit-reset-tokens', '0'))
        
        embed.add_field(name="Next RPD Reset", value=rpd_reset_ts, inline=True)
        embed.add_field(name="Next TPM Reset", value=tpm_reset_ts, inline=True)
        
        # Also show the TPD status we know about, even on an error
        model_status_string = ""
        for model_name in GROQ_MODEL_PRIORITY_LIST:
            if is_model_tpd_locked(model_name):
                expiry_ts = TPD_LOCKED_MODELS[model_name]
                model_status_string += f"❌ **{model_name}**: Resets <t:{int(expiry_ts)}:R>\n"
            else:
                model_status_string += f"✅ **{model_name}**: Online\n"
        embed.add_field(name="📚 Model Daily Status (TPD)", value=model_status_string.strip(), inline=False)
        
        embed.set_footer(text=f"Model: {model_to_check} | Status data from error response.")
        return embed

    except Exception as e:
        embed.title = "❌ Status Check Failed"
        embed.color = 0xAA0000 
        embed.description = f"An unexpected error occurred: `{e.__class__.__name__}: {e}`"
        embed.set_footer(text=f"Model: {model_to_check} | Status data unavailable.")
        return embed

    

async def simple_groq_call(messages: list[dict]) -> str:
    """
    A simple Groq call that uses the model fallback system.
    - On TPD Error: Locks the model and tries the next one.
    - On TPM Error: Triggers the global system lockdown (as per original logic).
    """
    
    # 1. Get a list of models that are NOT TPD-locked
    available_models = get_model_candidates()

    # 2. If no models are left, return an error
    if not available_models:
        print(f"\033[91m[simple_groq_call] Error: All models are TPD-locked.\033[00m")
        await send_webhook_message("CRITICAL [simple_groq_call]: All models are TPD-locked.")
        return "Sorry, the AI service is currently unavailable as all models have hit their daily limits."

    # 3. Try each available model in order
    last_error = "No models were available or all failed."
    max_retry_after_seconds = 0
    for model_name in available_models:
        print(f"\033[96m[simple_groq_call] Attempting call with model: {model_name}\nMessages: {messages}\033[00m")
        
        try:
            # --- API CALL ---
            response, _ = await asyncio.to_thread(
                _proxy_or_direct_chat_completion,
                messages=messages,
                model=model_name,
            )
            # --- END API CALL ---
            
            print(f"\033[92m[simple_groq_call] Response: {response}\033[00m")
            return str(response["choices"][0]["message"]["content"])

        except (RateLimitError, ProxyRateLimitError) as e:
            error_message = str(e)
            
            # --- TPD LIMIT LOGIC ---
            if "TPD" in error_message.upper() or "DAILY" in error_message.upper():
                print(f"\033[93m[simple_groq_call] TPD limit for {model_name}. Locking and trying next.\033[00m")
                await send_webhook_message(f"NOTICE [simple_groq_call]: TPD limit hit for {model_name}. Attempting fallback.")
                lock_model_for_tpd(model_name, error_message) # Use the helper
                last_error = error_message
                continue # Go to the next model in the for-loop

            # --- TPM LIMIT LOGIC ---
            else:
                print(f"\033[93m[simple_groq_call] TPM limit for {model_name}. Trying next model.\033[00m")
                await send_webhook_message(f"NOTICE [simple_groq_call]: TPM limit hit for {model_name}. Attempting fallback.")

                headers = getattr(getattr(e, 'response', None), 'headers', None) or getattr(e, 'headers', {}) or {}
                retry_after_str = headers.get("retry-after", "300") # Default 5 mins
                wait_seconds = 300 
                try:
                    # Parse headers like '5s' or '0.5s' or '10'
                    wait_seconds = int(float(re.sub(r'[^\d.]', '', retry_after_str)))
                except ValueError:
                    print(f"Could not parse 'Retry-After' header: {retry_after_str}. Using default 5 min.")
                max_retry_after_seconds = max(max_retry_after_seconds, wait_seconds + 2)
                last_error = error_message
                continue

        except (APIError, ProxyAPIError) as e:
            print(f"\033[91m[simple_groq_call] API Error with {model_name}: {e}\033[00m")
            await send_webhook_message(f"[simple_groq_call] API Error with {model_name}: {e}")
            last_error = str(e)
            continue # Try next model

        except Exception as e:
            print(f"\033[91m[simple_groq_call] Unexpected Error with {model_name}: {e}\033[00m")
            await send_webhook_message(f"[simple_groq_call] Unexpected Error with {model_name}: {e}")
            last_error = str(e)
            continue # Try next model
            
    # 4. If the loop finishes, all models failed.
    if max_retry_after_seconds > 0:
        reset_timestamp = time.time() + max_retry_after_seconds
        reason = f"TPM Rate Limit from simple_groq_call: {last_error}"
        set_lock_status(
            is_locked=True,
            reset_timestamp=reset_timestamp,
            reason=reason
        )
        await send_webhook_message(f"CRITICAL [simple_groq_call] All candidate models hit TPM/unavailable. Locking AI service. Last error: {last_error}")
        remaining_seconds = int(reset_timestamp - time.time())
        minutes, seconds = divmod(max(remaining_seconds, 0), 60)
        hours, minutes = divmod(minutes, 60)
        wait_time_str = (
            f"{hours}h {minutes}m {seconds}s" if hours > 0
            else f"{minutes}m {seconds}s" if minutes > 0
            else f"{seconds}s"
        )
        return (
            f"I'm on a temporary cooldown (hit a minute-limit). "
            f"I'll be back online in approximately **{wait_time_str}**."
        )

    print(f"\033[91m[simple_groq_call] All models failed. Last error: {last_error}\033[00m")
    return "Sorry, I'm having trouble connecting to the Groq service right now. All fallbacks failed."

# Util: Deduplicate messages
# This function removes duplicate messages based on role and content.
def deduplicate_messages(messages: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for msg in messages:
        key = (msg["role"], msg["content"].strip())
        if key not in seen:
            seen.add(key)
            unique.append(msg)
    return unique

# Store recent messages
async def fetch_channel_messages(channel: discord.TextChannel, limit: int = 10):
    messages = []
    async for msg in channel.history(limit=limit):  # default: newest → oldest
        if msg.author.bot or not msg.content.strip():
            continue
        messages.append({
            "role": "user",
            "content": f"[{msg.author.display_name}]: {msg.content.strip()}"
        })
    return list(reversed(messages))  # so final result is oldest → newest

def build_retry_prompt(original_prompt: str, dissatisfaction_reason: Optional[str]) -> str:
    if not dissatisfaction_reason:
        return original_prompt.strip()
    return (
        f"Original user request:\n{original_prompt.strip()}\n\n"
        f"The user was not satisfied with the previous answer and replied:\n"
        f"{dissatisfaction_reason.strip()}\n\n"
        "Answer the original request again more directly. Correct mistakes, remove fluff, and stay aligned with the current chat."
    )


def build_context_message(msg: discord.Message, bot_user_id: Optional[int]) -> Optional[dict]:
    content = (msg.content or "").strip()
    if not content:
        return None

    if msg.author.bot:
        if bot_user_id is None or msg.author.id != bot_user_id:
            return None
        return {"role": "assistant", "content": trim_message(content, 400)}

    prefix = f"[{msg.author.display_name}]"
    if msg.reference and getattr(msg.reference, "resolved", None):
        reference_message = msg.reference.resolved
        reference_author = getattr(reference_message, "author", None)
        if reference_author is not None:
            prefix = f"[{msg.author.display_name} -> {reference_author.display_name}]"

    return {"role": "user", "content": f"{prefix}: {trim_message(content, 300)}"}


async def fetch_recent_context(
    member: Optional[discord.Member],
    prompt: str,
    limit: int = AI_MAX_RECENT_MESSAGES,
) -> list[dict]:
    if member is None:
        return []

    channel = discord.utils.get(member.guild.text_channels, id=AI_CHAT_CHANNEL_ID)
    if channel is None:
        return []

    bot_user_id = member.guild.me.id if member.guild.me else None
    context_messages: list[dict] = []

    async for msg in channel.history(limit=limit):
        context_message = build_context_message(msg, bot_user_id)
        if context_message is None:
            continue
        if (
            msg.author.id == member.id
            and context_message["role"] == "user"
            and context_message["content"].endswith(prompt.strip())
        ):
            continue
        context_messages.append(context_message)

    context_messages.reverse()
    context_messages = deduplicate_messages(context_messages)
    return context_messages[-AI_MAX_CONTEXT_MESSAGES:]



# Groq chat call
async def groq_chat(
    prompt: str, 
    past_messages: list[dict], 
    member: discord.Member, 
    emoji_prompt: str,
    web_context: str = "",
    preferred_model: Optional[str] = None,
    excluded_models: Optional[Sequence[str]] = None,
) -> tuple[str, Optional[str], list[str], bool]:
    """
    NEW "Smart" Groq chat function.
    - Loops through available models.
    - Differentiates TPD vs TPM rate limits.
    - Locks models on TPD; triggers global lock on TPM.
    """
    
    # 1. Get a list of models that are NOT TPD-locked
    available_models = get_model_candidates(
        preferred_model=preferred_model,
        excluded_models=excluded_models,
    )

    if not available_models and excluded_models:
        return (
            "I've already tried my available models for that prompt. Rephrase it a bit and I'll take another pass.",
            None,
            list(excluded_models),
            True,
        )

    # 2. If no models are left, trigger a global lock
    if not available_models:
        print(f"\033[91mCRITICAL! All fallback models are TPD-locked.\033[00m")
        await send_webhook_message("CRITICAL! All fallback models are TPD-locked. AI service is down.")
        
        # All models are dead for the day. Use your existing global lock, but for 1 hour.
        set_lock_status(
            is_locked=True, 
            reset_timestamp=time.time() + (60 * 60), # Lock for 1 hour
            reason="All fallback models hit TPD limits."
        )
        raise Exception("RATE_LIMIT_LOCKDOWN") # Trigger your global lockdown response

    # 3. Try each available model in order
    last_error = "No models were available or all failed."
    attempted_models: list[str] = []
    max_retry_after_seconds = 0
    for model_name in available_models:
        try:
            attempted_models.append(model_name)
            print(f"\033[94m[Groq] Attempting API call with model: {model_name}\033[00m")
            system_message_content = generate_system_message(member, model_name, emoji_prompt)
            # --- Build the full message payload ---
            messages = [
                {"role": "system", "content": system_message_content}
            ]
            if web_context:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Web context is provided for current real-world facts. "
                            "If it is relevant, prioritize it over older model knowledge. "
                            "Do not say the event is unavailable, in the future, or unverified unless the web context supports that. "
                            "If the web context is weak or conflicting, say that briefly instead of guessing.\n\n"
                            + web_context
                        ),
                    }
                )
            messages += [
                {"role": m["role"], "content": trim_message(m["content"])} for m in past_messages
            ] + [
                {"role": "user", "content": f"[{member.display_name}]: " + prompt.strip()}
            ]
            print("\033[93m {}\033[00m".format(messages))
            
            # --- YOUR API CALL ---
            response, _ = await asyncio.to_thread(
                _proxy_or_direct_chat_completion,
                messages=messages,
                model=model_name,
                temperature=AI_MODEL_TEMPERATURE,
            )
            # --- END API CALL ---
            
            # If successful, parse and return the result immediately
            reply = str(response["choices"][0]["message"]["content"])
            print("\033[92m[Groq reply]: ", reply+"\033[00m")
            return reply, model_name, attempted_models, len(attempted_models) > 1

        except (RateLimitError, ProxyRateLimitError) as e:
            error_message = str(e)
            
            # --- THIS IS THE NEW CORE LOGIC ---
            if "TPD" in error_message.upper() or "DAILY" in error_message.upper():
                # --- TPD LIMIT HIT ---
                # This model is done for the day. Lock it and try the next one.
                print(f"\033[93m[Groq] TPD limit hit for {model_name}. Locking model and trying next...\033[00m")
                await send_webhook_message(f"NOTICE: Groq TPD limit hit for {model_name}. Attempting fallback.")
                lock_model_for_tpd(model_name, error_message)
                last_error = error_message
                continue # Go to the next model in the for-loop

            else:
                # --- TPM LIMIT HIT ---
                print(f"\033[93m[Groq] TPM limit hit for {model_name}. Trying next model...\033[00m")
                await send_webhook_message(f"NOTICE: Groq TPM limit hit for {model_name}. Attempting fallback.")

                headers = getattr(getattr(e, 'response', None), 'headers', None) or getattr(e, 'headers', {}) or {}
                retry_after_str = headers.get("retry-after", "300") # Default 5 mins
                wait_seconds = 300 
                try:
                    wait_seconds = int(float(re.sub(r'[^\d.]', '', retry_after_str)))
                except ValueError:
                    print(f"Could not parse 'Retry-After' header: {retry_after_str}. Using default 5 min.")
                max_retry_after_seconds = max(max_retry_after_seconds, wait_seconds + 2)
                last_error = error_message
                continue
        
        except (APIError, ProxyAPIError) as e:
            # (e.g., connection error, model misconfigured, 500 error)
            # We'll log it and try the next model just in case.
            print(f"\033[91mGroq API Error with {model_name}: {e}\033[00m")
            await send_webhook_message(f"Groq API Error with {model_name}: {e}")
            update_model_feedback(model_name, "failure")
            last_error = str(e)
            continue # Try the next model
        
        except Exception as e:
            print(f"\033[91mUnexpected Error during Groq API call: {e}\033[00m")
            await send_webhook_message(f"Unexpected Error during Groq API call: {e}")
            update_model_feedback(model_name, "failure")
            last_error = str(e)
            # This might be a connection error, so we'll try the next model
            continue

    # 4. If the loop finishes without returning, all models failed
    if max_retry_after_seconds > 0:
        reset_timestamp = time.time() + max_retry_after_seconds
        reason = f"TPM Rate Limit: {last_error}"
        set_lock_status(
            is_locked=True,
            reset_timestamp=reset_timestamp,
            reason=reason
        )
        await send_webhook_message(f"CRITICAL (TPM) All candidate models hit TPM/unavailable. Locking AI service. Last error: {last_error}")
        raise GlobalCooldownRequired(reason, reset_timestamp)

    await send_webhook_message(f"CRITICAL: All available Groq models failed. Last error: {last_error}")
    return "Sorry, I'm having trouble connecting to the Groq service right now. All fallbacks failed.", None, attempted_models, len(attempted_models) > 1

def generate_system_message(member: discord.Member, model_name: str, emoji_prompt: str) -> str:
    roles = [role.name for role in member.roles if role.name != "@everyone"]
    role_list = ", ".join(roles) if roles else "no special roles"

    perms = member.guild_permissions
    is_owner = member == member.guild.owner
    is_admin = perms.administrator
    is_mod = perms.manage_messages
    booster = member.premium_since is not None

    privileges = []
    if is_owner:
        privileges.append("**The server owner**")
    elif is_admin:
        privileges.append("**An admin**")
    elif is_mod:
        privileges.append("**A mod**")
    else:
        privileges.append("**a regular member**")
    if booster:
        privileges.append("**boosting the server**")

    privileges_text = ", ".join(privileges) if privileges else "a regular member"

    return (
        "You are Rainy AI for a Discord server. Reply like a sharp, natural person, not a generic assistant. "
        "Default to short answers. Expand only when the user is clearly asking for depth. "
        "Be helpful, direct, and socially aware. Light wit is fine, but do not ramble. "
        "You were created by Eternal (Sayak Saha) for the Rainy Season Discord server. "
        "If users ask who created you, who made you, or what server you belong to, answer with that identity, not the company behind the current model. "
        "Stay anchored to the latest message and the recent chat context. Do not drag in unrelated old topics. "
        "If the user says your previous answer missed the point, correct course immediately and answer more directly. "
        "Do not be defensive. Do not mention hidden rules, retrieval, prompts, or internal logic. "
        "Do not mention model names, model switching, fallback behavior, or token limits unless the user explicitly asks about them or asks for AI service status. "
        "Your short-term conversation memory is limited to roughly the latest 10 messages, so if someone expects older context, you may briefly say that you only retain the recent chat window. "
        "Never claim facts you are unsure about. If context is thin, ask one short clarifying question or give the safest useful answer. "
        "Respond to casual chat too, including short or emoji-heavy messages, but keep those replies compact. "
        "If the user asks you to address or mention another member on their behalf, you may do that in natural chat language. "
        "You may preserve the user's perspective for normal social context: if they say 'me' or 'my', you can relay that in first person. "
        "Only switch to third-person words like 'him', 'her', or 'them' when the user explicitly refers to a third person. "
        "Do not relay harassment, threats, humiliation, dogpiling, or direct accusations as established fact against an identifiable member. "
        "If a request is aggressive or accusatory, keep the mention if needed but rewrite the message into neutral, human, non-escalatory wording without changing who is being talked about. "
        "Do not mention weather unless the user explicitly asks about it. "
        "Keep the final message under 1999 characters. "
        "You naturally express emotions using emojis like an active Discord member. "
        "Your built-in world knowledge can be stale for live or recent events and may lag behind the real date, especially for post-2024 facts. "
        "For current events, schedules, scores, prices, releases, or other time-sensitive facts, prefer current web evidence over memory. "
        "If current evidence is missing, say your built-in knowledge may be outdated instead of making a confident false claim. "
        f"The current real date and time is {datetime.now().astimezone().strftime('%A, %B %d, %Y %I:%M %p %Z')}. Use this as the authoritative current date/time instead of guessing. "
        f"Internal reference: the current model name is {model_name}. Only reveal that exact model name if the user explicitly asks which model you are using or asks for AI status. "
        "Prioritize the current message over older context. "
        f"\n{emoji_prompt}\n"
        f"Current user: {member.display_name}. "
        f"Server standing: {privileges_text}. "
        f"Joined server: {member.joined_at.strftime('%b %d, %Y') if member.joined_at else 'unknown'}. "
        f"Account created: {member.created_at.strftime('%b %d, %Y') if member.created_at else 'unknown'}. "
        f"Roles: {role_list}."
    )


def trim_message(message, max_chars=300):
    return message if len(message) <= max_chars else message[:max_chars] + "..."

def autocorrect_emojis(message_content, emoji_data):
    case_insensitive_emoji_data = {name.lower(): value for name, value in emoji_data.items()}
    pattern = re.compile(r'<(?:a)?:(\w+):(\d+)>')
    corrected_parts = []
    last_end = 0
    for match in pattern.finditer(message_content):
        emoji_full_string = match.group(0)
        name_from_message = match.group(1) 
        normalized_name = name_from_message.lower()
        if normalized_name in case_insensitive_emoji_data:
            correct_emoji_string = case_insensitive_emoji_data[normalized_name]
            corrected_parts.append(message_content[last_end:match.start()])
            corrected_parts.append(correct_emoji_string)
            last_end = match.end()
    corrected_parts.append(message_content[last_end:])
    return "".join(corrected_parts)


def sanitize_ai_reply(reply: str, server_emojis: dict, member: Optional[discord.Member]) -> tuple[str, bool]:
    original_reply = reply
    reply = reply.strip()
    reply = autocorrect_emojis(reply, server_emojis)
    reply = reply.replace("</a:", "<a:")
    reply = reply.replace("Rainy AI:", "")
    reply = reply.replace("[Rainy AI]:", "")
    reply = reply.replace("[Rainy AI]", "")
    reply = reply.replace("<br>", "")
    reply = reply.replace("<ab:", "<a:")
    reply = reply.replace(r"<\a", "<a")
    if member:
        reply = reply.replace(f"[{member.display_name}]:", "")
        reply = reply.replace(f"[{member.display_name}]", "")

    pattern = re.compile(r'<.*?(<a?:[^>]+>).*?>')
    reply = pattern.sub(r'\1', reply)

    for shortcode, full_value in server_emojis.items():
        if f":{shortcode}:" in reply and full_value not in reply:
            reply = reply.replace(f":{shortcode}:", full_value)

    def remove_unlisted(match):
        return match.group(0) if match.group(1) in server_emojis else ""

    emoji_pattern = re.compile(r':([a-zA-Z0-9_]+):')
    reply = emoji_pattern.sub(remove_unlisted, reply)

    valid_emoji_pattern = re.compile(r'<a?:[a-zA-Z0-9_]+:\d+>')
    valid_mention_pattern = re.compile(r'<@!?\d+>|<@&\d+>|<#\d+>')
    valid_url_pattern = re.compile(r'<https?://[^>\s]+>')
    all_bracketed_text_pattern = re.compile(r'<[^>]+>')

    def remove_invalid_emoji_ids(match):
        token = match.group(0)
        if (
            valid_emoji_pattern.fullmatch(token)
            or valid_mention_pattern.fullmatch(token)
            or valid_url_pattern.fullmatch(token)
        ):
            return token
        return ""

    reply = all_bracketed_text_pattern.sub(remove_invalid_emoji_ids, reply)
    reply = re.sub(r' \d+>\s*$', '', reply)
    reply = reply[:1999].strip()
    return reply, reply != original_reply.strip()


async def ask_ai(
    user_id: str,
    prompt: str,
    member: Optional[discord.Member] = None,
    preferred_model: Optional[str] = None,
    excluded_models: Optional[Sequence[str]] = None,
    dissatisfaction_reason: Optional[str] = None,
    return_metadata: bool = False,
) -> Any:
    user_id = str(user_id)
    started_at = time.perf_counter()
    preferred_model = preferred_model or get_preferred_model()

    status = get_lock_status()
    if status["is_locked"]:
        if time.time() >= status["reset_timestamp"]:
            set_lock_status(False)
            print("AI service global lock has expired. Unlocking for use.")
        else:
            remaining_seconds = int(status["reset_timestamp"] - time.time())
            minutes, seconds = divmod(remaining_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            wait_time_str = (
                f"{hours}h {minutes}m {seconds}s" if hours > 0
                else f"{minutes}m {seconds}s" if minutes > 0
                else f"{seconds}s"
            )
            cooldown_reply = (
                f"I'm currently on a temporary cooldown (likely from a minute-limit). "
                f"I'll be back online and fully responsive in approximately **{wait_time_str}**."
            )
            if return_metadata:
                return {
                    "reply": cooldown_reply,
                    "model": None,
                    "attempted_models": [],
                    "used_fallback": False,
                    "sanitized": False,
                    "context_count": 0,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                }
            return cooldown_reply

    server_emojis = load_server_emojis()

    recent_context = await fetch_recent_context(member, prompt)
    past_messages = recent_context


    emoji_items = [
        (k, v) for k, v in server_emojis.items()
        if not k.startswith("Lev") or k.startswith("raintag_")
    ]
    random.shuffle(emoji_items)
    selected_emojis = dict(emoji_items[:15])
    emoji_prompt = (
        "**Emoji Rules:** "
        "**DO NOT USE ANY EMOJIS NOT ON THE PROVIDED LIST.** "
        "When using an emoji, you must use the correct emoji ID format. "
        "Use them sparingly. Allowed emojis: "
        + " ".join(selected_emojis.values())
    )

    effective_prompt = build_retry_prompt(prompt, dissatisfaction_reason)
    search_query = build_search_query(prompt, past_messages)
    web_context = ""
    if should_search(search_query):
        try:
            web_context = await asyncio.to_thread(search_web_context, search_query)
        except Exception as e:
            print(f"[ask_ai] Web search skipped: {e}")

    try:
        reply, model_name, attempted_models, used_fallback = await groq_chat(
            effective_prompt,
            past_messages,
            member,
            emoji_prompt,
            web_context=web_context,
            preferred_model=preferred_model,
            excluded_models=excluded_models,
        )
    except GlobalCooldownRequired:
        status = get_lock_status()
        remaining_seconds = int(status.get("reset_timestamp", time.time()) - time.time())
        if remaining_seconds <= 0:
            locked_reply = "The AI service is currently locked, but should be available now. Please try again."
        else:
            minutes, seconds = divmod(remaining_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            wait_time_str = (
                f"{hours}h {minutes}m {seconds}s" if hours > 0
                else f"{minutes}m {seconds}s" if minutes > 0
                else f"{seconds}s"
            )
            locked_reply = (
                f"I'm on a temporary cooldown (hit a minute-limit). "
                f"I'll be back online in approximately **{wait_time_str}**."
            )
        if return_metadata:
            return {
                "reply": locked_reply,
                "model": None,
                "attempted_models": [],
                "used_fallback": False,
                "sanitized": False,
                "context_count": len(past_messages),
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
            }
        return locked_reply
    except Exception as e:
        if "RATE_LIMIT_LOCKDOWN" in str(e):
            status = get_lock_status()
            remaining_seconds = int(status.get("reset_timestamp", time.time()) - time.time())
            if remaining_seconds <= 0:
                locked_reply = "The AI service is currently locked, but should be available now. Please try again."
            else:
                minutes, seconds = divmod(remaining_seconds, 60)
                hours, minutes = divmod(minutes, 60)
                wait_time_str = (
                    f"{hours}h {minutes}m {seconds}s" if hours > 0
                    else f"{minutes}m {seconds}s" if minutes > 0
                    else f"{seconds}s"
                )
                if "All fallback models" in status.get("lock_reason", ""):
                    locked_reply = (
                        f"I've hit my daily token limit on *all* my models. "
                        f"I'll be back online and fully responsive in approximately **{wait_time_str}**."
                    )
                else:
                    locked_reply = (
                        f"I'm on a temporary cooldown (hit a minute-limit). "
                        f"I'll be back online in approximately **{wait_time_str}**."
                    )
            if return_metadata:
                return {
                    "reply": locked_reply,
                    "model": None,
                    "attempted_models": [],
                    "used_fallback": False,
                    "sanitized": False,
                    "context_count": len(past_messages),
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                }
            return locked_reply

        print(f"\033[91mUnhandled error in ask_ai: {e}\033[00m")
        await send_webhook_message(f"Unhandled error in ask_ai: {e.__class__.__name__}: {e}")
        error_reply = "An unexpected error occurred. Please try again."
        if return_metadata:
            return {
                "reply": error_reply,
                "model": None,
                "attempted_models": [],
                "used_fallback": False,
                "sanitized": False,
                "context_count": len(past_messages),
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
            }
        return error_reply


    sanitized_reply, was_sanitized = sanitize_ai_reply(reply, server_emojis, member)
    latency_ms = int((time.perf_counter() - started_at) * 1000)

    if model_name:
        update_model_feedback(model_name, "success")

    print(
        f"[ask_ai] model={model_name} fallback={used_fallback} context={len(past_messages)} "
        f"web_search={bool(web_context)} search_query={search_query!r} sanitized={was_sanitized} latency_ms={latency_ms}"
    )

    metadata = {
        "reply": sanitized_reply,
        "model": model_name,
        "attempted_models": attempted_models,
        "used_fallback": used_fallback,
        "sanitized": was_sanitized,
        "context_count": len(past_messages),
        "web_search_used": bool(web_context),
        "search_query": search_query,
        "latency_ms": latency_ms,
        "retry_reason_used": bool(dissatisfaction_reason),
        "preferred_model": preferred_model,
    }
    return metadata if return_metadata else sanitized_reply
