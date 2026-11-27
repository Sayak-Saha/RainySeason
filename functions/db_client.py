import motor.motor_asyncio
from pymongo import MongoClient
from config import MONGO_URI

_async_client = None
_sync_client = None


def get_async_mongo_client() -> motor.motor_asyncio.AsyncIOMotorClient:
    """Return the shared AsyncIOMotorClient instance."""
    global _async_client
    if _async_client is None:
        if not MONGO_URI:
            raise ValueError("MONGO_URI is not configured.")
        _async_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    return _async_client


def get_sync_mongo_client() -> MongoClient:
    """Return the shared synchronous PyMongo MongoClient instance."""
    global _sync_client
    if _sync_client is None:
        if not MONGO_URI:
            raise ValueError("MONGO_URI is not configured.")
        _sync_client = MongoClient(MONGO_URI)
    return _sync_client
