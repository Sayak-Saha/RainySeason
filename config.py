from pymongo import MongoClient
from pymongo.errors import PyMongoError
from mongo import MONGO_URI  #mongo.example.py is a template for the mongo.py file, which contains the actual MongoDB URI. Make sure to replace "YOUR_MONGODB_URI_HERE" in mongo.example.py with your actual MongoDB URI and rename it to mongo.py.

_client = None
try:
    _client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=3000
    )
    _client.admin.command("ping")
    _doc = _client["secure_config"]["secrets"].find_one(
        {"_id": "app_config"}
    )

    if not _doc:
        raise ValueError("Secret config document not found")

    BOT_TOKEN = _doc["BOT_TOKEN"]
    GROQ_API_KEY = _doc.get("GROQ_API_KEY")
    GROQ_PROXY_URL = _doc.get("GROQ_PROXY_URL")
    TOKEN = _doc["TOKEN"]
    WEBHOOK_URL = _doc["WEBHOOK_URL"]

except (PyMongoError, ValueError) as e:
    print(f"Failed to load secrets: {e}")
    raise

finally:
    if _client is not None:
        _client.close()
        
GUILD_ID=1369397376175050863
ROLE_ID=1374830565764894870
TARGET_BADGE="40ba0d7db664fec35a2acd27334e574d"
APPLICATION_ID=1026022417363124255
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_MODEL_PRIORITY_LIST = [
    "openai/gpt-oss-20b",                       # 200K TPD (lower limit, but a good final fallback)
    "llama-3.1-8b-instant",                  # 500K TPD
    "qwen/qwen3-32b",                          # 500K TPD
    "meta-llama/llama-4-scout-17b-16e-instruct" # 500K TPD
]
AI_CHAT_CHANNEL_ID = 1397670643167658145
AI_MAX_RECENT_MESSAGES = 11
AI_MAX_CONTEXT_MESSAGES = 10
AI_DISSATISFACTION_WINDOW_SECONDS = 45
AI_AUTO_RETRY_MAX_PER_MESSAGE = 1
AI_MODEL_TEMPERATURE = 0.85
AI_RETRY_ACTION_COOLDOWN_SECONDS = 15
AI_RETRY_ACTION_WINDOW_SECONDS = 180
AI_RETRY_ACTION_MAX_PER_WINDOW = 4
AI_STATUS_GUILD_COOLDOWN_SECONDS = 20
AI_STATUS_REFRESH_GUILD_COOLDOWN_SECONDS = 15
#for App.py
EXCLUDED_CHANNEL_IDS = [
    1381242084106964992, #wordle
    1369398533802692618, #rules
    1370711095937073225, #guide
    1369658127934427217, #mod
    1369398533802692621, #modlog
    1370702300951220246, #automod
    1374101143034134529, #automode-name
    1369423517870981230, #Boosters
    1369637441841004625, #polls
    1370368688762523688, #raincolor
    1369398707866304513, #forcast
    1423367782736724128, #rainyailog
    1423710098651349052, #deploylog
    1371087314939416616, #create tickets
    1371088937413644298, #Tickets Archive
    1369693594000167003, #identity role channel
    1369497844544704543, #freebies
    1369517456602824805, #raintags channel
    1430896438728065207, #tags list
    1376508927159570495 #supporters
]
rainkeeper_id = 1374830565764894870
invalid_message_starts = ("/", "!", ".", "Owo ", "Wh ", "Wb ", "W ", "pls ", "M!")
MESSAGE_STATUS_COOLDOWN=10
GROW_TREE_CHANNEL_ID=1430542819847835781
GROW_TREE_MESSAGE_ID=1431659447599497399
GROW_TREE_REACTIONROLE_MESSAGE_ID=1430593222660325467
print("Config loaded successfully.")
